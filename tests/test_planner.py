import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.planner import date_window, rules_plan, validate_llm_json  # noqa: E402

TODAY = date(2026, 11, 11)
GSD_NOTE = ("GSD - REQUIRED - Member demographic sheet: Name and date of birth. Latest 2025 hemoglobin A1c (HbA1c), "
            "GMI value from 2025 lab result or progress note. Referrals, specialist or consultation notes. "
            "Hospital laboratory records, Progress notes.")
TRC_NOTE = ("TRC - REQUIRED - Member demographic sheet: Name and date of birth, 2026 Evidence of discharge medication "
            "reconciliations, medication lists and medication reviews, 2026 Hospital discharge summaries, 2026 Hospital "
            "discharge instructions, 2026 Documentation of notification of inpatient admissions or evidence of admission "
            "orders, 2026 Specialist consultation reports, 2026 Progress notes and telehealth's. Require inpatient and "
            "outpatient records. If you are not sure you have this information within the record, please send all "
            "progress notes for 2026 with notation of the elements you do not have in the record (i.e.: Discharge "
            "medications not reconciled, or hospital stay not reported to this office.) Note: Inpatient notes should be "
            "only from one facility per record.")

# The agent output for the GSD note, in the agreed schema.
GSD_INTENT = {
    "measure_codes": ["GSD"],
    "mode": "measure_specific",
    "date_windows": [{"purpose": "job_envelope", "from": "2025-01-01", "to": "2025-12-31"},
                     {"purpose": "measurement_year", "from": "2025-01-01", "to": "2025-12-31"}],
    "resource_groups": [
        {"resource": "Patient", "purpose": "demographics", "required": True},
        {"resource": "DiagnosticReport", "purpose": "lab_report", "required": True},
        {"resource": "Observation", "purpose": "hba1c_or_gmi", "required": True},
        {"resource": "DocumentReference", "purpose": "progress_notes", "required": False},
        {"resource": "DocumentReference", "purpose": "member_history", "required": False},
        {"resource": "DocumentReference", "purpose": "referrals_consults", "required": False},
    ],
    "clinical_targets": [{"kind": "hba1c", "any_history": False, "through": None}],
    "fallback_strategy": "strict",
    "notes": "GSD - REQUIRED - ...",
}


def test_gsd_intent_becomes_the_plan():
    plan = validate_llm_json(GSD_INTENT, GSD_NOTE, TODAY)
    intent = plan.intent.model_dump(by_alias=True)
    assert intent["measure_codes"] == ["GSD"] and intent["mode"] == "measure_specific"
    assert intent["date_windows"] == GSD_INTENT["date_windows"]
    assert [(g["resource"], g["purpose"], g["required"]) for g in intent["resource_groups"]][:6] == \
        [(g["resource"], g["purpose"], g["required"]) for g in GSD_INTENT["resource_groups"]]
    assert set(intent) == set(GSD_INTENT)
    # executable plan
    assert (plan.start, plan.end) == ("2025-01-01", "2026-01-01")          # job envelope, end exclusive
    assert (plan.obs_start, plan.obs_end) == ("2025-01-01", "2026-01-01")  # measurement year
    assert [r.code for r in plan.reports] == ["LAB"] and plan.reports[0].required
    assert "4548-4" in {o.code for o in plan.observations}
    assert {"11506-3", "34117-2", "11488-4"} <= {d.code for d in plan.documents}
    assert all(not d.required for d in plan.documents)


def test_validate_drops_unknown_groups_and_targets():
    raw = {**GSD_INTENT,
           "measure_codes": ["GSD", "XYZ"],
           "resource_groups": GSD_INTENT["resource_groups"] + [
               {"resource": "DocumentReference", "purpose": "made_up", "required": True},
               {"resource": "Condition", "purpose": "problems", "required": True}],
           "clinical_targets": [{"kind": "hba1c"}, {"kind": "cholesterol"}],
           "fallback_strategy": "whatever"}
    plan = validate_llm_json(raw, GSD_NOTE, TODAY)
    groups = {(g.resource, g.purpose) for g in plan.intent.resource_groups}
    assert ("DocumentReference", "made_up") not in groups and ("Condition", "problems") not in groups
    assert [t.kind for t in plan.intent.clinical_targets] == ["hba1c", "gmi"]  # gmi named in the note
    assert plan.intent.measure_codes == ["GSD"] and plan.intent.fallback_strategy == "strict"
    assert "Dropped unknown group" in plan.notes


def test_validate_bad_dates_fall_back_and_cap_at_today():
    raw = {"date_windows": [{"purpose": "job_envelope", "from": "2026-05-01", "to": "2026-01-01"}]}
    plan = validate_llm_json(raw, "records 2025 and 2026", TODAY)
    assert (plan.start, plan.end) == ("2025-01-01", "2026-11-12")
    raw = {"date_windows": [{"purpose": "job_envelope", "from": "2026-01-01", "to": "2027-06-30"}]}
    plan = validate_llm_json(raw, "TRC 2026", TODAY)
    assert plan.intent.date_windows[0].to == "2026-11-11" and plan.end == "2026-11-12"


def test_date_window():
    assert date_window("2024 records", TODAY) == ("2024-01-01", "2025-01-01")
    assert date_window("no year", TODAY) == ("2026-01-01", "2026-11-12")


def test_rules_cbp_requires_bp_and_adds_progress_note():
    plan = rules_plan("CBP - last blood pressure reading in 2026", TODAY)
    assert {o.code for o in plan.observations} == {"85354-9", "8480-6", "8462-4"}
    assert all(o.required for o in plan.observations)
    assert "11506-3" in {d.code for d in plan.documents}
    assert plan.measures == ["CBP"]


def test_rules_trc_intent():
    plan = rules_plan(TRC_NOTE, TODAY)
    intent = plan.intent
    required = {g.purpose for g in intent.resource_groups if g.required}
    assert {"admission", "discharge_summary", "discharge_instructions", "medication_reconciliation",
            "progress_notes", "demographics"} <= required
    assert {("Encounter", "inpatient_stays"), ("Encounter", "outpatient_visits")} <= \
        {(g.resource, g.purpose) for g in intent.resource_groups}
    assert intent.fallback_strategy == "progress_notes_if_missing"
    assert [w.purpose for w in intent.date_windows] == ["job_envelope", "measurement_year"]
    assert {"83805-2", "18842-5", "8653-8", "10183-2", "11506-3"} <= {d.code for d in plan.documents}
    assert [e.code for e in plan.encounters] == ["inpatient_stays", "outpatient_visits"]


def test_build_messages_lists_purposes_and_note():
    from agent.planner import build_messages
    system, user = build_messages("TRC 2026 discharge summaries", TODAY)
    assert system["role"] == "system" and "DiagnosticReport: lab_report" in system["content"]
    assert "Encounter: inpatient_stays" in system["content"] and '"measure_codes"' in system["content"]
    assert user["content"] == "Today is 2026-11-11.\nService note:\nTRC 2026 discharge summaries"
