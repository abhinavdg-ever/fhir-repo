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

# (phrases found in the service note, "document" | "observation", codes)
RULES: list[tuple[list[str], str, list[str]]] = [
    (["inpatient admission", "admission order", "admission note"], "document", ["34117-2", "83805-2", "47039-3", "28568-4", "34111-5"]),
    (["progress note", "telehealth"], "document", ["11506-3"]),
    (["discharge summar"], "document", ["18842-5"]),
    (["discharge instruction"], "document", ["8653-8"]),
    (["medication reconcil", "medication list", "medication review"], "document", ["10183-2", "10160-0", "56445-0"]),
    (["consultation"], "document", ["11488-4"]),
    (["referral"], "document", ["57133-1"]),
    (["member history"], "document", ["34117-2"]),
    (["lab result", "laboratory record"], "document", ["11502-2"]),
    (["nursing", "nurse note"], "document", ["28623-7"]),
    (["emergency", "ed visit", "ed note"], "document", ["28568-4", "57053-1", "34111-5"]),
    (["outpatient"], "document", ["34108-1"]),
    (["transfer"], "document", ["18761-7"]),
    (["operative", "surgical", "surgery"], "document", ["11504-8", "28570-0"]),
    (["blood pressure"], "observation", ["85354-9", "8480-6", "8462-4"]),
    (["hemoglobin a1c", "hba1c"], "observation", ["4548-4", "17856-6", "4549-2", "17855-8"]),
    (["gmi"], "observation", ["97506-0"]),
]

ALL_RECORDS_PHRASES = ["all medical records", "all records", "complete medical record", "entire medical record"]

MEASURES = ["TRC", "CBP", "BPD", "GSD"]


def doc_label(code: str) -> str:
    return DOCUMENT_CODES.get(code, {}).get("label", code)


def obs_label(code: str) -> str:
    return OBSERVATION_CODES.get(code, {}).get("label", code)
