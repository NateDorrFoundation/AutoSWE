# Phase 0 — Spike & fixture capture

**Goal:** turn every open question in the `harnesses.md` "candidate" section into a
recorded, evidence-backed answer before any backend code is written. Also capture
the real `stream-json` output that becomes the canonical test fixtures.

**Output:**
- `docs/autoswe/spike-antigravity.md` — the decision record, modelled on `spike-pi-mcp.md`.
  It records the host, `agy --version`, the auth mode, each experiment's exact
  command, the raw output excerpt, and the conclusion.
- `tests/fixtures/antigravity/*.jsonl` — redacted, real event streams (list below).
- An updated capability-matrix row for every **(if confirmed)** item in the README.

**Where:** the dedicated autoSWE box (Windows 11, the same host as the `pi-local` /
`ollama-codex` profiles). Run everything inside a throwaway clone of the test
project, **not** in this repo.

## 0.1 Install & auth

1. Install the CLI following `docs/antigravity/getting-started.md` (CLI tab,
   PowerShell installer). Record:
   - the resolved binary (`(Get-Command agy).Source`): is it `.exe`, `.cmd`, or `.ps1`?
     This decides the `_resolve_agy_executable` branch in Phase 1.
   - `agy --version`. This becomes the floor for the Phase 6 version test.
2. Authenticate in each mode autoSWE might use, and note which one works
   **without a TTY** (run from a scheduled task / `Start-Process -NoNewWindow`):
   - OAuth (keyring: Windows Credential Manager, `install.md:73`).
   - Gemini API key: `GEMINI_API_KEY` + `"modelProvider": "gemini"` in
     `~/.gemini/antigravity-cli/settings.json` (`cli-reference.md:193`). Confirm whether
     the env var alone is enough or the setting is also required. This decides
     whether the profile's `gemini_api_key` field can be env-only.
3. Confirm the unauthenticated behavior: the run must exit non-zero with
   `authentication required` instead of hanging (`cli-headless.md`, Note). Record the
   exit code and the `result.status`/`error` values. They feed the subtype mapping.
4. Set `"useG1Credits": false` and record what a quota-exhausted run looks like
   (status, error string, exit code), so the poller does not silently burn paid
   overage credits (`cli-credits.md`).

## 0.2 Transport experiments (CLI)

Run each one with `--output-format stream-json` and save the raw stdout:

| # | Experiment | Question it answers | Fixture |
|---|---|---|---|
| T1 | `agy -p "Reply with OK" --output-format stream-json --model <m>` | Baseline event shape. Is `conversation_id` on `init`? | `basic_success.jsonl` |
| T2 | Prompt via **stdin**: `--input-format stream-json --output-format stream-json`, write one `user` event, **close stdin** | Does the process run exactly one turn and exit 0 after `result`? Is a ~20 KB prompt accepted? (**This is the planned transport.**) | `stdin_single_turn.jsonl` |
| T3 | A coding task in a temp repo with `--dangerously-skip-permissions` ("create hello.txt and run `git status`") | Shapes of the tool steps: `tool_name` values for create/edit/command, `tool_info.parameters` keys (e.g. `CommandLine`, the file-path key), `ACTIVE`→`DONE` pairing | `tool_steps.jsonl` |
| T4 | A task that makes a tool fail (`run_command` on a missing binary), then recovers | `tool_info.error` shape. Is the final `status` still `SUCCESS`? | `tool_error_recovered.jsonl` |
| T5 | `--model does-not-exist` | Error envelope, exit code 1 | `unknown_model.jsonl` |
| T6 | `--json-schema <file>` **plus** tool use (read a file, then answer in the schema) | Is `structured_output` populated after a multi-step agentic run, not just a one-shot answer? A schema-violation case? | `structured_output.jsonl` |
| T7 | Resume: run T1, then `--conversation <id>` with "what did I ask?" | Resume works headless. Does the id stay the same or change? | `resume.jsonl` |
| T8 | Kill mid-run (`taskkill /T /F` on the tree after 5 s) | Does the CLI leave child processes behind? Is the conversation still resumable? Last line written? | `killed_partial.jsonl` |
| T9 | `--print-timeout 10s` on a long task | Timeout status (`CANCELED`? `ERROR`?) and exit code | `print_timeout.jsonl` |
| T10 | A prompt that makes the agent call `ask_question` | Does the run end with `status: "WAITING"`? Can `ask_question` be denied (`permissions.deny`) or is it absent in headless? | `waiting.jsonl` |
| T11 | Prompt that starts with `/plan` text, with and without `--disable-slash-commands` | Proves the flag stops issue-comment text from being run as a CLI slash command | — |
| T12 | Check where `cwd` comes from: launch with the subprocess `cwd=<repo>` and confirm `init.cwd` | There is no `-C` flag, so the subprocess cwd is the only lever | — |
| T13 | A run that emits a very large single line (cat a 5 MB file) | Line-size behavior, to confirm the 16 MiB bound is enough | — |
| T14 | `subagent_info` on a step that spawns a subagent (`start_subagent`) | Shape for progress rendering. Do subagents need their own handling? | `subagent.jsonl` |

**Redaction:** run fixtures through the same scrubbing as `scripts/capture_scenario.py`
(tokens, emails, absolute user paths → `<HOME>`), and keep `usage` blocks as they are.

## 0.3 Permission / read-only experiments (feeds Phase 4)

The open question: **can a single run be made read-only without mutating the
global `~/.gemini/antigravity-cli/settings.json`** (that would race under
`MAX_CONCURRENT > 1`)?

| # | Experiment | Pass criterion |
|---|---|---|
| P1 | `--mode plan` without `--dangerously-skip-permissions`, prompt: "write a file named x.txt" | No file is written. Record whether it is soft-denied (the stderr notice, `cli-headless.md` "Permissions in headless mode") or refused by the model |
| P2 | Default mode, no skip flag, `permissions.deny: ["write_file(<abs repo path>)", "command(*)"]` | Writes and commands are blocked, but reads (`view_file`, `search_directory`) work |
| P3 | Is there a **per-invocation config override**? Try in order: (a) workspace `<repo>/.agents/settings.json` (does it exist? MCP has a workspace file at `.agents/mcp_config.json`); (b) overriding `USERPROFILE`/`HOME` to an isolated dir. Does auth survive, given it lives in the OS keyring rather than a file? (c) any `--settings`/config-dir flag in `agy --help` (not in the mirrored docs, so check the live binary) | Find a lever that scopes the deny rules to one run |
| P4 | Same as P2 under `--sandbox` | Does the sandbox add OS-level write blocking on Windows (`sandbox.md`; v1.4.4+)? |
| P5 | Does a read-only run still allow MCP tool calls (needed for `post_plan`)? `mcp(autoswe_comment/*)` allow rule | The MCP call goes through while `write_file` stays denied |

**Decision rule:** if P3 finds a per-run lever and P1 or P2 blocks writes,
Phase 4 ships `mode`. Otherwise Phase 4 ships the documented degrade (no `mode`,
loud warning + `ensure_worktree_unchanged`), and the Codex-equivalent row in the
README matrix becomes final.

## 0.4 MCP experiments (feeds Phase 5)

| # | Experiment | Question |
|---|---|---|
| M1 | Add the `autoswe_comment` server to `~/.gemini/config/mcp_config.json` (global), **without** `eager`. Ask the agent to post a plan | Lazy path: the agent calls `call_mcp_tool` with `{ServerName, ToolName, Arguments}` (`mcp.md`, "Lazy loading"). Record the `tool_name` / `tool_info.parameters` shape |
| M2 | Same with `"tools": {"post_plan": {"eager": true}, "post_question": {"eager": true}, "update_progress": {"eager": true}}` | Eager path: the tool is exposed as `mcp_autoswe_comment_post_plan` (single underscores, `mcp.md` "Eager loading"). Confirm the exact `tool_name` in the stream |
| M3 | Leave the per-task vars (`AUTOSWE_*` comment id / repo / PAT) **out** of `mcp_config.json` and set them only on the `agy` process env | Does the MCP child inherit `agy`'s process env? (This is the pi env-vs-config-hash rule. If it fails, design an alternative: a per-run workspace `.agents/mcp_config.json` outside the repo via `--add-dir`, or a wrapper script) |
| M4 | Workspace config `<repo>/.agents/mcp_config.json` | Is it merged with the global one? Who wins on a name clash? (A target repo must not be able to shadow `autoswe_comment`) |
| M5 | First run after a fresh install (cold `~/.gemini/antigravity-cli/mcp/` cache) | Is a warm-up needed (as with pi), or does the eager registration make it moot? |

## 0.5 SDK quick-look (feeds Phase 7, timebox 2 h)

Only to decide whether Phase 7 is worth doing:
- `pip install google-antigravity` in a scratch venv. Does `Agent` spawn a subprocess
  (which binary)? Does it reuse the CLI's auth?
- Can `ask_user("ask_question", handler=…)` intercept the question payload, and can
  the handler **end the run** cleanly (the AskUserQuestion → pause semantics)?
- Does `response.usage_metadata` exist per turn, and are tool events observable via
  hooks (`hooks.pre_turn`, `on_tool_error`, anything like `post_tool`)?

## Exit criteria

- [ ] `spike-antigravity.md` committed, with a filled-in answer for every table row above
- [ ] Fixtures committed under `tests/fixtures/antigravity/` (redacted)
- [ ] README capability matrix updated (strike out anything that failed)
- [ ] Go/no-go recorded for: stdin transport (T2), `mode` (P1–P3), `mcp` naming + env (M2/M3),
      `structured_output` (T6)
- [ ] If T2 fails (stdin single-turn does not exit cleanly): record the fallback, which is `-p` with
      a short bootstrap prompt that tells the agent to `view_file` a temp prompt file
      outside the repo, and adjust Phase 1
