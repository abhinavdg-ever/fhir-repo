import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import redact as r  # noqa: E402
from agent.planner import build_messages  # noqa: E402

NOTE = ("Patient: John Q Smith, DOB: 03/14/1962, MRN: 12742400, Member ID: XK99812. Call 555-123-4567 or "
        "jsmith@example.com, 12 Main Street. TRC 2026 discharge summaries, progress notes 11506-3 and 4548-4.")


def test_redacts_pii_keeps_years_and_loinc():
    out = r.redact(NOTE)
    for secret in ("John", "Smith", "1962", "12742400", "XK99812", "555-123-4567", "jsmith", "Main Street"):
        assert secret not in out
    assert "[REDACTED-person]" in out and "[REDACTED-phone-number]" in out
    assert "TRC 2026 discharge summaries" in out and "11506-3" in out and "4548-4" in out


def test_requirement_text_is_untouched():
    note = "GSD - REQUIRED - Member demographic sheet: Name and date of birth. Latest 2025 HbA1c."
    assert r.redact(note) == note


def test_prompt_sends_only_redacted_note():
    _, user = build_messages(NOTE, date(2026, 11, 11))
    assert "Smith" not in user["content"] and "12742400" not in user["content"]


def test_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("LLM_REDACT", "false")
    assert r.redact(NOTE) == NOTE


def test_cost_and_json_parsing():
    from agent import llm
    m = llm.get_model("gpt-4o")
    assert llm.cost(m, 1_000_000, 1_000_000) == 12.5
    assert llm.cost(m, 1000, 0, cached_tokens=1000) == 0.00125
    assert llm.parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.parse_json('Here it is: {"a": {"b": 2}} done') == {"a": {"b": 2}}
    assert [x.api for x in llm.models() if x.deployment == "claude-haiku-4-5"] == ["anthropic"]


def test_price_override(monkeypatch):
    from agent import llm
    monkeypatch.setenv("LLM_PRICES", '{"gpt-4o": [1, 2]}')
    m = llm.get_model("gpt-4o")
    assert (m.input, m.output, m.price_source) == (1.0, 2.0, "LLM_PRICES (.env)")
