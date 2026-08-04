from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from bayes_basket_target.contracts import content_sha256
from scripts.backfill_native_ensemble_forward_scopes import (
    _load_packet_index,
    _packet_is_fresh,
    _select_event_date_scopes,
)


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def test_packet_freshness_is_bounded_and_strict_at_ttl() -> None:
    packet = {"aggregator_snapshot_at_utc": "2026-08-03T00:00:00Z"}
    as_of = _utc("2026-08-03T06:00:00Z")
    assert _packet_is_fresh(
        packet,
        timestamp_key="aggregator_snapshot_at_utc",
        as_of=as_of,
        max_age_seconds=6 * 60 * 60 + 1,
    )
    assert not _packet_is_fresh(
        packet,
        timestamp_key="aggregator_snapshot_at_utc",
        as_of=_utc("2026-08-03T06:00:00Z"),
        max_age_seconds=6 * 60 * 60,
    )
    assert not _packet_is_fresh(
        packet,
        timestamp_key="aggregator_snapshot_at_utc",
        as_of=_utc("2026-08-02T23:59:59Z"),
        max_age_seconds=6 * 60 * 60,
    )


def test_official_index_is_scope_exact_and_invalid_inbox_is_absent(
    tmp_path: Path,
) -> None:
    packet = {
        "schema": "official_native_ensemble_forward_packet_v1",
        "station_id": "KSEA",
        "event_date": "2026-08-03",
        "received_at_utc": "2026-08-03T00:00:00Z",
    }
    identity = {
        "schema": "native_ensemble_forward_inbox_v1",
        "packets": [packet],
    }
    path = tmp_path / "official.json"
    path.write_text(
        json.dumps(
            {**identity, "content_sha256": content_sha256(identity)},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    indexed, state = _load_packet_index(
        path,
        packet_schema="official_native_ensemble_forward_packet_v1",
    )
    assert state == "ready"
    assert indexed[("KSEA", "2026-08-03")]["station_id"] == "KSEA"

    path.write_text("{broken", encoding="utf-8")
    indexed, state = _load_packet_index(
        path,
        packet_schema="official_native_ensemble_forward_packet_v1",
    )
    assert indexed == {}
    assert state == "invalid"


def test_event_date_filter_keeps_only_target_scopes() -> None:
    scopes = [
        {"event_date": "2026-08-02", "station_ids": ["RKPK"]},
        {"event_date": "2026-08-03", "station_ids": ["RKPK"]},
        {"event_date": "2026-08-03", "station_ids": ["KSEA"]},
    ]
    selected = _select_event_date_scopes(
        scopes,
        event_date="2026-08-03",
    )
    assert selected == scopes[1:]
