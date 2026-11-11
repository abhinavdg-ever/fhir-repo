"""Minimal async FHIR R4 client for Cerner (Oracle Health) Millennium.

Works against the open sandbox (no auth) or a secured tenant with a bearer
token. Swap in SMART backend-services auth for production.
"""
from __future__ import annotations

import base64
from urllib.parse import urljoin

import httpx

FHIR_JSON = "application/fhir+json"
LOINC = "http://loinc.org"


class FhirClient:
    def __init__(self, base: str, token: str | None = None, timeout: float = 45.0):
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
        try:
            return await self.http.get(url, **kwargs)
        except httpx.HTTPError as e:  # timeouts, DNS, connection resets
            raise FhirError(0, url, f"{type(e).__name__}: {e}") from e

    async def get_json(self, path: str, params=None) -> dict:
        r = await self._get(self.url(path), params=params)
        if r.status_code >= 400:
            raise FhirError(r.status_code, r.request.url, r.text[:500])
        return r.json()

    async def search(self, resource: str, params: list[tuple[str, str]], max_pages: int = 10) -> list[dict]:
        """Run a search and follow `next` links. Returns the resources."""
        out: list[dict] = []
        bundle = await self.get_json(resource, params)
        for _ in range(max_pages):
            out += [e["resource"] for e in bundle.get("entry", []) if "resource" in e]
            nxt = next((l["url"] for l in bundle.get("link", []) if l.get("relation") == "next"), None)
            if not nxt:
                break
            bundle = await self.get_json(nxt)
        # Cerner can put OperationOutcome entries in a searchset; keep only real hits
        return [r for r in out if r.get("resourceType") == resource]

    # ---- Patient ----
    async def find_patients(self, name: str, birthdate: str | None = None) -> list[dict]:
        name = name.strip()
        if name.isdigit():  # a Patient ID was typed
            return [await self.get_json(f"Patient/{name}")]
        params = [("name", name), ("_count", "20")]
        if birthdate:
            params.append(("birthdate", birthdate))
        return await self.search("Patient", params, max_pages=1)

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
        return await self.search("Observation", params, max_pages=5)

    # ---- Binary ----
    async def download_binary(self, url: str, content_type: str | None) -> tuple[bytes, str]:
        """Ask for the raw file first (Accept = attachment.contentType).
        If the server refuses, ask for the FHIR Binary JSON and decode its base64 `data`."""
        target = self.url(url)
        if content_type:
            r = await self._get(target, headers={"Accept": content_type})
            if r.status_code < 400 and not r.headers.get("content-type", "").startswith(FHIR_JSON):
                return r.content, r.headers.get("content-type", content_type).split(";")[0]
        r = await self._get(target, headers={"Accept": FHIR_JSON})
        if r.status_code >= 400:
            raise FhirError(r.status_code, r.request.url, r.text[:500])
        body = r.json()
        return base64.b64decode(body.get("data", "")), body.get("contentType", content_type or "application/octet-stream")


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
