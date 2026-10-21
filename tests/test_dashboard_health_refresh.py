"""系统健康快刷 and the copy-red button (user 2026-10-21 「系统健康的渲染提高优先级」
「报红可以一键复制」)."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import calibration_dashboard as C  # noqa: E402


def test_the_health_refresh_reads_only_cheap_every_run_sections() -> None:
    assert set(C.HEALTH_REFRESH_SECTIONS) <= set(C.RENDER_EVERY_RUN_SECTIONS)
    assert "regional" not in C.HEALTH_REFRESH_SECTIONS  # 216 s: stays on the half-hour run


def test_the_health_agent_is_installable_and_runs_every_five_minutes() -> None:
    plist = (ROOT / "launchd" / "com.weather.dashboard_health.plist").read_text()
    assert "<integer>300</integer>" in plist and "dashboard_health_sweep.sh" in plist
    assert "com.weather.dashboard_health.plist" in (ROOT / "scripts" / "install_launchagent.sh").read_text()
    sweep = (ROOT / "scripts" / "dashboard_health_sweep.sh").read_text()
    assert "--refresh-health" in sweep and "ssot_wal_under 0" in sweep and "--html-out" in sweep


def test_every_red_alert_goes_to_the_copy_button() -> None:
    src = C.HTML_TEMPLATE
    assert 'const reds = (D.alerts || []).filter(a => a.health === "red");' in src
    assert 'class="copy-red"' in src and "navigator.clipboard" in src
