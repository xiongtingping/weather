"""Rotating native capture must accumulate coverage, not overwrite it.

`capture_official_native_ensemble_forward` writes an inbox containing only the
scope it just fetched.  The resident captures one scope per rotation step, so
writing that file straight to the shared path would leave exactly one station's
packet alive at any time.  The merge keeps every other packet, replaces the
matching scope, and prunes days that are over everywhere.
"""

import json
from pathlib import Path

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.production_worker import (
    OFFICIAL_NATIVE_FORWARD_INBOX_SCHEMA,
    _merge_official_native_inbox,
)


def _packet(station: str, event_date: str, marker: str = "v1") -> dict:
    return {
        "schema": "official_native_ensemble_forward_packet_v1",
        "station_id": station,
        "event_date": event_date,
        "lead_bucket": "d2",
        "run_cycle": "18Z",
        "marker": marker,
    }


def _write_inbox(path: Path, packets: list[dict]) -> None:
    identity = {"schema": OFFICIAL_NATIVE_FORWARD_INBOX_SCHEMA, "packets": packets}
    path.write_text(
        json.dumps({**identity, "content_sha256": content_sha256(identity)}),
        encoding="utf-8",
    )


def _packets(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["packets"]


def _scopes(path: Path) -> set[tuple[str, str]]:
    return {(p["station_id"], p["event_date"]) for p in _packets(path)}


def test_other_stations_survive_a_capture(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox.json"
    _write_inbox(inbox, [_packet("KMIA", "2026-08-07"), _packet("KATL", "2026-08-07")])
    staging = tmp_path / "staging.json"
    _write_inbox(staging, [_packet("KLGA", "2026-08-07")])

    result = _merge_official_native_inbox(
        inbox, staging_path=staging, as_of_utc="2026-08-06T15:00:00Z"
    )

    assert result["packet_count"] == 3
    assert _scopes(inbox) == {
        ("KMIA", "2026-08-07"), ("KATL", "2026-08-07"), ("KLGA", "2026-08-07"),
    }


def test_same_scope_is_replaced_not_duplicated(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox.json"
    _write_inbox(inbox, [_packet("KMIA", "2026-08-07", marker="old")])
    staging = tmp_path / "staging.json"
    _write_inbox(staging, [_packet("KMIA", "2026-08-07", marker="new")])

    _merge_official_native_inbox(
        inbox, staging_path=staging, as_of_utc="2026-08-06T15:00:00Z"
    )

    packets = _packets(inbox)
    assert len(packets) == 1
    assert packets[0]["marker"] == "new"


def test_certainly_closed_days_are_pruned(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox.json"
    _write_inbox(inbox, [
        _packet("KATL", "2026-08-01"),
        _packet("KATL", "2026-08-03"),
        _packet("KATL", "2026-08-06"),
    ])
    staging = tmp_path / "staging.json"
    _write_inbox(staging, [_packet("KMIA", "2026-08-07")])

    result = _merge_official_native_inbox(
        inbox, staging_path=staging, as_of_utc="2026-08-06T15:00:00Z"
    )

    # 08-01 and 08-03 are over in every timezone; 08-06 is not.
    assert result["pruned"] == 2
    assert _scopes(inbox) == {("KATL", "2026-08-06"), ("KMIA", "2026-08-07")}


def test_a_fresh_capture_for_a_closed_day_is_still_kept(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox.json"
    _write_inbox(inbox, [])
    staging = tmp_path / "staging.json"
    _write_inbox(staging, [_packet("KMIA", "2026-08-01")])

    # Pruning applies to retained packets, never to what was just captured;
    # silently dropping a caller's own capture would be a confusing no-op.
    result = _merge_official_native_inbox(
        inbox, staging_path=staging, as_of_utc="2026-08-06T15:00:00Z"
    )

    assert result["pruned"] == 0
    assert _scopes(inbox) == {("KMIA", "2026-08-01")}


def test_content_hash_matches_written_packets(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox.json"
    _write_inbox(inbox, [_packet("KATL", "2026-08-07")])
    staging = tmp_path / "staging.json"
    _write_inbox(staging, [_packet("KMIA", "2026-08-07")])

    _merge_official_native_inbox(
        inbox, staging_path=staging, as_of_utc="2026-08-06T15:00:00Z"
    )

    written = json.loads(inbox.read_text(encoding="utf-8"))
    identity = {
        "schema": written["schema"],
        "packets": written["packets"],
    }
    assert written["content_sha256"] == content_sha256(identity)


def test_missing_inbox_is_created(tmp_path: Path) -> None:
    inbox = tmp_path / "absent.json"
    staging = tmp_path / "staging.json"
    _write_inbox(staging, [_packet("KMIA", "2026-08-07")])

    result = _merge_official_native_inbox(
        inbox, staging_path=staging, as_of_utc="2026-08-06T15:00:00Z"
    )

    assert result["packet_count"] == 1
    assert inbox.is_file()


def test_empty_staging_fails_closed(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox.json"
    _write_inbox(inbox, [_packet("KATL", "2026-08-07")])
    staging = tmp_path / "staging.json"
    _write_inbox(staging, [])

    with pytest.raises(ContractError, match="official_native_staging_packets_missing"):
        _merge_official_native_inbox(
            inbox, staging_path=staging, as_of_utc="2026-08-06T15:00:00Z"
        )

    # A failed merge must leave the shared inbox untouched.
    assert _scopes(inbox) == {("KATL", "2026-08-07")}
