import plistlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_research_pipeline_is_daily_launchd_and_never_passes_apply_flags():
    path = ROOT / "launchd" / "com.weather.forecast_source_weight_research.plist"
    with path.open("rb") as handle:
        payload = plistlib.load(handle)

    arguments = payload["ProgramArguments"]
    assert payload["Label"] == "com.weather.forecast_source_weight_research"
    assert arguments[-3:] == ["--execute", "--max-incremental-requests", "12"]
    assert "apply" not in " ".join(arguments).lower()
    assert payload["KeepAlive"] is False
    assert payload["StartCalendarInterval"] == {"Hour": 2, "Minute": 30}
