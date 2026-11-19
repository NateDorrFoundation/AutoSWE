#!/usr/bin/env python3
"""agy Gemini->OpenAI wire bridge (vendored; the Phase 0 spike depends on it).

Purpose: `agy` (Antigravity CLI) speaks the Gemini generateContent wire, which
LiteLLM cannot serve. This ~250-line proxy translates Gemini <-> OpenAI
chat.completions in both directions, including accumulation of streamed
function-call argument deltas into one complete functionCall at finish_reason
(required: the CLI's tool loop does not reassemble OpenAI-style deltas itself).

Listen: 127.0.0.1:4001 (override with AGY_PROXY_PORT).
Upstream: OpenAI-compatible LiteLLM (default http://linux-server1:4000/v1,
override with AGY_PROXY_UPSTREAM).
Auth: the upstream key is read ONLY from AGY_PROXY_UPSTREAM_KEY (env /
EnvironmentFile) — never hardcode it here; keep the real key in a local
proxy.env outside the repo (chmod 600 it). The `agy` side needs only
GEMINI_API_KEY=<any non-empty string> + GOOGLE_GEMINI_BASE_URL=...:4001.
Inbound (opt-in): if AGY_BRIDGE_TOKEN is set, every endpoint requires the
same token as the x-goog-api-key header or the ?key= query param — `agy`
sends whatever GEMINI_API_KEY holds in exactly those places, so the
deployment sets AGY_BRIDGE_TOKEN=<random> in proxy.env and invokes `agy`
with GEMINI_API_KEY=<the same token>. Unset = allow all (spike default).

Error contract: upstream failures and internal errors are never masqueraded
as a normal reply. The stream yields ONE final chunk with finishReason "ERROR"
carrying the error text, then raises, so uvicorn tears the response down —
`agy` sees a stream abort / ERROR terminal, never STOP-with-error-text it
could record as SUCCESS. Verified against the real CLI on a test port: the run
ends `result.status:"ERROR"` (well-formed record), and agy re-issues the
aborted request until its wall-clock/print-timeout fires — a hard-failed
upstream exits via timeout, not fast.

Endpoints served:
  GET  /v1beta/models
  POST /v1beta/models/<model>:generateContent
  POST /v1beta/models/<model>:streamGenerateContent   (alt=sse or default)

Minimal but tool-aware: systemInstruction, text parts, functionCall/
functionResponse, tools -> OpenAI tools, usageMetadata, finishReason.

Install: copy as ~/.litellm-agy/gemini-openai-proxy.py next to a local proxy.env
and run via scripts/agy-gemini-proxy.service (systemd user unit; the ExecStart
path must match; see spike-antigravity.md §0.1.2 for the full BYOK recipe).
"""
import hmac
import json
import os
import sys

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

UPSTREAM = os.environ.get("AGY_PROXY_UPSTREAM", "http://linux-server1:4000/v1")
UPSTREAM_KEY = os.environ.get("AGY_PROXY_UPSTREAM_KEY", "")
PORT = int(os.environ.get("AGY_PROXY_PORT", "4001"))
BRIDGE_TOKEN = os.environ.get("AGY_BRIDGE_TOKEN", "")

app = FastAPI()


def inbound_denied(request: Request):
    """Opt-in inbound shared-secret check (see module header, "Inbound").

    When AGY_BRIDGE_TOKEN is unset (spike default) every request is allowed.
    When set, the request must present the same token via the x-goog-api-key
    header or the ?key= query param — Gemini clients (incl. `agy` via
    GEMINI_API_KEY) send their key in exactly those places.
    """
    if not BRIDGE_TOKEN:
        return None
    supplied = request.headers.get("x-goog-api-key") or request.query_params.get("key") or ""
    if hmac.compare_digest(supplied, BRIDGE_TOKEN):
        return None
    return JSONResponse({"error": {"message": "invalid or missing API key",
                                   "status": "UNAUTHENTICATED"}}, status_code=401)

import re

MODEL_RE = re.compile(r"^(?:gemini-api:|blade:|evergreen:|beyond-api:)(?:.*?/models/(.+)|(?:.*/)?([^/]+))$")


def normalize_model(name: str) -> str:
    name = name.removeprefix("models/")
    if "/models/" in name:
        return name.split("/models/", 1)[1]
    m = MODEL_RE.match(name)
    if m:
        return m.group(1) or m.group(2)
    return name

FINISH_MAP = {
    "stop": "STOP",
    "length": "MAX_TOKENS",
    "tool_calls": "STOP",
    "function_call": "STOP",
    "content_filter": "SAFETY",
}


def gemini_to_openai(body: dict, model: str) -> dict:
    # Gemini functionResponse parts carry no id, so tool_call ids are assigned
    # deterministically per (tool name, occurrence): the n-th functionCall for a
    # name and the n-th functionResponse for that name get the same id (history
    # replays calls and responses in strict interleaved order). A random id per
    # call would leave every tool result pointing at a nonexistent tool call,
    # which strict OpenAI-compatible upstreams reject with 400.
    # Known spike limitation: the pairing assumes every functionResponse's
    # functionCall was replayed in the same history; a response whose call was
    # never replayed (e.g. cancelled) gets an id matching nothing, which those
    # strict upstreams then 400 on.
    call_seq: dict = {}
    resp_seq: dict = {}
    msgs = []
    sysinst = body.get("systemInstruction")
    if sysinst:
        texts = [p.get("text", "") for p in sysinst.get("parts", []) if "text" in p]
        if texts:
            msgs.append({"role": "system", "content": "\n".join(texts)})
    for turn in body.get("contents", []):
        role = "assistant" if turn.get("role") == "model" else "user"
        parts = turn.get("parts", [])
        texts, tool_calls = [], []
        for p in parts:
            if "text" in p:
                texts.append(p["text"])
            elif "functionCall" in p:
                fc = p["functionCall"]
                fname = fc.get("name", "")
                seq = call_seq.get(fname, 0)
                call_seq[fname] = seq + 1
                tool_calls.append({
                    "id": f"call_{fname}_{seq}",
                    "type": "function",
                    "function": {
                        "name": fname,
                        "arguments": json.dumps(fc.get("args", {})),
                    },
                })
            elif "functionResponse" in p:
                fr = p["functionResponse"]
                fname = fr.get("name", "")
                seq = resp_seq.get(fname, 0)
                resp_seq[fname] = seq + 1
                msgs.append({
                    "role": "tool",
                    "tool_call_id": f"call_{fname}_{seq}",
                    "name": fname,
                    "content": json.dumps(fr.get("response", {})),
                })
        if tool_calls:
            m = {"role": "assistant", "content": "\n".join(texts) or None,
                 "tool_calls": tool_calls}
            msgs.append(m)
        elif texts:
            # plain text turn; function-response-only turns were already appended
            msgs.append({"role": role, "content": "\n".join(texts)})
    cfg = {
        "model": model,
        "messages": msgs,
        "stream": False,
    }
    gen = body.get("generationConfig") or {}
    if "temperature" in gen:
        cfg["temperature"] = gen["temperature"]
    if "maxOutputTokens" in gen:
        cfg["max_tokens"] = gen["maxOutputTokens"]
    if "topP" in gen:
        cfg["top_p"] = gen["topP"]
    tools = body.get("tools") or []
    decls = []
    for t in tools:
        for d in t.get("functionDeclarations", []):
            decls.append({
                "type": "function",
                "function": {
                    "name": d.get("name"),
                    "description": d.get("description", ""),
                    "parameters": d.get("parametersJsonSchema")
                        or d.get("parameters") or {"type": "object", "properties": {}},
                },
            })
    if decls:
        cfg["tools"] = decls
        cfg["tool_choice"] = "auto"
    return cfg


def openai_to_gemini(resp: dict) -> dict:
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    parts = []
    if msg.get("reasoning_content") or msg.get("reasoning"):
        rc = msg.get("reasoning_content") or msg.get("reasoning")
        parts.append({"text": rc, "thought": True})
    if msg.get("content"):
        parts.append({"text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        try:
            args = json.loads(tc["function"]["arguments"] or "{}")
        except json.JSONDecodeError:
            args = {"_raw": tc["function"]["arguments"]}
        parts.append({"functionCall": {"name": tc["function"]["name"], "args": args}})
    usage = resp.get("usage") or {}
    out = {
        "candidates": [{
            "content": {"parts": parts or [{"text": ""}], "role": "model"},
            "finishReason": FINISH_MAP.get(choice.get("finish_reason"), "STOP"),
            "index": 0,
        }],
        "usageMetadata": {
            "promptTokenCount": usage.get("prompt_tokens", 0),
            "candidatesTokenCount": usage.get("completion_tokens", 0),
            "totalTokenCount": usage.get("total_tokens", 0),
        },
        "modelVersion": resp.get("model", ""),
    }
    return out


def gemini_usage(usage: dict) -> dict:
    return {"promptTokenCount": usage.get("prompt_tokens", 0),
            "candidatesTokenCount": usage.get("completion_tokens", 0),
            "totalTokenCount": usage.get("total_tokens", 0)}


def flushed_calls(tool_acc: dict) -> list:
    """Turn accumulated tool-call fragments into complete functionCall parts."""
    parts = []
    for tacc in tool_acc.values():
        try:
            args = json.loads(tacc["args"] or "{}")
        except json.JSONDecodeError:
            args = {"_raw": tacc["args"]}
        parts.append({"functionCall": {"name": tacc["name"], "args": args}})
    return parts


def sse_gemini_chunk(parts=None, finish=None, usage=None, model="") -> str:
    cand = {"content": {"parts": parts or [], "role": "model"}, "index": 0}
    if finish:
        cand["finishReason"] = finish
    obj = {"candidates": [cand], "modelVersion": model}
    if usage:
        obj["usageMetadata"] = usage
    return "data: " + json.dumps(obj) + "\n\n"


# TODO(phase-1): host-specific constants — this model list and the UPSTREAM
# default are deployment values; parameterize (AGY_PROXY_UPSTREAM already
# overrides UPSTREAM; a AGY_BRIDGE_MODELS env would cover this list).
@app.get("/v1beta/models")
async def list_models(request: Request):
    denied = inbound_denied(request)
    if denied:
        return denied
    return {"models": [{"name": "models/qwen3.8-flash-next",
                        "displayName": "qwen3.8-flash-next (local)",
                        "supportedGenerationMethods": ["generateContent"]}]}


@app.post("/v1beta/models/{model:path}:generateContent")
async def generate(model: str, request: Request):
    denied = inbound_denied(request)
    if denied:
        return denied
    body = await request.json()
    model = normalize_model(model)
    cfg = gemini_to_openai(body, model)
    async with httpx.AsyncClient(timeout=600) as client:
        r = await client.post(f"{UPSTREAM}/chat/completions", json=cfg,
                              headers={"Authorization": f"Bearer {UPSTREAM_KEY}"})
    if r.status_code != 200:
        return JSONResponse({"error": {"message": r.text[:2000], "status": "UPSTREAM_ERROR"}},
                            status_code=r.status_code)
    return JSONResponse(openai_to_gemini(r.json()))


@app.post("/v1beta/models/{model:path}:streamGenerateContent")
async def stream(model: str, request: Request):
    denied = inbound_denied(request)
    if denied:
        return denied
    body = await request.json()
    model = normalize_model(model)
    cfg = gemini_to_openai(body, model)
    cfg["stream"] = True
    cfg.setdefault("stream_options", {})["include_usage"] = True

    async def event_stream():
        usage = None
        model_out = model
        tool_acc = {}  # index -> {"name": str, "args": str}
        try:
            async with httpx.AsyncClient(timeout=600) as client:
                async with client.stream(
                    "POST", f"{UPSTREAM}/chat/completions", json=cfg,
                    headers={"Authorization": f"Bearer {UPSTREAM_KEY}"},
                ) as r:
                    if r.status_code != 200:
                        text = (await r.aread()).decode()[:2000]
                        # Error contract (module header): never STOP-with-error-text.
                        # Raise so the except below emits the ERROR chunk and aborts.
                        raise RuntimeError(f"upstream error: {text}")
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("model"):
                            model_out = chunk["model"]
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                        for ch in chunk.get("choices") or []:
                            d = ch.get("delta") or {}
                            parts = []
                            if d.get("reasoning_content") or d.get("reasoning"):
                                rc = d.get("reasoning_content") or d.get("reasoning")
                                if rc:
                                    parts.append({"text": rc, "thought": True})
                            if d.get("content"):
                                parts.append({"text": d["content"]})
                            for tc in d.get("tool_calls") or []:
                                idx = tc.get("index", 0)
                                acc = tool_acc.setdefault(idx, {"name": "", "args": ""})
                                fn = tc.get("function") or {}
                                if fn.get("name"):
                                    acc["name"] += fn["name"]
                                if fn.get("arguments"):
                                    acc["args"] += fn["arguments"]
                            finish = FINISH_MAP.get(ch.get("finish_reason")) \
                                if ch.get("finish_reason") else None
                            if finish and tool_acc:
                                parts.extend(flushed_calls(tool_acc))
                                tool_acc = {}
                            if parts or finish:
                                yield sse_gemini_chunk(parts or None, finish, None, model_out)
                    # include_usage puts usage in its own trailing chunk
                    # (choices: []) that arrives after the finish chunk; forward
                    # it as a standalone usageMetadata chunk, before the flush.
                    if usage:
                        yield sse_gemini_chunk(None, None, gemini_usage(usage), model_out)
                    if tool_acc:
                        # Upstream ended with [DONE] but never sent a finish_reason:
                        # flush the accumulated tool calls so they are not dropped.
                        yield sse_gemini_chunk(flushed_calls(tool_acc), "STOP",
                                               None, model_out)
                        tool_acc = {}
        except Exception as e:  # noqa: BLE001
            # Error contract (module header): ONE final chunk with finishReason
            # ERROR carrying the text, then re-raise so uvicorn aborts the
            # stream — `agy` never records a STOP (SUCCESS) on a failed run.
            yield sse_gemini_chunk([{"text": f"proxy error: {e}"}], "ERROR", None, model)
            raise

    return StreamingResponse(event_stream(), media_type="text/event-stream")


if __name__ == "__main__":
    print(f"gemini-openai-proxy: :{PORT} -> {UPSTREAM}", file=sys.stderr)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
