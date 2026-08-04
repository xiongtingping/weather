from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.gap_ledger import build_gap_ledger
from bayes_basket_target.gap_ledger_pointer import (
    GapLedgerPointerError,
    SCHEMA,
    build_gap_ledger_pointer,
    replay_canonical_gap_ledger,
)


AS_OF = "2026-08-03T12:00:00Z"
INNER_SHA = "a" * 64


def _record() -> dict[str, object]:
    identity = {
        "station_id": "ZUUU",
        "model_id": "ECMWF",
        "model_family": "global_deterministic",
        "run_cycle": "PREVIOUS_DAY2",
        "lead_bucket": "d2",
        "event_date": "2026-08-02",
        "evidence_role": "historical_strict_oos",
        "native_arrival_verified": True,
        "raw_receipt_hash_complete": True,
        "awc_truth_closed": True,
        "taf_revision_complete": True,
        "train_count": 20,
        "holdout_count": 8,
        "state": "OOS_READY",
        "blockers": [],
        "evidence": ["model:" + "b" * 64, "truth:" + "c" * 64],
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _status(*, state: str = "BUILT", blockers: list[str] | None = None) -> dict[str, object]:
    identity = {
        "station_id": "ZUUU",
        "lead_bucket": "d2",
        "model_family": "global_deterministic",
        "builder_id": "d2.global_deterministic.historical_strict.v1",
        "state": state,
        "blockers": [] if blockers is None else blockers,
        "source_pointer": "/audit/builder.json",
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _write_ledger(
    tmp_path: Path,
    *,
    statuses: list[dict[str, object]] | None = None,
) -> tuple[Path, dict[str, object]]:
    ledger = build_gap_ledger(
        [_record()],
        as_of_utc=AS_OF,
        source_pointers={"builder": "/audit/builder.json"},
        builder_scope_statuses=[] if statuses is None else statuses,
    )
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(ledger, sort_keys=True) + "\n", encoding="utf-8")
    return path, ledger


def test_pointer_exactly_binds_a_replayed_ledger_without_activation(
    tmp_path: Path,
) -> None:
    path, ledger = _write_ledger(tmp_path, statuses=[_status()])

    pointer = build_gap_ledger_pointer(
        ledger_path=path,
        inner_oos_snapshot_content_sha256=INNER_SHA,
    )

    assert pointer["schema"] == SCHEMA
    assert pointer["state"] == "VERIFIED_NO_LIVE"
    assert pointer["blockers"] == []
    assert pointer["ledger_path"] == str(path.resolve())
    assert pointer["ledger_content_sha256"] == ledger["content_sha256"]
    assert pointer["ledger_file_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert pointer["record_count"] == 1
    assert pointer["effective_as_of_utc"] == AS_OF
    assert pointer["inner_oos_snapshot_content_sha256"] == INNER_SHA
    assert pointer["production_no_live"] is True
    assert pointer["calls_order_gateway"] is False
    assert pointer["orders_submitted"] == 0
    assert pointer["writes_production_db"] is False
    assert pointer["writes_target_database"] is False
    assert "consumable" not in pointer
    assert "activation_ready" not in pointer
    assert "ACTIVATION_READY" not in json.dumps(pointer, sort_keys=True)
    identity = {key: value for key, value in pointer.items() if key != "content_sha256"}
    assert pointer["content_sha256"] == content_sha256(identity)


def test_blocked_builder_scope_fails_closed_without_consumable_output(tmp_path: Path) -> None:
    path, _ = _write_ledger(
        tmp_path,
        statuses=[_status(state="BLOCKED", blockers=["BUILD_ERROR:timezone"])],
    )

    pointer = build_gap_ledger_pointer(
        ledger_path=path,
        inner_oos_snapshot_content_sha256=INNER_SHA,
    )

    assert pointer["state"] == "BLOCKED"
    assert pointer["blockers"] == [
        "BUILDER_SCOPE_BLOCKED:ZUUU|d2|global_deterministic"
    ]
    assert pointer["formal_p0_eligible"] is False
    assert pointer["formal_live_for_republisher"] is False
    assert "consumable" not in pointer
    assert "activation_ready" not in pointer


def test_pointer_rejects_ledger_content_tampering_even_when_json_is_valid(tmp_path: Path) -> None:
    path, ledger = _write_ledger(tmp_path, statuses=[_status()])
    ledger["records"][0]["state"] = "ACTIVATION_READY"
    path.write_text(json.dumps(ledger, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(
        GapLedgerPointerError,
        match="ledger_pointer_ledger_content_sha256_mismatch",
    ):
        build_gap_ledger_pointer(
            ledger_path=path,
            inner_oos_snapshot_content_sha256=INNER_SHA,
        )


def test_pointer_rejects_noncanonical_replay_even_with_recomputed_top_hash(tmp_path: Path) -> None:
    path, ledger = _write_ledger(tmp_path, statuses=[_status()])
    ledger["summary"]["rows"] = 99
    identity = {key: value for key, value in ledger.items() if key != "content_sha256"}
    ledger["content_sha256"] = content_sha256(identity)
    path.write_text(json.dumps(ledger, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(
        GapLedgerPointerError,
        match="ledger_pointer_canonical_replay_mismatch",
    ):
        build_gap_ledger_pointer(
            ledger_path=path,
            inner_oos_snapshot_content_sha256=INNER_SHA,
        )


def test_pointer_rejects_duplicate_record_scope_after_top_hash_is_recomputed(
    tmp_path: Path,
) -> None:
    path, ledger = _write_ledger(tmp_path, statuses=[_status()])
    ledger["records"].append(dict(ledger["records"][0]))
    ledger["summary"]["rows"] = 2
    identity = {key: value for key, value in ledger.items() if key != "content_sha256"}
    ledger["content_sha256"] = content_sha256(identity)
    path.write_text(json.dumps(ledger, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(
        GapLedgerPointerError,
        match="ledger_pointer_canonical_replay_failed:ContractError",
    ):
        build_gap_ledger_pointer(
            ledger_path=path,
            inner_oos_snapshot_content_sha256=INNER_SHA,
        )


def test_pointer_binds_exact_file_bytes_not_only_semantic_ledger_content(
    tmp_path: Path,
) -> None:
    path, ledger = _write_ledger(tmp_path, statuses=[_status()])
    first = build_gap_ledger_pointer(
        ledger_path=path,
        inner_oos_snapshot_content_sha256=INNER_SHA,
    )
    path.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    second = build_gap_ledger_pointer(
        ledger_path=path,
        inner_oos_snapshot_content_sha256=INNER_SHA,
    )

    assert first["ledger_content_sha256"] == second["ledger_content_sha256"]
    assert first["ledger_file_sha256"] != second["ledger_file_sha256"]
    assert first["content_sha256"] != second["content_sha256"]


@pytest.mark.parametrize(
    ("state", "blockers", "error"),
    [
        ("UNKNOWN", [], "ledger_pointer_builder_state_invalid"),
        ("BUILT", ["BUILD_ERROR:unexpected"], "ledger_pointer_built_status_has_blocker"),
        ("BLOCKED", [], "ledger_pointer_blocked_status_requires_one_blocker"),
        ("BLOCKED", ["A", "B"], "ledger_pointer_blocked_status_requires_one_blocker"),
    ],
)
def test_pointer_rejects_noncanonical_builder_status_semantics(
    tmp_path: Path, state: str, blockers: list[str], error: str,
) -> None:
    path, ledger = _write_ledger(
        tmp_path,
        statuses=[_status(state=state, blockers=blockers)],
    )
    # The source ledger's own content address must stay valid so this test
    # reaches pointer-level builder semantics rather than the hash fence.
    identity = {key: value for key, value in ledger.items() if key != "content_sha256"}
    ledger["content_sha256"] = content_sha256(identity)
    path.write_text(json.dumps(ledger, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(GapLedgerPointerError, match=error):
        build_gap_ledger_pointer(
            ledger_path=path,
            inner_oos_snapshot_content_sha256=INNER_SHA,
        )


def test_replay_requires_explicit_inner_snapshot_content_address(tmp_path: Path) -> None:
    path, ledger = _write_ledger(tmp_path, statuses=[_status()])
    assert replay_canonical_gap_ledger(ledger) == ledger

    with pytest.raises(
        GapLedgerPointerError,
        match="ledger_pointer_inner_oos_snapshot_content_sha256_invalid",
    ):
        build_gap_ledger_pointer(
            ledger_path=path,
            inner_oos_snapshot_content_sha256="not-a-sha",
        )
