# Phase 7 — Follow-ups (post-merge, each optional)

These are deliberately out of the core workstream. Each one is a separate issue
with its own go/no-go.

## 7.1 SDK transport → `can_use_tool` + the WAITING flow

**Why:** today only `claude_code` can pause on a question (AskUserQuestion →
`can_use_tool` callback → `autoswe:waiting`). The Antigravity SDK's policy engine
(`docs/antigravity/sdk-policies.md`) has `ask_user(tool, handler=…)`, a real
per-tool async callback. It would make Antigravity the second backend with the WAITING flow.

**Shape:** a second backend class, `AntigravitySdkBackend` (backend name
`antigravity_sdk`), in `backends/antigravity_sdk.py`. It is in-process, like
`claude_code`:
- `LocalAgentConfig(model=…, workspaces=[spec.cwd], mcp_servers=[McpStdioServer(..., env=<per-task>)], policies=…, response_schema=…, conversation_id=…, save_dir=…)`.
  Per-task MCP env goes directly on `McpStdioServer.env`, so the config-hash
  problem disappears.
- mode → `CapabilitiesConfig(enabled_tools=BuiltinTools.read_only())` for plan/review
  (`sdk-tools.md`), plus `deny`/`allow` policies.
- `can_use_tool` → a policy `ask_user("*", handler=adapter)`, where `adapter` calls
  `spec.can_use_tool(name, input, ctx)` and maps the result. `ask_question` →
  the existing `ask_user_question.py` interception.
- Progress via `response` token iteration + hooks; usage via `response.usage_metadata`.
- Dependency: an optional extra `autoswe[antigravity]` in `pyproject.toml`, imported
  lazily in the factory.

**Go/no-go inputs:** the Phase 0 §0.5 quick-look. Does the handler see the
`ask_question` payload? Can the run end cleanly from inside the handler? Does the
SDK leave no orphan processes on cancel? If any answer is no, drop this item.

**Tests:** an `AntigravitySdkFake` patching `google.antigravity.Agent`;
`test_runner_can_use_tool.py` parametrised over the new backend; the WAITING
transition rows from `TRANSITIONS` added to a curated `ANTIGRAVITY_SDK_TRANSITIONS`.

## 7.2 `WAITING` status → question flow (CLI)

If Phase 0 T10 showed that a headless run ending `WAITING` carries the question text
(in `result.response` or the last `agent_response`), the CLI backend could set
`question_posted`-equivalent semantics: post the question as a comment, mark the task
waiting, and resume with `--conversation <id>` when the user replies. This needs a new
`RunResult` signal (e.g. `question_text`) and a planner branch, so it changes the
handlers. It needs its own design note and `TRANSITIONS` rows. Compare it with 7.1
first, because 7.1 is the cleaner route if the SDK works.

## 7.3 Optional cost estimate

The CLI reports tokens, not USD. If operators want a number on API-key billing
(not subscription credits), add `antigravity_pricing.py` mirroring
`codex_pricing.py`: a maintained per-model table that returns `None` for unknown
models and for subscription/OAuth auth (credits are not dollars). The default
stays `None`. Only do this if someone actually asks for it.

## 7.4 Multi-turn session reuse

`--input-format stream-json` can keep one process alive across turns. autoSWE's
model is one process per phase, so there's no need today. Note it as a possible
latency win if a future handler runs several prompts back to back (e.g. the test-gate
"fix the failing tests" loop in `harness/test_gate.py`).

## 7.5 Upstream gaps to watch

Re-check these whenever `docs/antigravity/` is refreshed:
- a session **fork** flag would let us advertise `session_fork`;
- a per-invocation settings flag would simplify Phase 4;
- a `--session-id`-style pre-seed would let `RunResult.session_id` survive a crash before `init`;
- a turn cap would let us honor `max_turns`.
