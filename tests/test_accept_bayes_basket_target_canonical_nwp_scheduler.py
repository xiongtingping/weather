from __future__ import annotations

from datetime import datetime, timezone

from bayes_basket_target.station_registry import coordinate_registry_manifest
from bayes_basket_target.nwp_rotation_state import build_rotation_state
from scripts.accept_bayes_basket_target_canonical_nwp_scheduler import (
    audit_status,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _status(capture: dict) -> dict:
    return {
        "heartbeat_at": _now(),
        "forecast_prefetch": {
            "resident_nwp_capture": capture,
        },
    }


def _capture(*, day_count: int = 1) -> dict:
    stations = coordinate_registry_manifest()["station_ids"]
    scopes = [
        {
            "station_ids": [station],
            "event_date": event_date,
            "settlement_timezone": "UTC",
        }
        for event_date in (
            "2026-07-30",
            "2026-07-31",
            "2026-08-01",
        )[:day_count]
        for station in stations
    ]
    selected = scopes[:4]
    capture = {
        "state": "candidate_capture_only",
        "station_scope_mode": "canonical_allowlist",
        "configured_station_ids": stations,
        "discovered_station_ids": stations,
        "scope_count": len(scopes),
        "scope_catalog_sha256": "",
        "selected_scope_count": 4,
        "successful_scope_count": 0,
        "candidate_prefetch_attempt_count": 1,
        "candidate_prefetch_scope_limit": 1,
        "calls_order_gateway": False,
        "places_orders": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }
    capture["rotation_state"] = build_rotation_state(
        station_ids=stations,
        scopes=scopes,
        selected_scopes=selected,
        cursor_by_priority={0: 4},
        outcomes=[
            {
                "scope_key": (
                    f"{row['station_ids'][0]}|2026-07-30|UTC"
                ),
                "state": "candidate_capture_only",
                "candidate_only": True,
            }
            for row in selected
        ],
        previous=None,
        updated_at_utc=_now(),
    )
    capture["scope_catalog_sha256"] = capture[
        "rotation_state"
    ]["catalog_sha256"]
    return capture


def test_canonical_candidate_only_rotation_is_accepted() -> None:
    result = audit_status(_status(_capture()), as_of_utc=_now())
    assert result["state"] == "PASS"
    assert len(result["discovered_station_ids"]) == 45


def test_single_station_or_unbounded_capture_fails_closed() -> None:
    capture = _capture()
    capture["discovered_station_ids"] = ["RKPK"]
    capture["selected_scope_count"] = 45
    result = audit_status(_status(capture), as_of_utc=_now())
    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert "canonical_nwp_discovered_scope_mismatch" in result["blockers"]
    assert "canonical_nwp_selected_scope_bound_invalid" in result["blockers"]


def test_not_due_uses_last_complete_attempt() -> None:
    status = _status({
        "state": "not_due",
        "last_attempt": _capture(),
    })
    result = audit_status(status, as_of_utc=_now())
    assert result["state"] == "PASS"


def test_canonical_90_and_135_scope_catalogs_are_accepted() -> None:
    for day_count, expected in ((2, 90), (3, 135)):
        result = audit_status(
            _status(_capture(day_count=day_count)),
            as_of_utc=_now(),
        )
        assert result["state"] == "PASS"
        assert result["scope_count"] == expected


def test_scope_count_or_catalog_hash_drift_fails_closed() -> None:
    capture = _capture(day_count=3)
    capture["scope_count"] = 45
    capture["scope_catalog_sha256"] = "0" * 64
    result = audit_status(_status(capture), as_of_utc=_now())
    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert "canonical_nwp_rotation_catalog_mismatch" in result["blockers"]
    assert (
        "canonical_nwp_rotation_catalog_hash_mismatch"
        in result["blockers"]
    )
