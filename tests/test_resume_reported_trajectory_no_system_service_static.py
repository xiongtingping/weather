from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/resume_reported_trajectory_no_system_service.sh"
USER_DOMAIN_ENTRY = "scripts/install_reported_trajectory_no_user_services.sh"


def test_retired_resume_entry_fails_before_any_service_control() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "exit 78" in text
    assert "launchctl" not in text
    assert "sudo" not in text
    assert "/Library/LaunchDaemons" not in text
    assert USER_DOMAIN_ENTRY in text
    assert "当前登录用户" in text


def test_retired_resume_entry_has_no_old_system_resume_logic() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "bootstrap" not in text
    assert "bootout" not in text
    assert "kickstart" not in text
    assert "system/" not in text
