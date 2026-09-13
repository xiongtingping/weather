"""P0 freshness runs on the evidence the blend is built on -- and the capture
probe reads the same clock, so a packet the compiler refuses asks to be re-read.

Measured 2026-09-06..12 over 360 station-days: the packet clock (newest receipt
of any archive in the snapshot) kept P0 servable 85.1% of forward-day time; the
seated clock alone would have kept 79.7%, and 94% of the forward-day time lost
was time in which the seated systems' archives were re-read later that day --
the re-read was possible, nothing asked for it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import bayes_basket_target.production_worker as production_worker_module
from bayes_basket_target.contracts import ProbabilityEnvelope, content_sha256
from bayes_basket_target.materialization import insert_probability_envelope
from bayes_basket_target.production_worker import (
    TargetProductionWorker,
    _native_forward_packet_is_stale,
    _official_inbox_packets_all_stale,
)
from bayes_basket_target.ssot import apply_production_ssot_schema


def _packet(station: str, *, queries: dict[str, str]) -> dict:
    return {
        "schema": "official_native_forward_packet_test",
        "station_id": station,
        "event_date": "2026-09-14",
        "received_at_utc": max(queries.values()),
        "archive_query_at_utc_by_system": queries,
    }


ICON_GEM_OLD = {
    "DWD_ICON_GLOBAL_EPS": "2026-09-13T01:00:00Z",
    "ECCC_GEM_GLOBAL_ENS": "2026-09-13T01:00:00Z",
    "ECMWF_IFS_ENS_OPEN_PF50": "2026-09-13T06:30:00Z",
    "NOAA_GEFS_05": "2026-09-13T06:30:00Z",
}


def test_the_probe_reads_the_seated_clock_when_it_knows_the_seat_set() -> None:
    packet = _packet("KSFO", queries=ICON_GEM_OLD)
    as_of = "2026-09-13T07:30:00Z"
    # The packet clock says an hour old; the blend's own evidence is 6.5h old.
    assert _native_forward_packet_is_stale(packet, as_of_utc=as_of) is False
    assert _native_forward_packet_is_stale(
        packet,
        as_of_utc=as_of,
        seated_system_ids=("DWD_ICON_GLOBAL_EPS", "ECCC_GEM_GLOBAL_ENS"),
    ) is True
    # A station blending the fresh pair from the same packet is fine.
    assert _native_forward_packet_is_stale(
        packet,
        as_of_utc=as_of,
        seated_system_ids=("ECMWF_IFS_ENS_OPEN_PF50", "NOAA_GEFS_05"),
    ) is False
    # A seated system this packet does not carry: the compiler cannot seat
    # that blend from it, so the packet clock is all there is.
    assert _native_forward_packet_is_stale(
        packet,
        as_of_utc=as_of,
        seated_system_ids=("DWD_ICON_GLOBAL_EPS", "ECMWF_AIFS_SINGLE"),
    ) is False


def test_the_probe_threshold_is_the_compilers_usable_window() -> None:
    """Stale once less than a mintable window is left, as the compile says."""

    packet = _packet("KSFO", queries={"NOAA_GEFS_05": "2026-09-13T01:00:00Z"})
    # Six hours less 59 seconds: under a minute left -> the compile refuses.
    assert _native_forward_packet_is_stale(
        packet, as_of_utc="2026-09-13T06:59:01Z"
    ) is True
    assert _native_forward_packet_is_stale(
        packet, as_of_utc="2026-09-13T06:58:59Z"
    ) is False


def test_the_day_is_re_read_for_the_station_whose_blend_aged(
    tmp_path: Path,
) -> None:
    """Every packet of a day aged together under the packet clock; not now."""

    inbox = tmp_path / "official_native_ensemble_forward_inbox.json"
    inbox.write_text(json.dumps({"packets": [
        _packet("KSFO", queries=ICON_GEM_OLD),
        _packet("KLAX", queries=ICON_GEM_OLD),
    ]}), encoding="utf-8")
    as_of = "2026-09-13T07:30:00Z"
    assert _official_inbox_packets_all_stale(
        inbox, event_date="2026-09-14", as_of_utc=as_of
    ) is False
    assert _official_inbox_packets_all_stale(
        inbox,
        event_date="2026-09-14",
        as_of_utc=as_of,
        station_id="KSFO",
        seated_system_ids=("DWD_ICON_GLOBAL_EPS", "ECCC_GEM_GLOBAL_ENS"),
    ) is True
    assert _official_inbox_packets_all_stale(
        inbox,
        event_date="2026-09-14",
        as_of_utc=as_of,
        station_id="KLAX",
        seated_system_ids=("ECMWF_IFS_ENS_OPEN_PF50", "NOAA_GEFS_05"),
    ) is False
    # The packet-clock reading is unchanged: every packet past it still is.
    assert _official_inbox_packets_all_stale(
        inbox, event_date="2026-09-14", as_of_utc="2026-09-13T12:30:00Z"
    ) is True


def _p0(
    station: str, checkpoint: str, decision: str, weights: dict[str, float]
) -> ProbabilityEnvelope:
    return ProbabilityEnvelope.from_mapping({
        "stage": "P0",
        "checkpoint_id": checkpoint,
        "station_id": station,
        "event_date": "2026-09-14",
        "settlement_timezone": "America/Los_Angeles",
        "decision_at_utc": decision,
        "fresh_until_utc": "2026-09-14T00:00:00Z",
        "bucket_ordinals": {"a": 0, "b": 1},
        "probabilities": {"a": 0.5, "b": 0.5},
        "posterior_version": "p0-test",
        "posterior_sha256": content_sha256([checkpoint]),
        "bridge_sha256": "b" * 64,
        "lineage": {
            "settlement_timezone": "America/Los_Angeles",
            "system_weights_json": json.dumps(weights),
        },
    })


def test_the_seat_set_is_what_the_newest_p0_weighted() -> None:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    for envelope in (
        _p0("KSFO", "p0-ksfo-old", "2026-09-13T01:00:00.000Z",
            {"ECMWF_IFS_ENS_OPEN_PF50": 0.5, "NOAA_GEFS_05": 0.5}),
        _p0("KSFO", "p0-ksfo-new", "2026-09-13T05:00:00.000Z",
            {"DWD_ICON_GLOBAL_EPS": 0.48, "ECCC_GEM_GLOBAL_ENS": 0.52,
             "NOAA_GEFS_05": 0.0}),
        _p0("KLAX", "p0-klax", "2026-09-13T04:00:00.000Z",
            {"ECMWF_IFS_ENS_OPEN_PF50": 0.4, "NOAA_GEFS_05": 0.6}),
        _p0("KSEA", "p0-ksea-future", "2026-09-13T09:00:00.000Z",
            {"NOAA_GEFS_05": 1.0}),
    ):
        insert_probability_envelope(
            conn, envelope, created_at_utc=envelope.decision_at_utc
        )
    worker = object.__new__(TargetProductionWorker)
    seats = worker._latest_p0_weighted_systems(
        conn, event_date="2026-09-14", as_of_utc="2026-09-13T07:30:00Z"
    )
    # Newest per station, zero-weight riders out, nothing from the future.
    assert seats == {
        "KSFO": ("DWD_ICON_GLOBAL_EPS", "ECCC_GEM_GLOBAL_ENS"),
        "KLAX": ("ECMWF_IFS_ENS_OPEN_PF50", "NOAA_GEFS_05"),
    }
    # One read a day and a cycle.
    conn.close()
    assert worker._latest_p0_weighted_systems(
        None, event_date="2026-09-14", as_of_utc="2026-09-13T07:30:00Z"
    ) == seats
    # A new cycle clock asks again (and an unreadable store answers nothing).
    assert worker._latest_p0_weighted_systems(
        None, event_date="2026-09-14", as_of_utc="2026-09-13T07:35:00Z"
    ) == {}


def test_an_expired_packet_in_the_p0_inbox_does_not_block_the_rest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expired between compile and write is not malformed.

    It used to raise ``resident_p0_stale_forbidden``, and one raise failed the
    whole bounded inbox -- every station's P0 that cycle.  The seated clock
    closes windows earlier than the packet clock did, so this is no longer a
    once-in-a-while race.
    """

    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    read_db = tmp_path / "target-read.db"
    with sqlite3.connect(read_db) as read:
        read.execute("CREATE TABLE marker(value TEXT)")
    worker = TargetProductionWorker(read_db, commit_conn=conn)
    worker.p0_inbox_path = tmp_path / "p0-inbox.json"

    def _envelope(station: str, fresh_until: str) -> ProbabilityEnvelope:
        return ProbabilityEnvelope.from_mapping({
            "stage": "P0",
            "checkpoint_id": f"p0-{station}",
            "station_id": station,
            "event_date": "2099-09-14",
            "settlement_timezone": "America/Los_Angeles",
            "decision_at_utc": "2099-09-13T05:00:00.000Z",
            "fresh_until_utc": fresh_until,
            "bucket_ordinals": {"a": 0, "b": 1},
            "probabilities": {"a": 0.5, "b": 0.5},
            "posterior_version": "p0-test",
            "posterior_sha256": content_sha256([station]),
            "bridge_sha256": "b" * 64,
            "lineage": {
                "settlement_timezone": "America/Los_Angeles",
                "native_transport_provider": (
                    "OFFICIAL_NATIVE_MIXED_CYCLE_SNAPSHOT"
                ),
                "settlement_definition": "station_local_calendar_day_tmax",
                "bucket_intervals_c_json": json.dumps(
                    {"a": [None, 20.5], "b": [20.5, None]},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        })

    envelopes = {
        "KSFO": _envelope("KSFO", "2099-09-13T07:00:00.000Z"),
        "KLAX": _envelope("KLAX", "2099-09-13T11:00:00.000Z"),
    }
    packets = [
        {"stage": "P0", "station_id": station,
         "probability_pathway": production_worker_module.P0_PROBABILITY_PATHWAY}
        for station in envelopes
    ]
    worker.p0_inbox_path.write_text(json.dumps({
        "schema": "bayes_basket_target_p0_inbox_v1",
        "packets": [
            {
                "source_class": "calibrated_native_cross_ensemble",
                "packet": packet,
                "packet_content_sha256": content_sha256(packet),
            }
            for packet in packets
        ],
    }), encoding="utf-8")
    monkeypatch.setattr(
        production_worker_module,
        "build_probability_envelope",
        lambda packet: envelopes[packet["station_id"]],
    )
    try:
        result = worker.materialize_resident_p0(
            as_of_utc="2099-09-13T08:00:00.000Z"
        )
        assert result["blockers"] == []
        assert result["state"] == "COMMITTED"
        states = {
            item["station_id"]: item["state"] for item in result["receipts"]
        }
        assert states == {
            "KSFO": "IGNORED_EXPIRED_IN_INBOX",
            "KLAX": "COMMITTED",
        }
        assert [
            row[0] for row in conn.execute(
                "SELECT station_id FROM basket_probability_envelope"
            )
        ] == ["KLAX"]
    finally:
        worker.close()
