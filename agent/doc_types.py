"""Allow-list of service-note document types (LOINC codes) plus keyword rules.

The LLM may only pick codes from DOCUMENT_CODES / OBSERVATION_CODES; anything
else it returns is dropped. The keyword RULES run when the user turns the LLM off; the title keywords
drive the "match by title" mode for notes Cerner never mapped to LOINC.

Codes are the commonly used LOINC codes for chart-chase work. Confirm them
against loinc.org and your NCQA value sets before production use.
"""

# Codes marked "seen on Cerner" are the ones the Cerner sandbox actually puts on
# DocumentReference.type; title keywords include Cerner's own note titles
# (e.g. "Inpatient Clinical Summary" is how Cerner titles discharge summaries).
DOCUMENT_CODES: dict[str, dict] = {
    "11506-3": {"label": "Progress note", "title_keywords": ["progress", "office visit", "telehealth", "clinic note", "follow up", "follow-up"]},  # seen on Cerner
    "18842-5": {"label": "Discharge summary", "title_keywords": ["discharge summary", "dc summary", "inpatient clinical summary"]},  # seen on Cerner
    "8653-8": {"label": "Hospital discharge instructions", "title_keywords": ["discharge instruction"]},
    "34117-2": {"label": "History and physical note", "title_keywords": ["history and physical", "h&p", "h & p"]},  # seen on Cerner
    "83805-2": {"label": "Admission note (physician)", "title_keywords": ["admission note", "admission evaluation", "admit note"]},  # seen on Cerner
    "47039-3": {"label": "Hospital admission history and physical", "title_keywords": ["admission h&p", "admission history", "admission notice", "admitted"]},
    "28623-7": {"label": "Nursing narrative note", "title_keywords": ["nursing narrative", "nursing note", "nurse note", "nursing progress"]},  # seen on Cerner
    "28568-4": {"label": "Emergency department note (physician)", "title_keywords": ["ed physician", "ed provider", "emergency physician", "ed note"]},  # seen on Cerner
    "57053-1": {"label": "Emergency department note (nurse)", "title_keywords": ["ed nursing", "ed nurse", "emergency nursing", "triage"]},  # seen on Cerner
    "34111-5": {"label": "Emergency department note", "title_keywords": ["emergency"]},
    "34108-1": {"label": "Outpatient note", "title_keywords": ["outpatient", "ambulatory clinical summary", "ambulatory note"]},  # seen on Cerner
    "18761-7": {"label": "Transfer summary", "title_keywords": ["transfer summary", "transfer note"]},  # seen on Cerner
    "11488-4": {"label": "Consult note", "title_keywords": ["consult"]},  # seen on Cerner
    "34839-1": {"label": "Rheumatology consult note", "title_keywords": ["rheumatology"]},  # seen on Cerner
    "57133-1": {"label": "Referral note", "title_keywords": ["referral"]},  # seen on Cerner
    "10183-2": {"label": "Hospital discharge medications", "title_keywords": ["discharge medication", "med rec", "medication reconciliation"]},
    "10160-0": {"label": "History of medication use", "title_keywords": ["medication history", "medication list"]},
    "56445-0": {"label": "Medication summary", "title_keywords": ["medication summary"]},
    "11502-2": {"label": "Laboratory report", "title_keywords": ["lab report", "laboratory"]},
    "28570-0": {"label": "Procedure note", "title_keywords": ["procedure note"]},  # seen on Cerner
    "11504-8": {"label": "Surgical operation note", "title_keywords": ["operative report", "operative", "operation note", "op note"]},  # seen on Cerner
    "18748-4": {"label": "Diagnostic imaging study", "title_keywords": ["radiology", "imaging", "x-ray", "mri"]},
    "34895-3": {"label": "Education note", "title_keywords": ["education"]},  # seen on Cerner
    "89821-3": {"label": "Wound care progress note", "title_keywords": ["wound care", "wound"]},  # seen on Cerner
}

OBSERVATION_CODES: dict[str, dict] = {
    "85354-9": {"label": "Blood pressure panel"},
    "8480-6": {"label": "Systolic blood pressure"},
    "8462-4": {"label": "Diastolic blood pressure"},
    "4548-4": {"label": "Hemoglobin A1c"},
    "17856-6": {"label": "Hemoglobin A1c by HPLC"},
    "4549-2": {"label": "Hemoglobin A1c by electrophoresis"},
    "17855-8": {"label": "Hemoglobin A1c, calculated"},
    "97506-0": {"label": "Glucose management indicator (GMI)"},
}

# "All medical records": every general clinical-note type. Records of any other type are
# still found by the all-records sweep and added to the packet (see RunRequest.include_other).
GENERIC_DOCUMENTS = ["11506-3", "34117-2", "83805-2", "11488-4", "18842-5", "8653-8", "28568-4",
                     "57053-1", "34111-5", "34108-1", "18761-7", "28623-7", "28570-0", "11504-8",
                     "11502-2", "18748-4", "57133-1", "10160-0"]

# ---- Clinical intent vocabulary (the agent's output schema) ----

MODES = ["measure_specific", "all_records", "general"]
# strict: only the plan | title_match: + Match By Doc Name | broad: + List Other Records
# progress_notes_if_missing: when a required element is missing, also send every progress
# note in the window and note the missing elements (as TRC service notes ask)
FALLBACK_STRATEGIES = ["strict", "title_match", "broad", "progress_notes_if_missing"]
DATE_WINDOW_PURPOSES = ["job_envelope", "measurement_year"]
TARGET_KINDS = {
    "hba1c": ["4548-4", "17856-6", "4549-2", "17855-8"],
    "gmi": ["97506-0"],
    "blood_pressure": ["85354-9", "8480-6", "8462-4"],
}

# resource -> purpose -> {"label", "codes"}. The agent picks purposes; the app maps them to codes.
PURPOSES: dict[str, dict[str, dict]] = {
    "Patient": {
        "demographics": {"label": "Member demographics / biographical sheet", "codes": []},
    },
    "DocumentReference": {
        "progress_notes": {"label": "Progress, office and telehealth visit notes", "codes": ["11506-3"]},
        "member_history": {"label": "History and physical", "codes": ["34117-2"]},
        "referrals_consults": {"label": "Referrals, specialist and consultation notes", "codes": ["11488-4", "57133-1", "34839-1"]},
        "discharge_summary": {"label": "Hospital discharge summaries", "codes": ["18842-5"]},
        "discharge_instructions": {"label": "Hospital discharge instructions", "codes": ["8653-8"]},
        "admission": {"label": "Inpatient admission notes / evidence of admission", "codes": ["83805-2", "47039-3", "34117-2"]},
        "emergency": {"label": "Emergency department notes", "codes": ["28568-4", "57053-1", "34111-5"]},
        "medication_reconciliation": {"label": "Medication reconciliation, lists and reviews", "codes": ["10183-2", "10160-0", "56445-0"]},
        "nursing": {"label": "Nursing narrative notes", "codes": ["28623-7"]},
        "outpatient": {"label": "Outpatient / ambulatory notes", "codes": ["34108-1"]},
        "transfer": {"label": "Transfer summaries", "codes": ["18761-7"]},
        "procedures": {"label": "Procedure and operative notes", "codes": ["28570-0", "11504-8"]},
        "imaging": {"label": "Diagnostic imaging reports", "codes": ["18748-4"]},
        "lab_documents": {"label": "Laboratory report documents", "codes": ["11502-2"]},
        "education": {"label": "Patient education notes", "codes": ["34895-3"]},
        "wound_care": {"label": "Wound care notes", "codes": ["89821-3"]},
        "all_clinical_notes": {"label": "All clinical notes", "codes": GENERIC_DOCUMENTS},
    },
    "Observation": {
        "hba1c_or_gmi": {"label": "HbA1c / GMI results", "codes": TARGET_KINDS["hba1c"] + TARGET_KINDS["gmi"]},
        "blood_pressure": {"label": "Blood pressure readings", "codes": TARGET_KINDS["blood_pressure"]},
    },
    "DiagnosticReport": {
        "lab_report": {"label": "Laboratory reports", "codes": ["LAB"]},  # DiagnosticReport.category
    },
    # Encounter.class codes (v3 ActCode). Cerner ignores ?class=, so the app sorts them itself.
    "Encounter": {
        "inpatient_stays": {"label": "Inpatient stays (evidence of admission)", "codes": ["IMP", "ACUTE", "NONAC", "OBSENC", "SS"]},
        "emergency_visits": {"label": "Emergency department visits", "codes": ["EMER"]},
        "outpatient_visits": {"label": "Outpatient / ambulatory visits", "codes": ["AMB", "VR", "HH", "FLD"]},
    },
}

# How a missing required element is written on the missing-items sheet.
GAP_NOTATION: dict[str, str] = {
    "admission": "Hospital stay not reported to this office (no admission documentation)",
    "inpatient_stays": "No inpatient stay found in the record",
    "discharge_summary": "Discharge summary not in the record",
    "discharge_instructions": "Discharge instructions not in the record",
    "medication_reconciliation": "Discharge medications not reconciled",
    "progress_notes": "No progress / telehealth visit note found",
    "referrals_consults": "No specialist consultation report in the record",
    "lab_report": "No laboratory report found",
    "hba1c_or_gmi": "No HbA1c / GMI result found",
    "blood_pressure": "No blood pressure reading found",
}

# Keyword rules (used when the LLM is off): phrase in the note -> (resource, purpose).
RULES: list[tuple[list[str], str, str]] = [
    (["inpatient admission", "admission order", "admission note"], "DocumentReference", "admission"),
    (["progress note", "telehealth", "office visit"], "DocumentReference", "progress_notes"),
    (["discharge summar"], "DocumentReference", "discharge_summary"),
    (["discharge instruction"], "DocumentReference", "discharge_instructions"),
    (["medication reconcil", "medication list", "medication review"], "DocumentReference", "medication_reconciliation"),
    (["consultation", "consult", "specialist", "referral"], "DocumentReference", "referrals_consults"),
    (["member history", "history and physical"], "DocumentReference", "member_history"),
    (["nursing", "nurse note"], "DocumentReference", "nursing"),
    (["emergency", "ed visit", "ed note"], "DocumentReference", "emergency"),
    (["outpatient"], "DocumentReference", "outpatient"),
    (["inpatient", "hospital stay", "admission"], "Encounter", "inpatient_stays"),
    (["outpatient"], "Encounter", "outpatient_visits"),
    (["emergency", "ed visit"], "Encounter", "emergency_visits"),
    (["transfer"], "DocumentReference", "transfer"),
    (["operative", "surgical", "surgery", "procedure note"], "DocumentReference", "procedures"),
    (["imaging", "radiology", "x-ray"], "DocumentReference", "imaging"),
    (["lab result", "laboratory record", "lab report", "laboratory report"], "DiagnosticReport", "lab_report"),
    (["blood pressure", "bp reading"], "Observation", "blood_pressure"),
    (["hemoglobin a1c", "hba1c", "a1c", "gmi"], "Observation", "hba1c_or_gmi"),
]
FALLBACK_PHRASES = ["if you are not sure", "send all progress notes", "notation of the elements"]

TARGET_RULES: list[tuple[list[str], str]] = [
    (["hemoglobin a1c", "hba1c", "a1c"], "hba1c"),
    (["gmi", "glucose management indicator"], "gmi"),
    (["blood pressure", "bp reading"], "blood_pressure"),
]

ALL_RECORDS_PHRASES = ["all medical records", "all records", "complete medical record", "entire medical record"]

MEASURES = ["TRC", "CBP", "BPD", "GSD"]


def purpose_info(resource: str, purpose: str) -> dict | None:
    return PURPOSES.get(resource, {}).get(purpose)


def doc_label(code: str) -> str:
    return DOCUMENT_CODES.get(code, {}).get("label", code)


def obs_label(code: str) -> str:
    return OBSERVATION_CODES.get(code, {}).get("label", code)
