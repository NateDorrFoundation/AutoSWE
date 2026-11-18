# Phase 6 — Docs, ops & live E2E

**Goal:** the backend is documented to the same standard as `pi`, operable from a
fresh install, and proven against the live E2E corpus.

## 6.1 Docs (`docs/autoswe/` first, per the CLAUDE.md derivation rule)

- **`harnesses.md`**
  - Replace the `#### antigravity (candidate …)` section with a full `#### antigravity`
    section in the same order as `#### pi`: transport summary → requirements → profile
    fields → capabilities → mode → tools table (Phase 4) → MCP mechanism + the
    env-vs-config rule (Phase 5) → retry semantics (resume-in-place, no fork; link
    `#retry-semantics`) → command mapping (fresh/resume) → session identity (parse-only:
    `init`/`result` `conversation_id`, which is weaker than pi's pre-seeded id) →
    retryable subtypes/exceptions (+ `config_error`) → cost (`None`, tokens logged) → known
    limitations (no `max_turns`, no AskUserQuestion/WAITING, no plan file, no system-prompt
    flag, Orchestrator ignores `effort`).
  - Profile Schema table: add the antigravity fields (Phase 2 §2.3). Update the
    `backend` row and the `model` "required for" cell.
  - Per-profile `env` precedence table: add an antigravity column.
  - "Mixing Backends": add one pattern, e.g. "Antigravity Orchestrator for fix, Claude for review".
  - Shared-contract section: add antigravity to the `retryable_subtypes` sentence.
- **`config.md`**: mention `harnesses.json.example`'s new profiles.
- **`safeguards.md`**: `--dangerously-skip-permissions` on fix runs (the dedicated-box
  posture); repo-supplied `.agents/` rules/skills/MCP load by design (as the Claude
  `.mcp.json` note does); `useG1Credits=false` recommendation; the Phase 4 per-run
  permission isolation.
- **`debugging.md`**: where `agy` logs go (`log_dir`), how to replay a conversation
  (`agy --conversation <id>` interactively), soft-deny stderr lines, auth-expired symptoms
  (`config_error` + `authentication required`), how to clean stale Phase 4 run dirs.
- **`testing.md`**: add `AntigravityFake` to the fakes list and the `ANTIGRAVITY_TRANSITIONS`
  axis.
- **`spike-antigravity.md`**: finalise it (Phase 0 output). Link it from `harnesses.md`.
- **`README.md` (docs/autoswe)**: drop "candidate" from the harnesses row and the doc-mirror bullet.

Then the downstream files:
- **`CLAUDE.md`**: the overview "Pluggable coding backends" paragraph adds
  `antigravity` (Antigravity CLI, `stream-json`) and the module path; the fakes bullet
  list adds `AntigravityFake`; the test-convention table adds the two rows from
  Phase 3 §3.7.
- **Repo-root `README.md`**: add it to the backends list.

## 6.2 Ops

- **CLI version floor:** add `tests/test_agy_cli_version.py` (or extend
  `test_cli_version.py`'s pattern) with a pinned floor = the Phase 0 `agy --version`.
  It has the same skip semantics: skip when no `agy` resolves or the version can't be
  queried, so CI without `agy` stays green. Expose `MIN_AGY_VERSION` from
  `antigravity.py`, and have the backend log `[WARN]` once per process when the
  installed CLI is below it (don't fail the run).
- **`setup.sh` / `setup.ps1`**: no install step (the CLI is a user-level install that
  needs interactive auth). Add a **check** that, when any harness profile uses
  `backend: antigravity`, runs `agy --version` and prints the one-time
  "run `agy` interactively once to authenticate" hint. Add the Phase 5 warm-up call only
  if Phase 5 §5.5 was needed.
- **`update.ps1` / `update.sh`**: no change (the CLI updates itself). Document this in
  `debugging.md`.
- **Live profile:** add an `agy-live` profile to the Linux dev box megibot's
  `config/harnesses.json` (gitignored), with absolute Linux `cli_path`
  (`~/.local/bin/agy`, expanded) / `log_dir` and a `_comment` in the same style as
  `pi-local`. The Windows-box live profile is deferred to the windows-pending parity
  pass.

## 6.3 Live E2E (`tests/e2e/`)

- `tests/e2e/MATRIX.md`: add an `antigravity` backend column.
- Drive the corpus (`tests/e2e/README.md`, `VERIFY.md`) with
  `PLAN_HARNESS=FIX_HARNESS=REVIEW_HARNESS=agy-live` on the GitHub **and** Azure test
  projects, driven from megibot (Linux). At a minimum, cover: plan → fix → review → PR
  happy path; `/retry` after an induced failure; `/abort` mid-fix (kill path, no
  orphaned `agy` processes — check with `pgrep -g <pgid>`); a mixed config (Claude
  plan → antigravity fix). Windows-box matrix rows stay windows-pending.
- Record findings per `REPORTING.md`. File any bug as an issue tagged `antigravity`,
  each with a `TRANSITIONS` row added when fixed.

## Exit criteria

- [ ] All docs updated, and nothing in `docs/` still calls antigravity a "candidate"
- [ ] Live E2E matrix filled in for both providers
- [ ] Gate green; merge PR `antigravity` → `master`
