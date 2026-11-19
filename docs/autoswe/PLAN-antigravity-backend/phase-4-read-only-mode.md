# Phase 4 — Read-only enforcement (`mode` capability)

**Goal:** plan and review phases on an Antigravity profile are **enforced** read-only
at the CLI level. Then `has_read_only_enforcement()` is True, and those phases
keep their guarantees without leaning only on the post-run
`ensure_worktree_unchanged` rollback (the pi outcome, not the Codex one).

**Gate:** only start once Phase 0 §0.3 has recorded a **per-run** lever (P3) and a
blocking mechanism (P1 and/or P2). If it hasn't, close this phase as "won't do".
Document the degrade in `harnesses.md` (as for Codex issue #166) and keep `mode`
unadvertised.

## 4.1 Why the obvious approach is wrong

Permission rules live in `~/.gemini/antigravity-cli/settings.json`, which is a
**host-global** file. If autoSWE wrote deny rules into it before a plan run and
removed them afterwards, it would race against a concurrent `/fix` (with
`MAX_CONCURRENT > 1`) on another worktree. It would also leave the host
locked down if the poller crashed mid-run. **Do not mutate the global settings
per run.**

## 4.2 Mechanism (pick based on the Phase 0 P3 result)

**Option A: per-run isolated config home** (if P3(b) worked: overriding
`USERPROFILE`/`HOME` keeps keyring auth):
- Profile field `config_home` (default: `<autoswe data dir>/agy-home/<profile name>`).
- Per run, build `<config_home>/runs/<uuid>/.gemini/antigravity-cli/settings.json`
  from a base (`config_home/base-settings.json` if present, else `{}`), merged with
  the mode-derived `permissions` block (§4.3). Point `HOME`/`USERPROFILE` at it on
  the subprocess env only. Delete the run dir in `finally`.
- Stable things (`useG1Credits`, `modelProvider`, and the Phase 5 MCP config) live in
  the base file and are copied in.

**Option B: workspace-level settings** (if P3(a) found `<workspace>/.agents/settings.json`
or similar):
- **Not** in the target worktree (that pollutes the repo under test and is visible to
  `git status`). Only viable if the CLI accepts an extra workspace via `--add-dir`
  pointing at a run-scoped directory outside the repo, *and* that directory's settings
  are honored. Treat this option as unlikely.

**Option C: a CLI flag** (if `agy --help` on the live binary shows a settings/config-dir
flag): use it directly. This is the simplest option and wins if it exists.

Whichever option is chosen, put it behind one helper:
`_stage_run_config(spec, harness_cfg) -> (env_patch: dict, cleanup: Callable)`.
Then `_run_async` stays transport-agnostic, and the tests patch one seam.

## 4.3 Mode → flags + rules mapping

| `RunSpec.mode` | Flags | `permissions` block (staged per run) |
|---|---|---|
| `plan` | `--mode plan`, **no** `--dangerously-skip-permissions` | `deny: ["write_file(<cwd>)", "write_file(<cwd>/.git/)", "command(*)"]`, `allow: ["read_file(<cwd>)", "mcp(autoswe_comment/*)"]` |
| `read_only` | default mode, **no** skip flag | same as `plan` (+ `mcp(autoswe_inline_comment/*)` for review) |
| `read_write` / unset | `--dangerously-skip-permissions` if `skip_permissions` (default true) | `deny: ["write_file(<cwd>/.git/)"]` belt-and-braces; nothing else |

- The rule targets are **absolute worktree paths** (`spec.cwd`). On Windows, apply
  the `regex:` guidance from `permissions.md` for command matching, and confirm the
  path-rule semantics with Phase 0 P2.
- `command(*)` deny on read-only phases: the planner and reviewer don't need a shell.
  If the review prompt depends on `git diff`/`git log`, switch to
  `allow: ["command(regex:git (diff|log|show|status)( .*)?)"]` *before* the deny
  (Deny > Ask > Allow precedence, so use a narrower deny instead of `command(*)`).
  Decide this by reading `autoswe/harness/prompts.py` review template, and record
  the decision.
- `spec.extra_tools` / `spec.disallowed_tools_override`: map the Claude-style names
  that handlers pass today onto Antigravity rules where a clean equivalent exists.
  Otherwise log and ignore them. Keep the mapping table in the module, as pi does.
- `ask_question`: deny it in **every** mode if Phase 0 T10 showed it can be denied.
  Otherwise rely on the `WAITING` → `error` mapping from Phase 1.
- `--sandbox`: if Phase 0 P4 shows OS-level blocking works on Windows, emit it on
  read-only phases by default (profile `sandbox` still forces it on for all phases).

## 4.4 Enforcement self-check

After parsing `init`, compare `init.permission_mode` with the expected value
(`request-review` for read-only, `always-proceed` for read_write+skip). On a mismatch
on a read-only phase, log `[ERROR][ANTIGRAVITY] read-only not in effect` and **set
`error_flag`**. A plan run that silently has write access is worse than a failed
plan run. `ensure_worktree_unchanged` still runs regardless.

Soft-deny notices on stderr during a read-only run are **expected**, so log them at
info level. During a `read_write` run they mean the skip flag isn't working, so
they stay at warning level (Phase 1).

## 4.5 Capabilities

`capabilities()` gains `"mode"`. `has_read_only_enforcement` becomes True, and the
planner/reviewer loud-degrade warnings stop for this backend.
`ensure_worktree_unchanged` still runs. It is unconditional in the handlers, and
it stays as the backstop.

## 4.6 Tests

- `test_antigravity_backend.py`: the mode → flags/rules table (each row); the staged
  settings file contents (absolute paths, rule order); cleanup on success, failure, and
  kill; base-settings merge preserves unrelated keys; the `permission_mode` mismatch
  → `error`; env patch reaches the subprocess only (never `os.environ`); two concurrent
  runs get distinct run dirs.
- `test_backend_capabilities.py`: `mode` present → `has_read_only_enforcement` True.
- `test_planner_readonly.py`: add an antigravity case. The planner passes `mode="plan"`
  and does **not** log the degrade warning.
- `transitions.py`: change the "read-only violation rolled back" row so it expects the
  violation to be blocked (the fake emits a soft-denied write and the tree stays
  clean), and add a new row for the `permission_mode` mismatch → failed.

## Exit criteria

- [ ] Live check on the dedicated box: a `/plan` on the test project with a prompt that
      tries to edit a file leaves `git status` clean **and** the log shows a soft-deny
      (not a rollback)
- [ ] `harnesses.md` mode table written (Phase 6 folds it in)
- [ ] Gate green
