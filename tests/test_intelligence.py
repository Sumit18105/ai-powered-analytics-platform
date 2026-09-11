import polars as pl

from app.services.intelligence import executive_insights, executive_report, scenario_analysis


def sample_frame():
    return pl.DataFrame({
        "Date": pl.date_range(__import__("datetime").date(2026, 1, 1), __import__("datetime").date(2026, 4, 1), interval="1mo", eager=True),
        "Region": ["North", "South", "North", "South"],
        "Revenue": [100.0, 80.0, 140.0, 70.0],
        "Cost": [60.0, 55.0, 70.0, 50.0],
    })


def test_intelligence_generates_evidence():
    result = executive_insights(sample_frame())
    assert result["items"]
    report = executive_report(sample_frame(), "Sales")
    assert report["title"] == "Executive report — Sales"
    assert report["sections"]


def test_scenario_analysis_is_non_mutating():
    frame = sample_frame()
    result = scenario_analysis(frame, "Revenue", 10, "Region")
    assert result["items"]
    assert frame["Revenue"].to_list() == [100.0, 80.0, 140.0, 70.0]
