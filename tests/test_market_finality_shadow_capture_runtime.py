from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from urllib.error import URLError

from market_finality_shadow_capture_runtime import run_market_finality_shadow_capture_cycle
from market_resolution_finality_shadow_writer import (
    FINALITY_TABLE,
    initialize_isolated_market_resolution_finality_schema,
)
from market_rule_raw_archive import archive_market_rule_raw_snapshot, load_market_rule_raw_snapshot
from market_rule_target_contract_parser import parse_market_rule_target_contract_extraction
from resolution_target_capture_cursor import initialize_resolution_target_capture_cursor
from resolution_target_contract_runtime import build_market_resolution_target_contract_proposal
from resolution_target_contract_shadow_writer import (
    initialize_isolated_resolution_target_contract_schema,
    persist_resolution_target_contract_shadow,
)
from sequential_bayes_public_ssot_shadow_writer import initialize_isolated_public_ssot_shadow_schema


ROOT = Path(__file__).resolve().parents[1]
AUTHORITY = json.loads((ROOT / "config/resolution_authorities/zuuu_amsc_wunderground_daily_high_v4.json").read_text())
SLUG = "highest-temperature-in-chengdu-on-july-13-2026"
DAY = "2026-07-13"
TARGET = f"market_resolution_target_v1:{SLUG}"
SOURCE = "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU"


def _raw(*, status: str, closed: bool) -> bytes:
    return json.dumps(
        [
            {
                "slug": SLUG,
                "eventDate": DAY,
                "closed": closed,
                "resolutionSource": SOURCE,
                "markets": [
                    {"groupItemTitle": "30°C or below", "umaResolutionStatus": status, "outcomes": ["Yes", "No"], "outcomePrices": [0.1, 0.9]},
                    {"groupItemTitle": "31°C or higher", "umaResolutionStatus": status, "outcomes": ["Yes", "No"], "outcomePrices": [0.9, 0.1]},
                ],
            }
        ]
    ).encode("utf-8")


def _initialize_target(path: Path, raw_dir: Path) -> None:
    initial = _raw(status="pending", closed=False)
    archived = archive_market_rule_raw_snapshot(
        archive_dir=raw_dir,
        target_identity=TARGET,
        public_slug=SLUG,
        resolution_local_date=DAY,
        source_url=f"https://gamma-api.polymarket.com/events?slug={SLUG}",
        raw_payload=initial,
        requested_at="2026-07-12T06:00:00Z",
        response_started_at="2026-07-12T06:00:01Z",
        response_completed_at="2026-07-12T06:00:02Z",
        response_status=200,
    )
    capture = load_market_rule_raw_snapshot(archive_dir=raw_dir, manifest_ref=archived["manifest_ref"])
    parsed = parse_market_rule_target_contract_extraction(initial, public_slug=SLUG, resolution_local_date=DAY)
    target = build_market_resolution_target_contract_proposal(
        authority_contract=AUTHORITY,
        target_identity=TARGET,
        public_slug=SLUG,
        resolution_local_date=DAY,
        station_id="ZUUU",
        resolution_source=SOURCE,
        market_rule_raw_payload=initial,
        market_rule_raw_ref=capture["raw_ref"],
        market_rule_raw_sha256=capture["raw_sha256"],
        market_rule_extraction=parsed["extraction"],
    )
    with sqlite3.connect(path) as conn:
        initialize_isolated_public_ssot_shadow_schema(conn)
        initialize_resolution_target_capture_cursor(conn, forward_capture_not_before="2026-07-12T05:00:00Z")
        initialize_isolated_resolution_target_contract_schema(conn)
        initialize_isolated_market_resolution_finality_schema(conn)
        persist_resolution_target_contract_shadow(conn, target, created_at="2026-07-12T06:00:03Z")


def test_finality_sidecar_attests_only_after_raw_all_resolved_state(tmp_path) -> None:
    shadow_db = tmp_path / "shadow.db"
    raw_dir = tmp_path / "raw"
    _initialize_target(shadow_db, raw_dir)

    def fetcher(**kwargs):
        return archive_market_rule_raw_snapshot(
            **kwargs,
            source_url=f"https://gamma-api.polymarket.com/events?slug={SLUG}",
            raw_payload=_raw(status="resolved", closed=True),
            requested_at="2026-07-14T01:00:00Z",
            response_started_at="2026-07-14T01:00:01Z",
            response_completed_at="2026-07-14T01:00:02Z",
            response_status=200,
        )

    result = run_market_finality_shadow_capture_cycle(
        shadow_db_path=shadow_db,
        market_rule_archive_dir=raw_dir,
        authority_contract=AUTHORITY,
        captured_at="2026-07-14T01:00:03Z",
        fetcher=fetcher,
    )
    assert result["ok"] is True
    assert result["outcomes"][0]["state"] == "attested"
    with sqlite3.connect(shadow_db) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {FINALITY_TABLE}").fetchone()[0] == 1


def test_finality_sidecar_treats_unresolved_status_as_pending_not_label_truth(tmp_path) -> None:
    shadow_db = tmp_path / "shadow.db"
    raw_dir = tmp_path / "raw"
    _initialize_target(shadow_db, raw_dir)

    def fetcher(**kwargs):
        return archive_market_rule_raw_snapshot(
            **kwargs,
            source_url=f"https://gamma-api.polymarket.com/events?slug={SLUG}",
            raw_payload=_raw(status="pending", closed=False),
            requested_at="2026-07-14T01:00:00Z",
            response_started_at="2026-07-14T01:00:01Z",
            response_completed_at="2026-07-14T01:00:02Z",
            response_status=200,
        )

    result = run_market_finality_shadow_capture_cycle(
        shadow_db_path=shadow_db,
        market_rule_archive_dir=raw_dir,
        authority_contract=AUTHORITY,
        captured_at="2026-07-14T01:00:03Z",
        fetcher=fetcher,
    )
    assert result["ok"] is True
    assert result["outcomes"][0]["state"] == "pending"
    with sqlite3.connect(shadow_db) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {FINALITY_TABLE}").fetchone()[0] == 0


def test_finality_sidecar_defers_until_zuuu_local_day_has_ended(tmp_path) -> None:
    shadow_db = tmp_path / "shadow.db"
    raw_dir = tmp_path / "raw"
    _initialize_target(shadow_db, raw_dir)
    calls = []

    result = run_market_finality_shadow_capture_cycle(
        shadow_db_path=shadow_db,
        market_rule_archive_dir=raw_dir,
        authority_contract=AUTHORITY,
        captured_at="2026-07-13T07:00:00Z",  # 15:00 Asia/Shanghai on DAY
        fetcher=lambda **kwargs: calls.append(kwargs) or {},
    )

    assert result["ok"] is True
    assert result["targets_polled"] == 0
    assert result["outcomes"][0]["state"] == "deferred_event_day_open"
    assert result["outcomes"][0]["finality_poll_not_before"] == "2026-07-14T00:00:00+08:00"
    assert calls == []


def test_finality_sidecar_retries_network_error_without_blocking_live_or_registry(tmp_path) -> None:
    shadow_db = tmp_path / "shadow.db"
    raw_dir = tmp_path / "raw"
    _initialize_target(shadow_db, raw_dir)

    def fetcher(**_kwargs):
        raise URLError("temporary")

    result = run_market_finality_shadow_capture_cycle(
        shadow_db_path=shadow_db,
        market_rule_archive_dir=raw_dir,
        authority_contract=AUTHORITY,
        captured_at="2026-07-14T01:00:03Z",
        fetcher=fetcher,
    )

    assert result["ok"] is True
    assert result["retryable_capture_error_count"] == 1
    assert result["outcomes"][0]["state"] == "retryable_capture_error"
    with sqlite3.connect(shadow_db) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {FINALITY_TABLE}").fetchone()[0] == 0


def test_finality_sidecar_does_not_downgrade_structural_parser_error_to_pending(tmp_path) -> None:
    shadow_db = tmp_path / "shadow.db"
    raw_dir = tmp_path / "raw"
    _initialize_target(shadow_db, raw_dir)

    def fetcher(**kwargs):
        return archive_market_rule_raw_snapshot(
            **kwargs,
            source_url=f"https://gamma-api.polymarket.com/events?slug={SLUG}",
            raw_payload=b"not-json",
            requested_at="2026-07-14T01:00:00Z",
            response_started_at="2026-07-14T01:00:01Z",
            response_completed_at="2026-07-14T01:00:02Z",
            response_status=200,
        )

    result = run_market_finality_shadow_capture_cycle(
        shadow_db_path=shadow_db,
        market_rule_archive_dir=raw_dir,
        authority_contract=AUTHORITY,
        captured_at="2026-07-14T01:00:03Z",
        fetcher=fetcher,
    )

    assert result["ok"] is False
    assert result["outcomes"][0]["state"] == "blocked"
    assert (
        "market_finality_capture_parser:market_finality_parser_raw_json_invalid"
        in result["outcomes"][0]["block_reasons"]
    )
