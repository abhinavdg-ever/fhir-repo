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
    r = client.post("/api/run/stream", json={"patient_id": "1", "plan": plan, "discover": False})
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


def test_fhir_calls_are_logged_with_retries(monkeypatch):
    import asyncio

    import httpx

    from worker import fhir_client

    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"resourceType": "Patient", "id": "7"})

    monkeypatch.setenv("FHIR_MAX_RPS", "0")
    before = client.get("/api/calls").json()["last"]
    async def go():
        c = fhir_client.FhirClient("https://fhir.example.cerner.com/r4/t")
        c.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await c.get_patient("7")
        finally:
            await c.close()

    asyncio.run(go())
    logged = client.get("/api/calls", params={"after": before}).json()["calls"]
    assert [(c["status"], c["attempt"]) for c in logged] == [(429, 1), (200, 2)]
    assert logged[-1]["url"].endswith("/Patient/7")


def test_packet_job_reports_per_file_status_and_serves_zip(monkeypatch):
    import io
    import time
    import zipfile

    from worker import fhir_client

    async def fake_download(self, url, content_type):
        if url.endswith("XR-2"):
            raise fhir_client.FhirError(404, url, "gone")
        return b"%PDF-1.4 fake", "application/pdf"

    monkeypatch.setattr(fhir_client.FhirClient, "download_binary", fake_download)
    base = app_module.FHIR_BASE
    result = {
        "patient": {"id": "1", "name": "Doe, Jane", "birthDate": "1958-04-02"},
        "window": {"start": None, "end": None},
        "rows": [{"code": "11506-3", "label": "Progress note", "count": 2, "error": None, "docs": [
            {"id": "1", "file": "2026-01-01_Note_1.pdf", "binaryUrl": f"{base}/Binary/XR-1", "contentType": "application/pdf"},
            {"id": "2", "file": "2026-01-02_Note_2.pdf", "binaryUrl": f"{base}/Binary/XR-2", "contentType": "application/pdf"},
        ]}],
        "observations": [],
    }
    plan = {"start": "2026-01-01", "end": "2026-11-12", "documents": [{"code": "11506-3", "label": "Progress note"}]}
    with TestClient(app_module.app) as tc:  # keeps one event loop alive for the background task
        job = tc.post("/api/packet/jobs", json={"plan": plan, "result": result}).json()
        assert set(job["files"]) == {"2026-01-01_Note_1.pdf", "2026-01-02_Note_2.pdf"}
        for _ in range(50):
            status = tc.get(f"/api/packet/jobs/{job['job']}").json()
            if status["state"] != "running":
                break
            time.sleep(0.05)
        assert status["state"] == "ready"
        assert status["files"]["2026-01-01_Note_1.pdf"]["status"] == "done"
        assert status["files"]["2026-01-02_Note_2.pdf"]["status"] == "failed"
        z = tc.get(f"/api/packet/jobs/{job['job']}/zip")
        assert z.headers["X-Download-Errors"] == "1"
        names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
        assert "2026-01-01_Note_1.pdf" in names and "download-errors.txt" in names


def test_binary_accept_is_never_a_wildcard(monkeypatch):
    import asyncio

    import httpx

    from worker import fhir_client

    seen = []

    def handler(request):
        accept = request.headers["Accept"]
        seen.append(accept)
        if accept == "application/fhir+json":
            return httpx.Response(200, json={"resourceType": "Binary", "contentType": "text/plain", "data": "aGk="})
        if accept in ("text/plain", "application/pdf"):
            return httpx.Response(200, content=b"hi", headers={"content-type": accept})
        return httpx.Response(406, json={"resourceType": "OperationOutcome"})

    monkeypatch.setenv("FHIR_MAX_RPS", "0")
    async def go(ctype):
        seen.clear()
        c = fhir_client.FhirClient("https://fhir.example.cerner.com/r4/t")
        c.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await c.download_binary("https://fhir.example.cerner.com/r4/t/Binary/XR-1", ctype), list(seen)
        finally:
            await c.close()

    (data, ctype), calls = asyncio.run(go("text/plain; charset=UTF-8"))
    assert (data, ctype, calls) == (b"hi", "text/plain", ["text/plain"])
    (data, ctype), calls = asyncio.run(go("*/*"))
    assert (data, calls) == (b"hi", ["application/fhir+json"])
    (data, ctype), calls = asyncio.run(go("application/rtf"))  # refused -> falls back to FHIR JSON
    assert (data, calls) == (b"hi", ["application/rtf", "application/fhir+json"])


def test_title_scores_exact_close_and_unrelated():
    from worker.main import best_title_match, title_score
    assert title_score("Nursing Narrative Note", "28623-7") == 1.0
    assert 0.82 <= title_score("Nursing Narr", "28623-7") < 1.0
    assert best_title_match("Discharge Sumary", ["18842-5", "11506-3"])[0] == "18842-5"
    assert best_title_match("Inpatient Clinical Summary", ["18842-5"])[0] == "18842-5"
    assert best_title_match("Care Plan- Self Care Plan", ["18842-5", "11506-3", "28623-7"])[0] is None


def test_run_groups_other_records_and_matches_titles(monkeypatch):
    import json

    from worker import main as worker_main

    def doc(id_, title, loinc=None):
        coding = [{"system": "http://loinc.org", "code": loinc}] if loinc else [{"system": "https://x/codeSet/72", "code": "99"}]
        return {"resourceType": "DocumentReference", "id": id_, "status": "current",
                "type": {"text": title, "coding": coding}, "date": "2026-03-01T00:00:00Z",
                "content": [{"attachment": {"contentType": "application/pdf", "url": f"https://h.cerner.com/r4/t/Binary/XR-{id_}"}}]}

    async def fake_patient(self, pid):
        return {"resourceType": "Patient", "id": pid, "name": [{"text": "Doe, Jane"}]}

    async def fake_docs(self, pid, code, start, end):
        return [doc("1", "Discharge Summary", "18842-5")] if code == "18842-5" else []

    async def fake_all(self, pid, start, end):
        return [doc("1", "Discharge Summary", "18842-5"),       # already found by LOINC
                doc("2", "Nursing Narr"),                       # close title match -> nursing row
                doc("6", "Wound Care Progress Note", "89821-3"),  # has its own LOINC -> other, not progress
                doc("3", "Admission Note Physician", "83805-2"),  # other record, known code
                doc("4", "Admission Note Physician", "83805-2"),
                doc("5", "Care Plan- Self Care Plan")]          # other record, local code

    monkeypatch.setattr(worker_main.FhirClient, "get_patient", fake_patient)
    monkeypatch.setattr(worker_main.FhirClient, "documents_by_loinc", fake_docs)
    monkeypatch.setattr(worker_main.FhirClient, "documents_all", fake_all)
    plan = {"start": "2026-01-01", "end": "2026-11-12", "documents": [
        {"code": "18842-5", "label": "Discharge summary"}, {"code": "28623-7", "label": "Nursing narrative note"},
        {"code": "11506-3", "label": "Progress note"}]}
    r = client.post("/api/run/stream", json={"patient_id": "1", "plan": plan, "title_fallback": True, "discover": True})
    events = [json.loads(line) for line in r.text.splitlines()]
    assert [e["type"] for e in events] == ["start", "row", "row", "row", "sweep", "other", "done"]
    result = events[-1]["result"]
    nursing = next(r for r in result["rows"] if r["code"] == "28623-7")
    assert [(d["id"], d["matchedBy"]) for d in nursing["docs"]] == [("2", "title")] and nursing["docs"][0]["matchScore"] < 1
    groups = {g["key"]: g["count"] for g in result["other"]}
    assert groups["83805-2"] == 2 and groups["89821-3"] == 1 and sum(groups.values()) == 4
    assert next(g for g in result["other"] if g["key"] == "83805-2")["inCatalog"] is True


def test_run_reports_encounters_facility_and_gaps(monkeypatch):
    import json

    from agent.planner import rules_plan
    from worker import main as worker_main

    async def fake_patient(self, pid):
        return {"resourceType": "Patient", "id": pid, "name": [{"text": "Doe, Jane"}], "birthDate": "1958-04-02"}

    def note(id_, code, enc):
        return {"resourceType": "DocumentReference", "id": id_, "status": "current", "type": {"text": "Note"},
                "date": "2026-03-01T00:00:00Z", "context": {"encounter": [{"reference": f"Encounter/{enc}"}]},
                "content": [{"attachment": {"contentType": "application/pdf", "url": f"https://h.cerner.com/r4/t/Binary/XR-{id_}"}}]}

    async def fake_docs(self, pid, code, start, end):
        return {"18842-5": [note("1", code, "E1")], "83805-2": [note("2", code, "E2")]}.get(code, [])

    async def fake_encounters(self, pid, start, end):
        enc = lambda id_, cls, fac: {"resourceType": "Encounter", "id": id_, "status": "finished",
                                     "class": {"code": cls}, "period": {"start": "2026-03-01T00:00:00Z"},
                                     "serviceProvider": {"display": fac}}
        return [enc("E1", "IMP", "General Hospital"), enc("E2", "IMP", "City Hospital"), enc("E3", "AMB", "Clinic")]

    monkeypatch.setattr(worker_main.FhirClient, "get_patient", fake_patient)
    monkeypatch.setattr(worker_main.FhirClient, "documents_by_loinc", fake_docs)
    monkeypatch.setattr(worker_main.FhirClient, "encounters", fake_encounters)
    from tests.test_planner import TODAY, TRC_NOTE
    plan = rules_plan(TRC_NOTE, TODAY).model_dump(by_alias=True)
    r = client.post("/api/run/stream", json={"patient_id": "1", "plan": plan})
    result = json.loads(r.text.splitlines()[-1])["result"]
    stays = next(x for x in result["rows"] if x["code"] == "inpatient_stays")
    assert stays["resource"] == "Encounter" and stays["count"] == 2
    assert next(x for x in result["rows"] if x["code"] == "outpatient_visits")["count"] == 1
    assert result["facilityWarning"]["facilities"] == {"General Hospital": 1, "City Hospital": 1}
    notations = [g["notation"] for g in result["gaps"]]
    assert "Discharge medications not reconciled" in notations
    assert result["fallbackNote"] and result["intent"]["fallback_strategy"] == "progress_notes_if_missing"

    from common.models import Plan
    from worker.main import missing_sheet
    sheet = missing_sheet(result["patient"], Plan.model_validate(plan), result)
    assert "ELEMENTS NOT IN THE RECORD" in sheet and "Discharge medications not reconciled" in sheet
    assert "FACILITY CHECK" in sheet and "[required]" in sheet
