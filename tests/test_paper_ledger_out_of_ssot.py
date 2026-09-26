"""The paper ledger stays out of the resident's way.

2026-09-19 (「移出」): the old ledger's tables sat in the SSOT, so its pass --
almost all reading -- held the SSOT writer for its whole lease: 62 production
cycles waited on it, 4.25 hours of the 28.9 before the move.

2026-09-26 (「纸面账本不要阻断生产。失败即跳过。」): its successor
(``scripts/paper_ledger_aligned.py``) opens the SSOT read-only and writes only
``data/paper_ledger_aligned.db``, and the resident no longer schedules it at
all -- as a sweep it held one of the four maintenance slots for the hour or
more a day takes, and every restart killed it.  It is its own LaunchAgent.
"""
from __future__ import annotations

import importlib.util
import plistlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_the_resident_does_not_schedule_the_ledger() -> None:
    daemon = _load("_daemon_paper_domain", ROOT / "scripts" / "weather_basket_yes_daemon.py")
    hosted = {name for name, _script, _interval, _timeout in daemon.HOSTED_SWEEPS}
    scripts = {script for _name, script, _interval, _timeout in daemon.HOSTED_SWEEPS}
    assert "paper_ledger_aligned" not in hosted
    assert "scripts/paper_ledger_aligned_sweep.sh" not in scripts
    assert "paper_ledger_aligned" not in daemon.HOSTED_SWEEP_WRITE_DOMAIN
    assert "paper_ledger_aligned" not in daemon.HOSTED_SWEEP_LEASE_CONTRACT


def test_the_ledger_is_its_own_launch_agent() -> None:
    path = ROOT / "launchd" / "com.weather.paper_ledger_aligned.plist"
    with path.open("rb") as handle:
        agent = plistlib.load(handle)
    assert agent["ProgramArguments"][-1].endswith("scripts/paper_ledger_aligned_sweep.sh")
    assert agent["StartInterval"] == 3600
    installer = (ROOT / "scripts" / "install_launchagent.sh").read_text(encoding="utf-8")
    offered = installer.split("OPTIONAL_PLISTS=(", 1)[1].split("\n)\n", 1)[0]
    assert "com.weather.paper_ledger_aligned.plist" in offered
