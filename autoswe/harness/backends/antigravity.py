"""Antigravity backend — implements CodingBackend via the ``agy`` CLI.

Shells out to the antigravity CLI (``agy``) subprocess, maps a harness-agnostic
``RunSpec`` to CLI flags, and parses the JSON event stream (one JSON object per
line on stdout) into a ``RunResult``.  This is the Python-side integration for
the same shape as the Codex/pi backends: ``agy --input-format stream-json
--output-format stream-json`` emits NDJSON events (``init`` -> ``step_update``*
-> ``result``) and exits on completion (docs/antigravity/cli-headless.md;
transport facts recorded in ``docs/autoswe/spike-antigravity.md``).

The prompt is **not** passed in argv.  It is written to the subprocess's
**stdin** as a single ``{"event":"user","message":{"content": ...}}`` line
(spike lesson #1 / T2: stream-json input reads one NDJSON message per line and
runs a turn for each; closing stdin ends the run).  This keeps a 10-20 KB
``/fix`` prompt off the command line (Windows' ``cmd /c`` shim caps at ~8191
chars) and avoids quoting hazards entirely.

**Capabilities (Phase 1):** ``resume``, ``progress_stream``,
``structured_output`` only — the minimal honest set.  ``mode`` and ``mcp``
arrive in Phases 4/5; without ``mode``/``can_use_tool``,
``has_read_only_enforcement`` is False so plan/review loudly degrade and rely
on the worktree-rollback backstop.  There is **no fork primitive**: ``agy``
resumes a conversation in place (``--conversation``), so ``fork_session`` is
logged and treated as resume (the runner's capability gate keeps it from
happening; the log line is a guard).

``--disable-slash-commands`` is **always** passed: prompts embed
issue-comment text that routinely starts with ``/plan``, ``/fix``, etc.
(T11: without the flag the CLI expands the slash text).

``cost_usd`` is **always None** — the stream carries token counts (``usage``)
but no provider-reported cost, and we never estimate (spike lesson #7).  The
``usage`` block is logged once per run at info level so operators get the only
cost signal in ``logs/<slug>/``.

``--print-timeout`` is set to ``spec.timeout`` seconds so the CLI ends cleanly
with a terminal ``result`` before our hard kill (``asyncio.wait_for`` uses
``spec.timeout + 30`` as the backstop).  On that hard-kill path we kill the
**process tree** (``_kill_tree``): ``agy`` spawns child processes, and
``process.kill()`` alone would orphan them under the ``cmd``/powershell shim.

Session identity is the ``conversation_id`` carried on ``init`` and echoed on
``result``.  Unlike pi there is no id we can pre-seed: it is only known once
the stream starts, so a run that dies before ``init`` reports
``session_id=None`` (same parse-only path as Codex).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass, field

from autoswe.core.logging_utils import get_debug_logger, log
from autoswe.harness.backends.base import (
    CLAUDE_COMMENT_TOOL_NAMES,
    RunResult,
    RunSpec,
)

# Internal diagnostics (forward-compat skips) go to the debug logger only —
# plan §1.5 asks for debug level, and log() also prints to stdout.
_dbg = get_debug_logger()

# Max bytes allowed on stdout/stderr pipes before we truncate and flag the
# turn as failed.  Prevents pipe-buffer deadlock (~64KB on Linux) and
# unbounded memory growth when the child produces pathological output.
# Mirrors codex.py/pi.py's bound; T13 confirmed the CLI itself bounds tool
# output well below this (largest observed stream line ~10.6 KB).
_MAX_STREAM_BYTES = 16 * 1024 * 1024  # 16 MB

# stderr is only kept for the failure log: retain the TAIL, last N bytes.
_STDERR_TAIL_BYTES = 64 * 1024

# Backstop added to spec.timeout for our asyncio.wait_for.  The CLI's own
# --print-timeout fires at spec.timeout and ends the run with a terminal
# ``result`` (finished_reason=print_timeout, exit 3); we only reach this if
# the CLI itself is wedged.
_HARD_TIMEOUT_SLACK = 30

# Statuses the CLI may stamp on a terminal ``result`` event.  RUNNING should
# never reach a result line; it is included defensively so an unexpected
# status maps to a sane subtype instead of falling through the mapping.
_TERMINAL_STATUSES = frozenset(
    {"SUCCESS", "WAITING", "CANCELED", "INTERRUPTED", "ERROR", "INVALID", "RUNNING"}
)

# result.status -> RunResult.subtype (Phase 1 table; WAITING becomes the
# question flow only in Phase 7 — for now it is a plain error).
_STATUS_TO_SUBTYPE = {
    "SUCCESS": "success",
    "WAITING": "error",
    "CANCELED": "killed",
    "INTERRUPTED": "killed",
    "ERROR": "error",
    "INVALID": "error",
    "RUNNING": "error",
}

# ``finished_reason`` values (T8/T9) that corroborate the subtype beyond the
# status field: "interrupted" means the CLI trapped our SIGTERM and wrote a
# final result before dying — that is a kill, not a failure.
_INTERRUPTED_REASONS = frozenset({"interrupted"})

# Config-level failures (spike §0.1.3 auth timeout, T5 unknown model): the CLI
# cannot be made to work by retrying — the profile/host config is wrong.  They
# get subtype="config_error" (outside retryable_subtypes) and the CLI's own
# error text on RunResult.text so the FAILED comment names the fix.
_CONFIG_ERROR_PATTERNS = (
    "not recognized as a known model",
    "invalid model selection",
    "authentication failed",
    "authentication required",
    "authentication timed out",
)

# stderr markers of the headless soft-deny notice (spike P3b): a tool that
# would have prompted is auto-denied in print mode.  Not a crash — the first
# sign of a permission misconfiguration, so it gets a loud [WARN] line.
_SOFT_DENY_MARKERS = (
    "headless mode cannot prompt",
    "so it was auto-denied",
)

# Progress-label verbs per stream tool name (T3: the step-level names are
# write_file/read_file/run_command; parameters keys are Path / CommandLine).
_FILE_TOOL_VERBS = {
    "write_file": "write",
    "write_to_file": "write",
    "read_file": "read",
    "view_file": "read",
    "replace_file_content": "edit",
    "multi_replace_file_content": "edit",
    "sed_file": "edit",
    "notebook_edit": "edit",
    "list_dir": "list",
    "grep_search": "grep",
}


# ---------- Streaming accumulator ----------


@dataclass
class _AgyAccumulator:
    """Accumulates state during agy stream-json parsing.

    Passed directly to ``_parse_line`` so the parser mutates the accumulator
    in-place (avoids list-wrapper indirection).  Mirrors _PiAccumulator.
    """

    # Conversation id (== RunResult.session_id).  Set from ``init`` and
    # overridden by ``result`` when they differ (logged).  None until the
    # stream starts — unlike pi there is no id we pass and can pre-seed.
    conversation_id: str | None = None
    # ``init`` payload bits kept for Phase 4/5 assertions and cwd checks.
    init_cwd: str | None = None
    init_tools: list = field(default_factory=list)
    # Authoritative text: result.response, captured on the ``result`` event.
    final_response: str | None = None
    # Per-step_index accumulated text_delta chunks in stream order.  Fallback
    # source for RunResult.text when no result event arrives (killed stream).
    # Keyed by step_index so a multi-step run reassembles in order; ACTIVE
    # partials and the DONE remainder are *both* fresh chunks (T1/T3: the
    # concatenation of all deltas equals result.response), so appending every
    # delta never double-counts.
    _text_by_index: dict[int, list[str]] = field(default_factory=dict)
    # Per-step_index cumulative char count / fire point for delta-progress
    # throttling (see _fire_delta_progress).
    _delta_streamed: dict[int, int] = field(default_factory=dict)
    _delta_fired_at: dict[int, int] = field(default_factory=dict)
    # result-event fields.
    saw_result: bool = False
    status: str | None = None
    finished_reason: str | None = None
    error: str | None = None
    structured_output: dict | None = None
    usage: dict | None = None
    num_turns: int | None = None
    # Stream-overflow flag (readline ValueError / stdout byte budget).  Set by
    # the stdout reader, not _parse_line.
    error_flag: bool = False
    # Tool-level errors seen on step_update (README lesson #4: a failed tool
    # is NOT a run failure — do NOT set error_flag here).
    tool_errors: list[str] = field(default_factory=list)


# ---------- Executable resolution ----------


def _resolve_agy_executable(cli_path: str | None) -> tuple[str, list[str]]:
    """Resolve the agy executable and its argv prefix.

    Precedence: the caller-supplied path (already merged from
    ``spec.cli_path`` -> ``harness_cfg["cli_path"]``) → ``shutil.which("agy")``.
    Unlike pi, a bare fallback name is NOT accepted: agy is not on the default
    non-login shell PATH on the host (spike "PATH caveat"), so a
    non-resolving lookup is a *config error* and raises
    ``RuntimeError`` naming the attempted path.  RuntimeError is outside
    ``retryable_exceptions()`` (unlike FileNotFoundError, which is an OSError
    subclass the runner *does* retry), so a wrong config surfaces once
    instead of burning every attempt — same precedent as
    ``pi.py`` (``_run_async``'s spawn-time FileNotFoundError → RuntimeError
    conversion).

    Windows shims (Phase 0 windows-pending, implemented per plan): an npm-style
    ``.cmd``/``.bat`` shim cannot be exec'd directly (CreateProcess), so we
    invoke through ``cmd /c <path>``; a ``.ps1`` shim goes through
    ``powershell -NoProfile -ExecutionPolicy Bypass -File <path>``.

    Returns ``(exe, prefix_argv)`` where *prefix_argv* is inserted between
    *exe* and the agy flags (``["/c", path]`` for a cmd shim, the powershell
    flags for a ps1 shim, else empty).
    """
    path = (cli_path or "").strip()
    explicit = bool(path)
    if not path:
        path = shutil.which("agy") or ""
    if not path:
        raise RuntimeError(
            "agy executable not found: neither the harness profile's "
            "'cli_path' nor shutil.which('agy') resolved. Set 'cli_path' to "
            "the absolute agy path (e.g. ~/.local/bin/agy) — agy is not on "
            "the default non-login shell PATH."
        )
    lowered = path.lower()
    if lowered.endswith((".cmd", ".bat")):
        return "cmd", ["/c", path]
    if lowered.endswith(".ps1"):
        return "powershell", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path]
    if explicit and not os.path.exists(path):
        # A PATH lookup result is trusted (which() already verified it); an
        # explicitly configured path that does not exist is a config error we
        # name here so the message points at the profile, not at exec().
        raise RuntimeError(f"agy cli_path does not exist: {path!r}")
    return path, []


# ---------- Command construction ----------


def _build_argv(spec: RunSpec, harness_cfg: dict, schema_path: str | None) -> list[str]:
    """Build the agy flag argv (after the exe prefix) for one run.

    | Case | session flags |
    |---|---|
    | fresh | (none — the CLI creates a conversation) |
    | resume | ``--conversation <spec.resume>`` |
    | fork  | **not supported**: logged and treated as resume |

    ``schema_path`` is the caller-created temp file holding the JSON schema
    (only set when ``spec.output_format`` is present); the schema is never
    passed inline — it is too long for the Windows command line and has
    quoting hazards.
    """
    # stream-json in AND out: stdin NDJSON one-event-per-line transport,
    # stdout NDJSON event stream.  --disable-slash-commands is unconditional
    # (T11): issue-comment prompts must never be expanded as CLI commands.
    cmd = [
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--disable-slash-commands",
    ]

    # Model.  The factory enforces model presence for reproducibility; this
    # guard (and the one in _run_async) protects direct calls.
    model = str(spec.model or "").strip()
    if model:
        cmd.extend(["--model", model])

    # Optional profile knobs (Phase 2 documents these fields).
    effort = str(harness_cfg.get("effort") or "").strip()
    if effort:
        cmd.extend(["--effort", effort])
    agent = str(harness_cfg.get("agent") or "").strip()
    if agent:
        cmd.extend(["--agent", agent])

    # Phase 1 posture: emit the skip flag whenever the profile's
    # skip_permissions is true (the dedicated-machine default, same posture as
    # Codex bypass_approvals).  Phase 4 narrows this to read_write runs only.
    if bool(harness_cfg.get("skip_permissions", True)):
        cmd.append("--dangerously-skip-permissions")
    if bool(harness_cfg.get("sandbox")):
        cmd.append("--sandbox")

    if schema_path:
        cmd.extend(["--json-schema", schema_path])

    # The CLI's own deadline: end cleanly with a terminal result (exit 3,
    # finished_reason=print_timeout) before our wall-clock kill fires at
    # spec.timeout + _HARD_TIMEOUT_SLACK.
    cmd.extend(["--print-timeout", f"{int(spec.timeout)}s"])

    # Optional profile field: keep the CLI's own log next to logs/<slug>/.
    log_dir = str(harness_cfg.get("log_dir") or "").strip()
    if log_dir:
        # The conversation id is not known before the run starts; name the
        # log with the run uuid (the stream's init/result events carry the
        # mapping back to the conversation).
        cmd.extend(["--log-file", os.path.join(log_dir, f"{uuid.uuid4().hex}.log")])

    # Session flags.  agy has no fork primitive: --conversation resumes in
    # place.  The runner's capability gate (no "session_fork" capability)
    # keeps fork_session off; this log + fallthrough is the guard.
    if spec.fork_session:
        log("[ANTIGRAVITY] fork_session ignored (no fork primitive) — resuming in place")
    if spec.resume:
        cmd.extend(["--conversation", spec.resume])

    return cmd


def _user_event(prompt: str) -> bytes:
    """The single stdin line that carries the prompt to the CLI (T2).

    ``{"event":"user","message":{"content": <prompt>}}`` + newline, UTF-8.
    ensure_ascii=False keeps the payload compact and lossless (the JSON
    encoder still escapes the structural quotes/newlines, so exactly one real
    newline — the record terminator — ends up on the wire).
    """
    return (
        json.dumps(
            {"event": "user", "message": {"content": prompt}},
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\n"
    )


# ---------- Stream parsing ----------


def _tool_label(step: dict) -> str:
    """Render a tool step_update as a compact progress label.

    run_command → ``$ <CommandLine>``; file tools → ``<verb> <path>``;
    anything else → the tool name.  ``tool_info.parameters`` is a free-form
    object (T3: keys ``CommandLine`` / ``Path``), read defensively.
    """
    tool_info = step.get("tool_info")
    if not isinstance(tool_info, dict):
        tool_info = {}
    name = str(step.get("tool_name") or tool_info.get("name") or "tool")
    params = tool_info.get("parameters")
    if not isinstance(params, dict):
        params = {}
    if name == "run_command":
        command = str(params.get("CommandLine") or params.get("command") or "")
        return f"$ {command}".strip()
    verb = _FILE_TOOL_VERBS.get(name)
    if verb:
        path = str(params.get("Path") or params.get("path") or "")
        return f"{verb} {path}".strip()
    return name


def _fire_delta_progress(acc: _AgyAccumulator, callback, index: int, delta: str) -> None:
    """Fire throttled progress for an agent_response text delta.

    Progress lines are fire-and-forget; bounding output matters.  Fires on the
    first delta for a step index and again once the accumulated text has grown
    by ~80 chars since the last fire (mirrors pi/codex).
    """
    if not callback or not delta:
        return
    streamed = acc._delta_streamed.get(index, 0) + len(delta)
    acc._delta_streamed[index] = streamed
    fired_at = acc._delta_fired_at.get(index, -1)
    if fired_at < 0 or streamed - fired_at >= 80:
        acc._delta_fired_at[index] = streamed
        callback(delta[:120])


def _parse_line(line: str, acc: _AgyAccumulator, callback) -> None:
    """Parse a single JSON stream line and update the accumulator in-place.

    Dispatches on the top-level ``event`` key (``init`` / ``step_update`` /
    ``result``).  Unknown events are debug-logged and skipped: the protocol is
    designed to be forward-compatible.
    """
    line = line.strip()
    if not line:
        return
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        # Non-JSON line (stderr leak, progress banner) — skip at debug level.
        _dbg.debug(f"[ANTIGRAVITY] non-JSON stream line skipped: {line[:120]}")
        return
    if not isinstance(event, dict):
        return

    etype = event.get("event", "")

    if etype == "init":
        cid = event.get("conversation_id")
        if cid:
            acc.conversation_id = str(cid)
        init = event.get("init")
        if not isinstance(init, dict):
            init = {}
        acc.init_cwd = init.get("cwd")
        tools = init.get("tools")
        if isinstance(tools, list):
            acc.init_tools = tools
        # init.tools keeps the *registry* names (write_to_file/view_file),
        # which differ from the step-level names (write_file/read_file) — T3
        # quirk.  Phase 4/5 assert deny-rules/classification off the STREAM
        # names, but the registry list stays here for reference.
        log(
            f"[ANTIGRAVITY] init model={init.get('model')} "
            f"cwd={init.get('cwd')} permission_mode={init.get('permission_mode')} "
            f"tools={len(tools) if isinstance(tools, list) else 0}"
        )

    elif etype == "step_update":
        step = event.get("step_update")
        if not isinstance(step, dict):
            return
        step_type = step.get("step_type")
        state = step.get("state")

        # Subagent steps (T14): one progress line per subagent entry; the
        # subagent's own steps do not stream into the parent.
        subagent_info = step.get("subagent_info")
        if isinstance(subagent_info, dict):
            if state == "ACTIVE" and callback:
                for sub in subagent_info.get("subagents") or []:
                    if isinstance(sub, dict):
                        callback(
                            f"Subagent: {sub.get('role', '?')} ({sub.get('type_name', '?')})"
                        )
            return

        if step_type == "agent_response":
            delta = step.get("text_delta")
            if isinstance(delta, str) and delta:
                index = step.get("step_index")
                if not isinstance(index, int):
                    index = 0
                # ACTIVE partials and the DONE remainder are all fresh chunks
                # (fixture-confirmed: concatenation == result.response), so a
                # plain append cannot double-count.
                acc._text_by_index.setdefault(index, []).append(delta)
                _fire_delta_progress(acc, callback, index, delta)

        elif step_type == "tool":
            if state == "ACTIVE":
                if callback:
                    callback(f"Tool: {_tool_label(step)}")
            elif state in ("DONE", "ERROR"):
                # A refused/failed tool ends state=ERROR (permission_denied)
                # or DONE with tool_info.error; either way it is NOT a
                # run-level failure (README lesson #4) — the model sees the
                # error and normally recovers, so we log and collect but do
                # NOT set error_flag.
                tool_info = step.get("tool_info")
                err = tool_info.get("error") if isinstance(tool_info, dict) else None
                if isinstance(err, dict):
                    name = str(step.get("tool_name") or tool_info.get("name") or "tool")
                    etype_ = str(err.get("type", "error"))
                    message = str(err.get("message", ""))
                    log(f"[ANTIGRAVITY] tool {name} failed: {etype_}: {message}")
                    acc.tool_errors.append(f"{name}: {etype_}: {message}")

        elif step_type in ("user_input", "checkpoint"):
            # Usage bookkeeping only.
            pass
        # step_type "system_message" / "unknown" / "finish" / "subagent"
        # (without subagent_info) carry no Phase 1 progress value — ignored.

    elif etype == "result":
        result = event.get("result")
        if not isinstance(result, dict):
            result = {}
        acc.saw_result = True
        status = result.get("status")
        acc.status = str(status) if status is not None else None
        reason = result.get("finished_reason")
        acc.finished_reason = str(reason) if reason is not None else None
        response = result.get("response")
        acc.final_response = response if isinstance(response, str) else None
        error = result.get("error")
        acc.error = error if isinstance(error, str) and error else None
        structured = result.get("structured_output")
        # Advisory (T6): the schema does not guarantee truth — handlers
        # validate downstream per the trust-boundary rule.  Shape-check here.
        acc.structured_output = structured if isinstance(structured, dict) else None
        usage = result.get("usage")
        acc.usage = usage if isinstance(usage, dict) else None
        num_turns = result.get("num_turns")
        acc.num_turns = num_turns if isinstance(num_turns, int) else None
        # result's conversation_id wins over init's if they differ.
        cid = result.get("conversation_id")
        if cid:
            cid = str(cid)
            if acc.conversation_id is not None and cid != acc.conversation_id:
                log(
                    f"[ANTIGRAVITY] result conversation_id {cid[:8]} != init "
                    f"{acc.conversation_id[:8]} — using result id"
                )
            acc.conversation_id = cid

    else:
        # Forward compatibility: unknown events are debug-logged and skipped.
        _dbg.debug(f"[ANTIGRAVITY] unknown stream event '{etype}' skipped")


# ---------- Finalize ----------


def _is_config_error(error_text: str | None) -> bool:
    """True when the CLI's result.error names a config-level failure.

    Unknown model (T5) and authentication failure/timeout (§0.1.3) are not
    fixed by retrying: they mean the profile or host config is wrong.
    """
    if not error_text:
        return False
    lowered = error_text.lower()
    return any(pattern in lowered for pattern in _CONFIG_ERROR_PATTERNS)


def _finalize(
    acc: _AgyAccumulator,
    returncode: int | None,
    *,
    killed: bool = False,
    duration_seconds: float = 0.0,
    stderr_tail: bytes = b"",
) -> RunResult:
    """Turn the accumulator + exit code into a RunResult (Phase 1 table).

    ``killed`` marks our own hard-kill path (wall-clock backstop);
    ``returncode < 0`` is a death-by-signal (agy traps SIGTERM and writes a
    final result — T8 — but the process still dies on the signal).
    ``duration_seconds`` is the harness's own wall-clock (T8/T9:
    result.duration_seconds is CLI accounting, NEVER wall-clock).
    ``stderr_tail`` is the bounded stderr tail: auth/config deaths (spike
    §0.1.3) can exit non-zero *before* any result event, so it is scanned for
    the config-error patterns before defaulting to retryable "error".
    """
    status = acc.status
    config_text = ""
    if status is not None and status not in _TERMINAL_STATUSES:
        log(f"[WARN][ANTIGRAVITY] unexpected result.status {status!r}")

    # First match wins.
    if killed or (returncode is not None and returncode < 0):
        subtype = "killed"
    elif acc.error_flag:
        # Stream overflow.
        subtype = "error"
    elif not acc.saw_result:
        # The stream never produced a terminal result.  A non-zero exit whose
        # stderr carries auth/config text is the pre-result death of spike
        # §0.1.3 (e.g. "authentication required"): a config error, not a
        # retryable run failure — otherwise the retry policy burns every
        # attempt on a wrong profile.
        stderr_text = stderr_tail.decode("utf-8", errors="replace") if stderr_tail else ""
        if returncode != 0 and _is_config_error(stderr_text):
            subtype = "config_error"
            config_text = stderr_text.strip()
        else:
            subtype = "error"
    elif status == "SUCCESS" and returncode == 0:
        subtype = "success"
    elif status == "WAITING":
        log("[ANTIGRAVITY] run ended WAITING on input — ask_question is not supported headless")
        subtype = "error"
    elif status in ("CANCELED", "INTERRUPTED") or acc.finished_reason in _INTERRUPTED_REASONS:
        subtype = "killed"
    elif _is_config_error(acc.error):
        # Unknown model / auth failure: a config error, not a retryable run
        # failure.  RunResult.text carries the CLI's own message so the
        # FAILED comment tells the operator what to fix.
        subtype = "config_error"
    elif status == "SUCCESS":
        # status stamped SUCCESS but the exit code says otherwise — the exit
        # code wins over the status.
        subtype = "error"
    else:
        # ERROR / INVALID / RUNNING, or an unexpected status (maps to error).
        subtype = _STATUS_TO_SUBTYPE.get(status or "", "error")

    # Text assembly: result.response wins; fall back to the accumulated
    # text_delta chunks (re-assembled in step_index order) when the stream
    # never produced a response — e.g. a run killed before the result event.
    if acc.final_response:
        text = acc.final_response
        source = "result.response"
    else:
        parts: list[str] = []
        for index in sorted(acc._text_by_index):
            parts.append("".join(acc._text_by_index[index]))
        text = "".join(parts)
        source = "text_delta fallback"
    if subtype == "config_error":
        # The FAILED comment tells the operator what to fix: the CLI's own
        # message — from the result event when there was one, else stderr.
        text = acc.error or config_text or text

    if acc.usage:
        usage = acc.usage
        log(
            "[ANTIGRAVITY] usage "
            f"input={usage.get('input_tokens', 0)} "
            f"output={usage.get('output_tokens', 0)} "
            f"thinking={usage.get('thinking_tokens', 0)} "
            f"cache_read={usage.get('cache_read_tokens', 0)} "
            f"total={usage.get('total_tokens', 0)}"
        )
    if acc.tool_errors:
        log(f"[ANTIGRAVITY] {len(acc.tool_errors)} tool error(s) recovered during run")
    log(f"[ANTIGRAVITY] subtype={subtype} text from {source} ({len(text)} chars)")

    return RunResult(
        text=text,
        session_id=acc.conversation_id or None,
        subtype=subtype,
        ok=(subtype == "success"),
        # Tokens only — never estimate (README lesson #7).
        cost_usd=None,
        duration_seconds=duration_seconds,
        plan_file_path=None,
        # Phase 5 (MCP) sets the posted flags; Phase 1 has no comment tools.
        plan_posted=False,
        question_posted=False,
        structured_output=acc.structured_output,
    )


# ---------- Process-tree kill ----------


def _kill_tree(process) -> None:
    """Kill the agy process AND its descendants.

    ``agy`` spawns children (tool shells; and on Windows it may run under a
    ``cmd``/powershell shim), so ``process.kill()`` would leave orphans.
    POSIX: the subprocess is spawned with ``start_new_session=True`` so it is
    its own process-group leader and ``os.killpg`` reaches the whole tree
    (spike T8 confirmed the group kill leaves no orphans and the child `sleep`
    dies with it).  Windows: ``taskkill /T /F /PID`` walks the tree —
    implemented here but windows-pending until the Phase 6 parity pass.
    Best-effort: falls back to ``process.kill()`` when the group/pid is
    already gone; never raises (callers are on the timeout path).
    """
    pid = process.pid
    try:
        if os.name == "nt":
            # taskkill /T kills the pid and its child tree; /F forces.
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                check=False,
            )
        else:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        # Group already gone or killpg unavailable for this pid — fall back to
        # killing just the direct child.
        try:
            process.kill()
        except ProcessLookupError:
            pass


# ---------- AntigravityBackend ----------


class AntigravityBackend:
    """agy CLI backend implementing CodingBackend.

    Shells out to ``agy --input-format stream-json --output-format
    stream-json`` (plus ``--conversation`` for resume), writes the prompt as
    one stdin event line, streams the JSON event lines for live progress, and
    returns a RunResult.

    ``cost_usd`` is always ``None`` — the stream reports tokens, not money
    (spike lesson #7).  Duration is tracked via ``time.monotonic()`` (the
    CLI's own ``result.duration_seconds`` is accounting, not wall-clock —
    T8/T9).
    """

    # Phase 1 minimal honest set (Phase 1 plan §1.8):
    #   resume           — --conversation <id> (T7 confirmed headless resume)
    #   progress_stream  — step_update events drive progress_callback
    #   structured_output— --json-schema + result.structured_output (T6,
    #                      advisory: handlers must validate downstream)
    # NOT advertised: mode (Phase 4), mcp (Phase 5), session_fork (no
    # primitive), can_use_tool, plan_file.
    CAPABILITIES: set[str] = {"resume", "progress_stream", "structured_output"}
    RETRYABLE_SUBTYPES: set[str] = {"error", "killed"}

    @classmethod
    def capabilities(cls) -> set[str]:
        return cls.CAPABILITIES.copy()

    @classmethod
    def comment_tool_names(cls) -> dict[str, str]:
        """Claude defaults — agy has no MCP yet (Phase 5 overrides).

        Same precedent as Codex: a prompt placeholder still renders to a
        concrete tool name and the missing-tool case degrades to the text
        fallback.
        """
        return dict(CLAUDE_COMMENT_TOOL_NAMES)

    @classmethod
    def retryable_subtypes(cls) -> set[str]:
        return cls.RETRYABLE_SUBTYPES.copy()

    @classmethod
    def retryable_exceptions(cls) -> tuple:
        # agy has no SDK; its failure surface is the subprocess boundary, same
        # as Codex/pi.  asyncio.TimeoutError is the wall-clock backstop
        # (wait_for); OSError covers *transient* spawn-time failures from
        # create_subprocess_exec (PermissionError, resource exhaustion, etc.).
        # NOTE: a missing/misconfigured executable is NOT retried: both
        # _resolve_agy_executable and the spawn-time FileNotFoundError catch
        # raise RuntimeError, which is outside this tuple — the runner has no
        # FileNotFoundError carve-out, so letting the OSError subclass through
        # would burn every attempt on a broken config (pi.py precedent).
        return (asyncio.TimeoutError, OSError)

    def run(self, spec: RunSpec) -> Awaitable[RunResult]:
        """Execute the spec via the agy CLI.

        Returns an awaitable that resolves to a RunResult.  Uses
        ``asyncio.create_subprocess_exec`` so ``progress_callback`` fires with
        live updates while the CLI runs; the runner wraps this in
        ``asyncio.wait_for`` for timeouts on top of our own backstop.
        """
        harness_cfg = (spec.state or {}).get("_harness_cfg") or {}

        # Structured output: write the schema to a private temp file up front
        # (mkstemp — unique, user-controlled path, mode 600) so the file's
        # lifetime spans the whole run and is always reclaimed in the finally
        # below, including the kill path (the codex --output-last-message
        # pattern).  Never inline: too long for the Windows command line and
        # quoting hazards.
        schema_path: str | None = None
        if spec.output_format is not None:
            schema_fd, schema_path = tempfile.mkstemp(
                prefix="autoswe-agy-schema-", suffix=".json"
            )
            try:
                with os.fdopen(schema_fd, "w", encoding="utf-8") as f:
                    json.dump(spec.output_format.get("schema", spec.output_format), f)
            except OSError:
                try:
                    os.unlink(schema_path)
                except OSError:
                    pass
                schema_path = None
                raise

        async def _wrapped() -> RunResult:
            try:
                return await self._run_async(spec, harness_cfg, schema_path)
            finally:
                if schema_path:
                    try:
                        os.unlink(schema_path)
                    except OSError:
                        # Best-effort cleanup: a stray temp file is harmless
                        # and the OS reclaims it; never let cleanup break the
                        # run result.
                        pass

        return _wrapped()

    async def _run_async(
        self, spec: RunSpec, harness_cfg: dict, schema_path: str | None
    ) -> RunResult:
        """Run the agy CLI subprocess with streaming JSON.  Returns RunResult."""
        # The factory enforces model presence for reproducibility; this guard
        # protects direct AntigravityBackend().run(spec) calls.
        model = str(spec.model or "").strip()
        if not model:
            raise ValueError(
                "antigravity harness profile is missing required 'model'. "
                "Set it to a model known to the agy settings (custom models "
                "included; see docs/autoswe/spike-antigravity.md §0.1.2)."
            )

        # _resolve_agy_executable raises RuntimeError (non-retryable) when
        # nothing resolves — a config error that surfaces once.
        exe, prefix_argv = _resolve_agy_executable(
            str(spec.cli_path or harness_cfg.get("cli_path") or "").strip()
        )
        argv = [exe, *prefix_argv, *_build_argv(spec, harness_cfg, schema_path)]

        # Environment.  Precedence lowest -> highest, matching the other
        # backends (docs/autoswe/harnesses.md "Per-profile env"):
        #   os.environ < named fields < profile "env" < spec.env_overrides.
        # Named field: harness_cfg["gemini_api_key"] -> GEMINI_API_KEY.  (The
        # Vertex fields the plan lists are omitted: Phase 0 measured the BYOK
        # bridge path and never observed the CLI honoring them.)
        env = dict(os.environ)
        api_key = str(harness_cfg.get("gemini_api_key") or "").strip()
        if api_key:
            env["GEMINI_API_KEY"] = api_key
        env.update(harness_cfg.get("env") or {})
        if spec.env_overrides:
            env.update(spec.env_overrides)

        log(
            f"[ANTIGRAVITY] running model={model} "
            f"conversation={'NEW' if not spec.resume else spec.resume[:8]} "
            f"fork=ignored(no primitive) schema={'yes' if schema_path else 'no'}"
        )

        t0 = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                # There is no -C flag: the subprocess cwd is the ONLY
                # workspace lever (T12).
                cwd=spec.cwd,
                # Own the process group so _kill_tree can reach the whole
                # tree on the hard-kill path (T8).
                start_new_session=(os.name != "nt"),
                # StreamReader limit threads to BOTH stdout and stderr pipes
                # (pi #251 lesson): a single JSON event larger than the limit
                # makes readline() raise ValueError, which is not a retryable
                # exception and would crash the poller.  Raise it past
                # _MAX_STREAM_BYTES so any line the byte budget allows is
                # readable; the residual case is caught in read_stdout below.
                # T13: the CLI bounds tool output itself (largest observed
                # line ~10.6 KB), so this is headroom, not a tight bound.
                limit=_MAX_STREAM_BYTES + 1,
            )
        except FileNotFoundError as e:
            # Exec vanished between resolution and spawn, or the resolved
            # shim (cmd/powershell) is missing.  Convert to RuntimeError
            # (outside retryable_exceptions, pi.py precedent): FileNotFoundError
            # is an OSError subclass and the runner would otherwise retry a
            # broken config until the attempts run out.
            raise RuntimeError(
                f"agy executable '{exe}' could not be spawned "
                f"(cli_path={spec.cli_path or harness_cfg.get('cli_path') or 'agy on PATH'!r}): {e}"
            ) from e
        except (PermissionError, OSError) as e:
            # Surface spawn-time failures under the backend's retryable
            # exception set so the runner retries instead of crashing.
            raise OSError(f"failed to spawn agy executable: {e}") from e

        acc = _AgyAccumulator()

        async def _write_stdin() -> None:
            """Write the single stdin event line and close the pipe (T2).

            Runs CONCURRENTLY with the stdout/stderr readers in the gather
            below: while the CLI reads a large prompt off stdin it also emits
            startup output on stdout, so the readers must run at the same time
            to avoid a pipe-buffer deadlock.  Closing stdin is what ends the
            one-turn run.  If the CLI dies before reading, the write raises
            BrokenPipeError — harmless, the run is already failing on the
            exit-code path, so swallow it and let that path report.  (A fake
            subprocess in tests may have no stdin pipe; getattr no-ops those.)
            """
            stdin = getattr(process, "stdin", None)
            if stdin is None:
                return
            try:
                stdin.write(_user_event(spec.prompt))
                await stdin.drain()
                stdin.close()
            except (BrokenPipeError, ConnectionResetError, OSError) as e:
                log(f"[ANTIGRAVITY] could not write prompt to stdin: {e} — continuing (the run will report the exit code)")

        async def read_stderr() -> bytes:
            """Collect stderr, keeping a bounded TAIL (_STDERR_TAIL_BYTES).

            The tail is what a nonzero exit gets logged with.  Lines matching
            the headless soft-deny notice (spike P3b) are surfaced as
            [WARN][ANTIGRAVITY] soft-denied as soon as they are seen — they
            are the first sign of a permission misconfiguration.
            """
            if not process.stderr:
                return b""
            chunks: list[bytes] = []
            tail_bytes = 0
            total_bytes = 0
            while True:
                chunk = await process.stderr.read(64 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > _MAX_STREAM_BYTES:
                    log(f"[ANTIGRAVITY] stderr exceeded {_MAX_STREAM_BYTES} bytes — draining remainder")
                    while True:
                        leftover = await process.stderr.read(64 * 1024)
                        if not leftover:
                            break
                    break
                chunks.append(chunk)
                tail_bytes += len(chunk)
                while chunks and tail_bytes > _STDERR_TAIL_BYTES + 64 * 1024:
                    tail_bytes -= len(chunks.pop(0))
                for raw_line in chunk.split(b"\n"):
                    lowered = raw_line.lower()
                    if any(marker.encode() in lowered for marker in _SOFT_DENY_MARKERS):
                        log(f"[WARN][ANTIGRAVITY] soft-denied: {raw_line.decode('utf-8', errors='replace').strip()[:200]}")
            return b"".join(chunks)[-(_STDERR_TAIL_BYTES):]

        async def read_stdout_jsonl() -> None:
            """Read stdout line-by-line, parse JSON events, fire callbacks.

            Bounded by _MAX_STREAM_BYTES; on a single line over the
            StreamReader limit, readline() raises ValueError: flag the run
            (error_flag) and DRAIN the pipe (discarding data) so the child
            does not block — the run surfaces as subtype='error', never a
            poller crash (pi #251 path).
            """
            if process.stdout:
                total_bytes = 0
                while True:
                    try:
                        raw = await process.stdout.readline()
                    except ValueError:
                        log(f"[ANTIGRAVITY] single JSON line exceeded the StreamReader limit ({_MAX_STREAM_BYTES + 1} bytes) — truncating stream")
                        acc.error_flag = True
                        # read() not readline(): a pathological child may
                        # write non-newline data, which would block readline.
                        while True:
                            leftover = await process.stdout.read(64 * 1024)
                            if not leftover:
                                break
                        break
                    if not raw:
                        break
                    total_bytes += len(raw)
                    if total_bytes > _MAX_STREAM_BYTES:
                        log(f"[ANTIGRAVITY] stdout exceeded {_MAX_STREAM_BYTES} bytes — truncating stream")
                        acc.error_flag = True
                        while True:
                            leftover = await process.stdout.read(64 * 1024)
                            if not leftover:
                                break
                        break
                    text = raw.decode("utf-8", errors="replace")
                    _parse_line(text, acc=acc, callback=spec.progress_callback)

        # gather runs the stdin writer and both stream readers together, plus
        # process.wait() so the returncode is reaped when the streams close.
        stderr_tail = b""
        killed = False
        try:
            _gather_results = await asyncio.wait_for(
                asyncio.gather(
                    _write_stdin(),
                    read_stdout_jsonl(),
                    read_stderr(),
                    process.wait(),
                    return_exceptions=False,
                ),
                timeout=spec.timeout + _HARD_TIMEOUT_SLACK,
            )
            # Index 2 is read_stderr()'s tail (the others return None/returncode).
            stderr_tail = _gather_results[2] if len(_gather_results) > 2 else b""
        except asyncio.TimeoutError:
            # The CLI ignored its own --print-timeout and is wedged past the
            # backstop.  Kill the whole tree (a plain process.kill() would
            # orphan agy's children / only hit the cmd shim on Windows).
            killed = True
            _kill_tree(process)
            log(
                f"[ANTIGRAVITY] hard timeout after {spec.timeout + _HARD_TIMEOUT_SLACK}s "
                "— killed process tree (the CLI's own --print-timeout did not fire)"
            )

        # Reap the process so `returncode` is set.  On the timeout path the
        # tree-kill just finished it; on the happy path wait() already
        # completed inside the gather and this is a no-op (and on the Windows
        # proactor loop an explicit wait() here is what makes returncode
        # reliable — same lesson as pi).
        returncode = await process.wait()
        duration = time.monotonic() - t0

        if returncode != 0:
            log(f"[ANTIGRAVITY] exit={returncode}")
            # The NDJSON stream carries no diagnostic for startup crashes —
            # the real error text is on stderr.  Log the tail (redact the
            # profile's API key if set).
            stderr_text = stderr_tail.decode("utf-8", errors="replace").strip()
            if stderr_text:
                api_key = str(harness_cfg.get("gemini_api_key") or "")
                if api_key:
                    stderr_text = stderr_text.replace(api_key, "***")
                log(f"[ANTIGRAVITY] stderr (exit={returncode}): {stderr_text[:2000]}")
        if returncode != 0 and not acc.saw_result:
            log(f"[ANTIGRAVITY] stream did not reach result (exit={returncode})")

        # cwd sanity check (T12): the subprocess cwd is the only workspace
        # lever; warn loudly when the CLI disagrees about where it worked.
        if acc.init_cwd:
            try:
                if os.path.realpath(acc.init_cwd) != os.path.realpath(spec.cwd):
                    log(
                        f"[WARN][ANTIGRAVITY] init.cwd {acc.init_cwd!r} does not "
                        f"resolve to spec.cwd {spec.cwd!r} — the run worked in an "
                        f"unexpected directory"
                    )
            except OSError:
                pass

        return _finalize(
            acc, returncode, killed=killed, duration_seconds=duration, stderr_tail=stderr_tail
        )
