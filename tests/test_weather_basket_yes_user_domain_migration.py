from __future__ import annotations

import os
from pathlib import Path
import plistlib
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
LABELS = (
    "com.weather.weather_basket_yes",
    "com.weather.market_ws",
    "com.weather.clob_order_service",
)


def _text(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def test_three_production_plists_are_user_no_live_only() -> None:
    for label in LABELS:
        with (ROOT / "launchd" / f"{label}.plist").open("rb") as handle:
            plist = plistlib.load(handle)
        environment = plist["EnvironmentVariables"]
        assert plist["Label"] == label
        assert "UserName" not in plist
        assert "GroupName" not in plist
        assert environment["WEATHER_SERVICE_DOMAIN"] == "user"
        assert environment["PM_TARGET_EXECUTION_MODE"] == "production_no_live"
    target = plistlib.loads(
        (ROOT / "launchd/com.weather.weather_basket_yes.plist").read_bytes()
    )
    gateway = plistlib.loads(
        (ROOT / "launchd/com.weather.clob_order_service.plist").read_bytes()
    )
    assert target["EnvironmentVariables"]["PM_TARGET_PRODUCTION_WRITER"] == "1"
    assert gateway["EnvironmentVariables"]["PM_CLOB_ORDER_SERVICE_READ_ONLY"] == "1"


def test_daily_user_control_has_no_sudo_or_system_mutation() -> None:
    control = _text("targetctl.sh")
    installer = _text("install_weather_basket_yes_user_services.sh")
    assert "sudo" not in control
    assert "sudo" not in installer
    # Daily control remains passwordless and read-only; it may inspect
    # persistent root-owned residue and fail closed, while only the explicit
    # one-time migration is allowed to retire those files.
    assert 'if [ -e "/Library/LaunchDaemons/${label}.plist" ]; then' in control
    assert 'if [ -e "/Library/LaunchDaemons/${label}.plist" ]; then' in installer
    assert "/bin/rm -f \"/Library/LaunchDaemons" not in control
    assert "/bin/rm -f \"/Library/LaunchDaemons" not in installer
    assert 'DOMAIN="gui/${UID_VALUE}"' in control
    assert 'DOMAIN="gui/${UID_VALUE}"' in installer
    assert 'launchctl print "system/${label}"' in control
    assert 'launchctl print "system/${label}"' in installer
    assert "refusing dual-active" in installer


def test_one_time_migration_never_opens_password_prompt() -> None:
    migration = _text("migrate_weather_basket_yes_to_user_domain.sh")
    assert "/usr/bin/sudo -n true" in migration
    assert "/usr/bin/sudo -n /bin/launchctl" in migration
    assert "/usr/bin/sudo -n /bin/rm" in migration
    assert not re.search(r"/usr/bin/sudo(?! -n(?: |$))", migration)
    credential_gate = migration.index("/usr/bin/sudo -n true")
    assert credential_gate < migration.index(
        '/usr/bin/sudo -n /bin/launchctl disable'
    )
    assert credential_gate < migration.index(
        '/usr/bin/sudo -n /bin/launchctl bootout'
    )
    assert "no service state was changed" in migration
    for retired in (
        "com.weather.monitor.daemon",
        "com.weather.model_release_watcher",
    ):
        assert retired in migration


def test_one_time_migration_retires_persistent_system_plists_not_only_loaded_jobs() -> None:
    migration = _text("migrate_weather_basket_yes_to_user_domain.sh")
    assert 'system_retirement_required=0' in migration
    assert 'if [ -e "/Library/LaunchDaemons/${label}.plist" ]; then' in migration
    assert 'for label in "${SYSTEM_RETIRE_LABELS[@]}"; do' in migration
    assert 'echo "system plist was not retired: ${plist}"' in migration


def test_migration_preflight_precedes_system_retirement() -> None:
    migration = _text("migrate_weather_basket_yes_to_user_domain.sh")
    retirement = migration.index(
        '/usr/bin/sudo -n /bin/launchctl disable'
    )
    assert migration.index('/bin/bash -n "$script"') < retirement
    assert migration.index('/usr/bin/plutil -lint "$source_plist"') < retirement
    assert migration.index(
        '/bin/launchctl print "gui/$(id -u)"'
    ) < retirement


def test_all_user_domain_shell_entrypoints_parse() -> None:
    for name in (
        "install_launchagent.sh",
        "install_weather_basket_yes_user_services.sh",
        "migrate_weather_basket_yes_to_user_domain.sh",
        "targetctl.sh",
        "weather_basket_yes_daemon.sh",
        "market_ws_listener.sh",
        "clob_order_service.sh",
    ):
        result = subprocess.run(
            ["/bin/bash", "-n", str(ROOT / "scripts" / name)],
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, f"{name}: {result.stderr}"


def test_status_is_read_only_and_reports_current_domain() -> None:
    before = (ROOT / "data/live_paused.flag").stat().st_mtime_ns
    result = subprocess.run(
        ["/bin/bash", str(ROOT / "scripts/targetctl.sh"), "status"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
        env={**os.environ, "HOME": str(Path.home())},
    )
    assert result.returncode == 0, result.stderr
    assert "production_no_live=true" in result.stdout
    for label in LABELS:
        assert f"{label}=" in result.stdout
    assert (ROOT / "data/live_paused.flag").stat().st_mtime_ns == before


def test_installer_binds_label_before_applying_service_contract() -> None:
    installer = _text("install_launchagent.sh")
    label_assignment = installer.index('label = data.get("Label", "")')
    service_contract = installer.index('if label in {')
    assert label_assignment < service_contract


def test_generic_installer_defaults_to_only_three_production_jobs() -> None:
    installer = _text("install_launchagent.sh")
    default_block = installer.split("DEFAULT_PLISTS=(", 1)[1].split(")", 1)[0]
    for label in LABELS:
        assert f"{label}.plist" in default_block
    for forbidden in (
        "auto_trade_entry_1m",
        "daily_trade_sms_summary",
        "archive_db_governance_offpeak",
        "archive_db_auto_guard",
    ):
        assert forbidden not in default_block
    assert 'SUPPORTED_PLISTS=("${DEFAULT_PLISTS[@]}")' in installer


def test_pure_yes_runtime_cannot_reimport_retired_hard_no_modules() -> None:
    assert not (ROOT / "reported_trajectory_no_runtime.py").exists()
    assert not (ROOT / "reported_trajectory_no_schema.py").exists()
    for relative in (
        "scripts/weather_basket_yes_daemon.py",
        "reported_trajectory_awc_ingress.py",
        "reported_trajectory_market_registry.py",
        "basket_strategy/production.py",
        "basket_strategy/probability_adapter.py",
        "basket_strategy/runtime.py",
        "basket_strategy/snapshot_adapter.py",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "reported_trajectory_no_runtime" not in text
        assert "reported_trajectory_no_schema" not in text
    runtime = (ROOT / "weather_basket_yes_runtime.py").read_text(
        encoding="utf-8"
    )
    assert "select_nearest_impossible_lower_bucket" not in runtime
    assert "unified_capital_plan" not in runtime
