import plistlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_hourly_forward_capture_launchd_is_research_only_and_uses_chengdu_date():
    with (ROOT / "launchd" / "com.weather.forecast_source_forward_capture.plist").open("rb") as handle:
        payload = plistlib.load(handle)

    command = " ".join(payload["ProgramArguments"])
    assert payload["Label"] == "com.weather.forecast_source_forward_capture"
    assert payload["StartCalendarInterval"] == {"Minute": 0}
    assert payload["ProgramArguments"][:2] == ["/bin/zsh", "-c"]
    assert "TZ=Asia/Shanghai" in command
    assert "capture_current_forecast_source_forward.py" in command
    assert "--allow-research-write" in command
    assert "--live" not in command
    assert "forecast_source_skill/forward" in command
