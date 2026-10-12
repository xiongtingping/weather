"""A cycle whose every scope was a rule saying "not now" is not an outage.

2026-10-12 16:01Z the page's banner carried 「生产回执 fail-closed：
blocked_fail_closed·all_scopes_refused:p1_entry_window_not_openx4」 over four
tomorrow scopes waiting for their P1 entry window (user 「有问题吧」).  The
worker writes such a cycle ``blocked_fail_closed`` because it committed
nothing; the board's fail-closed branch returned red before its own
expected-reason set (2026-09-25, the partial-cycle case) was read.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402

NOW = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _health(reason: str, reasons: dict[str, int]) -> dict:
    return C._production_health({"production": {
        "ok": False, "state": "blocked_fail_closed", "reason": reason,
        "committed_scope_count": 0, "blocked_scope_count": sum(reasons.values()),
        "blocked_scope_reasons": reasons, "worker_checked_at": NOW,
        "stage_timings_ms": {"cycle_total": 4000.0},
    }})


def test_every_scope_waiting_for_its_window_is_green_and_says_so() -> None:
    out = _health("all_scopes_refused:p1_entry_window_not_openx4", {"p1_entry_window_not_open": 4})
    assert out["production_health"] == "green"
    assert out["production_note"] == "本轮无落账：4 个 scope 都在等（P1 入场窗未开×4）"
    assert out["production_blocked_lanes"] == []


def test_a_refusal_outside_the_expected_set_still_reads_red() -> None:
    out = _health("all_scopes_refused:production_quote_expiredx3,p1_entry_window_not_openx1",
                  {"production_quote_expired": 3, "p1_entry_window_not_open": 1})
    assert out["production_health"] == "red"
    assert out["production_note"].startswith("生产回执 fail-closed：")


def test_a_fleet_wide_defect_or_a_worker_failure_still_reads_red() -> None:
    defect = _health("fleet_wide_scope_defect:KeyError", {"p1_entry_window_not_open": 4})
    assert defect["production_health"] == "red"
    worker = C._production_health({"production": {
        "ok": False, "state": "blocked_fail_closed",
        "reason": "target_ssot_read_only_open_failed:target_database_wal_checkpoint_busy",
        "stage_timings_ms": {"cycle_total": 0.1},
    }})
    assert worker["production_health"] == "red"
