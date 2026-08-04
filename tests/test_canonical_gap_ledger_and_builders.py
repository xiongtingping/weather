from __future__ import annotations

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.gap_ledger import GapLedger, GapRecord, build_gap_ledger
from bayes_basket_target.model_builders import builder_for, build_scope, registered_builders


def _record(event_date: str = "2026-07-23") -> dict:
    identity = {
        "station_id": "ZUUU",
        "model_id": "ECMWF",
        "model_family": "global_deterministic",
        "run_cycle": "PREVIOUS_DAY1",
        "lead_bucket": "d1",
        "event_date": event_date,
        "evidence_role": "historical_strict_oos",
        "native_arrival_verified": False,
        "raw_receipt_hash_complete": True,
        "awc_truth_closed": True,
        "taf_revision_complete": False,
        "train_count": 20,
        "holdout_count": 8,
        "state": "OOS_READY",
        "blockers": [],
        "evidence": ["receipt.json"],
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def test_gap_ledger_has_one_scope_and_never_relabels_future() -> None:
    future = _record("2026-08-04")
    future["evidence_role"] = "observational_candidate_not_oos"
    future["train_count"] = 0
    future["holdout_count"] = 0
    future["blockers"] = ["TRUTH_NOT_CLOSED"]
    future["content_sha256"] = content_sha256({k: v for k, v in future.items() if k != "content_sha256"})
    ledger = build_gap_ledger([_record(), future], as_of_utc="2026-08-03T12:00:00Z")
    assert ledger["summary"]["historical_strict_oos_rows"] == 1
    assert ledger["summary"]["observational_candidate_not_oos_rows"] == 1
    assert ledger["future_not_oos"] is True

    duplicate = _record()
    with pytest.raises(ContractError, match="gap_scope_duplicate"):
        GapLedger([GapRecord.from_mapping(_record()), GapRecord.from_mapping(duplicate)], as_of_utc="2026-08-03T12:00:00Z")


def test_builder_registry_has_exactly_one_scope_entry() -> None:
    scopes = [(item.lead_bucket, item.model_family) for item in registered_builders()]
    assert len(scopes) == 9
    assert len(scopes) == len(set(scopes))
    assert builder_for("d1", "global_deterministic").state == "READY"
    blocked = build_scope(lead_bucket="d2", model_family="regional")
    assert blocked["state"] == "BLOCKED"
    assert blocked["writes_target_database"] is False


def test_gap_ledger_rejects_aliases_and_defaultable_fields() -> None:
    row = _record()
    row["lead"] = row.pop("lead_bucket")
    row["content_sha256"] = content_sha256(
        {key: value for key, value in row.items() if key != "content_sha256"}
    )
    with pytest.raises(ContractError, match="unknown_field|missing_field"):
        GapRecord.from_mapping(row)


def test_gap_ledger_rejects_noncanonical_family() -> None:
    row = _record()
    row["model_family"] = "AIFS"
    row["content_sha256"] = content_sha256(
        {key: value for key, value in row.items() if key != "content_sha256"}
    )
    with pytest.raises(ContractError, match="gap_model_family_invalid"):
        GapRecord.from_mapping(row)
