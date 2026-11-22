# Phase 3 — Fake, scenarios & transitions

**Goal:** the new backend gets the same layered offline coverage as codex and pi
(`docs/autoswe/testing.md`). The **real** factory → `AntigravityBackend` → stream
parser → `RunResult` path runs unmodified in scenario and transition tests.

## 3.1 `tests/fakes/antigravity_fake.py` — `AntigravityFake`

Model it on `tests/fakes/pi_fake.py`:

- Patch `asyncio.create_subprocess_exec` (and keep `_get_real_create()` for the
  harness restore). Return a fake process whose `stdout` replays canonical
  `stream-json` lines, whose `stdin` captures the written bytes, and whose `wait()`
  returns the scripted rc.
- **The builder signatures must match `ClaudeFake` / `CodexFake` / `PiFake`**, so that
  existing transition-row response dicts feed it as-is:
  `script_response(text)`, `script_plan(plan_md)`, `script_questions(...)`,
  `script_fix(summary, files=...)`, `script_fail(error)`, `script_killed()`,
  `script_structured(payload)`. Each one builds lines from **templates derived from
  the Phase 0 fixtures**: `init` → `user_input` step → tool steps → `agent_response`
  steps → `checkpoint` → `result`. Don't hand-invent fields; load the field sets from
  `tests/fixtures/antigravity/`.
- `.calls`: a list of `{argv, cwd, env, stdin_events}` with argv already parsed
  (`model`, `conversation`, `json_schema_path`, `skip_permissions`, …), for
  `assert_antigravity_calls`.
- `script_questions` deserves a note. Until Phase 5 the backend has no MCP, so
  questions can only arrive as text (the planner's text-pattern fallback). After
  Phase 5 the builder emits the eager `mcp_autoswe_comment_post_question` tool step.
  Make the builder honor a `mcp=True` switch, the same as `PiFake` does.
- Simulate `fix` file edits the same way `PiFake` does (write to the worktree through
  `GitFake` / the real-git sandbox), so that `commit_and_push` sees real changes.

## 3.2 `tests/test_antigravity_fake.py` — fidelity

- Every builder's output parses through the **real** `_parse_line` / `_finalize` to the
  expected `RunResult` (text, session id, subtype, structured_output).
- Every line the fake emits has a key set that is a subset of the union of keys seen
  in the Phase 0 fixtures, per `event`/`step_type`. This catches drift when the
  fixtures are refreshed.
- Golden replay: each `tests/fixtures/antigravity/*.jsonl` fixture parses without
  warnings.

## 3.3 Contract / parity / capabilities

- `tests/test_backend_parity.py`: add `antigravity` to the parametrised backend list:
  `RunSpec` in → `RunResult` out; `ok` normalization; tuple unpacking; `retryable_*`
  types; `comment_tool_names()` keys equal `{"post_plan","post_question","update_progress"}`.
- `tests/test_backend_capabilities.py`: assert the exact Phase 1 set; that
  `has_read_only_enforcement` is False (until Phase 4 flips it, at which point this test
  is updated); `session_fork` absent; `plan_file` absent.
- `tests/test_runner_retry.py`: the runner retries `error`/`killed` and does **not** retry
  `config_error`; `OSError` from spawn is retried under the resolved backend's tuple.
- `tests/test_fake_parity.py`: add `AntigravityFake` to the fake-protocol coverage list.

## 3.4 Scenario harness axis (`tests/scenarios/harness.py`)

- `_setup_antigravity_harness(isolated_dir)`: writes `config/harnesses.json` with
  `{"antigravity": {"backend":"antigravity","model":"gemini-test","cli_path":"agy"}}`
  and clears `_harnesses_cache`. This mirrors `_setup_pi_harness`.
- `patch_antigravity(fake)` and `assert_antigravity_calls(...)` (in `tests/scenarios/runner.py`
  next to `assert_pi_calls`).
- Add the `antigravity: AntigravityFake | None` field to the world dataclass, and let
  `backend: str` accept `"antigravity"`. Then `patched_world(backend="antigravity")` works.

## 3.5 Transition matrix (`tests/scenarios/transitions.py`)

Add `ANTIGRAVITY_TRANSITIONS`, a curated subset run through
`patched_world(backend="antigravity")` and parametrised over `["github","azure"]`.
Pick the rows whose outcome **depends on backend capabilities**:

| Row | Why it matters for antigravity |
|---|---|
| `/plan` → `plan_ready` (text-pattern path) | no MCP before Phase 5 → `<AUTOSWE_PLAN>` / text fallback |
| `/plan` with structured output | `structured_output` capability → planner prefers the schema payload |
| `/plan` → read-only violation rolled back | before Phase 4: the loud-degrade + `ensure_worktree_unchanged` path |
| `/fix` → `fixed` (commit + push) | the core happy path |
| `/fix` → `failed` → `/retry` | **no `session_fork`** → resume-in-place / fresh (Codex semantics); provenance gate with `last_good_session_backend == "antigravity"` |
| mixed backends: claude plan → antigravity fix → `/retry` | provenance mismatch → fresh session, never handing a Claude session id to `--conversation` |
| `/review` → verdict via structured output | `HandlerResult.verdict` from `structured_output` |
| fix killed at timeout | `killed` → retry policy |
| `config_error` (unknown model) | FAILED comment carries the CLI error text, no retry |
| `/plan` that ends `WAITING` | surfaces as failed with the explanatory text (until Phase 7) |

Use `scripts/capture_scenario.py` to seed at least the `/fix` happy path from a
real Phase 0 run.

## 3.6 Real-git scenario (`git_scenario`)

Add one `GitWorld` test: an antigravity plan run whose fake writes a file in
the worktree **while `mode` is not advertised**. It asserts that
`ensure_worktree_unchanged` resets the tree, so the next phase starts clean. This
is the guarantee that makes it safe to ship before Phase 4.

## 3.7 CLAUDE.md test-convention table

Add these rows (the actual CLAUDE.md edit lands in Phase 6, but list them in the PR):
- antigravity backend (stream-json parsing, subprocess, flag mapping) → `test_antigravity_backend.py`
- `AntigravityFake` fidelity to the real parser → `test_antigravity_fake.py`

## Exit criteria

- [ ] `pytest -q -m "not live"` green, including `-m transition` with the new rows on both providers
- [ ] No test reaches the network or a real `agy` binary (assert via the fake's call log)
