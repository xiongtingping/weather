from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bayes_basket_target.cash_phase import BasketCashPhasePolicyArtifact, CashPhase
from bayes_basket_target.cash_phase_authority import (
    CANDIDATE_AUTHORITY_SCOPE,
    CashPhaseAuthorityManifest,
)
from bayes_basket_target.cash_phase_producer import (
    build_cash_phase_authority_set,
    build_cash_phase_ingress,
    materialize_cash_phase_authority_set,
)
from bayes_basket_target.contracts import ContractError, content_sha256


def _policy() -> BasketCashPhasePolicyArtifact:
    return BasketCashPhasePolicyArtifact.create(
        policy_version="cash-producer-test-v1",
        station_id="ZUUU",
        station_timezone="Asia/Shanghai",
        heavy_to_standard_local_time="19:54",
        standard_to_tail_local_time="09:58",
        cash_by_phase={"HEAVY": 6.0, "STANDARD": 4.0, "TAIL": 1.0},
        valid_from_utc="2026-07-01T00:00:00Z",
        valid_until_utc="2026-08-01T00:00:00Z",
        evidence_status="DESIGN_CANDIDATE",
    )


def test_cash_phase_producer_emits_lineaged_zero_order_ingress() -> None:
    policy = _policy()
    packet = build_cash_phase_ingress(
        policy=policy,
        station_id="ZUUU",
        event_date_local="2026-07-23",
        station_timezone="Asia/Shanghai",
        observed_at_utc="2026-07-23T04:00:00Z",
        current_phase=CashPhase.HEAVY,
        checkpoint_id="cash-producer-checkpoint",
    )

    market = packet["markets"][0]
    decision = market["cash_phase_decision"]
    assert packet["schema"] == "bayes_basket_cash_phase_ingress_v1"
    assert packet["producer_schema"] == "bayes_basket_target_cash_phase_producer_v1"
    assert packet["policy_version"] == policy.policy_version
    assert packet["policy_sha256"] == policy.content_sha256
    assert packet["phase_decision_sha256"] == content_sha256(decision)
    assert market["cash_phase_input"]["station_id"] == "ZUUU"
    assert decision["cash_template_usdc"] == 1.0
    assert packet["places_orders"] is False
    assert packet["calls_order_gateway"] is False


def test_cash_phase_producer_accepts_mapping_and_canonicalizes_timestamps() -> None:
    policy = _policy()
    packet = build_cash_phase_ingress(
        policy=policy.to_dict(),
        station_id="ZUUU",
        event_date_local="2026-07-23",
        station_timezone="Asia/Shanghai",
        observed_at_utc="2026-07-23T04:00:00+00:00",
        current_phase="HEAVY",
        checkpoint_id="cash-producer-checkpoint",
    )
    assert packet["markets"][0]["cash_phase_input"]["observed_at_utc"] == "2026-07-23T04:00:00Z"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"station_id": "OTHER"}, "scope_mismatch"),
        ({"station_timezone": "UTC"}, "scope_mismatch"),
        ({"observed_at_utc": "2026-08-01T00:00:00Z"}, "outside_validity"),
        ({"current_phase": "UNKNOWN"}, "current_cash_phase_invalid"),
    ],
)
def test_cash_phase_producer_fails_closed_on_scope_or_phase_drift(kwargs: dict[str, str], reason: str) -> None:
    policy = _policy()
    args = {
        "policy": policy,
        "station_id": "ZUUU",
        "event_date_local": "2026-07-23",
        "station_timezone": "Asia/Shanghai",
        "observed_at_utc": "2026-07-23T04:00:00Z",
        "current_phase": CashPhase.HEAVY,
        "checkpoint_id": "cash-producer-checkpoint",
    }
    args.update(kwargs)
    with pytest.raises(ContractError, match=reason):
        build_cash_phase_ingress(**args)


def test_cash_phase_producer_payload_is_json_serializable() -> None:
    policy = _policy()
    packet = build_cash_phase_ingress(
        policy=policy,
        station_id="ZUUU",
        event_date_local="2026-07-23",
        station_timezone="Asia/Shanghai",
        observed_at_utc="2026-07-23T04:00:00Z",
        current_phase="HEAVY",
        checkpoint_id="cash-producer-checkpoint",
    )
    json.dumps(packet, ensure_ascii=False, sort_keys=True)


def test_cash_phase_producer_rejects_future_settlement_day() -> None:
    with pytest.raises(ContractError, match="future_event_day_not_allowed"):
        build_cash_phase_ingress(
            policy=_policy(),
            station_id="ZUUU",
            event_date_local="2026-07-25",
            station_timezone="Asia/Shanghai",
            observed_at_utc="2026-07-24T15:59:59Z",
            current_phase=CashPhase.HEAVY,
            checkpoint_id="future-cash-checkpoint",
        )


def _formal_policy(
    station_id: str,
    *,
    timezone_name: str = "UTC",
    version: str | None = None,
    cash_by_phase: dict[str, float] | None = None,
    valid_from: str = "2026-07-01T00:00:00Z",
    valid_until: str = "2026-08-01T00:00:00Z",
    promoted: bool = False,
) -> BasketCashPhasePolicyArtifact:
    return BasketCashPhasePolicyArtifact.create(
        policy_version=version or f"cash-owned-{station_id}-v1",
        station_id=station_id,
        station_timezone=timezone_name,
        heavy_to_standard_local_time="19:54",
        standard_to_tail_local_time="09:58",
        cash_by_phase=cash_by_phase or {
            "HEAVY": 3.0,
            "STANDARD": 2.0,
            "TAIL": 1.0,
        },
        valid_from_utc=valid_from,
        valid_until_utc=valid_until,
        evidence_status="PROMOTED" if promoted else "DESIGN_CANDIDATE",
        promoted=promoted,
    )


def _authority_inputs(count: int = 1) -> dict[str, object]:
    station_ids = [f"S{index:02d}" for index in range(count)]
    return {
        "policies": [_formal_policy(station) for station in station_ids],
        "registry_rows": [
            {"station_id": station, "settlement_timezone": "UTC"}
            for station in station_ids
        ],
        "observed_at_utc": "2026-07-27T12:00:00Z",
        "observed_cash_phases": {
            station: "STANDARD" for station in station_ids
        },
        "expected_station_ids": station_ids,
    }


@pytest.mark.parametrize(
    ("existing_count", "expected_state"),
    [
        (0, "BLOCKED_REPAIR_CANDIDATE"),
        (1, "READY"),
        (2, "BLOCKED_REPAIR_CANDIDATE"),
    ],
)
def test_authority_set_handles_zero_one_two_existing_manifests(
    existing_count: int,
    expected_state: str,
) -> None:
    args = _authority_inputs()
    initial = build_cash_phase_authority_set(**args)
    desired = initial["authority_manifests"][0]
    packet = build_cash_phase_authority_set(
        **args,
        existing_manifests=[desired] * existing_count,
    )
    assert packet["state"] == expected_state
    assert packet["execution_authorized"] is (expected_state == "READY")
    assert packet["repair_candidate_complete"] is True
    assert len(packet["authority_manifests"]) == 1
    assert (
        packet["authority_manifests"][0]["authority_scope"]
        == CANDIDATE_AUTHORITY_SCOPE
    )


def test_authority_set_rejects_formal_scope_without_dedicated_oos_schema() -> None:
    args = _authority_inputs()
    args["policies"] = [_formal_policy("S00", promoted=True)]
    with pytest.raises(
        ContractError,
        match="cash_phase_formal_oos_authority_schema_missing",
    ):
        build_cash_phase_authority_set(
            **args,
            authority_scope="FORMAL_PROMOTED",
        )


def test_authority_set_45_stations_is_complete_and_content_addressed() -> None:
    args = _authority_inputs(45)
    first = build_cash_phase_authority_set(**args)
    second = build_cash_phase_authority_set(**args)
    assert first == second
    assert first["state"] == "BLOCKED_REPAIR_CANDIDATE"
    assert len(first["station_ids"]) == 45
    assert len(first["authority_manifests"]) == 45
    core = {
        key: value
        for key, value in first.items()
        if key != "authority_set_sha256"
    }
    assert first["authority_set_sha256"] == content_sha256(core)
    ready = build_cash_phase_authority_set(
        **args,
        existing_manifests=first["authority_manifests"],
    )
    assert ready["state"] == "READY"
    assert ready["execution_authorized"] is True
    assert ready["blockers"] == []
    assert len({
        row["station_id"] for row in ready["authority_manifests"]
    }) == 45


def test_authority_set_rejects_46_stations() -> None:
    with pytest.raises(
        ContractError,
        match="cash_phase_authority_station_scope_exceeded",
    ):
        build_cash_phase_authority_set(**_authority_inputs(46))


def test_extra_46th_existing_manifest_blocks_45_station_ready_set() -> None:
    args = _authority_inputs(45)
    candidate = build_cash_phase_authority_set(**args)
    desired = candidate["authority_manifests"]
    extra = CashPhaseAuthorityManifest.create(
        station_id="S45",
        policy_version="extra-v1",
        artifact_sha256="f" * 64,
        station_timezone="UTC",
        valid_from_utc="2026-07-01T00:00:00Z",
        valid_until_utc="2026-08-01T00:00:00Z",
            authority_scope=CANDIDATE_AUTHORITY_SCOPE,
    )
    packet = build_cash_phase_authority_set(
        **args,
        existing_manifests=[*desired, extra],
    )
    assert packet["state"] == "BLOCKED_REPAIR_CANDIDATE"
    assert packet["execution_authorized"] is False
    assert packet["blockers"][0] == {
        "station_id": "S45",
        "reason": "cash_phase_authority_manifest_station_scope_mismatch",
        "active_manifest_count": 1,
    }


def test_registry_timezone_is_the_only_timezone_authority() -> None:
    args = _authority_inputs()
    args["registry_rows"] = [{
        "station_id": "S00",
        "settlement_timezone": "Asia/Seoul",
    }]
    packet = build_cash_phase_authority_set(**args)
    assert packet["state"] == "BLOCKED_FAIL_CLOSED"
    assert packet["authority_manifests"] == []
    assert packet["blockers"][0]["candidate_count"] == 0

    args["registry_rows"] = [
        {"station_id": "S00", "settlement_timezone": "UTC"},
        {"station_id": "S00", "settlement_timezone": "Asia/Seoul"},
    ]
    with pytest.raises(
        ContractError,
        match="cash_phase_registry_settlement_timezone_not_unique",
    ):
        build_cash_phase_authority_set(**args)


@pytest.mark.parametrize("phase", ["", "UNKNOWN", "HEAVY"])
def test_unknown_or_unobserved_phase_yields_zero_atomic_candidates(
    phase: str,
) -> None:
    args = _authority_inputs()
    args["policies"] = [
        _formal_policy(
            "S00",
            cash_by_phase={"STANDARD": 2.0, "TAIL": 1.0},
        )
    ]
    args["observed_cash_phases"] = {"S00": phase}
    packet = build_cash_phase_authority_set(**args)
    expected_blocked = phase in {"", "UNKNOWN", "HEAVY"}
    assert expected_blocked
    assert packet["state"] == "BLOCKED_FAIL_CLOSED"
    assert packet["authority_manifests"] == []
    assert packet["repair_candidate_complete"] is False


def test_overlapping_formal_policy_windows_yield_no_repair_candidate() -> None:
    args = _authority_inputs()
    args["policies"] = [
        _formal_policy(
            "S00",
            version="cash-overlap-v1",
            valid_until="2026-07-28T00:00:00Z",
        ),
        _formal_policy(
            "S00",
            version="cash-overlap-v2",
            valid_from="2026-07-27T00:00:00Z",
        ),
    ]
    packet = build_cash_phase_authority_set(**args)
    assert packet["state"] == "BLOCKED_FAIL_CLOSED"
    assert packet["authority_manifests"] == []
    assert (
        packet["blockers"][0]["reason"]
        == "cash_phase_authority_policy_window_overlap"
    )


def test_latent_existing_manifest_overlap_blocks_ready_state() -> None:
    args = _authority_inputs()
    candidate = build_cash_phase_authority_set(**args)
    desired = candidate["authority_manifests"][0]
    future = [
        CashPhaseAuthorityManifest.create(
            station_id="S00",
            policy_version=f"future-v{index}",
            artifact_sha256=str(index) * 64,
            station_timezone="UTC",
            valid_from_utc=valid_from,
            valid_until_utc=valid_until,
        authority_scope=CANDIDATE_AUTHORITY_SCOPE,
        )
        for index, (valid_from, valid_until) in enumerate(
            [
                ("2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z"),
                ("2026-08-15T00:00:00Z", "2026-10-01T00:00:00Z"),
            ],
            start=1,
        )
    ]
    packet = build_cash_phase_authority_set(
        **args,
        existing_manifests=[desired, *future],
    )
    assert packet["state"] == "BLOCKED_REPAIR_CANDIDATE"
    assert packet["execution_authorized"] is False
    assert (
        packet["blockers"][0]["reason"]
        == "cash_phase_authority_manifest_window_overlap"
    )


def test_low_wrong_low_rewind_is_deterministic() -> None:
    args = _authority_inputs()
    low_1 = build_cash_phase_authority_set(**args)
    wrong_args = dict(args)
    wrong_args["observed_cash_phases"] = {"S00": "UNKNOWN"}
    wrong = build_cash_phase_authority_set(**wrong_args)
    low_2 = build_cash_phase_authority_set(**args)
    assert low_1 == low_2
    assert low_1["state"] == "BLOCKED_REPAIR_CANDIDATE"
    assert wrong["state"] == "BLOCKED_FAIL_CLOSED"
    assert wrong["authority_manifests"] == []


def test_atomic_materialization_failure_preserves_previous_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "authority.json"
    destination.write_text('{"old":true}\n', encoding="utf-8")
    before = destination.read_bytes()
    real_replace = os.replace

    def fail_replace(source: object, target: object) -> None:
        if Path(target) == destination:
            raise OSError("injected_replace_failure")
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected_replace_failure"):
        materialize_cash_phase_authority_set(
            destination,
            **_authority_inputs(),
        )
    assert destination.read_bytes() == before
    assert list(tmp_path.glob(".*.tmp.*")) == []


def test_atomic_materialization_zero_write_when_unchanged(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "authority.json"
    first = materialize_cash_phase_authority_set(
        destination,
        **_authority_inputs(),
    )
    mtime = destination.stat().st_mtime_ns
    second = materialize_cash_phase_authority_set(
        destination,
        **_authority_inputs(),
    )
    assert first["wrote"] is True
    assert second["wrote"] is False
    assert destination.stat().st_mtime_ns == mtime
