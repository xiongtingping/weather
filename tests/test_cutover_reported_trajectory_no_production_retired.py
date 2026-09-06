from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "cutover_reported_trajectory_no_production.py"
TARGETCTL = ROOT / "scripts" / "targetctl.sh"


def test_legacy_entrypoint_is_an_explicit_side_effect_free_refusal() -> None:
    source = SCRIPT.read_text(encoding="utf-8").lower()

    assert "targetctl.sh" in source
    assert "production_no_live" in source
    for forbidden in (
        "sudo",
        "launchctl",
        "gateway",
        "system/",
        "bootstrap",
        "bootout",
        "kickstart",
        "subprocess",
        "sqlite",
        "unlink",
        "write_text",
    ):
        assert forbidden not in source

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--confirm-live-reported-trajectory-no-v1"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 78
    assert "retired" in result.stderr
    assert "targetctl.sh" in result.stderr


def test_targetctl_is_the_single_user_domain_three_service_switch() -> None:
    source = TARGETCTL.read_text(encoding="utf-8")

    assert 'DOMAIN="gui/${UID_VALUE}"' in source
    assert 'PAUSE="$ROOT/data/live_paused.flag"' in source
    # The switch is the operator's in both directions (user ruling
    # 2026-09-06, "取消暂停旗标,我自己开关" / "拆").  This used to assert the
    # opposite for the set: ``stop_user_services`` armed the flag before
    # stopping anything and nothing ever lowered it, so a completed restart
    # left submission shut and a failed one left it shut with the services
    # down.  The clear was already forbidden here and stays forbidden; the
    # set now joins it.  What replaces both is saying the state out loud.
    assert 'touch "$PAUSE"' not in source
    assert 'rm -f "$PAUSE"' not in source
    # Saying the state replaces setting it: stop and status each report it,
    # so a restart names the flag on the way in and the installer names it
    # again on the way out.
    assert source.count("live_pause_present=%s") >= 2

    assert 'production_no_live=true' in source
    for label in (
        "com.weather.reported_trajectory_no",
        "com.weather.market_ws",
        "com.weather.clob_order_service",
    ):
        assert label in source
    assert "assert_system_absent" in source
