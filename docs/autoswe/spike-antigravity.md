# Spike — antigravity CLI (`agy`) headless transport (Phase 0)

Decision record for `PLAN-antigravity-backend/phase-0-spike.md`, modelled on
`spike-pi-mcp.md`. **Run: 2026-11-18 (authenticated round) + 2026-11-19
(verification round, every outcome re-checked against the raw captures in
`/tmp/agy-spike/raw/`). Status: ✅ PASS — transport = headless CLI
(`agy --model <m> --settings <json> --print=…` / stdin, `--output-format
stream-json`). Every table row in the Phase 0 plan has a recorded answer; fixtures
are committed under `tests/fixtures/antigravity/`.**

> Round 1 of this spike exited ⛔ AUTH-BLOCKED (Google OAuth is not completable
> headlessly). That exit was superseded the same day by a **first-party BYOK path**
> (custom model provider → local wire bridge): no Google login, no keyring, no
> enterprise gateway. §0.1.2 records the working recipe. The pre-auth measurements
> in §0.1.3 stay because they are real and feed the Phase 1 subtype mapping.

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
  must ship the absolute path or the poller environment must include `~/.local/bin`
  (Phase 2 config docs; same rule as `pi`'s pinned `cli_path`).
- **Working dir:** every experiment ran in a throwaway clone of the test project at
  `/tmp/agy-spike/work` (never the AutoSWE repo). `agy` was invoked with the
  subprocess `cwd` set there (see T12).
- Config dir: `~/.gemini/antigravity-cli/` (`conversations/` now populated by spike
  runs). MCP config `~/.gemini/config/mcp_config.json` holds the spike's
  `autoswe_comment` server (M1–M5); see §0.4.

## §0.1 Install & auth — recorded answers

### 0.1.1 Resolved binary + version

Recorded above: native ELF executable at `~/.local/bin/agy`, version floor **1.5.11**.

### 0.1.2 Auth — the working recipe (BYOK via wire bridge), verbatim

**No Google/OAuth auth and no enterprise gateway is needed.** `agy` speaks the Gemini
`generateContent` wire; a ~200-line Gemini↔OpenAI translation bridge forwards it to
the same LiteLLM gateway the claude-code/codex backends use. Verified end-to-end
2026-11-18:

1. **Wire bridge** — `agy` speaks the Gemini `generateContent` wire. LiteLLM cannot
   serve that wire, so there is a Gemini↔OpenAI translation proxy:
   - Code: `~/.litellm-agy/gemini-openai-proxy.py`, listens `127.0.0.1:4001`,
     forwards to `http://linux-server1:4000/v1` (same LiteLLM key as
     claude-code/codex backends).
   - systemd user service `agy-gemini-proxy` (enabled, `Restart=always`, linger on →
     survives reboots). Streaming tool calls required argument accumulation in the
     bridge (OpenAI deltas → one complete Gemini `functionCall` at `finish_reason`).
2. **Settings** (can be passed per-run via `--settings`, no global mutation):
   ```json
   {"modelProvider":"gemini","customModelsConfig":{"customModels":{"qwen3.8-flash-next":{"modelName":"gemini-api://127.0.0.1:4001/models/qwen3.8-flash-next"}}}}
   ```
   Gotcha: `customModels` is a **map keyed by model id**, not an array — array form
   fails settings validation at `cli_setting_manager.go:52`.
3. **Env per run**: `GEMINI_API_KEY=<any non-empty string>` +
   `GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:4001`.
4. **Invoke**: `agy --model qwen3.8-flash-next --print="<prompt>"` — flag gotchas:
   `--print` consumes the next token as the prompt (attach with `=` or feed stdin);
   `--model` must come **before** `--print`.

Working invocation used for every experiment:

```bash
GEMINI_API_KEY=spike GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:4001 \
  ~/.local/bin/agy --model qwen3.8-flash-next \
  --settings '{"modelProvider":"gemini","customModelsConfig":{"customModels":{"qwen3.8-flash-next":{"modelName":"gemini-api://127.0.0.1:4001/models/qwen3.8-flash-next"}}}}' \
  --print="<prompt>" --output-format stream-json
```

**No-TTY behavior (the poller's mode):** every experiment above ran non-interactively
from a plain bash session with stdin redirected, no TTY — all worked. Auth on this
path is env + inline settings only: **no keyring, no OS credential store involved**,
so `MAX_CONCURRENT > 1` has no auth race and HOME-override isolation (§0.3 P3) cannot
break auth. The `GEMINI_API_KEY`-env-only-vs-`modelProvider`-setting question
(plan §0.1.2) is settled for this path: on the **stock** Gemini provider the docs say
`modelProvider: "gemini"` in `settings.json` is consulted; on the custom-provider BYOK
path used here, **env + inline `--settings` are sufficient** — global
`~/.gemini/antigravity-cli/settings.json` was never mutated (md5-verified unchanged,
`9d2ffda1…`, across all P experiments).

Auth modes not used, recorded for completeness:

| Mode | Result |
|---|---|
| OAuth (browser flow) | **Not completable headlessly** (round 1): consent URL + "paste the authorization code", no device-code flow, no `auth` subcommand. Not needed on the BYOK path. |
| `AGY_LLM_GATEWAY_*` enterprise mode | Exists but requires Google login — **not used**; BYOK is cleaner. |

### 0.1.3 Unauthenticated behavior (recorded pre-auth round 1 — feeds Phase 1 subtype mapping)

Measured on megibot, non-TTY, stdin closed (`</dev/null`), stock config, no BYOK env:

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

- stderr tail: `Error: authentication timed out.`

**Consequences for Phase 1:**
1. The `status: "ERROR"` + `error: "authentication failed or timed out"` +
   `finished_reason: "error"` + exit-code-1 envelope is a **real subtype** the parser
   must map (auth failure → task `error`, not `failed`).
2. A missing-auth stock-config run silently burns **60 s per dispatch**. On the BYOK
   path the analogous fast-fail probe is **bridge reachability** (`GET
   $GOOGLE_GEMINI_BASE_URL/models`), not `agy models` — with custom-provider settings
   active, `agy models` lists models even though the bridge would be down (it then
   fails mid-run with a 400 from the upstream, see P3b). Phase 1 should pre-flight the
   bridge when the backend starts.

### 0.1.4 Quota exhaustion (`useG1Credits: false`)

**Not applicable on the BYOK path** — there is no Google account, no credits, no
overage exposure: all spend lands on the LiteLLM gateway behind the bridge. The
poller-protects-paid-credits concern is satisfied by construction. Recorded as a
windows-pending parity item **only** if a deployment later uses the OAuth/G1-credits
mode; the `useG1Credits:false` shape was not exercised.

## `agy --help` survey (round 1, still accurate on 1.5.11)

| Flag | Help text (verbatim, relevant part) | Confirmed by |
|---|---|---|
| `--settings` | "Settings for this session only, never saved: a JSON object of `settings.json` keys, inline or as a file path" | **P2/P3/P5.** The per-invocation override — absent from the mirrored docs, present in the live binary. Carries `permissions.deny`, the custom-model config, everything. |
| `--input-format` | "(text, stream-json). stream-json reads one NDJSON message per line from stdin and **runs a turn for each**; it requires `--output-format stream-json`" | **T2.** |
| `--output-format` | "(text, json, stream-json), default text" | T1–T14. |
| `--print-timeout` | "Optional time limit for print mode; **0 waits until the turn completes (default 0s)**" | **T9.** Opt-in per invocation — Phase 1 must pass it explicitly or rely on wall-clock kill. |
| `--json-schema` | "JSON schema string or path … to enforce structured output (**for stream-json, only applicable to the final result**)" | **T6** — but see the advisory finding. |
| `--mode` | "(accept-edits, auto, plan)" | **P1: advisory only** — never a security boundary. |
| `--sandbox` | "Run in a sandbox with terminal restrictions enabled" | **P4** (Linux). Windows behavior windows-pending. |
| `--disable-slash-commands` | "Disable slash command and skill expansion in print mode" | **T11.** Must always pass for issue-comment prompts. |
| `--conversation` / `-c, --continue` | Resume by ID / continue most recent | **T7, T8-resume.** |
| `--add-dir` | "Add a directory to the workspace (repeatable)" | Not needed — M3 env inheritance works. |
| `--dangerously-skip-permissions` | "Auto-approve all tool permission requests without prompting" | **T3.** Headless **auto-denies** by default instead of prompting (see P2 note). |
| *(no `-C`)* | No cwd flag in help | **T12:** subprocess `cwd=` is the only lever. |
| `--effort` | "(low\|medium\|high\|xhigh\|max)" | Phase 2 profile field candidate. |
| `agy mcp` | "Manage MCP servers (add, remove, list, enable, disable)" | Phase 5 — config-management subcommand exists. |

## §0.2 Transport experiments (T1–T14)

All shapes below verified from the committed fixtures (`tests/fixtures/antigravity/`)
and the raw captures. Event protocol (documented NDJSON, confirmed): `init` →
`step_update`* → `result`. **`conversation_id` is on `init`** (and echoed on every
`step_update` and `result`).

| # | Experiment (command shape) | Answer | Fixture |
|---|---|---|---|
| T1 | `agy --model m --print="Reply with OK" --output-format stream-json` | Baseline: `init` carries `model`, `cwd`, full `tools` list, `permission_mode`; `result.status=SUCCESS`, `num_turns=1`. **Exit 0.** | `basic_success.jsonl` |
| T2 | `--input-format stream-json --output-format stream-json`, write one `{"event":"user",…}` line, close stdin | **GO.** Runs exactly one turn, `result` then **exit 0** on closed stdin. A ~20 KB prompt is accepted without complaint (second run, SUCCESS, exit 0). **No fallback needed** — stdin is the Phase 1 transport. | `stdin_single_turn.jsonl` |
| T3 | coding task with `--dangerously-skip-permissions` ("create hello.txt, run `git status`") | Tool steps pair **`state:"ACTIVE"` → `"DONE"`** on the same `step_index`. Observed `tool_name` + `tool_info.parameters` keys: create → `write_file` `{Path}`; command → `run_command` `{CommandLine}`; read → `read_file` `{Path}`. **Quirk:** the `init.tools` list uses different names (`write_to_file`, `view_file`) than the observed step `tool_name`s (`write_file`, `read_file`) — Phase 1 must key deny-rules and classification off the **stream** names, and verify against `permissions.deny` vocabulary (which uses the stream names). | `tool_steps.jsonl` |
| T4 | `run_command` on a missing binary, then agent recovers | Non-zero exit of the *command* is not a tool error: the step still ends `DONE` (error surfaces in tool output text, visible to the model). A tool that is **refused** ends `state:"ERROR"` with `tool_info.error: {"type":"TOOL_ERROR","message":"…"}` (see `permission_denied.jsonl`). Final `result.status` stays **SUCCESS** either way — lesson #4 (tool error ≠ run error) holds. | `tool_error_recovered.jsonl` |
| T5 | `--model does-not-exist` | **Exit 1**, single `result` line, **no `init` event**: `status:"ERROR"`, `finished_reason:"error"`, `error:"invalid model selection (--model …): model … is not recognized as a known model or custom model in settings\nAvailable models: …"` (the list includes custom models → doubles as the model pre-flight). stderr has the same text. | `unknown_model.jsonl` |
| T6 | `--json-schema <file>` + multi-step tool use (read file, run command, then answer in schema) | `result.structured_output` **is populated after a multi-step agentic run** (`{"file_exists":true,"first_word":"autoSWE"}`), with extra internal `user_input`/`finish` steps at the end. **But it is advisory:** in the violation run (schema `line_count ≥ 1000`, true value 4) the model **fudged the answer to `1000`** and the run still reported SUCCESS. The harness **must validate `structured_output` downstream** (trust-boundary rule) — the schema does not guarantee truth. | `structured_output.jsonl`, `structured_output_violation.jsonl` |
| T7 | T1, then `--conversation <id>` "what did I ask?" | **Resume works headless.** The `conversation_id` **stays the same**; `step_index` continues from where the prior run left off; `num_turns` increments. | `resume.jsonl` |
| T8 | `run_command sleep 30`, then `kill -TERM -<pgid>` on the process group | The CLI traps SIGTERM and writes a **final `result` line** before dying: `status:"ERROR"`, `error:"interrupted"`, `finished_reason:"interrupted"`; stderr `error: interrupted`. Killing the **process group** leaves no orphans (the `sleep` child dies with it). The conversation is **still resumable** afterward (follow-up `--conversation <id>` run: SUCCESS, and the agent correctly reports the cancelled `sleep 30`). | `killed_partial.jsonl` |
| T9 | `--print-timeout 10s` on a `sleep 25` task | **Exit code 3** (distinct from auth/model errors' 1). `result.status:"ERROR"`, `finished_reason:"print_timeout"`, `error:"print timeout after 10s with the turn still in progress; the response is partial"`. stderr additionally emits a machine-readable line: `AGY_ERROR: {"short_error":…,"status":"DEADLINE_EXCEEDED","retryable":false,"reason":"print_timeout"}` — Phase 1 can key the timeout subtype off `finished_reason:"print_timeout"` and exit 3. | `print_timeout.jsonl` |
| T10 | prompt that makes the agent call `ask_question` | **There is no `WAITING` status in headless print mode.** The question step surfaces as `step_type:"unknown"` and completes immediately with *no selection returned*; the agent continues and the run ends **SUCCESS**. A second run with `ask_question` denied via `--settings permissions.deny` behaved identically (SUCCESS, no hang). So `ask_question` **can** be denied, and headless can never pause on a question — the AskUserQuestion → `autoswe:waiting` semantics are **SDK-only (Phase 7)**. | `waiting.jsonl` |
| T11 | prompt starting with `/plan` text, with vs without `--disable-slash-commands` | Without the flag the CLI **expands** the slash text (93-line run: the agent executed the expanded planning/skill behavior on the repo). With `--disable-slash-commands` no expansion occurs. **Phase 1 must always pass `--disable-slash-commands`** — issue-comment bodies must never be run as CLI slash commands. | — (behavioral) |
| T12 | launch with subprocess `cwd=<repo>`, inspect `init.cwd` | Confirmed: `init.cwd` == the subprocess `cwd`; there is **no `-C` flag**. The subprocess `cwd=` is the only workspace lever (works fine with `create_subprocess_exec(cwd=…)`). | — (in every fixture) |
| T13 | a run that cats a ~5 MB file | The CLI bounds tool-output text itself: **largest single stream line observed ≈ 10.6 KB** (`cat` output is truncated inside the tool result, not echoed raw). The planned **16 MiB `readline` bound is comfortably enough**; no fixture needed. | — |
| T14 | `define_subagent` + `invoke_subagent` | New `step_type:"subagent"`, `tool_name:"invoke_subagent"`, with `step_update.subagent_info.subagents[] = {type_name, role, initial_prompt, conversation_id (on DONE)}`. The subagent's own steps do **not** stream into the parent; only the invoke step ACTIVE→DONE pair is visible, plus the child's `conversation_id` if deep-observation is ever wanted. Progress rendering only needs one extra step_type branch; subagents need **no separate handling** in Phase 1. | `subagent.jsonl` |

## §0.3 Permission / read-only experiments (P1–P5)

**Decision rule from the plan:** *"if P3 finds a per-run lever and P1 or P2 blocks
writes, Phase 4 ships `mode`."* → **Both conditions met. Phase 4 ships `mode`.**

| # | Experiment | Result |
|---|---|---|
| P1 | `--mode plan`, no skip flag, "write a file named x.txt" | **FAIL — advisory only.** `write_file` ran to `DONE`, the file was written, and the agent reported the write; `result.status=SUCCESS`. `--mode plan` changes nothing headless; **it is not a security boundary** and Phase 4 must not rely on it. (It also never soft-denied — no stderr notice.) |
| P2 | default mode (no skip flag) + `permissions.deny: ["write_file(<repo>/)", "command(*)"]` | **PASS (the lever's shape matters).** First attempt used a glob (`write_file(<repo>/*)`) which **did not match** — the write went through (`state:"DONE"`). With a **directory prefix with trailing slash** (`write_file(/abs/repo/dir/)`) the deny bites: the step goes `state:"ERROR"` with `tool_info.error={"type":"TOOL_ERROR","message":"permission check failed for write_file \"…\": Permission denied for write_file(…). Matches user-configured deny rule."}`; `command(ls)` denied the same way; `read_file` stayed **allowed**. Exit 0, status SUCCESS (denied tools ≠ failed run). |
| P3 | per-invocation config override | **Lever found: (c) the live binary's `--settings '<json>'` flag** ("never saved", inline or file path) — carries `permissions.deny` per run, and the global `~/.gemini/antigravity-cli/settings.json` was md5-verified untouched (`9d2ffda1…`) across all P runs. **(b) `HOME=<isolated>` also works** and auth survives trivially (BYOK auth is env-only; nothing lives in the OS keyring on this path) — but without `--settings` inline the isolated HOME loses the custom-model config and runs fall back to stock `gemini-3.1-pro-preview` → upstream 400, so it isolates conversation storage only. **(a) workspace `<repo>/.agents/settings.json`: not needed** — (c) subsumes it; no evidence of a workspace settings file being consulted. **Conclusion: Phase 4 needs no HOME-override isolation; per-run `--settings` is the single lever, race-free under `MAX_CONCURRENT > 1`.** |
| P4 | P2 (deny rules) + `--sandbox` | Same `TOOL_ERROR` denials (writes/commands blocked, exit 0). On Linux the deny rules alone already block; `--sandbox` adds no observable difference on this path (it layers terminal restrictions underneath). Windows sandbox behavior: **windows-pending**. |
| P5 | P2 deny profile **plus** MCP: `mcp_autoswe_comment_post_plan` allowed while `write_file`/`run_command` denied | **PASS.** The MCP tool call went through (returned the server's `suppressed (minimal posting)` ack) while the same run recorded **3 `TOOL_ERROR` denials** for write/command attempts. Read-only + comment-posting is a coherent permission profile for plan/review phases. |

**Headless permission note (from P3b/M4b stderr):** with default permissions and no
skip flag, a tool that would prompt is **auto-denied** in print mode — stderr:
`jetski: no output produced — a tool required the "command" permission that headless
mode cannot prompt for, so it was auto-denied. Add an allow-rule under
permissions.allow …` and the `result` gains `"denied_actions":[{"action":"command","display_name":"Bash"}]`.
The P2b fixture run (exit 0) is the explicit-rule variant. Phase 1's stderr tail and
Phase 4's mode mapping should treat `denied_actions` as a soft signal, not a crash.

**Phase 4 mode mapping (settled):** read-only = no `--dangerously-skip-permissions`,
no `--mode`, per-run `--settings` with
`permissions.deny: ["write_file(<worktree>/)", "replace_file_content(<worktree>/)", …all mutating tools…, "command(*)", "ask_question", "ask_permission", "ask_custom_permission"]`
(stream names per T3) + explicit `permissions.allow` for `view_file`/`read_file`/
`search`/`list_dir` and `mcp(autoswe_comment/*)`. Mutating phases get
`--dangerously-skip-permissions`. **`ask_question`/`ask_permission`/
`ask_custom_permission` are in the tool list and MUST be in the deny set on every
profile** (headless cannot answer them — T10).

## §0.4 MCP experiments (M1–M5)

Server under test: `autoswe_comment` (`/home/<USER>/github/AutoSWE/.venv/bin/python
-m mcp_servers.autoswe_comment_server`) registered in the **global**
`~/.gemini/config/mcp_config.json`. All tool calls were made with
`AUTOSWE_SUPPRESS_POSTING=1` on the `agy` process env (the server returns
`suppressed (minimal posting)` instead of posting) — no real issue comments were made.

| # | Experiment | Answer |
|---|---|---|
| M1 | global config, **no** `eager`, agent asked to post a plan | **Lazy path confirmed:** the agent calls the generic `call_mcp_tool` with `tool_info.parameters = {"ServerName":"autoswe_comment","ToolName":"post_plan","Arguments":{"body":"…"}}`; the tool result appears in `tool_info.output`. Usable, but a one-hop indirection — prefer eager. |
| M2 | global config with `"tools": {"post_plan":{"eager":true}, "post_question":{"eager":true}, "update_progress":{"eager":true}}` | **Eager path confirmed:** the tool is exposed as a **first-class step** with `tool_name` exactly **`mcp_autoswe_comment_post_plan`** (single underscores, as `mcp.md` documents). Phase 5's stream classification keys off this exact string. |
| M3 | per-task `AUTOSWE_*` vars set **only on the `agy` process env**, absent from `mcp_config.json` | **The MCP child inherits `agy`'s process env.** Proof: `AUTOSWE_SUPPRESS_POSTING=1` was exported only on the `agy` process; the child tool returned `suppressed (minimal posting)` (the server reads it from its own env). The pi env-vs-config-hash rule transfers: per-issue identity goes through the subprocess env; the stable config file never needs to change → **no config rewrite, no race**. |
| M4 | workspace `<repo>/.agents/mcp_config.json` with a hostile same-name server | Global and workspace configs **merge**, but on a **name clash the global `autoswe_comment` wins** — the eager global server answered the call (`suppressed` ack, correct name) despite the workspace shadow. **A target repo cannot shadow `autoswe_comment`.** (A workspace file that only *adds* new servers does load.) |
| M5 | first run after deleting the `~/.gemini/antigravity-cli/mcp/` cache | **No warm-up needed** — unlike pi, eager registration in the very first (cold-cache) run produced `mcp_autoswe_comment_post_plan` and succeeded. The cold-start pi dance does not carry over. |

## §0.5 SDK quick-look (feeds Phase 7 — timeboxed)

`google-antigravity` 0.1.23, inspected in the AutoSWE venv (`import google.antigravity`):

- **Subprocess?** `LocalAgentConfig` **does spawn a harness binary** —
  `connections/local/local_connection.py` uses `subprocess.Popen` with
  `_get_default_binary_path_external()` binary discovery ("localharness"). So the
  orphan-risk argument from the plan holds, but it's a killable `Popen` (process-group
  kill works the same as T8), not an in-process task — slightly better than the plan
  assumed. **`LocalOpenAIAgentConfig` talks the OpenAI wire directly** ("any external
  OpenAI-compatible completions API") → **no bridge needed** on that config; it is the
  clean BYOK SDK path.
- **Tool policy / question interception?** The SDK exposes the `can_use_tool`
  analogue the CLI lacks: hooks include **`pre_tool_call_decide` (DecideHook)**,
  `post_tool_call`, `on_tool_error`, `pre_turn`, `post_turn`, `stop`,
  `on_interaction`, plus a `policy/` module. A `DecideHook` on `ask_question` +
  `StopHook` is the plausibly-complete recipe for AskUserQuestion → pause semantics;
  end-of-run-from-handler was **not exercised in the timebox** — verify first thing in
  Phase 7.
- **Usage / observability?** `UsageMetadata` is a first-class type and `post_turn`
  exists → per-turn usage and tool events are observable. `response_schema` on
  `AgentConfig` (validated and stringified into `finish_tool_schema_json`) parallels
  `--json-schema`.

**Phase 7 verdict: worth doing** (GO) — the SDK is the only surface that can produce
`autoswe:waiting`, needs no wire bridge on the OpenAI config, and its process model is
no worse than the plan feared. Not blocking for Phases 1–6.

## Go / no-go summary

| Decision | Verdict | Evidence |
|---|---|---|
| **T2 stdin transport (load-bearing)** | **GO** | One turn, `result`, exit 0 on closed stdin; ~20 KB prompt fine. Fallback (`-p` + bootstrap `view_file` of a temp prompt file) **not needed**. `stdin_single_turn.jsonl` |
| **`mode` / read-only (P1–P3)** | **GO — via per-run `--settings`, not `--mode`** | `--mode plan` is advisory (P1 FAIL). `--settings` per-run `permissions.deny` with **directory-prefix** rules blocks writes+commands, allows reads (P2/P2b), survives concurrent use (global settings untouched), and composes with MCP allow (P5). Phase 4 ships `mode`. `permission_denied.jsonl` |
| **`mcp` naming + env (M2/M3)** | **GO** | Eager `tool_name` is exactly `mcp_autoswe_comment_post_plan`; per-task env **inherits** to the MCP child; global config cannot be shadowed by the target repo; no cold-cache warm-up. |
| **`structured_output` (T6)** | **GO, with mandatory downstream validation** | Populated on `result` after multi-step tool use — but **advisory**: the model fudged a violating value to satisfy the schema and still reported SUCCESS. Treat `structured_output` as LLM output per the trust-boundary rule. |
| Auth | **SOLVED (BYOK bridge)** | §0.1.2 recipe; no OAuth, no keyring, no TTY dependency. |

## Fixtures committed (`tests/fixtures/antigravity/`)

`basic_success` (T1) · `stdin_single_turn` (T2) · `tool_steps` (T3) ·
`tool_error_recovered` (T4) · `unknown_model` (T5) · `structured_output` +
`structured_output_violation` (T6) · `resume` (T7) · `killed_partial` (T8) ·
`print_timeout` (T9) · `waiting` (T10) · `subagent` (T14) · `permission_denied`
(P2b/P3).

**Redaction:** captured with the per-experiment runner in `/tmp/agy-spike/run.sh` and
scrubbed like `scripts/capture_scenario.py` — the runs happened inside `/tmp/agy-spike`
so no absolute user-home paths occur; a token/email/home-path/`AUTOSWE_*`-secret scan
over the committed files returns nothing; `usage` blocks are kept verbatim (all-zero
token counts are real on this bridge path — the bridge does not report usage;
`cost_usd` stays `None`, lesson #7).

## Deferred / windows-pending parity items

- Windows: `(Get-Command agy).Source` `.exe`/`.cmd`/`.ps1` resolution branch;
  `taskkill /T /F` tree-kill equivalence (T8 used `kill -TERM -<pgid>`);
  `--sandbox` OS-level behavior on Windows (P4); Windows Credential Manager auth mode
  (not used on the BYOK path); `useG1Credits` quota shape (0.1.4, only if a
  deployment ever uses the OAuth mode).
- Phase 7 pre-check: SDK `DecideHook` → clean end-of-run (handler ending the run) was
  not exercised in the Phase 0 timebox.
