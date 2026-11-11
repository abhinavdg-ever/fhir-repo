"""FastAPI app: the LLM agent plus the routes the UI calls.

  GET  /api/health     FHIR base and whether Azure OpenAI is configured
  GET  /api/catalog    allowed document and result codes
  POST /api/plan       service note -> LOINC plan (502 if the LLM fails)
  GET  /api/patients   find the member by name (+ optional DOB) or Patient ID
  POST /api/run        run the plan against Cerner (worker.main.handle_job)
  POST /api/run/stream the same, as NDJSON progress events (one line per LOINC call)
  GET  /api/calls      the FHIR requests sent to Cerner (for the UI's API calls window)
  GET  /api/binary     stream one note's file from its Binary URL
  POST /api/packet     build the zip packet in memory and stream it back (blocking)
  POST /api/packet/jobs             start a packet download in the background
  GET  /api/packet/jobs/{job}       per-file status: queued, downloading, done, failed
  GET  /api/packet/jobs/{job}/zip   the finished zip

Run from the repo root:  uvicorn agent.app:app --port 8000
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from agent.doc_types import DOCUMENT_CODES, OBSERVATION_CODES  # noqa: E402
from agent.planner import LlmError, llm_configured, llm_settings, make_plan  # noqa: E402
from common.models import PacketRequest, Plan, PlanRequest, RunRequest  # noqa: E402
from worker.fhir_client import CALL_LOG, FhirClient, FhirError, patient_summary  # noqa: E402
from worker.main import CONCURRENCY, DOWNLOAD_CONCURRENCY, binary_allowed, build_packet, handle_job, run_job_events, safe  # noqa: E402
from worker.packets import JOBS, start_packet_job  # noqa: E402

FHIR_BASE = os.getenv("FHIR_BASE", "https://fhir-open.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d")
FHIR_TOKEN = os.getenv("FHIR_BEARER_TOKEN") or None

app = FastAPI(title="FHIR Record Retrieval")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_methods=["*"], allow_headers=["*"], expose_headers=["X-Download-Errors"])


def client() -> FhirClient:
    return FhirClient(FHIR_BASE, FHIR_TOKEN)


@app.get("/api/health")
def health():
    on = llm_configured()
    s = llm_settings()
    return {"fhirBase": FHIR_BASE, "llm": on, "llmDeployment": s["deployment"] if on else None,
            "llmTemperature": s["temperature"] if on else None,
            "fhirMaxRps": float(os.getenv("FHIR_MAX_RPS", "3") or 0), "fhirConcurrency": CONCURRENCY,
            "fhirDownloadConcurrency": DOWNLOAD_CONCURRENCY}


@app.get("/api/catalog")
def catalog():
    return {"documents": [{"code": c, "label": v["label"]} for c, v in DOCUMENT_CODES.items()],
            "observations": [{"code": c, "label": v["label"]} for c, v in OBSERVATION_CODES.items()]}


@app.get("/api/calls")
def calls(after: int = 0):
    entries = CALL_LOG.since(after)
    return {"calls": entries, "last": entries[-1]["id"] if entries else after}


@app.post("/api/plan", response_model=Plan)
async def plan(req: PlanRequest):
    if not req.service_note.strip():
        raise HTTPException(400, "Service note is empty")
    try:
        # The OpenAI SDK call is blocking; keep it off the event loop.
        return await asyncio.to_thread(make_plan, req.service_note, req.use_llm)
    except LlmError as e:
        raise HTTPException(502, str(e))


@app.get("/api/patients")
async def patients(name: str, birthdate: str | None = None):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Enter a patient name or Patient ID")
    if not name.isdigit() and not birthdate:
        # A name alone matches too many people to pick the right member's chart.
        raise HTTPException(400, "Date of birth is required when searching by name")
    c = client()
    try:
        return [patient_summary(p) for p in await c.find_patients(name, birthdate)]
    except FhirError as e:
        if e.status == 404:
            return []
        raise HTTPException(502, str(e))
    finally:
        await c.close()


@app.post("/api/run")
async def run(req: RunRequest):
    c = client()
    try:
        return await handle_job(c, req)
    except FhirError as e:
        raise HTTPException(502, str(e))
    finally:
        await c.close()


@app.post("/api/run/stream")
async def run_stream(req: RunRequest):
    async def events():
        c = client()
        try:
            async for event in run_job_events(c, req):
                yield json.dumps(event) + "\n"
        except FhirError as e:
            yield json.dumps({"type": "error", "detail": str(e)}) + "\n"
        finally:
            await c.close()

    return StreamingResponse(events(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/binary")
async def binary(url: str, type: str | None = None, name: str | None = None):
    if not binary_allowed(url, FHIR_BASE):
        raise HTTPException(400, "URL is not a Binary on the configured FHIR server")
    c = client()
    try:
        data, ctype = await c.download_binary(url, type)
    except FhirError as e:
        raise HTTPException(502, str(e))
    finally:
        await c.close()
    filename = safe(name or "", 120) or "document"
    return StreamingResponse(iter([data]), media_type=ctype,
                             headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(filename)}"})


@app.post("/api/packet")
async def packet(req: PacketRequest):
    c = client()
    try:
        buf, zip_name, failed = await build_packet(c, req.plan, req.result)
    finally:
        await c.close()
    return StreamingResponse(buf, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{zip_name}"',
        "X-Download-Errors": str(failed),
    })


@app.post("/api/packet/jobs")
async def packet_job_start(req: PacketRequest):
    return start_packet_job(client, req.plan, req.result).snapshot()


def _job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Packet job not found or expired")
    return job


@app.get("/api/packet/jobs/{job_id}")
def packet_job_status(job_id: str):
    return _job(job_id).snapshot()


@app.get("/api/packet/jobs/{job_id}/zip")
def packet_job_zip(job_id: str):
    job = _job(job_id)
    if job.state != "ready" or job.zip_bytes is None:
        raise HTTPException(409, f"Packet is {job.state}" + (f": {job.error}" if job.error else ""))
    return StreamingResponse(iter([job.zip_bytes]), media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{job.zip_name}"',
        "X-Download-Errors": str(job.failed),
    })


# Serve the built React app (frontend/dist) from the same port, if present.
_dist = ROOT / "frontend" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="ui")
