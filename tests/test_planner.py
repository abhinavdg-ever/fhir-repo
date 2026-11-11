import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.planner import date_window, rules_plan, validate_llm_json  # noqa: E402

TODAY = date(2026, 11, 11)


def test_validate_drops_invented_code():
    raw = {
        "measures": ["TRC", "XYZ"],
        "start": "2026-01-01",
        "end_exclusive": "2026-11-12",
        "documents": [{"code": "18842-5", "reason": "discharge summaries"},
                      {"code": "99999-9", "reason": "made up"},
                      {"code": "18842-5", "reason": "dupe"}],
        "observations": [{"code": "1234-5"}],
    }
    plan = validate_llm_json(raw, "TRC 2026", TODAY)
    assert [d.code for d in plan.documents] == ["18842-5"]
    assert plan.observations == []
    assert plan.measures == ["TRC"]
    assert plan.source == "azure-openai"


def test_validate_bad_dates_use_window():
    plan = validate_llm_json({"start": "2026-05-01", "end_exclusive": "2026-01-01"}, "2025 and 2026", TODAY)
    assert (plan.start, plan.end) == ("2025-01-01", "2026-11-12")
    plan = validate_llm_json({"start": "nope"}, "2026", TODAY)
    assert (plan.start, plan.end) == ("2026-01-01", "2026-11-12")


def test_date_window():
    assert date_window("2024 records", TODAY) == ("2024-01-01", "2025-01-01")
    assert date_window("no year", TODAY) == ("2026-01-01", "2026-11-12")


def test_rules_cbp_adds_progress_note():
    plan = rules_plan("CBP - last blood pressure reading in 2026", TODAY)
    assert {o.code for o in plan.observations} == {"85354-9", "8480-6", "8462-4"}
    assert "11506-3" in {d.code for d in plan.documents}
    assert plan.measures == ["CBP"]
