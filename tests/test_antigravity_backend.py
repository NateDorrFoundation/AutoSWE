"""Tests for autoswe.harness.backends.antigravity — AntigravityBackend.

Phase 1 unit tests (the full AntigravityFake arrives in Phase 3).  Parsing
tests replay the committed Phase 0 fixtures under tests/fixtures/antigravity/
through the real _parse_line -> _finalize path; subprocess tests patch
``asyncio.create_subprocess_exec`` with a minimal stub that replays canned
stdout.  No live agy calls.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import autoswe.harness.backends.antigravity as agy_mod
from autoswe.harness.backends.antigravity import (
    ANTIGRAVITY_COMMENT_TOOL_NAMES,
    AntigravityBackend,
    _AgyAccumulator,
    _build_argv,
    _classify_mcp_comment_call,
    _finalize,
    _kill_tree,
    _mcp_comment_active,
    _parse_line,
    _resolve_agy_executable,
    _tool_label,
    _user_event,
)
from autoswe.harness.backends.base import (
    CLAUDE_COMMENT_TOOL_NAMES,
    CodingBackend,
    RunResult,
    RunSpec,
)

MODEL = "qwen3.8-flash-next"
FIXTURES = Path(__file__).parent / "fixtures" / "antigravity"

# An existing file used as a fake resolved exe where a cli_path is needed
# (must exist on disk: _resolve_agy_executable existence-checks plain paths).
FAKE_EXE = str(FIXTURES / "README.md")


# ---------- Helpers ----------


def _spec(**overrides) -> RunSpec:
    defaults = {
        "prompt": "test prompt",
        "cwd": "/tmp/repo",
        "model": MODEL,
        "cli_path": FAKE_EXE,
        "state": {"_harness_cfg": {"backend": "antigravity", "model": MODEL}},
    }
    defaults.update(overrides)
    return RunSpec(**defaults)


def _fixture_text(name: str) -> str:
    return (FIXTURES / f"{name}.jsonl").read_text(encoding="utf-8")


def _parse_fixture(name: str) -> tuple[_AgyAccumulator, list[str]]:
    """Replay a committed fixture through the real parser; return (acc, progress)."""
    acc = _AgyAccumulator()
    progress: list[str] = []
    for line in _fixture_text(name).splitlines():
        _parse_line(line, acc, progress.append)
    return acc, progress


class _MockStdin:
    def __init__(self):
        self.data = b""
        self.closed = False

    def write(self, b: bytes) -> None:
        self.data += b

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _MockStdout:
    """Line-oriented stdout; models the StreamReader limit like pi tests do."""

    def __init__(self, data: bytes, limit: int | None = None, hang=None):
        self._lines = data.splitlines(keepends=True)
        self._pos = 0
        self._buf = data
        self._bpos = 0
        self._limit = limit
        self._hang = hang

    async def readline(self) -> bytes:
        if self._hang is not None:
            await self._hang.wait()
        if self._pos < len(self._lines):
            line = self._lines[self._pos]
            self._pos += 1
            if self._limit is not None and len(line) > self._limit:
                raise ValueError("Separator is not found, and chunk exceed the limit")
            return line
        return b""

    async def read(self, size: int = -1) -> bytes:
        out = self._buf[self._bpos:self._bpos + size]
        self._bpos += len(out)
        return out

    @property
    def drained(self) -> bool:
        return self._bpos >= len(self._buf)


class _MockStderr:
    def __init__(self, data: bytes, hang=None):
        self._data = data
        self._pos = 0
        self._hang = hang

    async def read(self, size: int = -1) -> bytes:
        if self._hang is not None:
            await self._hang.wait()
        out = self._data[self._pos:self._pos + size]
        self._pos += len(out)
        return out


class _MockProcess:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, limit=None, hang=None):
        self.stdin = _MockStdin()
        self.stdout = _MockStdout(stdout, limit=limit, hang=hang)
        self.stderr = _MockStderr(stderr, hang=hang)
        self.returncode = returncode
        self.pid = 4242
        self.killed = False

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        self.killed = True


def _full_run(spec: RunSpec, proc: _MockProcess):
    """Run the real backend under a stubbed subprocess; return (result, captured)."""
    captured: dict = {}

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        captured["env"] = dict(kwargs.get("env") or {})
        captured["kwargs"] = kwargs
        return proc

    with patch("asyncio.create_subprocess_exec", new=fake_exec):
        result = asyncio.run(AntigravityBackend().run(spec))
    return result, captured


def _schema_path_from(argv: list[str]) -> str | None:
    if "--json-schema" in argv:
        return argv[argv.index("--json-schema") + 1]
    return None


def _settings_path_from(argv: list[str]) -> str | None:
    if "--settings" in argv:
        return argv[argv.index("--settings") + 1]
    return None


# ---------- argv / command construction ----------


def test_build_argv_fresh_shape():
    cmd = _build_argv(_spec(timeout=42), {}, None)
    # [:7] — the fixed head is 7 tokens (flag/value pairs + --model <model>).
    assert cmd[:7] == [
        "--input-format", "stream-json", "--output-format", "stream-json",
        "--disable-slash-commands", "--model", MODEL,
    ]
    assert "--print-timeout" in cmd
    assert cmd[cmd.index("--print-timeout") + 1] == "42s"
    assert "--conversation" not in cmd
    assert "--json-schema" not in cmd
    # Dedicated-machine posture: skip flag on by default.
    assert "--dangerously-skip-permissions" in cmd


def test_build_argv_always_disables_slash_commands():
    for cfg in ({}, {"skip_permissions": False}, {"sandbox": True}):
        cmd = _build_argv(_spec(), cfg, None)
        assert "--disable-slash-commands" in cmd


def test_build_argv_resume_adds_conversation():
    cmd = _build_argv(_spec(resume="conv-abc"), {}, None)
    assert cmd[cmd.index("--conversation") + 1] == "conv-abc"


def test_build_argv_fork_session_treated_as_resume():
    with patch.object(agy_mod, "log") as mock_log:
        cmd = _build_argv(_spec(resume="conv-abc", fork_session=True), {}, None)
    # Treated as resume: --conversation present, no fork-specific flag.
    assert cmd[cmd.index("--conversation") + 1] == "conv-abc"
    assert not any("fork" in a.lower() for a in cmd)
    assert any("fork_session ignored" in str(c.args[0]) for c in mock_log.call_args_list)


def test_prompt_never_in_argv():
    sentinel = "SENTINEL-prompt-body-never-in-argv"
    proc = _MockProcess(stdout=b"")
    result, captured = _full_run(_spec(prompt=sentinel), proc)
    assert not any(sentinel in a for a in captured["argv"])
    # ...but it did reach the CLI via stdin.
    assert sentinel.encode() in proc.stdin.data


def test_build_argv_profile_flags_map():
    cfg = {
        "effort": "high",
        "agent": "planner",
        "sandbox": True,
        "log_dir": "/tmp/agy-logs",
        "skip_permissions": False,
    }
    cmd = _build_argv(_spec(), cfg, None)
    assert cmd[cmd.index("--effort") + 1] == "high"
    assert cmd[cmd.index("--agent") + 1] == "planner"
    assert "--sandbox" in cmd
    log_file = cmd[cmd.index("--log-file") + 1]
    assert log_file.startswith("/tmp/agy-logs" + os.sep) and log_file.endswith(".log")
    assert "--dangerously-skip-permissions" not in cmd


def test_json_schema_flag_only_with_output_format_and_temp_removed(tmp_path):
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    proc = _MockProcess(stdout=_fixture_text("basic_success").encode())
    result, captured = _full_run(_spec(output_format={"type": "json_schema", "schema": schema}), proc)
    schema_path = _schema_path_from(captured["argv"])
    assert schema_path and schema_path.endswith(".json")
    # Reclaimed after the run — including its content was written beforehand:
    assert not os.path.exists(schema_path)
    assert result.subtype == "success"

    # Without output_format: no flag at all.
    proc2 = _MockProcess(stdout=_fixture_text("basic_success").encode())
    _, captured2 = _full_run(_spec(), proc2)
    assert _schema_path_from(captured2["argv"]) is None


# ---------- stdin transport ----------


def test_stdin_single_event_roundtrip():
    prompt = 'quote " and \\ backslash \n newline, non-ASCII: é✓漢\n' + "x" * 20_000
    proc = _MockProcess(stdout=b"")
    _full_run(_spec(prompt=prompt), proc)
    data = proc.stdin.data
    # Exactly one line: the only real newline is the record terminator.
    assert data.endswith(b"\n") and data.count(b"\n") == 1
    event = json.loads(data.decode("utf-8"))
    assert event["event"] == "user"
    assert event["message"]["content"] == prompt
    assert proc.stdin.closed


def test_user_event_shape():
    data = _user_event("hi")
    assert data == b'{"event": "user", "message": {"content": "hi"}}\n'


# ---------- executable resolution ----------


def test_resolve_direct_path(tmp_path):
    exe = tmp_path / "agy.exe"
    exe.write_bytes(b"")
    assert _resolve_agy_executable(str(exe)) == (str(exe), [])


def test_resolve_cmd_bat_shims():
    for name in ("agy.cmd", "AGY.BAT"):
        exe, prefix = _resolve_agy_executable(f"C:\\tools\\{name}")
        assert exe == "cmd"
        assert prefix == ["/c", f"C:\\tools\\{name}"]


def test_resolve_ps1_shim():
    exe, prefix = _resolve_agy_executable("C:\\tools\\agy.ps1")
    assert exe == "powershell"
    assert prefix == [
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "C:\\tools\\agy.ps1"
    ]


def test_resolve_nothing_found_raises(monkeypatch):
    # RuntimeError, NOT FileNotFoundError: FileNotFoundError is an OSError
    # subclass and the runner's retryable tuple contains OSError with no
    # FileNotFoundError carve-out — a broken config would burn every attempt.
    monkeypatch.setattr(agy_mod.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError) as exc:
        _resolve_agy_executable("")
    assert "cli_path" in str(exc.value)
    retryable = AntigravityBackend.retryable_exceptions()
    assert not issubclass(RuntimeError, retryable)
    assert issubclass(FileNotFoundError, retryable)  # why the conversion matters


def test_resolve_named_missing_path_raises():
    with pytest.raises(RuntimeError) as exc:
        _resolve_agy_executable("/definitely/not/here/agy")
    assert "/definitely/not/here/agy" in str(exc.value)


def test_spawn_file_not_found_becomes_runtime_error():
    # Exec resolved but vanished before spawn (or the shim is missing):
    # pi.py precedent converts to RuntimeError — outside retryable_exceptions.
    async def boom(*argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "agy")

    with patch("asyncio.create_subprocess_exec", new=boom):
        with pytest.raises(RuntimeError) as exc:
            asyncio.run(AntigravityBackend().run(_spec()))
    assert "could not be spawned" in str(exc.value)


def test_resolve_cli_path_wins_over_path(monkeypatch, tmp_path):
    chosen = tmp_path / "agy"
    chosen.write_bytes(b"")
    monkeypatch.setattr(agy_mod.shutil, "which", lambda name: "/wrong/agy")
    assert _resolve_agy_executable(str(chosen))[0] == str(chosen)


def test_resolve_falls_back_to_which(monkeypatch):
    monkeypatch.setattr(agy_mod.shutil, "which", lambda name: "/usr/local/bin/agy")
    assert _resolve_agy_executable("") == ("/usr/local/bin/agy", [])


# ---------- environment precedence ----------


def _env_for(monkeypatch, *, named=None, profile_env=None, overrides=None) -> dict:
    """Run a stubbed run and return the env the subprocess would get."""
    monkeypatch.setenv("GEMINI_API_KEY", "from-os")
    cfg: dict = {"backend": "antigravity"}
    if named:
        cfg["gemini_api_key"] = named
    if profile_env is not None:
        cfg["env"] = profile_env
    proc = _MockProcess(stdout=b"")
    _, captured = _full_run(
        _spec(state={"_harness_cfg": cfg}, env_overrides=overrides), proc
    )
    return captured["env"]


def test_env_precedence_os_lowest(monkeypatch):
    env = _env_for(monkeypatch)
    assert env["GEMINI_API_KEY"] == "from-os"


def test_env_precedence_named_field_beats_os(monkeypatch):
    env = _env_for(monkeypatch, named="from-named")
    assert env["GEMINI_API_KEY"] == "from-named"


def test_env_precedence_profile_env_beats_named(monkeypatch):
    env = _env_for(monkeypatch, named="from-named", profile_env={"GEMINI_API_KEY": "from-profile"})
    assert env["GEMINI_API_KEY"] == "from-profile"


def test_env_precedence_overrides_beat_profile(monkeypatch):
    env = _env_for(
        monkeypatch,
        named="from-named",
        profile_env={"GEMINI_API_KEY": "from-profile", "EXTRA": "keep"},
        overrides={"GEMINI_API_KEY": "from-overrides"},
    )
    assert env["GEMINI_API_KEY"] == "from-overrides"
    assert env["EXTRA"] == "keep"


def test_spawn_uses_spec_cwd_and_oversized_stream_limit():
    proc = _MockProcess(stdout=b"")
    _, captured = _full_run(_spec(cwd="/tmp/worktree"), proc)
    assert captured["kwargs"]["cwd"] == "/tmp/worktree"
    assert captured["kwargs"]["limit"] == agy_mod._MAX_STREAM_BYTES + 1
    # Own process group so _kill_tree can reach the whole tree (T8).
    assert captured["kwargs"]["start_new_session"] is (os.name != "nt")


# ---------- stream parsing (one test per committed fixture) ----------


def test_fixture_basic_success():
    acc, _ = _parse_fixture("basic_success")
    result = _finalize(acc, 0)
    assert result.subtype == "success" and result.ok
    assert result.text == "OK\n"
    assert result.session_id == "95a5ac14-50d5-4903-ad5c-c11695322ee1"
    assert result.cost_usd is None
    assert result.structured_output is None


def test_fixture_stdin_single_turn():
    acc, _ = _parse_fixture("stdin_single_turn")
    result = _finalize(acc, 0)
    assert result.subtype == "success"
    assert result.text == "banana\n"


def test_fixture_tool_steps_progress_labels():
    acc, progress = _parse_fixture("tool_steps")
    assert any(p == "Tool: write /tmp/agy-spike/work/hello.txt" for p in progress)
    cmds = [p for p in progress if p.startswith("Tool: $ ")]
    assert cmds and cmds[0].startswith("Tool: $ cd /tmp/agy-spike/work && git status")
    assert _finalize(acc, 0).subtype == "success"


def test_fixture_tool_error_recovered_is_success():
    acc, _ = _parse_fixture("tool_error_recovered")
    # README lesson #4: a failed command is not a run failure.
    result = _finalize(acc, 0)
    assert result.subtype == "success"
    assert not acc.error_flag


def test_tool_step_error_collects_without_flagging():
    acc, _ = _parse_fixture("permission_denied")
    assert acc.tool_errors  # P2b run: denied write/command attempts
    assert not acc.error_flag
    assert _finalize(acc, 0).subtype == "success"


def test_fixture_unknown_model_is_config_error():
    acc, _ = _parse_fixture("unknown_model")
    result = _finalize(acc, 1)  # T5 exit code
    assert result.subtype == "config_error"
    assert not result.ok
    assert "not recognized as a known model" in result.text
    # config_error is outside the retryable set.
    assert "config_error" not in AntigravityBackend.retryable_subtypes()
    # No init event -> no session id.
    assert result.session_id is None


def test_fixture_structured_output():
    acc, _ = _parse_fixture("structured_output")
    result = _finalize(acc, 0)
    assert result.subtype == "success"
    assert result.structured_output == {"file_exists": True, "first_word": "autoSWE"}


def test_fixture_resume_session_id():
    acc, _ = _parse_fixture("resume")
    result = _finalize(acc, 0)
    assert result.subtype == "success"
    # T7: resume keeps the same conversation id.
    assert result.session_id == "95a5ac14-50d5-4903-ad5c-c11695322ee1"
    assert acc.num_turns == 2


def test_fixture_killed_partial_is_killed():
    # T8: the CLI traps SIGTERM, writes a final result (finished_reason
    # "interrupted"), then dies.  Either death-by-signal (negative rc, the
    # asyncio view) or exit 143 (128+SIGTERM, the spawner's view) maps killed.
    acc, _ = _parse_fixture("killed_partial")
    assert _finalize(acc, -15).subtype == "killed"
    acc2, _ = _parse_fixture("killed_partial")
    assert _finalize(acc2, 143).subtype == "killed"


def test_fixture_print_timeout_is_error():
    acc, _ = _parse_fixture("print_timeout")
    # T9: CLI ended itself at its --print-timeout; exit 3, status ERROR.
    assert _finalize(acc, 3).subtype == "error"


def test_fixture_waiting_ends_success_in_headless():
    # T10: headless print mode NEVER pends — ask_question completes with no
    # selection and the run ends SUCCESS.  The parser must reflect that.
    acc, _ = _parse_fixture("waiting")
    assert _finalize(acc, 0).subtype == "success"


def test_synthetic_waiting_status_is_error():
    acc = _AgyAccumulator()
    _parse_line(json.dumps({
        "event": "result",
        "result": {"conversation_id": "c1", "status": "WAITING", "response": "..."},
    }), acc, None)
    with patch.object(agy_mod, "log") as mock_log:
        result = _finalize(acc, 0)
    assert result.subtype == "error"
    assert any("ask_question is not supported headless" in str(c.args[0]) for c in mock_log.call_args_list)


def test_synthetic_canceled_status_is_killed():
    acc = _AgyAccumulator()
    _parse_line(json.dumps({
        "event": "result",
        "result": {"conversation_id": "c1", "status": "CANCELED"},
    }), acc, None)
    assert _finalize(acc, 0).subtype == "killed"


def test_result_conversation_id_wins_over_init():
    acc = _AgyAccumulator()
    with patch.object(agy_mod, "log") as mock_log:
        _parse_line(json.dumps({"event": "init", "conversation_id": "aaa", "init": {}}), acc, None)
        _parse_line(json.dumps({
            "event": "result", "result": {"conversation_id": "bbb", "status": "SUCCESS"},
        }), acc, None)
    assert acc.conversation_id == "bbb"
    assert any("using result id" in str(c.args[0]) for c in mock_log.call_args_list)


def test_subagent_progress_label():
    acc, progress = _parse_fixture("subagent")
    assert any(p == "Subagent: File Summarizer (summarizer)" for p in progress)
    assert _finalize(acc, 0).subtype == "success"


def test_unknown_event_is_skipped():
    acc = _AgyAccumulator()
    _parse_line('{"event":"brand_new_thing"}', acc, None)
    _parse_line("not json at all", acc, None)
    assert not acc.saw_result and not acc.error_flag


# ---------- no-result finalization (pre-result config deaths, spike §0.1.3) ----------


def test_no_result_auth_failure_on_stderr_is_config_error():
    # agy dies before emitting any result event with auth text on stderr:
    # config error (outside retryable_subtypes), CLI text surfaced for the
    # FAILED comment — not a retryable "error" that burns every attempt.
    acc = _AgyAccumulator()
    stderr = (
        b"Error: authentication required for the configured project.\n"
        b"Run `agy auth login` to set up credentials.\n"
    )
    res = _finalize(acc, 1, stderr_tail=stderr)
    assert res.subtype == "config_error"
    assert res.ok is False
    assert res.subtype not in AntigravityBackend.RETRYABLE_SUBTYPES
    assert "authentication required" in res.text


def test_no_result_unrelated_stderr_stays_retryable_error():
    acc = _AgyAccumulator()
    res = _finalize(acc, 1, stderr_tail=b"renderer crashed: segmentation fault")
    assert res.subtype == "error"
    assert res.subtype in AntigravityBackend.RETRYABLE_SUBTYPES


def test_no_result_zero_exit_stderr_never_config_error():
    # returncode 0 with no result stays "error": the config-error scan is
    # only for non-zero exits.
    acc = _AgyAccumulator()
    res = _finalize(acc, 0, stderr_tail=b"authentication required")
    assert res.subtype == "error"


def test_env_sourced_api_key_masked_in_config_error_text_and_logs():
    # Review round-4: the stderr tail becomes RunResult.text on the
    # config_error path (posted to a public FAILED comment).  The key must be
    # masked regardless of which source won the env precedence merge — here
    # env_overrides, the highest tier, NOT the named profile field the old
    # redaction covered.
    secret = "AIzaSECRET-aaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    stderr = (
        f"Error: authentication required for the configured project "
        f"(key {secret} rejected)\n".encode()
    )
    proc = _MockProcess(stdout=b"", stderr=stderr, returncode=1)
    with patch.object(agy_mod, "log") as mock_log:
        result, captured = _full_run(
            _spec(env_overrides={"GEMINI_API_KEY": secret}), proc
        )
    # The key is still delivered to the CLI — masking is display-only.
    assert captured["env"]["GEMINI_API_KEY"] == secret
    assert result.subtype == "config_error"
    assert "authentication required" in result.text
    assert secret not in result.text
    assert secret not in "".join(
        str(c.args[0]) for c in mock_log.call_args_list if c.args
    )


def test_profile_sourced_api_key_still_masked():
    # Regression guard for the original (profile-field) redaction path, now
    # covered by the single resolved-key masking in _run_async.
    secret = "AIzaBBBBbbbbBBBBbbbbBBBBbbbbBBBBbbbb"
    stderr = (
        f"Error: authentication required (credential {secret} invalid)\n".encode()
    )
    proc = _MockProcess(stdout=b"", stderr=stderr, returncode=1)
    result, _ = _full_run(
        _spec(state={"_harness_cfg": {"backend": "antigravity", "model": MODEL,
                                      "gemini_api_key": secret}}),
        proc,
    )
    assert result.subtype == "config_error"
    assert secret not in result.text


def test_init_cwd_mismatch_warns():
    # Plan §1.3: subprocess cwd is the only workspace lever; warn loudly when
    # the CLI's init.cwd disagrees with spec.cwd.
    events = "\n".join([
        json.dumps({"event": "init", "conversation_id": "c1",
                    "init": {"cwd": "/an/entirely/different/dir", "model": MODEL,
                             "permission_mode": "default", "tools": []}}),
        json.dumps({"event": "result",
                    "result": {"conversation_id": "c1", "status": "SUCCESS",
                               "response": "ok"}}),
    ]) + "\n"
    proc = _MockProcess(stdout=events.encode())
    with patch.object(agy_mod, "log") as mock_log:
        result, _ = _full_run(_spec(cwd="/tmp/repo"), proc)
    assert result.subtype == "success"
    assert any(
        c.args
        and "[WARN][ANTIGRAVITY] init.cwd" in str(c.args[0])
        and "/an/entirely/different/dir" in str(c.args[0])
        and "/tmp/repo" in str(c.args[0])
        for c in mock_log.call_args_list
    )


def test_init_cwd_match_does_not_warn():
    events = "\n".join([
        json.dumps({"event": "init", "conversation_id": "c1",
                    "init": {"cwd": "/tmp/repo", "model": MODEL,
                             "permission_mode": "default", "tools": []}}),
        json.dumps({"event": "result",
                    "result": {"conversation_id": "c1", "status": "SUCCESS",
                               "response": "ok"}}),
    ]) + "\n"
    proc = _MockProcess(stdout=events.encode())
    with patch.object(agy_mod, "log") as mock_log:
        result, _ = _full_run(_spec(cwd="/tmp/repo"), proc)
    assert result.subtype == "success"
    assert not any(
        c.args and "init.cwd" in str(c.args[0]) for c in mock_log.call_args_list
    )


def test_tool_label_rendering():
    assert _tool_label({
        "tool_name": "run_command",
        "tool_info": {"parameters": {"CommandLine": "ls -la"}},
    }) == "$ ls -la"
    assert _tool_label({
        "tool_name": "read_file",
        "tool_info": {"parameters": {"Path": "/x/y.py"}},
    }) == "read /x/y.py"
    assert _tool_label({"tool_name": "search_web", "tool_info": {}}) == "search_web"
    # Malformed payloads must not raise.
    assert _tool_label({"tool_info": "garbage"}) == "tool"


# ---------- text assembly ----------


def test_result_response_wins_over_chunks():
    acc = _AgyAccumulator()
    _parse_line(json.dumps({
        "event": "step_update",
        "step_update": {"step_index": 0, "state": "ACTIVE", "step_type": "agent_response",
                        "text_delta": "partial answer"},
    }), acc, None)
    _parse_line(json.dumps({
        "event": "result",
        "result": {"conversation_id": "c", "status": "SUCCESS", "response": "AUTHORITATIVE"},
    }), acc, None)
    assert _finalize(acc, 0).text == "AUTHORITATIVE"


def test_fallback_to_text_chunks_when_no_result():
    acc = _AgyAccumulator()
    for delta in ("Hello ", "world"):
        _parse_line(json.dumps({
            "event": "step_update",
            "step_update": {"step_index": 1, "state": "ACTIVE",
                            "step_type": "agent_response", "text_delta": delta},
        }), acc, None)
    result = _finalize(acc, -15)
    # No result event, killed: text survives via the delta fallback.
    assert result.subtype == "killed"
    assert result.text == "Hello world"


def test_delta_progress_fires_then_throttles_at_80_chars():
    # _fire_delta_progress: first delta for a step index fires immediately
    # (capped at 120 chars), then only once the accumulated text has grown by
    # ~80 chars since the last fire.
    acc = _AgyAccumulator()
    fired: list[str] = []

    def _step_line(delta: str) -> str:
        return json.dumps({
            "event": "step_update",
            "step_update": {"step_index": 1, "state": "ACTIVE",
                            "step_type": "agent_response", "text_delta": delta},
        })

    d1 = "a" * 100
    _parse_line(_step_line(d1), acc, fired.append)
    assert fired == [d1], "first delta must reach the callback"

    d2 = "b" * 100
    _parse_line(_step_line(d2), acc, fired.append)
    assert fired == [d1, d2], "growth since last fire (100) >= 80 → fires again"

    _parse_line(_step_line("c" * 10), acc, fired.append)
    assert len(fired) == 2, "10-char growth since last fire stays throttled"

    # Accumulation is independent of throttling: all chunks land.
    assert "".join(acc._text_by_index[1]) == d1 + d2 + "c" * 10


def test_active_done_deltas_not_double_counted():
    # Fixture T1 semantics: ACTIVE carries the partial, DONE carries the
    # remainder; concatenation equals result.response exactly.
    acc, _ = _parse_fixture("basic_success")
    chunks = "".join(
        "".join(acc._text_by_index[i]) for i in sorted(acc._text_by_index)
    )
    assert chunks == "OK\n"
    assert chunks == acc.final_response


# ---------- overflow / kill / contract ----------


def test_stream_overflow_errors_without_exception():
    big = json.dumps({
        "event": "result",
        "result": {"conversation_id": "c", "status": "SUCCESS", "response": "x" * 500},
    }).encode()
    proc = _MockProcess(stdout=big + b"\n", limit=100)
    result, _ = _full_run(_spec(), proc)
    assert result.subtype == "error"
    # stdout was drained after the ValueError (no child pipe deadlock).
    assert proc.stdout.drained


def test_soft_deny_stderr_line_warns(monkeypatch):
    # Spike P3b: a tool that would have prompted is auto-denied in headless
    # print mode — not a crash, but the first sign of a permission
    # misconfiguration, so read_stderr() must surface a loud [WARN] line.
    deny_line = (
        b"Warning: tool run_command requires approval; headless mode cannot "
        b"prompt for it, so it was auto-denied.\n"
    )
    proc = _MockProcess(
        stdout=_fixture_text("basic_success").encode(), stderr=deny_line
    )
    with patch.object(agy_mod, "log") as mock_log:
        result, _ = _full_run(_spec(), proc)
    warns = [
        str(c.args[0])
        for c in mock_log.call_args_list
        if c.args and "soft-denied" in str(c.args[0])
    ]
    assert len(warns) == 1, "one WARN per matching stderr line"
    assert all(w.startswith("[WARN][ANTIGRAVITY] soft-denied:") for w in warns)
    assert "headless mode cannot prompt" in warns[0]
    # The soft-deny WARN alone never fails the run (README lesson #4).
    assert result.subtype == "success"


def test_soft_deny_marker_without_match_does_not_warn():
    proc = _MockProcess(
        stdout=_fixture_text("basic_success").encode(),
        stderr=b"some unrelated stderr noise\n",
    )
    with patch.object(agy_mod, "log") as mock_log:
        result, _ = _full_run(_spec(), proc)
    assert not any(
        c.args and "soft-denied" in str(c.args[0]) for c in mock_log.call_args_list
    )
    assert result.subtype == "success"


def test_runner_cancel_kills_tree_and_propagates(monkeypatch):
    # The runner's wait_for(spec.timeout) fires 30s BEFORE the backend's own
    # backstop and cancels the coroutine mid-gather.  _run_async must kill the
    # process tree on CancelledError too, or agy leaks on every retry/cancel
    # (review round-3 finding: the #327 orphan gap on the runner path).
    hang = asyncio.Event()  # never set: the readers would block forever
    proc = _MockProcess(hang=hang)
    killed: list = []
    monkeypatch.setattr(agy_mod, "_kill_tree", lambda p: killed.append(p))

    async def main():
        async def fake_exec(*argv, **kwargs):
            return proc

        with patch("asyncio.create_subprocess_exec", new=fake_exec):
            task = asyncio.create_task(AntigravityBackend().run(_spec(timeout=3600)))
            # Wait until the stdin writer has completed inside the gather, so
            # the task is demonstrably parked in the stream readers.
            for _ in range(200):
                await asyncio.sleep(0.005)
                if proc.stdin.closed:
                    break
            assert proc.stdin.closed, "run never reached the gather"
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("CancelledError did not propagate")

    asyncio.run(main())
    assert killed == [proc], "cancellation must kill the process tree"


def test_timeout_kills_tree_and_removes_temp_schema(monkeypatch):
    monkeypatch.setattr(agy_mod, "_HARD_TIMEOUT_SLACK", 0)
    hang = asyncio.Event()
    proc = _MockProcess(hang=hang)
    killed: list = []

    def spy_kill_tree(process):
        killed.append(process)

    monkeypatch.setattr(agy_mod, "_kill_tree", spy_kill_tree)
    schema = {"type": "object"}
    result, captured = _full_run(
        _spec(timeout=0, output_format={"type": "json_schema", "schema": schema}), proc
    )
    assert result.subtype == "killed"
    assert killed == [proc]
    schema_path = _schema_path_from(captured["argv"])
    assert schema_path and not os.path.exists(schema_path)


def test_kill_tree_posix_uses_killpg(monkeypatch):
    # POSIX: the whole process group gets SIGKILL (T8-confirmed no orphans).
    calls: list = []
    monkeypatch.setattr(agy_mod.os, "name", "posix")
    monkeypatch.setattr(agy_mod.os, "getpgid", lambda pid: 4242)
    monkeypatch.setattr(agy_mod.os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
    _kill_tree(_MockProcess())
    assert calls == [(4242, agy_mod.signal.SIGKILL)]


def test_kill_tree_windows_uses_taskkill(monkeypatch):
    runs: list = []
    monkeypatch.setattr(agy_mod.os, "name", "nt")
    monkeypatch.setattr(
        agy_mod.subprocess, "run",
        lambda cmd, **kw: runs.append(cmd),
    )
    _kill_tree(_MockProcess())
    assert runs == [["taskkill", "/T", "/F", "/PID", "4242"]]


def test_kill_tree_falls_back_when_group_gone(monkeypatch):
    def boom(pid):
        raise ProcessLookupError
    monkeypatch.setattr(agy_mod.os, "name", "posix")
    monkeypatch.setattr(agy_mod.os, "getpgid", boom)
    proc = _MockProcess()
    _kill_tree(proc)  # must not raise
    assert proc.killed


def test_backend_contract():
    backend = AntigravityBackend()
    assert isinstance(backend, CodingBackend)
    # Phase 1 honest set + Phase 4's mode + Phase 5's mcp (session_fork stays
    # unadvertised: agy has no fork primitive).
    assert backend.capabilities() == {
        "resume", "progress_stream", "structured_output", "mode", "mcp",
    }
    # Fresh dict each call, agy eager spellings since Phase 5 (single
    # underscores — NOT the Claude double-underscore shape).
    assert backend.comment_tool_names() == ANTIGRAVITY_COMMENT_TOOL_NAMES
    assert "mcp_autoswe_comment_post_plan" in ANTIGRAVITY_COMMENT_TOOL_NAMES.values()
    assert CLAUDE_COMMENT_TOOL_NAMES != ANTIGRAVITY_COMMENT_TOOL_NAMES
    backend.comment_tool_names()["post_plan"] = "mutated"
    assert backend.comment_tool_names() == ANTIGRAVITY_COMMENT_TOOL_NAMES
    assert backend.retryable_subtypes() == {"error", "killed"}
    assert asyncio.TimeoutError in backend.retryable_exceptions()
    assert OSError in backend.retryable_exceptions()


def test_run_result_unpacks_as_three_tuple():
    acc, _ = _parse_fixture("basic_success")
    result = _finalize(acc, 0)
    text, session_id, subtype = result
    assert text == "OK\n"
    assert session_id == "95a5ac14-50d5-4903-ad5c-c11695322ee1"
    assert subtype == "success"
    assert isinstance(result, RunResult)


def test_missing_model_raises_value_error():
    with pytest.raises(ValueError, match="model"):
        asyncio.run(AntigravityBackend().run(_spec(model=None)))


# ---------- Phase 4 §4.3: mode → permissions table ----------


def test_rule_prefix_absolute_trailing_slash_and_normalized():
    # P2's hard lesson encoded: absolute, forward-slash, trailing slash —
    # globs and slash-less prefixes silently do NOT match.
    assert agy_mod._rule_prefix("/tmp/repo") == "/tmp/repo/"
    assert agy_mod._rule_prefix("/tmp/repo/") == "/tmp/repo/"


@pytest.mark.skipif(os.name != "nt", reason="drive-letter abspath is Windows semantics")
def test_rule_prefix_windows_drive_letter_normalized():
    # Pinned literal (not a re-derivation of the implementation): backslash
    # rule targets ride the same forward-slash shape as Linux.
    assert agy_mod._rule_prefix("C:\\work\\wt") == "C:/work/wt/"


@pytest.mark.skipif(os.name == "nt", reason="POSIX abspath semantics under test")
def test_rule_prefix_relative_cwd_absolutized(tmp_path, monkeypatch):
    # A relative cwd is absolutized against the process cwd, never leaked
    # into a rule as-is.  chdir to a known dir so the expectation is a
    # literal, not the same expression _rule_prefix computes.
    monkeypatch.chdir(tmp_path)
    expected = str(tmp_path.resolve()) + "/relative/wt/"
    assert agy_mod._rule_prefix("relative/wt") == expected


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink creation needs no privilege")
def test_rule_prefix_canonicalizes_symlinked_cwd(tmp_path):
    # Review MEDIUM: the CLI resolves writes against canonical paths, so a
    # symlink component in spec.cwd must not leave abspath-built deny targets
    # unmatched (silent enforcement weakening behind a passing self-check).
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    expected = str(real.resolve()).replace("\\", "/").rstrip("/") + "/"
    # Whole-cwd symlink …
    assert agy_mod._rule_prefix(str(link)) == expected
    # … and a symlink component mid-path (tail need not exist).
    assert agy_mod._rule_prefix(str(link) + "/wt") == expected + "wt/"


def test_rule_prefix_rejects_empty_cwd():
    # Fail-loud (review round-4): abspath("") would silently anchor every
    # deny rule to the poller's cwd — a mis-plumbed spec must not enforce
    # nothing while looking enforced.
    for empty in ("", None):
        with pytest.raises(ValueError, match=r"spec\.cwd"):
            agy_mod._rule_prefix(empty)
    # And the seam fails before staging anything.
    with pytest.raises(ValueError, match=r"spec\.cwd"):
        agy_mod._stage_run_config(_spec(mode="plan", cwd=""), {})


def test_permissions_plan_and_read_only_share_the_read_only_profile():
    for mode in ("plan", "read_only"):
        perm = agy_mod._permissions_for_mode(mode, "/w/")
        deny = perm["deny"]
        # Every mutating tool denied against the worktree (prefix target).
        for tool in agy_mod._MUTATING_TOOL_RULES:
            assert f"{tool}(/w/)" in deny
        assert "write_file(/w/.git/)" in deny
        assert "command(*)" in deny
        # ask_* are denied in EVERY mode (§4.3: headless cannot answer them).
        for tool in agy_mod._NEVER_ALLOWED_TOOL_RULES:
            assert tool in deny
        # Reads stay allowed alongside the deny block (P5 coexistence).
        # Both grep spellings are carried (stream spelling unverified in
        # fixtures; unmatched allow is a no-op — Phase 6 live pass picks).
        for tool in ("read_file", "view_file", "search", "grep_search", "list_dir"):
            assert f"{tool}(/w/)" in perm["allow"]
        assert "mcp(autoswe_comment/*)" in perm["allow"]


def test_permissions_read_only_adds_inline_comment_allow_plan_does_not():
    assert "mcp(autoswe_inline_comment/*)" in agy_mod._permissions_for_mode(
        "read_only", "/w/")["allow"]
    assert "mcp(autoswe_inline_comment/*)" not in agy_mod._permissions_for_mode(
        "plan", "/w/")["allow"]


def test_permissions_read_write_is_belt_and_braces_only():
    for mode in ("read_write", "anything-unset-ish"):
        perm = agy_mod._permissions_for_mode(mode, "/w/")
        assert perm["deny"] == ["write_file(/w/.git/)",
                                *agy_mod._NEVER_ALLOWED_TOOL_RULES]
        assert perm["allow"] == []


def test_ask_tools_denied_even_in_read_write():
    # The §4.3 "EVERY mode" row: headless print mode cannot answer a prompt,
    # and an interactive default would hang the run.
    deny = agy_mod._permissions_for_mode("read_write", "/w/")["deny"]
    assert set(agy_mod._NEVER_ALLOWED_TOOL_RULES) <= set(deny)


# ---------- Phase 4 §4.3: tool override mapping ----------


def test_disallowed_override_mapped_to_rule_or_logged_and_ignored():
    deny: list[str] = []
    allow: list[str] = []
    spec = _spec(disallowed_tools_override=[
        "Bash",                       # mapped: wildcard command deny
        "Write",                      # mapped: grows the worktree prefix target
        "mcp__github__create_pr",     # mapped: mcp rule spelling
        "TotallyMadeUpTool",          # unmapped: log + ignore
    ])
    with patch.object(agy_mod, "log") as mock_log:
        agy_mod._apply_tool_overrides(spec, deny, allow)
    assert "command(*)" in deny
    assert "write_file(/tmp/repo/)" in deny  # P2: a bare rule matches nothing
    assert "mcp(github/create_pr)" in deny
    assert "TotallyMadeUpTool" not in deny and "TotallyMadeUpTool" not in allow
    assert any("no agy rule equivalent" in str(c.args[0]) for c in mock_log.call_args_list)


def test_extra_override_adds_mcp_allow_only():
    allow: list[str] = []
    spec = _spec(extra_tools=["mcp__autoswe_comment__post_plan", "Read"])
    agy_mod._apply_tool_overrides(spec, [], allow)
    # Already-mode-governed / unmapped names add no allow; the MCP name does.
    assert allow == ["mcp(autoswe_comment/post_plan)"]


# ---------- Phase 4 §4.2: per-run settings staging (ONE seam) ----------


def test_stage_run_config_returns_argv_channel_and_cleans_up():
    args, env_patch, cleanup = agy_mod._stage_run_config(_spec(mode="plan"), {})
    assert args[0] == "--settings"
    path = args[1]
    assert os.path.exists(path)
    assert env_patch == {}  # Option C: no env channel needed (P3(b))
    cleanup()
    assert not os.path.exists(path)


def test_two_concurrent_runs_get_distinct_settings_dirs():
    a_args, _, a_cleanup = agy_mod._stage_run_config(_spec(mode="plan"), {})
    b_args, _, b_cleanup = agy_mod._stage_run_config(_spec(mode="read_write"), {})
    try:
        assert a_args[1] != b_args[1]
        # Same filename, different private dir → MAX_CONCURRENT>1 safe.
        assert os.path.dirname(a_args[1]) != os.path.dirname(b_args[1])
        assert os.path.basename(a_args[1]) == os.path.basename(b_args[1]) == "settings.json"
    finally:
        a_cleanup()
        b_cleanup()


def test_staged_dir_reclaimed_when_schema_staging_fails(monkeypatch):
    # Review MEDIUM: schema staging used to live between _stage_run_config
    # and _wrapped, so a mkstemp/json failure propagated before the finally
    # existed and stranded the autoswe-agy-run-* dir.  Staging now runs
    # inside _wrapped, so the finally reclaims it even when nothing spawned.
    staged: list[str] = []
    real_mkdtemp = agy_mod.tempfile.mkdtemp

    def spy_mkdtemp(*args, **kwargs):
        d = real_mkdtemp(*args, **kwargs)
        staged.append(d)
        return d

    def boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(agy_mod.tempfile, "mkdtemp", spy_mkdtemp)
    monkeypatch.setattr(agy_mod.tempfile, "mkstemp", boom)
    spec = _spec(mode="read_write", output_format={"schema": {"type": "object"}})

    with pytest.raises(OSError, match="No space left"):
        asyncio.run(AntigravityBackend().run(spec))

    assert len(staged) == 1
    assert not os.path.exists(staged[0])


def test_staged_settings_content_pins_worktree_prefix():
    args, _, cleanup = agy_mod._stage_run_config(
        _spec(mode="plan", cwd="/home/me/wt/issue-1"), {})
    try:
        data = json.loads(Path(args[1]).read_text(encoding="utf-8"))
    finally:
        cleanup()
    deny = data["permissions"]["deny"]
    assert "write_file(/home/me/wt/issue-1/)" in deny
    assert "command(*)" in deny
    assert "/home/me/wt/issue-1/.git/" in " ".join(deny)
    assert "read_file(/home/me/wt/issue-1/)" in data["permissions"]["allow"]


def test_base_settings_merge_preserves_unrelated_keys_and_order(tmp_path):
    # Profile base (Phase 5 will park MCP registration + useG1Credits here)
    # is copied into every staged file; the mode block rides alongside it.
    home = tmp_path / "cfg-home" / MODEL
    home.mkdir(parents=True)
    (home / "base-settings.json").write_text(json.dumps({
        "useG1Credits": True,
        "modelProvider": "byok",
        "permissions": {"deny": ["command(ls)"], "allow": ["grep(/base/)"]},
    }), encoding="utf-8")
    args, _, cleanup = agy_mod._stage_run_config(
        _spec(mode="plan"), {"config_home": str(tmp_path / "cfg-home")})
    try:
        data = json.loads(Path(args[1]).read_text(encoding="utf-8"))
    finally:
        cleanup()
    assert data["useG1Credits"] is True
    assert data["modelProvider"] == "byok"
    # Base rules kept, ours appended after them, deduped.
    deny = data["permissions"]["deny"]
    assert deny[0] == "command(ls)"
    assert "command(*)" in deny
    assert "grep(/base/)" in data["permissions"]["allow"]


def test_bad_base_settings_degrades_to_empty_not_crash(tmp_path):
    home = tmp_path / "cfg-home" / MODEL
    home.mkdir(parents=True)
    (home / "base-settings.json").write_text("{ not json", encoding="utf-8")
    with patch.object(agy_mod, "log") as mock_log:
        args, _, cleanup = agy_mod._stage_run_config(
            _spec(mode="plan"), {"config_home": str(tmp_path / "cfg-home")})
    try:
        data = json.loads(Path(args[1]).read_text(encoding="utf-8"))
    finally:
        cleanup()
    # Enforcement is never lost to a broken base file.
    assert "command(*)" in data["permissions"]["deny"]
    assert any("not valid JSON" in str(c.args[0]) for c in mock_log.call_args_list)


def test_base_settings_string_rule_block_fails_loudly(tmp_path, monkeypatch):
    # Review INFO: a bare string where deny/allow expect a LIST must raise
    # loudly — the merge loop would otherwise iterate "command(*)"
    # character-by-character into one-char rules ("c", "o", …).  Validation
    # runs before mkdtemp, so a bad base file stages nothing to leak.
    home = tmp_path / "cfg-home" / MODEL
    home.mkdir(parents=True)
    staged: list[str] = []
    real_mkdtemp = agy_mod.tempfile.mkdtemp

    def spy_mkdtemp(*args, **kwargs):
        d = real_mkdtemp(*args, **kwargs)
        staged.append(d)
        return d

    monkeypatch.setattr(agy_mod.tempfile, "mkdtemp", spy_mkdtemp)
    for key in ("deny", "allow"):
        (home / "base-settings.json").write_text(
            json.dumps({"permissions": {key: "command(*)"}}), encoding="utf-8")
        with pytest.raises(agy_mod.AgyConfigError, match=rf"permissions\.{key}"):
            agy_mod._stage_run_config(
                _spec(mode="plan"), {"config_home": str(tmp_path / "cfg-home")})
    assert staged == []  # validated before anything was staged


def test_run_stages_settings_in_argv_and_reclaims_on_success():
    proc = _MockProcess(stdout=_fixture_text("basic_success").encode())
    result, captured = _full_run(_spec(mode="plan"), proc)
    assert result.subtype == "success"
    path = _settings_path_from(captured["argv"])
    assert path and path.endswith("settings.json")
    # finally-reclaimed even on the happy path.
    assert not os.path.exists(path)
    assert not os.path.exists(os.path.dirname(path))


def test_run_reclaims_staged_dir_on_failure_and_kill():
    # Failure path: error result (non-zero rc) still goes through finally.
    proc = _MockProcess(stdout=b"", stderr=b"renderer crashed", returncode=1)
    result, captured = _full_run(_spec(mode="plan"), proc)
    assert result.subtype == "error"
    path = _settings_path_from(captured["argv"])
    assert path and not os.path.exists(os.path.dirname(path))

    # Kill path: hard-timeout teardown (same harness as the schema temp test).
    monkey_slack = patch.object(agy_mod, "_HARD_TIMEOUT_SLACK", 0)
    hang = asyncio.Event()
    proc2 = _MockProcess(hang=hang)
    with monkey_slack, patch.object(agy_mod, "_kill_tree", lambda p: None):
        result2, captured2 = _full_run(_spec(timeout=0, mode="read_write"), proc2)
    assert result2.subtype == "killed"
    path2 = _settings_path_from(captured2["argv"])
    assert path2 and not os.path.exists(os.path.dirname(path2))


def test_read_only_run_gets_mode_flag_and_no_skip_flag():
    proc = _MockProcess(stdout=_fixture_text("basic_success").encode())
    _, captured = _full_run(_spec(mode="plan"), proc)
    argv = captured["argv"]
    assert argv[argv.index("--mode") + 1] == "plan"
    assert "--dangerously-skip-permissions" not in argv
    assert "--settings" in argv


def test_read_write_run_keeps_skip_flag_and_default_mode():
    proc = _MockProcess(stdout=_fixture_text("basic_success").encode())
    _, captured = _full_run(_spec(mode="read_write"), proc)
    argv = captured["argv"]
    assert "--mode" not in argv
    assert "--dangerously-skip-permissions" in argv


def test_settings_dir_never_leaks_into_os_environ():
    # The whole point of Option C over Option A: no env indirection that
    # could race a concurrent run or outlive the process.
    before = dict(os.environ)
    proc = _MockProcess(stdout=_fixture_text("basic_success").encode())
    _full_run(_spec(mode="plan"), proc)
    assert os.environ == before


# ---------- Phase 4 §4.4: permission_mode self-check ----------


def _stream_with_mode(permission_mode: str | None) -> bytes:
    init: dict = {"cwd": "/tmp/repo", "model": MODEL, "tools": []}
    if permission_mode is not None:
        init["permission_mode"] = permission_mode
    return ("\n".join([
        json.dumps({"event": "init", "conversation_id": "c1", "init": init}),
        json.dumps({"event": "result",
                    "result": {"conversation_id": "c1", "status": "SUCCESS",
                               "response": "ok"}}),
    ]) + "\n").encode()


def test_read_only_mismatch_raises_not_silently_succeeds():
    # The §4.4 core refusal: a plan run whose init attests always-proceed had
    # NO write guard — failing loudly beats shipping an unguarded plan.
    # (Raise rather than error-flag only: the planner never inspects subtype,
    # so a flagged-but-returned error result would park the task in waiting.)
    proc = _MockProcess(stdout=_stream_with_mode("always-proceed"))
    with patch.object(agy_mod, "log") as mock_log:
        with pytest.raises(RuntimeError, match="read-only not in effect"):
            _full_run(_spec(mode="plan"), proc)
    assert any(
        c.args and "[ERROR][ANTIGRAVITY] read-only not in effect" in str(c.args[0])
        for c in mock_log.call_args_list
    )


def test_read_only_mismatch_still_reclaims_staged_dir():
    # The raise must happen AFTER the finally that reclaims the temp settings
    # dir — a mismatch leak would strand a file per failed plan.
    proc = _MockProcess(stdout=_stream_with_mode("always-proceed"))
    captured_settings: list[str] = []

    async def fake_exec(*argv, **kwargs):
        captured_settings.append(_settings_path_from(list(argv)))
        return proc

    with patch("asyncio.create_subprocess_exec", new=fake_exec):
        with pytest.raises(RuntimeError):
            asyncio.run(AntigravityBackend().run(_spec(mode="plan")))
    path = captured_settings[0]
    assert path and not os.path.exists(os.path.dirname(path))


def test_read_only_matching_posture_does_not_raise():
    proc = _MockProcess(stdout=_stream_with_mode("request-review"))
    result, _ = _full_run(_spec(mode="plan"), proc)
    assert result.subtype == "success"


def test_read_only_unattested_init_fails_closed():
    # Review round-2 MEDIUM: init parsed but WITHOUT permission_mode is not
    # "no attestation to check" — under staged --settings the CLI always
    # attests, so silence means the guard is unverifiable → fail closed,
    # same ERROR + raise path as a mismatch (never inherit fail-open).
    proc = _MockProcess(stdout=_stream_with_mode(None))
    with patch.object(agy_mod, "log") as mock_log:
        with pytest.raises(RuntimeError, match="did not attest permission_mode"):
            _full_run(_spec(mode="plan"), proc)
    assert any(
        c.args
        and "[ERROR][ANTIGRAVITY] read-only phase: CLI did not attest"
        in str(c.args[0])
        for c in mock_log.call_args_list
    )


def test_pre_init_death_keeps_normal_error_outcome():
    # The fail-open carve-out that REMAINS: a stream that dies before the init
    # line never reached attestation, so it maps to its ordinary outcome
    # (error/killed) rather than the posture RuntimeError.
    proc = _MockProcess(stdout=b"", stderr=b"boom", returncode=1)
    result, _ = _full_run(_spec(mode="plan"), proc)
    assert result.subtype == "error"
    assert "permission_mode" not in (result.text or "")


def test_unattested_init_on_read_write_run_still_warns_or_passes():
    # Fail-closed is read-only-only: a read_write run has no guard to verify,
    # so a missing attestation must not raise (worst case the WARN path).
    proc = _MockProcess(stdout=_stream_with_mode(None))
    result, _ = _full_run(_spec(mode="read_write"), proc)
    assert result.subtype == "success"


def test_read_write_posture_drift_only_warns():
    # Skip flag requested but init says request-review: degraded convenience,
    # not a security failure → WARN, run proceeds (§4.4 asymmetry).
    proc = _MockProcess(stdout=_stream_with_mode("request-review"))
    with patch.object(agy_mod, "log") as mock_log:
        result, _ = _full_run(_spec(mode="read_write"), proc)
    assert result.subtype == "success"
    assert any(
        c.args and "[WARN][ANTIGRAVITY] init.permission_mode=" in str(c.args[0])
        for c in mock_log.call_args_list
    )
    assert not any(
        c.args and "[ERROR][ANTIGRAVITY]" in str(c.args[0])
        for c in mock_log.call_args_list
    )


def test_expected_permission_mode_table():
    assert agy_mod._expected_permission_mode("plan", {}) == "request-review"
    assert agy_mod._expected_permission_mode("read_only", {}) == "request-review"
    assert agy_mod._expected_permission_mode(None, {}) == "always-proceed"
    assert agy_mod._expected_permission_mode(
        "read_write", {"skip_permissions": True}) == "always-proceed"
    # read_write WITHOUT skip: interactive default is fine headless → no check.
    assert agy_mod._expected_permission_mode(
        "read_write", {"skip_permissions": False}) is None


def test_soft_deny_on_read_only_run_is_info_not_warn():
    # §4.4: on an enforced read-only run a soft-deny line is the deny block
    # WORKING — it must not shout WARN the way it does on a run where it
    # signals misconfiguration.
    deny_line = (
        b"Warning: tool write_file requires approval; headless mode cannot "
        b"prompt for it, so it was auto-denied.\n"
    )
    proc = _MockProcess(
        stdout=_fixture_text("basic_success").encode(), stderr=deny_line
    )
    with patch.object(agy_mod, "log") as mock_log:
        result, _ = _full_run(_spec(mode="plan"), proc)
    msgs = [str(c.args[0]) for c in mock_log.call_args_list if c.args]
    soft = [m for m in msgs if "soft-denied" in m]
    assert soft == ["[ANTIGRAVITY] soft-denied (expected, read-only): " + deny_line.decode().rstrip()]
    assert all(not m.startswith("[WARN]") for m in soft)
    assert result.subtype == "success"


# ---------- Phase 5 §5.3: MCP comment classification & flags ----------


def _step_event(index: int, state: str, tool_name: str, params=None, error=None) -> str:
    """One step_update line in the fixture-confirmed shape (ACTIVE and DONE
    of one call share step_index)."""
    tool_info: dict = {"name": tool_name}
    if params is not None:
        tool_info["parameters"] = params
    if error is not None:
        tool_info["error"] = error
    return json.dumps({
        "event": "step_update",
        "step_update": {
            "step_index": index,
            "step_type": "tool",
            "state": state,
            "tool_name": tool_name,
            "tool_info": tool_info,
        },
    })


def _parse_lines(*lines: str) -> tuple[_AgyAccumulator, list]:
    acc = _AgyAccumulator()
    progress: list = []
    for line in lines:
        _parse_line(line, acc, progress.append)
    return acc, progress


def _eager_pair(index, tool_name, body, error=None) -> list[str]:
    return [
        _step_event(index, "ACTIVE", tool_name, {"body": body}),
        _step_event(index, "DONE", tool_name, {"body": body}, error=error),
    ]


def test_classifier_eager_shape():
    for tool, kind in [
        ("mcp_autoswe_comment_post_plan", "plan"),
        ("mcp_autoswe_comment_post_question", "question"),
        ("mcp_autoswe_comment_update_progress", "progress"),
    ]:
        role, body = _classify_mcp_comment_call({
            "tool_name": tool, "tool_info": {"parameters": {"body": "hello"}},
        })
        assert (role, body) == (kind, "hello")


def test_classifier_lazy_proxy_dict_and_string_arguments():
    base = {"ServerName": "autoswe_comment", "ToolName": "post_plan"}
    role, body = _classify_mcp_comment_call({
        "tool_name": "call_mcp_tool",
        "tool_info": {"parameters": {**base, "Arguments": {"body": "B"}}},
    })
    assert (role, body) == ("plan", "B")
    # Arguments may arrive as a JSON string.
    role, body = _classify_mcp_comment_call({
        "tool_name": "call_mcp_tool",
        "tool_info": {"parameters": {**base, "Arguments": json.dumps({"body": "S"})}},
    })
    assert (role, body) == ("plan", "S")


def test_classifier_rejects_foreign_servers_and_unknown_tools():
    # Lazy proxy for a DIFFERENT server is not ours.
    role, _ = _classify_mcp_comment_call({
        "tool_name": "call_mcp_tool",
        "tool_info": {"parameters": {"ServerName": "other", "ToolName": "post_plan",
                                     "Arguments": {"body": "x"}}},
    })
    assert role is None
    # An eager-named but foreign tool name is not ours.
    role, _ = _classify_mcp_comment_call({"tool_name": "mcp_other_post_plan"})
    assert role is None
    # A plain built-in tool.
    role, _ = _classify_mcp_comment_call({"tool_name": "write_file",
                                          "tool_info": {"parameters": {"path": "/x"}}})
    assert role is None


def test_classifier_body_never_non_string():
    role, body = _classify_mcp_comment_call({
        "tool_name": "mcp_autoswe_comment_post_plan",
        "tool_info": {"parameters": {"body": {"nope": 1}}},
    })
    assert (role, body) == ("plan", "")


def test_mcp_comment_active_trigger():
    assert _mcp_comment_active(_spec(mcp_servers={"autoswe_comment": {}})) is True
    assert _mcp_comment_active(_spec()) is False
    assert _mcp_comment_active(_spec(mcp_servers={})) is False
    assert _mcp_comment_active(_spec(mcp_servers={"autoswe_comment": "not-a-dict"})) is False


def test_eager_post_plan_sets_flags():
    acc, progress = _parse_lines(*_eager_pair(3, "mcp_autoswe_comment_post_plan", "the plan"))
    assert acc.plan_posted is True
    assert acc.plan_posted_body == "the plan"
    assert acc.question_posted is False
    assert any("Tool: mcp_autoswe_comment_post_plan" in p for p in progress)


def test_eager_post_plan_error_unsets_flag_and_body():
    err = {"type": "mcp_tool_error", "message": "provider 403"}
    acc, _ = _parse_lines(
        _step_event(4, "ACTIVE", "mcp_autoswe_comment_post_plan", {"body": "x"}),
        _step_event(4, "DONE", "mcp_autoswe_comment_post_plan", {"body": "x"}, error=err),
    )
    # §5.3: an answered-but-failed post must NOT suppress the text fallback.
    assert acc.plan_posted is False
    assert acc.plan_posted_body is None
    # The tool error is still collected (README lesson #4: not a run failure).
    assert acc.tool_errors and not acc.error_flag


def test_error_state_step_also_unsets():
    # Permission-denied posts end state=ERROR, not DONE — same treatment.
    err = {"type": "permission_denied", "message": "mcp(autoswe_comment/*) not allowed"}
    acc, _ = _parse_lines(
        _step_event(5, "ACTIVE", "mcp_autoswe_comment_post_question", {"body": "?"}),
        _step_event(5, "ERROR", "mcp_autoswe_comment_post_question", {"body": "?"}, error=err),
    )
    assert acc.question_posted is False


def test_second_plan_error_preserves_first_success():
    # Two post_plan calls: the FIRST lands, the SECOND fails.  The flag is a
    # count, not a latch — a failed re-push must not un-post the successful
    # one.  The body keeps the *last* non-empty intent (documented "last
    # non-empty body wins"): the planner's #241 re-push puts that newest
    # intended plan on the thread regardless.
    err = {"type": "mcp_tool_error", "message": "boom"}
    acc, _ = _parse_lines(
        *_eager_pair(1, "mcp_autoswe_comment_post_plan", "first"),
        _step_event(2, "ACTIVE", "mcp_autoswe_comment_post_plan", {"body": "second"}),
        _step_event(2, "DONE", "mcp_autoswe_comment_post_plan", {"body": "second"}, error=err),
    )
    assert acc.plan_posted is True
    assert acc.plan_posted_body == "second"


def test_ignored_plan_error_done_cannot_wipe_successful_plan():
    """Review regression (issue #322 round 2): the full unmasked chain —
    post_plan SUCCEEDS → post_question ACTIVE → second post_plan is IGNORED
    (question-terminal, so never counted) and must also never be tracked →
    the ignored call's ERROR terminal → question also ERRORs.  Before the
    fix the ignored call sat in ``_comment_steps`` un-counted, so its
    error-DONE decremented ``_plan_calls`` 1→0 and wiped the *successful*
    plan's flag/body.  Now only counted calls are tracked: plan_posted
    stays True with the original body; only the question is un-set."""
    from tests.fakes.antigravity_fake import (
        _mcp_comment_steps,
        _step_tool_active,
        _step_tool_done,
        _success_stream,
    )

    question_name = ANTIGRAVITY_COMMENT_TOOL_NAMES["post_question"]
    err = {"type": "mcp_tool_error", "message": "post failed"}
    steps = [
        *_mcp_comment_steps("s1", 1, "post_plan", "P"),                 # success
        _step_tool_active("s1", 2, question_name, {"body": "?"}),        # question in flight
        *_mcp_comment_steps("s1", 3, "post_plan", "ignored", error=err),  # ignored ACTIVE + ERROR
        _step_tool_done("s1", 2, question_name, error=err),              # question fails too
    ]
    stream = _success_stream("s1", "tag-free text", tool_steps=steps)
    proc = _MockProcess(stdout=("\n".join(stream) + "\n").encode())

    result, _ = _full_run(_spec(), proc)

    assert result.subtype == "success"
    assert result.plan_posted is True
    assert result.plan_posted_body == "P"
    assert result.question_posted is False


def test_question_terminal_precedence_and_risk_edge():
    """The Risks-test: question ACTIVE -> plan ACTIVE (must be ignored) ->
    question DONE WITH error -> NEITHER flag set -> text fallback stays live."""
    err = {"type": "mcp_tool_error", "message": "post_question failed"}
    with patch.object(agy_mod, "log") as mock_log:
        acc, _ = _parse_lines(
            _step_event(1, "ACTIVE", "mcp_autoswe_comment_post_question", {"body": "Which DB?"}),
            _step_event(2, "ACTIVE", "mcp_autoswe_comment_post_plan", {"body": "ignored plan"}),
            _step_event(1, "DONE", "mcp_autoswe_comment_post_question", {"body": "Which DB?"}, error=err),
        )
    msgs = [str(c.args[0]) for c in mock_log.call_args_list if c.args]
    assert any("post_plan ignored" in m and "question-terminal" in m for m in msgs)
    assert acc.plan_posted is False and acc.plan_posted_body is None
    assert acc.question_posted is False


def test_lazy_proxy_pair_classified_end_to_end():
    params = {"ServerName": "autoswe_comment", "ToolName": "post_plan",
              "Arguments": json.dumps({"body": "lazy plan"})}
    acc, _ = _parse_lines(
        _step_event(7, "ACTIVE", "call_mcp_tool", params),
        _step_event(7, "DONE", "call_mcp_tool", params),
    )
    assert acc.plan_posted is True
    assert acc.plan_posted_body == "lazy plan"


def test_update_progress_feeds_callback():
    long_body = "x" * 500
    acc, progress = _parse_lines(
        _step_event(9, "ACTIVE", "mcp_autoswe_comment_update_progress", {"body": long_body}),
        _step_event(9, "DONE", "mcp_autoswe_comment_update_progress", {"body": long_body}),
    )
    assert long_body[:200] in progress
    assert acc.plan_posted is False and acc.question_posted is False


def test_flags_survive_to_finalize():
    acc, _ = _parse_lines(*_eager_pair(1, "mcp_autoswe_comment_post_question", "?"))
    result = _finalize(acc, 0)
    assert result.question_posted is True
    assert result.plan_posted is False


# ---------- Phase 5 §5.1/§5.2: config merge & env routing ----------


def _mcp_spec(tmp_path, **overrides) -> RunSpec:
    """A spec that triggers the MCP path, with the global config retargeted at
    tmp_path (antigravity_mcp_config_path resolves home at call time, but the
    tests retarget the function itself for cross-platform determinism)."""
    servers = {
        "autoswe_comment": {
            "command": "/usr/bin/python3",
            "args": ["-m", "mcp_servers.autoswe_comment_server"],
            "env": {
                "AUTOSWE_COMMENT_ID": "991",
                "AUTOSWE_TOKEN": "ghp_secret",
                "AUTOSWE_SUPPRESS_POSTING": "0",
            },
        },
    }
    servers.update(overrides)
    return _spec(mcp_servers=servers)


@pytest.fixture()
def agy_config_path(tmp_path, monkeypatch):
    p = tmp_path / ".gemini" / "config" / "mcp_config.json"
    monkeypatch.setattr(agy_mod, "antigravity_mcp_config_path", lambda: p)
    return p


def test_merge_creates_file_with_stable_entry_only(agy_config_path):
    proc = _MockProcess(stdout=_fixture_text("basic_success").encode())
    _, captured = _full_run(_mcp_spec(agy_config_path.parent.parent), proc)

    written = json.loads(agy_config_path.read_text(encoding="utf-8"))
    assert set(written.keys()) == {"mcpServers"}
    entry = written["mcpServers"]["autoswe_comment"]
    assert entry["tools"]["post_plan"] == {"eager": True}
    assert entry["timeoutSeconds"] == 120
    # §5.2 hard rule: per-task values NEVER reach the file.
    assert "env" not in entry
    assert "ghp_secret" not in agy_config_path.read_text(encoding="utf-8")
    assert "991" not in agy_config_path.read_text(encoding="utf-8")
    # ...and they DO ride the agy subprocess env (M3).
    assert captured["env"]["AUTOSWE_COMMENT_ID"] == "991"
    assert captured["env"]["AUTOSWE_TOKEN"] == "ghp_secret"


def test_merge_preserves_foreign_servers_and_top_level_keys(agy_config_path):
    foreign = {
        "mcpServers": {
            "operator_server": {"command": "npx", "args": ["-y", "something"]},
            "autoswe_comment": {"command": "STALE", "args": []},
        },
        "someOperatorTopLevelKey": {"keep": "me"},
    }
    agy_config_path.parent.mkdir(parents=True)
    agy_config_path.write_text(json.dumps(foreign, indent=2) + "\n", encoding="utf-8")

    proc = _MockProcess(stdout=_fixture_text("basic_success").encode())
    _full_run(_mcp_spec(agy_config_path.parent.parent), proc)

    written = json.loads(agy_config_path.read_text(encoding="utf-8"))
    assert written["someOperatorTopLevelKey"] == {"keep": "me"}
    assert written["mcpServers"]["operator_server"] == foreign["mcpServers"]["operator_server"]
    # Only OUR entry was replaced.
    assert written["mcpServers"]["autoswe_comment"]["command"] != "STALE"


def test_merge_is_idempotent_compare_before_write(agy_config_path):
    proc = _MockProcess(stdout=_fixture_text("basic_success").encode())
    spec = _mcp_spec(agy_config_path.parent.parent)
    _full_run(spec, proc)
    first = agy_config_path.read_text(encoding="utf-8")

    # Age the mtime; a rewrite would refresh it.  Second run must NOT write.
    past = 1_000_000_000
    os.utime(agy_config_path, (past, past))
    _full_run(spec, proc)
    assert agy_config_path.read_text(encoding="utf-8") == first
    st = os.stat(agy_config_path)
    assert st.st_mtime == past  # skipped: stable values, stable bytes


def test_merge_repairs_corrupt_file(agy_config_path):
    agy_config_path.parent.mkdir(parents=True)
    agy_config_path.write_text("{not json", encoding="utf-8")
    proc = _MockProcess(stdout=_fixture_text("basic_success").encode())
    with patch.object(agy_mod, "log") as mock_log:
        _full_run(_mcp_spec(agy_config_path.parent.parent), proc)
    msgs = [str(c.args[0]) for c in mock_log.call_args_list if c.args]
    assert any("[WARN]" in m and "not valid JSON" in m for m in msgs)
    written = json.loads(agy_config_path.read_text(encoding="utf-8"))
    assert written["mcpServers"]["autoswe_comment"]["tools"]["post_plan"]["eager"] is True


def test_merge_write_failure_logs_and_never_raises(tmp_path, monkeypatch):
    # A directory where the file belongs: open(..., "w") raises OSError.
    path = tmp_path / "mcp_config.json"
    path.mkdir()
    monkeypatch.setattr(agy_mod, "antigravity_mcp_config_path", lambda: path)
    proc = _MockProcess(stdout=_fixture_text("basic_success").encode())
    with patch.object(agy_mod, "log") as mock_log:
        result, _ = _full_run(_mcp_spec(tmp_path), proc)  # must not raise
    assert result.subtype == "success"  # run proceeds — text fallback path
    msgs = [str(c.args[0]) for c in mock_log.call_args_list if c.args]
    assert any("could not write" in m for m in msgs)


def test_no_mcp_servers_never_touches_config(agy_config_path):
    proc = _MockProcess(stdout=_fixture_text("basic_success").encode())
    _, captured = _full_run(_spec(), proc)
    assert not agy_config_path.exists()
    assert "AUTOSWE_COMMENT_ID" not in captured["env"]


def test_inline_entry_written_without_env_but_env_routes(agy_config_path):
    proc = _MockProcess(stdout=_fixture_text("basic_success").encode())
    spec = _mcp_spec(
        agy_config_path.parent.parent,
        autoswe_inline_comment={
            "command": "/usr/bin/python3",
            "args": ["-m", "mcp_servers.autoswe_inline_comment_server"],
            "env": {"AUTOSWE_PR_NUMBER": "77", "AUTOSWE_COMMIT_SHA": "deadbeef"},
        },
    )
    _, captured = _full_run(spec, proc)

    from autoswe.harness.mcp_config import autoswe_repo_root

    written = json.loads(agy_config_path.read_text(encoding="utf-8"))
    inline = written["mcpServers"]["autoswe_inline_comment"]
    assert inline["args"] == ["-m", "mcp_servers.autoswe_inline_comment_server"]
    assert "env" not in inline
    # Review round-2: the inline entry must carry the SAME stable cwd as the
    # primary — the mcp_servers package only resolves from the autoSWE
    # checkout root, never from spec.cwd (the target worktree the agy
    # subprocess runs in, which the entry would otherwise inherit).
    assert inline["cwd"] == str(autoswe_repo_root())
    assert inline["cwd"] != "/tmp/repo"
    raw = agy_config_path.read_text(encoding="utf-8")
    assert "deadbeef" not in raw and "AUTOSWE_PR_NUMBER" not in raw
    assert captured["env"]["AUTOSWE_PR_NUMBER"] == "77"
    assert captured["env"]["AUTOSWE_COMMIT_SHA"] == "deadbeef"


def test_subprocess_entry_matches_mcp_config_builder(agy_config_path):
    """The file entry is exactly mcp_config.antigravity_comment_server output —
    no drift between the builder and what the backend writes."""
    from autoswe.harness.mcp_config import antigravity_comment_server, autoswe_repo_root

    proc = _MockProcess(stdout=_fixture_text("basic_success").encode())
    _full_run(_mcp_spec(agy_config_path.parent.parent), proc)
    written = json.loads(agy_config_path.read_text(encoding="utf-8"))
    assert written["mcpServers"]["autoswe_comment"] == antigravity_comment_server(
        sys.executable, autoswe_repo_root()
    )
