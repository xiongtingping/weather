from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from market_resolution_finality_shadow_writer import (
    initialize_isolated_market_resolution_finality_schema,
    persist_market_resolution_finality_attestation_shadow,
)
from market_rule_target_contract_parser import parse_market_rule_target_contract_extraction
from resolution_final_source_shadow_writer import (
    SOURCE_TABLE,
    initialize_isolated_resolution_final_source_staging_schema,
)
from resolution_target_capture_cursor import initialize_resolution_target_capture_cursor
from resolution_target_contract_runtime import build_market_resolution_target_contract_proposal
from resolution_target_contract_shadow_writer import (
    initialize_isolated_resolution_target_contract_schema,
    persist_resolution_target_contract_shadow,
)
from sequential_bayes_public_ssot_shadow_writer import initialize_isolated_public_ssot_shadow_schema
from settlement_final_source_shadow_capture_runtime import run_settlement_final_source_shadow_capture_cycle
from wunderground_final_raw_archive import archive_wunderground_daily_raw_capture


ROOT = Path(__file__).resolve().parents[1]
AUTHORITY = json.loads((ROOT / "config/resolution_authorities/zuuu_amsc_wunderground_daily_high_v4.json").read_text())
SLUG = "highest-temperature-in-chengdu-on-july-3-2026"
DAY = "2026-07-03"
TARGET = f"market_resolution_target_v1:{SLUG}"
SOURCE = "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU"


def _target_raw() -> bytes:
    return json.dumps(
        [{"slug": SLUG, "eventDate": DAY, "resolutionSource": SOURCE, "markets": [{"groupItemTitle": "31°C"}]}]
    ).encode("utf-8")


def _setup(path: Path) -> dict:
    raw = _target_raw()
    parsed = parse_market_rule_target_contract_extraction(raw, public_slug=SLUG, resolution_local_date=DAY)
    target = build_market_resolution_target_contract_proposal(
        authority_contract=AUTHORITY,
        target_identity=TARGET,
        public_slug=SLUG,
        resolution_local_date=DAY,
        station_id="ZUUU",
        resolution_source=SOURCE,
        market_rule_raw_payload=raw,
        market_rule_raw_ref="market-rules/initial.json",
        market_rule_raw_sha256=hashlib.sha256(raw).hexdigest(),
        market_rule_extraction=parsed["extraction"],
    )
    attestation = {
        "schema": "market_resolution_finality_attestation_v1",
        "ok": True,
        "state": "finality_attested",
        "market_resolution_final": True,
        "target_identity": TARGET,
        "public_slug": SLUG,
        "event_date": DAY,
        "station_id": "ZUUU",
        "resolution_source": SOURCE,
        "resolved_at": "2026-07-03T18:00:00Z",
        "attested_at": "2026-07-03T18:00:00Z",
        "resolution_state_observed_at": "2026-07-03T18:00:00Z",
        "market_rule_snapshot_sha256": hashlib.sha256(raw).hexdigest(),
        "market_rule_raw_ref": "market-rules/initial.json",
        "target_contract_id": target["target_contract_id"],
        "target_contract_sha256": target["target_contract_sha256"],
        "market_finality_raw_ref": "market-finality/final.json",
        "market_finality_raw_sha256": "f" * 64,
        "boundary": {"does_not_read_market_outcome": True, "does_not_read_market_price": True},
    }
    with sqlite3.connect(path) as conn:
        initialize_isolated_public_ssot_shadow_schema(conn)
        initialize_resolution_target_capture_cursor(conn, forward_capture_not_before="2026-07-01T00:00:00Z")
        initialize_isolated_resolution_target_contract_schema(conn)
        initialize_isolated_market_resolution_finality_schema(conn)
        initialize_isolated_resolution_final_source_staging_schema(conn)
        persist_resolution_target_contract_shadow(conn, target, created_at="2026-07-03T18:00:00Z")
        persist_market_resolution_finality_attestation_shadow(conn, attestation, created_at="2026-07-03T18:00:00Z")
    return target


def test_settlement_sidecar_requires_finality_and_appends_only_shadow_s(tmp_path) -> None:
    shadow_db = tmp_path / "shadow.db"
    wu_archive = tmp_path / "wu"
    _setup(shadow_db)

    def fetcher(**kwargs):
        kwargs.pop("authority_contract")
        raw = b'{"observations":[{"valid_time_gmt":1783054800,"temp":30},{"valid_time_gmt":1783058400,"temp":31}]}'
        return archive_wunderground_daily_raw_capture(
            **kwargs,
            source_url="https://api.weather.com/v1/location/ZUUU%3A9%3ACN/observations/historical.json?apiKey=REDACTED&units=m&startDate=20260703&endDate=20260703",
            raw_payload=raw,
            requested_at="2026-07-03T18:01:00Z",
            response_started_at="2026-07-03T18:01:01Z",
            response_completed_at="2026-07-03T18:01:02Z",
            response_status=200,
        )

    result = run_settlement_final_source_shadow_capture_cycle(
        shadow_db_path=shadow_db,
        wunderground_raw_archive_dir=wu_archive,
        authority_contract=AUTHORITY,
        captured_at="2026-07-03T18:01:03Z",
        fetcher=fetcher,
    )
    assert result["ok"] is True
    assert result["outcomes"][0]["state"] == "appended_settlement_source"
    with sqlite3.connect(shadow_db) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {SOURCE_TABLE}").fetchone()[0] == 1


def test_settlement_sidecar_never_fetches_or_creates_s_without_finality_receipt(tmp_path) -> None:
    shadow_db = tmp_path / "shadow.db"
    with sqlite3.connect(shadow_db) as conn:
        initialize_isolated_public_ssot_shadow_schema(conn)
        initialize_resolution_target_capture_cursor(conn, forward_capture_not_before="2026-07-01T00:00:00Z")
        initialize_isolated_resolution_target_contract_schema(conn)
        initialize_isolated_market_resolution_finality_schema(conn)
        initialize_isolated_resolution_final_source_staging_schema(conn)

    def fetcher(**_kwargs):
        raise AssertionError("Wunderground must not be fetched without finality")

    result = run_settlement_final_source_shadow_capture_cycle(
        shadow_db_path=shadow_db,
        wunderground_raw_archive_dir=tmp_path / "wu",
        authority_contract=AUTHORITY,
        captured_at="2026-07-03T18:01:03Z",
        fetcher=fetcher,
    )
    assert result["ok"] is True
    assert result["targets_considered"] == 0


def test_settlement_sidecar_skips_finality_bound_to_superseded_target_contract(tmp_path, monkeypatch) -> None:
    shadow_db = tmp_path / "shadow.db"
    old_target = _setup(shadow_db)
    latest_target = {
        **old_target,
        "target_contract_sha256": "a" * 64,
    }
    monkeypatch.setattr(
        "settlement_final_source_shadow_capture_runtime.list_latest_resolution_target_contracts_shadow",
        lambda _conn: [latest_target],
    )

    def fetcher(**_kwargs):
        raise AssertionError("superseded finality must not fetch settlement S")

    result = run_settlement_final_source_shadow_capture_cycle(
        shadow_db_path=shadow_db,
        wunderground_raw_archive_dir=tmp_path / "wu",
        authority_contract=AUTHORITY,
        captured_at="2026-07-03T18:01:03Z",
        fetcher=fetcher,
    )
    assert result["ok"] is True
    assert result["outcomes"][0]["state"] == "skipped_superseded_target_contract_finality"
    assert result["outcomes"][0]["latest_target_contract_sha256"] == "a" * 64
