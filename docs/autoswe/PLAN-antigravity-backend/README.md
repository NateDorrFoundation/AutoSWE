# Plan — add `antigravity` as a first-class coding backend

**Status:** Phases 0–5 complete (Phase 5 = MCP comment posting, issue #322;
the Phase 5 live exit criterion is open for the Phase 6 dedicated-box pass) —
spike verdict in
[`spike-antigravity.md`](../spike-antigravity.md): transport = headless CLI (BYOK
bridge auth), **GO** on stdin transport / `mode` via per-run `--settings` / MCP
eager+env / `structured_output` (advisory — validate downstream) (run 2026-11-18,
logged 2026-11-19 UTC) · **Branch:** `antigravity` (PRs target `antigravity`
during the workstream, then one merge PR to `master`) · **Reference docs:**
`docs/antigravity/` (49-page mirror) · **Prior art:** `PLAN-pi-backend.md` /
`PLAN-pi-mcp.md` (git history, commits `583b0a2`, `ec28279`), `spike-pi-mcp.md`.

## Goal

`AntigravityBackend` sits behind the existing `CodingBackend` protocol
(`autoswe/harness/backends/base.py`) exactly like `ClaudeCodeBackend`,
`CodexBackend` and `PiBackend`. Handlers (`planner`, `coder`, `reviewer`) stay
the same. Every difference from the other backends goes through
`capabilities()` / `comment_tool_names()`, and the handlers already gate on
those (`runner.backend_has_capability`, `runner.has_read_only_enforcement`,
`runner.comment_tool_names`).

A harness profile like this one should drive a full plan → fix → review cycle
from issue comments:

```json
"agy-neon": { "backend": "antigravity", "model": "gemini-3.8-flash-high" }
```

**Host decision (2026-11-18, Nate):** the workstream executes on the **Linux dev box
`megibot`** (agy 1.5.11 + SDK already staged). Windows-specific spike answers and
shim/kill paths are marked **windows-pending**: the code still ships the Windows
branches, but verification happens in a later Windows parity pass (Phase 6 follow-up).

## Transport decision: headless CLI first, SDK later

Antigravity offers two programmatic surfaces. The recommendation is to build the
core backend on the **headless CLI** (`agy`, print mode, `stream-json`) and treat the
Python SDK as a later evaluation (Phase 7). Phase 0 confirms or reverses this.

| Concern | Headless CLI (`agy`) | Python SDK (`google-antigravity`) |
|---|---|---|
| Event protocol | **Documented** NDJSON: `init` → `step_update`* → `result` (`cli-headless.md`) | Token/thought async iterators. Tool events only via hooks, with thin docs |
| Fits existing infra | Same subprocess shape as `codex`/`pi`. The `PiFake`-style fake, the `.cmd` resolution and the stream bounding all carry over | New in-process shape. Needs a new fake that patches SDK classes |
| Timeout / kill | Process kill is robust (proven on codex/pi) | Cancelling in-process tasks. The SDK spawns its own binary underneath ("manages binary discovery"), so orphan risk |
| Structured output | `--json-schema` → `result.structured_output` | `response_schema` (Pydantic) |
| Resume | `--conversation <id>` | `conversation_id` + `save_dir` |
| Per-tool callback (`can_use_tool`) | **No** | **Yes**: `ask_user(tool, handler=…)` policy. This would be the only non-Claude path to AskUserQuestion → `autoswe:waiting` |
| New Python dependency | None | `google-antigravity` (optional extra, deferred import) |

The CLI gets every capability the other subprocess backends have, plus
structured output, at the lowest risk. The SDK's one unique advantage is the
policy handler (a `can_use_tool` analogue). That advantage is additive, so it
becomes a follow-up instead of blocking the core backend.

## Target capability matrix (after Phase 5)

| Capability | Target | Mechanism | Phase |
|---|---|---|---|
| `resume` | ✅ | `--conversation <conversation_id>` | 1 |
| `progress_stream` | ✅ | `step_update` events (`tool`, `agent_response` `text_delta`) | 1 |
| `structured_output` | ✅ **confirmed (Phase 0, T6)** — but **advisory**: the model can violate the schema and still report SUCCESS; harness must validate downstream | `--json-schema <file>` → `result.structured_output`, populated after multi-step tool use | 1 |
| `mode` | ✅ **confirmed (Phase 0, P1–P3)** — but **not** via `--mode plan` (advisory, writes succeeded): per-run `--settings` inline `permissions.deny` with **directory-prefix** rules (`write_file(<repo>/)`, glob `/*` doesn't match) + no `--dangerously-skip-permissions` on read-only phases; MCP stays allowed under the deny profile (P5) | per-run `--settings` override ("never saved"), global settings untouched → race-free under `MAX_CONCURRENT > 1` | 4 |
| `mcp` | ✅ | stable `mcp_config.json` entry with `eager: true` tools; per-task env through the subprocess env | 5 |
| `session_fork` | ❌ | no fork primitive documented. Do not advertise it; `/retry` resumes in place or starts fresh (Codex semantics) | — |
| `can_use_tool` | ❌ (CLI) | SDK-only. See Phase 7 | 7 |
| `plan_permission` | ❌ | — | — |
| `plan_file` | ❌ | no native plan file. The planner's `~/.claude/plans` scan stays skipped | — |
| `max_turns` | no-op | no turn cap. The guard is wall-clock `timeout` (+ `--print-timeout`) | 1 |
| `cost_usd` | `None` | no usage at all on the BYOK bridge path — the bridge reports **zero token counts** and no USD (verified: all-zero `usage` in every fixture). Never estimate (the same rule as pi, the reverse of codex) | 1 |

## Phases

Each phase is a self-contained PR. **Each one must pass the pre-checkin gate**
(`pytest -q -m "not live"` and `ruff check autoswe tests`) and include its own tests
(the CLAUDE.md test convention). Phase 3 adds the cross-cutting scenario and
transition coverage on top.

| # | Phase | Output | Depends on |
|---|---|---|---|
| 0 | [Spike & fixture capture](phase-0-spike.md) | `docs/autoswe/spike-antigravity.md`, canonical stream fixtures, go/no-go on the open questions | — |
| 1 | [Backend core](phase-1-backend-core.md) | `autoswe/harness/backends/antigravity.py` (exe resolution, argv, stream parser, subprocess, subtype mapping) + unit tests | 0 |
| 2 | [Config & factory](phase-2-config-factory.md) | `KNOWN_BACKENDS`, factory branch, profile fields, `harnesses.json.example` | 1 |
| 3 | [Fake, scenarios & transitions](phase-3-tests.md) | `AntigravityFake`, `backend="antigravity"` scenario axis, `ANTIGRAVITY_TRANSITIONS`, parity/capability rows | 1, 2 |
| 4 | [Read-only enforcement (`mode`)](phase-4-read-only-mode.md) | per-run permission isolation, mode → flags/rules mapping, `mode` capability | 0, 3 |
| 5 | [MCP comment posting (`mcp`)](phase-5-mcp-comments.md) | `mcp_config.json` staging, tool naming, stream classification, `mcp` capability | 0, 3 |
| 6 | [Docs, ops & live E2E](phase-6-docs-ops.md) | `harnesses.md` section, CLAUDE.md/README, setup scripts, CLI version floor, E2E matrix | 1–5 |
| 7 | [Follow-ups](phase-7-follow-ups.md) | SDK transport evaluation (`can_use_tool` / WAITING), `WAITING` status mapping, optional cost table | 6 |

Phases 4 and 5 are independent, so they can run in parallel after Phase 3. Until
Phase 4 lands, the backend does **not** advertise `mode`. Plan and review phases
on an Antigravity profile then loudly degrade and fall back on
`ensure_worktree_unchanged`, which is the existing Codex path and safe by
construction.

## Lessons carried over from codex/pi (do not re-learn them)

1. **The prompt never goes on the command line.** Windows `cmd /c` caps the command
   line at ~8191 chars, and `/fix` prompts are 10–20 KB (pi.py header comment). For
   `agy`, use `--input-format stream-json` and write one `{"event":"user",…}` line
   to stdin, then close stdin (Phase 1).
2. **Resolve `.cmd`/`.bat` shims and invoke them through `cmd /c`.** `cli_path` is
   pinned so that cron's minimal PATH works (`pi.py:_resolve_pi_executable`).
3. **Bound the StreamReader** at `_MAX_STREAM_BYTES + 1` (16 MiB), and turn a
   `ValueError` from `readline()` into `subtype="error"` (issue #251).
4. **A tool error is not a run error.** A failed tool step that the agent recovers
   from must not sink a run whose final `result.status` is `SUCCESS` (the pi
   `isError` revision).
5. **Per-task values never go into a host-stable config file.** Only stable values go
   in `mcp_config.json`. Per-issue identity goes through the subprocess env (the pi
   env-vs-config-hash rule, Phase 5).
6. **Fail fast on a missing `model`** in the factory. `agy` would otherwise use the
   account's last-selected model, which is not reproducible.
7. **Never guess cost.** Use `cost_usd=None` unless the CLI reports USD.

## Definition of done (workstream)

- An `antigravity` profile drives the live E2E corpus (`tests/e2e/`) on GitHub and
  Azure, with results recorded in `tests/e2e/MATRIX.md`.
- `docs/autoswe/harnesses.md` has a full `#### antigravity` section (in the same
  shape as `pi`), and the "candidate" section is removed.
- The CLAUDE.md overview and test-convention table list the new backend and its test files.
- The gate is green on `master` after the merge.
