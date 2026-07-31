from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_forecast_capture_launchdaemon_is_retired() -> None:
    plist = ROOT / "launchd/com.weather.forecast_source_forward_capture.plist"
    script = (
        ROOT
        / "scripts/install_forecast_source_forward_capture_launchdaemon.sh"
    )
    text = script.read_text(encoding="utf-8")
    assert not plist.exists()
    assert "exit 78" in text
    assert "no fourth service" in text
    for forbidden in (
        "sudo",
        "launchctl",
        "/Library/LaunchDaemons",
        "bootstrap",
        "kickstart",
    ):
        assert forbidden not in text

    completed = subprocess.run(
        ["/bin/bash", str(script)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 78
