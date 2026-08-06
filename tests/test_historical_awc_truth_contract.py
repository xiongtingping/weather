from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time as wall_time, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

import pytest

from bayes_basket_target.canonical_contract import (
    CONTRACT_VERSION,
    SCHEMA,
    CanonicalContract,
)
from bayes_basket_target.canonical_ingress import ingest_canonical_contract
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.historical_awc_replay import (
    build_historical_awc_replay_evidence,
)
from bayes_basket_target.historical_awc_truth_contract import (
    INITIAL_REPLAY_REVISION,
    build_historical_awc_truth_contract,
)
from bayes_basket_target.ssot import apply_production_ssot_schema


def _legacy_packet(rows: list[dict]) -> dict:
    identity = {
        "schema": "bayes_basket_target_canonical_observation_evidence_v1",
        "historical_only": True,
        "canonical_ingress": True,
        "observations": rows,
    }
    return {**identity, "evidence_sha256": content_sha256(identity)}


def _raw(root: Path, *, raw_text: str, stamp: str, temperature: float) -> dict:
    raw_sha = hashlib.sha256(raw_text.encode()).hexdigest()
    raw = {
        "station_id": "ZBAA",
        "source_lane": "awc",
        "source_poll_id": "awc-historical-test",
        "source_url": "https://aviationweather.gov/api/data/metar?ids=ZBAA&format=json",
        "report_type": raw_text.split()[0],
        "valid_at_utc": stamp,
        "reported_temperature_c": temperature,
        "raw_text": raw_text,
        "raw_sha256": raw_sha,
    }
    path = root / "objects" / raw_sha[:2] / f"{raw_sha}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(raw, handle, sort_keys=True)
    return {
        "station_id": "ZBAA",
        "source_lane": "awc",
        "source_poll_id": "awc-historical-test",
        "report_type": raw["report_type"],
        "valid_at_utc": stamp,
        "reported_temperature_c": temperature,
        "raw_sha256": raw_sha,
    }


def _evidence(tmp_path: Path, *, include_second_day: bool = False) -> dict:
    rows: list[dict] = []
    days = [date(2026, 7, 1)] + ([date(2026, 7, 2)] if include_second_day else [])
    for local_day in days:
        start = datetime.combine(local_day, wall_time.min, tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc)
        for offset in range(24):
            stamp_dt = start + timedelta(hours=offset)
            stamp = stamp_dt.isoformat(timespec="seconds").replace("+00:00", "Z")
            rows.append(
                _raw(
                    tmp_path,
                    raw_text=f"METAR ZBAA {stamp_dt.strftime('%d%H%M')}Z 00000KT CAVOK 30/20 Q1000",
                    stamp=stamp,
                    temperature=30.0,
                )
            )
    rows.append(
        _raw(
            tmp_path,
            raw_text="SPECI ZBAA 010530Z 00000KT CAVOK 32/20 Q1000",
            stamp="2026-07-01T05:30:00Z",
            temperature=32.0,
        )
    )
    return build_historical_awc_replay_evidence(
        packets=[_legacy_packet(rows)],
        raw_root=tmp_path,
        station_id="ZBAA",
        settlement_timezone="Asia/Shanghai",
        start_date="2026-07-01",
        end_date="2026-07-02",
        as_of_utc="2026-08-04T00:00:00Z",
    )


def test_reverified_evidence_becomes_truth_only_canonical_contract(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    contract, receipt = build_historical_awc_truth_contract(
        [evidence], as_of_utc="2026-08-04T00:00:00Z"
    )
    parsed = CanonicalContract.from_mapping(contract)

    assert len(parsed.truth_facts) == 1
    truth = parsed.truth_facts[0]
    assert truth.truth_value == 32.0
    assert truth.truth_revision_no == INITIAL_REPLAY_REVISION
    assert truth.source_role == "AWC_HISTORICAL_REPLAY_INGRESS"
    assert truth.authority_scope == "PRODUCTION_NO_LIVE_ONLY"
    assert parsed.model_facts == ()
    assert receipt["blocked_station_count"] == 1
    assert receipt["stations"][0]["truth_blocked_dates"] == ["2026-07-02"]
    assert receipt["stations"][0]["blockers"] == ["DATA_MISSING", "OOS_SAMPLE_SHORT"]


def test_truth_bundle_validates_and_ingresses_atomically(tmp_path: Path) -> None:
    contract, _receipt = build_historical_awc_truth_contract(
        [_evidence(tmp_path, include_second_day=True)],
        as_of_utc="2026-08-04T00:00:00Z",
    )
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        preview = ingest_canonical_contract(conn, contract)
        assert preview["state"] == "VALIDATED_NO_WRITE"
        assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 0
        committed = ingest_canonical_contract(conn, contract, commit=True)
        assert committed["inserted_count"] == 2
        assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 2


def test_role_cannot_create_second_truth_scope_for_same_revision(tmp_path: Path) -> None:
    contract, _receipt = build_historical_awc_truth_contract(
        [_evidence(tmp_path, include_second_day=True)],
        as_of_utc="2026-08-04T00:00:00Z",
    )
    historical = contract["truth_facts"][0]
    formal = deepcopy(historical)
    formal["truth_id"] = "formal-" + formal["truth_id"]
    formal["source_role"] = "CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1"
    formal["authority_scope"] = "FORMAL_NATIVE_ELIGIBLE"
    formal["truth_content_sha256"] = "f" * 64
    formal["content_sha256"] = content_sha256(
        {key: value for key, value in formal.items() if key != "content_sha256"}
    )
    formal_contract_identity = {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "as_of_utc": "2026-08-04T00:00:00Z",
        "builder_id": "formal.truth.test",
        "model_facts": [],
        "ensemble_member_facts": [],
        "native_arrival_facts": [],
        "ensemble_member_receipt_manifest_facts": [],
        "ensemble_member_arrival_binding_facts": [],
        "truth_facts": [formal],
        "residual_facts": [],
        "activation_facts": [],
    }
    formal_contract = {
        **formal_contract_identity,
        "content_sha256": content_sha256(formal_contract_identity),
    }
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        ingest_canonical_contract(conn, contract, commit=True)
        with pytest.raises(ContractError, match="canonical_scope_content_conflict"):
            ingest_canonical_contract(conn, formal_contract, commit=True)
        assert conn.execute(
            "SELECT COUNT(*) FROM canonical_ssot_fact WHERE fact_type='truth'"
        ).fetchone()[0] == 2


def test_tampered_or_hour_coverage_inconsistent_evidence_cannot_form_truth(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    tampered = deepcopy(evidence)
    tampered["observations"][0]["reported_temperature_c"] = 99.0
    with pytest.raises(ContractError, match="evidence_sha256_mismatch"):
        build_historical_awc_truth_contract(
            [tampered], as_of_utc="2026-08-04T00:00:00Z"
        )

    nonexact = deepcopy(evidence)
    row = nonexact["observations"][0]
    row["valid_at_utc"] = "2026-07-01T05:52:00Z"
    row_identity = {key: value for key, value in row.items() if key != "content_sha256"}
    row["content_sha256"] = content_sha256(row_identity)
    evidence_identity = {key: value for key, value in nonexact.items() if key != "evidence_sha256"}
    nonexact["evidence_sha256"] = content_sha256(evidence_identity)
    with pytest.raises(ContractError, match="completeness_observation_slots_mismatch"):
        build_historical_awc_truth_contract(
            [nonexact], as_of_utc="2026-08-04T00:00:00Z"
        )
