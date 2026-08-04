from __future__ import annotations

import json
from pathlib import Path

from scripts.build_bayes_basket_target_cash_phase_oos_evidence import (
    build_from_paths,
)


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def test_missing_real_rkpk_evidence_is_na_with_exact_deficits(
    tmp_path: Path,
) -> None:
    policies = tmp_path / "policies.json"
    registry = tmp_path / "registry.json"
    _write(policies, {
        "cash_phase_policies": [{
            "station_id": "RKPK",
            "station_timezone": "Asia/Seoul",
            "policy_version": "rkpk-design-v1",
            "evidence_status": "DESIGN_CANDIDATE",
            "promoted": False,
        }],
    })
    _write(registry, {
        "rows": [{
            "station_id": "RKPK",
            "settlement_timezone": "Asia/Seoul",
        }],
    })
    receipt = build_from_paths(
        observations_path=tmp_path / "missing-observations.json",
        policies_path=policies,
        registry_path=registry,
        training_cutoffs_path=tmp_path / "missing-cutoffs.json",
        fault_cases_path=tmp_path / "missing-faults.json",
        as_of_utc="2026-07-28T00:00:00Z",
        evaluation_start_utc="2026-06-28T00:00:00Z",
        evaluation_end_utc="2026-07-28T00:00:00Z",
    )
    assert receipt["status"] == "NA"
    assert receipt["rkpk"]["status"] == "NA"
    assert receipt["rkpk"]["registry_timezones"] == ["Asia/Seoul"]
    assert receipt["rkpk"]["observation_count"] == 0
    assert (
        "forward_realized_observations_missing"
        in receipt["rkpk"]["missing_requirements"]
    )
    assert (
        "phase_heavy_unique_event_deficit:30"
        in receipt["rkpk"]["missing_requirements"]
    )
    assert (
        "settled_attribution_deficit:90"
        in receipt["rkpk"]["missing_requirements"]
    )
    assert (
        "price_coverage_deficit:90"
        in receipt["rkpk"]["missing_requirements"]
    )
    assert receipt["design_candidate_mutated"] is False
    assert receipt["writes_authority"] is False
    assert receipt["calls_order_gateway"] is False
    assert receipt["orders_submitted"] == 0


def test_rkpk_wrong_timezone_is_reported_not_guessed(tmp_path: Path) -> None:
    paths = {
        name: tmp_path / f"{name}.json"
        for name in (
            "observations",
            "policies",
            "registry",
            "training",
            "faults",
        )
    }
    _write(paths["observations"], {"observations": []})
    _write(paths["policies"], {"cash_phase_policies": [{
        "station_id": "RKPK",
        "station_timezone": "America/New_York",
        "policy_version": "stale-rkpk-v1",
        "evidence_status": "DESIGN_CANDIDATE",
        "promoted": False,
    }]})
    _write(paths["registry"], {"rows": [{
        "station_id": "RKPK",
        "settlement_timezone": "Asia/Seoul",
    }]})
    _write(paths["training"], {"policy_training_cutoffs": {}})
    _write(paths["faults"], {"fault_cases": []})
    receipt = build_from_paths(
        observations_path=paths["observations"],
        policies_path=paths["policies"],
        registry_path=paths["registry"],
        training_cutoffs_path=paths["training"],
        fault_cases_path=paths["faults"],
        as_of_utc="2026-07-28T00:00:00Z",
        evaluation_start_utc="2026-06-28T00:00:00Z",
        evaluation_end_utc="2026-07-28T00:00:00Z",
    )
    assert receipt["status"] == "NA"
    assert (
        "policy_registry_timezone_mismatch"
        in receipt["rkpk"]["missing_requirements"]
    )


def test_rkpk_candidate_price_is_counted_with_the_oos_evaluator_alias(
    tmp_path: Path,
) -> None:
    paths = {
        name: tmp_path / f"{name}.json"
        for name in ("observations", "policies", "registry", "training", "faults")
    }
    _write(paths["observations"], {
        "observations": [
            {
                "station_id": "RKPK",
                "phase": "TAIL",
                "candidate_price": 0.02,
                "settlement_pnl_usdc": 0.0,
                "labels": [{"winning_bucket": "39°C"}],
            }
            for _ in range(90)
        ],
    })
    _write(paths["policies"], {"cash_phase_policies": [{
        "station_id": "RKPK",
        "station_timezone": "Asia/Seoul",
        "policy_version": "rkpk-design-v1",
        "evidence_status": "DESIGN_CANDIDATE",
        "promoted": False,
    }]})
    _write(paths["registry"], {"rows": [{
        "station_id": "RKPK",
        "settlement_timezone": "Asia/Seoul",
    }]})
    _write(paths["training"], {"policy_training_cutoffs": {"RKPK": "2026-06-28T00:00:00Z"}})
    _write(paths["faults"], {"fault_cases": []})
    receipt = build_from_paths(
        observations_path=paths["observations"],
        policies_path=paths["policies"],
        registry_path=paths["registry"],
        training_cutoffs_path=paths["training"],
        fault_cases_path=paths["faults"],
        as_of_utc="2026-07-28T00:00:00Z",
        evaluation_start_utc="2026-06-28T00:00:00Z",
        evaluation_end_utc="2026-07-28T00:00:00Z",
    )
    assert receipt["rkpk"]["price_coverage_count"] == 90
    assert not any(
        item.startswith("price_coverage_deficit:")
        for item in receipt["rkpk"]["missing_requirements"]
    )
