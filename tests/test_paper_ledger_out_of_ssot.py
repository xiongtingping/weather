"""The paper ledger stays out of the SSOT writer's way (2026-09-19, 「移出」).

The old ledger's tables sat in the SSOT, so its pass -- almost all reading --
held the SSOT writer for its whole lease: 62 production cycles waited on it,
4.25 hours of the 28.9 before the move.  Its successor (2026-09-25,
``scripts/paper_ledger_aligned.py``) opens the SSOT read-only and writes only
``data/paper_ledger_aligned.db``; this pins that the resident schedules it
without an SSOT writer turn.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_the_resident_no_longer_waits_on_the_ledger() -> None:
    daemon = _load("_daemon_paper_domain", ROOT / "scripts" / "weather_basket_yes_daemon.py")
    assert daemon.hosted_sweep_write_domain("paper_ledger_aligned") == "paper_ledger"
    assert "paper_ledger" not in daemon._EXCLUSIVE_WRITE_DOMAINS
    assert daemon.writer_excluding_sweeps(["paper_ledger_aligned"]) == []
    assert "paper_ledger_aligned" not in daemon.HOSTED_SWEEP_LEASE_CONTRACT
    # It starts in the same slot as an SSOT sweep, and neither holds the other.
    due = [
        (5.0, 0, "settlement_truth", "a.sh", 1800.0),
        (4.0, 1, "paper_ledger_aligned", "b.sh", 1800.0),
    ]
    admitted = daemon.admissible_hosted_sweeps(due, running_names=[])
    assert [item[2] for item in admitted] == ["settlement_truth", "paper_ledger_aligned"]
    assert daemon.admissible_hosted_sweeps(
        [(9.0, 0, "paper_ledger_aligned", "b.sh", 1800.0)], running_names=["settlement_truth"],
    ) != []
