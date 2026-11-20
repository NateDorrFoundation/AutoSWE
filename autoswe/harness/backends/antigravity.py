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

**Capabilities (Phase 4 + 5):** ``resume``, ``progress_stream``,
``structured_output``, ``mode``, ``mcp``.  With ``mcp`` advertised the agent
posts plans/questions/progress through the shared ``autoswe_comment`` server
and ``planner.py``'s ``has_mcp`` branch turns on for this backend (no more
``<AUTOSWE_PLAN>`` scraping on MCP runs).  With ``mode``
advertised, ``has_read_only_enforcement`` is True and the planner/reviewer
loud-degrade warnings (issue #166) stop for this backend;
``ensure_worktree_unchanged`` still runs as the unconditional backstop.
There is **no fork primitive**: ``agy`` resumes a conversation in place
(``--conversation``), so ``fork_session`` is logged and treated as resume
(the runner's capability gate keeps it from happening; the log line is a
guard).

**MCP comment posting (Phase 5, spike §0.4 M2–M5).** The ``autoswe_comment``
server is registered in the agy **global** ``~/.gemini/config/mcp_config.json``
(entry-scoped merge, ``_merge_antigravity_mcp_config`` — foreign servers
preserved, never a worktree ``.agents/mcp_config.json``, which M4 proved can
never shadow the global entry anyway).  The file carries **stable values only**
(python path, repo root): per-task ``AUTOSWE_*`` env rides the ``agy``
subprocess env, which M3 proved the MCP child inherits (same stable-file
design as ``pi.py:_write_pi_mcp_json``).  The entry's tools are **eager**, so
the agent's calls surface as first-class tool steps named
``mcp_autoswe_comment_<tool>`` (M2-confirmed spelling); the lazy
``call_mcp_tool`` proxy shape is parsed as a fallback.  Classification happens
on the ``ACTIVE`` step and is **un-set on a ``DONE``/``ERROR`` step carrying
``tool_info.error``** — stricter than pi (which classifies on start): a failed
post must not count as posted, or the planner would skip its text fallback.

**Read-only enforcement (Phase 4, spike §0.3):** ``--mode plan`` is
**advisory only** (P1: a plan-mode run wrote files anyway) — it is never a
security boundary and is passed only as intent signalling.  The real lever
is the live binary's per-run ``--settings <path>`` flag (P3 lever (c),
"never saved"): each run stages a throwaway settings JSON in a private temp
dir carrying the §4.3 ``permissions`` block, and ``--settings`` points at
it.  Deny rules use **directory-prefix targets with a trailing slash** —
glob forms silently do NOT match (P2).  The host-global
``~/.gemini/antigravity-cli/settings.json`` is **never** mutated (it is
shared: writing per-run rules there would race a concurrent ``/fix`` under
``MAX_CONCURRENT > 1`` and leak a lockdown on crash).  A ``permission_mode``
self-check after ``init`` (see ``_expected_permission_mode``) fails a
read-only run whose posture does not match — a plan run that silently has
write access is worse than a failed plan run (§4.4).

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
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Awaitable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from autoswe.core.logging_utils import get_debug_logger, log
from autoswe.harness.backends.base import RunResult, RunSpec
from autoswe.harness.mcp_config import (
    antigravity_mcp_config_path,
    autoswe_repo_root,
    build_antigravity_mcp_config,
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


# ---------- MCP comment tools (Phase 5, spike §0.4 M2) ----------

# The autoswe_comment server's three tools as the agy CLI names them when the
# config entry registers them eagerly (``"eager": true``): ``mcp_<server>_<tool>``
# with SINGLE underscores (docs/antigravity/mcp.md "Eager loading").  Phase 0
# M2 confirmed the spelling live: exactly ``mcp_autoswe_comment_post_plan``.
# This dict is the single source of truth (CodingBackend.comment_tool_names
# contract): the prompt placeholders render from it, the stream classifier
# matches on it, and the config entry's eager list keys the same three tools.
ANTIGRAVITY_COMMENT_TOOL_NAMES: dict[str, str] = {
    "post_plan": "mcp_autoswe_comment_post_plan",
    "post_question": "mcp_autoswe_comment_post_question",
    "update_progress": "mcp_autoswe_comment_update_progress",
}

# Logical role per tool (mirrors pi's _MCP_COMMENT_TOOL_KINDS): post_plan /
# post_question set the RunResult already-posted flags; update_progress feeds
# the live progress callback.
_MCP_COMMENT_TOOL_KINDS: tuple[tuple[str, str], ...] = (
    ("post_plan", "plan"),
    ("post_question", "question"),
    ("update_progress", "progress"),
)

# eager tool name -> role, for the classifier's direct-shape branch.
_ROLE_BY_EAGER_TOOL: dict[str, str] = {
    ANTIGRAVITY_COMMENT_TOOL_NAMES[bare]: kind for bare, kind in _MCP_COMMENT_TOOL_KINDS
}

# The lazy proxy built-in (docs/antigravity/mcp.md "lazy vs eager" step 3):
# an agent that reads the tool schema from disk invokes every lazy MCP tool
# through this one, with parameters {ServerName, ToolName, Arguments}.  Eager
# registration is the primary path; this is the built-in fallback if a future
# CLI keeps the tools lazy despite the config.
_LAZY_PROXY_TOOL_NAME = "call_mcp_tool"


def _coerce_args(value) -> dict:
    """Coerce a lazy-proxy ``Arguments`` value into a dict (pi's twin).

    ``Arguments`` may arrive as an object or as a JSON string; returns ``{}``
    for anything that is not (or does not parse into) a dict.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _classify_mcp_comment_call(step: dict) -> tuple[str | None, str]:
    """Classify a ``tool`` step_update as an autoswe_comment call.

    Returns ``(role, body)`` where *role* is ``"plan"`` / ``"question"`` /
    ``"progress"`` / ``None`` and *body* is the tool's ``body`` argument
    (``""`` when absent).  Two shapes reach here (§5.3):

    - **eager / direct** — ``tool_name`` is one of the
      ``ANTIGRAVITY_COMMENT_TOOL_NAMES`` values; the body rides in
      ``tool_info.parameters.body``.
    - **lazy proxy** — ``tool_name == "call_mcp_tool"`` with parameters
      ``{ServerName: "autoswe_comment", ToolName: <tool>, Arguments: {body}}``
      (``Arguments`` may be a JSON string, so ``_coerce_args`` it).
    """
    tool_name = str(step.get("tool_name") or "")
    tool_info = step.get("tool_info")
    params = tool_info.get("parameters") if isinstance(tool_info, dict) else None
    if not isinstance(params, dict):
        params = {}

    if tool_name in _ROLE_BY_EAGER_TOOL:
        role = _ROLE_BY_EAGER_TOOL[tool_name]
        body = params.get("body")
    elif tool_name == _LAZY_PROXY_TOOL_NAME and str(params.get("ServerName") or "") == "autoswe_comment":
        target = str(params.get("ToolName") or "")
        role = next(
            (kind for bare, kind in _MCP_COMMENT_TOOL_KINDS
             if target == bare or target.endswith("_" + bare)),
            None,
        )
        body = _coerce_args(params.get("Arguments")).get("body")
    else:
        return None, ""

    if not isinstance(body, str):
        body = ""
    return role, body


def _mcp_comment_active(spec: RunSpec) -> bool:
    """True when the run's ``spec.mcp_servers`` names the autoswe_comment server.

    The Phase 5 trigger, mirroring ``pi.py:_mcp_comment_active``: it both
    merges the stable entry into the agy global config and routes the entry's
    per-task ``env`` block into the subprocess env (never the file).
    """
    servers = spec.mcp_servers or {}
    cfg = servers.get("autoswe_comment") if isinstance(servers, dict) else None
    return isinstance(cfg, dict)


# The only keys autoSWE ever writes into the global mcpServers map.  An owned
# key present on disk but absent from the current run's owned set is a stale
# autoSWE entry — e.g. an ``autoswe_inline_comment`` left behind by a GitHub
# fix run now facing a review/Azure run with no inline spec — and is pruned,
# so the agent never sees a registered server that can only fail server-side.
# Foreign keys are never in this tuple and are therefore never touched.
_OWNED_MCP_KEYS = ("autoswe_comment", "autoswe_inline_comment")


if sys.platform == "win32":  # pragma: no cover - exercised on Windows hosts
    import msvcrt

    def _lock_file_exclusive(f) -> None:
        # LK_LOCK blocks in ~1/8 s steps for up to 10 s, then raises OSError —
        # bounded on purpose so a wedged holder can never stall a run forever.
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock_file(f) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_file_exclusive(f) -> None:
        # Advisory to any other flock() user — every autoSWE writer of this
        # file takes the same lock, which is the whole race class we own.
        fcntl.flock(f, fcntl.LOCK_EX)

    def _unlock_file(f) -> None:
        fcntl.flock(f, fcntl.LOCK_UN)


def _unlock_file_safely(f) -> None:
    # Best-effort, and closing the fd right after releases the lock anyway.
    try:
        _unlock_file(f)
    except OSError:
        pass


@contextmanager
def _advisory_mcp_lock(config_path: Path):
    """Hold an advisory exclusive lock on the sibling ``<config>.lock``.

    Best-effort: a failed mkdir/open logs and the body runs unlocked; a
    failed lock (wedged holder past the bounded Windows wait) likewise logs
    and proceeds — the merge must never fail on lock problems.  ``yielded``
    flips immediately before the first yield, so an OSError raised *by the
    body* re-raises instead of falling into the acquisition-failure arm —
    the generator yields exactly once on every path.
    """
    lock_path = config_path.with_name(config_path.name + ".lock")
    yielded = False
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+b") as f:
            try:
                _lock_file_exclusive(f)
            except OSError as e:
                log(f"[WARN][ANTIGRAVITY] could not lock {lock_path} ({e}); merging unlocked")
            yielded = True
            try:
                yield
            finally:
                _unlock_file_safely(f)
    except OSError as e:
        if yielded:
            raise
        log(f"[WARN][ANTIGRAVITY] could not lock {lock_path} ({e}); merging unlocked")
        yield


def _merge_antigravity_mcp_config(owned_entries: dict[str, dict]) -> None:
    """Entry-scoped merge of autoSWE's servers into the agy global mcp_config.

    Only called when a run names the comment server (``_mcp_comment_active``).
    autoSWE owns **only** the keys in *owned_entries* (``autoswe_comment`` and
    optionally ``autoswe_inline_comment``) — every other server and top-level
    key an operator put in the file is preserved, exactly like
    ``pi.py:_write_pi_mcp_json``.  Owned keys present on disk but absent from
    *owned_entries* are stale and pruned (see ``_OWNED_MCP_KEYS``).  The file
    carries stable values only (per-task env rides the subprocess env, M3),
    so the serialized bytes are equal for every task: **compare before
    writing** and skip the write when the file already matches (the
    host-stable file is written once).

    The whole read-modify-write (read, merge, prune, write) is serialised
    under an advisory exclusive lock on a sibling ``<file>.lock``.  The prune
    makes the owned-entry sets differ between concurrent runs (a GitHub fix
    run owns ``autoswe_inline_comment``; a concurrent plan/Azure run prunes
    it), so last-writer-wins is NOT benign — without the lock a run's agy
    child could load a config that another run already pruned a live entry
    out of.  Locking is best-effort like everything else here: any OSError
    (uncreatable lock file, wedged holder past the bounded wait) logs and
    proceeds unlocked rather than failing the run.
    """
    path = antigravity_mcp_config_path()
    with _advisory_mcp_lock(path):
        _merge_antigravity_mcp_config_locked(owned_entries, path)


def _merge_antigravity_mcp_config_locked(
    owned_entries: dict[str, dict], path: Path
) -> None:
    """Read-merge-prune-write core, called under ``_lock_file_exclusive``."""
    existing: dict = {}
    raw: str | None = None
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        pass
    except OSError as e:
        log(f"[WARN][ANTIGRAVITY] could not read existing {path} ({e}); rewriting")
    if raw is not None:
        try:
            existing = json.loads(raw)
        except json.JSONDecodeError as e:
            # A corrupt file would otherwise keep the entry from ever landing;
            # the autoswe entries are authoritative, so start fresh.
            log(f"[WARN][ANTIGRAVITY] {path} is not valid JSON ({e}); rewriting")
            existing = {}
        if not isinstance(existing, dict):
            existing = {}

    servers = existing.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers.update(owned_entries)
    for key in _OWNED_MCP_KEYS:
        if key not in owned_entries:
            servers.pop(key, None)
    existing["mcpServers"] = servers

    payload = json.dumps(existing, indent=2) + "\n"
    if raw is not None and payload == raw:
        # Idempotent: stable values, stable bytes — skip the write.
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload)
        log(f"[ANTIGRAVITY] wrote autoswe_comment config to {path}")
    except OSError as e:
        log(f"[WARN][ANTIGRAVITY] could not write {path}: {e} — MCP tools may be unavailable (text fallback)")


# ---------- Read-only mode table (Phase 4, spike §0.3 / plan §4.3) ----------

# RunSpec.mode values that must be ENFORCED read-only at the CLI level.
# ``plan`` comes from the planner, ``read_only`` from the reviewer.
_READ_ONLY_MODES = frozenset({"plan", "read_only"})

# LIVE CHECK (dedicated box, 2026-11-20 — issue #321 exit criterion), two
# findings recorded here so future passes don't re-litigate them:
#   1. ``--mode plan`` + staged ``--settings`` attests init.permission_mode
#      == "request-review" on the real binary — the §4.4 self-check's
#      expected spelling is confirmed, so fail-closed will not misfire.
#   2. stderr warns "--mode plan has no effect while slash command expansion
#      is disabled", and we ALWAYS pass --disable-slash-commands (T11
#      mandatory).  ``--mode plan`` is therefore provably inert belt on the
#      agy profile — the staged deny rules are the ONLY enforcement layer
#      (a second, mechanism-level proof of P1's advisory finding).

# Mutating stream tool names (T3: ``permissions.deny`` rules are keyed off
# the STREAM names — write_file/read_file — not the init.tools registry
# names write_to_file/view_file).  Every mutating tool is denied against the
# worktree on read-only phases: a single allow-list miss is a write.
_MUTATING_TOOL_RULES = (
    "write_file",
    "replace_file_content",
    "multi_replace_file_content",
    "sed_file",
    "notebook_edit",
)

# T10: headless print mode can never answer an interactive prompt (the
# question step completes with no selection), and the WAITING→error mapping
# only catches the case the CLI bothers to stamp.  These three are denied in
# EVERY mode (§4.3 "MUST be in the deny set on every profile") — Phase 7's
# AskUserQuestion flow will replace the mechanism, not this rule.
_NEVER_ALLOWED_TOOL_RULES = (
    "ask_question",
    "ask_permission",
    "ask_custom_permission",
)

# Rule SHAPE → spike evidence (a wrong shape fails SILENTLY — that is P2's
# whole lesson, so each spelling below is pinned to the §0.3 row that proved
# it, not to taste):
#   write_file(<dir>/) …   directory-prefix target, TRAILING SLASH REQUIRED
#                          → §0.3 P2 (glob ``<repo>/*`` did not match; slash-
#                          less prefix matched only the bare path)
#   command(*)             wildcard target → §0.3 P2 (``command(ls)`` denied)
#   ask_question (et al.)  BARE name, no target → §0.3 T10 (deniable via
#                          permissions.deny; headless never pauses anyway)
#   mcp(server/tool)       wildcard target → §0.3 P5 (MCP allow coexists with
#                          the write/command deny profile in one run)
#   search(<dir>/) and     BOTH spellings allowed: the grep stream spelling is
#   grep_search(<dir>/)    the one rule shape no fixture row pins (see
#                          _permissions_for_mode); unmatched allow = no-op, so
#                          the dual spelling is free — live pass decides which
#                          one is real (harnesses.md Phase 6 live-pass row)

# ``spec.extra_tools`` / ``spec.disallowed_tools_override`` carry Claude-style
# names today.  Map the ones with a clean agy rule equivalent (stream names);
# everything else is log-and-ignore'd, same table convention as pi.py's
# _MODE_TOOLS.  MCP-spellings (``mcp__server__tool``) are converted to
# ``mcp(server/tool)`` rules directly.
_CLAUDE_TO_AGY_TOOL_RULES = {
    "AskUserQuestion": "ask_question",
    "Bash": "command(*)",
    "Edit": "replace_file_content",
    "MultiEdit": "multi_replace_file_content",
    "NotebookEdit": "notebook_edit",
    "Write": "write_file",
}

# Review-prompt decision (§4.3 asks for it, recorded here): the review
# template receives the diff PRE-RENDERED — the reviewer handler runs
# ``git diff`` itself and injects the result into {{DIFF}}/{{DIFF_STAT}}
# (autoswe/harness/prompts.py) — so the agy session never needs git
# diff/log/show.  The read-only ``command(*)`` deny therefore STAYS at full
# breadth; no narrower ``command(regex:git (diff|log|show|status)( .*)?)``
# allowance is needed.


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
    # ``init`` event was parsed.  The §4.4 self-check distinguishes "the CLI
    # never got far enough to attest anything" (pre-init death — nothing to
    # check) from "init parsed but carried no permission_mode" — a CLI that
    # ignored the staged --settings AND omitted the attestation, which for a
    # read-only phase must fail CLOSED, not pass (review round-2 MEDIUM).
    init_seen: bool = False
    # Permission posture the CLI reported (``request-review`` /
    # ``always-proceed``) — the Phase 4 §4.4 self-check compares it against
    # _expected_permission_mode().  None until the stream starts (and forever
    # for streams that die before ``init``, where the check cannot run; a
    # parsed init WITHOUT the field stays None too — init_seen tells the two
    # apart).
    init_permission_mode: str | None = None
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
    # MCP already-posted flags (Phase 5).  Set when an ACTIVE tool step
    # classifies (via _classify_mcp_comment_call) as post_plan / post_question;
    # these feed RunResult.plan_posted / question_posted so the planner's
    # has_mcp branch sees the comment already on the thread.
    plan_posted: bool = False
    question_posted: bool = False
    # The post_plan tool's ``body`` argument (the plan markdown), captured on
    # the ACTIVE step so the planner can finalize the sticky planning comment
    # in place after the run — the last call's body wins (issue #241).
    # None when post_plan was never called with a non-empty body.
    plan_posted_body: str | None = None
    # step_index -> role for IN-FLIGHT comment calls, so the DONE/ERROR step
    # can un-set the flag when the post failed (§5.3: stricter than pi, which
    # classifies on start and never looks back).  step_index is always present
    # in fixture-confirmed streams; a missing one is tracked under -1 (the
    # un-set then degrades to the conservative direction).
    _comment_steps: dict[int, str] = field(default_factory=dict)
    # Outstanding (ACTIVE, not yet resolved) post_plan / post_question call
    # counts.  The flags derive from ``count > 0`` so a failed second call
    # cannot wipe a successful first one's flag, and vice versa.
    _plan_calls: int = 0
    _question_calls: int = 0


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


def _build_argv(
    spec: RunSpec,
    harness_cfg: dict,
    schema_path: str | None,
    settings_args: list[str] | None = None,
) -> list[str]:
    """Build the agy flag argv (after the exe prefix) for one run.

    | Case | session flags |
    |---|---|
    | fresh | (none — the CLI creates a conversation) |
    | resume | ``--conversation <spec.resume>`` |
    | fork  | **not supported**: logged and treated as resume |

    ``schema_path`` is the caller-created temp file holding the JSON schema
    (only set when ``spec.output_format`` is present); the schema is never
    passed inline — it is too long for the Windows command line and has
    quoting hazards.  ``settings_args`` is the ``_stage_run_config`` seam's
    ``--settings`` fragment (the per-run permissions block, Phase 4).
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

    # Phase 4 posture split (§4.3): the skip flag is a read_write privilege —
    # read-only phases never get it (that is the whole point of "mode").
    # ``--sandbox`` is NOT emitted on read-only phases by default: spike P4
    # proved OS-level blocking on Linux but Windows is windows-pending, so the
    # profile ``sandbox`` field stays the only way to force it on (for all
    # phases, as before).
    if (spec.mode or "read_write") == "plan":
        # Advisory intent only — P1 proved --mode plan never blocks anything
        # headless; the staged permissions block is the actual enforcement.
        cmd.extend(["--mode", "plan"])
    if (spec.mode or "read_write") not in _READ_ONLY_MODES \
            and bool(harness_cfg.get("skip_permissions", True)):
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

    # Per-run staged settings (--settings <path>, Phase 4 §4.2 Option C).
    if settings_args:
        cmd.extend(settings_args)

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


# ---------- Per-run settings staging (Phase 4 §4.2, Option C) ----------


def _rule_prefix(cwd: str) -> str:
    """Canonical absolute prefix for permission-rule targets.

    P2's hard lesson: deny targets are **directory prefixes with a trailing
    slash** — a glob (``<repo>/*``) silently does NOT match, and a prefix
    without the slash matches only the bare path.  Backslashes are normalized
    to forward slashes so one rule shape works on both platforms (rule
    semantics verified on Linux, spike §0.3; Windows path rules ride the
    Phase 6 parity pass).

    ``realpath``, not ``abspath``: the CLI resolves writes against canonical
    paths, so a symlink component anywhere in ``spec.cwd`` would leave
    ``abspath``-built deny targets unmatched — enforcement silently weakened
    while the §4.4 self-check still sees ``request-review``.  Canonicalizing
    here matches the init.cwd sanity check below (same module), which already
    compares realpaths.

    Raises on a falsy cwd: falling back to the empty path would silently
    anchor every deny rule to the *poller's* cwd — a mis-plumbed spec must
    fail loudly instead of enforcing nothing.
    """
    if not cwd:
        raise ValueError(
            "antigravity permission rules require an explicit spec.cwd; "
            "refusing to target deny rules at the poller's working directory"
        )
    path = os.path.realpath(str(cwd)).replace("\\", "/").rstrip("/")
    return path + "/"


def _permissions_for_mode(mode: str, cwd_prefix: str) -> dict:
    """The §4.3 mode → ``permissions`` block (the module's mode table).

    | mode | deny | allow |
    |---|---|---|
    | ``plan`` / ``read_only`` | every mutating tool against the worktree,
    |   ``write_file(<cwd>/.git/)``, ``command(*)``, the three ``ask_*`` |
    |   reads against the worktree, ``mcp(autoswe_comment/*)`` (+ review's
    |   ``mcp(autoswe_inline_comment/*)``) |
    | ``read_write`` / unset | ``write_file(<cwd>/.git/)`` belt-and-braces,
    |   the three ``ask_*`` | (none — default posture) |

    Deny > Ask > Allow is the CLI's precedence, so the allow list only
    rescues reads; it can never reopen a denied tool (P5 confirmed the
    read-only deny profile and the MCP allow coexist in one run).
    """
    read_only = mode in _READ_ONLY_MODES
    deny: list[str] = []
    if read_only:
        deny.extend(f"{tool}({cwd_prefix})" for tool in _MUTATING_TOOL_RULES)
        # Explicit .git entry mirrors the §4.3 table: prefix semantics make it
        # redundant with <cwd>/, but it stays a rule in its own right so a
        # future narrowing of the worktree prefix can never reopen .git.
        deny.append(f"write_file({cwd_prefix}.git/)")
        deny.append("command(*)")
    else:
        deny.append(f"write_file({cwd_prefix}.git/)")
    deny.extend(_NEVER_ALLOWED_TOOL_RULES)

    allow: list[str] = []
    if read_only:
        allow = [
            f"read_file({cwd_prefix})",
            f"view_file({cwd_prefix})",
            # BOTH grep spellings, deliberately: no committed fixture stream
            # pins the search tool's stream name (fixtures carry stream names
            # only for run_command/write_file/read_file; the init.tools
            # registry spells it ``grep_search``, the rules want stream
            # names — unverified which one the matcher recognizes).  An
            # unmatched ALLOW entry is a no-op under deny>ask>allow, so
            # carrying both costs nothing and the planner cannot silently
            # lose grep.  Which spelling is live is a Phase 6 live-pass row
            # (harnesses.md); then drop the dead one.
            f"search({cwd_prefix})",
            f"grep_search({cwd_prefix})",
            f"list_dir({cwd_prefix})",
            "mcp(autoswe_comment/*)",
        ]
        if mode == "read_only":
            allow.append("mcp(autoswe_inline_comment/*)")
    return {"deny": deny, "allow": allow}


def _mcp_rule(name: str) -> str | None:
    """``mcp__server__tool`` (Claude spelling) → ``mcp(server/tool)`` agy rule.

    Returns None when *name* is not MCP-spelled (single/double-underscore
    boundary as in ``mcp__autoswe_comment__post_plan``).
    """
    parts = str(name).split("__")
    if len(parts) >= 3 and parts[0] == "mcp":
        return "mcp(" + "/".join(parts[1:]) + ")"
    return None


def _apply_tool_overrides(spec: RunSpec, deny: list[str], allow: list[str]) -> None:
    """Map RunSpec tool overrides (Claude names) onto agy rules (§4.3).

    Mapped names get the equivalent rule; everything else is logged and
    ignored, pi-style: inventing a rule for a tool the CLI does not have is
    worse than ignoring it.  File-mutating mappings grow a worktree-prefix
    target (spike P2: a bare ``write_file``-style rule with no target does
    not match); ``command`` already maps to the wildcard ``command(*)``, and
    ``ask_*`` stay bare exactly as the spike's deny set spells them.
    """
    for tool in spec.disallowed_tools_override or []:
        rule = _mcp_rule(tool) or _CLAUDE_TO_AGY_TOOL_RULES.get(str(tool))
        if rule is None:
            log(f"[ANTIGRAVITY] disallowed tool {tool!r} has no agy rule equivalent — ignored")
            continue
        if rule in _MUTATING_TOOL_RULES:
            rule = f"{rule}({_rule_prefix(spec.cwd)})"
        if rule not in deny:
            deny.append(rule)
    for tool in spec.extra_tools or []:
        rule = _mcp_rule(tool)
        if rule is None:
            # Mapped names are already governed by the mode table (reads are
            # allowed, mutations denied); unmapped names have no equivalent.
            reason = (
                "already governed by the mode table"
                if str(tool) in _CLAUDE_TO_AGY_TOOL_RULES else "no agy rule equivalent"
            )
            _dbg.debug(f"[ANTIGRAVITY] extra tool {tool!r} ignored ({reason})")
            continue
        if rule not in allow:
            allow.append(rule)


class AgyConfigError(RuntimeError):
    """Operator-authored agy profile config is malformed — fail loud.

    Deliberately outside ``retryable_exceptions()``: a bad
    ``base-settings.json`` does not fix itself on retry, and coercing it
    anyway (e.g. iterating a bare string ``"command(*)"`` character-by-
    character into one-char rules ``"c"``, ``"o"``, …) would stage wrong
    permissions instead of failing the run.
    """


def _resolve_config_home(harness_cfg: dict, model: str | None) -> os.PathLike:
    """Base-settings home for this profile (profile ``config_home`` field).

    Default ``<AUTOSWE_DIR>/agy-home/<sanitized model>`` — the profile NAME
    is not threaded into the resolved harness dict, so the model stands in as
    the stable per-profile key.  Only ``base-settings.json`` is ever read
    from here; per-run staged files land in a private temp dir.

    The directory is OPTIONAL by design: live-verified on the dedicated box,
    ``--settings`` LAYERS over the host-global ``~/.gemini/antigravity-cli/
    settings.json`` rather than replacing it (an isolated HOME whose global
    settings carried only the model-provider config resolved its custom model
    under a ``--settings`` file containing only a permissions block — matching
    the flag text "for this session only, never saved").  So an absent
    config_home loses nothing; the CLI still sees its host-global config.
    """
    from autoswe.core.config import AUTOSWE_DIR  # lazy: config pulls in the factory

    configured = str(harness_cfg.get("config_home") or "").strip()
    root = Path(configured) if configured else Path(AUTOSWE_DIR) / "agy-home"
    safe_model = re.sub(r"[^A-Za-z0-9._-]", "_", str(model or "default"))
    return root / safe_model


def _load_base_settings(harness_cfg: dict, model: str | None) -> dict:
    """``<config_home>/base-settings.json`` (``{}`` when absent/unreadable).

    Raises :class:`AgyConfigError` on a malformed ``permissions`` block
    (a bare string where ``deny``/``allow`` expect a list of rules): a
    degraded ``{}`` is right for an unreadable file — enforcement still
    comes from the mode block — but a wrong-SHAPE rule would merge
    char-by-char into one-char rules and silently mis-enforce.

    An OPTIONAL per-profile override layer, NOT a required rescue: ``--settings``
    is live-verified (dedicated-box experiment, isolated HOME + provider-only
    global settings) to LAYER over the host-global settings file, never
    replace it — so a bare ``{}`` base keeps every host-global key
    (useG1Credits, modelProvider/customModels, …) exactly as the unsandboxed
    CLI sees them.  Author this file only to OVERRIDE host config for one
    profile, or to park per-profile state (the Phase 5 MCP registration).
    Do NOT auto-seed it by copying the host-global file: under layering that
    buys nothing and would freeze a stale snapshot.
    """
    base_file = Path(_resolve_config_home(harness_cfg, model)) / "base-settings.json"
    try:
        raw = base_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        log(f"[WARN][ANTIGRAVITY] cannot read {base_file}: {e} — staging from {{}}")
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log(f"[WARN][ANTIGRAVITY] {base_file} is not valid JSON ({e}) — staging from {{}}")
        return {}
    if not isinstance(data, dict):
        return {}
    base_perm = data.get("permissions")
    if isinstance(base_perm, dict):
        for key in ("deny", "allow"):
            if isinstance(base_perm.get(key), str):
                raise AgyConfigError(
                    f"{base_file}: permissions.{key} must be a list of rule "
                    f"strings, not a bare string {base_perm[key]!r} (merging "
                    "a bare string would iterate it character-by-character "
                    "into one-char rules)"
                )
    return data


def _stage_run_config(spec: RunSpec, harness_cfg: dict) -> tuple[list[str], dict, object]:
    """Stage one run's ``--settings`` JSON; return ``(settings_args, env_patch, cleanup)``.

    The ONE seam §4.2 asks for: ``_run_async`` stays transport-agnostic and
    the tests patch/assert one function.  Shape deviation from the plan's
    ``(env_patch, cleanup)`` 2-tuple, documented deliberately: the Phase 0
    winner is **Option C** (the live binary's per-run ``--settings`` flag —
    spike §0.3 P3 lever (c)), which needs an ARGV channel, so the seam
    returns ``(settings_args, env_patch, cleanup)``.  ``env_patch`` is empty
    on Option C — P3(b) proved HOME-override isolation unnecessary (BYOK auth
    is env-only) — the slot keeps Option A's channel one seam deep.

    Mechanics: private temp dir (``mkdtemp`` — distinct per concurrent run,
    so two parallel plan/fix sessions never share a settings file,
    race-free under ``MAX_CONCURRENT > 1``) holding ``settings.json`` = the
    profile's OPTIONAL ``base-settings.json`` (or ``{}``) merged with the mode
    block (§4.3) + mapped tool overrides.  The CLI layers this file OVER the
    host-global settings (live-verified — see _load_base_settings), so a bare
    ``{}`` base loses no host config.  ``cleanup`` deletes the run dir; the
    caller runs it in a ``finally``, so success, failure, kill and cancel all
    reclaim it.  The host-global settings file is NEVER opened for writing.
    """
    mode = str(spec.mode or "read_write")
    permissions = _permissions_for_mode(mode, _rule_prefix(spec.cwd))
    _apply_tool_overrides(spec, permissions["deny"], permissions["allow"])
    # Load/validate the profile base BEFORE staging anything: a malformed
    # base raises AgyConfigError here, so no staged dir can leak (the
    # round-3 leak invariant: everything failure-capable either precedes
    # mkdtemp or lives under the block whose finally reclaims the dir).
    base = _load_base_settings(harness_cfg, spec.model)

    run_dir = tempfile.mkdtemp(prefix="autoswe-agy-run-")
    settings_path = os.path.join(run_dir, "settings.json")
    try:
        settings = dict(base)
        base_perm = base.get("permissions") if isinstance(base.get("permissions"), dict) else {}
        merged_perm = dict(base_perm)
        for key in ("deny", "allow"):
            merged: list[str] = []
            seen: set[str] = set()
            for rule in list(base_perm.get(key) or []) + list(permissions[key]):
                if isinstance(rule, str) and rule not in seen:
                    seen.add(rule)
                    merged.append(rule)
            merged_perm[key] = merged
        settings["permissions"] = merged_perm
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f)
    except OSError:
        # Staging failure with the mode block unwritten = a run that would
        # silently lose its enforcement.  Reclaim the dir and fail loudly.
        shutil.rmtree(run_dir, ignore_errors=True)
        raise

    def cleanup() -> None:
        shutil.rmtree(run_dir, ignore_errors=True)

    return ["--settings", settings_path], {}, cleanup


def _expected_permission_mode(mode: str | None, harness_cfg: dict) -> str | None:
    """The ``init.permission_mode`` value the run should report (Phase 4 §4.4).

    ``request-review`` for read-only phases, ``always-proceed`` for
    read_write with the skip flag (the dedicated-machine default).  read_write
    WITHOUT skip has no required posture (the CLI's interactive default is
    fine headless), so None means "nothing to check".
    """
    if (mode or "read_write") in _READ_ONLY_MODES:
        return "request-review"
    if bool(harness_cfg.get("skip_permissions", True)):
        return "always-proceed"
    return None


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
        acc.init_seen = True
        acc.init_cwd = init.get("cwd")
        permission_mode = init.get("permission_mode")
        acc.init_permission_mode = (
            str(permission_mode) if permission_mode is not None else None
        )
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
                # Phase 5: classify the call on the START step (the tool may
                # never reach a DONE the parser sees, e.g. a hard kill
                # mid-post — same optimistic-set rationale as pi).  A tracked
                # DONE/ERROR WITH tool_info.error un-sets it again below, so
                # an answered-but-failed post never suppresses the planner's
                # text fallback (§5.3).
                role, body = _classify_mcp_comment_call(step)
                if role is not None:
                    index = step.get("step_index")
                    if not isinstance(index, int):
                        index = -1
                    if role == "plan":
                        if acc.question_posted:
                            # Question-terminal precedence (issue #230): the
                            # run already asked the user, so a post_plan that
                            # follows must NOT flip the outcome back to a
                            # posted plan — same rule and log shape as pi.
                            # Deliberately NOT counted and NOT tracked: this
                            # call never entered _plan_calls, so a later
                            # error-DONE for it must not decrement the count
                            # and wipe an earlier successful plan's flag.
                            log("[ANTIGRAVITY] post_plan ignored — post_question already observed in this run (question-terminal)")
                        else:
                            acc._plan_calls += 1
                            acc.plan_posted = True
                            # Capture the body so the planner can finalize the
                            # sticky planning comment in place (#241); the
                            # last non-empty body wins.
                            if body:
                                acc.plan_posted_body = body
                            log(f"[ANTIGRAVITY] autoswe_comment post_plan (body {len(body)} chars)")
                            acc._comment_steps[index] = role
                    elif role == "question":
                        acc._question_calls += 1
                        acc.question_posted = True
                        log(f"[ANTIGRAVITY] autoswe_comment post_question (body {len(body)} chars)")
                        acc._comment_steps[index] = role
                    elif role == "progress":
                        # update_progress drives the live progress callback
                        # with its body so the operator sees the status line.
                        if callback and body:
                            callback(body[:200])
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
                # Phase 5 §5.3: a FAILED comment post must not count as
                # posted, or the planner's has_mcp branch would skip the text
                # fallback while nothing is on the thread.  Resolve the
                # in-flight call tracked from the ACTIVE step.
                index = step.get("step_index")
                if not isinstance(index, int):
                    index = -1
                role = acc._comment_steps.pop(index, None)
                if role is not None and err:
                    if role == "plan":
                        acc._plan_calls = max(0, acc._plan_calls - 1)
                        acc.plan_posted = acc._plan_calls > 0
                        if not acc.plan_posted:
                            acc.plan_posted_body = None
                        still = acc.plan_posted
                    elif role == "question":
                        acc._question_calls = max(0, acc._question_calls - 1)
                        acc.question_posted = acc._question_calls > 0
                        still = acc.question_posted
                    else:
                        still = False
                    # Count-based: only un-set when no successful call of this
                    # role remains; say so truthfully in the log.
                    log(f"[ANTIGRAVITY] autoswe_comment post_{role} FAILED — "
                        + ("posted flag kept (another call succeeded)" if still
                           else "un-set posted flag (planner text fallback stays available)"))
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
        # Phase 5 (MCP): set from the stream by _parse_line.  A killed or
        # overflowed stream never invents plan_posted — the flags only ever
        # reflect tool calls actually seen (and un-set if their post failed).
        plan_posted=acc.plan_posted,
        question_posted=acc.question_posted,
        plan_posted_body=acc.plan_posted_body,
        structured_output=acc.structured_output,
    )


# ---------- Process-tree kill ----------


# The same tree-kill gap still exists in pi.py (bare process.kill()); tracked
# in #327 — do not copy that pattern.
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

    # Phase 1 honest set + Phase 4's ``mode`` + Phase 5's ``mcp``:
    #   resume           — --conversation <id> (T7 confirmed headless resume)
    #   progress_stream  — step_update events drive progress_callback
    #   structured_output— --json-schema + result.structured_output (T6,
    #                      advisory: handlers must validate downstream)
    #   mode             — per-run --settings permissions block enforces
    #                      read-only on plan/read_only phases (§4.2/§4.3);
    #                      this is what flips has_read_only_enforcement to
    #                      True and stops the issue #166 loud-degrade.
    #   mcp              — eager autoswe_comment registration in the agy
    #                      global mcp_config.json (M2 tool naming, M3 env
    #                      inheritance); the stream classifier turns the
    #                      post_plan/post_question steps into the
    #                      plan_posted/question_posted flags, which is what
    #                      turns on planner.py's has_mcp branch (no more
    #                      <AUTOSWE_PLAN> scraping on MCP runs).
    # NOT advertised: session_fork (no primitive), can_use_tool, plan_file.
    CAPABILITIES: set[str] = {
        "resume", "progress_stream", "structured_output", "mode", "mcp",
    }
    RETRYABLE_SUBTYPES: set[str] = {"error", "killed"}

    @classmethod
    def capabilities(cls) -> set[str]:
        return cls.CAPABILITIES.copy()

    @classmethod
    def comment_tool_names(cls) -> dict[str, str]:
        """The agy eager spellings: ``mcp_autoswe_comment_<tool>``.

        Single underscores after the server name (agy naming, M2-confirmed
        exactly ``mcp_autoswe_comment_post_plan``) — unlike Claude Code's
        double-underscore ``mcp__autoswe_comment__post_plan``.  This dict is
        the single source of truth: the prompt placeholders render from it,
        and the stream classifier matches the same names, so the names the
        agent is granted, the names the parser recognizes, and the names the
        prompt references can never drift apart.  Fresh dict each call.
        """
        return dict(ANTIGRAVITY_COMMENT_TOOL_NAMES)

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

        # Phase 4: stage the per-run --settings permissions block (the ONE
        # seam, _stage_run_config).  The staged dir is reclaimed in the
        # _wrapped finally below alongside the schema temp file — covering
        # success, failure, kill and cancel — and nothing is ever written to
        # the host-global settings file.  Everything that can still fail
        # AFTER cleanup is bound (schema staging included) lives inside
        # _wrapped, so its finally owns every resource: a failure between
        # here and the spawn must not strand the staged dir (review MEDIUM).
        settings_args, env_patch, cleanup = _stage_run_config(spec, harness_cfg)

        async def _wrapped() -> RunResult:
            # Structured output: write the schema to a private temp file
            # (mkstemp — unique, user-controlled path, mode 600) so the
            # file's lifetime spans the whole run and is always reclaimed in
            # the finally below, including the kill path (the codex
            # --output-last-message pattern).  Never inline: too long for the
            # Windows command line and quoting hazards.
            schema_path: str | None = None
            try:
                if spec.output_format is not None:
                    schema_fd, schema_path = tempfile.mkstemp(
                        prefix="autoswe-agy-schema-", suffix=".json"
                    )
                    try:
                        with os.fdopen(schema_fd, "w", encoding="utf-8") as f:
                            json.dump(
                                spec.output_format.get("schema", spec.output_format), f
                            )
                    except BaseException:
                        # Any failure mode (OSError, non-serializable schema…)
                        # leaves the staged settings dir to the finally below;
                        # here just drop the half-written schema file.
                        try:
                            os.unlink(schema_path)
                        except OSError:
                            pass
                        schema_path = None
                        raise
                return await self._run_async(
                    spec, harness_cfg, schema_path, settings_args, env_patch
                )
            finally:
                cleanup()
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
        self,
        spec: RunSpec,
        harness_cfg: dict,
        schema_path: str | None,
        settings_args: list[str] | None = None,
        env_patch: dict | None = None,
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
        argv = [
            exe, *prefix_argv,
            *_build_argv(spec, harness_cfg, schema_path, settings_args),
        ]

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
        # Backend-managed patch from _stage_run_config (empty on Option C —
        # the per-run lever is --settings, not HOME isolation).  Applied last
        # on purpose: a profile env dict must not be able to undo the
        # backend's own isolation.  Never written back to os.environ.
        if env_patch:
            env.update(env_patch)

        # Phase 5 MCP wiring — same design as pi, same proof behind it:
        # stable values in the config file, per-task values in THIS process's
        # env.  M3 proved the MCP child inherits agy's process env, so the
        # server entry's per-task ``env`` block (comment id, owner/repo, PAT,
        # provider, …) is merged into the subprocess env here and NEVER into
        # mcp_config.json — the file carries stable bytes and is written at
        # most once per host.  The merge itself is entry-scoped (foreign
        # servers preserved) and best-effort (a write failure logs and the
        # run degrades to the text fallback).
        mcp_servers = spec.mcp_servers or {}
        if _mcp_comment_active(spec):
            comment_cfg = mcp_servers.get("autoswe_comment")
            inline_cfg = mcp_servers.get("autoswe_inline_comment") \
                if isinstance(mcp_servers, dict) else None
            # Seeded from the spec-mandated builder so the backend and
            # ``build_antigravity_mcp_config`` can never drift (the builder
            # owns the launch triple, timeoutSeconds and the eager tools;
            # cwd is its autoSWE-checkout-root, not spec.cwd — the builder
            # already pins that).
            owned: dict[str, dict] = dict(
                build_antigravity_mcp_config(
                    sys.executable, str(autoswe_repo_root())
                )["mcpServers"]
            )
            if isinstance(inline_cfg, dict):
                # Verbatim launch bits (no eager ``tools`` block — lazy is
                # fine for the review-only inline server).  ``env`` is
                # stripped: it must never reach the file.  ``cwd`` is added
                # even though ``build_mcp_inline_comment_server`` omits it:
                # agy spawns with cwd=spec.cwd (the target worktree) and the
                # entry's cwd defaults to that when unset
                # (docs/antigravity/mcp.md), where
                # ``python -m mcp_servers.autoswe_inline_comment_server``
                # cannot resolve — the mcp_servers package lives at the
                # autoSWE checkout root, same lever as the primary entry.
                owned["autoswe_inline_comment"] = {
                    **{k: v for k, v in inline_cfg.items() if k != "env"},
                    "cwd": str(autoswe_repo_root()),
                }
                inline_env = inline_cfg.get("env")
                if isinstance(inline_env, dict):
                    env.update(inline_env)
            if isinstance(comment_cfg, dict):
                server_env = comment_cfg.get("env")
                if isinstance(server_env, dict):
                    env.update(server_env)
            _merge_antigravity_mcp_config(owned)

        run_mode = str(spec.mode or "read_write")
        read_only = run_mode in _READ_ONLY_MODES
        if read_only:
            enforcement = "settings-deny"
        elif run_mode == "read_write" and bool(harness_cfg.get("skip_permissions", True)):
            enforcement = "skip-flag"
        else:
            enforcement = "none(expected)"

        log(
            f"[ANTIGRAVITY] running model={model} mode={run_mode} "
            f"enforcement={enforcement} "
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
            the headless soft-deny notice (spike P3b) are surfaced as soon as
            they are seen — at info level on a read-only run (a denied
            write/command there is the enforcement WORKING, §4.4) and at
            [WARN] elsewhere (during a read_write run a soft-deny means the
            skip flag is not in effect — the Phase 1 reading).
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
                        text = raw_line.decode("utf-8", errors="replace").strip()[:200]
                        if read_only:
                            # Expected: the staged deny rules biting (a denied
                            # write/command).  Not a misconfiguration.
                            log(f"[ANTIGRAVITY] soft-denied (expected, read-only): {text}")
                        else:
                            log(f"[WARN][ANTIGRAVITY] soft-denied: {text}")
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
        except asyncio.CancelledError:
            # The runner wraps backend.run() in its OWN wait_for(spec.timeout),
            # which fires _HARD_TIMEOUT_SLACK seconds BEFORE our backstop and
            # *cancels* this coroutine mid-gather — our TimeoutError branch
            # below would then never run and the agy process tree would be
            # orphaned on every cancel/retry (the exact #327 gap).  Kill the
            # tree before propagating the cancellation.
            _kill_tree(process)
            log("[ANTIGRAVITY] run cancelled — killed process tree")
            raise
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

        # Redact the RESOLVED GEMINI_API_KEY — whichever source won the
        # precedence merge above (os.environ / named field / profile env /
        # env_overrides), not just the profile field — from the stderr tail
        # once, here.  The tail feeds BOTH the failure log below AND the
        # stderr_tail _finalize turns into RunResult.text on the config_error
        # path, which is posted to a public GitHub FAILED comment;
        # mask_sensitive() has no Gemini-style pattern, so nothing downstream
        # would catch it.
        resolved_api_key = str(env.get("GEMINI_API_KEY") or "")
        if resolved_api_key:
            stderr_tail = stderr_tail.replace(
                resolved_api_key.encode("utf-8", "replace"), b"***"
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
            # the real error text is on stderr.  Log the tail (already
            # key-redacted above, regardless of which source set the key).
            stderr_text = stderr_tail.decode("utf-8", errors="replace").strip()
            if stderr_text:
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

        # Phase 4 §4.4 enforcement self-check: init.permission_mode is the
        # CLI's own attestation of what it let the run do.  A read-only phase
        # that reports a non-read-only posture means the staged --settings
        # deny rules were NOT honored — and a plan run that silently has
        # write access is worse than a failed plan run, so this fails hard.
        # Deviation from the plan's letter ("set error_flag" only), documented
        # deliberately: with error_flag alone the run still RETURNS, and the
        # planner/reviewer never inspect the subtype (agy_plan_waiting_status
        # transition proves it), so a silently-degraded plan would park the
        # task in `waiting` as if the run were legitimate — the opposite of
        # §4.6's mandated mismatch→failed outcome.  Raising routes through
        # the handler's exception boundary → FAILED.  error_flag is still set
        # for any path that swallows the raise, and the raise happens after
        # the process is reaped + the staged dir is reclaimed by the caller's
        # finally.  read_write posture drift gets a WARN, never a failure.
        # Fail CLOSED on a missing attestation too (review round-2 MEDIUM):
        # init_seen marks "the CLI got far enough to attest", so an init
        # without permission_mode is a CLI ignoring the staged --settings AND
        # hiding its posture — the same silent-write-access case, not a
        # pre-init death (init_seen False: nothing attested anything; the
        # stream's own error/killed mapping decides).  The posture error also
        # supersedes subtype="killed" by design: a misattested read-only run
        # that then got hard-timeout-killed must not slip through on the
        # retryable killed path.  Error details carry only mode/expected/
        # actual — the staged temp file is already deleted by the finally and
        # this message reaches the public FAILED comment via the planner.
        expected_posture = _expected_permission_mode(spec.mode, harness_cfg)
        if read_only and acc.init_seen and acc.init_permission_mode is None:
            acc.error_flag = True
            detail = (
                "read-only phase: CLI did not attest permission_mode under "
                f"staged --settings (mode={spec.mode!r}, expected "
                f"{expected_posture!r}, actual <missing>)"
            )
            log(f"[ERROR][ANTIGRAVITY] {detail}")
            raise RuntimeError(f"[ANTIGRAVITY] {detail}")
        if (
            expected_posture is not None
            and acc.init_permission_mode is not None
            and acc.init_permission_mode != expected_posture
        ):
            if read_only:
                acc.error_flag = True
                detail = (
                    f"read-only not in effect: init.permission_mode="
                    f"{acc.init_permission_mode!r}, expected {expected_posture!r} "
                    f"(mode={spec.mode!r}) — the per-run --settings deny rules "
                    f"were not honored"
                )
                log(f"[ERROR][ANTIGRAVITY] {detail}")
                raise RuntimeError(f"[ANTIGRAVITY] {detail}")
            log(
                f"[WARN][ANTIGRAVITY] init.permission_mode={acc.init_permission_mode!r} "
                f"but {expected_posture!r} expected — the skip flag may not be in effect"
            )

        return _finalize(
            acc, returncode, killed=killed, duration_seconds=duration, stderr_tail=stderr_tail
        )
