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
