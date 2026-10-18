"""Charter 267 补记九: the round-75 centre chain recorded beside F.

The shrinkage follows the moments (no station spread, no station term), the
weights shrink to equal without history, the record window admits only event
days whose listing instant has just passed, and the ledger is append-only:
a station-day already recorded is never written again.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from bayes_basket_target import listing_forecast_f_mos as M

SYS = M.SYSTEMS[0]


def _row(station, day, truth, x):
    d = date(2026, 10, day)
    stamp = datetime(2026, 10, day, tzinfo=timezone.utc)
    return {"station": station, "day": d, "truth": truth, "xst": {SYS: x},
            "settled": stamp + timedelta(days=1), "listing": stamp - timedelta(days=2)}


def test_identical_station_biases_shrink_fully_to_the_fleet():
    rows = [_row(s, d, 21.0, 20.0) for s in ("AAAA", "BBBB") for d in range(1, 9)]
    table = M.bias_table(rows, date(2026, 10, 12))
    b_fleet, ks = table[SYS]
    assert b_fleet == pytest.approx(1.0)
    assert all(k == 0.0 for _b, k in ks.values())
    assert M.l2_members({SYS: 22.0}, "AAAA", table)[SYS] == pytest.approx(23.0)


def test_station_with_its_own_bias_keeps_part_of_it():
    rows = [_row("AAAA", d, 20.0 + (0.3 if d % 2 else -0.3), 18.0) for d in range(1, 11)]
    rows += [_row("BBBB", d, 20.0 + (0.3 if d % 2 else -0.3), 20.0) for d in range(1, 11)]
    _b, ks = M.bias_table(rows, date(2026, 10, 12))[SYS]
    assert 0.0 < ks["AAAA"][1] < 1.0


def test_weights_are_equal_without_history_and_shrink_toward_equal():
    members = {"A": 20.0, "B": 22.0}
    assert M.l4(members, [], date(2026, 10, 12)) == pytest.approx(21.0)
    own = [(date(2026, 10, d), 20.0, {"A": 20.0, "B": 22.0}) for d in range(1, 11)]
    value = M.l4(members, own, date(2026, 10, 12))
    assert 20.0 < value < 21.0


def test_record_window_is_only_just_after_the_listing_instant():
    now = datetime(2026, 10, 18, 15, 0, tzinfo=timezone.utc)
    # 10-20 listed 10-18 05:00Z (10 h ago); 10-21 lists 10-19 05:00Z (not yet).
    assert M.due_event_days(now) == [date(2026, 10, 20)]
    assert M.due_event_days(datetime(2026, 10, 19, 5, 30, tzinfo=timezone.utc)) == [date(2026, 10, 21)]


def test_ledger_is_append_only_per_station_day(tmp_path, monkeypatch):
    path = tmp_path / "ledger.jsonl"
    path.write_text(json.dumps({"station_id": "AAAA", "event_date": "2026-10-20"}) + "\n")
    monkeypatch.setattr(M, "global_roster", lambda conn: [("AAAA", 0.0, 0.0, "UTC")])
    receipt = M.record(None, now=datetime(2026, 10, 18, 15, 0, tzinfo=timezone.utc), path=path, f_payload={})
    assert receipt["written"] == 0
    assert path.read_text().count("\n") == 1
