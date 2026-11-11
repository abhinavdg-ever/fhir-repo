"""Request and plan contracts shared by the agent and the worker (Pydantic)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class PlanItem(BaseModel):
    code: str
    label: str
    reason: str = ""


class Plan(BaseModel):
    """The clinical intent read from a service note: which LOINC codes, which dates."""
    measures: list[str] = Field(default_factory=list)  # subset of TRC, CBP, BPD, GSD
    start: str  # inclusive, YYYY-MM-DD
    end: str    # exclusive, YYYY-MM-DD (Cerner wants period=lt<end>)
    documents: list[PlanItem] = Field(default_factory=list)
    observations: list[PlanItem] = Field(default_factory=list)
    source: str = "rules"  # "azure-openai" | "rules"
    notes: str = ""
    all_records: bool = False  # the note asks for all medical records: every record goes in the packet


class PlanRequest(BaseModel):
    service_note: str
    use_llm: bool = True


class RunRequest(BaseModel):
    patient_id: str
    plan: Plan
    apply_dates: bool = True       # off = no period/date params (handy on the sandbox)
    title_fallback: bool = False   # also match unmapped notes to plan codes by title (close matches count)
    discover: bool = False         # list every other record the patient has ("Not downloaded")


class PacketRequest(BaseModel):
    plan: Plan
    result: dict  # the /api/run response
