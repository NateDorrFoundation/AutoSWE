# Phase 5 — MCP comment posting (`mcp` capability)

**Goal:** the agent posts plans, questions and progress through autoSWE's
`autoswe_comment` MCP server (the same server Claude and pi use,
`autoswe/harness/mcp_config.py`). Then `RunResult.plan_posted` / `plan_posted_body` /
`question_posted` are set from the stream, and the planner's `has_mcp` branch
(`planner.py:104`) turns on for this backend.

**Gate:** Phase 0 §0.4. M2 must confirm the eager tool name, and M3 must confirm
that per-task env reaches the MCP child through `agy`'s process env.

## 5.1 Where the server is configured

Follow the pi design: **stable values in a config file, per-task values in the
process env**.

- **Config file:** the `mcp_config.json` the CLI reads. With Phase 4 Option A this is
  `<config_home>/base/.gemini/config/mcp_config.json`, copied into each run dir. Without
  Option A it is the global `~/.gemini/config/mcp_config.json`. In either case autoSWE owns
  **only** the `autoswe_comment` (and `autoswe_inline_comment`) entries and merges them
  entry-scoped, preserving other servers. This is the same approach as
  `pi.py:_write_pi_mcp_json`.
- **Never** write a worktree `.agents/mcp_config.json`. It pollutes the repo under
  test, and M4 checks whether workspace entries can shadow global ones.
- Add `build_antigravity_mcp_config(python_path, repo_root) -> dict` to
  `mcp_config.py`, next to `build_pi_mcp_json`. Entry shape:

```json
{
  "mcpServers": {
    "autoswe_comment": {
      "command": "<abs python>",
      "args": ["-m", "mcp_servers.<comment server module>"],
      "cwd": "<autoswe repo root>",
      "timeoutSeconds": 120,
      "tools": {
        "post_plan":       { "eager": true },
        "post_question":   { "eager": true },
        "update_progress": { "eager": true }
      }
    }
  }
}
```

  Copy `command`/`args`/`cwd` from `pi_mcp_comment_server()` so the two stay in sync,
  and factor the shared part out if it is identical. `eager: true` is required: lazy
  tools go through `call_mcp_tool`, which costs an extra file-read turn and
  gives the model a reason to skip the tool. Lazy calls are still parsed as a
  fallback (§5.3).
- **Per-task env** (comment id, owner/repo, issue number, PAT, provider: the `env`
  block from `build_mcp_comment_server(task, repo_cfg)`) is merged into the **`agy`
  subprocess env** in `_run_async`, never into the file. If M3 showed that the MCP child
  does *not* inherit the process env, fall back to writing the per-task `env` into the
  **run-scoped** copy of `mcp_config.json` (Option A run dir only, which is safe because
  that file is never shared). Without Option A, Phase 5 is blocked.
- The file is written idempotently (compare before writing), so a host-stable file
  is written once, the same as pi.

## 5.2 Tool naming → `comment_tool_names()`

From `mcp.md` "Eager loading", eager tools are named `mcp_<serverName>_<toolName>`:

```python
ANTIGRAVITY_COMMENT_TOOL_NAMES = {
    "post_plan":       "mcp_autoswe_comment_post_plan",
    "post_question":   "mcp_autoswe_comment_post_question",
    "update_progress": "mcp_autoswe_comment_update_progress",
}
```

**Replace these with the exact names from Phase 0 M2** if they differ. This dict is
the single source of truth: the prompt placeholders `{{POST_PLAN_TOOL}}` etc. render
from it (`base.py` `CLAUDE_COMMENT_TOOL_NAMES` docstring), so no prompt template
changes.

## 5.3 Stream classification

Add `_classify_mcp_comment_call(step) -> tuple[str | None, str]`, which mirrors
`pi.py:_classify_mcp_comment_call` and returns `(role, body)` for tool steps:

| Shape | Match | Args |
|---|---|---|
| eager/direct | `tool_name == ANTIGRAVITY_COMMENT_TOOL_NAMES[role]` | `tool_info.parameters.body` |
| lazy proxy | `tool_name == "call_mcp_tool"` and `parameters.ServerName == "autoswe_comment"` | role = `parameters.ToolName`, body = `parameters.Arguments.body` (Arguments may arrive as a JSON string, so `_coerce_args` it) |

Classify on the `ACTIVE` tool step (the call). On the `DONE` step, if `tool_info.error`
is set, **un-set** `plan_posted`/`question_posted` for that call and log it. A
failed post must not count as posted, because the planner would then skip its
text fallback. (This is stricter than pi, which classifies on start. Note the
difference in `harnesses.md`.)

Then:
- `post_plan` → `plan_posted = True`, `plan_posted_body = body` (issue #241 re-push).
- `post_question` → `question_posted = True`. **Question-terminal precedence** (issue
  #230): after a question, ignore any later `post_plan` in the same run.
- `update_progress` → `callback(body)`.

## 5.4 Read-only interplay (Phase 4)

The read-only `permissions` block must `allow` `mcp(autoswe_comment/*)` (and
`mcp(autoswe_inline_comment/*)` for review). Otherwise the default **Ask** for
unconfigured MCP tools (`permissions.md`, "Default CLI system behaviors") is
soft-denied headless, and the plan is never posted. Add a test for this.

## 5.5 Warm-up (only if M5 says so)

If a cold `~/.gemini/antigravity-cli/mcp/` cache changes what the agent sees on its
first run (it shouldn't with eager tools), add `antigravity_warmup_targets()` +
`warm_up_mcp_cache()` mirroring pi. Then generalise `cli.py:_cmd_warmup`
(currently hard-coded to `backend != "pi"`, `cli.py:131`) to dispatch on backend.
Otherwise write "no warm-up needed (eager registration)" in the spike doc and skip this.

## 5.6 Capabilities

`capabilities()` gains `"mcp"`. `comment_tool_names()` returns
`dict(ANTIGRAVITY_COMMENT_TOOL_NAMES)` (a fresh dict each call, per the protocol).

## 5.7 Tests

- `test_antigravity_backend.py`: config build + entry-scoped merge (foreign servers
  preserved, idempotent write); per-task env reaches the subprocess env and **never**
  the file (assert the file bytes don't change across two different tasks); classify
  eager + lazy shapes (including string `Arguments`); failed post on `DONE` reverts the flag;
  question-precedence; `update_progress` → callback.
- `tests/test_mcp_integration.py`: add antigravity to the backend parametrisation.
- `test_backend_capabilities.py`: `mcp` present; `comment_tool_names()` values are the
  antigravity spellings; the prompt render test (`test_prompts.py`) shows them.
- `AntigravityFake`: `script_plan(..., mcp=True)` / `script_questions(..., mcp=True)` emit
  eager tool steps; `mcp="lazy"` emits the proxy shape.
- `transitions.py`: change the plan rows in `ANTIGRAVITY_TRANSITIONS` to the MCP path,
  and keep one text-fallback row (`mcp` advertised but the agent didn't call the tool).

## Exit criteria

- [ ] Live: `/plan` on the test project posts the plan through MCP (sticky comment finalized
      in place, no `<AUTOSWE_PLAN>` scraping in the log), on GitHub **and** Azure
- [x] Gate green (2026-11-20, issue #322: `pytest -q -m "not live"` + `ruff check autoswe tests scripts` clean)

## Completion record (2026-11-20, issue #322)

Implemented exactly as specified above, on the `autoswe/issue-322` branch off
`antigravity`:

- `mcp_config.py`: `comment_server_launch` (shared launch triple with pi),
  `antigravity_comment_server` (timeoutSeconds + three `eager: true` tools),
  `build_antigravity_mcp_config`, `antigravity_mcp_config_path` (home resolved
  at call time). Phase 4 Option C means the global file is the only surface
  (M4: unshadowable), so §5.1's Option-A run-dir variant does not apply.
- `antigravity.py`: `_mcp_comment_active` trigger; entry-scoped
  `_merge_antigravity_mcp_config` (foreign servers preserved, compare-before-
  write, OSError → log + text fallback, never raise); per-task env routed to
  the agy subprocess env only (M3), inline entry written verbatim-minus-`env`;
  `_classify_mcp_comment_call` (eager names incl. string-`Arguments` lazy
  proxy fallback); ACTIVE-classify with DONE/ERROR `tool_info.error` un-set
  (§5.3) over per-role call counts — an earlier successful post keeps its
  flag when a later one fails; `mcp` capability + `ANTIGRAVITY_COMMENT_TOOL_NAMES`.
  §5.4 needed no production change (Phase 4 already allowed
  `mcp(autoswe_comment/*)` / `mcp(autoswe_inline_comment/*)`); §5.5 SKIPPED
  per M5 — no warm-up, `cli.py:_cmd_warmup` untouched.
- Tests: `test_antigravity_backend.py` (classifier, merge semantics, env
  routing incl. never-in-file bytes, flag semantics incl. the Risks edge),
  `test_config.py` (builders/path), `test_backend_capabilities.py`,
  `test_backend_parity.py` (name + capability parity), `test_prompts.py`
  (agy eager names render), `test_planner_returns.py` (has_mcp branch on the
  agy axis), `AntigravityFake` (`mcp=True/"lazy"/mcp_error` scripting),
  scenario feeder (`mcp_tool` → eager tool pair), and two new
  `ANTIGRAVITY_TRANSITIONS` rows (`agy_plan_mcp_post_plan`,
  `agy_plan_mcp_post_question`); `fresh_plan_command` is the kept
  text-fallback row (mcp advertised, tool never called).

**Gate:** green (offline suite + ruff).
**Live exit criterion:** NOT yet checked — requires a host with the `agy`
binary (absent here). The two live boxes above stay open for the Phase 6
dedicated-box pass.

**Review-round follow-ups (rounds 2–5, same branch):**
- Round 2: `_comment_steps` registers only calls that were *counted*, so a
  question-terminal-ignored `post_plan` can never have its error-DONE wipe a
  successful plan's flag (`test_ignored_plan_error_done_cannot_wipe_successful_plan`);
  the owned inline entry carries `cwd=autoswe_repo_root()` (agy inherits
  `spec.cwd` when the entry cwd is unset, where `mcp_servers` cannot resolve).
- Round 3 (tests): §5.7 pass-through parametrisation over the three
  MCP backends in `test_mcp_integration.py`; the HOME-redirect path test now
  relocates `USERPROFILE` alongside `HOME` (Windows `Path.home()` order).
- Round 4: the merge **prunes** owned keys absent from the current run's
  owned set (stale `autoswe_inline_comment`), foreign keys never touched
  (`test_merge_prunes_stale_owned_inline_entry`).
- Round 5: concurrency safety of the global file is the **lock-serialised
  read-modify-write** (advisory exclusive lock on sibling `<file>.lock`,
  `fcntl.flock`/`msvcrt.locking`, best-effort) — NOT "concurrent runs write
  identical bytes, benign": once the prune exists, concurrent runs' owned
  sets legitimately differ, so the earlier stable-bytes argument no longer
  covers interleavings (`test_merge_serialises_under_sibling_lockfile`,
  `test_merge_proceeds_unlocked_when_lock_fails`,
  `test_merge_proceeds_unlocked_when_lockfile_unusable`). The backend's owned
  primary entry is now seeded from `build_antigravity_mcp_config`, making
  the spec-mandated builder load-bearing (pinned by
  `test_subprocess_entry_matches_mcp_config_builder`).
- Round 6: the merge's read arm catches `(OSError, UnicodeDecodeError)` —
  an undecodable config (e.g. PowerShell `>`-redirected UTF-16LE) is a
  `ValueError` that previously escaped as a hard task FAILED instead of the
  documented log-and-rewrite degrade (`test_merge_recovers_from_undecodable_config`);
  the final write is same-dir temp + `os.replace()` so the lock-free `agy`
  child never sees a truncated file mid-rewrite
  (`test_merge_writes_atomically_via_os_replace`).
  **Out-of-scope follow-up (Phase 6):** `pi.py:_write_pi_mcp_json` has the
  identical `UnicodeDecodeError` read-arm gap — same one-line fix applies,
  left untouched here by scope decision.
