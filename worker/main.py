"""Worker: run a plan against Cerner and build the zip packet in memory.

handle_job()    one DocumentReference search per LOINC code (+ optional title
                sweep) and one Observation search -> per-code result rows
build_packet()  download every non-duplicate note and zip it with the
                biographical and missing-items sheets; nothing touches disk
"""
from __future__ import annotations

import asyncio
import io
import re
import zipfile
from datetime import datetime
from urllib.parse import urlparse

from agent.doc_types import DOCUMENT_CODES
from common.models import Plan, RunRequest
from worker.fhir_client import FhirClient, FhirError, doc_summary, obs_summary, patient_summary

CONCURRENCY = 4
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

async def handle_job(c: FhirClient, req: RunRequest) -> dict:
    p = req.plan
    start, end = (p.start, p.end) if req.apply_dates else (None, None)
    sem = asyncio.Semaphore(CONCURRENCY)
    patient = patient_summary(await c.get_patient(req.patient_id))

    async def by_code(item):
        async with sem:
            try:
                docs = await c.documents_by_loinc(req.patient_id, item.code, start, end)
                return item, [d for d in docs if d.get("status") != "entered-in-error"], None
            except FhirError as e:
                return item, [], str(e)

    seen: set[str] = set()
    rows = []
    for item, docs, err in await asyncio.gather(*(by_code(i) for i in p.documents)):
        summaries = []
        for d in docs:
            s = doc_summary(d)
            s["matchedBy"] = "loinc"
            s["duplicate"] = s["id"] in seen
            s["file"] = file_name(s, item.label)
            seen.add(s["id"])
            summaries.append(s)
        rows.append({"code": item.code, "label": item.label, "reason": item.reason,
                     "count": len(summaries), "error": err, "docs": summaries})

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

    observations, obs_error = [], None
    if p.observations:
        try:
            obs = await c.observations(req.patient_id, [o.code for o in p.observations], start, end)
            observations = sorted((obs_summary(o) for o in obs), key=lambda o: o["date"], reverse=True)
        except FhirError as e:
            obs_error = str(e)

    return {"patient": patient, "window": {"start": start, "end": end},
            "rows": rows, "observations": observations, "observationError": obs_error,
            "missing": [r["code"] for r in rows if r["count"] == 0 and not r["error"]]}


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
            status = f"{r['count']} found"
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


async def build_packet(c: FhirClient, plan: Plan, result: dict) -> tuple[io.BytesIO, str, int]:
    """Return (zip buffer, file name, number of failed downloads)."""
    pt = result.get("patient") or {}
    docs = [d for r in result.get("rows", []) for d in r.get("docs", [])
            if not d.get("duplicate") and d.get("binaryUrl")]
    sem = asyncio.Semaphore(CONCURRENCY)
    errors: list[str] = []

    async def fetch(d):
        name = d.get("file") or file_name(d, "note")
        if not binary_allowed(d["binaryUrl"], c.base):
            errors.append(f"{name}: URL not allowed")
            return None
        async with sem:
            try:
                data, _ = await c.download_binary(d["binaryUrl"], d.get("contentType"))
                return name, data
            except Exception as e:  # keep going; list it in download-errors.txt
                errors.append(f"{name}: {str(e)[:300]}")
                return None

    files = [f for f in await asyncio.gather(*(fetch(d) for d in docs)) if f]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("00_biographical-sheet.txt", bio_sheet(pt))
        used: set[str] = set()
        for name, data in sorted(files):
            if name in used:  # the same note can be listed twice; keep one copy
                continue
            used.add(name)
            z.writestr(name, data)
        z.writestr("missing-items.txt", missing_sheet(pt, plan, result))
        if errors:
            z.writestr("download-errors.txt", "\n".join(errors) + "\n")
    buf.seek(0)

    zip_name = f"{safe(pt.get('name', 'member')) or 'member'}_{pt.get('birthDate') or 'nodob'}_packet.zip"
    return buf, zip_name, len(errors)
