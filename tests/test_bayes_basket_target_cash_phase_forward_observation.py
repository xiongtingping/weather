from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from bayes_basket_target.cash_phase_forward_observation import (
    ForwardObservationError,
    append_settlement_labels,
    build_forward_observation_packet,
    write_forward_observation_packet,
)
from bayes_basket_target.contracts import content_sha256


ROOT = Path(__file__).resolve().parents[1]


def _inputs(*, probability_stage: str = "P1"):
    binding = {
        "schema": "bayes_basket_target_forward_evidence_binding_v1",
        "station_id": "RKPK",
        "event_date": "2026-07-27",
        "settlement_timezone": "Asia/Seoul",
        "decision_at_utc": "2026-07-26T21:08:23.560Z",
        "probability_stage": probability_stage,
        "probability_decision_at_utc": "2026-07-26T21:00:00Z",
        "probability_artifact_sha256": "3" * 64,
        "probability_lineage_sha256": "4" * 64,
        "ws_quote_observed_at_utc": "2026-07-26T21:08:20Z",
        "ws_quote_expires_at_utc": "2026-07-26T21:09:20Z",
        "ws_snapshot_sha256": "2" * 64,
        "ws_quote_count": 1,
        "inventory_sha256": "8" * 64,
        "order_state_sha256": "9" * 64,
        "cash_phase": "STANDARD",
        "cash_checkpoint_id": "cash-rkpk-1",
        "cash_observed_at_utc": "2026-07-26T21:08:20Z",
        "cash_decision_sha256": "5" * 64,
        "cash_policy_sha256": "6" * 64,
        "station_policy_sha256": "a" * 64,
        "delta_v2_artifact_sha256": "b" * 64,
        "delta_v2_evaluation_sha256": "e" * 64,
        "quote_source": "OFFICIAL_CLOB_WS",
    }
    binding["content_sha256"] = content_sha256(binding)
    cycle = {
        "schema": "bayes_basket_target_atomic_forward_cycle_receipt_v1",
        "cycle_id": "cycle-rkpk-1",
        "decision_id": "decision-rkpk-1",
        "plan_id": "plan-rkpk-1",
        "plan_sha256": "c" * 64,
        "decision_sha256": "d" * 64,
        "decision_at_utc": "2026-07-26T21:08:23.560Z",
        "forward_evidence_binding": binding,
        "execution_mode": "production_no_live",
        "writes_target_ledger": True,
        "orders_submitted": 0,
        "places_orders": False,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "authorizes_order": False,
        "evidence_only": True,
    }
    cycle["receipt_sha256"] = content_sha256(cycle)
    registry = {"rows": [{"station_id": "RKPK", "event_date": "2026-07-27", "settlement_timezone": "Asia/Seoul"}]}
    return cycle, registry


def _rehash(receipt):
    receipt["forward_evidence_binding"]["content_sha256"] = content_sha256(
        {
            key: value
            for key, value in receipt["forward_evidence_binding"].items()
            if key != "content_sha256"
        }
    )
    receipt["receipt_sha256"] = content_sha256(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    return receipt


def _rehash_packet(packet):
    for row in packet["observations"]:
        row["observation_sha256"] = content_sha256(
            {
                key: value
                for key, value in row.items()
                if key != "observation_sha256"
            }
        )
    packet["content_sha256"] = content_sha256(
        {key: value for key, value in packet.items() if key != "content_sha256"}
    )
    return packet


def _build(existing=None, *, probability_stage: str = "P1"):
    args = _inputs(probability_stage=probability_stage)
    return build_forward_observation_packet(
        cycle_receipt=args[0], registry=args[1], existing=existing,
    )


def test_minimal_no_live_packet_has_no_book_and_exact_identity() -> None:
    packet = _build()
    assert len(packet["observations"]) == 1
    row = packet["observations"][0]
    assert row["observation_key"] == "RKPK|2026-07-27|STANDARD|2026-07-26T21:08:23.560Z"
    assert row["settlement_timezone"] == "Asia/Seoul"
    assert row["execution_mode"] == "production_no_live"
    assert row["fill_status"] == "NO_EXECUTION"
    assert row["orders_submitted"] == 0
    assert row["evidence_only"] is True
    assert row["authorizes_order"] is False
    assert "asks" not in json.dumps(packet)
    assert packet["settlement_labels"] == []


def test_same_key_replay_is_zero_change_and_conflict_fails_closed() -> None:
    first = _build()
    second = _build(existing=first)
    assert second == first
    changed = deepcopy(first)
    changed["observations"][0]["ws_snapshot_sha256"] = "f" * 64
    with pytest.raises(ForwardObservationError, match="duplicate_conflict|content_sha256_mismatch"):
        _build(existing=changed)


def test_observation_missing_required_field_fails_after_packet_rehash(
    tmp_path: Path,
) -> None:
    packet = deepcopy(_build())
    del packet["observations"][0]["delta_v2_evaluation_sha256"]
    _rehash_packet(packet)
    with pytest.raises(
        ForwardObservationError,
        match="observation_required_fields_missing:delta_v2_evaluation_sha256",
    ):
        write_forward_observation_packet(tmp_path / "missing-field.json", packet)


def test_rehashed_packet_cannot_mask_source_binding_drift_on_replay() -> None:
    original = _build()
    tampered = deepcopy(original)
    row = tampered["observations"][0]
    row["forward_evidence_binding_sha256"] = "f" * 64
    row["probability_artifact_sha256"] = "e" * 64
    _rehash_packet(tampered)
    with pytest.raises(ForwardObservationError, match="observation_duplicate_conflict"):
        _build(existing=tampered)


@pytest.mark.parametrize(
    "scope,mutation,reason",
    [
        ("observation", "missing", "observation_required_fields_missing:fill_status"),
        ("observation", "conflict", "observation_fill_status_invalid"),
        ("packet", "missing", "observation_packet_fill_status_invalid"),
        ("packet", "conflict", "observation_packet_fill_status_invalid"),
    ],
)
def test_explicit_fill_status_missing_or_conflicting_is_rejected(
    tmp_path: Path,
    scope: str,
    mutation: str,
    reason: str,
) -> None:
    packet = deepcopy(_build())
    if scope == "observation":
        if mutation == "missing":
            del packet["observations"][0]["fill_status"]
        else:
            packet["observations"][0]["fill_status"] = "EXECUTED"
    else:
        if mutation == "missing":
            del packet["fill_status"]
        else:
            packet["fill_status"] = "EXECUTED"
    _rehash_packet(packet)
    with pytest.raises(ForwardObservationError, match=reason):
        write_forward_observation_packet(
            tmp_path / f"status-{scope}-{mutation}.json", packet
        )


@pytest.mark.parametrize("field,value", [("evidence_only", False), ("authorizes_order", True)])
def test_ws_order_authority_is_hard_gate(field: str, value: bool) -> None:
    args = list(_inputs())
    args[0] = deepcopy(args[0])
    args[0][field] = value
    _rehash(args[0])
    with pytest.raises(ForwardObservationError):
        build_forward_observation_packet(
            cycle_receipt=args[0], registry=args[1],
        )


def test_registry_timezone_mismatch_and_exact_stage_probability_binding() -> None:
    args = list(_inputs())
    args[1] = {"rows": [{"station_id": "RKPK", "event_date": "2026-07-27", "settlement_timezone": "America/New_York"}]}
    with pytest.raises(ForwardObservationError, match="timezone_mismatch"):
        build_forward_observation_packet(cycle_receipt=args[0], registry=args[1])
    for stage in ("P0", "P0-M", "P1"):
        row = _build(probability_stage=stage)["observations"][0]
        assert row["probability_stage"] == stage
        if stage == "P1":
            # The original P1-only stream is immutable: retained settlement
            # labels and retention receipts keep the v1 key unchanged.
            assert row["observation_key"] == (
                "RKPK|2026-07-27|STANDARD|2026-07-26T21:08:23.560Z"
            )
        else:
            assert row["observation_key"] == (
                "RKPK|2026-07-27|STANDARD|2026-07-26T21:08:23.560Z"
                f"|{stage}"
            )

    args = list(_inputs(probability_stage="P0-F"))
    with pytest.raises(ForwardObservationError, match="probability_stage_invalid"):
        build_forward_observation_packet(cycle_receipt=args[0], registry=args[1])


def test_same_clock_p0_and_p0m_are_distinct_exact_stage_observations() -> None:
    """A later P0-M must not poison the same-clock P0 forward record."""

    p0_packet = _build(probability_stage="P0")
    combined = _build(existing=p0_packet, probability_stage="P0-M")
    rows = combined["observations"]
    assert len(rows) == 2
    assert {row["probability_stage"] for row in rows} == {"P0", "P0-M"}
    assert len({row["observation_key"] for row in rows}) == 2


def test_rehashed_packet_cannot_smuggle_unknown_or_noncanonical_stage(
    tmp_path: Path,
) -> None:
    packet = _build(probability_stage="P0")
    packet["observations"][0]["probability_stage"] = "P0-F"
    _rehash_packet(packet)
    with pytest.raises(ForwardObservationError, match="probability_stage_invalid"):
        write_forward_observation_packet(tmp_path / "unknown-stage.json", packet)

    packet = _build(probability_stage="P0")
    packet["observations"][0]["probability_stage"] = "p0"
    _rehash_packet(packet)
    with pytest.raises(
        ForwardObservationError,
        match="probability_stage_not_canonical",
    ):
        write_forward_observation_packet(tmp_path / "lowercase-stage.json", packet)


@pytest.mark.parametrize(
    "field,value,reason",
    [
        (
            "probability_decision_at_utc",
            "2026-07-26T21:08:24Z",
            "probability_after_decision",
        ),
        (
            "ws_quote_observed_at_utc",
            "2026-07-26T21:08:24Z",
            "quote_after_decision",
        ),
        (
            "ws_quote_expires_at_utc",
            "2026-07-26T21:08:23.560Z",
            "quote_expired_at_decision",
        ),
        (
            "cash_observed_at_utc",
            "2026-07-26T21:08:24Z",
            "cash_after_decision",
        ),
    ],
)
def test_unified_receipt_rejects_source_clock_reversal(
    field: str, value: str, reason: str
) -> None:
    receipt, registry = _inputs()
    receipt = deepcopy(receipt)
    receipt["forward_evidence_binding"][field] = value
    _rehash(receipt)
    with pytest.raises(ForwardObservationError, match=reason):
        build_forward_observation_packet(
            cycle_receipt=receipt,
            registry=registry,
        )


def test_labels_are_append_only_and_unknown_or_conflicting_label_blocks() -> None:
    packet = _build()
    key = packet["observations"][0]["observation_key"]
    label = {
        "observation_key": key,
        "winning_bucket": "36°C",
        "labeled_at_utc": "2026-07-28T00:00:00Z",
        "settlement_authority": "market_registry_official_resolution",
        "official_settlement_sha256": "7" * 64,
        "settlement_timezone": "Asia/Seoul",
    }
    labeled = append_settlement_labels(packet, [label])
    assert labeled["settlement_labels"][0]["winning_bucket"] == "36°C"
    with pytest.raises(ForwardObservationError, match="unknown_observation"):
        append_settlement_labels(packet, [{**label, "observation_key": "unknown"}])
    with pytest.raises(ForwardObservationError, match="label_conflict"):
        append_settlement_labels(labeled, [{**label, "winning_bucket": "37°C"}])


def test_existing_settlement_labels_are_never_dropped_on_new_replay() -> None:
    packet = _build()
    key = packet["observations"][0]["observation_key"]
    labeled = append_settlement_labels(packet, [{
        "observation_key": key,
        "winning_bucket": "36°C",
        "labeled_at_utc": "2026-07-28T00:00:00Z",
        "settlement_authority": "market_registry_official_resolution",
        "official_settlement_sha256": "7" * 64,
        "settlement_timezone": "Asia/Seoul",
    }])
    replay = _build(existing=labeled)
    assert replay["settlement_labels"] == labeled["settlement_labels"]


def test_atomic_write_is_idempotent_and_db_targets_are_forbidden(tmp_path: Path) -> None:
    output = tmp_path / "target-local.json"
    packet = _build()
    assert write_forward_observation_packet(output, packet) is True
    before = output.read_bytes()
    assert write_forward_observation_packet(output, packet) is False
    assert output.read_bytes() == before
    with pytest.raises(ForwardObservationError, match="database_forbidden"):
        write_forward_observation_packet(tmp_path / "archive.db", packet)
    assert not list(tmp_path.glob(".*.tmp.*"))


def test_public_writer_rejects_tampered_packet_and_row_cap() -> None:
    packet = _build()
    tampered = deepcopy(packet)
    tampered["authorizes_order"] = True
    with pytest.raises(ForwardObservationError, match="authorizes_order"):
        write_forward_observation_packet("target-local.json", tampered)
    rows = []
    for index in range(4096):
        row = deepcopy(packet["observations"][0])
        row["event_date"] = f"{2030 + index // 365:04d}-01-01"
        row["observation_key"] = f"RKPK|{row['event_date']}|STANDARD|2026-07-26T21:08:23.560Z"
        row["observation_sha256"] = "a" * 64
        rows.append(row)
    bounded = deepcopy(packet)
    bounded["observations"] = rows
    bounded["content_sha256"] = "0" * 64
    with pytest.raises(ForwardObservationError):
        write_forward_observation_packet("target-local.json", bounded)

