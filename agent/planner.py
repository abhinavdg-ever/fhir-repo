"""Service note -> request plan (LOINC lists + date window).

With use_llm on, Azure OpenAI reads the note and any failure is raised as
LlmError; there is no silent fallback. With use_llm off, the keyword rules in
doc_types.py build the plan. LLM output is always checked against the allow-list,
so a code the allow-list doesn't know never reaches Cerner.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta

from agent.doc_types import (DOCUMENT_CODES, GENERIC_DOCUMENTS, MEASURES, OBSERVATION_CODES,
                             RULES, doc_label, obs_label)
from common.models import Plan, PlanItem


class LlmError(Exception):
    """Azure OpenAI is not configured or the call failed."""


# ---------- deterministic pieces ----------

def date_window(note: str, today: date | None = None) -> tuple[str, str]:
    """Jan 1 of the earliest 20xx year in the note to Jan 1 after the latest, capped at tomorrow."""
    today = today or date.today()
    years = sorted({int(y) for y in re.findall(r"\b(20\d{2})\b", note)}) or [today.year]
    start = date(years[0], 1, 1)
    end = min(date(years[-1] + 1, 1, 1), today + timedelta(days=1))
    if end <= start:
        end = today + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def find_measures(note: str) -> list[str]:
    return [m for m in MEASURES if re.search(rf"\b{m}\b", note)]


def rules_plan(note: str, today: date | None = None) -> Plan:
    text = note.lower()
    docs: dict[str, str] = {}
    obs: dict[str, str] = {}
    for phrases, kind, codes in RULES:
        hit = next((p for p in phrases if p in text), None)
        if not hit:
            continue
        target = docs if kind == "document" else obs
        for c in codes:
            target.setdefault(c, f'note mentions "{hit}"')
    if "all medical records" in text:
        for c in GENERIC_DOCUMENTS:
            docs.setdefault(c, "generic all-records request")
    if obs and "11506-3" not in docs:  # BP / A1c evidence usually sits in the visit note
        docs["11506-3"] = "visit note that shows the result and provider"
    start, end = date_window(note, today)
    return Plan(
        measures=find_measures(note), start=start, end=end,
        documents=[PlanItem(code=c, label=doc_label(c), reason=r) for c, r in docs.items()],
        observations=[PlanItem(code=c, label=obs_label(c), reason=r) for c, r in obs.items()],
        source="rules",
    )


# ---------- Azure OpenAI ----------

def _env(name: str, default: str = "") -> str:
    """Read an env var, treating .env.example placeholders like <your-resource> as unset."""
    value = os.getenv(name, default).strip()
    return "" if "<" in value else value


def llm_configured() -> bool:
    return bool(_env("AOAI_ENDPOINT") and _env("AOAI_DEPLOYMENT"))


def llm_settings() -> dict:
    """The knobs you can change in .env to try different models or behaviour."""
    temperature = _env("AOAI_TEMPERATURE", "0")
    return {
        "deployment": _env("AOAI_DEPLOYMENT"),
        "apiVersion": _env("AOAI_API_VERSION", "2024-10-21"),
        "temperature": float(temperature) if temperature else None,
        "timeout": float(_env("AOAI_TIMEOUT", "30") or 30),
    }


def _client():
    from openai import AzureOpenAI

    s = llm_settings()
    common = dict(azure_endpoint=_env("AOAI_ENDPOINT"), api_version=s["apiVersion"],
                  timeout=s["timeout"], max_retries=1)
    key = _env("AOAI_API_KEY")
    if key:
        return AzureOpenAI(api_key=key, **common)
    # No key: use Entra ID (az login / managed identity) if azure-identity is installed.
    try:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    except ImportError as exc:
        raise LlmError("AOAI_API_KEY is empty and azure-identity is not installed") from exc
    token = get_bearer_token_provider(DefaultAzureCredential(),
                                      "https://cognitiveservices.azure.com/.default")
    return AzureOpenAI(azure_ad_token_provider=token, **common)


SYSTEM_PROMPT = """You turn a health plan's chart-chase "service note" into FHIR search requests.
Return ONLY a JSON object:
{"measures": [...], "start": "YYYY-MM-DD", "end_exclusive": "YYYY-MM-DD",
 "documents": [{"code": "<LOINC>", "reason": "<short phrase from the note>"}],
 "observations": [{"code": "<LOINC>", "reason": "..."}], "notes": "<one sentence>"}
Rules:
- Use ONLY codes from the allowed lists. Never invent codes.
- documents = notes to download; observations = numeric results.
- Blood pressure -> 85354-9, 8480-6, 8462-4. HbA1c -> 4548-4, 17856-6, 4549-2, 17855-8. GMI -> 97506-0.
- If a result is requested, also include 11506-3.
- "All medical records" -> every general clinical-note code on the list.
- The demographic/biographical sheet is the Patient resource; no code for it."""


def _allowed_lists() -> str:
    d = "\n".join(f"{c}: {v['label']}" for c, v in DOCUMENT_CODES.items())
    o = "\n".join(f"{c}: {v['label']}" for c, v in OBSERVATION_CODES.items())
    return f"Allowed document codes:\n{d}\nAllowed observation codes:\n{o}"


def validate_llm_json(raw: dict, note: str, today: date | None = None) -> Plan:
    """Keep only allow-listed codes, known measures and sane dates."""
    fallback_start, fallback_end = date_window(note, today)
    start, end = raw.get("start"), raw.get("end_exclusive")
    try:
        if date.fromisoformat(end) <= date.fromisoformat(start):
            raise ValueError("end before start")
    except (TypeError, ValueError):
        start, end = fallback_start, fallback_end

    def items(key, allowed, label):
        out, seen = [], set()
        for it in raw.get(key) or []:
            code = str(it.get("code", "") if isinstance(it, dict) else it).strip()
            if code in allowed and code not in seen:
                seen.add(code)
                reason = str(it.get("reason", "")) if isinstance(it, dict) else ""
                out.append(PlanItem(code=code, label=label(code), reason=reason))
        return out

    measures = []
    for m in raw.get("measures") or []:
        if m in MEASURES and m not in measures:
            measures.append(m)

    return Plan(
        measures=measures, start=start, end=end,
        documents=items("documents", DOCUMENT_CODES, doc_label),
        observations=items("observations", OBSERVATION_CODES, obs_label),
        source="azure-openai",
        notes=str(raw.get("notes") or ""),
    )


def llm_plan(note: str, today: date | None = None) -> Plan:
    if not llm_configured():
        raise LlmError("Azure OpenAI is not configured: set AOAI_ENDPOINT and AOAI_DEPLOYMENT in .env")
    today = today or date.today()
    s = llm_settings()
    request = dict(
        model=s["deployment"],  # Azure: the deployment name
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT + "\n" + _allowed_lists()},
            {"role": "user", "content": f"Today is {today.isoformat()}.\nService note:\n{note}"},
        ],
    )
    if s["temperature"] is not None:
        request["temperature"] = s["temperature"]
    try:
        client = _client()
        try:
            resp = client.chat.completions.create(**request)
        except Exception as exc:
            # gpt-5 / o-series deployments reject a temperature other than the default.
            if "temperature" not in str(exc).lower() or "temperature" not in request:
                raise
            request.pop("temperature")
            resp = client.chat.completions.create(**request)
        raw = json.loads(resp.choices[0].message.content or "{}")
    except LlmError:
        raise
    except Exception as exc:
        raise LlmError(f"Azure OpenAI call failed ({type(exc).__name__}): {exc}") from exc
    return validate_llm_json(raw, note, today)


def make_plan(note: str, use_llm: bool = True) -> Plan:
    return llm_plan(note) if use_llm else rules_plan(note)
