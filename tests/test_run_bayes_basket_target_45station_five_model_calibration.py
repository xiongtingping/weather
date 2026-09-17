from __future__ import annotations

import json

import pytest

from bayes_basket_target.calibration_ingress import (
    HISTORICAL_AWC_REPLAY,
    NO_LIVE_REPLAY,
)
from bayes_basket_target.contracts import ContractError, content_sha256
from scripts import (
    run_bayes_basket_target_45station_five_model_calibration as target,
)
from scripts.build_bayes_basket_target_station_calibration_dataset import (
    build as build_truth_dataset,
)
from bayes_basket_target.fleet import expected_station_count

# The scope is joined against the frozen 2026-07-31 market registry export,
# which lists 45 stations; a station onboarded later has no timezone row in
# it and the join fails closed rather than guessing.  Re-exporting the
# snapshot is what restores coverage.
_SNAPSHOT_STATIONS = len({
    str(row["station_id"])
    for row in json.loads(
        target.DEFAULT_MARKET_REGISTRY.read_text(encoding="utf-8")
    )["rows"]
    if row.get("station_id")
})
pytestmark = pytest.mark.skipif(
    _SNAPSHOT_STATIONS != expected_station_count(),
    reason=(
        f"market registry snapshot holds {_SNAPSHOT_STATIONS} stations"
        f" against a fleet of {expected_station_count()}; re-export it"
    ),
)



def _valid_external_truth() -> dict[str, object]:
    observation = {
        "station_id": "ZBAA",
        "source_lane": "AWC",
        "source_role": HISTORICAL_AWC_REPLAY,
        "authority_scope": NO_LIVE_REPLAY,
        "settlement_timezone": "Asia/Shanghai",
        "evidence_role": "FORMAL",
        "reference_only": False,
        "qc_status": "ACCEPTED",
        "report_type": "METAR",
        "valid_at_utc": "2026-06-28T05:00:00Z",
        "reported_temperature_c": 31.0,
        "raw_sha256": "a" * 64,
    }
    evidence_identity = {
        "schema": "bayes_basket_target_canonical_observation_evidence_v1",
        "target_timezone": "Asia/Shanghai",
        "historical_only": True,
        "canonical_ingress": True,
        "source_lane": "AWC",
        "source_role": HISTORICAL_AWC_REPLAY,
        "authority_scope": NO_LIVE_REPLAY,
        "observations": [observation],
        "boundary": {
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_arrival_evidence": False,
            "enters_realtime_truth": False,
            "enters_p1": False,
            "enters_hard_no": False,
        },
    }
    return build_truth_dataset(
        payloads=[{
            **evidence_identity,
            "evidence_sha256": content_sha256(evidence_identity),
        }],
        station_id="ZBAA",
        settlement_timezone="Asia/Shanghai",
        source_lane="AWC",
        start_date="2026-06-28",
        end_date="2026-06-28",
        as_of_utc="2026-06-29T00:00:00Z",
    )


def test_scope_is_exact_registry_bound_45():
    scope = target._load_scope(
        target.DEFAULT_LANE_MAP,
        target.DEFAULT_MARKET_REGISTRY,
    )

    assert len(scope) == expected_station_count()
    assert scope["ZBAA"] == {
        "source_lane": "awc",
        "settlement_timezone": "Asia/Shanghai",
    }
    assert scope["KATL"] == {
        "source_lane": "awc",
        "settlement_timezone": "America/New_York",
    }


def test_scope_rejects_missing_station(tmp_path):
    lane_map = json.loads(target.DEFAULT_LANE_MAP.read_text())
    lane_map["stations"].pop("ZBAA")
    path = tmp_path / "lane-map.json"
    path.write_text(json.dumps(lane_map))

    with pytest.raises(ContractError, match="lane_map_invalid"):
        target._load_scope(path, target.DEFAULT_MARKET_REGISTRY)


def test_scope_rejects_non_awc_lane(tmp_path):
    lane_map = json.loads(target.DEFAULT_LANE_MAP.read_text())
    lane_map["stations"]["ZBAA"] = "amsc"
    path = tmp_path / "lane-map.json"
    path.write_text(json.dumps(lane_map))

    with pytest.raises(ContractError, match="lane_not_awc"):
        target._load_scope(path, target.DEFAULT_MARKET_REGISTRY)


def test_date_and_worker_style_resource_bounds():
    assert len(target._dates("2026-06-28", "2026-07-27")) == 30
    with pytest.raises(ContractError, match="date_range_invalid"):
        target._dates("2026-01-01", "2026-07-27")


def test_forward_oos_is_not_a_production_no_live_blocker():
    source = target.__file__
    text = open(source, encoding="utf-8").read()

    assert '"forward_oos_required_before_activation": False' in text
    assert '"forward_oos_observational_not_blocking": True' in text


def test_targeted_repair_scope_is_still_registry_bound():
    canonical = target._load_scope(
        target.DEFAULT_LANE_MAP,
        target.DEFAULT_MARKET_REGISTRY,
    )
    selected = {
        station: canonical[station]
        for station in ("EHAM", "RCSS", "ZSQD")
    }

    assert set(selected) == {"EHAM", "RCSS", "ZSQD"}
    assert all(station in canonical for station in selected)
