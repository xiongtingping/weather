from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MIGRATION = "migrate_weather_basket_yes_to_user_domain.sh"
TARGET_MARKERS = (
    "com.weather.weather_basket_yes",
    "com.weather.reported_trajectory_no",
    "com.weather.market_ws",
    "com.weather.clob_order_service",
    "TARGET_LABEL",
)


def test_only_one_time_migrator_may_mutate_target_system_jobs() -> None:
    violations: list[str] = []
    mutation = re.compile(
        r"launchctl[^\n]{0,80}"
        r"(?:bootout|bootstrap|kickstart|enable|disable)[^\n]{0,160}"
        r"(?:system/(?:com\.weather\.(?:weather_basket_yes|reported_trajectory_no|market_ws|"
        r"clob_order_service)|\$\{?TARGET_LABEL\}?)"
        r"|\$(?:SERVICE|TARGET_SERVICE)(?![A-Z_]))"
    )
    system_binding = re.compile(
        r"(?<![A-Z_])(?:SERVICE|TARGET_SERVICE|SERVICE_DOMAIN)"
        r"\s*=\s*[\"']system"
        r"|(?:TARGET_PLIST|INSTALLED_PLIST)\s*=\s*[\"']"
        r"/Library/LaunchDaemons"
    )
    for path in sorted(SCRIPTS.iterdir()):
        if not path.is_file() or path.name == MIGRATION:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not any(marker in text for marker in TARGET_MARKERS):
            continue
        if mutation.search(text) or system_binding.search(text):
            violations.append(path.name)
    assert violations == []


def test_one_time_migrator_is_noninteractive_and_narrow() -> None:
    text = (SCRIPTS / MIGRATION).read_text(encoding="utf-8")
    assert "/usr/bin/sudo -n true" in text
    assert "/usr/bin/sudo -n /bin/launchctl" in text
    assert "sudo -v" not in text
    assert re.search(r"/usr/bin/sudo(?! -n(?: |$))", text) is None
    assert "com.weather.reported_trajectory_no" in text
    assert "com.weather.weather_basket_yes" in text
    assert "com.weather.market_ws" in text
    assert "com.weather.clob_order_service" in text


def test_current_target_entrypoints_never_remove_live_pause() -> None:
    for relative in (
        "targetctl.sh",
        "install_weather_basket_yes_user_services.sh",
        "cutover_bayes_basket_target_probability_evidence_no_live.sh",
        "cutover_delta_basket_score_v2_no_live.sh",
        "cutover_bayes_basket_target_prepared_supersession_no_live.sh",
    ):
        text = (SCRIPTS / relative).read_text(encoding="utf-8")
        assert 'rm -f "$PAUSE"' not in text
        assert 'rm -f "$LIVE_PAUSE"' not in text
