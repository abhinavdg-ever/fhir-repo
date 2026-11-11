"""Packet downloads as background jobs, so the UI isn't blocked and can show per-file status.

Each job downloads its notes (rate-limited like every FHIR call), tracks each file as
queued -> downloading -> done | failed, then holds the finished zip in memory until it
is fetched. Jobs expire after JOB_TTL seconds. Nothing is written to disk.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field

from common.models import Plan
from worker.main import build_packet, packet_docs, zip_name_for

JOB_TTL = 30 * 60


@dataclass
class PacketJob:
    id: str
    zip_name: str
    files: dict[str, dict]  # file name -> {"status": ..., "error": ...}
    state: str = "running"  # running | ready | failed
    error: str | None = None
    failed: int = 0
    zip_bytes: bytes | None = None
    created: float = field(default_factory=time.time)
    task: asyncio.Task | None = None

    def snapshot(self) -> dict:
        counts: dict[str, int] = {}
        for f in self.files.values():
            counts[f["status"]] = counts.get(f["status"], 0) + 1
        return {"job": self.id, "state": self.state, "error": self.error, "zipName": self.zip_name,
                "failed": self.failed, "counts": counts, "files": self.files}


JOBS: dict[str, PacketJob] = {}


def _expire() -> None:
    cutoff = time.time() - JOB_TTL
    for job_id in [j for j, job in JOBS.items() if job.created < cutoff and job.state != "running"]:
        del JOBS[job_id]


def start_packet_job(make_client, plan: Plan, result: dict) -> PacketJob:
    _expire()
    files = {name: {"status": "queued", "error": None} for name in packet_docs(result)}
    job = PacketJob(id=uuid.uuid4().hex[:12], zip_name=zip_name_for(result), files=files)

    def on_status(name: str, status: str, error: str | None = None) -> None:
        job.files[name] = {"status": status, "error": error}

    async def run() -> None:
        c = make_client()
        try:
            buf, _, job.failed = await build_packet(c, plan, result, on_status)
            job.zip_bytes = buf.getvalue()
            job.state = "ready"
        except Exception as e:  # report it on the job instead of losing it in a background task
            job.state, job.error = "failed", f"{type(e).__name__}: {e}"
        finally:
            await c.close()

    job.task = asyncio.create_task(run())
    JOBS[job.id] = job
    return job
