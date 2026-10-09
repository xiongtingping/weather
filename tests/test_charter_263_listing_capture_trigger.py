"""Charter 263: a listed station-day is captured on listing, not when the
rotation cursor reaches it.

Charter 195 (三) runs a listed scope's pass once, past the held run and the
retry floor, but only from the rotation step that visits the scope -- 393
scopes, 66 a poll, a poll every ten minutes.  ``run_listing_triggered_captures``
runs on every forecast refresh and submits, through the rotation's own entry,
every (station, day) the forward registry lists whose local day has not closed
and whose official packet is missing: once per event date per call.
"""
from __future__ import annotations

import pytest

import bayes_basket_target.production_worker as pw

AS_OF = "2026-10-09T05:01:30Z"  # the 05:00Z listing batch has just landed
DAY = "2026-10-11"


def _worker(tmp_path):
    worker = object.__new__(pw.TargetProductionWorker)
    worker.nwp_runtime_root = tmp_path
    worker.station_allowlist = ()
    (tmp_path / "market_registry_forward.json").write_bytes(b"{}")
    (tmp_path / "official_native_ensemble_forward_inbox.json").write_bytes(b"{}")
    return worker


def _row(station, day, tz):
    return {"station_id": station, "event_date": day, "settlement_timezone": tz}


def _packet(station, day):
    return {
        "schema": pw.OFFICIAL_NATIVE_FORWARD_PACKET_SCHEMA,
        "station_id": station,
        "event_date": day,
    }


def _wire(monkeypatch, worker, *, rows, packets, forward=True):
    registry = {
        "filters": {"include_not_started_for_p0": forward},
        "rows": rows,
    }
    monkeypatch.setattr(pw, "load_market_registry_snapshot", lambda path: registry)
    monkeypatch.setattr(
        pw, "_load_native_forward_inbox", lambda path, error_code: packets
    )
    calls = []

    def fake_capture(**kwargs):
        calls.append(kwargs)
        return {"state": "CAPTURED"}

    monkeypatch.setattr(worker, "_capture_official_native_packet", fake_capture)
    return calls


def test_a_listed_day_without_a_packet_is_captured_once_per_date(
    monkeypatch, tmp_path
) -> None:
    worker = _worker(tmp_path)
    calls = _wire(
        monkeypatch,
        worker,
        rows=[
            _row("EFHK", DAY, "Europe/Helsinki"),
            _row("EDDM", DAY, "Europe/Berlin"),
            _row("EDDM", DAY, "Europe/Berlin"),  # one row per bucket
            _row("ZBAA", DAY, "Asia/Shanghai"),  # already holds a packet
            _row("ZBAA", "2026-10-10", "Asia/Shanghai"),  # held too
        ],
        packets=[_packet("ZBAA", DAY), _packet("ZBAA", "2026-10-10")],
    )
    receipt = worker.run_listing_triggered_captures(as_of_utc=AS_OF)
    assert receipt["state"] == "RAN"
    # One pass for the date, through the rotation's entry with the listing
    # station set; the pass covers every station of the date.
    assert len(calls) == 1
    call = calls[0]
    assert call["event_date"] == DAY
    assert call["station_id"] == call["listing_station"] == "EDDM"
    assert call["lead_days"] == 2
    assert call["inbox_path"] == tmp_path / "official_native_ensemble_forward_inbox.json"
    assert receipt["outcomes"] == [{
        "event_date": DAY,
        "station_id": "EDDM",
        "stations_without_packet": 2,
        "state": "CAPTURED",
    }]
    assert receipt["places_orders"] is False
    assert receipt["calls_order_gateway"] is False


def test_a_station_the_pass_already_attempted_yields_to_the_next(
    monkeypatch, tmp_path
) -> None:
    worker = _worker(tmp_path)
    # Charter 195 (三)'s own record, written by the capture entry.
    worker._official_native_listing_attempts = {(DAY, "EDDM")}
    calls = _wire(
        monkeypatch,
        worker,
        rows=[_row("EDDM", DAY, "Europe/Berlin"), _row("EFHK", DAY, "Europe/Helsinki")],
        packets=[],
    )
    worker.run_listing_triggered_captures(as_of_utc=AS_OF)
    assert [c["listing_station"] for c in calls] == ["EFHK"]


def test_closed_days_held_stations_and_the_allowlist_are_not_candidates(
    monkeypatch, tmp_path
) -> None:
    worker = _worker(tmp_path)
    worker.station_allowlist = ("EDDM", "RJTT")
    calls = _wire(
        monkeypatch,
        worker,
        rows=[
            _row("EDDM", "2026-10-07", "Europe/Berlin"),  # closed everywhere
            _row("EDDM", "2026-10-09", "Europe/Berlin"),  # open, packet held
            _row("RJTT", "2026-10-10", "Asia/Tokyo"),  # outside: no packet
            _row("EFHK", DAY, "Europe/Helsinki"),  # not on the allowlist
        ],
        packets=[_packet("EDDM", "2026-10-09")],
    )
    receipt = worker.run_listing_triggered_captures(as_of_utc=AS_OF)
    assert [(c["event_date"], c["listing_station"], c["lead_days"]) for c in calls] == [
        ("2026-10-10", "RJTT", 1),
    ]
    assert receipt["outcomes"][0]["stations_without_packet"] == 1


def test_two_dates_are_two_passes(monkeypatch, tmp_path) -> None:
    worker = _worker(tmp_path)
    calls = _wire(
        monkeypatch,
        worker,
        rows=[_row("KLAX", "2026-10-10", "America/Los_Angeles"), _row("KLAX", DAY, "America/Los_Angeles")],
        packets=[],
    )
    worker.run_listing_triggered_captures(as_of_utc=AS_OF)
    assert sorted(c["event_date"] for c in calls) == ["2026-10-10", DAY]


def test_a_registry_without_the_forward_boundary_triggers_nothing(
    monkeypatch, tmp_path
) -> None:
    worker = _worker(tmp_path)
    calls = _wire(
        monkeypatch, worker, rows=[_row("EDDM", DAY, "Europe/Berlin")], packets=[], forward=False
    )
    assert worker.run_listing_triggered_captures(as_of_utc=AS_OF)["state"] == "REGISTRY_NOT_FORWARD"
    assert calls == []


def test_the_switch_leaves_the_rotation_as_the_only_trigger(monkeypatch, tmp_path) -> None:
    worker = _worker(tmp_path)
    calls = _wire(monkeypatch, worker, rows=[_row("EDDM", DAY, "Europe/Berlin")], packets=[])
    monkeypatch.setattr(pw, "LISTING_TRIGGER_ENABLED", False)
    assert worker.run_listing_triggered_captures(as_of_utc=AS_OF)["state"] == "DISABLED"
    assert calls == []


def test_a_missing_registry_is_not_an_error(tmp_path) -> None:
    worker = _worker(tmp_path)
    (tmp_path / "market_registry_forward.json").unlink()
    assert worker.run_listing_triggered_captures(as_of_utc=AS_OF)["state"] == "NO_REGISTRY"
