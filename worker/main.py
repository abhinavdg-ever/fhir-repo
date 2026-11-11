"""Worker: run a plan against Cerner and build the zip packet in memory.

run_job_events() one DocumentReference search per LOINC code (+ optional title
                 sweep) and one Observation search, yielding progress as it goes
handle_job()     the same, returning only the final result
build_packet()  download every non-duplicate note and zip it with the
                biographical and missing-items sheets; nothing touches disk
"""
from __future__ import annotations

import asyncio
import difflib
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


# ---------- title matching ----------

TITLE_MATCH_MIN = float(os.getenv("TITLE_MATCH_MIN", "0.82"))  # 0-1; 1 = keyword must appear exactly


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9&]+", " ", (text or "").lower()).split())


def title_score(title: str, code: str) -> float:
    """How well a note title fits a document code: 1.0 when one of its keywords appears,
    otherwise the best fuzzy similarity of a keyword (or the label) to a run of the same
    number of words in the title, so "Nursing Narr" or "Discharge Sumary" still match."""
    entry = DOCUMENT_CODES.get(code)
    t = _norm(title)
    if not entry or not t:
        return 0.0
    keywords = [_norm(k) for k in entry.get("title_keywords", [])]
    if any(k and k in t for k in keywords):
        return 1.0
    words = t.split()
    best = 0.0
    for phrase in keywords + [_norm(entry["label"])]:
        if len(phrase) < 6:  # short keywords like "h&p" only count as exact matches
            continue
        n = len(phrase.split())
        for i in range(max(1, len(words) - n + 1)):
            window = " ".join(words[i:i + n])
            best = max(best, difflib.SequenceMatcher(None, phrase, window).ratio())
    return round(min(best, 0.99), 2)  # 1.0 is reserved for exact keyword hits


def best_title_match(title: str, codes: list[str]) -> tuple[str | None, float]:
    scored = [(title_score(title, c), c) for c in codes]
    score, code = max(scored, default=(0.0, None))
    return (code, score) if score >= TITLE_MATCH_MIN else (None, score)


def group_key(doc: dict) -> tuple[str, str, str | None]:
    """(key, label, loinc) for grouping "other" records by document type."""
    if doc.get("loinc"):
        code = doc["loinc"][0]
        return code, DOCUMENT_CODES.get(code, {}).get("label") or doc.get("title") or code, code
    if doc.get("localCodes"):
        local = doc["localCodes"][0].split(" ", 1)[0]  # "code display" -> code
        return f"local:{local}", doc.get("title") or local, None
    return f"title:{_norm(doc.get('title')) or 'untitled'}", doc.get("title") or "Untitled", None


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

    # One search for everything the patient has: notes no plan code found are either
    # matched to a plan code by title, or listed as "other records" (not downloaded).
    other_groups: list[dict] = []
    other_info = {"error": None, "truncated": False, "request": ""}
    if (req.title_fallback and rows) or req.discover:
        by_code = {r["code"]: r for r in rows}
        groups: dict[str, dict] = {}
        try:
            everything = await c.documents_all(req.patient_id, start, end)
            other_info.update(truncated=getattr(everything, "truncated", False),
                              request=getattr(everything, "request_url", ""))
            for d in everything:
                if d.get("id") in seen or d.get("status") == "entered-in-error":
                    continue
                s = doc_summary(d)
                # Title matching is for notes Cerner never coded with LOINC; a note that has a
                # different LOINC code is a different document type and goes to "other".
                if req.title_fallback and rows and not s.get("loinc"):
                    code, score = best_title_match(s["title"], list(by_code))
                    if code:
                        row = by_code[code]
                        s.update(matchedBy="title", matchScore=score, duplicate=False, file=file_name(s, row["label"]))
                        row["docs"].append(s)
                        row["count"] += 1
                        seen.add(s["id"])
                        continue
                if req.discover:
                    key, label, loinc = group_key(s)
                    s.update(matchedBy="other", duplicate=False, file=file_name(s, label))
                    g = groups.setdefault(key, {"key": key, "label": label, "loinc": loinc,
                                                "inCatalog": loinc in DOCUMENT_CODES, "docs": []})
                    g["docs"].append(s)
                    seen.add(s["id"])
        except FhirError as e:
            other_info["error"] = str(e)
        for g in groups.values():
            dates = sorted(d["date"] for d in g["docs"] if d.get("date"))
            g.update(count=len(g["docs"]), first=dates[0] if dates else None, last=dates[-1] if dates else None)
            g["docs"].sort(key=lambda d: d.get("date") or "", reverse=True)
        other_groups = sorted(groups.values(), key=lambda g: -g["count"])
        yield {"type": "sweep", "rows": rows}
        if req.discover:
            yield {"type": "other", "groups": other_groups, **other_info}

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
        "other": other_groups, "otherError": other_info["error"], "otherTruncated": other_info["truncated"],
        "otherRequest": other_info["request"],
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
        if r.get("extra"):  # other records the reviewer added to the packet
            lines.append(f"  {r.get('label')}: {r.get('count', 0)} added to this packet")
            continue
        if r.get("error"):
            status = f"ERROR: {r['error']}"
        elif r.get("count"):
            status = f"{r['count']} found" + (" (more exist; raise FHIR_MAX_PAGES or narrow the window)" if r.get("truncated") else "")
        else:
            status = "NOT in this record"
        lines.append(f"  {r.get('label')} (LOINC {r.get('code')}): {status}")
    other = result.get("other") or []
    if other or result.get("otherError"):
        added = {d.get("id") for r in result.get("rows", []) if r.get("extra") for d in r.get("docs", [])}
        left = [(g, sum(1 for d in g.get("docs", []) if d.get("id") not in added)) for g in other]
        left = [(g, n) for g, n in left if n]
        lines += ["", "  Other records in the chart, not in this packet:"]
        if result.get("otherError"):
            lines.append(f"    ERROR listing them: {result['otherError']}")
        for g, n in left:
            code = g.get("loinc") or g.get("key", "")
            lines.append(f"    {g.get('label')} ({code}): {n}")
        if not left and not result.get("otherError"):
            lines.append("    none")
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


def crlf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


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
        # The sheets use Windows line endings so they read correctly in any Notepad.
        z.writestr("00_biographical-sheet.txt", crlf(bio_sheet(pt)))
        for name, data in sorted(files):
            z.writestr(name, data)
        z.writestr("missing-items.txt", crlf(missing_sheet(pt, plan, result)))
        if errors:
            z.writestr("download-errors.txt", crlf("\n".join(errors) + "\n"))
    buf.seek(0)
    return buf, zip_name_for(result), len(errors)
