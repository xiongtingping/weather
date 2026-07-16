from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from resolution_observed_x_s_pair_shadow import (
    PAIR_TABLE,
    build_observed_x_s_pair,
    initialize_isolated_resolution_observed_x_s_pair_schema,
    persist_observed_x_s_pair_shadow,
)
from resolution_observed_x_s_pair_capture_runtime import run_resolution_observed_x_s_pair_capture_cycle
from market_resolution_finality_shadow_writer import FINALITY_TABLE, initialize_isolated_market_resolution_finality_schema
from resolution_final_source_shadow_writer import SOURCE_TABLE, initialize_isolated_resolution_final_source_staging_schema
from resolution_target_contract_shadow_writer import TARGET_TABLE, initialize_isolated_resolution_target_contract_schema
from sequential_bayes_public_ssot_shadow_writer import initialize_isolated_public_ssot_shadow_schema
from sequential_bayes_shadow_writer import ShadowTransactionIntegrityError


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source(*, source_id: str, role: str, scalar_key: str, value: float) -> dict:
    return {
        "source_observation_id": f"{source_id}-20260703",
        "source_id": source_id,
        "source_class": "settlement_source",
        "role": role,
        "station_id": "ZUUU",
        "event_date": "2026-07-03",
        "fetched_at": "2026-07-03T19:00:00Z",
        "raw_ref": f"raw://{source_id}",
        "raw_sha256": _sha(source_id),
        "payload": {scalar_key: value, "label_finality": "final"},
    }


def _pair() -> dict:
    return build_observed_x_s_pair(
        target_contract={
            "target_contract_sha256": _sha("contract"),
            "target_identity": "market_resolution_target_v1:highest-temperature-in-chengdu-on-july-3-2026",
            "resolution_local_date": "2026-07-03",
            "authority_contract_id": "zuuu_amsc_wunderground_daily_high_v4",
            "authority_contract_sha256": _sha("authority-v4"),
            "resolution_source_id": "wunderground_zuuu_historical_observation_daily_high_final_v3",
            "bridge_closed": False,
            "validated": False,
        },
        market_finality_attestation={
            "ok": True,
            "market_resolution_final": True,
            "target_contract_sha256": _sha("contract"),
            "target_identity": "market_resolution_target_v1:highest-temperature-in-chengdu-on-july-3-2026",
            "event_date": "2026-07-03",
            "market_finality_raw_sha256": _sha("finality"),
            "market_finality_raw_ref": "raw://finality",
            "resolution_state_observed_at": "2026-07-03T19:00:00Z",
        },
        physical_final_source=_source(
            source_id="amsc_zuuu_scheduled_metar_daily_max_v2",
            role="physical_daily_high_final_truth",
            scalar_key="final_physical_high_c",
            value=30.0,
        ),
        settlement_final_source=_source(
            source_id="wunderground_zuuu_historical_observation_daily_high_final_v3",
            role="resolution_settlement_final",
            scalar_key="settlement_scalar_c",
            value=31.0,
        ),
        captured_at="2026-07-03T19:01:00Z",
    )


def test_raw_x_s_pair_is_append_only_and_not_a_bridge_or_bucket_label() -> None:
    pair = _pair()
    assert pair["ok"] is True
    assert pair["bridge_closed"] is False
    assert pair["market_settlement_bucket"] is None
    assert pair["market_settlement_bucket_na_reason"]

    conn = sqlite3.connect(":memory:")
    initialize_isolated_public_ssot_shadow_schema(conn)
    initialize_isolated_resolution_observed_x_s_pair_schema(conn)
    first = persist_observed_x_s_pair_shadow(conn, pair)
    second = persist_observed_x_s_pair_shadow(conn, pair)

    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert conn.execute(f"SELECT COUNT(*) FROM {PAIR_TABLE}").fetchone()[0] == 1


def test_pair_rejects_hash_drift_and_missing_isolation_sentinel() -> None:
    bad_contract = _pair()
    bad_contract["target_contract_sha256"] = _sha("tampered")

    conn = sqlite3.connect(":memory:")
    initialize_isolated_public_ssot_shadow_schema(conn)
    initialize_isolated_resolution_observed_x_s_pair_schema(conn)
    with pytest.raises(ValueError, match="sha256_invalid"):
        persist_observed_x_s_pair_shadow(conn, bad_contract)
    with pytest.raises(ShadowTransactionIntegrityError, match="manifest_missing"):
        initialize_isolated_resolution_observed_x_s_pair_schema(sqlite3.connect(":memory:"))


def test_capture_cycle_appends_pair_only_after_the_two_final_sources_and_finality(tmp_path: Path) -> None:
    db = tmp_path / "pairs.db"
    conn = sqlite3.connect(db)
    initialize_isolated_public_ssot_shadow_schema(conn)
    initialize_isolated_resolution_target_contract_schema(conn)
    initialize_isolated_market_resolution_finality_schema(conn)
    initialize_isolated_resolution_final_source_staging_schema(conn)
    initialize_isolated_resolution_observed_x_s_pair_schema(conn)
    contract_sha = _sha("contract")
    target = {
        "target_contract_sha256": contract_sha,
        "target_identity": "market_resolution_target_v1:highest-temperature-in-chengdu-on-july-3-2026",
        "resolution_local_date": "2026-07-03",
        "authority_contract_id": "zuuu_amsc_wunderground_daily_high_v4",
        "authority_contract_sha256": _sha("authority-v4"),
        "resolution_source_id": "wunderground_zuuu_historical_observation_daily_high_final_v3",
        "bridge_closed": False,
        "validated": False,
    }
    finality = {
        "ok": True,
        "market_resolution_final": True,
        "target_contract_sha256": contract_sha,
        "target_identity": target["target_identity"],
        "event_date": "2026-07-03",
        "market_finality_raw_sha256": _sha("finality"),
        "market_finality_raw_ref": "raw://finality",
        "resolution_state_observed_at": "2026-07-03T19:00:00Z",
    }
    conn.execute(
        f"INSERT INTO {TARGET_TABLE}(target_contract_id,target_identity,resolution_local_date,target_contract_sha256,supersedes_target_contract_sha256,revision_no,contract_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
        ("target-contract", target["target_identity"], "2026-07-03", contract_sha, None, 0, json.dumps(target), "2026-07-03T19:00:00Z"),
    )
    conn.execute(
        f"INSERT INTO {FINALITY_TABLE}(target_contract_sha256,target_identity,event_date,market_finality_raw_sha256,market_finality_raw_ref,attestation_json,observed_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (contract_sha, target["target_identity"], "2026-07-03", _sha("finality"), "raw://finality", json.dumps(finality), "2026-07-03T19:00:00Z", "2026-07-03T19:00:00Z"),
    )
    for source in (
        _source(source_id="amsc_zuuu_scheduled_metar_daily_max_v2", role="physical_daily_high_final_truth", scalar_key="final_physical_high_c", value=30.0),
        _source(source_id="wunderground_zuuu_historical_observation_daily_high_final_v3", role="resolution_settlement_final", scalar_key="settlement_scalar_c", value=31.0),
    ):
        conn.execute(
            f"INSERT INTO {SOURCE_TABLE}(source_observation_id,source_id,role,station_id,event_date,raw_ref,raw_sha256,observation_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (source["source_observation_id"], source["source_id"], source["role"], "ZUUU", "2026-07-03", source["raw_ref"], source["raw_sha256"], json.dumps(source), source["fetched_at"]),
        )
    conn.commit()
    conn.close()

    result = run_resolution_observed_x_s_pair_capture_cycle(
        shadow_db_path=db, captured_at="2026-07-03T19:01:00Z"
    )

    assert result["state"] == "observed_x_s_pair_appended"
    with sqlite3.connect(db) as check:
        assert check.execute(f"SELECT COUNT(*) FROM {PAIR_TABLE}").fetchone()[0] == 1
