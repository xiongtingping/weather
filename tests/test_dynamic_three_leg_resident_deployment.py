from __future__ import annotations

import plistlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_resident_launcher_uses_controlled_project_python_without_loading_dotenv() -> None:
    text = (ROOT / "scripts" / "resident_dynamic_three_leg_execution.sh").read_text(
        encoding="utf-8"
    )
    assert "resolve_project_python" in text
    assert "exec /usr/bin/python3" not in text
    assert 'resident_dynamic_three_leg_execution_loop.py" "$@"' in text
    assert "load_repo_env_file" not in text
    assert "load_polymarket_launchctl_env" not in text


def test_resident_plist_is_default_disabled_with_every_live_flag_zero() -> None:
    value = plistlib.loads(
        (ROOT / "launchd" / "com.weather.dynamic_three_leg_execution.plist").read_bytes()
    )
    assert value["Disabled"] is True
    assert value["RunAtLoad"] is False
    environment = value["EnvironmentVariables"]
    for key in (
        "PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED",
        "PM_DYNAMIC_THREE_LEG_DECISION_RESIDENT_ENABLED",
        "PM_DYNAMIC_THREE_LEG_PRODUCTION_ENABLED",
        "PM_DYNAMIC_THREE_LEG_LIVE_SUBMIT_ENABLED",
        "PM_MARKET_ALPHA_TAKE_PROFIT_PRODUCTION_ENABLED",
        "PM_MARKET_ALPHA_TAKE_PROFIT_LIVE_SUBMIT_ENABLED",
    ):
        assert environment[key] == "0"


def test_installer_can_only_install_unloaded_disabled_unit() -> None:
    text = (ROOT / "scripts" / "install_dynamic_three_leg_resident_disabled.sh").read_text(
        encoding="utf-8"
    )
    assert 'launchctl disable "$DOMAIN/$LABEL"' in text
    assert "launchctl bootout" in text
    assert "launchctl bootstrap" not in text
    assert "launchctl kickstart" not in text
    assert "required_zero" in text


def test_resident_reconciles_before_supersession_and_supersedes_before_new_decision() -> None:
    text = (ROOT / "resident_dynamic_three_leg_execution_loop.py").read_text(
        encoding="utf-8"
    )
    reconcile = text.index("reconcile_id = _next_reconcile")
    dynamic_supersession = text.index(
        "supersede_stale_prepared_dynamic_three_leg_intents(", reconcile
    )
    alpha_supersession = text.index(
        "supersede_stale_prepared_market_alpha_intents(", dynamic_supersession
    )
    decision = text.index("worker.process_ready_latest_outbox(", alpha_supersession)
    assert reconcile < dynamic_supersession < alpha_supersession < decision
