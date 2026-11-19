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
from pathlib import Path
from unittest.mock import patch

import pytest

import autoswe.harness.backends.antigravity as agy_mod
from autoswe.harness.backends.antigravity import (
    AntigravityBackend,
    _AgyAccumulator,
    _build_argv,
    _finalize,
    _kill_tree,
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


# ---------- argv / command construction ----------


def test_build_argv_fresh_shape():
    cmd = _build_argv(_spec(timeout=42), {}, None)
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
    monkeypatch.setattr(agy_mod.shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError) as exc:
        _resolve_agy_executable("")
    assert "cli_path" in str(exc.value)


def test_resolve_named_missing_path_raises():
    with pytest.raises(FileNotFoundError) as exc:
        _resolve_agy_executable("/definitely/not/here/agy")
    assert "/definitely/not/here/agy" in str(exc.value)


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
    # Phase 1 minimal honest set (mode/mcp/session_fork arrive later).
    assert backend.capabilities() == {"resume", "progress_stream", "structured_output"}
    # Fresh dict each call, Claude defaults until Phase 5.
    assert backend.comment_tool_names() == CLAUDE_COMMENT_TOOL_NAMES
    backend.comment_tool_names()["post_plan"] = "mutated"
    assert backend.comment_tool_names() == CLAUDE_COMMENT_TOOL_NAMES
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
