# Phase 2 — Config & factory

**Goal:** an `antigravity` profile in `config/harnesses.json` resolves to
`AntigravityBackend` for any phase. Missing or invalid fields fail at startup, not
mid-dispatch.

## 2.1 `autoswe/core/config.py`

- `KNOWN_BACKENDS = {"claude_code", "codex", "pi", "antigravity"}` (line ~333).
- In `load_harnesses_config()`, add an antigravity-specific validation block after the
  `max_turns` block. It only runs when `backend == "antigravity"`:
  - `effort`, when present, must be one of `{"low","medium","high","xhigh","max"}` → `ValueError`
    naming the entry. (Which values a model accepts is checked by the CLI at run time.
    This check only catches typos.)
  - `skip_permissions` / `sandbox` must be booleans when present (coerce `"true"/"false"` strings
    from `${VAR}` expansion the way `bypass_approvals` is handled for codex. Reuse that helper
    if it exists, otherwise add `_coerce_bool`).
  - Any `cli_path` / `log_dir` containing a literal `~` → expand with `os.path.expanduser`. The
    `pi-local` comment in `harnesses.json` records the bug where `~` reached a subprocess
    unexpanded.
- `resolve_harness()` needs no change. The profile passes straight through.
- `resolve_max_turns()` needs no change. It is a documented no-op for this backend.

## 2.2 `autoswe/harness/backends/factory.py`

Add the branch after `pi`, in the same style:

```python
if backend_name == "antigravity":
    # agy falls back to the account's last-selected model when --model is
    # omitted — non-reproducible, so a profile must name one (codex/pi contract).
    model = str(harness_cfg.get("model") or "").strip()
    if not model:
        raise ValueError(
            "antigravity harness profile is missing required 'model'. "
            "Set it to an `agy models` slug, e.g. 'gemini-4-neon-high', "
            "'gemini-3.8-flash-medium', or 'Orchestrator (Neon)'."
        )
    # Deferred import: only loads the antigravity submodule when such a profile is configured.
    from autoswe.harness.backends.antigravity import AntigravityBackend

    return AntigravityBackend()
```

Update the docstring examples and the final `Supported backends:` message.

Optionally warn (don't fail) when `model` starts with `Orchestrator` and `effort` is
set. `models.md` says effort does not apply to Orchestrator, and the CLI reports
it as not adjustable.

## 2.3 Profile fields

| Field | Required | Default | Maps to |
|---|---|---|---|
| `backend` | **yes** | — | `"antigravity"` |
| `model` | **yes** | — | `--model` (an `agy models` slug, or a quoted display name for Orchestrator) |
| `effort` | no | — | `--effort` |
| `agent` | no | — | `--agent` |
| `cli_path` | no | `shutil.which("agy")` | executable (`.cmd`/`.ps1` handled) |
| `timeout` | no | `AGENT_TIMEOUT` | wall clock + `--print-timeout` |
| `gemini_api_key` | no | — | `GEMINI_API_KEY` env (plus the `modelProvider` note from Phase 0 §0.1) |
| `skip_permissions` | no | `true` | `--dangerously-skip-permissions` (read_write only after Phase 4) |
| `sandbox` | no | `false` | `--sandbox` |
| `log_dir` | no | — | `--log-file <log_dir>/<id>.log` |
| `config_home` | no | — | **only if Phase 0 P3(b) worked:** isolated home for settings/MCP (Phase 4/5) |
| `max_turns` | no | — | accepted, no-op |
| `env` | no | — | merged into the subprocess env |

**PATH caveat (Phase 0 §Environment):** on megibot `agy` is **not** on the default
non-login shell PATH (`command -v agy` → not found; only `~/.local/bin/agy` works), so
`shutil.which("agy")` fails under cron/systemd. Live profiles must pin `cli_path` to
the absolute resolved path — same rule as pi's pinned `cli_path`.

Do **not** add `system_prompt`. The CLI has no documented system-prompt flag. Steering
goes through prompt templates, as with Codex. `docs/antigravity/rules.md` (AGENTS.md-style
rules) loads from the target repo by design. Note this in `safeguards.md` in Phase 6.

## 2.4 `config/harnesses.json.example`

Add three commented profiles:

```json
"agy-neon": {
  "_comment": "Antigravity CLI headless (agy). Auth: run `agy` once interactively on this host (keyring), or set gemini_api_key. Set useG1Credits=false in ~/.gemini/antigravity-cli/settings.json to avoid overage billing.",
  "backend": "antigravity",
  "model": "gemini-4-neon-high",
  "timeout": 3600
},
"agy-flash": {
  "backend": "antigravity",
  "model": "gemini-3.8-flash-medium",
  "gemini_api_key": "${GEMINI_API_KEY}"
},
"agy-orchestrator": {
  "_comment": "Planner+Hydrogen pair in one run; --effort does not apply.",
  "backend": "antigravity",
  "model": "Orchestrator (Neon)"
}
```

## 2.5 CLI surface

`autoswe/cli.py` needs no new subcommand in this phase. If Phase 0 M5 shows a
warm-up is needed, Phase 5 extends `_cmd_warmup` (it is currently pi-only;
see `cli.py:131`).

## 2.6 Tests in this phase

- `tests/test_config.py`: `antigravity` is accepted; unknown backend message lists it;
  bad `effort` → `ValueError`; `skip_permissions: "false"` string → `False`; `~` expansion
  on `cli_path`/`log_dir`; `${GEMINI_API_KEY}` interpolation.
- `tests/test_backend_factory.py`: `{"backend":"antigravity","model":"m"}` → `AntigravityBackend`;
  missing/blank `model` → `ValueError` (message mentions `agy models`); case-insensitive
  `"Antigravity"`; deferred import (module not in `sys.modules` after resolving a
  `claude_code` profile).

## Exit criteria

- [ ] `python autoswe.py queue list` starts cleanly with an `antigravity` profile in `harnesses.json`
- [ ] Gate green
