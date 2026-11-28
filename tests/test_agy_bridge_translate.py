"""Offline tests for the vendored agy wire bridge (scripts/agy-gemini-proxy.py).

The bridge lives in scripts/ (outside the ruff gate) but is production
infrastructure and the source of every tests/fixtures/antigravity/ capture, so
its pure translation logic is pinned here: model normalization, the
deterministic call_{name}_{seq} tool_call_id pairing, streamed tool-call
argument accumulation, the finish-reason map, and the never-STOP-on-error
stream-abort contract.

No network: the module body imports fastapi/httpx/uvicorn, which are not
AutoSWE dependencies, so the module is exec'd once with those stubbed in
sys.modules (restored immediately afterwards — the exec'd namespace keeps its
own references). Streaming tests drive the handler's async generator with
fake httpx clients.
"""
import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parents[1] / "scripts" / "agy-gemini-proxy.py"


def _load_bridge() -> types.ModuleType:
    saved = {}
    stubs = {}
    for name in ("httpx", "uvicorn"):
        saved[name] = sys.modules.get(name)
        stubs[name] = types.ModuleType(name)
    fastapi = types.ModuleType("fastapi")

    def _route(*a, **k):
        def deco(fn):
            return fn
        return deco

    fastapi.FastAPI = lambda *a, **k: types.SimpleNamespace(get=_route, post=_route)
    fastapi.Request = object
    saved["fastapi"] = sys.modules.get("fastapi")
    stubs["fastapi"] = fastapi
    responses = types.ModuleType("fastapi.responses")
    # Preserve status_code so the 401 paths stay assertable.
    responses.JSONResponse = lambda payload=None, **k: {
        "__resp__": payload, "status_code": k.get("status_code", 200)}
    responses.StreamingResponse = lambda gen, **k: gen
    saved["fastapi.responses"] = sys.modules.get("fastapi.responses")
    stubs["fastapi.responses"] = responses

    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location("agy_gemini_bridge", BRIDGE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for name, orig in saved.items():
            if orig is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = orig
    return mod


@pytest.fixture(scope="module")
def bridge() -> types.ModuleType:
    # The module reads env at import: pin the optional-inbound-auth var off so
    # an ambient AGY_BRIDGE_TOKEN on the runner can't change what we exercise.
    mp = pytest.MonkeyPatch()
    mp.setenv("AGY_BRIDGE_TOKEN", "")
    mp.setenv("AGY_PROXY_UPSTREAM", "http://upstream.invalid/v1")
    mp.setenv("AGY_PROXY_UPSTREAM_KEY", "")
    try:
        return _load_bridge()
    finally:
        mp.undo()


# ---- normalize_model -------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    # the request shape every experiment used: gemini-api:// base + /models/<id>
    ("gemini-api://127.0.0.1:4001/models/qwen3.8-flash-next", "qwen3.8-flash-next"),
    # bare Gemini full resource name — the shape list_models emits
    ("models/qwen3.8-flash-next", "qwen3.8-flash-next"),
    # internal prefix form, /models/ split
    ("blade:some/vendor/models/deep-id", "deep-id"),
    # internal prefix form, MODEL_RE branch (no /models/ substring)
    ("gemini-api:vendor/model-name", "model-name"),
    # unknown passthrough: must stay byte-identical for the upstream
    ("qwen3.8-flash-next", "qwen3.8-flash-next"),
])
def test_normalize_model_shapes(bridge, name, expected):
    assert bridge.normalize_model(name) == expected


# ---- gemini_to_openai: id pairing + message translation --------------------

def _two_call_history():
    return {"systemInstruction": {"parts": [{"text": "be terse"}]},
            "contents": [
                {"role": "user", "parts": [{"text": "ls then pwd"}]},
                {"role": "model", "parts": [
                    {"text": "running"},
                    {"functionCall": {"name": "run_command", "args": {"CommandLine": "ls"}}}]},
                {"role": "user", "parts": [
                    {"functionResponse": {"name": "run_command", "response": {"out": "ok"}}}]},
                {"role": "model", "parts": [
                    {"functionCall": {"name": "run_command", "args": {"CommandLine": "pwd"}}}]},
                {"role": "user", "parts": [
                    {"functionResponse": {"name": "run_command", "response": {"out": "/tmp"}}}]},
            ]}


def test_tool_call_ids_pair_deterministically(bridge):
    cfg = bridge.gemini_to_openai(_two_call_history(), "m")
    msgs = cfg["messages"]
    assert msgs[0] == {"role": "system", "content": "be terse"}
    call_ids = [tc["id"] for m in msgs if m.get("tool_calls") for tc in m["tool_calls"]]
    tool_ids = [m["tool_call_id"] for m in msgs if m.get("role") == "tool"]
    # two calls of the SAME name get occurrence-indexed ids, and the
    # functionResponse turns (which carry no id in Gemini) reuse them.
    assert call_ids == ["call_run_command_0", "call_run_command_1"]
    assert tool_ids == call_ids


def test_assistant_tool_call_message_shape(bridge):
    cfg = bridge.gemini_to_openai(_two_call_history(), "m")
    asst = [m for m in cfg["messages"] if m.get("tool_calls")][0]
    assert asst["content"] == "running"
    tc = asst["tool_calls"][0]
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "run_command"
    assert json.loads(tc["function"]["arguments"]) == {"CommandLine": "ls"}


def test_tools_translated_with_tool_choice(bridge):
    body = {"contents": [], "tools": [{"functionDeclarations": [
        {"name": "run_command", "description": "run",
         "parametersJsonSchema": {"type": "object", "properties": {}}}]}]}
    cfg = bridge.gemini_to_openai(body, "m")
    assert cfg["tools"] == [{"type": "function", "function": {
        "name": "run_command", "description": "run",
        "parameters": {"type": "object", "properties": {}}}}]
    assert cfg["tool_choice"] == "auto"


# ---- openai_to_gemini / flushed_calls ---------------------------------------

def test_openai_to_gemini_tool_calls(bridge):
    out = bridge.openai_to_gemini({"choices": [{"message": {"tool_calls": [
        {"id": "x", "function": {"name": "run_command", "arguments": '{"c":"ls"}'}}]},
        "finish_reason": "tool_calls"}], "model": "m"})
    part = out["candidates"][0]["content"]["parts"][0]
    assert part["functionCall"] == {"name": "run_command", "args": {"c": "ls"}}
    assert out["candidates"][0]["finishReason"] == "STOP"


def test_flushed_calls_malformed_args_get_raw_fallback(bridge):
    parts = bridge.flushed_calls({0: {"name": "run_command", "args": '{"c":1}'},
                                  1: {"name": "read_file", "args": "not json"}})
    assert parts[0]["functionCall"]["args"] == {"c": 1}
    assert parts[1]["functionCall"]["args"] == {"_raw": "not json"}


def test_finish_map(bridge):
    assert bridge.FINISH_MAP["stop"] == "STOP"
    assert bridge.FINISH_MAP["length"] == "MAX_TOKENS"
    assert bridge.FINISH_MAP["tool_calls"] == "STOP"
    assert bridge.FINISH_MAP["content_filter"] == "SAFETY"
    # unknown finish reasons fall through to STOP in openai_to_gemini
    out = bridge.openai_to_gemini({"choices": [{"message": {"content": "hi"},
                                                "finish_reason": "sudden-termination"}]})
    assert out["candidates"][0]["finishReason"] == "STOP"


# ---- inbound shared-secret guard (opt-in) ------------------------------------

class _AuthReq:
    def __init__(self, headers=None, query=None):
        self.headers = headers or {}
        self.query_params = query or {}


def test_inbound_token_check_off_by_default(bridge):
    assert bridge.BRIDGE_TOKEN == ""  # pinned by the fixture
    assert bridge.inbound_denied(_AuthReq()) is None


def test_inbound_token_check_enforced_when_set(bridge):
    old = bridge.BRIDGE_TOKEN
    bridge.BRIDGE_TOKEN = "sekrit"
    try:
        assert bridge.inbound_denied(_AuthReq())["status_code"] == 401
        assert bridge.inbound_denied(
            _AuthReq(headers={"x-goog-api-key": "sekrit"})) is None
        assert bridge.inbound_denied(_AuthReq(query={"key": "sekrit"})) is None
        assert bridge.inbound_denied(
            _AuthReq(headers={"x-goog-api-key": "wrong"}))["status_code"] == 401
    finally:
        bridge.BRIDGE_TOKEN = old


# ---- streaming contract: accumulation + never-STOP-on-error -----------------

class _FakeResp:
    status_code = 200

    def __init__(self, lines, boom=None):
        self._lines = lines
        self._boom = boom

    async def aiter_lines(self):
        for line in self._lines:
            if self._boom and line == self._boom[0]:
                raise self._boom[1]
            yield line

    async def aread(self):
        return b'{"error":"boom"}'


class _FakeStreamCtx:
    def __init__(self, r):
        self.r = r

    async def __aenter__(self):
        return self.r

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, resp=None, boom=None):
        self.resp = resp
        self.boom = boom

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, *a, **k):
        if self.boom:
            raise self.boom
        return _FakeStreamCtx(self.resp)


class _Req:
    async def json(self):
        return {"contents": [{"role": "user", "parts": [{"text": "ls"}]}]}


TOOL_DELTA = ('data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
              '"function":{"name":"run_command","arguments":"{\\"c\\":"}}]}}]}')
TOOL_DELTA2 = 'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"ls\\"}"}}]}}]}'
FINISH = 'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}'
USAGE = 'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}'


def _set_client(bridge, resp=None, boom=None):
    bridge.httpx.AsyncClient = lambda **k: _FakeClient(resp=resp, boom=boom)


async def _drain(bridge, resp=None, boom=None):
    _set_client(bridge, resp=resp, boom=boom)
    gen = await bridge.stream("m", _Req())
    chunks, exc = [], None
    try:
        async for sse in gen:
            chunks.append(json.loads(sse[len("data: "):]))
    except Exception as e:  # the abort contract re-raises
        exc = e
    return chunks, exc


def _finish_reasons(chunks):
    return [c["candidates"][0].get("finishReason") for c in chunks]


def _function_calls(chunks):
    return [p["functionCall"] for c in chunks
            for p in c["candidates"][0]["content"]["parts"] if "functionCall" in p]


def test_stream_accumulates_fragments_into_one_functioncall(bridge):
    chunks, exc = asyncio.run(_drain(bridge, resp=_FakeResp(
        [TOOL_DELTA, TOOL_DELTA2, FINISH, USAGE, "data: [DONE]"])))
    assert exc is None
    # split argument fragments flushed as exactly ONE complete functionCall
    assert _function_calls(chunks) == [{"name": "run_command", "args": {"c": "ls"}}]
    assert _finish_reasons(chunks) == ["STOP", None]  # finish, then standalone usage
    assert chunks[-1]["usageMetadata"]["totalTokenCount"] == 7


def test_stream_flushes_at_done_without_finish_reason(bridge):
    chunks, exc = asyncio.run(_drain(bridge, resp=_FakeResp(
        [TOOL_DELTA, TOOL_DELTA2, "data: [DONE]"])))
    assert exc is None
    assert _function_calls(chunks) == [{"name": "run_command", "args": {"c": "ls"}}]
    assert _finish_reasons(chunks) == ["STOP"]


def test_stream_upstream_error_aborts_never_stop(bridge):
    resp = _FakeResp([])
    resp.status_code = 500
    chunks, exc = asyncio.run(_drain(bridge, resp=resp))
    assert isinstance(exc, RuntimeError) and "upstream error" in str(exc)
    assert _finish_reasons(chunks) == ["ERROR"]  # exactly one terminal chunk
    assert "STOP" not in _finish_reasons(chunks)


def test_stream_connect_failure_aborts_never_stop(bridge):
    chunks, exc = asyncio.run(_drain(bridge, boom=ConnectionRefusedError("refused")))
    assert isinstance(exc, ConnectionRefusedError)
    assert _finish_reasons(chunks) == ["ERROR"]
    assert "STOP" not in _finish_reasons(chunks)
