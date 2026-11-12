"""Service note -> clinical intent -> executable plan.

The agent (Azure OpenAI, or keyword rules when it is turned off) returns a
ClinicalIntent: measure codes, mode, date windows, resource groups with a purpose
and a required flag, clinical targets and a fallback strategy. The intent is then
validated against the vocabulary in doc_types.py and turned into a Plan: the
LOINC codes to search, the DiagnosticReport categories, and the date windows.

With use_llm on, any failure is raised as LlmError; there is no silent fallback.
Nothing the model returns reaches Cerner without passing through clean_intent().
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta

from agent.doc_types import (ALL_RECORDS_PHRASES, DATE_WINDOW_PURPOSES, DOCUMENT_CODES,
                             FALLBACK_PHRASES, FALLBACK_STRATEGIES, MEASURES, MODES, PURPOSES,
                             RULES, TARGET_KINDS, TARGET_RULES, doc_label, obs_label)
from agent.redact import RedactionError, redact
from common.models import (ClinicalIntent, ClinicalTarget, DateWindow, Plan, PlanItem,
                           ResourceGroup)


from agent.llm import LlmError, chat_json, get_model  # noqa: E402  (LlmError: not configured or the call failed)


# Words that let the job envelope run to today instead of stopping at the last year named.
OPEN_ENDED_PHRASES = ["ytd", "year to date", "to date", "through present", "to present", "current"]

# Measure components that can't be skipped: the group is required when the measure is named.
MEASURE_REQUIRED: dict[str, set[str]] = {
    "TRC": {"admission", "discharge_summary", "discharge_instructions", "medication_reconciliation", "progress_notes"},
    "GSD": {"lab_report", "hba1c_or_gmi"},
    "CBP": {"blood_pressure"},
    "BPD": {"blood_pressure"},
}


# ---------- small deterministic helpers ----------

def date_window(note: str, today: date | None = None) -> tuple[str, str]:
    """Jan 1 of the earliest 20xx year in the note to Jan 1 after the latest, capped at tomorrow.
    Returns (start, end_exclusive)."""
    today = today or date.today()
    years = sorted({int(y) for y in re.findall(r"\b(20\d{2})\b", note)}) or [today.year]
    start = date(years[0], 1, 1)
    end = min(date(years[-1] + 1, 1, 1), today + timedelta(days=1))
    if end <= start:
        end = today + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def find_measures(note: str) -> list[str]:
    return [m for m in MEASURES if re.search(rf"\b{m}\b", note)]


def wants_all_records(note: str) -> bool:
    text = note.lower()
    return any(p in text for p in ALL_RECORDS_PHRASES)


def note_excerpt(note: str, limit: int = 100) -> str:
    text = " ".join(note.split())
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _day(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def default_windows(note: str, measures: list[str], today: date) -> dict[str, DateWindow]:
    start, end_excl = date_window(note, today)
    to = date.fromisoformat(end_excl) - timedelta(days=1)
    windows = {"job_envelope": DateWindow(purpose="job_envelope", from_=start, to=min(to, today).isoformat())}
    if measures:
        years = sorted({int(y) for y in re.findall(r"\b(20\d{2})\b", note)}) or [today.year]
        y = years[-1]
        windows["measurement_year"] = DateWindow(purpose="measurement_year", from_=date(y, 1, 1).isoformat(),
                                                 to=min(date(y, 12, 31), today).isoformat())
    return windows


# ---------- intent validation (never trust the model) ----------

def clean_intent(raw: dict, note: str, today: date | None = None) -> tuple[ClinicalIntent, list[str]]:
    """Coerce a raw intent dict into a valid ClinicalIntent. Returns (intent, remarks)."""
    today = today or date.today()
    remarks: list[str] = []
    raw = raw if isinstance(raw, dict) else {}

    measures: list[str] = []
    for m in [*(raw.get("measure_codes") or []), *find_measures(note)]:
        m = str(m).strip().upper()
        if m in MEASURES and m not in measures:
            measures.append(m)

    mode = str(raw.get("mode") or "")
    if wants_all_records(note):
        mode = "all_records"
    elif mode not in MODES:
        mode = "measure_specific" if measures else "general"

    # Date windows: valid purpose, real dates, from <= to, nothing after today.
    defaults = default_windows(note, measures, today)
    windows: dict[str, DateWindow] = {}
    for w in raw.get("date_windows") or []:
        if not isinstance(w, dict) or w.get("purpose") not in DATE_WINDOW_PURPOSES or w["purpose"] in windows:
            continue
        start, end = _day(w.get("from")), _day(w.get("to"))
        if not start or not end:
            remarks.append(f"Ignored an invalid {w.get('purpose')} window.")
            continue
        end = min(end, today)
        if end < start:
            remarks.append(f"Ignored the {w['purpose']} window: it ends before it starts.")
            continue
        windows[w["purpose"]] = DateWindow(purpose=w["purpose"], from_=start.isoformat(), to=end.isoformat())
    for purpose, window in defaults.items():
        windows.setdefault(purpose, window)
    # The job envelope only runs past the last year the note names when the note says so (YTD, to date...).
    years = sorted({int(y) for y in re.findall(r"\b(20\d{2})\b", note)})
    open_ended = any(p in note.lower() for p in OPEN_ENDED_PHRASES)
    job = windows["job_envelope"]
    if years and not open_ended and date.fromisoformat(job.to).year > years[-1]:
        windows["job_envelope"] = DateWindow(purpose="job_envelope", from_=job.from_,
                                             to=min(date(years[-1], 12, 31), today).isoformat())

    # Resource groups: only known resource/purpose pairs; Patient demographics always required.
    groups: dict[tuple[str, str], ResourceGroup] = {("Patient", "demographics"): ResourceGroup(
        resource="Patient", purpose="demographics", required=True)}
    for g in raw.get("resource_groups") or []:
        if not isinstance(g, dict):
            continue
        resource, purpose = str(g.get("resource") or ""), str(g.get("purpose") or "")
        if purpose not in PURPOSES.get(resource, {}):
            remarks.append(f"Dropped unknown group {resource or '?'}/{purpose or '?'}.")
            continue
        key = (resource, purpose)
        required = bool(g.get("required")) or groups.get(key, ResourceGroup(resource=resource, purpose=purpose)).required
        groups[key] = ResourceGroup(resource=resource, purpose=purpose, required=required)
    # For a named measure, which groups are required is fixed by the measure, not left to the model.
    if mode == "measure_specific" and measures:
        must = set().union(*(MEASURE_REQUIRED.get(m, set()) for m in measures))
        for key, g in groups.items():
            g.required = g.resource == "Patient" or g.purpose in must
    if mode == "all_records":
        groups.setdefault(("DocumentReference", "all_clinical_notes"),
                          ResourceGroup(resource="DocumentReference", purpose="all_clinical_notes"))

    # Clinical targets: known kinds; an Observation group exists for each target and vice versa.
    targets: dict[str, ClinicalTarget] = {}
    for t in raw.get("clinical_targets") or []:
        if not isinstance(t, dict) or t.get("kind") not in TARGET_KINDS or t["kind"] in targets:
            continue
        through = _day(t.get("through"))
        targets[t["kind"]] = ClinicalTarget(kind=t["kind"], any_history=bool(t.get("any_history")),
                                            through=min(through, today).isoformat() if through else None)
    text = note.lower()
    for key in list(groups):
        if key == ("Observation", "hba1c_or_gmi"):
            targets.setdefault("hba1c", ClinicalTarget(kind="hba1c"))
            if "gmi" in text or "glucose management" in text:
                targets.setdefault("gmi", ClinicalTarget(kind="gmi"))
        elif key == ("Observation", "blood_pressure"):
            targets.setdefault("blood_pressure", ClinicalTarget(kind="blood_pressure"))
    for kind in targets:
        purpose = "blood_pressure" if kind == "blood_pressure" else "hba1c_or_gmi"
        groups.setdefault(("Observation", purpose), ResourceGroup(resource="Observation", purpose=purpose, required=True))
    # A "through" date at or after the end of the window adds nothing: keep it null.
    window_end = (windows.get("measurement_year") or windows["job_envelope"]).to
    for t in targets.values():
        if t.through and t.through >= window_end:
            t.through = None
    # Results are usually evidenced in the visit note that shows the provider.
    if any(r == "Observation" for r, _ in groups):
        groups.setdefault(("DocumentReference", "progress_notes"),
                          ResourceGroup(resource="DocumentReference", purpose="progress_notes"))

    fallback = str(raw.get("fallback_strategy") or "")
    if any(p in text for p in FALLBACK_PHRASES):
        fallback = "progress_notes_if_missing"
    elif fallback not in FALLBACK_STRATEGIES:
        fallback = "broad" if mode == "all_records" else "strict"
    if fallback == "progress_notes_if_missing":
        groups.setdefault(("DocumentReference", "progress_notes"),
                          ResourceGroup(resource="DocumentReference", purpose="progress_notes"))

    intent = ClinicalIntent(
        measure_codes=measures, mode=mode,
        date_windows=[windows[p] for p in DATE_WINDOW_PURPOSES if p in windows],
        resource_groups=list(groups.values()), clinical_targets=list(targets.values()),
        fallback_strategy=fallback, notes=note_excerpt(note),
    )
    return intent, remarks


# ---------- intent -> executable plan ----------

def plan_from_intent(intent: ClinicalIntent, source: str, reasons: dict[str, str] | None = None,
                     remarks: list[str] | None = None, today: date | None = None) -> Plan:
    today = today or date.today()
    reasons = reasons or {}
    tomorrow = today + timedelta(days=1)
    windows = {w.purpose: w for w in intent.date_windows}
    job = windows["job_envelope"]
    start = job.from_
    end = min(date.fromisoformat(job.to) + timedelta(days=1), tomorrow).isoformat()

    def reason(group: ResourceGroup) -> str:
        info = PURPOSES[group.resource][group.purpose]
        why = reasons.get(group.purpose) or info["label"]
        return f"{group.purpose}{' (required)' if group.required else ''}: {why}"

    documents: list[PlanItem] = []
    seen: set[str] = set()
    for g in intent.resource_groups:
        if g.resource != "DocumentReference":
            continue
        for code in PURPOSES[g.resource][g.purpose]["codes"]:
            if code in DOCUMENT_CODES and code not in seen:
                seen.add(code)
                documents.append(PlanItem(code=code, label=doc_label(code), reason=reason(g),
                                          group=g.purpose, required=g.required))

    observations: list[PlanItem] = []
    obs_groups = {g.purpose: g for g in intent.resource_groups if g.resource == "Observation"}
    for t in intent.clinical_targets:
        g = obs_groups.get("blood_pressure" if t.kind == "blood_pressure" else "hba1c_or_gmi")
        for code in TARGET_KINDS[t.kind]:
            if code not in {o.code for o in observations}:
                observations.append(PlanItem(code=code, label=obs_label(code), reason=f"{t.kind} result",
                                             group=g.purpose if g else t.kind, required=bool(g and g.required)))

    reports = [PlanItem(code=code, label=PURPOSES[g.resource][g.purpose]["label"] + " (DiagnosticReport)",
                        reason=reason(g), group=g.purpose, required=g.required)
               for g in intent.resource_groups if g.resource == "DiagnosticReport"
               for code in PURPOSES[g.resource][g.purpose]["codes"]]

    encounters = [PlanItem(code=g.purpose, label=PURPOSES[g.resource][g.purpose]["label"],
                           reason=reason(g), group=g.purpose, required=g.required)
                  for g in intent.resource_groups if g.resource == "Encounter"]

    # Results window: the measurement year when there is one; any_history removes the lower bound.
    obs_window = windows.get("measurement_year") or job
    obs_start: str | None = obs_window.from_
    obs_end = min(date.fromisoformat(obs_window.to) + timedelta(days=1), tomorrow)
    if intent.clinical_targets and any(t.any_history for t in intent.clinical_targets):
        obs_start = None
    throughs = [date.fromisoformat(t.through) for t in intent.clinical_targets if t.through]
    if throughs and len(throughs) == len(intent.clinical_targets):
        obs_end = min(obs_end, max(throughs) + timedelta(days=1))

    return Plan(
        measures=intent.measure_codes, start=start, end=end,
        documents=documents, observations=observations, reports=reports, encounters=encounters,
        obs_start=obs_start, obs_end=obs_end.isoformat(),
        source=source, notes=" ".join(remarks or []), all_records=intent.mode == "all_records",
        intent=intent,
    )


# ---------- keyword rules (LLM off) ----------

def rules_intent(note: str, today: date | None = None) -> tuple[ClinicalIntent, dict[str, str], list[str]]:
    """Build the same intent schema from keyword rules. Returns (intent, reasons, remarks)."""
    text = note.lower()
    measures = find_measures(note)
    required_purposes = set().union(*(MEASURE_REQUIRED.get(m, set()) for m in measures))
    groups, reasons = [], {}
    for phrases, resource, purpose in RULES:
        hit = next((p for p in phrases if p in text), None)
        if hit and purpose not in reasons:
            reasons[purpose] = f'note mentions "{hit}"'
            groups.append({"resource": resource, "purpose": purpose, "required": purpose in required_purposes})
    if wants_all_records(note):
        reasons["all_clinical_notes"] = "all-records request"
        groups.append({"resource": "DocumentReference", "purpose": "all_clinical_notes", "required": False})
    targets = [{"kind": kind, "any_history": False, "through": None}
               for phrases, kind in TARGET_RULES if any(p in text for p in phrases)]
    raw = {"measure_codes": measures, "resource_groups": groups, "clinical_targets": targets}
    intent, remarks = clean_intent(raw, note, today)
    return intent, reasons, remarks


def rules_plan(note: str, today: date | None = None) -> Plan:
    intent, reasons, remarks = rules_intent(note, today)
    return plan_from_intent(intent, "rules", reasons, remarks, today)


# ---------- Azure OpenAI ----------

def _env(name: str, default: str = "") -> str:
    """Read an env var, treating .env.example placeholders like <your-resource> as unset."""
    value = os.getenv(name, default).strip()
    return "" if "<" in value else value


def llm_configured() -> bool:
    return bool(_env("AOAI_ENDPOINT") and _env("AOAI_DEPLOYMENT"))


def llm_settings(model: str | None = None) -> dict:
    """The knobs you can change in .env to try different models or behaviour."""
    temperature = _env("AOAI_TEMPERATURE", "0")
    return {
        "deployment": model or _env("AOAI_DEPLOYMENT"),
        "apiVersion": "v1",
        "temperature": float(temperature) if temperature else None,
        "timeout": float(_env("AOAI_TIMEOUT", "30") or 30),
    }


SYSTEM_PROMPT = """You turn a health plan's chart-chase "service note" into a clinical retrieval intent for a FHIR (Cerner) record pull.
Return ONLY a JSON object with exactly these keys:
{
  "measure_codes": ["TRC" | "CBP" | "BPD" | "GSD", ...],
  "mode": "measure_specific" | "all_records" | "general",
  "date_windows": [{"purpose": "job_envelope" | "measurement_year", "from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}],
  "resource_groups": [{"resource": "Patient" | "DocumentReference" | "Observation" | "DiagnosticReport" | "Encounter", "purpose": "<allowed purpose>", "required": true | false}],
  "clinical_targets": [{"kind": "hba1c" | "gmi" | "blood_pressure", "any_history": false, "through": null}],
  "fallback_strategy": "strict" | "title_match" | "broad" | "progress_notes_if_missing",
  "notes": "<the service note, shortened>"
}
Rules:
- mode: "measure_specific" when the note names a measure; "all_records" when it asks for all or complete medical records; otherwise "general".
- date_windows: always one "job_envelope" covering every date records are requested for ("from" and "to" inclusive). It ends on Dec 31 of the last year the note names; only for YTD / "to date" requests does it end today. When a measure is named, add "measurement_year": Jan 1 to Dec 31 of that measure's year (not after today).
- resource_groups: always {"resource": "Patient", "purpose": "demographics", "required": true}. Use only the purposes listed below, each under its own resource. One group per purpose.
- required = true when the measure cannot be met without that evidence; supporting records are false. Required by measure: TRC -> admission, discharge_summary, discharge_instructions, medication_reconciliation, progress_notes; GSD -> lab_report and Observation hba1c_or_gmi; CBP and BPD -> Observation blood_pressure.
- Admissions, hospital stays, inpatient records -> Encounter inpatient_stays (plus the admission documents); outpatient records -> Encounter outpatient_visits; ED visits -> Encounter emergency_visits. Encounters give the facility of each stay.
- Lab results requested -> DiagnosticReport lab_report. HbA1c or GMI -> Observation hba1c_or_gmi; blood pressure -> Observation blood_pressure. Add progress_notes when a result is requested (the visit note shows the provider).
- clinical_targets: one per numeric result requested. any_history = true only if a result from any date is acceptable; "through" = a cut-off date if the note gives one, else null.
- fallback_strategy: "strict" by default; "title_match" if the note accepts any similar document; "broad" for all-records requests; "progress_notes_if_missing" when the note says to send all progress notes (with a notation of what is missing) if required items are not in the record.
- The demographic / biographical sheet is the Patient resource."""


def _allowed_purposes() -> str:
    lines = ["Allowed purposes (resource: purpose - what it covers):"]
    for resource, purposes in PURPOSES.items():
        for purpose, info in purposes.items():
            lines.append(f"{resource}: {purpose} - {info['label']}")
    return "\n".join(lines)


def build_messages(note: str, today: date | None = None) -> list[dict]:
    """The exact chat messages sent to Azure OpenAI for a service note (PII/PHI redacted first)."""
    today = today or date.today()
    try:
        note = redact(note)
    except RedactionError as exc:
        raise LlmError(str(exc)) from exc
    return [
        {"role": "system", "content": SYSTEM_PROMPT + "\n" + _allowed_purposes()},
        {"role": "user", "content": f"Today is {today.isoformat()}.\nService note:\n{note}"},
    ]


def validate_llm_json(raw: dict, note: str, today: date | None = None) -> Plan:
    """The model's intent JSON -> a checked intent -> the executable plan."""
    intent, remarks = clean_intent(raw, note, today)
    return plan_from_intent(intent, "azure-openai", remarks=remarks, today=today)


def llm_plan(note: str, today: date | None = None, model: str | None = None) -> Plan:
    if not llm_configured():
        raise LlmError("Azure OpenAI is not configured: set AOAI_ENDPOINT and AOAI_DEPLOYMENT in .env")
    today = today or date.today()
    s = llm_settings(model)
    m = get_model(s["deployment"])
    raw, usage = chat_json(m, build_messages(note, today), s["temperature"], s["timeout"])
    plan = validate_llm_json(raw, note, today)
    plan.model = m.deployment
    plan.usage = usage
    return plan


def make_plan(note: str, use_llm: bool = True, model: str | None = None) -> Plan:
    return llm_plan(note, model=model) if use_llm else rules_plan(note)
