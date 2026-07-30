from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.model_run_contract import (  # noqa: E402
    PROVIDERS,
    build_model_run_contract,
    validate_capture_request,
)


def _contract() -> dict[str, object]:
    return build_model_run_contract(
        target_local_date="2026-07-25",
        run_initialization_utc="2026-07-24T00:00:00Z",
        station_ids=["KJFK", "ZUUU"],
        target_timezone="America/New_York",
    )


def test_contract_binds_all_three_official_providers_to_one_run() -> None:
    contract = _contract()
    assert tuple(contract["providers"]) == PROVIDERS
    assert contract["run_initialization_utc"] == "2026-07-24T00:00:00Z"
    assert contract["boundary"]["execution_mode"] == "production_no_live"
    assert contract["boundary"]["formal_probability_input_eligible"] is True
    assert contract["boundary"]["live_order_authorized"] is False
    assert contract["boundary"]["live_submit_authorized"] is False
    assert "offline_research_only" not in contract["boundary"]

    validated = validate_capture_request(
        contract,
        provider_id="icon_global_official",
        station_id="KJFK",
        target_local_date="2026-07-25",
        run_initialization_utc="2026-07-24T00:00:00+00:00",
        target_timezone="America/New_York",
    )
    assert validated["contract_sha256"] == contract["contract_sha256"]


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("run_initialization_utc", "2026-07-24T06:00:00Z", "model_run_contract_run_mismatch"),
        ("target_local_date", "2026-07-26", "model_run_contract_target_date_mismatch"),
        ("station_id", "EDDM", "model_run_contract_station_not_in_scope"),
    ],
)
def test_contract_rejects_mixed_capture_requests(field: str, value: str, reason: str) -> None:
    contract = _contract()
    kwargs = {
        "provider_id": "ecmwf_ifs025_official",
        "station_id": "KJFK",
        "target_local_date": "2026-07-25",
        "run_initialization_utc": "2026-07-24T00:00:00Z",
        "target_timezone": "America/New_York",
    }
    kwargs[field] = value
    with pytest.raises(ContractError, match=reason):
        validate_capture_request(contract, **kwargs)


def test_contract_tampering_fails_closed() -> None:
    contract = _contract()
    contract["run_initialization_utc"] = "2026-07-24T12:00:00Z"
    with pytest.raises(ContractError, match="model_run_contract_sha256_mismatch"):
        validate_capture_request(
            contract,
            provider_id="gfs_0p25_official",
            station_id="KJFK",
            target_local_date="2026-07-25",
            run_initialization_utc="2026-07-24T00:00:00Z",
            target_timezone="America/New_York",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("execution_mode", "shadow"),
        ("formal_probability_input_eligible", False),
        ("live_submit_authorized", True),
        ("unrelated_production_line_touched", True),
    ],
)
def test_contract_rejects_non_no_live_or_shadow_boundary(field: str, value: object) -> None:
    from bayes_basket_target.contracts import content_sha256

    contract = _contract()
    contract["boundary"][field] = value
    identity = {
        key: contract.get(key)
        for key in (
            "schema",
            "scope",
            "target_local_date",
            "target_timezone",
            "run_initialization_utc",
            "market_registry_snapshot_sha256",
            "providers",
            "station_ids",
            "boundary",
        )
    }
    contract["contract_sha256"] = content_sha256(identity)
    with pytest.raises(ContractError, match="model_run_contract_production_no_live_boundary_invalid"):
        validate_capture_request(
            contract,
            provider_id="gfs_0p25_official",
            station_id="KJFK",
            target_local_date="2026-07-25",
            run_initialization_utc="2026-07-24T00:00:00Z",
            target_timezone="America/New_York",
        )
