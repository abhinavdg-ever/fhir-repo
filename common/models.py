"""Request and plan contracts shared by the agent and the worker (Pydantic)."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


# ---- the agent's output: a clinical retrieval intent ----

class DateWindow(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    purpose: str                          # job_envelope | measurement_year
    from_: str = Field(alias="from")      # YYYY-MM-DD, inclusive
    to: str                               # YYYY-MM-DD, inclusive


class ResourceGroup(BaseModel):
    resource: str                         # Patient | DocumentReference | Observation | DiagnosticReport | Encounter
    purpose: str                          # from agent.doc_types.PURPOSES
    required: bool = False


class ClinicalTarget(BaseModel):
    kind: str                             # hba1c | gmi | blood_pressure
    any_history: bool = False             # True: a result from any date counts
    through: str | None = None            # optional cut-off date (inclusive)


class ClinicalIntent(BaseModel):
    measure_codes: list[str] = Field(default_factory=list)
    mode: str = "general"                 # measure_specific | all_records | general
    date_windows: list[DateWindow] = Field(default_factory=list)
    resource_groups: list[ResourceGroup] = Field(default_factory=list)
    clinical_targets: list[ClinicalTarget] = Field(default_factory=list)
    fallback_strategy: str = "strict"     # strict | title_match | broad
    notes: str = ""


# ---- the executable plan derived from the intent (what the UI edits and /api/run takes) ----

class PlanItem(BaseModel):
    code: str
    label: str
    reason: str = ""
    group: str = ""                       # the resource group purpose it came from
    required: bool = False


class Plan(BaseModel):
    """The clinical intent read from a service note: which LOINC codes, which dates."""
    measures: list[str] = Field(default_factory=list)  # subset of TRC, CBP, BPD, GSD
    start: str  # inclusive, YYYY-MM-DD
    end: str    # exclusive, YYYY-MM-DD (Cerner wants period=lt<end>)
    documents: list[PlanItem] = Field(default_factory=list)
    observations: list[PlanItem] = Field(default_factory=list)
    reports: list[PlanItem] = Field(default_factory=list)  # DiagnosticReport categories, e.g. LAB
    encounters: list[PlanItem] = Field(default_factory=list)  # Encounter purposes, e.g. inpatient_stays
    obs_start: str | None = None  # Observation window (measurement year / any_history); None = no lower bound
    obs_end: str | None = None    # exclusive
    source: str = "rules"  # "azure-openai" | "rules"
    notes: str = ""
    all_records: bool = False  # the note asks for all medical records: every record goes in the packet
    intent: ClinicalIntent | None = None  # the agent output this plan was built from


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
