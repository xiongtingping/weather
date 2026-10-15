"""A packet that shipped two families of six has to be visible as that.

2026-09-10.  The strip read "模型在进来 3/4 · 最旧一家 ECCC · 32.0 小时前的 run"
and everything else on the page was green.  What it could not say was that
the 37 packets for 2026-09-12 -- the ones a D+2 decision reads -- carried
ECMWF and GEFS and nothing else.

Family age could not say it, because each family is judged against its own
cadence and a shortfall is absorbed there: ECMWF read 8.6h and healthy the
whole time.  The worker's receipt could not say it either, even though it
had been publishing ``families_held_on_earlier_run`` on every pass since
that field was written -- no consumer had ever read it, and the receipt is
published from inside a poll whose shape varies, so a page rendered on a
rotation-level poll sees no receipt at all and reads as fine.

So the count is taken off the serving inbox: a file, outliving every
process, holding exactly what the compiler is given.  The families expected
are the production registry's (six since charter 212), not a list kept here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402

# Every family the registry seats, in the board's order; the first two are
# ECMWF IFS and AIFS.
ALL = [system_id for system_id, *_rest in C.MEMBER_FAMILIES]
LABELS = [label for _system_id, label, *_rest in C.MEMBER_FAMILIES]


def _packet(event_date: str, systems: list[str], received: str) -> dict:
    return {
        "event_date": event_date,
        "received_at_utc": received,
        "systems": [{"ensemble_system_id": s} for s in systems],
    }


def _inbox(monkeypatch, tmp_path, packets: list[dict]) -> None:
    path = tmp_path / "inbox.json"
    path.write_text(json.dumps({
        "schema": "native_ensemble_forward_inbox_v1", "packets": packets,
    }), encoding="utf-8")
    monkeypatch.setattr(C, "NATIVE_FORWARD_INBOX", path)


def _at(monkeypatch, when: str) -> None:
    from datetime import datetime

    stamp = datetime.fromisoformat(when)
    monkeypatch.setattr(C, "_now", lambda: stamp)


def test_a_two_family_packet_reports_two_of_six(monkeypatch, tmp_path):
    _at(monkeypatch, "2026-09-10T08:00:00+00:00")
    _inbox(monkeypatch, tmp_path, [
        _packet("2026-09-12", ALL[:2], "2026-09-10T08:25:07Z")
        for _ in range(37)
    ])

    out = C._official_native_family_coverage({})
    row = out["native_inbox_coverage"][0]

    assert row["event_date"] == "2026-09-12"
    assert row["packets"] == 37
    assert (row["family_count"], row["expected_family_count"]) == (2, 6)
    assert row["absent"] == LABELS[2:]


def test_taf_rides_the_same_packet_and_is_not_a_family(monkeypatch, tmp_path):
    """Counting it would make a two-family packet read as three."""

    _at(monkeypatch, "2026-09-10T08:00:00+00:00")
    _inbox(monkeypatch, tmp_path, [
        _packet("2026-09-12", [*ALL[:2], "OFFICIAL_AIRPORT_TAF_TX"],
                "2026-09-10T08:25:07Z"),
    ])

    row = C._official_native_family_coverage({})["native_inbox_coverage"][0]
    assert row["family_count"] == 2
    assert "OFFICIAL_AIRPORT_TAF_TX" not in row["families"]


def test_the_far_date_alone_does_not_light_the_strip(monkeypatch, tmp_path):
    """ECCC's 12Z and ICON's own run land in the evening, so a D+2 date
    is short for most of its first day.  A light that is on every morning
    is not a light."""

    _at(monkeypatch, "2026-09-10T08:00:00+00:00")
    _inbox(monkeypatch, tmp_path, [
        _packet("2026-09-10", ALL, "2026-09-10T02:09:22Z"),
        _packet("2026-09-11", ALL, "2026-09-10T08:42:51Z"),
        _packet("2026-09-12", ALL[:2], "2026-09-10T08:25:07Z"),
    ])

    out = C._official_native_family_coverage({})
    assert out["native_inbox_health"] == "green"
    assert out["native_inbox_short_near_dates"] == []
    # Still printed, every date, whatever the colour.
    assert [r["family_count"] for r in out["native_inbox_coverage"]] == [6, 6, 2]


def test_a_near_date_short_of_families_does_light_it(monkeypatch, tmp_path):
    _at(monkeypatch, "2026-09-10T08:00:00+00:00")
    _inbox(monkeypatch, tmp_path, [
        _packet("2026-09-10", ALL[:2], "2026-09-10T02:09:22Z"),
        _packet("2026-09-11", ALL, "2026-09-10T08:42:51Z"),
    ])

    out = C._official_native_family_coverage({})
    assert out["native_inbox_health"] == "amber"
    assert out["native_inbox_short_near_dates"] == ["2026-09-10"]


def test_the_counts_stand_without_the_workers_receipt(monkeypatch, tmp_path):
    """The receipt is published from inside a poll whose shape varies.

    Reading the shortfall out of it would make the page silent on exactly
    the polls that carry no receipt -- and silent reads as fine.
    """

    _at(monkeypatch, "2026-09-10T08:00:00+00:00")
    _inbox(monkeypatch, tmp_path, [
        _packet("2026-09-10", ALL[:2], "2026-09-10T02:09:22Z"),
    ])

    out = C._official_native_family_coverage({"forecast_prefetch": {}})
    assert out["native_inbox_coverage"][0]["family_count"] == 2
    assert out["native_inbox_health"] == "amber"
    assert out["native_inbox_held_families"] == []


def test_the_hold_reason_is_carried_when_the_receipt_has_one(
    monkeypatch, tmp_path,
):
    _at(monkeypatch, "2026-09-10T08:00:00+00:00")
    _inbox(monkeypatch, tmp_path, [
        _packet("2026-09-12", ALL[:2], "2026-09-10T08:25:07Z"),
    ])
    status = {"forecast_prefetch": {"resident_nwp_capture": {
        "official_native_capture": {"last_completed": {
            "event_date": "2026-09-12",
            "families_held_on_earlier_run": {
                "ECCC_DD_WEATHER": "ContractError:eccc_geps_archive_scope_invalid",
                "DWD_OPENDATA_ICON_EPS": "ReadTimeout:x\nFor more information check: https://…",
            },
        }},
    }}}

    out = C._official_native_family_coverage(status)

    assert out["native_inbox_held_event_date"] == "2026-09-12"
    assert out["native_inbox_held_families"] == [
        {"label": "ICON", "reason": "ReadTimeout:x"},
        {"label": "ECCC",
         "reason": "ContractError:eccc_geps_archive_scope_invalid"},
    ]


def test_an_unreadable_inbox_says_nothing_rather_than_zero(
    monkeypatch, tmp_path,
):
    """No packets is a different fact from packets with no families, and
    a card that renders 0/4 off a missing file is the second one."""

    _at(monkeypatch, "2026-09-10T08:00:00+00:00")
    monkeypatch.setattr(C, "NATIVE_FORWARD_INBOX", tmp_path / "absent.json")
    assert C._official_native_family_coverage({}) == {}

    empty = tmp_path / "empty.json"
    empty.write_text('{"packets": []}', encoding="utf-8")
    monkeypatch.setattr(C, "NATIVE_FORWARD_INBOX", empty)
    assert C._official_native_family_coverage({}) == {}


def _activation_db(certified: list[str]):
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_native_ensemble_activation (artifact_kind TEXT,"
        " ensemble_system_id_or_star TEXT, authority_scope TEXT,"
        " valid_from_utc TEXT, valid_until_utc TEXT)"
    )
    conn.executemany(
        "INSERT INTO calibration_native_ensemble_activation VALUES"
        " ('NATIVE_ENSEMBLE_SYSTEM_CALIBRATION', ?, 'PRODUCTION_NO_LIVE_ONLY',"
        " '2026-10-01T00:00:00Z', '9999-12-31T23:59:59Z')",
        [(s,) for s in certified],
    )
    return conn


def test_a_family_with_no_certificate_yet_is_warming_up_not_missing(monkeypatch, tmp_path):
    # Charter 270: the operational AIGEFS rides no packet until it earns its
    # own certificates; a near date short of it alone stays green.
    _at(monkeypatch, "2026-10-15T08:00:00+00:00")
    aigefs = "NOAA_AIGEFS_OPS_025"
    others = [s for s in ALL if s != aigefs]
    _inbox(monkeypatch, tmp_path, [_packet("2026-10-15", others, "2026-10-15T07:00:00Z")])

    out = C._official_native_family_coverage({}, _activation_db(others))
    row = out["native_inbox_coverage"][0]
    assert row["absent"] == [] and row["certifying"] == ["AIGEFS"]
    assert out["native_inbox_health"] == "green"

    # ... and once it holds a certificate, being absent is short again.
    out = C._official_native_family_coverage({}, _activation_db(ALL))
    assert out["native_inbox_coverage"][0]["absent"] == ["AIGEFS"]
    assert out["native_inbox_health"] == "amber"
