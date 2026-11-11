"""Minimal async FHIR R4 client for Cerner (Oracle Health) Millennium.

Works against the open sandbox (no auth) or a secured tenant with a bearer
token. Swap in SMART backend-services auth for production.

All requests share one rate limiter (FHIR_MAX_RPS) and are retried on
429 / 502 / 503 / 504 and on timeouts / dropped connections (FHIR_MAX_RETRIES), honouring
Retry-After when the server sends it. FHIR_TIMEOUT sets seconds per request.
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import os
import random
import time
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

import httpx

FHIR_JSON = "application/fhir+json"
LOINC = "http://loinc.org"
RETRY_STATUSES = {429, 502, 503, 504}  # throttled or a gateway hiccup
MAX_RETRY_WAIT = 30.0  # seconds; never sleep longer than this on one retry


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


class RateLimiter:
    """Spaces requests at least 1/rps seconds apart, across every client in this process."""

    def __init__(self):
        self._lock: asyncio.Lock | None = None
        self._loop = None
        self._next = 0.0

    async def wait(self, rps: float) -> None:
        if rps <= 0:
            return
        loop = asyncio.get_running_loop()
        if self._lock is None or self._loop is not loop:  # one lock per event loop
            self._lock, self._loop = asyncio.Lock(), loop
        async with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + 1.0 / rps
        if delay > 0:
            await asyncio.sleep(delay)


LIMITER = RateLimiter()


class CallLog:
    """The most recent FHIR requests (every attempt, retries included), for the UI's API calls window.

    Process-wide and in memory: fine for a demo, shared by everyone using this server.
    """

    def __init__(self, size: int = 2000):
        self._calls: deque[dict] = deque(maxlen=size)
        self._ids = itertools.count(1)

    def add(self, **entry) -> None:
        entry["id"] = next(self._ids)
        entry["at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        self._calls.append(entry)

    def since(self, after: int = 0) -> list[dict]:
        return [c for c in self._calls if c["id"] > after]


CALL_LOG = CallLog()


def retry_after_seconds(response: httpx.Response, attempt: int) -> float:
    """Server's Retry-After (seconds or HTTP date) if given, else 1, 2, 4, 8… plus jitter."""
    header = response.headers.get("Retry-After", "").strip()
    wait = None
    if header:
        try:
            wait = float(header)
        except ValueError:
            try:
                wait = parsedate_to_datetime(header).timestamp() - time.time()
            except (TypeError, ValueError):
                wait = None
    if wait is None or wait < 0:
        wait = 2 ** attempt + random.uniform(0, 0.5)
    return min(wait, MAX_RETRY_WAIT)


class SearchResults(list):
    """A list of resources that remembers whether paging stopped early."""

    def __init__(self, items=(), truncated: bool = False, request_url: str = ""):
        super().__init__(items)
        self.truncated = truncated
        self.request_url = request_url  # the first-page search URL, as sent


class FhirClient:
    def __init__(self, base: str, token: str | None = None, timeout: float | None = None):
        timeout = timeout or _env_float("FHIR_TIMEOUT", 60)
        self.base = base.rstrip("/") + "/"
        headers = {"Accept": FHIR_JSON}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.http = httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True)

    async def close(self):
        await self.http.aclose()

    def url(self, path: str) -> str:
        return path if path.startswith("http") else urljoin(self.base, path)

    async def _get(self, url: str, **kwargs) -> httpx.Response:
        rps = _env_float("FHIR_MAX_RPS", 3)
        retries = int(_env_float("FHIR_MAX_RETRIES", 4))
        full_url = str(httpx.URL(url, params=kwargs.get("params")))
        accept = (kwargs.get("headers") or {}).get("Accept", FHIR_JSON)
        for attempt in range(retries + 1):
            await LIMITER.wait(rps)
            started = time.monotonic()
            try:
                r = await self.http.get(url, **kwargs)
            except httpx.TransportError as e:  # timeouts, connection resets: worth another try
                CALL_LOG.add(method="GET", url=full_url, accept=accept, status=None, attempt=attempt + 1,
                             ms=round((time.monotonic() - started) * 1000), error=type(e).__name__)
                if attempt == retries:
                    raise FhirError(0, url, f"{type(e).__name__} after {retries + 1} tries: {str(e)[:300]}") from e
                await asyncio.sleep(2 ** attempt + random.uniform(0, 0.5))
                continue
            except httpx.HTTPError as e:
                CALL_LOG.add(method="GET", url=full_url, accept=accept, status=None, attempt=attempt + 1,
                             ms=round((time.monotonic() - started) * 1000), error=type(e).__name__)
                raise FhirError(0, url, f"{type(e).__name__}: {e}") from e
            CALL_LOG.add(method="GET", url=str(r.request.url), accept=accept, status=r.status_code,
                         attempt=attempt + 1, ms=round((time.monotonic() - started) * 1000),
                         error=None if r.status_code < 400 else r.reason_phrase)
            if r.status_code not in RETRY_STATUSES or attempt == retries:
                return r
            await asyncio.sleep(retry_after_seconds(r, attempt))
        return r

    async def get_json(self, path: str, params=None) -> dict:
        r = await self._get(self.url(path), params=params)
        if r.status_code >= 400:
            raise FhirError(r.status_code, r.request.url, r.text[:500])
        return r.json()

    async def search(self, resource: str, params: list[tuple[str, str]], max_pages: int | None = None) -> SearchResults:
        """Run a search and follow `next` links up to max_pages (FHIR_MAX_PAGES, default 10).
        `.truncated` on the result is True when more pages were left unread."""
        max_pages = max_pages or int(_env_float("FHIR_MAX_PAGES", 10))
        out: list[dict] = []
        truncated = False
        bundle = await self.get_json(resource, params)
        for page in range(max_pages):
            out += [e["resource"] for e in bundle.get("entry", []) if "resource" in e]
            nxt = next((l["url"] for l in bundle.get("link", []) if l.get("relation") == "next"), None)
            if not nxt:
                break
            if page == max_pages - 1:
                truncated = True
                break
            bundle = await self.get_json(nxt)
        # Cerner can put OperationOutcome entries in a searchset; keep only real hits
        request_url = str(httpx.URL(self.url(resource), params=params))
        return SearchResults((r for r in out if r.get("resourceType") == resource),
                             truncated=truncated, request_url=request_url)

    # ---- Patient ----
    async def find_patients(self, name: str, birthdate: str | None = None) -> list[dict]:
        name = name.strip()
        if name.isdigit():  # a Patient ID was typed
            return [await self.get_json(f"Patient/{name}")]
        params = [*self._name_params(name), ("_count", "20")]
        if birthdate:
            params.append(("birthdate", birthdate))
        return await self.search("Patient", params, max_pages=1)

    @staticmethod
    def _name_params(name: str) -> list[tuple[str, str]]:
        """Cerner's `name` matches one word, so split full names into family + given.
        "Peters, Tim" and "Tim Peters" -> family=Peters, given=Tim; one word -> name=..."""
        if "," in name:
            family, given = (part.strip() for part in name.split(",", 1))
        else:
            words = name.split()
            if len(words) < 2:
                return [("name", name)]
            given, family = words[0], words[-1]
        params = [("family", family)] if family else []
        return params + ([("given", given)] if given else [])

    async def get_patient(self, pid: str) -> dict:
        return await self.get_json(f"Patient/{pid}")

    # ---- DocumentReference ----
    async def documents_by_loinc(self, pid: str, code: str, start: str | None, end: str | None) -> list[dict]:
        params = [("patient", pid), ("type", f"{LOINC}|{code}"), ("_count", "100")]
        if start and end:  # Cerner: period twice, ge + lt, same precision
            params += [("period", f"ge{start}"), ("period", f"lt{end}")]
        return await self.search("DocumentReference", params)

    async def documents_by_category(self, pid: str, start: str | None, end: str | None,
                                    category: str = "clinical-note") -> list[dict]:
        params = [("patient", pid), ("category", category), ("_count", "100")]
        if start and end:
            params += [("period", f"ge{start}"), ("period", f"lt{end}")]
        return await self.search("DocumentReference", params)

    # ---- Observation ----
    async def observations(self, pid: str, codes: list[str], start: str | None, end: str | None) -> list[dict]:
        params = [("patient", pid), ("code", ",".join(f"{LOINC}|{c}" for c in codes)), ("_count", "200")]
        if start and end:
            params += [("date", f"ge{start}"), ("date", f"lt{end}")]
        return await self.search("Observation", params)

    # ---- Binary ----
    async def download_binary(self, url: str, content_type: str | None) -> tuple[bytes, str]:
        """Fetch a note's file from its Binary URL.

        Cerner only accepts the file's own media type or application/fhir+json in
        Accept; anything else (e.g. a browser's */*) gets 406 "The Accept Header is
        invalid". So ask for the raw file with its exact type first, and on any
        refusal ask for the FHIR Binary JSON and decode its base64 `data`.
        """
        target = self.url(url)
        raw_type = clean_media_type(content_type)
        first_error = ""
        if raw_type and raw_type != FHIR_JSON:
            r = await self._get(target, headers={"Accept": raw_type})
            if r.status_code < 400 and not r.headers.get("content-type", "").startswith(FHIR_JSON):
                return r.content, clean_media_type(r.headers.get("content-type")) or raw_type
            if r.status_code >= 400:
                first_error = f"Accept {raw_type} -> HTTP {r.status_code}; "
        r = await self._get(target, headers={"Accept": FHIR_JSON})
        if r.status_code >= 400:
            raise FhirError(r.status_code, r.request.url, f"{first_error}Accept {FHIR_JSON} -> {r.text[:400]}")
        body = r.json()
        return base64.b64decode(body.get("data", "")), body.get("contentType") or raw_type or "application/octet-stream"


def clean_media_type(value: str | None) -> str | None:
    """'text/plain; charset=UTF-8' -> 'text/plain'. Wildcards and blanks -> None (Cerner rejects them)."""
    if not value:
        return None
    media = value.split(";", 1)[0].strip().lower()
    if not media or "*" in media or "/" not in media:
        return None
    return media


class FhirError(Exception):
    def __init__(self, status: int, url, detail: str):
        prefix = f"HTTP {status}" if status else "Request failed"
        super().__init__(f"{prefix} for {url}: {detail}")
        self.status = status


# ---------- small helpers that flatten resources for the UI ----------

def patient_summary(p: dict) -> dict:
    n = (p.get("name") or [{}])[0]
    full = n.get("text") or " ".join(n.get("given", []) + [n.get("family", "")]).strip()
    addr = (p.get("address") or [{}])[0]
    phone = next((t.get("value") for t in p.get("telecom", []) if t.get("system") == "phone"), "")
    ids = [f"{i.get('type', {}).get('text') or i.get('system', '')}: {i.get('value')}" for i in p.get("identifier", [])]
    return {
        "id": p.get("id"), "name": full, "birthDate": p.get("birthDate", ""), "gender": p.get("gender", ""),
        "address": ", ".join(filter(None, [" ".join(addr.get("line", [])), addr.get("city"), addr.get("state"), addr.get("postalCode")])),
        "phone": phone, "identifiers": ids,
    }


def doc_summary(d: dict) -> dict:
    t = d.get("type", {})
    codings = t.get("coding", [])
    loincs = [c.get("code") for c in codings if c.get("system") == LOINC]
    local = [f"{c.get('code')} {c.get('display', '')}".strip() for c in codings if c.get("system") != LOINC]
    title = t.get("text") or next((c.get("display") for c in codings if c.get("display")), "") or d.get("description", "")
    ctx = d.get("context", {})
    when = (ctx.get("period") or {}).get("start") or d.get("date") or ""
    atts = [c.get("attachment", {}) for c in d.get("content", [])]
    best = next((a for a in atts if a.get("contentType") == "application/pdf"), atts[0] if atts else {})
    return {
        "id": d.get("id"), "status": d.get("status"), "docStatus": d.get("docStatus"),
        "title": title, "loinc": loincs, "localCodes": local, "date": when[:10],
        "author": ", ".join(a.get("display", "") for a in d.get("author", []) if a.get("display")),
        "encounter": ", ".join(e.get("reference", "") for e in ctx.get("encounter", [])),
        "contentType": best.get("contentType"), "binaryUrl": best.get("url"),
    }


def obs_summary(o: dict) -> dict:
    code = o.get("code", {})
    def val(x):
        q = x.get("valueQuantity")
        if q:
            return f"{q.get('value')} {q.get('unit', '')}".strip()
        return x.get("valueString") or (x.get("valueCodeableConcept") or {}).get("text", "")
    parts = [val(c) for c in o.get("component", [])]
    value = " / ".join(p for p in parts if p) if parts else val(o)
    return {
        "id": o.get("id"), "date": (o.get("effectiveDateTime") or o.get("issued") or "")[:10],
        "name": code.get("text") or next((c.get("display") for c in code.get("coding", []) if c.get("display")), ""),
        "loinc": [c.get("code") for c in code.get("coding", []) if c.get("system") == LOINC],
        "value": value, "status": o.get("status"),
        "performer": ", ".join(p.get("display", "") for p in o.get("performer", []) if p.get("display")),
        "encounter": (o.get("encounter") or {}).get("reference", ""),
    }
