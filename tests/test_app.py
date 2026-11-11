import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from agent import app as app_module  # noqa: E402
from worker.main import binary_allowed  # noqa: E402

client = TestClient(app_module.app)


def test_binary_rejects_foreign_host():
    assert client.get("/api/binary", params={"url": "https://example.com/Binary/1"}).status_code == 400


def test_binary_allow_list():
    def ok(url):
        return binary_allowed(url, app_module.FHIR_BASE)

    assert ok(app_module.FHIR_BASE + "/Binary/XR-1")
    assert ok("https://fhir-ehr.cerner.com/r4/t/Binary/XR-1")
    assert not ok("http://fhir-ehr.cerner.com/r4/t/Binary/XR-1")
    assert not ok("https://fhir-ehr.cerner.com/r4/t/Patient/1")
    assert not ok("https://evil.cerner.com.example.com/Binary/1")


def test_plan_without_llm_uses_rules():
    r = client.post("/api/plan", json={"service_note": "2026 discharge summaries", "use_llm": False})
    assert r.status_code == 200
    assert r.json()["source"] == "rules"


def test_patient_name_search_requires_dob():
    r = client.get("/api/patients", params={"name": "jane"})
    assert r.status_code == 400
    assert "Date of birth" in r.json()["detail"]


def test_full_names_split_into_family_and_given():
    from worker.fhir_client import FhirClient
    split = FhirClient._name_params
    assert split("Peters, Tim") == [("family", "Peters"), ("given", "Tim")]
    assert split("Tim Peters") == [("family", "Peters"), ("given", "Tim")]
    assert split("peters") == [("name", "peters")]


def test_sheets_use_us_dates():
    from worker.main import bio_sheet, us_date
    assert us_date("1990-09-15") == "09/15/1990"
    assert "Date of birth: 09/15/1990" in bio_sheet({"birthDate": "1990-09-15"})


def test_fhir_get_retries_on_429(monkeypatch):
    import asyncio

    import httpx

    from worker import fhir_client

    calls = []

    def handler(request):
        calls.append(request.url)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"resourceType": "OperationOutcome"})
        return httpx.Response(200, json={"resourceType": "Patient", "id": "1"})

    monkeypatch.setenv("FHIR_MAX_RPS", "0")
    async def go():
        c = fhir_client.FhirClient("https://fhir.example.cerner.com/r4/t")
        c.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await c.get_patient("1")
        finally:
            await c.close()

    assert asyncio.run(go())["id"] == "1"
    assert len(calls) == 3


def test_fhir_get_gives_up_after_max_retries(monkeypatch):
    import asyncio

    import httpx
    import pytest

    from worker import fhir_client

    monkeypatch.setenv("FHIR_MAX_RPS", "0")
    monkeypatch.setenv("FHIR_MAX_RETRIES", "1")
    monkeypatch.setattr(fhir_client, "retry_after_seconds", lambda r, a: 0)
    async def go():
        c = fhir_client.FhirClient("https://fhir.example.cerner.com/r4/t")
        c.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(429, text="Too Many Requests")))
        try:
            await c.get_patient("1")
        finally:
            await c.close()

    with pytest.raises(fhir_client.FhirError, match="HTTP 429"):
        asyncio.run(go())


def test_fhir_get_retries_on_timeout(monkeypatch):
    import asyncio

    import httpx

    from worker import fhir_client

    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow page", request=request)
        return httpx.Response(200, json={"resourceType": "Patient", "id": "1"})

    monkeypatch.setenv("FHIR_MAX_RPS", "0")
    monkeypatch.setattr(fhir_client.random, "uniform", lambda a, b: 0)
    async def go():
        c = fhir_client.FhirClient("https://fhir.example.cerner.com/r4/t")
        c.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await c.get_patient("1")
        finally:
            await c.close()

    assert asyncio.run(go())["id"] == "1"
    assert len(calls) == 2


def test_run_stream_emits_progress_then_done(monkeypatch):
    import json

    from worker import main as worker_main

    async def fake_patient(self, pid):
        return {"resourceType": "Patient", "id": pid, "name": [{"text": "Doe, Jane"}], "birthDate": "1958-04-02"}

    async def fake_docs(self, pid, code, start, end):
        if code == "18842-5":
            return [{"resourceType": "DocumentReference", "id": "d1", "status": "current",
                     "type": {"text": "Discharge Summary"}, "date": "2026-03-14"}]
        return []

    monkeypatch.setattr(worker_main.FhirClient, "get_patient", fake_patient)
    monkeypatch.setattr(worker_main.FhirClient, "documents_by_loinc", fake_docs)
    plan = {"start": "2026-01-01", "end": "2026-11-12", "documents": [
        {"code": "18842-5", "label": "Discharge summary"}, {"code": "8653-8", "label": "Hospital discharge instructions"}]}
    r = client.post("/api/run/stream", json={"patient_id": "1", "plan": plan})
    events = [json.loads(line) for line in r.text.splitlines()]
    assert [e["type"] for e in events] == ["start", "row", "row", "done"]
    result = events[-1]["result"]
    assert [row["code"] for row in result["rows"]] == ["18842-5", "8653-8"]
    assert result["missing"] == ["8653-8"]


def test_search_flags_truncation_without_fetching_extra_page(monkeypatch):
    import asyncio

    import httpx

    from worker import fhir_client

    calls = []

    def handler(request):
        calls.append(str(request.url))
        n = len(calls)
        return httpx.Response(200, json={
            "resourceType": "Bundle",
            "entry": [{"resource": {"resourceType": "DocumentReference", "id": str(n)}}],
            "link": [{"relation": "next", "url": f"https://fhir.example.cerner.com/r4/t/DocumentReference?page={n + 1}"}],
        })

    monkeypatch.setenv("FHIR_MAX_RPS", "0")
    async def go():
        c = fhir_client.FhirClient("https://fhir.example.cerner.com/r4/t")
        c.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await c.search("DocumentReference", [("patient", "1")], max_pages=3)
        finally:
            await c.close()

    docs = asyncio.run(go())
    assert len(docs) == 3 and docs.truncated
    assert len(calls) == 3  # no wasted 4th page
