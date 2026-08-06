from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.pr06_aifs_single_historical_terminal import (
    build_pr06_aifs_single_historical_terminal_bundle,
)


ROOT = Path(__file__).resolve().parents[1]
CAPTURE = ROOT / "output/audits/aifs_official_forward_capture_20260801_full45.json"
DECODE = ROOT / "output/audits/aifs_official_station_candidates_20260801.json"
AWC = ROOT / "output/audits/pr12_awc_observation_contract_v13_legacy_origin_bound_20260805T120000Z.json"
AS_OF = "2026-08-05T12:00:00.000Z"


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text())


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bundle() -> dict[str, object]:
    return build_pr06_aifs_single_historical_terminal_bundle(
        official_capture=_load(CAPTURE),
        official_decode=_load(DECODE),
        awc_contract=_load(AWC),
        as_of_utc=AS_OF,
        official_capture_file_sha256=_sha(CAPTURE),
    )


def test_actual_aifs_inputs_create_only_exact_historical_terminals() -> None:
    bundle = _bundle()
    assert bundle["state"] == "BLOCKED"
    assert bundle["fact_count"] == 0
    assert bundle["terminal_count"] == 39
    assert bundle["blocker_counts"] == {"DATA_MISSING": 18, "LINEAGE_MISMATCH": 21}
    assert bundle["diagnostic_counts"] == {
        "OFFICIAL_NATIVE_AIFS_ET_SLOT_MISSING": 18,
        "OFFICIAL_NATIVE_AIFS_RECEIPT_AFTER_D1_DECISION": 21,
    }
    terminals = bundle["terminals"]
    assert isinstance(terminals, list)
    late = [row for row in terminals if row["event_date"] == "2026-08-01"]
    missing = [row for row in terminals if row["event_date"] == "2026-08-02"]
    assert len(late) == 21 and len(missing) == 18
    assert all(row["model_id"] == "AIFS" for row in terminals)
    assert all(row["source_legacy_model_label"] == "ECMWF" for row in terminals)
    assert all(len(row["receipt_after_decision_vector"]) == 4 for row in late)
    assert all(row["missing_valid_time_utcs"] == [] for row in late)
    assert all(len(row["missing_valid_time_utcs"]) == 3 for row in missing)
    assert all(row["writes_database"] is False and row["consumable"] is False for row in terminals)


def test_decode_tampering_fails_before_any_terminal_is_returned() -> None:
    decode = deepcopy(_load(DECODE))
    decode["raw_objects"][0]["raw_message_sha256"] = "0" * 64  # type: ignore[index]
    with pytest.raises(ContractError, match="decode_content_sha256_mismatch"):
        build_pr06_aifs_single_historical_terminal_bundle(
            official_capture=_load(CAPTURE),
            official_decode=decode,
            awc_contract=_load(AWC),
            as_of_utc=AS_OF,
            official_capture_file_sha256=_sha(CAPTURE),
        )


def test_capture_file_hash_must_match_decode_capture_evidence() -> None:
    with pytest.raises(ContractError, match="decode_semantics_invalid"):
        build_pr06_aifs_single_historical_terminal_bundle(
            official_capture=_load(CAPTURE),
            official_decode=_load(DECODE),
            awc_contract=_load(AWC),
            as_of_utc=AS_OF,
            official_capture_file_sha256="0" * 64,
        )
