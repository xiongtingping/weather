from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from market_rule_raw_archive import archive_market_rule_raw_snapshot
from resolution_final_source_shadow_writer import initialize_isolated_resolution_final_source_staging_schema
from resolution_label_shadow_writer import initialize_isolated_resolution_label_staging_schema
from resolution_target_capture_cursor import initialize_resolution_target_capture_cursor
from resolution_target_contract_shadow_writer import (
    TARGET_TABLE,
    initialize_isolated_resolution_target_contract_schema,
    load_latest_resolution_target_contract_shadow,
)
from resolution_authority_contract_runtime import validate_resolution_authority_contract
from sequential_bayes_public_ssot_shadow_writer import initialize_isolated_public_ssot_shadow_schema
from target_rule_shadow_capture_runtime import run_target_rule_shadow_capture_cycle


ROOT = Path(__file__).resolve().parents[1]
AUTHORITY = json.loads((ROOT / "config/resolution_authorities/zuuu_amsc_wunderground_daily_high_v4.json").read_text())
SLUG = "highest-temperature-in-chengdu-on-july-13-2026"
DAY = "2026-07-13"


def _raw() -> bytes:
    return json.dumps(
        [
            {
                "slug": SLUG,
                "eventDate": DAY,
                "resolutionSource": "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
                "markets": [
                    {"groupItemTitle": "30°C or below", "outcomes": ["Yes", "No"], "outcomePrices": [0.1, 0.9]},
                    {"groupItemTitle": "31°C or higher", "outcomes": ["Yes", "No"], "outcomePrices": [0.9, 0.1]},
                ],
            }
        ]
    ).encode("utf-8")


def _archive_db(path: Path, *, first_seen_at: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE weather_market_discovery(slug TEXT,event_date TEXT,first_seen_at TEXT)")
        conn.execute("INSERT INTO weather_market_discovery VALUES(?,?,?)", (SLUG, DAY, first_seen_at))


def _shadow_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        initialize_isolated_public_ssot_shadow_schema(conn)
        initialize_resolution_target_capture_cursor(conn, forward_capture_not_before="2026-07-12T05:00:00Z")
        initialize_isolated_resolution_target_contract_schema(conn)
        initialize_isolated_resolution_final_source_staging_schema(conn)
        initialize_isolated_resolution_label_staging_schema(conn)


def test_forward_target_capture_uses_new_discovery_identity_and_writes_only_shadow(tmp_path) -> None:
    archive_db = tmp_path / "archive.db"
    shadow_db = tmp_path / "shadow.db"
    raw_dir = tmp_path / "raw"
    _archive_db(archive_db, first_seen_at="2026-07-12T05:01:00Z")
    _shadow_db(shadow_db)

    def fetcher(**kwargs):
        return archive_market_rule_raw_snapshot(
            **kwargs,
            source_url="https://gamma-api.polymarket.com/events?slug=" + SLUG,
            raw_payload=_raw(),
            requested_at="2026-07-12T05:01:01Z",
            response_started_at="2026-07-12T05:01:02Z",
            response_completed_at="2026-07-12T05:01:03Z",
            response_status=200,
        )

    result = run_target_rule_shadow_capture_cycle(
        archive_db_path=archive_db,
        shadow_db_path=shadow_db,
        market_rule_archive_dir=raw_dir,
        authority_contract=AUTHORITY,
        captured_at="2026-07-12T05:01:04Z",
        fetcher=fetcher,
    )
    assert result["ok"] is True
    assert result["outcomes"][0]["state"] == "appended"
    with sqlite3.connect(shadow_db) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {TARGET_TABLE}").fetchone()[0] == 1


def test_target_capture_does_not_backfill_pre_watermark_discovery(tmp_path) -> None:
    archive_db = tmp_path / "archive.db"
    shadow_db = tmp_path / "shadow.db"
    _archive_db(archive_db, first_seen_at="2026-07-12T04:59:59Z")
    _shadow_db(shadow_db)
    result = run_target_rule_shadow_capture_cycle(
        archive_db_path=archive_db,
        shadow_db_path=shadow_db,
        market_rule_archive_dir=tmp_path / "raw",
        authority_contract=AUTHORITY,
        captured_at="2026-07-12T05:01:04Z",
    )
    assert result["ok"] is True
    assert result["state"] == "no_forward_targets"


def test_target_capture_fails_closed_when_production_discovery_surface_is_missing(tmp_path) -> None:
    shadow_db = tmp_path / "shadow.db"
    _shadow_db(shadow_db)
    result = run_target_rule_shadow_capture_cycle(
        archive_db_path=tmp_path / "missing-archive.db",
        shadow_db_path=shadow_db,
        market_rule_archive_dir=tmp_path / "raw",
        authority_contract=AUTHORITY,
        captured_at="2026-07-12T05:01:04Z",
    )
    assert result["ok"] is False
    assert result["block_reasons"] == ["target_rule_shadow_capture_preflight_failed:OperationalError"]


def test_target_capture_appends_authority_successor_instead_of_skipping_legacy_revision(tmp_path) -> None:
    archive_db = tmp_path / "archive.db"
    shadow_db = tmp_path / "shadow.db"
    raw_dir = tmp_path / "raw"
    _archive_db(archive_db, first_seen_at="2026-07-12T05:01:00Z")
    _shadow_db(shadow_db)
    target_identity = f"market_resolution_target_v1:{SLUG}"
    legacy_sha = "a" * 64
    legacy = {
        "target_contract_id": "legacy-v2-target-contract",
        "target_contract_sha256": legacy_sha,
        "target_identity": target_identity,
        "resolution_local_date": DAY,
        "authority_contract_sha256": "b" * 64,
    }
    with sqlite3.connect(shadow_db) as conn:
        conn.execute(
            f"""INSERT INTO {TARGET_TABLE}(
                target_contract_id,target_identity,resolution_local_date,target_contract_sha256,
                supersedes_target_contract_sha256,revision_no,contract_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            ("legacy-v2-target-contract", target_identity, DAY, legacy_sha, None, 0, json.dumps(legacy), "2026-07-12T05:01:00Z"),
        )

    def fetcher(**kwargs):
        return archive_market_rule_raw_snapshot(
            **kwargs,
            source_url="https://gamma-api.polymarket.com/events?slug=" + SLUG,
            raw_payload=_raw(),
            requested_at="2026-07-12T05:02:01Z",
            response_started_at="2026-07-12T05:02:02Z",
            response_completed_at="2026-07-12T05:02:03Z",
            response_status=200,
        )

    result = run_target_rule_shadow_capture_cycle(
        archive_db_path=archive_db,
        shadow_db_path=shadow_db,
        market_rule_archive_dir=raw_dir,
        authority_contract=AUTHORITY,
        captured_at="2026-07-12T05:02:04Z",
        fetcher=fetcher,
    )
    assert result["ok"] is True
    assert result["outcomes"][0]["state"] == "appended_successor"
    assert result["outcomes"][0]["supersedes_target_contract_sha256"] == legacy_sha
    with sqlite3.connect(shadow_db) as conn:
        latest = load_latest_resolution_target_contract_shadow(
            conn, target_identity=target_identity, resolution_local_date=DAY
        )
        assert conn.execute(f"SELECT COUNT(*) FROM {TARGET_TABLE}").fetchone()[0] == 2
    assert latest is not None
    assert latest["supersedes_target_contract_sha256"] == legacy_sha
    assert latest["authority_contract_sha256"] == validate_resolution_authority_contract(AUTHORITY)["contract_sha256"]
