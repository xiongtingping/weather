from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

import scripts.resident_target_ws_publisher as publisher
from scripts.resident_target_ws_publisher import (
    _apply_no_live_boundary,
    _apply_price_changes,
    _build_multi_scope_manifest,
    _merge_book_quote,
    _scope,
    _future_probability_expired,
)
from bayes_basket_target.market_registry import (
    apply_target_market_registry_schema,
)
from bayes_basket_target.contracts import ContractError
from bayes_basket_target.ssot import apply_production_ssot_schema
from bayes_basket_target.fleet import expected_station_count


def _database(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        apply_production_ssot_schema(conn)
        apply_target_market_registry_schema(conn)
        snapshot = "a" * 64
        conn.execute(
            "INSERT INTO basket_target_market_registry_snapshot("
            "snapshot_sha256,source_schema,source_scope,exported_at_utc,"
            "source_database_path,row_count,ingested_at_utc,content_sha256"
            ") VALUES(?,?,?,?,?,?,?,?)",
            (
                snapshot,
                "reported_trajectory_market_registry_v1",
                "shared_live_registry_export_only",
                "2026-07-27T15:30:00Z",
                "/source/archive.db",
                2,
                "2026-07-27T15:31:00Z",
                "b" * 64,
            ),
        )
        for outcome, token, lower in (
            ("37°C", "yes-37", 37),
            ("38°C", "yes-38", 38),
        ):
            registry_json = json.dumps({
                "market_id": f"m-{lower}",
                "lower": lower,
                "upper": lower,
            })
            conn.execute(
                "INSERT INTO basket_target_market_registry("
                "snapshot_sha256,slug,event_date,outcome,city_slug,station_id,"
                "settlement_timezone,settlement_unit,source_lane,"
                "source_eligible,source_block_reason,resolution_url,"
                "rules_sha256,yes_token,no_token,market_active,"
                "market_accepting_orders,registry_json,refreshed_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    snapshot,
                    "busan-2026-07-28",
                    "2026-07-28",
                    outcome,
                    "busan",
                    "RKPK",
                    "Asia/Seoul",
                    "C",
                    "awc",
                    1,
                    "",
                    "https://example.invalid",
                    "c" * 64,
                    token,
                    f"no-{lower}",
                    1,
                    1,
                    registry_json,
                    "2026-07-27T15:30:00Z",
                ),
            )
        conn.execute(
            "INSERT INTO basket_probability_envelope("
            "envelope_id,stage,checkpoint_id,station_id,event_date,"
            "settlement_timezone,decision_at_utc,fresh_until_utc,"
            "bucket_ordinals_json,probabilities_json,posterior_version,"
            "posterior_sha256,bridge_sha256,lineage_json,content_sha256,"
            "state,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "p0-test",
                "P0",
                "p0-test",
                "RKPK",
                "2026-07-28",
                "Asia/Seoul",
                "2026-07-27T15:31:00Z",
                "2026-07-28T15:00:00Z",
                '{"37°C":0,"38°C":1}',
                '{"37°C":0.5,"38°C":0.5}',
                "test",
                "d" * 64,
                "e" * 64,
                "{}",
                "f" * 64,
                "COMMITTED",
                "2026-07-27T15:31:00Z",
            ),
        )
        conn.commit()


def test_scope_closes_target_read_connection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    _database(db)
    opened: list[sqlite3.Connection] = []
    original = publisher._open_target_read_only

    def tracked_open(path: Path = db) -> sqlite3.Connection:
        conn = original(path)
        opened.append(conn)
        return conn

    monkeypatch.setattr(publisher, "_open_target_read_only", tracked_open)

    catalog = _scope(
        db_path=db,
        as_of_utc="2026-07-27T16:00:00Z",
    )

    assert catalog["scopes"]
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")


def _add_scope(
    path: Path,
    *,
    event_date: str,
    stage: str,
    decision_at_utc: str,
    fresh_until_utc: str,
    token_suffix: str,
    station_id: str = "RKPK",
    settlement_timezone: str = "Asia/Seoul",
) -> None:
    with sqlite3.connect(path) as conn:
        for outcome, lower in (("37°C", 37), ("38°C", 38)):
            token = f"yes-{token_suffix}-{lower}"
            conn.execute(
                "INSERT INTO basket_target_market_registry("
                "snapshot_sha256,slug,event_date,outcome,city_slug,station_id,"
                "settlement_timezone,settlement_unit,source_lane,"
                "source_eligible,source_block_reason,resolution_url,"
                "rules_sha256,yes_token,no_token,market_active,"
                "market_accepting_orders,registry_json,refreshed_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "a" * 64,
                    f"busan-{event_date}-{lower}",
                    event_date,
                    outcome,
                    station_id.lower(),
                    station_id,
                    settlement_timezone,
                    "C",
                    "awc",
                    1,
                    "",
                    "https://example.invalid",
                    "c" * 64,
                    token,
                    f"no-{token_suffix}-{lower}",
                    1,
                    1,
                    json.dumps({
                        "market_id": f"m-{token_suffix}-{lower}",
                        "lower": lower,
                        "upper": lower,
                    }),
                    decision_at_utc,
                ),
            )
        envelope_id = f"{stage.lower()}-{token_suffix}"
        conn.execute(
            "INSERT INTO basket_probability_envelope("
            "envelope_id,stage,checkpoint_id,station_id,event_date,"
            "settlement_timezone,decision_at_utc,fresh_until_utc,"
            "bucket_ordinals_json,probabilities_json,posterior_version,"
            "posterior_sha256,bridge_sha256,lineage_json,content_sha256,"
            "state,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                envelope_id,
                stage,
                envelope_id,
                station_id,
                event_date,
                settlement_timezone,
                decision_at_utc,
                fresh_until_utc,
                '{"37°C":0,"38°C":1}',
                '{"37°C":0.5,"38°C":0.5}',
                "test",
                "d" * 64,
                "e" * 64,
                "{}",
                "f" * 64,
                "COMMITTED",
                decision_at_utc,
            ),
        )
        conn.commit()


def _add_probability(
    path: Path,
    *,
    event_date: str,
    stage: str,
    decision_at_utc: str,
    fresh_until_utc: str,
    suffix: str,
    station_id: str = "RKPK",
    settlement_timezone: str = "Asia/Seoul",
) -> None:
    with sqlite3.connect(path) as conn:
        envelope_id = f"{stage.lower()}-{suffix}"
        conn.execute(
            "INSERT INTO basket_probability_envelope("
            "envelope_id,stage,checkpoint_id,station_id,event_date,"
            "settlement_timezone,decision_at_utc,fresh_until_utc,"
            "bucket_ordinals_json,probabilities_json,posterior_version,"
            "posterior_sha256,bridge_sha256,lineage_json,content_sha256,"
            "state,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                envelope_id,
                stage,
                envelope_id,
                station_id,
                event_date,
                settlement_timezone,
                decision_at_utc,
                fresh_until_utc,
                '{"37°C":0,"38°C":1}',
                '{"37°C":0.5,"38°C":0.5}',
                "test",
                "1" * 64,
                "2" * 64,
                "{}",
                "3" * 64,
                "COMMITTED",
                decision_at_utc,
            ),
        )
        conn.commit()


def test_rkpk_existing_scope_keeps_registry_timezone_contract(tmp_path: Path) -> None:
    db = tmp_path / "target.db"
    _database(db)
    catalog = _scope(
        db_path=db,
        as_of_utc="2026-07-28T02:00:00Z",
    )
    assert len(catalog["scopes"]) == 1
    scope = catalog["scopes"][0]
    assert scope["station_ids"] == ["RKPK"]
    assert scope["event_date"] == "2026-07-28"
    assert scope["target_timezone"] == "Asia/Seoul"
    assert scope["event_day_state"] == "open"
    assert scope["probability_stage"] == "P0"
    assert scope["tokens"] == ["yes-37", "yes-38"]
    assert set(scope["registry_scope"]["markets_by_token"]) == {
        "yes-37",
        "yes-38",
    }
    assert catalog["tokens"] == ["yes-37", "yes-38"]
    assert len(catalog["catalog_sha256"]) == 64


def test_scope_includes_open_p1_and_next_fresh_p0m_without_future_p1(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    _database(db)
    _add_scope(
        db,
        event_date="2026-07-29",
        stage="P0-M",
        decision_at_utc="2026-07-28T01:30:00Z",
        fresh_until_utc="2026-07-29T15:00:00Z",
        token_suffix="next",
    )
    _add_probability(
        db,
        event_date="2026-07-28",
        stage="P1",
        decision_at_utc="2026-07-28T01:45:00Z",
        fresh_until_utc="2026-07-28T15:00:00Z",
        suffix="current",
    )
    catalog = _scope(
        db_path=db,
        as_of_utc="2026-07-28T02:00:00Z",
    )

    assert [
        (scope["scope_key"], scope["event_day_state"], scope["probability_stage"])
        for scope in catalog["scopes"]
    ] == [
        ("RKPK|2026-07-28", "open", "P1"),
        ("RKPK|2026-07-29", "not_started", "P0-M"),
    ]
    assert catalog["tokens"] == [
        "yes-37",
        "yes-38",
        "yes-next-37",
        "yes-next-38",
    ]

    future_p1_db = tmp_path / "future-p1.db"
    _database(future_p1_db)
    _add_scope(
        future_p1_db,
        event_date="2026-07-29",
        stage="P1",
        decision_at_utc="2026-07-28T01:30:00Z",
        fresh_until_utc="2026-07-29T15:00:00Z",
        token_suffix="future-p1",
    )
    catalog = _scope(
        db_path=future_p1_db,
        as_of_utc="2026-07-28T02:00:00Z",
    )
    assert [scope["scope_key"] for scope in catalog["scopes"]] == [
        "RKPK|2026-07-28"
    ]


def test_dynamic_catalog_includes_two_registry_stations(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    _database(db)
    _add_scope(
        db,
        station_id="KLAX",
        settlement_timezone="America/Los_Angeles",
        event_date="2026-07-27",
        stage="P1",
        decision_at_utc="2026-07-28T01:30:00Z",
        fresh_until_utc="2026-07-28T08:00:00Z",
        token_suffix="klax",
    )

    catalog = _scope(db_path=db, as_of_utc="2026-07-28T02:00:00Z")

    assert [(scope["station_id"], scope["event_day_state"]) for scope in catalog["scopes"]] == [
        ("KLAX", "open"),
        ("RKPK", "open"),
    ]
    assert catalog["connection_shards"] == [{
        "shard_id": 0,
        "tokens": ["yes-37", "yes-38", "yes-klax-37", "yes-klax-38"],
    }]


def test_connection_shards_are_bounded_at_official_token_limit() -> None:
    assert publisher.MAX_TARGET_STATIONS == expected_station_count()
    assert publisher.MAX_TARGET_SCOPES == expected_station_count() * 3
    assert publisher.MAX_TARGET_TOKENS == 2048
    tokens = [f"yes-{index:04d}" for index in range(
        publisher.MAX_TOKENS_PER_CONNECTION + 1
    )]

    shards = publisher._connection_shards(tokens)

    assert [len(shard["tokens"]) for shard in shards] == [
        publisher.MAX_TOKENS_PER_CONNECTION,
        1,
    ]
    assert all(
        len(shard["tokens"]) <= publisher.MAX_TOKENS_PER_CONNECTION
        for shard in shards
    )
    assert [token for shard in shards for token in shard["tokens"]] == tokens


def test_station_and_token_resource_caps_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    _database(db)
    _add_scope(
        db,
        station_id="KLAX",
        settlement_timezone="Asia/Seoul",
        event_date="2026-07-28",
        stage="P0",
        decision_at_utc="2026-07-28T01:30:00Z",
        fresh_until_utc="2026-07-28T15:00:00Z",
        token_suffix="klax",
    )
    monkeypatch.setattr(publisher, "MAX_TARGET_STATIONS", 1)
    with pytest.raises(ContractError, match="resident_target_ws_station_cap_exceeded"):
        _scope(db_path=db, as_of_utc="2026-07-28T02:00:00Z")

    monkeypatch.setattr(publisher, "MAX_TARGET_STATIONS", expected_station_count())
    monkeypatch.setattr(publisher, "MAX_TARGET_SCOPES", 1)
    with pytest.raises(ContractError, match="resident_target_ws_scope_cap_exceeded"):
        _scope(db_path=db, as_of_utc="2026-07-28T02:00:00Z")

    monkeypatch.setattr(publisher, "MAX_TARGET_SCOPES", expected_station_count() * 3)
    monkeypatch.setattr(publisher, "MAX_TARGET_TOKENS", 3)
    with pytest.raises(ContractError, match="resident_target_ws_token_catalog_invalid"):
        _scope(db_path=db, as_of_utc="2026-07-28T02:00:00Z")


def test_open_scope_keeps_stale_latest_p1_subscription_without_fallback(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    _database(db)
    _add_probability(
        db,
        event_date="2026-07-28",
        stage="P1",
        decision_at_utc="2026-07-28T01:45:00Z",
        fresh_until_utc="2026-07-28T01:59:59Z",
        suffix="stale-current",
    )
    catalog = _scope(
        db_path=db,
        as_of_utc="2026-07-28T02:00:00Z",
    )
    assert len(catalog["scopes"]) == 1
    scope = catalog["scopes"][0]
    assert scope["event_day_state"] == "open"
    assert scope["probability_stage"] == "P1"
    # One second past the roll cadence is still the latest reading there is,
    # so it stays usable and says so: past-cadence, not absent.
    assert scope["probability_evidence_fresh"] is True
    assert scope["probability_past_cadence"] is True
    assert scope["probability_age_seconds"] == pytest.approx(1.0)
    assert scope["probability_envelope_id"].endswith("stale-current")


def test_open_scope_never_downgrades_stale_p1_to_later_p0_or_p0m(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    _database(db)
    _add_probability(
        db,
        event_date="2026-07-28",
        stage="P1",
        decision_at_utc="2026-07-28T01:45:00Z",
        fresh_until_utc="2026-07-28T01:59:59Z",
        suffix="final-stale",
    )
    _add_probability(
        db,
        event_date="2026-07-28",
        stage="P0",
        decision_at_utc="2026-07-28T01:50:00Z",
        fresh_until_utc="2026-07-28T15:00:00Z",
        suffix="later-replay",
    )
    _add_probability(
        db,
        event_date="2026-07-28",
        stage="P0-M",
        decision_at_utc="2026-07-28T01:55:00Z",
        fresh_until_utc="2026-07-28T15:00:00Z",
        suffix="later-replay",
    )

    catalog = _scope(
        db_path=db,
        as_of_utc="2026-07-28T02:00:00Z",
    )

    scope = catalog["scopes"][0]
    assert scope["event_day_state"] == "open"
    assert scope["probability_stage"] == "P1"
    assert scope["probability_evidence_fresh"] is True
    assert scope["probability_past_cadence"] is True
    assert scope["probability_envelope_id"] == "p1-final-stale"


def test_scope_rejects_ambiguous_latest_checkpoint_within_stage(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    _database(db)
    for suffix in ("same-clock-a", "same-clock-b"):
        _add_probability(
            db,
            event_date="2026-07-28",
            stage="P0",
            decision_at_utc="2026-07-28T01:50:00Z",
            fresh_until_utc="2026-07-28T15:00:00Z",
            suffix=suffix,
        )

    with pytest.raises(
        ContractError,
        match="resident_target_ws_probability_stage_checkpoint_ambiguous",
    ):
        _scope(db_path=db, as_of_utc="2026-07-28T02:00:00Z")


def test_only_future_probability_expiry_requires_catalog_refresh() -> None:
    open_scope = {
        "event_day_state": "open",
        "probability_fresh_until_utc": "2026-07-28T01:59:59Z",
    }
    future_scope = {
        "event_day_state": "not_started",
        "probability_fresh_until_utc": "2026-07-28T02:00:01Z",
        "probability_evidence_fresh": True,
    }
    catalog = {"scopes": [open_scope, future_scope]}
    assert _future_probability_expired(
        catalog,
        observed_at_utc="2026-07-28T02:00:00Z",
    ) is False
    assert _future_probability_expired(
        catalog,
        observed_at_utc="2026-07-28T02:00:01Z",
    ) is True
    assert _future_probability_expired(
        {"scopes": [open_scope]},
        observed_at_utc="2026-07-28T03:00:00Z",
    ) is False
    # Once the catalog carries the scope as stale, expiry is behind it and
    # must not be raised again on every frame (the 2026-08-14 freeze).
    already_stale = {**future_scope, "probability_evidence_fresh": False}
    assert _future_probability_expired(
        {"scopes": [open_scope, already_stale]},
        observed_at_utc="2026-07-28T03:00:00Z",
    ) is False

def test_station_carries_two_listed_days_and_cannot_rotate_through_a_third(
    tmp_path: Path,
) -> None:
    """Charter 48(e): the D-2 listing window is two not-started days per
    station (tomorrow and the day just listed).  One more is a station
    silently rotating through additional days, which is still refused."""

    db = tmp_path / "target.db"
    _database(db)
    for event_date, suffix in (
        ("2026-07-29", "next"),
        ("2026-07-30", "next-next"),
    ):
        _add_scope(
            db,
            event_date=event_date,
            stage="P0-M",
            decision_at_utc="2026-07-28T01:30:00Z",
            fresh_until_utc="2026-07-30T15:00:00Z",
            token_suffix=suffix,
        )
    catalog = _scope(
        db_path=db,
        as_of_utc="2026-07-28T02:00:00Z",
    )
    assert sorted(
        scope["event_date"]
        for scope in catalog["scopes"]
        if scope["event_day_state"] == "not_started"
    ) == ["2026-07-29", "2026-07-30"]
    _add_scope(
        db,
        event_date="2026-07-31",
        stage="P0-M",
        decision_at_utc="2026-07-28T01:30:00Z",
        fresh_until_utc="2026-07-31T15:00:00Z",
        token_suffix="next-next-next",
    )
    with pytest.raises(
        ContractError,
        match="resident_target_ws_station_state_ambiguous",
    ):
        _scope(
            db_path=db,
            as_of_utc="2026-07-28T02:00:00Z",
        )


def test_scope_excludes_closed_and_stale_probability_days(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    _database(db)
    _add_scope(
        db,
        event_date="2026-07-27",
        stage="P1",
        decision_at_utc="2026-07-27T02:00:00Z",
        fresh_until_utc="2026-07-29T02:00:00Z",
        token_suffix="closed",
    )
    _add_scope(
        db,
        event_date="2026-07-29",
        stage="P0-M",
        decision_at_utc="2026-07-28T01:00:00Z",
        fresh_until_utc="2026-07-28T01:59:59Z",
        token_suffix="stale",
    )
    catalog = _scope(
        db_path=db,
        as_of_utc="2026-07-28T02:00:00Z",
    )
    # The closed day is gone; the not-started day whose probability went
    # stale keeps its book (user ruling, 2026-09-06) and says so.
    assert [scope["scope_key"] for scope in catalog["scopes"]] == [
        "RKPK|2026-07-28", "RKPK|2026-07-29",
    ]
    stale = catalog["scopes"][1]
    assert stale["event_day_state"] == "not_started"
    assert stale["probability_evidence_fresh"] is False

def _quote(token: str, observed_at: str) -> dict[str, object]:
    return {
        "token_id": token,
        "side": "YES",
        "bids": [{"price": 0.4, "size": 10.0}],
        "asks": [{"price": 0.42, "size": 10.0}],
        "best_bid": 0.4,
        "best_ask": 0.42,
        "book_observed_at": observed_at,
        "created_at": observed_at,
        "quote_source": "OFFICIAL_CLOB_WS",
    }


def test_multi_scope_manifest_is_deterministic_and_zero_authority(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    _database(db)
    _add_scope(
        db,
        event_date="2026-07-29",
        stage="P0-M",
        decision_at_utc="2026-07-28T01:30:00Z",
        fresh_until_utc="2026-07-29T15:00:00Z",
        token_suffix="next",
    )
    observed_at = "2026-07-28T02:00:00Z"
    catalog = _scope(db_path=db, as_of_utc=observed_at)
    quotes = {
        token: _quote(token, observed_at)
        for token in catalog["tokens"]
    }
    first = _build_multi_scope_manifest(
        catalog,
        quotes=quotes,
        observed_at=observed_at,
        started_at="2026-07-28T01:59:59Z",
    )
    second = _build_multi_scope_manifest(
        catalog,
        quotes=quotes,
        observed_at=observed_at,
        started_at="2026-07-28T01:59:59Z",
    )

    assert first == second
    assert first["schema"] == publisher.MANIFEST_SCHEMA
    assert first["state"] == "connected"
    assert first["evidence_only"] is True
    assert first["authorizes_order"] is False
    assert first["calls_order_gateway"] is False
    assert first["live_submit_authorized"] is False
    assert list(first["scope_manifests"]) == [
        "RKPK|2026-07-28",
        "RKPK|2026-07-29",
    ]
    for summary in first["scopes"]:
        scope_key = summary["scope_key"]
        nested = first["scope_manifests"][scope_key]
        assert nested["scope_key"] == scope_key
        assert nested["snapshot_sha256"] == summary[
            "scope_manifest_sha256"
        ]
        assert nested["evidence_only"] is True
        assert nested["authorizes_order"] is False
        assert nested["calls_order_gateway"] is False
        assert nested["live_submit_authorized"] is False
    supplied = first["snapshot_sha256"]
    assert supplied == publisher._manifest_sha256({
        key: value
        for key, value in first.items()
        if key != "snapshot_sha256"
    })


def test_incomplete_or_disconnected_shard_never_writes_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    writes: list[dict[str, object]] = []

    async def fake_receive(
        shard: dict[str, object],
        *,
        queue: asyncio.Queue[tuple[int, str | None]],
        proxy: str | None,
    ) -> None:
        await queue.put((int(shard["shard_id"]), None))
        raise ConnectionError("forced_shard_disconnect")

    monkeypatch.setattr(publisher, "_receive_connection_shard", fake_receive)
    monkeypatch.setattr(publisher, "assert_process_memory_budget", lambda **_: None)
    monkeypatch.setattr(publisher, "write_bounded_json", lambda _path, body, **_: writes.append(body))
    catalog = {
        "scopes": [],
        "tokens": ["yes-37"],
        "connection_shards": [{"shard_id": 0, "tokens": ["yes-37"]}],
        "catalog_sha256": "a" * 64,
    }

    with pytest.raises(ConnectionError, match="forced_shard_disconnect"):
        asyncio.run(
            publisher._publish_connection(
                catalog,
                db_path=tmp_path / "unused.db",
                output_path=tmp_path / "manifest.json",
                proxy=None,
            )
        )

    assert writes == []


def test_price_change_updates_exact_book_without_history_write() -> None:
    quotes = {
        "yes-37": {
            "bids": [{"price": 0.4, "size": 10.0}],
            "asks": [{"price": 0.42, "size": 10.0}],
            "best_bid": 0.4,
            "best_ask": 0.42,
        },
    }
    _apply_price_changes(
        quotes,
        {
            "event_type": "price_change",
            "price_changes": [
                {
                    "asset_id": "yes-37",
                    "side": "BUY",
                    "price": 0.41,
                    "size": 7.0,
                },
                {
                    "asset_id": "yes-37",
                    "side": "SELL",
                    "price": 0.42,
                    "size": 0.0,
                },
                {
                    "asset_id": "yes-37",
                    "side": "BUY",
                    "price": 0.4,
                    "size": 0.0,
                },
                {
                    "asset_id": "yes-37",
                    "side": "BUY",
                    "price": 0.41,
                    "size": 0.0,
                },
            ],
        },
        observed_at="2026-07-28T02:00:01Z",
    )
    assert quotes["yes-37"]["best_bid"] is None
    assert quotes["yes-37"]["best_ask"] is None
    assert quotes["yes-37"]["bids"] == []
    assert quotes["yes-37"]["asks"] == []
    assert quotes["yes-37"]["book_observed_at"] == (
        "2026-07-28T02:00:01Z"
    )


def test_duplicate_book_and_noop_price_change_preserve_economic_clock() -> None:
    original = {
        "token_id": "yes-37",
        "side": "YES",
        "bids": [{"price": 0.4, "size": 10.0}],
        "asks": [{"price": 0.42, "size": 10.0}],
        "best_bid": 0.4,
        "best_ask": 0.42,
        "book_side_state": "two_sided",
        "book_observed_at": "2026-07-28T02:00:00Z",
        "created_at": "2026-07-28T02:00:00Z",
        "quote_source": "OFFICIAL_CLOB_WS",
    }
    quotes = {"yes-37": dict(original)}
    duplicate = {
        **original,
        "book_observed_at": "2026-07-28T02:00:15Z",
        "created_at": "2026-07-28T02:00:15Z",
    }

    assert _merge_book_quote(
        quotes,
        "yes-37",
        duplicate,
    ) is False
    _apply_price_changes(
        quotes,
        {
            "event_type": "price_change",
            "price_changes": [{
                "asset_id": "yes-37",
                "side": "BUY",
                "price": 0.4,
                "size": 10.0,
            }],
        },
        observed_at="2026-07-28T02:00:30Z",
    )

    assert quotes["yes-37"]["book_observed_at"] == (
        "2026-07-28T02:00:00Z"
    )
    assert quotes["yes-37"]["created_at"] == (
        "2026-07-28T02:00:00Z"
    )


def test_shard_message_confirms_quiet_books_without_changing_economics() -> None:
    quotes = {
        "active": {
            "bids": [{"price": 0.4, "size": 10.0}],
            "asks": [{"price": 0.42, "size": 10.0}],
            "book_observed_at": "2026-07-28T02:00:00Z",
        },
        "quiet": {
            "bids": [{"price": 0.2, "size": 5.0}],
            "asks": [{"price": 0.22, "size": 5.0}],
            "book_observed_at": "2026-07-28T02:00:00Z",
        },
    }
    before = {
        token: publisher._quote_economic_state(quote)
        for token, quote in quotes.items()
    }

    publisher._confirm_shard_stream(
        quotes,
        {"active", "quiet"},
        observed_at="2026-07-28T02:00:30Z",
    )

    assert {
        token: publisher._quote_economic_state(quote)
        for token, quote in quotes.items()
    } == before
    assert {
        quote["_stream_confirmed_at"] for quote in quotes.values()
    } == {"2026-07-28T02:00:30Z"}


def test_publisher_station_domain_is_exact_canonical_registry() -> None:
    assert len(publisher.CANONICAL_TARGET_STATIONS) == expected_station_count()
    assert "RKPK" in publisher.CANONICAL_TARGET_STATIONS
    assert "XXXX" not in publisher.CANONICAL_TARGET_STATIONS


def test_exact_manifest_repeats_zero_authority_at_root() -> None:
    manifest: dict[str, object] = {"state": "connected"}
    _apply_no_live_boundary(manifest)

    assert manifest["evidence_only"] is True
    assert manifest["authorizes_order"] is False
    assert manifest["calls_order_gateway"] is False
    assert manifest["live_submit_authorized"] is False
    assert manifest["orders_submitted"] == 0
    assert manifest["resident_exact_scope_publisher"] == {
        "schema": publisher.SCHEMA,
        "persistent_connection": True,
        "exact_scope": True,
        "multi_scope": True,
        "writes_historical_ticks": False,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
    }


def test_resident_publisher_reconnects_with_bounded_backoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    attempts: list[int] = []
    sleeps: list[float] = []

    monkeypatch.setattr(
        publisher,
        "_scope",
        lambda **_: {
            "scopes": [],
            "tokens": ["yes-37"],
            "catalog_sha256": "a" * 64,
        },
    )

    async def fake_publish(*_: object, **__: object) -> None:
        attempts.append(len(attempts) + 1)
        if len(attempts) < 3:
            raise ConnectionError("forced_disconnect")
        raise asyncio.CancelledError

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(publisher, "_publish_connection", fake_publish)
    monkeypatch.setattr(publisher.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(publisher, "_configured_proxy", lambda _: None)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            publisher.run_resident_target_ws_publisher(
                db_path=tmp_path / "unused.db",
                output_path=tmp_path / "manifest.json",
            )
        )

    assert attempts == [1, 2, 3]
    assert sleeps == [1.0, 1.7]
    assert max(sleeps) <= publisher.RETRY_MAX_SECONDS


def test_latest_p1_is_usable_past_cadence_but_not_past_the_age_limit(
    tmp_path: Path,
) -> None:
    """The latest reading is the reading -- until it describes another day.

    P1 re-rolls hourly, so a scope is routinely past that cadence with
    nothing newer to prefer; refusing there stood 26 of 48 scopes down
    during one recovery.  Age, not cadence, is what makes a probability
    unusable, and the limit has to bite or "use the latest" would mean
    trading on this morning's state all afternoon.
    """

    limit = publisher.P1_PROBABILITY_MAX_STALE_SECONDS
    assert limit >= 3600, "the limit must at least span one missed roll"

    def _catalog(as_of: str):
        db = tmp_path / f"target-{as_of.replace(':', '')}.db"
        _database(db)
        _add_probability(
            db,
            event_date="2026-07-28",
            stage="P1",
            decision_at_utc="2026-07-28T01:00:00Z",
            fresh_until_utc="2026-07-28T02:00:00Z",
            suffix="latest",
        )
        return _scope(db_path=db, as_of_utc=as_of)

    # Just inside the limit: usable, and marked as past the cadence.
    inside = _catalog("2026-07-28T04:59:00Z")["scopes"][0]
    assert inside["probability_evidence_fresh"] is True
    assert inside["probability_past_cadence"] is True
    assert inside["probability_age_seconds"] == pytest.approx(
        limit - 60.0, abs=1.0
    )

    # Past it: this is a different state of the day, and it is refused.
    outside = _catalog("2026-07-28T05:30:00Z")["scopes"][0]
    assert outside["probability_evidence_fresh"] is False
    assert outside["probability_past_cadence"] is False
    assert outside["probability_age_seconds"] > limit

    # Inside the cadence the flag is off: the two claims stay distinct.
    current = _catalog("2026-07-28T01:30:00Z")["scopes"][0]
    assert current["probability_evidence_fresh"] is True
    assert current["probability_past_cadence"] is False
    assert current["probability_age_seconds"] == 0.0


def _book_frame(token: str) -> str:
    return json.dumps({
        "event_type": "book",
        "asset_id": token,
        "bids": [{"price": "0.40", "size": "10"}],
        "asks": [{"price": "0.42", "size": "10"}],
    })


def test_reconnect_carries_books_instead_of_publishing_placeholders(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A planned refresh must not overwrite a complete manifest with placeholders.

    Measured 2026-09-02: after every 30 s refresh the fresh sockets confirmed
    themselves before the venue's initial book dump had landed, every waiting
    token was materialised as a bookless placeholder, and the manifest on disk
    spent 40% of the time claiming 400-800 unmaterialized tokens.  The next
    generation now starts from the books the previous one held.
    """

    db = tmp_path / "target.db"
    _database(db)
    catalog = _scope(db_path=db, as_of_utc="2026-07-28T02:00:00Z")
    tokens = list(catalog["tokens"])
    assert len(tokens) == 2
    writes: list[dict[str, object]] = []
    monkeypatch.setattr(publisher, "assert_process_memory_budget", lambda **_: None)
    monkeypatch.setattr(publisher, "write_bounded_json", lambda _path, body, **_: writes.append(body))
    monkeypatch.setattr(publisher, "RECEIVE_TIMEOUT_SECONDS", 0.1)

    async def first_generation(
        shard: dict[str, object],
        *,
        queue: asyncio.Queue[tuple[int, str | None]],
        proxy: str | None,
    ) -> None:
        # The venue's initial dump, delivered as one batched frame.
        await queue.put((
            int(shard["shard_id"]),
            json.dumps([json.loads(_book_frame(str(token))) for token in shard["tokens"]]),
        ))
        await asyncio.sleep(0.05)
        raise ConnectionError("gen1_closed")

    carried: dict[str, dict[str, object]] = {}
    monkeypatch.setattr(publisher, "_receive_connection_shard", first_generation)
    with pytest.raises(ConnectionError, match="gen1_closed"):
        asyncio.run(
            publisher._publish_connection(
                catalog,
                db_path=db,
                output_path=tmp_path / "manifest.json",
                proxy=None,
                carried_books=carried,
            )
        )
    assert len(writes) == 1
    first = writes[0]["scope_manifests"]["RKPK|2026-07-28"]
    assert first["unmaterialized_tokens"] == []
    assert set(carried) == set(tokens)
    gen1_book_clock = {
        token: first["quotes_by_token"][token]["book_observed_at"]
        for token in tokens
    }

    writes.clear()
    gen2_started = publisher._now_utc()

    async def second_generation(
        shard: dict[str, object],
        *,
        queue: asyncio.Queue[tuple[int, str | None]],
        proxy: str | None,
    ) -> None:
        # The socket is open and confirms itself before the venue's initial
        # book dump arrives: first the receive-timeout heartbeat, then a
        # frame that carries no book.
        await queue.put((int(shard["shard_id"]), None))
        await queue.put((
            int(shard["shard_id"]),
            json.dumps({"event_type": "last_trade_price", "asset_id": shard["tokens"][0]}),
        ))
        await asyncio.sleep(0.05)
        raise ConnectionError("gen2_closed")

    monkeypatch.setattr(publisher, "_receive_connection_shard", second_generation)
    with pytest.raises(ConnectionError, match="gen2_closed"):
        asyncio.run(
            publisher._publish_connection(
                catalog,
                db_path=db,
                output_path=tmp_path / "manifest.json",
                proxy=None,
                carried_books=carried,
            )
        )
    assert len(writes) == 1
    second = writes[0]["scope_manifests"]["RKPK|2026-07-28"]
    assert second["unmaterialized_tokens"] == []
    assert second["materialized_token_count"] == len(tokens)
    for token in tokens:
        quote = second["quotes_by_token"][token]
        # No dump has re-observed the book yet, so its economic clock is the
        # one the previous socket stamped ...
        assert quote["book_observed_at"] == gen1_book_clock[token]
        # ... while the transport lease belongs to this socket.
        assert quote["_stream_confirmed_at"] >= gen2_started
    # The carried set is books only: a placeholder never crosses a generation.
    assert set(carried) == set(tokens)
    assert all("asks" in quote and "bids" in quote for quote in carried.values())


def test_seeded_generation_waits_for_every_shard_to_confirm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Seeded books make every token present before every socket has opened.

    The manifest still claims one connected generation, so no write may
    happen until each shard has confirmed its own stream.
    """

    monkeypatch.setattr(publisher, "MAX_TOKENS_PER_CONNECTION", 1)
    db = tmp_path / "target.db"
    _database(db)
    catalog = _scope(db_path=db, as_of_utc="2026-07-28T02:00:00Z")
    assert len(catalog["connection_shards"]) == 2
    carried = {
        token: _quote(token, "2026-07-28T01:59:00Z")
        for token in catalog["tokens"]
    }
    writes: list[dict[str, object]] = []
    monkeypatch.setattr(publisher, "assert_process_memory_budget", lambda **_: None)
    monkeypatch.setattr(publisher, "write_bounded_json", lambda _path, body, **_: writes.append(body))
    monkeypatch.setattr(publisher, "RECEIVE_TIMEOUT_SECONDS", 0.1)
    release = asyncio.Event()
    late_shard_confirmed_at: list[str] = []

    async def receive(
        shard: dict[str, object],
        *,
        queue: asyncio.Queue[tuple[int, str | None]],
        proxy: str | None,
    ) -> None:
        shard_id = int(shard["shard_id"])
        if shard_id == 0:
            await queue.put((0, json.dumps({"event_type": "last_trade_price"})))
            await release.wait()
            raise ConnectionError("gen_closed")
        # Shard 1's handshake is still in flight while shard 0 already speaks.
        await asyncio.sleep(0.3)
        late_shard_confirmed_at.append(publisher._now_utc())
        await queue.put((1, None))
        await queue.put((1, json.dumps({"event_type": "last_trade_price"})))
        await asyncio.sleep(0.05)
        release.set()
        raise ConnectionError("gen_closed")

    monkeypatch.setattr(publisher, "_receive_connection_shard", receive)
    with pytest.raises(ConnectionError, match="gen_closed"):
        asyncio.run(
            publisher._publish_connection(
                catalog,
                db_path=db,
                output_path=tmp_path / "manifest.json",
                proxy=None,
                carried_books=carried,
            )
        )
    assert len(writes) == 1
    late_token = str(catalog["connection_shards"][1]["tokens"][0])
    quote = writes[0]["scope_manifests"]["RKPK|2026-07-28"]["quotes_by_token"][late_token]
    assert quote.get("_stream_confirmed_at", "") >= late_shard_confirmed_at[0]


def test_emptied_book_is_not_carried_into_the_next_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A book frame with nothing on either side retires the carried book.

    Seeding must not keep describing a market the venue says is gone: the
    token surfaces as the placeholder a cold generation would have shown.
    """

    db = tmp_path / "target.db"
    _database(db)
    catalog = _scope(db_path=db, as_of_utc="2026-07-28T02:00:00Z")
    tokens = list(catalog["tokens"])
    carried = {token: _quote(token, "2026-07-28T01:59:00Z") for token in tokens}
    writes: list[dict[str, object]] = []
    monkeypatch.setattr(publisher, "assert_process_memory_budget", lambda **_: None)
    monkeypatch.setattr(publisher, "write_bounded_json", lambda _path, body, **_: writes.append(body))
    monkeypatch.setattr(publisher, "RECEIVE_TIMEOUT_SECONDS", 0.1)
    emptied, kept = "yes-38", "yes-37"

    async def receive(
        shard: dict[str, object],
        *,
        queue: asyncio.Queue[tuple[int, str | None]],
        proxy: str | None,
    ) -> None:
        shard_id = int(shard["shard_id"])
        await queue.put((shard_id, json.dumps({
            "event_type": "book", "asset_id": emptied, "bids": [], "asks": [],
        })))
        await asyncio.sleep(0.05)
        raise ConnectionError("gen_closed")

    monkeypatch.setattr(publisher, "_receive_connection_shard", receive)
    with pytest.raises(ConnectionError, match="gen_closed"):
        asyncio.run(
            publisher._publish_connection(
                catalog,
                db_path=db,
                output_path=tmp_path / "manifest.json",
                proxy=None,
                carried_books=carried,
            )
        )
    assert len(writes) == 1
    scope = writes[0]["scope_manifests"]["RKPK|2026-07-28"]
    assert scope["unmaterialized_tokens"] == [emptied]
    assert scope["quotes_by_token"][kept]["book_observed_at"] == "2026-07-28T01:59:00Z"
    assert "asks" not in scope["quotes_by_token"][emptied]
    assert set(carried) == {kept}
