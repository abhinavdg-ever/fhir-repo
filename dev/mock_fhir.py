"""Tiny offline stand-in for a Cerner R4 tenant, for demos without network access.

Run:  uvicorn dev.mock_fhir:app --port 9000      (from the repo root)
Then: FHIR_BASE=http://localhost:9000/r4/mock

Shapes follow Cerner's (code set 72 + optional LOINC on DocumentReference.type,
Binary links in content.attachment.url). All data is made up.
"""
import base64
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

app = FastAPI()
CS72 = "https://fhir.cerner.com/mock/codeSet/72"
# Base URL written into Binary links; in Docker compose it is http://mock:9000/r4/mock.
MOCK_BASE = os.getenv("MOCK_BASE_URL", "http://localhost:9000/r4/mock").rstrip("/")
LOINC = "http://loinc.org"

PATIENT = {
    "resourceType": "Patient", "id": "12724066",
    "name": [{"text": "Doe, Jane", "family": "Doe", "given": ["Jane"]}],
    "birthDate": "1958-04-02", "gender": "female",
    "address": [{"line": ["12 Elm St"], "city": "Kansas City", "state": "MO", "postalCode": "64105"}],
    "telecom": [{"system": "phone", "value": "555-0100"}],
    "identifier": [{"type": {"text": "MRN"}, "value": "MRN-0042"}],
}


def doc(id_, title, local, loinc, when, author, enc):
    coding = [{"system": CS72, "code": local, "display": title}]
    if loinc:
        coding.append({"system": LOINC, "code": loinc})
    return {"resourceType": "DocumentReference", "id": id_, "status": "current", "docStatus": "final",
            "type": {"coding": coding, "text": title},
            "category": [{"coding": [{"code": "clinical-note"}]}],
            "subject": {"reference": "Patient/12724066"}, "date": when + "T12:00:00Z",
            "author": [{"display": author}],
            "context": {"encounter": [{"reference": enc}], "period": {"start": when + "T10:00:00Z"}},
            "content": [{"attachment": {"contentType": "application/pdf",
                                        "url": f"{MOCK_BASE}/Binary/XR-{id_}"}}]}


DOCS = [
    doc("201000001", "Telehealth Visit Note", "2820600", "11506-3", "2026-02-05", "Lee, A MD", "Encounter/1"),
    doc("201000002", "History and Physical", "2820510", "34117-2", "2026-03-10", "Patel, R MD", "Encounter/97953477"),
    doc("201000003", "Telephone Encounter - admission notice", "2820611", None, "2026-03-11", "Nurse, K RN", "Encounter/2"),
    doc("201000004", "Cardiology Consult", "2820520", "11488-4", "2026-03-12", "Shah, M MD", "Encounter/97953477"),
    doc("201000005", "Discharge Summary", "2820507", "18842-5", "2026-03-14", "Patel, R MD", "Encounter/97953477"),
    doc("201000006", "Office Visit Note", "2820571", "11506-3", "2026-03-20", "Lee, A MD", "Encounter/3"),
    doc("201000007", "Office Visit Note", "2820571", "11506-3", "2026-09-18", "Lee, A MD", "Encounter/4"),
    doc("201000008", "Progress Note", "2820571", "11506-3", "2025-11-18", "Lee, A MD", "Encounter/5"),
]


def bp(id_, when, sys_, dia, who):
    q = lambda c, v: {"code": {"coding": [{"system": LOINC, "code": c}]},
                      "valueQuantity": {"value": v, "unit": "mmHg"}}
    return {"resourceType": "Observation", "id": id_, "status": "final",
            "category": [{"coding": [{"code": "vital-signs"}]}],
            "code": {"coding": [{"system": LOINC, "code": "85354-9", "display": "Blood pressure panel"}],
                     "text": "Blood pressure"},
            "effectiveDateTime": when + "T10:00:00Z", "performer": [{"display": who}],
            "component": [q("8480-6", sys_), q("8462-4", dia)]}


OBS = [bp("o1", "2026-10-14", 146, 88, "Endoscopy RN"), bp("o2", "2026-09-18", 128, 82, "Lee, A MD"),
       bp("o3", "2025-11-18", 132, 84, "Lee, A MD")]


def bundle(items):
    return JSONResponse({"resourceType": "Bundle", "type": "searchset", "total": len(items),
                         "entry": [{"resource": r} for r in items], "link": []},
                        media_type="application/fhir+json")


def in_window(when: str, params: list[tuple[str, str]], key: str) -> bool:
    for k, v in params:
        if k != key:
            continue
        if v.startswith("ge") and when < v[2:]:
            return False
        if v.startswith("lt") and when >= v[2:]:
            return False
    return True


@app.get("/r4/mock/Patient")
def patients(name: str = "", birthdate: str = ""):
    hit = name.lower() in "jane doe doe, jane" if name else True
    hit = hit and (not birthdate or birthdate == PATIENT["birthDate"])
    return bundle([PATIENT] if hit else [])


@app.get("/r4/mock/Patient/{pid}")
def patient(pid: str):
    return JSONResponse(PATIENT, media_type="application/fhir+json")


@app.get("/r4/mock/DocumentReference")
def docrefs(request: Request):
    params = list(request.query_params.multi_items())
    q = dict(params)
    out = []
    for d in DOCS:
        when = d["context"]["period"]["start"][:10]
        if not in_window(when, params, "period"):
            continue
        if "type" in q:
            system, code = q["type"].split("|")
            if not any(c["system"] == system and c["code"] == code for c in d["type"]["coding"]):
                continue
        out.append(d)
    return bundle(out)


@app.get("/r4/mock/Observation")
def observations(request: Request):
    params = list(request.query_params.multi_items())
    return bundle([o for o in OBS if in_window(o["effectiveDateTime"][:10], params, "date")])


def tiny_pdf(text: str) -> bytes:
    stream = f"BT /F1 14 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


@app.get("/r4/mock/Binary/{bid}")
def binary(bid: str, request: Request):
    d = next((x for x in DOCS if f"XR-{x['id']}" == bid), None)
    pdf = tiny_pdf(f"{d['type']['text'] if d else bid} - mock document")
    if "application/fhir+json" in request.headers.get("accept", ""):
        return JSONResponse({"resourceType": "Binary", "contentType": "application/pdf",
                             "data": base64.b64encode(pdf).decode()}, media_type="application/fhir+json")
    return Response(pdf, media_type="application/pdf")
