"""Scripted antigravity (``agy``) backend fake — subprocess level.

Replaces ``asyncio.create_subprocess_exec`` with a stub that returns a
prebuilt ``FakeProcess`` feeding ``--output-format stream-json`` event lines
crafted to exercise the real ``AntigravityBackend`` stream parser
(``_parse_line`` / ``_finalize`` in ``autoswe/harness/backends/antigravity.py``)
unmodified — the same fidelity contract as ``PiFake``/``CodexFake``.

The emitted lines are *real* stream-json shapes derived from the Phase 0
fixtures in ``tests/fixtures/antigravity/`` (``init`` -> ``step_update``* ->
``result``); nothing is hand-invented (``tests/test_antigravity_fake.py`` pins
every emitted line's key set to the union of the fixture keys).  The prompt is
delivered the genuine way — a single stdin line
``{"event":"user","message":{"content": ...}}`` — and parsed back into
``stdin_events`` on the call record so tests can assert the transport.

Builder signatures mirror ``ClaudeFake``/``CodexFake``/``PiFake`` so the
transition-row ``claude_responses`` dicts can be fed verbatim:
``script_response``, ``script_plan``, ``script_questions``, ``script_fix``,
``script_fail``, ``script_killed``, ``script_structured``.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

# Capture the real function at import time — before any test can patch.
# This is the only reference to the genuine function; patch/unpatch use it
# so the value is stable regardless of how many times the attribute is
# monkeypatched during the test run.
_REAL_CREATE = asyncio.create_subprocess_exec

# Fixture-derived defaults (tests/fixtures/antigravity/*.jsonl).  An empty
# tool registry keeps the init line small; the parser only counts it.
_DEFAULT_TOOLS: tuple[str, ...] = ("write_file", "read_file", "run_command")
# Permission posture values observed in the fixtures: the CLI stamps
# "always-proceed" with --dangerously-skip-permissions, "request-review"
# otherwise (the Phase 1 backend default is skip → always-proceed).
_PERMISSION_ALWAYS_PROCEED = "always-proceed"
_PERMISSION_REQUEST_REVIEW = "request-review"
# The zeroed usage block every fixture ``result`` carries (agy reports tokens
# only — cost is never estimated, spike lesson #7).
_ZERO_USAGE = {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0,
               "cache_read_tokens": 0, "total_tokens": 0}


# ---------------------------------------------------------------------------
# stream-json line builders (derived from tests/fixtures/antigravity/)


def _event(obj: dict) -> str:
    """Serialize one stream-json event line."""
    return json.dumps(obj)


def _init_line(cid: str, model: str = "gemini-test", cwd: str = "/tmp",
               tools: list[str] | None = None,
               permission_mode: str = _PERMISSION_ALWAYS_PROCEED,
               json_schema: dict | None = None) -> dict:
    """The first line of a stream-json run (basic_success.jsonl shape).

    ``json_schema`` is echoed on init only when a ``--json-schema`` run
    started (structured_output.jsonl shape).
    """
    init: dict[str, Any] = {
        "model": model,
        "cwd": cwd,
        "tools": list(tools if tools is not None else _DEFAULT_TOOLS),
        "permission_mode": permission_mode,
    }
    if json_schema is not None:
        init["json_schema"] = json_schema
    return {"event": "init", "conversation_id": cid, "init": init}


def _step_user_input(cid: str, index: int = 0) -> dict:
    """The ``user_input`` bookkeeping step (state DONE, no payload)."""
    return {"event": "step_update", "step_update": {
        "conversation_id": cid, "step_index": index,
        "state": "DONE", "step_type": "user_input",
    }}


def _step_agent_delta(cid: str, index: int, delta: str,
                      state: str = "ACTIVE") -> dict:
    """An ``agent_response`` text delta (ACTIVE partials stream live)."""
    return {"event": "step_update", "step_update": {
        "conversation_id": cid, "step_index": index,
        "state": state, "step_type": "agent_response", "text_delta": delta,
    }}


def _step_agent_done(cid: str, index: int,
                     duration_seconds: float = 1.0) -> dict:
    """The ``agent_response`` DONE terminator for a streamed response.

    The DONE remainder is a fresh chunk too (fixture-confirmed: concatenation
    of all deltas == result.response), so the fake ends with a remainder of
    the text not yet streamed — here the final newline, matching
    basic_success.jsonl.
    """
    return {"event": "step_update", "step_update": {
        "conversation_id": cid, "step_index": index,
        "state": "DONE", "step_type": "agent_response",
        "text_delta": "\n", "duration_seconds": duration_seconds,
    }}


def _step_tool_active(cid: str, index: int, tool_name: str,
                      parameters: dict) -> dict:
    """A ``tool`` step in flight (killed_partial.jsonl / tool_steps shape)."""
    return {"event": "step_update", "step_update": {
        "conversation_id": cid, "step_index": index,
        "state": "ACTIVE", "step_type": "tool",
        "tool_name": tool_name,
        "tool_info": {"name": tool_name, "parameters": parameters},
    }}


def _step_tool_done(cid: str, index: int, tool_name: str,
                    output: str | None = None,
                    error: dict | None = None) -> dict:
    """A completed ``tool`` step; *error* carries the tool_error_recovered shape."""
    tool_info: dict[str, Any] = {"name": tool_name}
    if output is not None:
        tool_info["output"] = output
    if error is not None:
        tool_info["error"] = error
    return {"event": "step_update", "step_update": {
        "conversation_id": cid, "step_index": index,
        "state": "ERROR" if error is not None else "DONE",
        "step_type": "tool",
        "tool_name": tool_name,
        "tool_info": tool_info,
    }}


def _result_line(cid: str, status: str, response: str = "",
                 error: str | None = None,
                 finished_reason: str | None = None,
                 structured_output: dict | None = None,
                 json_schema: dict | None = None,
                 duration_seconds: float = 1.0,
                 num_turns: int = 1,
                 usage: dict | None = None) -> dict:
    """The terminal ``result`` event (status/reason tables in the backend)."""
    result: dict[str, Any] = {
        "conversation_id": cid,
        "status": status,
        "response": response,
        "duration_seconds": duration_seconds,
        "num_turns": num_turns,
        "usage": usage if usage is not None else dict(_ZERO_USAGE),
    }
    if error is not None:
        result["error"] = error
    if finished_reason is not None:
        result["finished_reason"] = finished_reason
    if structured_output is not None:
        result["structured_output"] = structured_output
    if json_schema is not None:
        result["json_schema"] = json_schema
    return {"event": "result", "result": result}


# ---------------------------------------------------------------------------
# Stream builders per scripted kind


def _success_stream(cid: str, text: str, *,
                    structured: dict | None = None,
                    json_schema: dict | None = None,
                    tool_steps: list[dict] | None = None) -> list[str]:
    """init → user_input → [tool steps] → agent_response deltas → result SUCCESS.

    With *structured*, the response also carries ``structured_output`` (and
    optionally the echoed ``json_schema``), matching structured_output.jsonl:
    the CLI also echoes the JSON into the response text, but the fake keeps
    the response clean — the parser reads ``structured_output`` off the result.
    """
    lines: list[dict] = [_init_line(cid)]
    idx = 0
    lines.append(_step_user_input(cid, idx))
    idx += 1
    for step in tool_steps or []:
        lines.append(step)
        idx += 1
    if text:
        lines.append(_step_agent_delta(cid, idx, text))
        lines.append(_step_agent_done(cid, idx))
    response = text if text.endswith("\n") or not text else text + "\n"
    lines.append(_result_line(
        cid, "SUCCESS", response=response,
        structured_output=structured, json_schema=json_schema,
    ))
    return [_event(line) for line in lines]


def _error_stream(cid: str, error_msg: str) -> list[str]:
    """init → user_input → result ERROR with the CLI's error text (rc 0)."""
    lines = [
        _init_line(cid),
        _step_user_input(cid),
        _result_line(cid, "ERROR", error=error_msg),
    ]
    return [_event(line) for line in lines]


def _waiting_stream(cid: str, text: str) -> list[str]:
    """A run that ended WAITING on interactive input (Phase 1: error subtype).

    The ``result`` status is the only difference from the success stream —
    Phase 1 maps WAITING to a plain error and logs the headless note.
    """
    lines = [
        _init_line(cid),
        _step_user_input(cid),
    ]
    if text:
        lines.append(_step_agent_delta(cid, 1, text))
        lines.append(_step_agent_done(cid, 1))
    response = text if text.endswith("\n") or not text else text + "\n"
    lines.append(_result_line(cid, "WAITING", response=response))
    return [_event(line) for line in lines]


def _killed_stream(cid: str, partial_text: str = "") -> list[str]:
    """killed_partial.jsonl shape: a tool in flight, then result ERROR /
    finished_reason "interrupted" (the CLI trapped our SIGTERM).

    The backend maps finished_reason "interrupted" to subtype "killed"
    regardless of exit code, so the fake keeps rc 0 — the stream shape is
    what the CLI actually produces on kill (T8).
    """
    lines = [
        _init_line(cid),
        _step_user_input(cid),
    ]
    if partial_text:
        lines.append(_step_agent_delta(cid, 1, partial_text))
    lines.append(_step_tool_active(cid, 2, "run_command",
                                   {"CommandLine": "sleep 30"}))
    lines.append(_result_line(cid, "ERROR", error="interrupted",
                              finished_reason="interrupted"))
    return [_event(line) for line in lines]


def _killed_rc_stream(cid: str, partial_text: str = "") -> list[str]:
    """A stream truncated with NO result event + rc -9 (signal death).

    Exercises _finalize's ``returncode < 0`` branch: text comes from the
    accumulated text_delta fallback, subtype "killed".
    """
    lines = [
        _init_line(cid),
        _step_user_input(cid),
    ]
    if partial_text:
        lines.append(_step_agent_delta(cid, 1, partial_text))
    return [_event(line) for line in lines]


def _config_error_stream(cid: str, error_text: str) -> list[str]:
    """unknown_model.jsonl shape: a bare ERROR ``result`` (no init) carrying
    the CLI's config-level message.

    The backend's ``_is_config_error`` pattern match turns this into subtype
    "config_error" with the CLI text as RunResult.text — no retry.
    """
    return [_event(_result_line(cid, "ERROR", error=error_text))]


# The default config-error text: the real "unknown model" message observed in
# tests/fixtures/antigravity/unknown_model.jsonl (kept short; the match only
# needs one of the backend's _CONFIG_ERROR_PATTERNS).
_DEFAULT_CONFIG_ERROR = (
    'invalid model selection (--model "does-not-exist"): model does-not-exist '
    "is not recognized as a known model or custom model in settings"
)


# ---------------------------------------------------------------------------
# Fake async subprocess plumbing


class FakeStreamReader:
    """Async streamreader feeding lines from a prebuilt list."""

    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)
        self._index = 0

    async def readline(self) -> bytes:
        """Return next line bytes, or b"" when exhausted (signals EOF)."""
        if self._index >= len(self._lines):
            return b""
        line = self._lines[self._index]
        self._index += 1
        return line

    async def read(self, size: int = -1) -> bytes:
        """Return remaining bytes (stderr drain path — empty unless scripted)."""
        if self._index >= len(self._lines):
            return b""
        rest = b"".join(self._lines[self._index:])
        self._index = len(self._lines)
        return rest


class FakeStdin:
    """Async stdin sink that parses the stdin NDJSON events written to it.

    The backend writes the prompt as one
    ``{"event":"user","message":{"content": ...}}`` line and closes the pipe
    (T2 transport).  This parses each line back into JSON and hands the event
    list to *record* so the owning call record can expose ``stdin_events``.
    """

    def __init__(self, record=None):
        self._record = record
        self._buf = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self._buf.extend(data)

    async def drain(self) -> None:
        """Parse the buffered NDJSON and hand the events to record."""
        if self._record is None:
            return
        events: list[dict] = []
        for raw in self._buf.decode("utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                events.append(json.loads(raw))
            except json.JSONDecodeError:
                events.append({"_raw": raw})
        self._record(events)

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    """Subprocess stand-in for asyncio.create_subprocess_exec.

    The AntigravityBackend reads stdout line-by-line, drains stderr with
    ``read()``, and writes one stdin event line.  This fake feeds prebuilt
    stream-json lines on stdout, optional scripted stderr (the pre-result
    config-death path), and a stdin sink that records the parsed events.
    ``pid`` exists because ``_kill_tree`` reads it on the hard-kill path.
    """

    def __init__(self, stdout_lines: list[bytes], returncode: int = 0,
                 stderr_bytes: bytes = b"", record_stdin=None):
        self.stdout = FakeStreamReader(stdout_lines)
        self.stderr = FakeStreamReader([stderr_bytes] if stderr_bytes else [])
        self.stdin = FakeStdin(record=record_stdin)
        self.returncode = returncode
        self.pid = -1  # a fake pid: killpg raises → _kill_tree falls back to kill()
        self.killed = False

    def kill(self) -> None:
        """Record a kill — the process is already 'done'."""
        self.killed = True

    async def wait(self) -> int:
        """Return the canned returncode (idempotent — the backend awaits twice)."""
        return self.returncode


# ---------------------------------------------------------------------------
# Command parser


# agy flags that take a following value (_build_argv in the backend).
_VALUE_FLAGS = {
    "--input-format": "input_format",
    "--output-format": "output_format",
    "--model": "model",
    "--effort": "effort",
    "--agent": "agent",
    "--json-schema": "json_schema_path",
    "--print-timeout": "print_timeout",
    "--log-file": "log_file",
    "--conversation": "conversation",
}
_BOOL_FLAGS = {
    "--disable-slash-commands": "disable_slash_commands",
    "--dangerously-skip-permissions": "skip_permissions",
    "--sandbox": "sandbox",
}


def _parse_command(cmd: list[str]) -> dict:
    """Extract the agy flag argv from a spawned command list.

    Records every flag ``_build_argv`` may emit (``--model``,
    ``--json-schema`` (the structured-output schema temp file),
    ``--conversation`` (the resume flag), the always-on ``--disable-slash-
    commands``, the Phase 1 posture ``--dangerously-skip-permissions``, …) and
    derives the session convenience booleans: agy has no fork primitive, so
    ``is_resume`` means "``--conversation`` present" and everything else is
    fresh.  ``cwd`` / ``env`` / ``stdin_events`` are filled in by the caller
    (spawn kwargs and the stdin sink respectively).
    """
    result: dict[str, Any] = {"argv": list(cmd)}

    # A Windows .cmd/.bat shim is invoked through `cmd /c <agy> ...`
    # (mirrors the pi fake's prefix detection).
    prefix: list[str] = []
    start = 0
    if len(cmd) >= 3 and cmd[0] == "cmd" and cmd[1] == "/c":
        prefix = ["cmd", "/c"]
        start = 2
    result["prefix"] = prefix
    result["executable"] = cmd[start] if start < len(cmd) else ""

    i = start
    while i < len(cmd):
        part = cmd[i]
        if part in _VALUE_FLAGS:
            if i + 1 < len(cmd):
                result[_VALUE_FLAGS[part]] = cmd[i + 1]
                i += 2
                continue
        elif part in _BOOL_FLAGS:
            result[_BOOL_FLAGS[part]] = True
            i += 1
            continue
        i += 1

    # The prompt is NOT on the command line: it rides the single stdin event
    # line (Windows `cmd /c` ~8191-char limit), so ``prompt`` is seeded empty
    # and filled by the stdin sink when the backend writes it.
    result["prompt"] = ""
    result["prompt_prefix"] = ""

    result.setdefault("skip_permissions", False)
    result.setdefault("disable_slash_commands", False)
    result["is_resume"] = "conversation" in result
    result["is_fresh"] = not result["is_resume"]
    return result


# ---------------------------------------------------------------------------
# AntigravityFake


class AntigravityFake:
    """Scripted antigravity backend fake (subprocess level).

    Unlike ClaudeFake (which patches runner.run), AntigravityFake patches
    ``asyncio.create_subprocess_exec`` so the real factory → AntigravityBackend
    → stream-json parser → RunResult path runs unmodified.

    It also pins ``antigravity._resolve_agy_executable`` to ``("agy", [])``
    while patched: ``agy`` is deliberately NOT on the test host's PATH (the
    backend raises a non-retryable RuntimeError otherwise), and the resolution
    itself is tested against the real resolver in ``test_antigravity_backend.py``.

    Can be used as a context manager (``with AntigravityFake():``) to
    auto-patch/unpatch, or manually via ``patch()``/``unpatch()``.

    Attributes (mutable, for assertions):
        calls - list[dict]: one per spawn, with ``argv`` (raw command), parsed
                flags (``model``, ``conversation``, ``json_schema_path``,
                ``skip_permissions``, …), ``cwd``, ``env``, ``stdin_events``
                (parsed stdin NDJSON) and ``prompt``.
    """

    def __init__(self):
        # Scripted streams: (lines, returncode, stderr_bytes) triples built
        # ahead of time by the builders below.
        self._scripts: list[tuple[list[str], int, bytes]] = []
        self._call_index = 0
        self.calls: list[dict[str, Any]] = []
        self._patch_tuple = None  # (module, original) stored by patch()
        self._resolver_saved = None
        self._resolver_mod = None

    def __enter__(self):
        """Auto-patch on context manager entry."""
        self._patch_tuple = self.patch()
        return self

    def __exit__(self, *exc):
        """Auto-unpatch on context manager exit."""
        self.unpatch()
        return False

    # -- Script append helper --

    def _add(self, lines: list[str], returncode: int = 0,
             stderr: bytes = b"") -> None:
        self._scripts.append((lines, returncode, stderr))

    # -- Builders (mirror ClaudeFake / CodexFake / PiFake signatures) --

    def script_response(self, text: str, session_id: str = "s1",
                        subtype: str = "success",
                        structured_output: dict | None = None,
                        error: str | None = None,
                        json_schema: dict | None = None,
                        status: str | None = None) -> None:
        """Add a response to the script.  Order matters — each agy run consumes the next.

        *subtype* selects the stream shape: ``success`` (rc 0 SUCCESS),
        ``error`` (ERROR result), ``killed`` (interrupted result),
        ``killed_rc`` (truncated stream + rc -9), ``config_error`` (bare ERROR
        result naming a config-level failure).  *status* overrides the result
        status (e.g. ``"WAITING"`` for the headless question flow).
        """
        self.script_kind(text, session_id, subtype,
                         structured_output=structured_output, error=error,
                         json_schema=json_schema, status=status)

    def script_kind(self, text: str, session_id: str, kind: str, *,
                    structured_output: dict | None = None,
                    error: str | None = None,
                    json_schema: dict | None = None,
                    status: str | None = None,
                    tool_steps: list[dict] | None = None) -> None:
        """Route one scripted response to its stream builder."""
        if kind == "success":
            if status == "WAITING":
                self._add(_waiting_stream(session_id, text))
            else:
                self._add(_success_stream(
                    session_id, text, structured=structured_output,
                    json_schema=json_schema, tool_steps=tool_steps))
        elif kind == "error":
            self._add(_error_stream(session_id, error or text or "error"))
        elif kind == "killed":
            self._add(_killed_stream(session_id, text))
        elif kind == "killed_rc":
            self._add(_killed_rc_stream(session_id, text), returncode=-9)
        elif kind == "config_error":
            self._add(_config_error_stream(
                session_id, error or _DEFAULT_CONFIG_ERROR))
        else:
            raise ValueError(f"unknown scripted stream kind: {kind!r}")

    def script_plan(self, plan_md: str, session_id: str = "s1") -> None:
        """Add a plan-phase response.

        Wraps the plan in ``<AUTOSWE_PLAN>`` tags in the assistant response —
        the planner's tag parsing must keep working (agy has no plan file and
        no MCP yet, so text-pattern plan capture is the Phase 3 path; the
        structured-output path is ``script_structured``).
        """
        self.script_response(
            f"<AUTOSWE_PLAN>{plan_md}</AUTOSWE_PLAN>", session_id=session_id)

    def script_questions(self, questions: str, session_id: str = "s1",
                         *, mcp: bool = False,
                         mcp_body: str | None = None) -> None:
        """Add a plan-phase response with questions (text-pattern path).

        *mcp* is the Phase 5 switch and stays off for now: when enabled it
        emits an eager ``mcp__autoswe_comment__post_question`` tool step (the
        Claude-spelling name the backend's comment_tool_names advertises).
        Note that without the "mcp" capability the real parser will NOT set
        ``question_posted`` — the tags in the response text are what drive
        WAITING through Phase 3/7.
        """
        tool_steps = None
        if mcp:
            tool_steps = [_step_tool_active(
                session_id, 1, "mcp__autoswe_comment__post_question",
                {"body": mcp_body if mcp_body is not None else questions})]
        self._add(_success_stream(
            session_id, f"<AUTOSWE_QUESTIONS>{questions}</AUTOSWE_QUESTIONS>",
            tool_steps=tool_steps))

    def script_fix(self, summary: str = "Changes applied.",
                   files: tuple[str, ...] = (),
                   session_id: str = "s1") -> None:
        """Add a fix-phase response.

        *files* emits one in-flight ``write_file`` tool step per path (the
        stream's T3 step-level tool name; parameters key ``Path``) so progress
        shows the edits.  The ACTUAL worktree changes are not the fake's job —
        they are simulated the pi-fake way, by GitFake / the real-git sandbox,
        so ``commit_and_push`` sees real changes.
        """
        tool_steps = [
            _step_tool_active(session_id, 1 + i, "write_file", {"Path": path})
            for i, path in enumerate(files)
        ]
        self._add(_success_stream(session_id, summary, tool_steps=tool_steps))

    def script_fail(self, error: str = "error", session_id: str = "s1") -> None:
        """Schedule an ERROR ``result`` (subtype "error", retryable)."""
        self._add(_error_stream(session_id, error))

    def script_killed(self, session_id: str = "s1",
                      partial_text: str = "") -> None:
        """Schedule a killed run (interrupted result → subtype "killed")."""
        self._add(_killed_stream(session_id, partial_text))

    def script_killed_rc(self, session_id: str = "s1",
                         partial_text: str = "") -> None:
        """Schedule a stream truncated by SIGKILL (no result, rc -9)."""
        self._add(_killed_rc_stream(session_id, partial_text), returncode=-9)

    def script_config_error(self, error_text: str = _DEFAULT_CONFIG_ERROR,
                            session_id: str = "s1") -> None:
        """Schedule a config-level failure (unknown model / auth → no retry).

        ``RunResult.text`` carries the CLI's own message (the FAILED comment
        must name the fix).
        """
        self._add(_config_error_stream(session_id, error_text))

    def script_structured(self, payload: dict, session_id: str = "s1",
                          schema: dict | None = None,
                          text: str = "") -> None:
        """Add a structured-output response (``--json-schema`` runs, T6 shape).

        The payload lands on ``RunResult.structured_output``; *schema* echoes
        the json_schema block onto the result (advisory — handlers validate
        downstream).
        """
        self._add(_success_stream(session_id, text,
                                  structured=payload, json_schema=schema))

    def script_spawn_oserror(self) -> None:
        """Schedule a spawn-time OSError from create_subprocess_exec.

        The backend re-wraps it as a retryable OSError; the runner retries
        under ``AntigravityBackend.retryable_exceptions()``.
        """
        self._scripts.append(("__raise_oserror__", 0, b""))  # type: ignore[arg-type]

    # -- JSONL generator --

    def _next_stream(self) -> tuple[list[bytes], int, bytes, bool]:
        """(stdout_lines, returncode, stderr_bytes, raise_oserror) for next call."""
        if self._call_index >= len(self._scripts):
            lines, rc, stderr = _success_stream("s-default", ""), 0, b""
        else:
            entry = self._scripts[self._call_index]
            if entry[0] == "__raise_oserror__":
                return [], 0, b"", True
            lines, rc, stderr = entry
        encoded = [line.encode("utf-8") + b"\n" for line in lines]
        return encoded, rc, stderr, False

    # -- Patch plumbing --

    @classmethod
    def _get_real_create(cls):
        """Return the original ``asyncio.create_subprocess_exec``.

        The module-level ``_REAL_CREATE`` is captured once at import time —
        before any test can patch — so this always returns the genuine
        function regardless of how many times the attribute has been
        monkeypatched.
        """
        return _REAL_CREATE

    def patch(self):
        """Patch asyncio.create_subprocess_exec (+ the agy executable resolver).

        Returns (module, original) for unpatching.
        """
        import autoswe.harness.backends.antigravity as agy_mod

        self._saved_original = self._get_real_create()

        # Pin executable resolution: the agy binary is deliberately absent on
        # test hosts and a fake must not depend on host PATH.  Resolution
        # semantics (RuntimeError on a bad cli_path, cmd/powershell shims) are
        # tested directly against the real function in test_antigravity_backend.py.
        self._resolver_mod = agy_mod
        self._resolver_saved = agy_mod._resolve_agy_executable
        agy_mod._resolve_agy_executable = lambda cli_path: ("agy", [])

        def _make_process(cmd, kwargs):
            """Build a FakeProcess for the next scripted response."""
            call = _parse_command(list(cmd))
            call["cwd"] = kwargs.get("cwd")
            call["env"] = kwargs.get("env")
            call["stdin_events"] = []
            self.calls.append(call)

            stdout_lines, returncode, stderr_bytes, raise_oserror = \
                self._next_stream()
            self._call_index += 1
            if raise_oserror:
                raise OSError("fake spawn failure")

            # The prompt rides stdin, not argv: record the parsed stdin
            # events on the call dict so tests can assert the transport
            # (and confirm the command line stayed short).
            def _record_stdin(events: list[dict]) -> None:
                call["stdin_events"] = events
                for ev in events:
                    if ev.get("event") == "user":
                        content = (ev.get("message") or {}).get("content")
                        if isinstance(content, str):
                            call["prompt"] = content
                            call["prompt_prefix"] = content[:80]
                        break

            return FakeProcess(stdout_lines, returncode=returncode,
                               stderr_bytes=stderr_bytes,
                               record_stdin=_record_stdin)

        async def fake_create_subprocess_exec(*cmd, **kwargs):
            return _make_process(cmd, kwargs)

        asyncio.create_subprocess_exec = fake_create_subprocess_exec

        self._patch_tuple = (asyncio, self._saved_original)
        return asyncio, self._saved_original

    def unpatch(self, module=None, original=None) -> None:
        """Restore asyncio.create_subprocess_exec and the agy resolver."""
        if module is None and self._patch_tuple:
            module, original = self._patch_tuple
        if module is not None:
            module.create_subprocess_exec = self._get_real_create()
        if self._resolver_mod is not None and self._resolver_saved is not None:
            self._resolver_mod._resolve_agy_executable = self._resolver_saved
            self._resolver_saved = None
            self._resolver_mod = None
