from __future__ import annotations

from datetime import datetime, timezone
import gzip
import json
from pathlib import Path

import pytest

from bayes_basket_target.awc_historical_gap_fetch import (
    HistoricalAwcGapError,
    build_gap_inventory,
    execute_gap_fetch,
    verify_gap_inventory,
)
from bayes_basket_target.contracts import content_sha256


def _target_identity() -> dict[str, object]:
    return {
        "station_id": "KJFK",
        "event_date": "2026-08-03",
        "expected_valid_at_utc": "2026-08-03T12:00:00Z",
        "expected_report_type": "METAR",
        "requested_revision_key": "ALL_OFFICIAL_REVISIONS",
    }


def _inventory(*, action: str = "FETCH_REQUIRED") -> dict[str, object]:
    target_identity = _target_identity()
    target = {
        "target_id": content_sha256(target_identity),
        **target_identity,
        "settlement_timezone": "America/New_York",
        "station_day_start_utc": "2026-08-03T04:00:00Z",
        "station_day_end_utc": "2026-08-04T04:00:00Z",
        "action": action,
    }
    identity = {
        "schema": "awc_historical_truth_gap_inventory_v1",
        "as_of_utc": "2026-08-04T05:00:00Z",
        "source_truth_gap": {"receipt_content_sha256": "a" * 64},
        "source_replay_manifest_content_sha256": "b" * 64,
        "station_count": 1,
        "target_count": 1,
        "targets": [target],
        "natural_key": [
            "station_id",
            "event_date",
            "expected_valid_at_utc",
            "requested_revision_key",
        ],
        "checks": {
            "exact_truth_gap_receipt_bound": True,
            "exact_replay_station_leaves_bound": True,
            "closed_historical_days_only": True,
            "exact_hour_metar_slots_only": True,
            "provider_revision_number_invented": False,
            "forward_data_counted_as_oos": False,
        },
        "boundary": {
            "production_no_live": True,
            "writes_target_database": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "creates_truth_or_activation": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _awc_row(raw_text: str = "METAR KJFK 031200Z 18005KT 10SM CLR 25/10 A2992") -> dict[str, object]:
    stamp = datetime(2026, 8, 3, 12, tzinfo=timezone.utc).timestamp()
    return {
        "icaoId": "KJFK",
        "obsTime": stamp,
        "metarType": "METAR",
        "temp": 25.0,
        "rawOb": raw_text,
    }


def _transport_with(rows: object, *, status: int = 200):
    body = json.dumps(rows, sort_keys=True).encode("utf-8") if rows is not None else b""

    def transport(url: str, timeout: int, max_bytes: int, proxy: str):
        assert "aviationweather.gov/api/data/metar" in url
        assert timeout == 15
        assert max_bytes == 2 * 1024 * 1024
        assert proxy == "http://127.0.0.1:1082"
        return status, body, {"Date": "Tue, 04 Aug 2026 00:00:00 GMT"}

    return transport


def test_inventory_expands_missing_slots_without_inventing_revision() -> None:
    evidence = {
        "station_id": "KJFK",
        "target_timezone": "America/New_York",
        "truth_blocked_dates": ["2026-08-03"],
        "station_day_completeness": [
            {
                "event_date": "2026-08-03",
                "station_day_start_utc": "2026-08-03T04:00:00Z",
                "station_day_end_utc": "2026-08-04T04:00:00Z",
                "missing_exact_hour_metar_slots": [
                    "2026-08-03T12:00:00Z",
                    "2026-08-03T13:00:00Z",
                ],
                "quarantined_metar_revision_slots": [],
            }
        ],
    }
    receipt = {
        "as_of_utc": "2026-08-04T05:00:00Z",
        "stations": [
            {"station_id": "KJFK", "truth_blocked_dates": ["2026-08-03"]}
        ],
    }
    manifest_identity = {"schema": "manifest"}
    manifest = {
        **manifest_identity,
        "content_sha256": content_sha256(manifest_identity),
    }
    inventory = build_gap_inventory(
        truth_gap_receipt=receipt,
        replay_manifest=manifest,
        station_evidences=[evidence],
        source_binding={"receipt_content_sha256": "a" * 64},
    )
    assert inventory["target_count"] == 2
    assert {
        row["requested_revision_key"] for row in inventory["targets"]
    } == {"ALL_OFFICIAL_REVISIONS"}
    assert all("expected_revision_no" not in row for row in inventory["targets"])
    assert inventory["checks"]["provider_revision_number_invented"] is False


def test_inventory_tamper_and_duplicate_natural_key_fail_closed() -> None:
    inventory = _inventory()
    inventory["targets"][0]["station_id"] = "KLAX"
    with pytest.raises(HistoricalAwcGapError, match="content_sha256_mismatch"):
        verify_gap_inventory(inventory)

    inventory = _inventory()
    identity = {key: value for key, value in inventory.items() if key != "content_sha256"}
    identity["targets"] = [identity["targets"][0], dict(identity["targets"][0])]
    identity["target_count"] = 2
    duplicate = {**identity, "content_sha256": content_sha256(identity)}
    with pytest.raises(HistoricalAwcGapError, match="duplicate|binding"):
        verify_gap_inventory(duplicate)


def test_success_retains_official_raw_and_rerun_does_not_fetch(tmp_path: Path) -> None:
    inventory = _inventory()
    receipt, observations = execute_gap_fetch(
        inventory,
        raw_root=tmp_path,
        requested_at_utc="2026-08-04T05:00:00Z",
        transport=_transport_with([_awc_row()]),
    )
    assert receipt["terminal_state_counts"] == {"FETCHED_OFFICIAL_RAW": 1}
    assert receipt["newly_retained_raw_count"] == 1
    assert receipt["observation_count"] == 1
    assert observations["observation_count"] == 1
    raw_sha = receipt["terminals"][0]["raw_sha256s"][0]
    raw_path = tmp_path / "objects" / raw_sha[:2] / f"{raw_sha}.json.gz"
    assert raw_path.is_file()
    with gzip.open(raw_path, "rt", encoding="utf-8") as handle:
        raw = json.load(handle)
    assert raw["source_lane"] == "awc"
    assert raw["source_poll_id"].startswith("awc-historical-pr02c-")

    def forbidden_transport(*_args):
        raise AssertionError("retained exact raw must suppress network")

    rerun, rerun_observations = execute_gap_fetch(
        inventory,
        raw_root=tmp_path,
        requested_at_utc="2026-08-04T05:00:00Z",
        transport=forbidden_transport,
    )
    assert rerun["query_count"] == 0
    assert rerun["terminal_state_counts"] == {"ALREADY_RETAINED": 1}
    assert rerun["newly_retained_raw_count"] == 0
    assert rerun_observations["observation_count"] == 1


@pytest.mark.parametrize(
    ("rows", "status", "expected_state", "expected_subtype"),
    [
        (None, 204, "DATA_MISSING", "OFFICIAL_PRODUCT_NOT_FOUND"),
        ([], 200, "DATA_MISSING", "EXACT_SLOT_MISSING_FROM_OFFICIAL_RESPONSE"),
        ([], 429, "BLOCKED", "HTTP_OR_NETWORK_FAILURE"),
    ],
)
def test_provider_terminal_states_are_unique(
    tmp_path: Path,
    rows: object,
    status: int,
    expected_state: str,
    expected_subtype: str,
) -> None:
    receipt, observations = execute_gap_fetch(
        _inventory(),
        raw_root=tmp_path,
        requested_at_utc="2026-08-04T05:00:00Z",
        transport=_transport_with(rows, status=status),
    )
    assert receipt["terminal_count"] == receipt["inventory_target_count"] == 1
    assert receipt["terminals"][0]["state"] == expected_state
    assert receipt["terminals"][0]["subtype"] == expected_subtype
    assert observations["observation_count"] == 0


def test_expired_window_is_terminal_without_network(tmp_path: Path) -> None:
    inventory = _inventory()
    identity = {key: value for key, value in inventory.items() if key != "content_sha256"}
    target = dict(identity["targets"][0])
    target_identity = {
        "station_id": "KJFK",
        "event_date": "2026-07-01",
        "expected_valid_at_utc": "2026-07-01T12:00:00Z",
        "expected_report_type": "METAR",
        "requested_revision_key": "ALL_OFFICIAL_REVISIONS",
    }
    target.update(
        {
            **target_identity,
            "target_id": content_sha256(target_identity),
            "station_day_start_utc": "2026-07-01T04:00:00Z",
            "station_day_end_utc": "2026-07-02T04:00:00Z",
        }
    )
    identity["targets"] = [target]
    expired = {**identity, "content_sha256": content_sha256(identity)}

    def forbidden_transport(*_args):
        raise AssertionError("expired target must not be retried")

    receipt, _ = execute_gap_fetch(
        expired,
        raw_root=tmp_path,
        requested_at_utc="2026-08-04T05:00:00Z",
        transport=forbidden_transport,
    )
    assert receipt["query_count"] == 0
    assert receipt["terminals"][0]["subtype"] == "OFFICIAL_RETENTION_WINDOW_EXPIRED"


def test_conflicting_official_revisions_are_retained_but_quarantined(tmp_path: Path) -> None:
    rows = [
        _awc_row("METAR KJFK 031200Z 18005KT 10SM CLR 25/10 A2992"),
        _awc_row("METAR KJFK 031200Z COR 18005KT 10SM CLR 26/10 A2992"),
    ]
    receipt, observations = execute_gap_fetch(
        _inventory(),
        raw_root=tmp_path,
        requested_at_utc="2026-08-04T05:00:00Z",
        transport=_transport_with(rows),
    )
    terminal = receipt["terminals"][0]
    assert terminal["state"] == "BLOCKED"
    assert terminal["subtype"] == "CORRECTION_REVISION_REVIEW_REQUIRED"
    assert len(terminal["raw_sha256s"]) == 2
    assert receipt["newly_retained_raw_count"] == 2
    assert observations["observation_count"] == 0


def test_transport_failure_is_durable_and_retryable(tmp_path: Path) -> None:
    def failing_transport(*_args):
        raise TimeoutError("proxy timeout")

    receipt, observations = execute_gap_fetch(
        _inventory(),
        raw_root=tmp_path,
        requested_at_utc="2026-08-04T05:00:00Z",
        transport=failing_transport,
    )
    terminal = receipt["terminals"][0]
    assert terminal["state"] == "BLOCKED"
    assert terminal["subtype"] == "HTTP_OR_NETWORK_FAILURE"
    assert terminal["retryable"] is True
    assert receipt["queries"][0]["error_subtype"].startswith("TimeoutError:")
    assert observations["observation_count"] == 0


def test_prior_receipt_retries_only_retryable_targets(tmp_path: Path) -> None:
    missing, _ = execute_gap_fetch(
        _inventory(),
        raw_root=tmp_path,
        requested_at_utc="2026-08-04T05:00:00Z",
        transport=_transport_with([]),
    )

    def forbidden_transport(*_args):
        raise AssertionError("non-retryable exact-missing target must not query")

    frozen, frozen_observations = execute_gap_fetch(
        _inventory(),
        raw_root=tmp_path,
        requested_at_utc="2026-08-04T05:01:00Z",
        prior_receipt=missing,
        transport=forbidden_transport,
    )
    assert frozen["query_count"] == 0
    assert frozen["prior_receipt_content_sha256"] == missing["content_sha256"]
    assert frozen["terminals"][0]["subtype"] == "EXACT_SLOT_MISSING_FROM_OFFICIAL_RESPONSE"
    assert frozen_observations["observation_count"] == 0

    def failing_transport(*_args):
        raise TimeoutError("retry me")

    failed, _ = execute_gap_fetch(
        _inventory(),
        raw_root=tmp_path,
        requested_at_utc="2026-08-04T05:00:00Z",
        transport=failing_transport,
    )
    recovered, recovered_observations = execute_gap_fetch(
        _inventory(),
        raw_root=tmp_path,
        requested_at_utc="2026-08-04T05:01:00Z",
        prior_receipt=failed,
        transport=_transport_with([_awc_row()]),
    )
    assert recovered["query_count"] == 1
    assert recovered["terminal_state_counts"] == {"FETCHED_OFFICIAL_RAW": 1}
    assert recovered_observations["observation_count"] == 1
