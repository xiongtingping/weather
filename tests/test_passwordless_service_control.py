from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "weather_service_privileged_helper.sh"
INSTALLER = ROOT / "scripts" / "install_passwordless_service_control.sh"
CONTROL = ROOT / "scripts" / "weather_service_control.sh"


def _run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(script), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_privileged_helper_rejects_unknown_action_before_launchctl() -> None:
    result = _run(HELPER, "restart-arbitrary-service")
    assert result.returncode == 64
    assert "retired" in result.stderr

