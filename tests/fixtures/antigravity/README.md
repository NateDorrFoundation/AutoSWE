# Canonical `agy` stream-json fixtures (Phase 0 spike capture)

Real `agy` 1.5.11 `--output-format stream-json` captures from the megibot BYOK
bridge run (see `docs/autoswe/spike-antigravity.md` for the full record). These
are the golden streams the Phase 1 parser and the Phase 3 `AntigravityFake` are
built against.

The NDJSON events themselves carry no exit-code channel, so the exit code of
the process that produced each fixture is recorded here — this table is the
durable source (the raw `.exit` sidecar captures in `/tmp/agy-spike/raw/` are
ephemeral). Phase 1's subtype mapping keys off `result.status` /
`result.finished_reason` inside the stream; the exit codes below are the
corroborating channel (`print_timeout` is the only case where the exit code
alone distinguishes the subtype: **3**, not 0/1).

| Fixture | Experiment | Process exit code | Stream signature |
|---|---|---|---|
| `basic_success.jsonl` | T1 | 0 | `result.status=SUCCESS` |
| `stdin_single_turn.jsonl` | T2 (stdin transport) | 0 | one turn, then `result` |
| `tool_steps.jsonl` | T3 | 0 | `SUCCESS` after `write_file`+`run_command` |
| `tool_error_recovered.jsonl` | T4 | 0 | `SUCCESS` despite a failed command step |
| `unknown_model.jsonl` | T5 | **1** | single `result` line, **no `init`**, `finished_reason=error` |
| `structured_output.jsonl` | T6 | 0 | `result.structured_output` populated |
| `structured_output_violation.jsonl` | T6 (violation) | 0 | violating value — exit code does **not** flag schema violations; validate downstream |
| `resume.jsonl` | T7 | 0 | same `conversation_id`, `num_turns=2` |
| `killed_partial.jsonl` | T8 | 143 (SIGTERM) | CLI traps `kill -TERM -<pgid>`, last line is `result(finished_reason=interrupted)`; the `.exit` sidecar was lost with the process-group kill, so 143 = 128+SIGTERM from the spawner's view |
| `print_timeout.jsonl` | T9 (`--print-timeout 10s`) | **3** | `finished_reason=print_timeout`, `status=ERROR` |
| `waiting.jsonl` | T10 (`ask_question`) | 0 | headless never pends: `step_type=unknown`, run ends `SUCCESS` |
| `subagent.jsonl` | T14 | 0 | `step_type=subagent` + `subagent_info` |
| `permission_denied.jsonl` | P2b (deny rules) | 0 | denied tool steps end `state=ERROR` + `tool_info.error`, run still `SUCCESS` |

## `result.duration_seconds` is NOT wall-clock on the timeout/kill paths

In `print_timeout.jsonl` the 10 s `--print-timeout` fired after a ~6 s
`duration_seconds`, and in `killed_partial.jsonl` a kill at ~5 s recorded
`duration_seconds: 12.85`. The CLI's field tracks model/tool accounting, not
elapsed time. **Phase 1 must never derive a timeout or kill from
`result.duration_seconds`** — wall-clock is the harness's own
`time.monotonic()` bookkeeping (same rule as the other backends in
`docs/autoswe/harnesses.md`).

## Redaction

Captured in a throwaway clone under `/tmp/agy-spike/` (no user-home paths to
scrub); scanned clean for tokens/emails/`ghp_`/PAT patterns; `usage` blocks
kept verbatim (all-zero counts are real — the bridge reports no usage, so
`cost_usd` stays `None`).
