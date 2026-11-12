"""Redact PII/PHI from text before it goes to the LLM (phileas-redact, runs locally).

Only the redacted text leaves this machine. Bare years ("2025") and LOINC codes are kept, so
the model can still read the date window and the record types. Full dates (a DOB, 03/14/1962)
are redacted; set LLM_REDACT_DATES=false to keep them.

If redaction is on and phileas is missing or fails, nothing is sent (fail closed).
Turn it off with LLM_REDACT=false.
"""
from __future__ import annotations

import os
import re

FORMAT = "[REDACTED-%t]"

# phileas identifier -> its strategies key
_IDENTIFIERS = {
    "age": "ageFilterStrategies",
    "emailAddress": "emailAddressFilterStrategies",
    "creditCard": "creditCardFilterStrategies",
    "ssn": "ssnFilterStrategies",
    "phoneNumber": "phoneNumberFilterStrategies",
    "ipAddress": "ipAddressFilterStrategies",
    "url": "urlFilterStrategies",
    "zipCode": "zipCodeFilterStrategies",
    "vin": "vinFilterStrategies",
    "bitcoinAddress": "bitcoinAddressFilterStrategies",
    "bankRoutingNumber": "bankRoutingNumberFilterStrategies",
    "macAddress": "macAddressFilterStrategies",
    "streetAddress": "streetAddressFilterStrategies",
    "trackingNumber": "trackingNumberFilterStrategies",
    "driversLicense": "driversLicenseFilterStrategies",
    "ibanCode": "ibanCodeFilterStrategies",
    "passportNumber": "passportNumberFilterStrategies",
    "date": "dateFilterStrategies",
}

# phileas has no offline name detector, so catch the labelled values a service note carries.
# The value runs to the end of the line or the next separator.
_LABEL = r"(?i:\b(?:{labels})\b\s*(?:#|no\.?|number)?\s*[:=]\s*)"
_PATTERNS = [
    ("person", _LABEL.format(labels=r"patient(?:\s+name)?|member(?:\s+name)?|name|first\s+name|last\s+name")
     + r"[A-Z][A-Za-z'.\-]*(?:(?:, ?| )(?!(?i:member|patient|dob|mrn|id|date)\b)[A-Z][A-Za-z'.\-]*){0,3}"),
    ("dob", _LABEL.format(labels=r"dob|d\.o\.b\.?|date\s+of\s+birth|birth\s*date") + r"[0-9A-Za-z/\-., ]{6,20}\d"),
    ("member-id", _LABEL.format(labels=r"mrn|member\s*id|patient\s*id|subscriber\s*id|medicaid\s*id|medicare\s*id|"
                                       r"hicn|mbi|policy|account")
     + r"[A-Za-z0-9\-]{4,}"),
]
# The words that follow "Name:" in a service note's requirement text, not a person.
_NOT_NAMES = r"(?i)^(?:patient(?:\s+name)?|member(?:\s+name)?|name|first\s+name|last\s+name)\s*[:=]\s*" \
             r"(?:and|date|dob|of|the|required|n/?a|none)\b"


# LOINC codes (11506-3) look like ZIP codes to phileas; they are shielded during redaction.
_LOINC = re.compile(r"\b\d{3,5}-\d\b")


class RedactionError(RuntimeError):
    pass


def enabled() -> bool:
    return os.getenv("LLM_REDACT", "true").strip().lower() not in ("0", "false", "no", "off")


def _strategy(label: str) -> list[dict]:
    return [{"strategy": "REDACT", "redactionFormat": FORMAT.replace("%t", label) if label else FORMAT}]


def _policy_dict() -> dict:
    keep_dates = os.getenv("LLM_REDACT_DATES", "true").strip().lower() in ("0", "false", "no", "off")
    identifiers = {name: {key: _strategy("")} for name, key in _IDENTIFIERS.items()
                   if not (name == "date" and keep_dates)}
    identifiers["patterns"] = [{"pattern": p, "label": label, "patternFilterStrategies": _strategy(label)}
                               for label, p in _PATTERNS]
    return {"name": "llm-outbound", "identifiers": identifiers, "ignoredPatterns": [_NOT_NAMES]}


_service = None
_policy = None


def _load():
    global _service, _policy
    if _service is None:
        try:
            from phileas.policy.policy import Policy
            from phileas.services.filter_service import FilterService
        except ImportError as exc:
            raise RedactionError("phileas-redact is not installed (pip install -r requirements.txt); "
                                 "nothing was sent to the model. Set LLM_REDACT=false to skip redaction.") from exc
        _policy = Policy.from_dict(_policy_dict())
        _service = FilterService()
    return _service, _policy


def redact(text: str) -> str:
    """The text with PII/PHI replaced by [REDACTED-<type>] markers (unchanged when redaction is off)."""
    if not enabled() or not text:
        return text
    service, policy = _load()
    codes: list[str] = []

    def shield(m: re.Match) -> str:
        codes.append(m.group(0))
        return f"LOINCCODE{len(codes) - 1}X"

    shielded = _LOINC.sub(shield, text)
    try:
        out = service.filter(policy=policy, context="llm", document_id="service-note", text=shielded).filtered_text
    except Exception as exc:
        raise RedactionError(f"Redaction failed ({type(exc).__name__}); nothing was sent to the model.") from exc
    return re.sub(r"LOINCCODE(\d+)X", lambda m: codes[int(m.group(1))], out)
