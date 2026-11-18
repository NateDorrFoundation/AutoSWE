# Phase 1 — Backend core (`autoswe/harness/backends/antigravity.py`)

**Goal:** a working `AntigravityBackend` for the core run: fresh, resume, progress,
structured output, and the subtype mapping. Capabilities stay at the **minimal
honest set**. `mode` and `mcp` come in Phases 4 and 5.

**Template:** `autoswe/harness/backends/pi.py`, which has the same subprocess shape
(accumulator → line parser → async subprocess with concurrent
stdout/stderr readers + timeout-kill). Copy its structure and section ordering
so a reviewer can diff the two files side by side.

## 1.1 Module layout

```
antigravity.py
├── module docstring          — transport, stdin-prompt rationale, doc refs (docs/antigravity/cli-headless.md)
├── constants                 — _MAX_STREAM_BYTES = 16 MiB, _TERMINAL_STATUSES, _STATUS_TO_SUBTYPE
├── class _AgyAccumulator     — conversation_id, text chunks, final_response, status, error,
│                               structured_output, usage, saw_result, error_flag, tool_errors
├── _resolve_agy_executable(cli_path) -> (exe, prefix_argv)
├── _build_argv(spec, harness_cfg, schema_path) -> list[str]
├── _user_event(prompt) -> bytes                 — the single stdin line
├── _tool_label(step) -> str                     — progress rendering
├── _parse_line(line, acc, callback) -> None
├── _finalize(acc, returncode) -> RunResult
└── class AntigravityBackend  — capabilities / comment_tool_names / retryable_* / run / _run_async
```

## 1.2 Executable resolution

`_resolve_agy_executable(cli_path)`: order of preference is `spec.cli_path` → `harness_cfg["cli_path"]` → `shutil.which("agy")`. If the result ends in `.cmd`/`.bat`, return `("cmd", ["/c", path])`, as in `pi.py:_resolve_pi_executable`. If it is a `.ps1` shim (record this in Phase 0 §0.1), return `("powershell", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path])`. Raise a clear `FileNotFoundError` naming `cli_path` when nothing resolves. The runner's retry policy treats this as non-retryable, so it is a config error that surfaces once.

## 1.3 Command mapping

| Case | argv (after the exe prefix) |
|---|---|
| fresh | `--input-format stream-json --output-format stream-json --disable-slash-commands --model <model> [--effort <e>] [--agent <a>] [--dangerously-skip-permissions] [--sandbox] [--json-schema <tmp.json>] [--print-timeout <N>s] [--log-file <path>]` |
| resume | same + `--conversation <spec.resume>` |
| fork | **not supported**. If `spec.fork_session` is set, log `[ANTIGRAVITY] fork_session ignored (no fork primitive) — resuming in place` and treat it as resume. The capability gate in `_run_retry` already keeps this from happening; the log line is a guard in case it ever does |

Notes:
- **Prompt delivery:** never in argv. Write `_user_event(spec.prompt)`, which is
  `{"event":"user","message":{"content": <prompt>}}` + `\n` (UTF-8,
  `json.dumps(..., ensure_ascii=False)`), to stdin, then `drain()` and `close()`. This
  is the README lesson #1. Phase 0 T2 confirms that the process runs one turn and exits.
- **`--disable-slash-commands` is always on.** Prompts embed issue-comment text that
  routinely starts with `/plan`, `/fix`, etc.
- **`--dangerously-skip-permissions`:** in Phase 1, emit it whenever the profile's
  `skip_permissions` is true (the default, which is the dedicated-machine posture,
  the same as Codex `bypass_approvals`). Phase 4 changes it so it is emitted only for
  `read_write` runs.
- **cwd:** set `cwd=spec.cwd` on the subprocess (there is no `-C` flag). After
  parsing `init.cwd`, log a warning if it does not resolve to the same path.
- **`--print-timeout`:** set it to `spec.timeout` seconds so that the CLI ends
  cleanly with a terminal `result` before our hard kill. Our own `asyncio.wait_for`
  uses `spec.timeout + 30` as the backstop.
- **`--json-schema`:** only when `spec.output_format` is set. Write
  `spec.output_format["schema"]` to a private `mkstemp` `.json` file. Always delete
  it in `finally`, including the kill path (the codex `--output-last-message`
  pattern). Do not pass the schema inline: it is too long for the Windows command
  line and has quoting hazards.
- **`--log-file`:** the optional profile field `log_dir`. When set, write
  `<log_dir>/<conversation-or-uuid>.log` so that the CLI's own log can be found
  next to `logs/<slug>/`.
- **`max_turns`:** accepted and ignored (documented no-op, the same as codex/pi).

## 1.4 Environment

The precedence matches the other backends (`harnesses.md` "Per-profile `env`"),
lowest to highest:
1. inherited `os.environ`
2. named fields: `gemini_api_key` → `GEMINI_API_KEY`. Vertex fields, if Phase 0 shows
   the CLI honors them: `GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_CLOUD_PROJECT`,
   `GOOGLE_CLOUD_LOCATION`
3. profile `env`
4. `spec.env_overrides`

(Phase 5 adds the MCP server's per-task `env` block on top of these, as pi does.)

## 1.5 Stream parsing (`_parse_line`)

Ignore non-JSON lines (log them at debug level). Dispatch on `event`:

| Event | Handling |
|---|---|
| `init` | `acc.conversation_id = evt["conversation_id"]`. Log `init.model`, `init.permission_mode`, and the tool count. Keep `init.tools` on the accumulator: Phase 4/5 assert against it |
| `step_update`, `step_type == "agent_response"` | Append `text_delta` to `acc.text_chunks` (keyed by `step_index`, so that `ACTIVE` partials followed by a `DONE` carrying the remainder don't double-count; confirm the accumulation semantics against fixture T1/T2). Fire `callback` with the delta, throttled like `pi._fire_delta_progress` |
| `step_update`, `step_type == "tool"`, `state == "ACTIVE"` | Progress line `Tool: <label>` via `_tool_label`: `run_command` → `$ <CommandLine>`; file tools → `<verb> <path>` (key names from fixture T3) |
| `step_update`, `step_type == "tool"`, `state == "DONE"` with `tool_info.error` | Log `[ANTIGRAVITY] tool <name> failed: <type>: <message>` and append to `acc.tool_errors`. **Do not** set `error_flag` (README lesson #4) |
| `step_update` with `subagent_info` | Progress line `Subagent: <role> (<type_name>)` |
| `step_update`, `step_type` in {`user_input`, `checkpoint`} | Ignore (usage bookkeeping only) |
| `result` | `acc.saw_result = True`; copy `status`, `response`, `error`, `structured_output`, `usage`, `num_turns`, `duration_seconds`. `conversation_id` here wins over `init` if they differ (log the mismatch) |
| anything else | Debug-log it and continue. The protocol is designed to be forward compatible |

A `ValueError` from `readline()` (line too long) sets `error_flag`, then
stdout is drained (pi #251 path).

## 1.6 Finalize → `RunResult`

```
text            = acc.final_response (result.response) or "".join(text_chunks)
session_id      = acc.conversation_id or None
cost_usd        = None              # tokens only — never estimate (README lesson #7)
duration_seconds= time.monotonic() delta (not result.duration_seconds — keep it local like codex/pi)
structured_output = acc.structured_output if isinstance(dict) else None
plan_file_path  = None ; plan_posted = False ; question_posted = False   (Phase 5 sets the latter two)
subtype / ok    = see table
```

| Condition (first match wins) | `subtype` | `ok` |
|---|---|---|
| we killed it (timeout) or rc < 0 | `killed` | False |
| `error_flag` (stream overflow) | `error` | False |
| no `result` event | `error` | False |
| `status == "SUCCESS"` and rc == 0 | `success` | True |
| `status == "WAITING"` | `error` (Phase 7 maps this to the question flow). Log `run ended WAITING on input — ask_question is not supported headless` | False |
| `status in {"CANCELED","INTERRUPTED"}` | `killed` | False |
| `status in {"ERROR","INVALID","RUNNING"}` or rc != 0 | `error` | False |

Log `usage` (input/output/thinking/cache_read/total tokens) once per run at info
level. These are the only cost signal, and operators will want them in `logs/<slug>/`.

`retryable_subtypes() -> {"error", "killed"}`.
`retryable_exceptions() -> (asyncio.TimeoutError, OSError)`.
Exception: an `unknown model` error (T5) and `authentication required` (§0.1 step 3)
are config errors and should **not** be retried. Give them a distinct
`subtype="config_error"`, which is outside the retryable set, and put the CLI's
error text into `RunResult.text`, so that the FAILED comment tells the operator
what to fix.

## 1.7 Subprocess run (`_run_async`)

Copy `PiBackend._run_async`:
- `asyncio.create_subprocess_exec(exe, *argv, cwd=spec.cwd, env=env, stdin=PIPE, stdout=PIPE, stderr=PIPE, limit=_MAX_STREAM_BYTES + 1)`
- `asyncio.gather(write_stdin(), read_stdout(), read_stderr(), process.wait())` under
  `asyncio.wait_for(..., spec.timeout + 30)`
- stderr: keep a bounded tail (last 64 KB) for the failure log. Lines that match the
  headless soft-deny notice (Phase 0 P1 records the exact text) are logged as
  `[WARN][ANTIGRAVITY] soft-denied: …`. They are the first sign of a permission
  misconfiguration.
- On timeout: kill the **process tree**. On Windows use `taskkill /T /F /PID`, because
  `process.kill()` only kills the `cmd` shim and leaves `agy` orphaned (confirm with
  Phase 0 T8). On POSIX use `os.killpg` with `start_new_session=True`. Put this in a
  small `_kill_tree(process)` helper. If pi.py has the same latent gap, flag it
  in the PR, but don't fix it here.

## 1.8 Capabilities (Phase 1)

```python
@classmethod
def capabilities(cls) -> set[str]:
    return {"resume", "progress_stream", "structured_output"}   # structured_output only if Phase 0 T6 passed
@classmethod
def comment_tool_names(cls) -> dict[str, str]:
    return dict(CLAUDE_COMMENT_TOOL_NAMES)   # no MCP yet → Claude default (Codex precedent); Phase 5 overrides
```

Without `mode` and `can_use_tool`, `has_read_only_enforcement` is False, so plan
and review loudly degrade and fall back on `ensure_worktree_unchanged`. This is
correct and safe for Phase 1.

## 1.9 Tests in this phase (`tests/test_antigravity_backend.py`)

These are unit tests at the function level. The full fake comes in Phase 3.
Patch `asyncio.create_subprocess_exec` with a minimal stub that replays the
Phase 0 fixtures.

- **argv:** fresh / resume / `fork_session` → resume + log; `--disable-slash-commands`
  always present; prompt **never** in argv (assert no argv element equals or contains
  the prompt); `--json-schema` path present only with `output_format`, and the file is
  deleted afterwards (also on the timeout path); `--effort`/`--agent`/`--sandbox`/`--log-file`
  map from the profile; `--print-timeout` = `spec.timeout`.
- **stdin:** exactly one line, valid JSON, `event == "user"`, content round-trips a
  20 KB prompt containing quotes, newlines and non-ASCII characters; stdin is closed.
- **exe resolution:** `.exe` direct; `.cmd`/`.bat` → `cmd /c`; `.ps1` → powershell;
  nothing resolves → `FileNotFoundError`; `cli_path` wins over PATH.
- **env precedence:** os.environ < `gemini_api_key` < profile `env` < `env_overrides`.
- **parsing** (one test per fixture): `basic_success`, `stdin_single_turn`, `tool_steps`
  (progress labels), `tool_error_recovered` (→ `success`), `unknown_model` (→ `config_error`),
  `structured_output`, `resume` (session id), `killed_partial`, `print_timeout`, `waiting` (→ `error`).
- **text assembly:** `result.response` wins; falls back to `text_delta` chunks when `result`
  is missing; ACTIVE+DONE deltas are not double-counted.
- **overflow:** a line over the limit → `error` and stdout drained, no exception escapes.
- **kill:** timeout → `_kill_tree` called, `subtype == "killed"`, temp files removed.
- **contract:** `isinstance(AntigravityBackend(), CodingBackend)`; `RunResult` unpacks as a 3-tuple.

## Exit criteria

- [ ] `antigravity.py` merged, not yet reachable from the factory (Phase 2 wires it)
- [ ] `test_antigravity_backend.py` green; `ruff check` clean
- [ ] Any `.cmd` / process-tree-kill gap found in `pi.py` or `codex.py` is written up as an issue, not fixed here
