"""Allow-list of service-note document types (LOINC codes) plus keyword rules.

The LLM may only pick codes from DOCUMENT_CODES / OBSERVATION_CODES; anything
else it returns is dropped. The keyword RULES run when the user turns the LLM off; the title keywords
drive the "match by title" mode for notes Cerner never mapped to LOINC.

Codes are the commonly used LOINC codes for chart-chase work. Confirm them
against loinc.org and your NCQA value sets before production use.
"""

DOCUMENT_CODES: dict[str, dict] = {
    "11506-3": {"label": "Progress note", "title_keywords": ["progress", "office visit", "telehealth", "clinic note", "follow up", "follow-up"]},
    "18842-5": {"label": "Discharge summary", "title_keywords": ["discharge summary", "dc summary"]},
    "8653-8": {"label": "Hospital discharge instructions", "title_keywords": ["discharge instruction"]},
    "34117-2": {"label": "History and physical note", "title_keywords": ["history and physical", "h&p", "h & p"]},
    "47039-3": {"label": "Hospital admission history and physical", "title_keywords": ["admission h&p", "admission history", "admission notice", "admitted"]},
    "34111-5": {"label": "Emergency department note", "title_keywords": ["emergency", "ed note", "ed provider"]},
    "11488-4": {"label": "Consult note", "title_keywords": ["consult"]},
    "57133-1": {"label": "Referral note", "title_keywords": ["referral"]},
    "10183-2": {"label": "Hospital discharge medications", "title_keywords": ["discharge medication", "med rec", "medication reconciliation"]},
    "10160-0": {"label": "History of medication use", "title_keywords": ["medication history", "medication list"]},
    "56445-0": {"label": "Medication summary", "title_keywords": ["medication summary"]},
    "11502-2": {"label": "Laboratory report", "title_keywords": ["lab report", "laboratory"]},
    "28570-0": {"label": "Procedure note", "title_keywords": ["procedure note", "operative"]},
    "18748-4": {"label": "Diagnostic imaging study", "title_keywords": ["radiology", "imaging", "x-ray", "mri"]},
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

GENERIC_DOCUMENTS = ["11506-3", "34117-2", "11488-4", "18842-5", "8653-8", "34111-5",
                     "28570-0", "11502-2", "18748-4", "57133-1", "10160-0"]

# (phrases found in the service note, "document" | "observation", codes)
RULES: list[tuple[list[str], str, list[str]]] = [
    (["inpatient admission", "admission order"], "document", ["34117-2", "47039-3", "34111-5"]),
    (["progress note", "telehealth"], "document", ["11506-3"]),
    (["discharge summar"], "document", ["18842-5"]),
    (["discharge instruction"], "document", ["8653-8"]),
    (["medication reconcil", "medication list", "medication review"], "document", ["10183-2", "10160-0", "56445-0"]),
    (["consultation"], "document", ["11488-4"]),
    (["referral"], "document", ["57133-1"]),
    (["member history"], "document", ["34117-2"]),
    (["lab result", "laboratory record"], "document", ["11502-2"]),
    (["blood pressure"], "observation", ["85354-9", "8480-6", "8462-4"]),
    (["hemoglobin a1c", "hba1c"], "observation", ["4548-4", "17856-6", "4549-2", "17855-8"]),
    (["gmi"], "observation", ["97506-0"]),
]

MEASURES = ["TRC", "CBP", "BPD", "GSD"]


def doc_label(code: str) -> str:
    return DOCUMENT_CODES.get(code, {}).get("label", code)


def obs_label(code: str) -> str:
    return OBSERVATION_CODES.get(code, {}).get("label", code)
