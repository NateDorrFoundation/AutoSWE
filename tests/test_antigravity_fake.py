"""Fidelity guard — pins AntigravityFake to the real AntigravityBackend parser.

Feeds AntigravityFake-emitted ``stream-json`` lines through the real
``AntigravityBackend.run`` and asserts the resulting ``RunResult`` (text,
session id, subtype, structured_output) and the recorded argv — the same
contract as ``test_pi_fake.py`` / ``test_codex_fake.py``.

Two anti-drift layers, both required by Phase 3 §3.2:

1. **Key-set provenance**: every line the fake emits has a flattened key set
   ⊆ the union of the keys observed in the Phase 0 fixtures under
   ``tests/fixtures/antigravity/`` (per ``event``, and for ``step_update``
   further per ``step_type``).  The fake may not hand-invent protocol fields.
2. **Golden replay**: every fixture file parses through the real
   ``_parse_line`` / ``_finalize`` without a single ``[WARN]`` and with the
   expected terminal subtype.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from autoswe.harness.backends import antigravity as agy_mod
from autoswe.harness.backends.antigravity import (
    AntigravityBackend,
    _AgyAccumulator,
    _finalize,
    _parse_line,
)
from autoswe.harness.backends.base import RunSpec
from tests.fakes.antigravity_fake import (
    AntigravityFake,
    _config_error_stream,
    _error_stream,
    _killed_rc_stream,
    _killed_stream,
    _step_tool_active,
    _step_tool_done,
    _success_stream,
    _waiting_stream,
)

MODEL = "gemini-test"
FIXTURES = Path(__file__).parent / "fixtures" / "antigravity"

# The structured schema from structured_output.jsonl — reused verbatim so the
# json_schema echo test rides a *fixture* shape, not an invented one.
_FIXTURE_SCHEMA = {
    "type": "object",
    "properties": {
        "file_exists": {"type": "boolean"},
        "first_word": {"type": "string"},
    },
    "required": ["file_exists", "first_word"],
    "additionalProperties": False,
}


def _spec(**overrides) -> RunSpec:
    defaults = {
        "prompt": "test prompt",
        "cwd": "/tmp",
        "model": MODEL,
        "mode": "read_write",
        "timeout": 30,
        "state": {"_harness_cfg": {"backend": "antigravity", "model": MODEL}},
    }
    defaults.update(overrides)
    return RunSpec(**defaults)


def _run(spec, fake):
    async def _inner():
        with fake:
            return await AntigravityBackend().run(spec)

    return asyncio.run(_inner())


# ---------------------------------------------------------------------------
# 1. Builder round-trips through the REAL backend → RunResult
# ---------------------------------------------------------------------------


class TestAntigravityFakeFidelity:
    """Feed AntigravityFake stream-json through the real parser → RunResult."""

    def test_success_run_result(self):
        """script_response success → text, session_id, subtype=success."""
        fake = AntigravityFake()
        fake.script_response("Changed 3 files.", session_id="conv-abc")

        result = _run(_spec(), fake)

        assert result.text == "Changed 3 files.\n"
        assert result.session_id == "conv-abc"
        assert result.subtype == "success"
        assert result.ok is True

    def test_cost_usd_always_none(self):
        """The stream carries tokens only — cost_usd is NEVER estimated."""
        fake = AntigravityFake()
        fake.script_response("ok")

        result = _run(_spec(), fake)

        assert result.cost_usd is None

        # And every flag the backend does not set stays at its Phase 1 default.
    def test_phase1_flags_never_set(self):
        fake = AntigravityFake()
        fake.script_response("ok")

        result = _run(_spec(), fake)

        assert result.plan_posted is False
        assert result.question_posted is False
        assert result.plan_file_path is None
        assert result.structured_output is None

    def test_plan_tags_in_text(self):
        """script_plan wraps the plan in AUTOSWE_PLAN tags for the planner."""
        fake = AntigravityFake()
        fake.script_plan("1. Fix login\n2. Add tests", session_id="conv-plan")

        result = _run(_spec(), fake)

        assert "<AUTOSWE_PLAN>" in result.text
        assert "Fix login" in result.text
        assert result.subtype == "success"

    def test_questions_tags_in_text(self):
        """script_questions preserves AUTOSWE_QUESTIONS tags in the text."""
        fake = AntigravityFake()
        fake.script_questions("Which framework do you prefer?")

        result = _run(_spec(), fake)

        assert "<AUTOSWE_QUESTIONS>" in result.text
        assert "Which framework do you prefer?" in result.text
        assert result.subtype == "success"

    def test_questions_mcp_off_by_default(self):
        """mcp=False (the text-fallback default) emits NO tool step."""
        fake = AntigravityFake()
        fake.script_questions("Which DB?", session_id="conv-q")

        lines: list[str] = []
        _run(_spec(progress_callback=lines.append), fake)

        assert not any("post_question" in ln for ln in lines)

    def test_questions_mcp_on_emits_eager_step_and_sets_flag(self):
        """Phase 5 switch ON: the eager post_question step streams under the
        agy spelling, and the backend now advertises "mcp" — so the parser
        DOES set question_posted (the text tags are gone: the question is on
        the thread via MCP)."""
        fake = AntigravityFake()
        fake.script_questions("Which DB?", session_id="conv-q", mcp=True)

        lines: list[str] = []
        result = _run(_spec(progress_callback=lines.append), fake)

        assert any("mcp_autoswe_comment_post_question" in ln for ln in lines), (
            f"mcp=True must stream the eager post_question step; got {lines!r}"
        )
        assert result.question_posted is True

    def test_questions_mcp_lazy_emits_proxy_and_sets_flag(self):
        """mcp="lazy" scripts the call_mcp_tool proxy shape; the parser's
        fallback branch still classifies it."""
        fake = AntigravityFake()
        fake.script_questions("Which DB?", session_id="conv-q", mcp="lazy")

        lines: list[str] = []
        result = _run(_spec(progress_callback=lines.append), fake)

        assert any("call_mcp_tool" in ln for ln in lines), lines
        assert result.question_posted is True

    def test_plan_mcp_on_sets_plan_posted_with_body(self):
        """script_plan(mcp=True): eager post_plan pair (shared step_index),
        plan_posted + plan_posted_body captured for the #241 re-push."""
        fake = AntigravityFake()
        fake.script_plan("Step 1: do the thing", session_id="conv-p", mcp=True)

        result = _run(_spec(), fake)

        assert result.plan_posted is True
        assert result.plan_posted_body == "Step 1: do the thing"
        assert "<AUTOSWE_PLAN>" not in result.text

    def test_plan_mcp_error_unsets_the_flag(self):
        """mcp_error=True: terminal step carries tool_info.error — §5.3 says
        the parser must un-set plan_posted so the text fallback stays live."""
        fake = AntigravityFake()
        fake.script_plan("never posted", session_id="conv-e",
                         mcp=True, mcp_error=True)

        result = _run(_spec(), fake)

        assert result.plan_posted is False
        assert result.plan_posted_body is None

    def test_fix_progress_labels(self):
        """script_fix(files=...) surfaces per-file progress via the callback."""
        fake = AntigravityFake()
        fake.script_fix("Applied the patch.", files=("src/a.py", "src/b.py"))

        lines: list[str] = []
        result = _run(_spec(progress_callback=lines.append), fake)

        assert result.subtype == "success"
        assert any(ln == "Tool: write src/a.py" for ln in lines), lines
        assert any(ln == "Tool: write src/b.py" for ln in lines), lines

    def test_fail_run_result(self):
        """script_fail → in-stream ERROR result → subtype=error, session kept."""
        fake = AntigravityFake()
        fake.script_fail("quota exceeded", session_id="conv-err")

        result = _run(_spec(), fake)

        assert result.subtype == "error"
        assert result.ok is False
        assert result.session_id == "conv-err"

    def test_killed_run_result(self):
        """script_killed → interrupted result → subtype=killed."""
        fake = AntigravityFake()
        fake.script_killed(session_id="conv-kill", partial_text="halfway")

        result = _run(_spec(), fake)

        assert result.subtype == "killed"
        assert result.session_id == "conv-kill"

    def test_killed_rc_run_result(self):
        """script_killed_rc → no result event + rc -9 → killed; text comes
        from the accumulated text_delta fallback."""
        fake = AntigravityFake()
        fake.script_killed_rc(session_id="conv-9", partial_text="partial text")

        result = _run(_spec(), fake)

        assert result.subtype == "killed"
        assert result.session_id == "conv-9"
        assert "partial text" in result.text

    def test_config_error_run_result(self):
        """script_config_error → pattern-matched subtype=config_error, and
        RunResult.text carries the CLI's own message (the FAILED comment)."""
        fake = AntigravityFake()
        fake.script_config_error(session_id="conv-cfg")

        result = _run(_spec(), fake)

        assert result.subtype == "config_error"
        assert "not recognized as a known model" in result.text
        assert result.session_id == "conv-cfg"

    def test_waiting_status_maps_to_error(self):
        """status="WAITING" (Phase 7 flow) is a plain error in Phase 1."""
        fake = AntigravityFake()
        fake.script_response("awaiting input", status="WAITING")

        result = _run(_spec(), fake)

        assert result.subtype == "error"

    def test_structured_output_run_result(self):
        """script_structured → payload lands on RunResult.structured_output."""
        payload = {"file_exists": True, "first_word": "hello"}
        fake = AntigravityFake()
        fake.script_structured(payload, session_id="conv-so",
                               schema=_FIXTURE_SCHEMA)

        result = _run(_spec(), fake)

        assert result.subtype == "success"
        assert result.structured_output == payload
        assert result.session_id == "conv-so"

    def test_multiple_responses_consumed_in_order(self):
        """Multiple scripted streams are consumed in call order."""
        fake = AntigravityFake()
        fake.script_response("one", session_id="s1")
        fake.script_response("two", session_id="s2")
        fake.script_fail("boom", session_id="s3")
        spec = _spec()

        async def _run_many():
            with fake:
                backend = AntigravityBackend()
                r1 = await backend.run(spec)
                r2 = await backend.run(spec)
                r3 = await backend.run(spec)
                return r1, r2, r3

        r1, r2, r3 = asyncio.run(_run_many())

        assert (r1.text, r1.session_id) == ("one\n", "s1")
        assert (r2.text, r2.session_id) == ("two\n", "s2")
        assert r3.subtype == "error" and r3.session_id == "s3"
        assert len(fake.calls) == 3

    def test_spawn_oserror_is_retryable_oserror(self):
        """A scripted spawn failure surfaces as OSError (in the backend's
        retryable_exceptions tuple), not a crash."""
        fake = AntigravityFake()
        fake.script_spawn_oserror()

        with pytest.raises(OSError):
            _run(_spec(), fake)

    def test_attestation_none_omits_init_permission_mode(self):
        """attestation=None truly drops init.permission_mode end-to-end:
        on a read-only phase the real §4.4 self-check then fails closed
        (proving the field is absent, not merely un-checked)."""
        fake = AntigravityFake()
        fake.script_response("ok", attestation=None)

        with pytest.raises(RuntimeError, match="did not attest permission_mode"):
            _run(_spec(mode="plan"), fake)

    def test_attestation_none_passes_on_read_write(self):
        """The omitted attestation itself is inert on a read_write run —
        only the read-only self-check consumes it."""
        fake = AntigravityFake()
        fake.script_response("ok", attestation=None)

        result = _run(_spec(mode="read_write"), fake)

        assert result.subtype == "success"


# ---------------------------------------------------------------------------
# 2. Call-record (argv) fidelity
# ---------------------------------------------------------------------------


class TestAntigravityFakeCalls:
    """Assert AntigravityFake records the agy flags the real backend emitted."""

    def test_fresh_call_argv(self):
        """A fresh run always pins the stream-json transport + slash guard."""
        fake = AntigravityFake()
        fake.script_response("ok")

        _run(_spec(), fake)

        assert len(fake.calls) == 1
        call = fake.calls[0]
        assert call["executable"] == "agy"
        assert call["input_format"] == "stream-json"
        assert call["output_format"] == "stream-json"
        assert call["disable_slash_commands"] is True
        assert call["skip_permissions"] is True
        assert call["model"] == MODEL
        assert call["print_timeout"] == "30s"
        assert call["is_fresh"] is True
        assert call["is_resume"] is False
        assert "conversation" not in call

    def test_prompt_rides_stdin_not_argv(self):
        """The prompt is the single stdin user event — never on the command line."""
        prompt = "long prompt " + ("x" * 500)
        fake = AntigravityFake()
        fake.script_response("ok")

        _run(_spec(prompt=prompt), fake)

        call = fake.calls[0]
        assert prompt not in " ".join(call["argv"])
        assert call["prompt"] == prompt
        assert len(call["stdin_events"]) == 1
        ev = call["stdin_events"][0]
        assert ev["event"] == "user"
        assert ev["message"]["content"] == prompt

    def test_resume_call_argv(self):
        """A resume reaches agy as ``--conversation <id>`` (resume in place)."""
        fake = AntigravityFake()
        fake.script_response("resumed", session_id="conv-resume")

        _run(_spec(resume="conv-1"), fake)

        call = fake.calls[0]
        assert call["is_resume"] is True
        assert call["is_fresh"] is False
        assert call["conversation"] == "conv-1"

    def test_fork_spec_resumes_in_place(self):
        """agy has no fork primitive: a fork spec degrades to a plain resume."""
        fake = AntigravityFake()
        fake.script_response("ok")

        _run(_spec(resume="conv-src", fork_session=True), fake)

        call = fake.calls[0]
        assert call["conversation"] == "conv-src"
        assert not any("fork" in part.lower() for part in call["argv"])

    def test_structured_request_writes_schema_file(self):
        """spec.output_format → ``--json-schema <tmpfile>`` (never inline)."""
        fake = AntigravityFake()
        fake.script_structured({"file_exists": True, "first_word": "hi"},
                               schema=_FIXTURE_SCHEMA)

        _run(_spec(output_format={"type": "json_schema",
                                  "schema": _FIXTURE_SCHEMA}), fake)

        call = fake.calls[0]
        assert call["json_schema_path"].endswith(".json")
        # The schema file is reclaimed after the run (finally-cleanup path).
        assert not Path(call["json_schema_path"]).exists()

    def test_optional_profile_flags(self):
        """effort / agent / sandbox / log_dir profile knobs reach the argv."""
        fake = AntigravityFake()
        fake.script_response("ok")

        _run(_spec(state={"_harness_cfg": {
            "backend": "antigravity", "model": MODEL,
            "effort": "high", "agent": "default",
            "sandbox": True, "log_dir": "/tmp/agylogs",
        }}), fake)

        call = fake.calls[0]
        assert call["effort"] == "high"
        assert call["agent"] == "default"
        assert call["sandbox"] is True
        assert call["log_file"].startswith("/tmp/agylogs/")

    def test_skip_permissions_off(self):
        """skip_permissions=False drops the --dangerously-skip-permissions flag."""
        fake = AntigravityFake()
        fake.script_response("ok")

        _run(_spec(state={"_harness_cfg": {
            "backend": "antigravity", "model": MODEL,
            "skip_permissions": False,
        }}), fake)

        assert fake.calls[0]["skip_permissions"] is False

    def test_env_precedence(self):
        """gemini_api_key → GEMINI_API_KEY; spec.env_overrides win last."""
        fake = AntigravityFake()
        fake.script_response("ok")

        _run(_spec(
            env_overrides={"EXTRA": "1"},
            state={"_harness_cfg": {
                "backend": "antigravity", "model": MODEL,
                "gemini_api_key": "sk-fake",
            }},
        ), fake)

        env = fake.calls[0]["env"]
        assert env["GEMINI_API_KEY"] == "sk-fake"
        assert env["EXTRA"] == "1"

    def test_cwd_passed_to_spawn(self):
        """There is no -C flag: the subprocess cwd is the only workspace lever."""
        fake = AntigravityFake()
        fake.script_response("ok")

        _run(_spec(cwd="/tmp/somewhere"), fake)

        assert fake.calls[0]["cwd"] == "/tmp/somewhere"


# ---------------------------------------------------------------------------
# 3. Key-set provenance: fake lines ⊆ fixture keys (no hand-invented fields)
# ---------------------------------------------------------------------------


def _flatten(obj, prefix: str, out: set[str]) -> None:
    """Flatten a JSON object to dotted key paths.

    Descent stops inside ``json_schema`` / ``structured_output`` subtrees:
    their inner keys are caller data (the harness's schema / payload), not
    protocol fields, so they can't be pinned to fixture contents.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            out.add(p)
            if k in ("json_schema", "structured_output"):
                continue
            _flatten(v, p, out)
    elif isinstance(obj, list):
        for item in obj:
            _flatten(item, prefix, out)


def _fixture_unions() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """(per-event key union, per-step_type key union) over all fixtures."""
    by_event: dict[str, set[str]] = {}
    by_step_type: dict[str, set[str]] = {}
    for f in sorted(FIXTURES.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            ev = json.loads(line)
            et = ev.get("event", "?")
            paths: set[str] = set()
            _flatten(ev, "", paths)
            by_event.setdefault(et, set()).update(paths)
            if et == "step_update":
                st = (ev.get("step_update") or {}).get("step_type", "?")
                by_step_type.setdefault(st, set()).update(paths)
    return by_event, by_step_type


# Streams covering every builder's emitted shape (success w/ tool steps,
# structured with fixture schema, error, waiting, killed, killed-rc,
# config_error).  The Phase 5 ``mcp=True`` question preview is deliberately
# NOT listed: it is an anticipated Phase 5 shape (its ``body`` parameter key
# has no fixture provenance yet) and stays off the Phase 3 default path.
def _all_fake_lines() -> list[dict]:
    streams = [
        _success_stream("cid", "hello"),
        _success_stream("cid", "applied", tool_steps=[
            _step_tool_active("cid", 1, "run_command",
                              {"CommandLine": "pytest -q"}),
            _step_tool_done("cid", 1, "run_command", output="3 passed"),
            _step_tool_active("cid", 2, "write_file", {"Path": "src/a.py"}),
            _step_tool_done("cid", 2, "write_file",
                            error={"type": "permission_denied",
                                   "message": "denied in headless mode"}),
        ]),
        _success_stream("cid", "hi", structured={"file_exists": True,
                                                 "first_word": "x"},
                        json_schema=_FIXTURE_SCHEMA),
        _error_stream("cid", "boom"),
        _waiting_stream("cid", "awaiting"),
        _killed_stream("cid", "partial"),
        _killed_rc_stream("cid", "partial"),
        _config_error_stream("cid", "invalid model selection"),
    ]
    return [json.loads(line) for stream in streams for line in stream]


class TestAntigravityFakeKeyProvenance:
    """No emitted line may carry a protocol key the Phase 0 fixtures lack."""

    def test_emitted_keys_subset_of_fixture_union(self):
        by_event, by_step_type = _fixture_unions()
        assert set(by_event) >= {"init", "step_update", "result"}, (
            "fixture corpus lost an event type the fake emits"
        )
        for obj in _all_fake_lines():
            et = obj.get("event", "?")
            assert et in by_event, f"fake emits unknown event {et!r}"
            paths: set[str] = set()
            _flatten(obj, "", paths)
            extra = paths - by_event[et]
            assert not extra, (
                f"line keys not observed in fixtures for event {et!r}: "
                f"{sorted(extra)}"
            )
            if et == "step_update":
                st = obj["step_update"].get("step_type")
                assert st in by_step_type, (
                    f"fake emits step_type {st!r} no fixture carries"
                )
                st_extra = paths - by_step_type[st]
                assert not st_extra, (
                    f"step_update keys not observed in fixtures for "
                    f"step_type {st!r}: {sorted(st_extra)}"
                )


# ---------------------------------------------------------------------------
# 4. Golden replay: every fixture parses warning-free through the real parser
# ---------------------------------------------------------------------------

# Terminal subtype expected from each fixture when finalized at rc 0.
_FIXTURE_SUBTYPES = {
    "basic_success": "success",
    "killed_partial": "killed",
    "permission_denied": "success",
    "print_timeout": "error",
    "resume": "success",
    "stdin_single_turn": "success",
    "structured_output": "success",
    "structured_output_violation": "success",
    "subagent": "success",
    "tool_error_recovered": "success",
    "tool_steps": "success",
    "unknown_model": "config_error",
    "waiting": "success",
}


class TestAntigravityGoldenReplay:
    """Real _parse_line/_finalize over each fixture: no [WARN], right subtype."""

    @pytest.mark.parametrize("fixture_name", sorted(_FIXTURE_SUBTYPES))
    def test_fixture_replays_warning_free(self, fixture_name, capsys):
        lines = (FIXTURES / f"{fixture_name}.jsonl").read_text(
            encoding="utf-8").splitlines()

        acc = _AgyAccumulator()
        for line in lines:
            line = line.strip()
            if line:
                _parse_line(line, acc=acc, callback=None)
        result = _finalize(acc, 0)

        captured = capsys.readouterr()
        assert "[WARN]" not in captured.out, (
            f"{fixture_name}.jsonl replay emitted a warning:\n{captured.out}"
        )
        assert result.subtype == _FIXTURE_SUBTYPES[fixture_name]
        if fixture_name == "unknown_model":
            # The config death happens before the CLI creates a conversation
            # (conversation_id is "" on the wire): session_id=None is the
            # fixture-true outcome, not a lost id.
            assert result.session_id is None
        else:
            assert result.session_id, f"{fixture_name} lost its conversation id"

    def test_tool_label_run_command_and_file(self):
        """The progress-label mapping for the observed tool parameter keys."""
        run = {"tool_name": "run_command",
               "tool_info": {"name": "run_command",
                             "parameters": {"CommandLine": "pytest -q"}}}
        write = {"tool_name": "write_file",
                 "tool_info": {"name": "write_file",
                               "parameters": {"Path": "src/a.py"}}}
        assert agy_mod._tool_label(run) == "$ pytest -q"
        assert agy_mod._tool_label(write) == "write src/a.py"
