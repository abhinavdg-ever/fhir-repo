"""The models the planner can use (Azure AI Foundry deployments), how to call each, and what it costs.

GPT, Llama and Mistral deployments answer on the OpenAI-compatible v1 endpoint
(<AOAI_ENDPOINT>/openai/v1/chat/completions); Claude answers only on the Anthropic Messages
endpoint (<AOAI_ENDPOINT>/anthropic/v1/messages). Both use the same Foundry resource and key.

Prices are USD per 1M tokens, Global Standard, pay-as-you-go:
- GPT and Llama: Azure Retail Prices API (prices.azure.com), Nov 2026.
- Mistral Small 2503 and Claude Haiku 4.5 are Marketplace models (not in that API): publisher list price.
Override any of them in .env with LLM_PRICES, e.g. LLM_PRICES={"gpt-4o": [2.5, 10, 1.25]}.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field

import httpx


class LlmError(RuntimeError):
    pass


@dataclass
class Model:
    deployment: str
    label: str
    api: str                 # "openai" (v1 chat completions) | "anthropic" (Messages)
    input: float             # USD per 1M input tokens
    output: float            # USD per 1M output tokens
    cached: float | None = None  # USD per 1M cached input tokens (None = billed as input)
    json_mode: bool = True   # accepts response_format=json_object
    price_source: str = "Azure Retail Prices API"
    extra: dict = field(default_factory=dict)


CATALOG: list[Model] = [
    Model("gpt-4o", "GPT-4o (2024-11-20)", "openai", 2.50, 10.00, 1.25),
    Model("gpt-4.1-mini", "GPT-4.1 mini", "openai", 0.40, 1.60, 0.10),
    Model("Llama-4-Maverick-17B-128E-Instruct-FP8", "Llama 4 Maverick 17B", "openai", 0.25, 1.00),
    Model("mistral-small-2503", "Mistral Small 3.1 (2503)", "openai", 0.10, 0.30,
          price_source="Mistral list price (Azure Marketplace)"),
    Model("claude-haiku-4-5", "Claude Haiku 4.5", "anthropic", 1.00, 5.00, 0.10, json_mode=False,
          price_source="Anthropic list price (Azure Marketplace)"),
]


# The costliest text model on Azure AI Foundry (Global Standard, Azure Retail Prices API, Nov 2026):
# every cost is also shown against it ("x% lower than o1-pro"), and the UI's cost stars use it as 5 stars.
COSTLIEST = Model("o1-pro", "o1-pro", "openai", 150.00, 600.00)


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name, default).split(" #")[0].strip()
    return "" if "<" in value else value


def _model_names() -> list[str]:
    """AOAI_MODELS (first = default), else the built-in catalog. A legacy AOAI_DEPLOYMENT goes first."""
    names = [n.strip() for n in _env("AOAI_MODELS").split(",") if n.strip()] or [m.deployment for m in CATALOG]
    legacy = _env("AOAI_DEPLOYMENT")
    if legacy:
        names = [legacy] + [n for n in names if n != legacy]
    return names


def default_deployment() -> str:
    return _model_names()[0]


def models() -> list[Model]:
    """The catalog, limited to AOAI_MODELS when set, plus any deployment named there that is not in it."""
    by_name = {m.deployment: m for m in CATALOG}
    overrides = {}
    if _env("LLM_PRICES"):
        try:
            overrides = json.loads(_env("LLM_PRICES"))
        except ValueError:
            overrides = {}
    out = []
    for name in _model_names():
        m = by_name.get(name) or Model(name, name, "anthropic" if name.lower().startswith("claude") else "openai",
                                       0.0, 0.0, json_mode=not name.lower().startswith("claude"),
                                       price_source="unknown: set LLM_PRICES in .env")
        if name in overrides:
            p = overrides[name]
            m = Model(**{**m.__dict__, "input": float(p[0]), "output": float(p[1]),
                         "cached": float(p[2]) if len(p) > 2 else m.cached, "price_source": "LLM_PRICES (.env)"})
        out.append(m)
    return out


def get_model(name: str | None) -> Model:
    name = name or default_deployment()
    for m in models():
        if m.deployment == name:
            return m
    raise LlmError(f"Unknown model {name!r}; available: {', '.join(m.deployment for m in models())}")


def cost(m: Model, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
    cached_rate = m.input if m.cached is None else m.cached
    fresh = max(input_tokens - cached_tokens, 0)
    return round((fresh * m.input + cached_tokens * cached_rate + output_tokens * m.output) / 1_000_000, 6)


# ---- calling ----

def _auth_headers(api: str) -> dict:
    key = _env("AOAI_API_KEY")
    if key:
        return {"x-api-key": key} if api == "anthropic" else {"api-key": key}
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:
        raise LlmError("AOAI_API_KEY is empty and azure-identity is not installed") from exc
    token = DefaultAzureCredential().get_token("https://cognitiveservices.azure.com/.default").token
    return {"Authorization": f"Bearer {token}"}


def _post(url: str, headers: dict, body: dict, timeout: float) -> dict:
    try:
        r = httpx.post(url, headers=headers, json=body, timeout=timeout)
    except httpx.HTTPError as exc:
        raise LlmError(f"Model call failed ({type(exc).__name__}): {exc}") from exc
    if r.status_code != 200:
        try:
            detail = r.json().get("error", r.text)
            detail = detail.get("message", detail) if isinstance(detail, dict) else detail
        except ValueError:
            detail = r.text
        raise LlmError(f"HTTP {r.status_code}: {str(detail)[:300]}")
    return r.json()


def parse_json(text: str) -> dict:
    """The JSON object in a reply; tolerates ```json fences and text around it."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text[text.find("{"): text.rfind("}") + 1]
    try:
        return json.loads(candidate)
    except ValueError as exc:
        raise LlmError(f"The model did not return JSON: {text[:200]}") from exc


def chat_json(model: Model, messages: list[dict], temperature: float | None, timeout: float,
              max_tokens: int = 2000) -> tuple[dict, dict]:
    """Send the messages, return (parsed JSON reply, usage record)."""
    base = _env("AOAI_ENDPOINT").rstrip("/")
    if not base:
        raise LlmError("AOAI_ENDPOINT is not set in .env")
    started = time.perf_counter()
    if model.api == "anthropic":
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        body = {"model": model.deployment, "max_tokens": max_tokens, "system": system,
                "messages": [m for m in messages if m["role"] != "system"]}
        if temperature is not None:
            body["temperature"] = temperature
        data = _post(f"{base}/anthropic/v1/messages",
                     {**_auth_headers("anthropic"), "anthropic-version": "2023-06-01"}, body, timeout)
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        u = data.get("usage") or {}
        cached = int(u.get("cache_read_input_tokens") or 0)
        input_tokens = int(u.get("input_tokens") or 0) + cached + int(u.get("cache_creation_input_tokens") or 0)
        output_tokens = int(u.get("output_tokens") or 0)
    else:
        body = {"model": model.deployment, "messages": messages, "max_tokens": max_tokens}
        if model.json_mode:
            body["response_format"] = {"type": "json_object"}
        if temperature is not None:
            body["temperature"] = temperature
        url = f"{base}/openai/v1/chat/completions"
        headers = _auth_headers("openai")
        try:
            data = _post(url, headers, body, timeout)
        except LlmError as exc:
            # gpt-5 / o-series reject temperature and max_tokens; some models reject json mode.
            msg = str(exc).lower()
            if not any(k in msg for k in ("temperature", "max_tokens", "response_format", "json")):
                raise
            body.pop("temperature", None)
            if "max_tokens" in msg:
                body["max_completion_tokens"] = body.pop("max_tokens")
            if "response_format" in msg or "json" in msg:
                body.pop("response_format", None)
            data = _post(url, headers, body, timeout)
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        u = data.get("usage") or {}
        input_tokens = int(u.get("prompt_tokens") or 0)
        output_tokens = int(u.get("completion_tokens") or 0)
        cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    usage = {
        "model": model.deployment, "label": model.label,
        "inputTokens": input_tokens, "cachedTokens": cached, "outputTokens": output_tokens,
        "costUsd": cost(model, input_tokens, output_tokens, cached),
        "latencyMs": round((time.perf_counter() - started) * 1000),
        "pricePer1M": {"input": model.input, "cached": model.cached, "output": model.output},
        "costliest": {"model": COSTLIEST.deployment, "costUsd": cost(COSTLIEST, input_tokens, output_tokens)},
        "priceSource": model.price_source,
    }
    record(usage)
    return parse_json(text), usage


# ---- usage ledger (in memory, since the server started) ----

_lock = threading.Lock()
_ledger: list[dict] = []


def record(usage: dict) -> None:
    with _lock:
        _ledger.append({**usage, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        del _ledger[:-1000]


def usage_summary() -> dict:
    with _lock:
        calls = list(_ledger)
    totals: dict[str, dict] = {}
    for c in calls:
        t = totals.setdefault(c["model"], {"model": c["model"], "label": c["label"], "calls": 0,
                                           "inputTokens": 0, "outputTokens": 0, "costUsd": 0.0})
        t["calls"] += 1
        t["inputTokens"] += c["inputTokens"]
        t["outputTokens"] += c["outputTokens"]
        t["costUsd"] = round(t["costUsd"] + c["costUsd"], 6)
    return {"calls": calls[::-1][:50], "byModel": list(totals.values()),
            "totalCostUsd": round(sum(t["costUsd"] for t in totals.values()), 6)}


def reset_usage() -> None:
    with _lock:
        _ledger.clear()
