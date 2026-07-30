from pathlib import Path


UI_INDEX = Path(__file__).resolve().parents[1] / "ui" / "index.html"


def test_model_dashboard_exposes_missing_and_freshness_metadata() -> None:
    html = UI_INDEX.read_text(encoding="utf-8")

    assert "function dashboardModelPeakInfo(item, modelKey)" in html
    assert "function dashboardTafPeakInfo(item)" in html
    assert "forecast_weak" in html
    assert "未物化" in html
    assert "检查 " in html
    assert "可能陈旧" in html
    assert "“—”表示未物化，不代表 0" in html


def test_model_dashboard_does_not_compute_frontend_average_or_bucket() -> None:
    html = UI_INDEX.read_text(encoding="utf-8")

    assert "不在前端计算" in html
