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
- [ ] Gate green
