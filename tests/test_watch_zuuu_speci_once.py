from datetime import datetime, timezone
from pathlib import Path

from scripts.watch_zuuu_speci_once import build_poll_state, build_state, new_speci_sources


ROOT = Path(__file__).resolve().parents[1]


def test_speci_fetch_failure_uses_production_probability_ingress_flag() -> None:
    source = (ROOT / "scripts" / "watch_zuuu_speci_once.py").read_text(encoding="utf-8")

    assert "bool(args.production_probability_ingress)" in source
    assert "args.realtime_ingress" not in source


def _source(report_type: str, raw: str, valid_time: str) -> dict:
    return {
        "report_type": report_type,
        "raw_text": raw,
        "valid_time_utc": valid_time,
        "valid_time_local": "2026-07-14T10:00:00+08:00",
        "temperature_c": 31.0,
    }


def test_watch_selects_only_unseen_speci_without_touching_metar_or_taf() -> None:
    speci = _source("SPECI", "SPECI ZUUU 140200Z 18003MPS 9999 31/24 Q1002=", "2026-07-14T02:00:00Z")
    selected = new_speci_sources(
        [
            _source("METAR", "METAR ZUUU 140200Z 18003MPS 9999 31/24 Q1002=", "2026-07-14T02:00:00Z"),
            _source("TAF", "TAF ZUUU 140000Z 1400/1500 18003MPS=", "2026-07-14T00:00:00Z"),
            speci,
        ],
        seen_raw_sha256=set(),
    )

    assert len(selected) == 1
    assert selected[0]["report_type"] == "SPECI"
    assert selected[0]["raw_sha256"]
    assert new_speci_sources(selected, seen_raw_sha256={selected[0]["raw_sha256"]}) == []


def test_watch_rejects_reports_outside_the_live_window() -> None:
    selected = new_speci_sources(
        [_source("SPECI", "SPECI ZUUU 140200Z 18003MPS 9999 31/24 Q1002=", "2026-07-14T02:00:00Z")],
        seen_raw_sha256=set(),
        not_before=datetime(2026, 7, 14, 2, 15, tzinfo=timezone.utc),
    )

    assert selected == []


def test_watch_no_new_heartbeat_is_auditable_and_preserves_seen_identity() -> None:
    state = build_poll_state(
        {"seen_raw_sha256": ["old-sha"]},
        completed_at="2026-07-14T02:31:00Z",
        state="no_new_speci",
        realtime_ingress_enabled=True,
    )

    assert state["ok"] is True
    assert state["state"] == "no_new_speci"
    assert state["realtime_ingress_enabled"] is True
    assert state["seen_raw_sha256"] == ["old-sha"]
    assert state["guards"]["writes_posterior"] is False
    assert state["guards"]["places_orders"] is False


def test_watch_state_records_realtime_strong_ingress_without_execution() -> None:
    source = new_speci_sources(
        [_source("SPECI", "SPECI ZUUU 140200Z 18003MPS 9999 31/24 Q1002=", "2026-07-14T02:00:00Z")],
        seen_raw_sha256=set(),
    )
    sha = source[0]["raw_sha256"]
    state = build_state(
        {},
        completed_at="2026-07-14T02:01:00Z",
        archive_result={"raw_objects_by_sha256": {sha: {"raw_object_ref": "objects/ab/example.json.gz"}}},
        new_sources=source,
        realtime_ingress={
            "schema": "amsc_speci_realtime_strong_ingress_v1",
            "ok": True,
            "state": "source_fact_and_posterior_refreshed",
            "places_orders": False,
        },
    )

    assert state["state"] == "new_speci_source_fact_and_posterior_refreshed"
    assert state["new_speci_count"] == 1
    assert state["new_speci"][0]["report_type"] == "SPECI"
    assert state["seen_raw_sha256"] == [sha]
    assert state["guards"]["writes_archive_db"] is True
    assert state["guards"]["writes_posterior"] is True
    assert state["guards"]["writes_task_queue"] is False
    assert state["guards"]["places_orders"] is False


def test_watch_state_keeps_raw_archive_when_posterior_is_fail_closed() -> None:
    source = new_speci_sources(
        [_source("SPECI", "SPECI ZUUU 140200Z 18003MPS 9999 31/24 Q1002=", "2026-07-14T02:00:00Z")],
        seen_raw_sha256=set(),
    )
    sha = source[0]["raw_sha256"]
    state = build_state(
        {},
        completed_at="2026-07-14T02:01:00Z",
        archive_result={"raw_objects_by_sha256": {sha: {"raw_object_ref": "objects/ab/example.json.gz"}}},
        new_sources=source,
        realtime_ingress={"ok": False, "state": "source_fact_retained_posterior_blocked", "places_orders": False},
    )

    assert state["ok"] is True
    assert state["state"] == "new_speci_archived_posterior_blocked"
    assert state["new_speci"][0]["raw_object_ref"]
    assert state["guards"]["writes_posterior"] is False
