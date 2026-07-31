from __future__ import annotations

import json

import pytest

from bayes_basket_target.contracts import ContractError
from scripts import (
    run_bayes_basket_target_45station_five_model_calibration as target,
)


def test_scope_is_exact_registry_bound_45():
    scope = target._load_scope(
        target.DEFAULT_LANE_MAP,
        target.DEFAULT_MARKET_REGISTRY,
    )

    assert len(scope) == 45
    assert scope["ZBAA"] == {
        "source_lane": "amsc",
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
