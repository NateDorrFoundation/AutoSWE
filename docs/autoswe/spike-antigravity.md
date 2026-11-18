# Spike — antigravity CLI (`agy`) headless transport (Phase 0)

Decision record for `PLAN-antigravity-backend/phase-0-spike.md`, modelled on
`spike-pi-mcp.md`. **Run: 2026-11-18. Status: ⛔ AUTH-BLOCKED — stopped after §0.1
per the host-decision order of attack (#317).**

> **AUTH-BLOCKED: escalate for `GEMINI_API_KEY`** (or one interactive OAuth login on
> megibot — see "To unblock" at the bottom). All of §0.2–§0.5 (transport T1–T14,
> permissions P1–P5, MCP M1–M5, SDK quick-look) require an authenticated run and are
> **deferred, not answered**. No fixture under `tests/fixtures/antigravity/` was
> captured; nothing may be committed there until the spike is re-run with auth.
> Every capability-matrix "(if confirmed)" item in the plan README stays as-is —
> neither struck out nor promoted.

## Environment

- **Host:** Linux dev box `megibot` (host decision 2026-11-18, Nate — #317 comment).
  Windows rows from the original plan are **windows-pending**: no Windows host was
  used; verification deferred to the Phase 6 parity pass.
- **agy:** `1.5.11` (`agy --version` → exactly `1.5.11`, one line, no prefix).
  This is the Phase 6 CLI floor unless a later spike run upgrades.
- **Binary:** `/home/<USER>/.local/bin/agy` — `ELF 64-bit LSB pie executable, x86-64,
  stripped` (single ~256 MB self-contained binary). **This answers the Phase 1
  `_resolve_agy_executable` question for Linux:** it is a native executable, not a
  script/shim, so `asyncio.create_subprocess_exec` can exec the resolved path
  directly with no `shell=True` — same branch shape as the codex/pi resolution.
  The Windows `.exe`/`.cmd`/`.ps1` question remains windows-pending.
- **PATH caveat:** `agy` is **not** on the default non-login shell PATH
  (`command -v agy` → not found; only `~/.local/bin/agy` works). The harness profile
  must either ship the absolute path or the poller environment must include
  `~/.local/bin`. Record this for Phase 2 config docs.
- Config dir: `~/.gemini/antigravity-cli/` exists (`conversations/` empty — zero
  conversations ever run authenticated). MCP config `~/.gemini/config/mcp_config.json`
  exists and is **empty (0 bytes)**. No OAuth token/credential file exists anywhere
  under `~/.gemini` (searched `*oauth*`, `*credential*`, `*token*`).

## §0.1 Install & auth — recorded answers

### 0.1.1 Resolved binary + version

Recorded above: native ELF executable, version floor **1.5.11**.

### 0.1.2 Auth modes

| Mode | Result |
|---|---|
| OAuth (browser flow) | **Blocked headlessly.** `agy -p` prints the consent URL, waits, and offers "paste the authorization code here" on stdin. There is **no device-code flow and no `auth` subcommand** (`agy --help` subcommands: `acp agent agents changelog help install mcp mic-serve models plugin plugins remote-control update` — nothing for login). Completing the flow needs a human with a Google account in a browser. `~/.local/bin/agy models` confirms the only sign-in path is interactive: *"Please sign in to view available models. Launch the CLI without arguments to sign in."* |
| `GEMINI_API_KEY` env | **Not testable:** no `GEMINI_API_KEY` in the environment, and none in any config under `~/.gemini`. Whether env-only suffices vs. also needing `"modelProvider": "gemini"` in `~/.gemini/antigravity-cli/settings.json` (per `cli-reference.md:193`) remains **open**. |
| Keyring | A `gnome-keyring-daemon --components=pkcs11,secrets` **is running** on the box, but there are no antigravity credentials in it (none stored). Keyring availability is therefore not the blocker — the login itself is. |

### 0.1.3 Unauthenticated behavior (this feeds the Phase 1 subtype mapping)

Measured twice on megibot, non-TTY, stdin closed (`</dev/null`), inside a throwaway
dir (`/tmp/agy-spike`):

```
agy -p "Reply with OK" --output-format stream-json    # stdin closed, no TTY
```

- The CLI **does not exit immediately and does not hang forever**: it prints the
  consent URL to **stderr**, blocks exactly **60 s** on its own auth timeout
  ("Waiting for authentication (timeout 60s)..." — closed stdin is *not* treated as
  an immediate auth failure), then exits. **Exit code: 1.**
- stdout (stream-json) gets exactly one final line — no `init` event is emitted:

```json
{"event":"result","result":{"conversation_id":"","status":"ERROR","response":"","error":"authentication failed or timed out","duration_seconds":0,"num_turns":0,"usage":{"input_tokens":0,"output_tokens":0,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":0},"finished_reason":"error"}}
```

- stderr tail: `Error: authentication timed out.` / `error: authentication failed or
  timed out`.

**Consequences for Phase 1:**
1. The `status: "ERROR"` + `error: "authentication failed or timed out"` +
   `finished_reason: "error"` + exit-code-1 envelope is a **real subtype** the parser
   must map (auth failure → task `error`, not `failed`).
2. A missing-auth run silently burns **60 s per dispatch**. Phase 1 should pre-flight
   auth with `agy models` (fails **fast**, exit 1, "Please sign in…" on stderr, no
   blocking) when the backend starts, so the poller never eats 60 s × N tasks.

### 0.1.4 Quota exhaustion (`useG1Credits: false`)

**Deferred** — requires an authenticated account. Recorded as an open Phase 0 item.

## `agy --help` survey (no auth needed — evidence for Phases 1/4/5)

Captured from the live 1.5.11 binary 2026-11-18. These are **surface-level**
confirmations; every behavioral claim still needs an authenticated experiment.

| Flag | Help text (verbatim, relevant part) | Spike/phase relevance |
|---|---|---|
| `--settings` | "Settings for this session only, never saved: a JSON object of `settings.json` keys, inline or as a file path" | **Found.** The P3(c) per-invocation override — absent from the mirrored docs, present in the live binary. If it accepts `permissions.deny`, Phase 4 may not need HOME-override isolation at all. **Runtime verification deferred** (needs auth). |
| `--input-format` | "(text, stream-json). stream-json reads one NDJSON message per line from stdin and **runs a turn for each**; it requires `--output-format stream-json`" | T2 transport is the CLI's documented shape. The load-bearing questions (exactly one turn + exit 0 after `result` on closed stdin; ~20 KB prompt) remain **untested**. |
| `--output-format` | "(text, json, stream-json), default text" | T1–T14 shapes. |
| `--print-timeout` | "Optional time limit for print mode; **0 waits until the turn completes (default 0s)**" | T9: the timeout is opt-in per invocation — Phase 1 must pass it explicitly or rely on wall-clock kill. |
| `--json-schema` | "JSON schema string or path … to enforce structured output (**for stream-json, only applicable to the final result**)" | T6: the docs pre-announce that structured output lands on the final `result` only. Whether it survives multi-step tool use is still the fixture question. |
| `--mode` | "(accept-edits, auto, plan)" | P1 lever exists at the CLI surface. Enforcement question open. |
| `--sandbox` | "Run in a sandbox with terminal restrictions enabled" | P4 lever exists (Linux). Windows behavior windows-pending. |
| `--disable-slash-commands` | "Disable slash command and skill expansion in print mode" | T11 lever exists. |
| `--conversation` / `-c, --continue` | Resume by ID / continue most recent | T7 lever exists. |
| `--add-dir` | "Add a directory to the workspace (repeatable)" | M3 fallback lever (per-run workspace). |
| `--dangerously-skip-permissions` | "Auto-approve all tool permission requests without prompting" | T3 lever. Note the wording: headless **prompts** by default — relevant to P1/P2 soft-deny question. |
| *(no `-C`)* | No cwd flag in help | T12's premise confirmed at surface level: subprocess `cwd=` is the only lever. |
| `--effort` | "(low\|medium\|high\|xhigh\|max)" | Phase 2 profile field candidate. |
| `agy mcp` | "Manage MCP servers (add, remove, list, enable, disable)" | Phase 5: there is a config-management subcommand, not only file editing. |
| `agy models` | lists models; **fails fast when unauthenticated** | The auth pre-flight probe (see §0.1.3). |

## Go / no-go summary

| Decision | Verdict | Why |
|---|---|---|
| T2 stdin transport (load-bearing) | **UNDECIDED — blocked on auth** | CLI surface matches the plan (`--input-format stream-json` exists with the documented single-NDJSON-turn semantics), but exit-after-`result` and prompt-size behavior are untested. Fallback remains recorded in the plan: `-p` with a bootstrap prompt pointing the agent at a temp prompt file via `view_file`. |
| `mode` / read-only (P1–P3) | **UNDECIDED — blocked on auth** | `--mode plan`, `--sandbox`, and the newly-found `--settings` all exist; none verified to block writes. |
| `mcp` naming + env (M2/M3) | **UNDECIDED — blocked on auth** | `mcp_config.json` present (empty), `agy mcp` subcommand exists; lazy/eager naming untested. |
| `structured_output` (T6) | **UNDECIDED — blocked on auth** | Help text confirms final-result-only semantics; multi-step survival untested. |

## Deferred items (all require authenticated `agy`)

- Fixtures `tests/fixtures/antigravity/*.jsonl` (T1–T10, T14) — **none captured**.
- Experiments T11–T13, P1–P5, M1–M5, §0.5 SDK quick-look.
- 0.1.2 GEMINI_API_KEY env-only-vs-settings question; 0.1.4 quota-exhaustion shape.
- All Windows rows: windows-pending per host decision.
- README capability-matrix updates: **not touched** — no item has a confirmed answer
  to promote or a failure to strike.

## To unblock

Any one of these, then re-run `/retry` (or a fresh `/fix`) on this issue:

1. **`GEMINI_API_KEY`** on megibot (exported for the poller or written per plan) +
   confirm the `modelProvider` question while at it. Fastest path; matches the
   "escalate for GEMINI_API_KEY" instruction.
2. **One interactive OAuth login** by whoever owns the Google account: run
   `~/.local/bin/agy` (no args) in a terminal on megibot that can reach a browser —
   e.g. from the GNOME session with `DISPLAY` set (the keyring daemon is already
   running, so the token should persist in the OS keyring). After that, every
   headless `agy -p` run inherits the keyring auth — exactly the no-TTY mode the
   poller needs. This also settles the keyring half of 0.1.2 for free.
