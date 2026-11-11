"""Worker: run a plan against Cerner and build the zip packet in memory.

run_job_events() one DocumentReference search per LOINC code (+ optional title
                 sweep) and one Observation search, yielding progress as it goes
handle_job()     the same, returning only the final result
build_packet()  download every non-duplicate note and zip it with the
                biographical and missing-items sheets; nothing touches disk
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import zipfile
from datetime import datetime
from urllib.parse import urlparse

from agent.doc_types import DOCUMENT_CODES
from common.models import Plan, RunRequest
from worker.fhir_client import FhirClient, FhirError, doc_summary, obs_summary, patient_summary

CONCURRENCY = int(os.getenv("FHIR_CONCURRENCY", "2") or 2)  # LOINC searches in flight at once
# Binary downloads in flight at once. Each sandbox Binary call takes ~4 s whatever its size,
# so downloads need more parallelism than searches; FHIR_MAX_RPS still caps the request rate.
DOWNLOAD_CONCURRENCY = int(os.getenv("FHIR_DOWNLOAD_CONCURRENCY", "10") or 10)
EXT = {"application/pdf": "pdf", "text/html": "html", "application/xhtml+xml": "html",
       "text/plain": "txt", "application/xml": "xml", "text/xml": "xml",
       "text/rtf": "rtf", "application/rtf": "rtf", "image/jpeg": "jpg", "image/png": "png",
       "image/tiff": "tif"}


def us_date(iso: str | None) -> str:
    """YYYY-MM-DD -> MM/DD/YYYY for the human-readable sheets (file names stay ISO so they sort)."""
    if iso and len(iso) >= 10 and iso[4] == "-" and iso[7] == "-":
        return f"{iso[5:7]}/{iso[8:10]}/{iso[:4]}"
    return iso or ""


def safe(s: str, limit: int = 60) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s or "").strip("-")[:limit]


def file_name(doc: dict, fallback_title: str) -> str:
    ext = EXT.get((doc.get("contentType") or "").split(";")[0], "bin")
    title = safe(doc.get("title") or fallback_title) or "note"
    return f"{doc.get('date') or 'undated'}_{title}_{safe(doc.get('id') or '')}.{ext}"


def binary_allowed(url: str, fhir_base: str) -> bool:
    """Only fetch Cerner Binary URLs, so the download endpoints are not an open proxy."""
    try:
        u = urlparse(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    if "/Binary/" not in u.path or not host:
        return False
    if host == (urlparse(fhir_base).hostname or "").lower():  # the configured tenant (or local mock)
        return u.scheme in ("https", "http")
    return u.scheme == "https" and (host.endswith(".cerner.com") or host.endswith(".oraclecloud.com"))


# ---------- the job ----------

async def run_job_events(c: FhirClient, req: RunRequest):
    """Run the plan and yield progress events as each LOINC call finishes.

    {"type": "start", "patient", "window", "codes", "observations", "titleSweep"}
    {"type": "row", "row"}                       one per LOINC call, in completion order
    {"type": "sweep", "rows"}                    rows after the title-match sweep
    {"type": "observations", "observations", "observationError"}
    {"type": "done", "result"}                   the same dict /api/run returns
    """
    p = req.plan
    start, end = (p.start, p.end) if req.apply_dates else (None, None)
    patient = patient_summary(await c.get_patient(req.patient_id))
    yield {"type": "start", "patient": patient, "window": {"start": start, "end": end},
           "codes": [{"code": i.code, "label": i.label} for i in p.documents],
           "observations": bool(p.observations), "titleSweep": bool(req.title_fallback and p.documents)}

    sem = asyncio.Semaphore(max(1, CONCURRENCY))

    async def by_code(item):
        async with sem:
            try:
                docs = await c.documents_by_loinc(req.patient_id, item.code, start, end)
                kept = [d for d in docs if d.get("status") != "entered-in-error"]
                return item, kept, None, docs
            except FhirError as e:
                return item, [], str(e), None

    seen: set[str] = set()
    rows_by_code: dict[str, dict] = {}
    for next_done in asyncio.as_completed([by_code(i) for i in p.documents]):
        item, docs, err, raw = await next_done
        summaries = []
        for d in docs:
            s = doc_summary(d)
            s["matchedBy"] = "loinc"
            s["duplicate"] = s["id"] in seen  # first code to finish keeps the note
            s["file"] = file_name(s, item.label)
            seen.add(s["id"])
            summaries.append(s)
        row = {"code": item.code, "label": item.label, "reason": item.reason,
               "count": len(summaries), "error": err, "docs": summaries,
               "truncated": getattr(raw, "truncated", False), "request": getattr(raw, "request_url", "")}
        rows_by_code[item.code] = row
        yield {"type": "row", "row": row}

    rows = [rows_by_code[i.code] for i in p.documents]  # back in plan order

    if req.title_fallback and rows:
        try:
            for d in await c.documents_by_category(req.patient_id, start, end):
                if d.get("id") in seen or d.get("status") == "entered-in-error":
                    continue
                s = doc_summary(d)
                title = (s["title"] or "").lower()
                row = next((r for r in rows
                            if any(k in title for k in DOCUMENT_CODES.get(r["code"], {}).get("title_keywords", []))),
                           None)
                if row:
                    s.update(matchedBy="title", duplicate=False, file=file_name(s, row["label"]))
                    row["docs"].append(s)
                    row["count"] += 1
                    seen.add(s["id"])
        except FhirError as e:
            rows.append({"code": "clinical-note", "label": "Title-match sweep", "reason": "",
                         "count": 0, "error": str(e), "docs": []})
        yield {"type": "sweep", "rows": rows}

    observations, obs_error, obs_truncated = [], None, False
    if p.observations:
        try:
            obs = await c.observations(req.patient_id, [o.code for o in p.observations], start, end)
            observations = sorted((obs_summary(o) for o in obs), key=lambda o: o["date"], reverse=True)
            obs_truncated = getattr(obs, "truncated", False)
        except FhirError as e:
            obs_error = str(e)
        yield {"type": "observations", "observations": observations, "observationError": obs_error,
               "observationsTruncated": obs_truncated}

    yield {"type": "done", "result": {
        "patient": patient, "window": {"start": start, "end": end},
        "rows": rows, "observations": observations, "observationError": obs_error,
        "observationsTruncated": obs_truncated,
        "missing": [r["code"] for r in rows if r["count"] == 0 and not r["error"]]}}


async def handle_job(c: FhirClient, req: RunRequest) -> dict:
    """Run the whole plan and return the final result (no progress events)."""
    result = {}
    async for event in run_job_events(c, req):
        if event["type"] == "done":
            result = event["result"]
    return result


# ---------- the packet ----------

def bio_sheet(pt: dict) -> str:
    lines = ["MEMBER BIOGRAPHICAL INFORMATION SHEET", "",
             f"Name:          {pt.get('name', '')}", f"Date of birth: {us_date(pt.get('birthDate'))}",
             f"Sex:           {pt.get('gender', '')}", f"Address:       {pt.get('address', '')}",
             f"Phone:         {pt.get('phone', '')}", f"Patient ID:    {pt.get('id', '')}"]
    lines += [f"Identifier:    {i}" for i in pt.get("identifiers", [])]
    lines += ["", f"Generated {datetime.now():%m/%d/%Y %H:%M} from FHIR Patient/{pt.get('id', '')}"]
    return "\n".join(lines) + "\n"


def missing_sheet(pt: dict, plan: Plan, result: dict) -> str:
    w = result.get("window") or {}
    window = f"{us_date(w['start'])} to before {us_date(w['end'])}" if w.get("start") else "no date filter"
    lines = [f"Member: {pt.get('name', '')} | DOB {us_date(pt.get('birthDate'))} | Patient/{pt.get('id', '')}",
             f"Measures: {', '.join(plan.measures) or 'generic'} | Window: {window}", ""]
    for r in result.get("rows", []):
        if r.get("error"):
            status = f"ERROR: {r['error']}"
        elif r.get("count"):
            status = f"{r['count']} found" + (" (more exist; raise FHIR_MAX_PAGES or narrow the window)" if r.get("truncated") else "")
        else:
            status = "NOT in this record"
        lines.append(f"  {r.get('label')} (LOINC {r.get('code')}): {status}")
    if plan.observations:
        obs = result.get("observations") or []
        codes = ", ".join(o.code for o in plan.observations)
        if result.get("observationError"):
            latest = f"ERROR: {result['observationError']}"
        elif obs:
            latest = (f"latest {obs[0].get('value')} on {us_date(obs[0].get('date'))} "
                      "(check it isn't an ED, inpatient or procedure-day reading)")
        else:
            latest = "NONE found"
        lines += ["", f"  Results ({codes}): {latest}"]
    return "\n".join(lines) + "\n"


def packet_docs(result: dict) -> dict[str, dict]:
    """The notes that go in the packet, keyed by file name (duplicates and file-less notes skipped)."""
    out: dict[str, dict] = {}
    for r in result.get("rows", []):
        for d in r.get("docs", []):
            if d.get("duplicate") or not d.get("binaryUrl"):
                continue
            out.setdefault(d.get("file") or file_name(d, r.get("label", "note")), d)
    return out


def zip_name_for(result: dict) -> str:
    pt = result.get("patient") or {}
    return f"{safe(pt.get('name', 'member')) or 'member'}_{pt.get('birthDate') or 'nodob'}_packet.zip"


async def build_packet(c: FhirClient, plan: Plan, result: dict, on_status=None) -> tuple[io.BytesIO, str, int]:
    """Download every packet note and zip it with the sheets. Returns (zip buffer, file name, failures).

    on_status(file_name, status, error) is called as each file moves through
    "downloading" -> "done" | "failed", so a caller can show per-file progress.
    """
    pt = result.get("patient") or {}
    docs = packet_docs(result)
    sem = asyncio.Semaphore(max(1, DOWNLOAD_CONCURRENCY))
    errors: list[str] = []
    report = on_status or (lambda name, status, error=None: None)

    async def fetch(name: str, d: dict):
        if not binary_allowed(d["binaryUrl"], c.base):
            errors.append(f"{name}: URL not allowed")
            report(name, "failed", "URL not allowed")
            return None
        async with sem:
            report(name, "downloading")
            try:
                data, _ = await c.download_binary(d["binaryUrl"], d.get("contentType"))
            except Exception as e:  # keep going; list it in download-errors.txt
                message = str(e)[:300]
                errors.append(f"{name}: {message}")
                report(name, "failed", message)
                return None
            report(name, "done")
            return name, data

    files = [f for f in await asyncio.gather(*(fetch(n, d) for n, d in docs.items())) if f]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("00_biographical-sheet.txt", bio_sheet(pt))
        for name, data in sorted(files):
            z.writestr(name, data)
        z.writestr("missing-items.txt", missing_sheet(pt, plan, result))
        if errors:
            z.writestr("download-errors.txt", "\n".join(errors) + "\n")
    buf.seek(0)
    return buf, zip_name_for(result), len(errors)
