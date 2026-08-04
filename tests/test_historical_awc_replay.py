from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.historical_awc_replay import (
    AUTHORITY_SCOPE,
    SCHEMA,
    SOURCE_ROLE,
    build_historical_awc_replay_evidence,
)


def _raw(root: Path, *, raw_text: str, stamp: str, source_url: str | None = None) -> str:
    sha = hashlib.sha256(raw_text.encode()).hexdigest()
    value = {
        "station_id": "ZBAA",
        "source_lane": "awc",
        "source_poll_id": "awc-historical-raw",
        "source_url": source_url or "https://aviationweather.gov/api/data/metar?ids=ZBAA&format=json",
        "report_type": raw_text.split()[0],
        "valid_at_utc": stamp,
        "reported_temperature_c": 31.0,
        "raw_text": raw_text,
        "raw_sha256": sha,
    }
    path = root / "objects" / sha[:2] / f"{sha}.json.gz"
    path.parent.mkdir(parents=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)
    return sha


def _packet(*, raw_sha: str, stamp: str) -> dict:
    identity = {
        "schema": "bayes_basket_target_canonical_observation_evidence_v1",
        "target_timezone": "Asia/Shanghai",
        "historical_only": True,
        "canonical_ingress": True,
        "observations": [{
            "station_id": "ZBAA",
            "source_lane": "awc",
            "source_poll_id": "awc-historical-candidate",
            "report_type": "METAR",
            "valid_at_utc": stamp,
            "reported_temperature_c": 31.0,
            "raw_sha256": raw_sha,
        }],
        "boundary": {
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_arrival_evidence": False,
        },
    }
    return {**identity, "evidence_sha256": content_sha256(identity)}


def _build(tmp_path: Path, packet: dict) -> dict:
    return build_historical_awc_replay_evidence(
        packets=[packet],
        raw_root=tmp_path,
        station_id="ZBAA",
        settlement_timezone="Asia/Shanghai",
        start_date="2026-07-01",
        end_date="2026-07-01",
        as_of_utc="2026-07-03T00:00:00Z",
    )


def test_official_raw_becomes_explicit_historical_replay(tmp_path: Path) -> None:
    stamp = "2026-07-01T05:00:00Z"
    sha = _raw(tmp_path, raw_text="METAR ZBAA 010500Z 00000KT CAVOK 31/20 Q1000", stamp=stamp)
    result = _build(tmp_path, _packet(raw_sha=sha, stamp=stamp))

    assert result["schema"] == SCHEMA
    assert result["source_role"] == SOURCE_ROLE
    assert result["authority_scope"] == AUTHORITY_SCOPE
    assert result["observed_dates"] == ["2026-07-01"]
    assert result["truth_ready_dates"] == []
    assert result["truth_blocked_dates"] == ["2026-07-01"]
    assert result["observations"][0]["source_role"] == SOURCE_ROLE
    assert result["observations"][0]["reference_only"] is False
    assert result["boundary"]["writes_production_db"] is False


def test_nonzero_minute_metar_is_not_truth_but_speci_is_allowed(tmp_path: Path) -> None:
    metar_stamp = "2026-07-01T05:52:00Z"
    metar_sha = _raw(tmp_path, raw_text="METAR ZBAA 010552Z 00000KT CAVOK 31/20 Q1000", stamp=metar_stamp)
    packet = _packet(raw_sha=metar_sha, stamp=metar_stamp)
    result = _build(tmp_path, packet)
    assert result["observations"] == []
    assert result["truth_blocked_dates"] == ["2026-07-01"]
    assert result["rejection_counts"] == {
        "historical_awc_metar_nonexact_minute": 1
    }
    diagnostics = result["missing_date_diagnostics"][0]
    assert diagnostics["event_date"] == "2026-07-01"
    assert diagnostics["blockers"] == ["DATA_MISSING", "OOS_SAMPLE_SHORT"]
    assert {item["reason"] for item in diagnostics["diagnostics"]} == {
        "historical_awc_metar_nonexact_minute",
        "historical_awc_exact_hour_slots_missing",
    }

    speci_stamp = "2026-07-01T05:52:00Z"
    speci_sha = _raw(tmp_path, raw_text="SPECI ZBAA 010552Z 00000KT CAVOK 31/20 Q1000", stamp=speci_stamp)
    speci = _packet(raw_sha=speci_sha, stamp=speci_stamp)
    speci["observations"][0]["report_type"] = "SPECI"
    speci["evidence_sha256"] = content_sha256({key: value for key, value in speci.items() if key != "evidence_sha256"})
    assert len(_build(tmp_path, speci)["observations"]) == 1


def test_unofficial_url_and_tampered_evidence_fail_closed(tmp_path: Path) -> None:
    stamp = "2026-07-01T05:00:00Z"
    sha = _raw(
        tmp_path,
        raw_text="METAR ZBAA 010500Z 00000KT CAVOK 31/20 Q1000",
        stamp=stamp,
        source_url="https://example.invalid/api/data/metar?ids=ZBAA",
    )
    with pytest.raises(ContractError, match="official_source_url_invalid"):
        _build(tmp_path, _packet(raw_sha=sha, stamp=stamp))

    packet = _packet(raw_sha=sha, stamp=stamp)
    packet["observations"][0]["reported_temperature_c"] = 32.0
    with pytest.raises(ContractError, match="legacy_evidence_sha256_mismatch"):
        _build(tmp_path, packet)


def test_role_cannot_be_obtained_without_raw_object(tmp_path: Path) -> None:
    (tmp_path / "objects").mkdir()
    packet = _packet(raw_sha="a" * 64, stamp="2026-07-01T05:00:00Z")
    result = _build(tmp_path, packet)
    assert result["observations"] == []
    assert result["truth_blocked_dates"] == ["2026-07-01"]
    assert result["rejection_counts"] == {"historical_awc_raw_object_missing": 1}
    assert {item["reason"] for item in result["missing_date_diagnostics"][0]["diagnostics"]} == {
        "historical_awc_raw_object_missing",
        "historical_awc_exact_hour_slots_missing",
    }


def test_output_hash_detects_mutation(tmp_path: Path) -> None:
    stamp = "2026-07-01T05:00:00Z"
    sha = _raw(tmp_path, raw_text="METAR ZBAA 010500Z 00000KT CAVOK 31/20 Q1000", stamp=stamp)
    result = _build(tmp_path, _packet(raw_sha=sha, stamp=stamp))
    identity = {key: value for key, value in result.items() if key != "evidence_sha256"}
    assert result["evidence_sha256"] == content_sha256(identity)
    changed = deepcopy(result)
    changed["observations"][0]["reported_temperature_c"] = 30.0
    assert changed["evidence_sha256"] != content_sha256({key: value for key, value in changed.items() if key != "evidence_sha256"})
