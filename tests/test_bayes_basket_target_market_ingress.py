from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target import ContractError, content_sha256
from bayes_basket_target.market_ingress import (
    _scope_quote_values,
    load_official_ws_quotes,
    materialize_market_evidence,
    materialize_official_ws_evidence_cache,
)
from bayes_basket_target.ssot import apply_production_ssot_schema


def _freshness_args() -> dict[str, object]:
    policy = {
        "schema": "bayes_basket_quote_freshness_policy_v1",
        "policy_id": "market-ingress-test-v1",
        "max_quote_age_seconds": 5.0,
        "valid_from_utc": "2026-07-01T00:00:00Z",
        "valid_until_utc": "2026-08-01T00:00:00Z",
        "authority_scope": "PRODUCTION_NO_LIVE_CANDIDATE",
        "evidence_status": "DESIGN_CANDIDATE",
    }
    policy["content_sha256"] = content_sha256(policy)
    return {
        "quote_freshness_policy": policy,
        "policy_authority_scope": "PRODUCTION_NO_LIVE_CANDIDATE",
    }


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _manifest(
    tmp_path: Path,
    *,
    complete: bool = True,
    ask_only: bool = False,
    bid_only: bool = False,
) -> Path:
    assert not (ask_only and bid_only)
    quote = {
        "token_id": "yes-38",
        "slug": "highest-temperature-in-test-on-july-23-2026",
        "event_date": "2026-07-23",
        "outcome": "38°C",
        "side": "YES",
        # The resident listener uses numeric zero for an authoritative empty
        # bid side.  It must remain valid buy-only evidence.
        "best_bid": 0.0 if ask_only else 0.4,
        "best_ask": None if bid_only else 0.42,
        "bids": [] if ask_only else [{"price": 0.4, "size": 10.0}],
        "asks": [] if bid_only else [{"price": 0.42, "size": 10.0}],
        "book_observed_at": "2026-07-23T05:00:00Z",
        "created_at": "2026-07-23T05:00:00Z",
    }
    if not complete:
        # Never observed: no book came back for this token at all.  An
        # observed-but-empty book is a different thing and is admitted --
        # see the two tests below.
        quote["best_bid"] = None
        quote["best_ask"] = None
        quote["bids"] = []
        quote["asks"] = []
        quote["book_observed_at"] = ""
        quote["created_at"] = ""
    shard_dir = tmp_path / "shards"
    shard_dir.mkdir()
    core = {
        "schema": "clob_ws_token_quote_snapshot_v1",
        "token_id": "yes-38",
        "quote": quote,
        "shard_index": 0,
        "shard_generation": 1,
        "shard_active": True,
        "source": "official_polymarket_clob_websocket",
        "evidence_only": True,
        "authorizes_order": False,
    }
    shard = {**core, "snapshot_sha256": hashlib.sha256(_canonical(core)).hexdigest()}
    (shard_dir / "yes.json").write_text(json.dumps(shard), encoding="utf-8")
    root = {
        "schema": "clob_ws_latest_quote_snapshot_v1",
        "state": "connected",
        "active_shards": [0],
        "expected_shards": 1,
        "active_shard_generations": {"0": 1},
        "quote_storage": "sha256_token_shards_v1",
        "quote_shard_dir": str(shard_dir),
        "quotes_by_token": {},
        "materialized_token_count": 1 if complete else 0,
        "unmaterialized_tokens": [] if complete else ["yes-38"],
        "source": "official_polymarket_clob_websocket",
        "evidence_only": True,
        "authorizes_order": False,
    }
    root["snapshot_sha256"] = hashlib.sha256(_canonical(root)).hexdigest()
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(root), encoding="utf-8")
    return path


def _inputs(tmp_path: Path):
    inventory = {
        "schema": "bayes_basket_confirmed_inventory_ingress_v1",
        "markets": [{
            "station_id": "TEST",
            "event_date": "2026-07-23",
            "inventory_snapshot_id": "inv-1",
            "inventory_version": "inv-v1",
            "observed_at": "2026-07-23T05:00:00Z",
            "complete": 1,
            "remaining_budget_usdc": 20.0,
            "inventory_json": {"filled_by_bucket": {"38°C": 0.0}},
        }],
    }
    order = {
        "schema": "bayes_basket_order_state_ingress_v1",
        "markets": [{
            "station_id": "TEST",
            "event_date": "2026-07-23",
            "order_state_snapshot_id": "ord-1",
            "order_state_version": "ord-v1",
            "observed_at": "2026-07-23T05:00:00Z",
            "known": 1,
            "order_state_json": {"pending_by_bucket": {"38°C": 0.0}},
        }],
    }
    cash = {
        "schema": "bayes_basket_cash_phase_ingress_v1",
        "markets": [{
            "station_id": "TEST",
            "event_date": "2026-07-23",
            "cash_phase_input": {
                "policy_version": "policy-v1",
                "station_id": "TEST",
                "current_phase": "HEAVY",
                "event_date_local": "2026-07-23",
                "observed_at_utc": "2026-07-23T05:00:00Z",
                "station_timezone": "UTC",
                "checkpoint_id": "cp-1",
            },
        }],
    }
    paths = []
    for name, value in (("inventory", inventory), ("order", order), ("cash", cash)):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        paths.append(path)
    return tuple(paths)


# The exchange's own admission rule, on every live Gamma market.  The
# production snapshot refuses a bucket without it, so a fixture without it is
# not a smaller fixture -- it is a market that cannot be traded.
EXCHANGE_SIZING = {"order_min_size": 5, "order_price_min_tick_size": 0.01}


def _registry(conn):
    conn.execute("""CREATE TABLE reported_trajectory_market_registry(
        slug TEXT,event_date TEXT,station_id TEXT,settlement_timezone TEXT,
        outcome TEXT,yes_token TEXT,registry_json TEXT,source_eligible INTEGER,
        market_active INTEGER,market_accepting_orders INTEGER)""")
    conn.execute("""INSERT INTO reported_trajectory_market_registry VALUES
        (?,?,?,?,?,?,?,?,?,?)""", (
        "highest-temperature-in-test-on-july-23-2026", "2026-07-23", "TEST", "UTC",
        "38°C", "yes-38",
        json.dumps({"market_id": "m-38", "lower": 38.0, "upper": 38.0, **EXCHANGE_SIZING}), 1, 1, 1,
    ))


def test_shard_manifest_is_hash_verified_and_loaded(tmp_path: Path) -> None:
    path = _manifest(tmp_path)
    root, quotes = load_official_ws_quotes(path)
    assert root["source"] == "official_polymarket_clob_websocket"
    assert quotes["yes-38"]["asks"][0]["price"] == 0.42

    tampered = json.loads(path.read_text())
    tampered["state"] = "tampered"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ContractError, match="content_sha_mismatch"):
        load_official_ws_quotes(path)


def test_ask_only_manifest_is_valid_buy_fok_evidence(tmp_path: Path) -> None:
    path = _manifest(tmp_path, ask_only=True)
    root, quotes = load_official_ws_quotes(path)
    assert root["source"] == "official_polymarket_clob_websocket"
    assert quotes["yes-38"]["asks"][0]["price"] == 0.42
    assert quotes["yes-38"]["bids"] == []

    inventory, order, cash = _inputs(tmp_path)
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        _registry(conn)
        result = materialize_market_evidence(
            conn,
            manifest_path=path,
            inventory_path=inventory,
            order_state_path=order,
            cash_phase_path=cash,
            **_freshness_args(),
            commit=True,
        )
        assert result["state"] == "COMMITTED"
        buckets = json.loads(
            conn.execute(
                "SELECT buckets_json FROM basket_official_ws_snapshot"
            ).fetchone()[0]
        )
        assert buckets[0]["bid_available"] is False
        assert buckets[0]["bid_levels"] == []
        assert buckets[0]["bid_price"] == 0.0


def test_bid_only_manifest_is_retained_but_buy_is_unavailable(
    tmp_path: Path,
) -> None:
    path = _manifest(tmp_path, bid_only=True)
    root = json.loads(path.read_text(encoding="utf-8"))
    root["unmaterialized_tokens"] = ["yes-38"]
    root["snapshot_sha256"] = hashlib.sha256(
        _canonical({
            key: value
            for key, value in root.items()
            if key != "snapshot_sha256"
        })
    ).hexdigest()
    path.write_text(json.dumps(root), encoding="utf-8")
    _root, quotes = load_official_ws_quotes(path)
    assert quotes["yes-38"]["asks"] == []
    assert quotes["yes-38"]["bids"][0]["price"] == 0.4

    inventory, order, cash = _inputs(tmp_path)
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        _registry(conn)
        result = materialize_market_evidence(
            conn,
            manifest_path=path,
            inventory_path=inventory,
            order_state_path=order,
            cash_phase_path=cash,
            **_freshness_args(),
            commit=True,
        )
        assert result["state"] == "COMMITTED"
        buckets = json.loads(
            conn.execute(
                "SELECT buckets_json FROM basket_official_ws_snapshot"
            ).fetchone()[0]
        )
        assert buckets[0]["ask_available"] is False
        assert buckets[0]["ask_levels"] == []
        assert buckets[0]["best_ask"] is None
        assert buckets[0]["bid_available"] is True


def test_basket_freshness_uses_oldest_economic_book_leg() -> None:
    rows = [
        {
            "station_id": "TEST",
            "event_date": "2026-07-23",
            "outcome": bucket,
            "yes_token": token,
            "lower": ordinal,
            "upper": ordinal,
            "order_min_size": 5,
        }
        for ordinal, (bucket, token) in enumerate(
            (("38°C", "yes-38"), ("39°C", "yes-39")),
            start=38,
        )
    ]
    quotes = {
        "yes-38": {
            "token_id": "yes-38",
            "side": "YES",
            "best_bid": 0.4,
            "best_ask": 0.42,
            "bids": [{"price": 0.4, "size": 10.0}],
            "asks": [{"price": 0.42, "size": 10.0}],
            "book_observed_at": "2026-07-23T05:00:00Z",
            "_stream_confirmed_at": "2026-07-23T05:00:04Z",
            "_shard_index": 0,
            "_shard_generation": 1,
        },
        "yes-39": {
            "token_id": "yes-39",
            "side": "YES",
            "best_bid": 0.3,
            "best_ask": 0.32,
            "bids": [{"price": 0.3, "size": 10.0}],
            "asks": [{"price": 0.32, "size": 10.0}],
            "book_observed_at": "2026-07-23T05:00:01Z",
            "_stream_confirmed_at": "2026-07-23T05:00:02Z",
            "_shard_index": 1,
            "_shard_generation": 1,
        },
    }
    buckets, observed_at = _scope_quote_values(rows, quotes)
    assert observed_at == "2026-07-23T05:00:00Z"
    assert buckets[0]["book_observed_at"] == "2026-07-23T05:00:00Z"
    assert "effective_quote_observed_at" not in buckets[0]
    assert "stream_confirmed_at" not in buckets[0]


def test_market_ingress_preserves_distinct_official_token_ticks() -> None:
    rows = [
        {
            "station_id": "TEST",
            "event_date": "2026-07-23",
            "outcome": bucket,
            "yes_token": token,
            "lower": ordinal,
            "upper": ordinal,
            "order_min_size": 5,
        }
        for ordinal, (bucket, token) in enumerate(
            (("38°C", "yes-38"), ("39°C", "yes-39")),
            start=38,
        )
    ]
    quotes = {
        "yes-38": {
            "token_id": "yes-38",
            "side": "YES",
            "best_bid": 0.003,
            "best_ask": 0.007,
            "bids": [{"price": 0.003, "size": 10.0}],
            "asks": [{"price": 0.007, "size": 10.0}],
            "tick_size": 0.001,
            "book_observed_at": "2026-07-23T05:00:00Z",
        },
        "yes-39": {
            "token_id": "yes-39",
            "side": "YES",
            "best_bid": 0.30,
            "best_ask": 0.32,
            "bids": [{"price": 0.30, "size": 10.0}],
            "asks": [{"price": 0.32, "size": 10.0}],
            "tick_size": 0.01,
            "book_observed_at": "2026-07-23T05:00:00Z",
        },
    }
    buckets, _ = _scope_quote_values(rows, quotes)
    assert [item["price_tick_size"] for item in buckets] == [0.001, 0.01]
    assert [item["tick_size_source"] for item in buckets] == [
        "OFFICIAL_WS_TICK_SIZE",
        "OFFICIAL_WS_TICK_SIZE",
    ]


def test_market_ingress_derives_legacy_low_price_book_grid() -> None:
    rows = [{
        "station_id": "TEST",
        "event_date": "2026-07-23",
        "outcome": "38°C",
        "yes_token": "yes-38",
        "lower": 38,
        "upper": 38,
        "order_min_size": 5,
    }]
    quotes = {
        "yes-38": {
            "token_id": "yes-38",
            "side": "YES",
            "best_bid": 0.003,
            "best_ask": 0.007,
            "bids": [{"price": 0.003, "size": 10.0}],
            "asks": [
                {"price": 0.007, "size": 10.0},
                {"price": 0.009, "size": 20.0},
            ],
            "book_observed_at": "2026-07-23T05:00:00Z",
        },
    }
    buckets, _ = _scope_quote_values(rows, quotes)
    assert buckets[0]["price_tick_size"] == 0.001
    assert (
        buckets[0]["tick_size_source"]
        == "OFFICIAL_WS_BOOK_GRID_DERIVED"
    )


def test_market_ingress_rejects_official_tick_price_mismatch() -> None:
    rows = [{
        "station_id": "TEST",
        "event_date": "2026-07-23",
        "outcome": "38°C",
        "yes_token": "yes-38",
        "lower": 38,
        "upper": 38,
        "order_min_size": 5,
    }]
    quotes = {
        "yes-38": {
            "token_id": "yes-38",
            "side": "YES",
            "best_bid": 0.003,
            "best_ask": 0.007,
            "bids": [{"price": 0.003, "size": 10.0}],
            "asks": [{"price": 0.007, "size": 10.0}],
            "tick_size": 0.01,
            "book_observed_at": "2026-07-23T05:00:00Z",
        },
    }
    with pytest.raises(ContractError, match="official_ws_price_tick_mismatch"):
        _scope_quote_values(rows, quotes)


@pytest.fixture
def _fixture_day_inside_the_cache_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the fixture's 2026-07-23 book inside the evidence cache's window.

    Every materialization ages out scopes older than
    ``WS_EVIDENCE_CACHE_RETAIN_DAYS`` against the wall clock before it writes
    (504fc730, 2026-08-19: the cache had filled its 512-scope ceiling).  From
    2026-07-26 on, the fixture day was deleted at the top of each call, so a
    replay re-inserted it (``wrote`` True) and a rewind had no row left to
    rewind against -- the guard was never reached, not broken.  The window is
    widened for these tests; the ageing itself is not what they are about.
    """

    import bayes_basket_target.market_ingress as ingress

    monkeypatch.setattr(ingress, "WS_EVIDENCE_CACHE_RETAIN_DAYS", 100_000)


@pytest.mark.usefixtures("_fixture_day_inside_the_cache_window")
def test_evidence_only_ws_cache_is_independent_and_latest_only(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        _registry(conn)

        validated = materialize_official_ws_evidence_cache(conn, manifest_path=manifest)
        assert validated["state"] == "VALIDATED_NO_WRITE"
        assert validated["evidence_only"] is True
        assert validated["authorizes_order"] is False
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_official_ws_evidence_cache"
        ).fetchone()[0] == 0

        committed = materialize_official_ws_evidence_cache(
            conn, manifest_path=manifest, commit=True
        )
        assert committed["state"] == "COMMITTED"
        row = conn.execute(
            "SELECT quote_source,evidence_only,authorizes_order,buckets_json "
            "FROM basket_official_ws_evidence_cache"
        ).fetchone()
        assert row[0] == "OFFICIAL_CLOB_WS"
        assert row[1] == 1
        assert row[2] == 0
        assert json.loads(row[3])[0]["token_id"] == "yes-38"

        replay = materialize_official_ws_evidence_cache(
            conn, manifest_path=manifest, commit=True
        )
        assert replay["receipts"][0]["wrote"] is False
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_official_ws_evidence_cache"
        ).fetchone()[0] == 1
        # The execution-input table remains empty: evidence-only connectivity
        # must not satisfy q=0/order-state/cash-phase materialization.
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_official_ws_snapshot"
        ).fetchone()[0] == 0


@pytest.mark.usefixtures("_fixture_day_inside_the_cache_window")
def test_evidence_only_ws_cache_rejects_time_rewind(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        _registry(conn)
        materialize_official_ws_evidence_cache(
            conn, manifest_path=manifest, commit=True
        )

        shard_path = next((tmp_path / "shards").glob("*.json"))
        shard = json.loads(shard_path.read_text(encoding="utf-8"))
        shard["quote"]["book_observed_at"] = "2026-07-22T23:59:00Z"
        shard["quote"]["created_at"] = "2026-07-22T23:59:00Z"
        core = {key: value for key, value in shard.items() if key != "snapshot_sha256"}
        shard["snapshot_sha256"] = hashlib.sha256(_canonical(core)).hexdigest()
        shard_path.write_text(json.dumps(shard), encoding="utf-8")

        with pytest.raises(ContractError, match="evidence_time_rewind"):
            materialize_official_ws_evidence_cache(
                conn, manifest_path=manifest, commit=True
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_official_ws_evidence_cache"
        ).fetchone()[0] == 1


def test_market_ingress_requires_selected_quotes_and_is_atomic(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    inventory, order, cash = _inputs(tmp_path)
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        _registry(conn)
        validated = materialize_market_evidence(
            conn, manifest_path=manifest, inventory_path=inventory,
            order_state_path=order, cash_phase_path=cash,
            **_freshness_args(),
        )
        assert validated["state"] == "VALIDATED_NO_WRITE"
        assert conn.execute("SELECT COUNT(*) FROM basket_official_ws_snapshot").fetchone()[0] == 0
        committed = materialize_market_evidence(
            conn, manifest_path=manifest, inventory_path=inventory,
            order_state_path=order, cash_phase_path=cash,
            **_freshness_args(), commit=True,
        )
        assert committed["state"] == "COMMITTED"
        assert conn.execute("SELECT COUNT(*) FROM basket_official_ws_snapshot").fetchone()[0] == 1
        replay = materialize_market_evidence(
            conn, manifest_path=manifest, inventory_path=inventory,
            order_state_path=order, cash_phase_path=cash,
            **_freshness_args(), commit=True,
        )
        assert replay["receipts"][0]["wrote_quote"] is False


def test_an_observed_but_empty_book_is_evidence_not_a_gap() -> None:
    """Nobody bidding at any price is a market state, not a capture failure.

    Refusing it cost whole stations: on 2026-08-16 NZWN, RKPK, ZSPD and ZSQD
    each had four to eight priced buckets and the rest empty, and a scope is
    admitted only when every bucket is complete -- so the good books were
    discarded along with the empty ones and those stations carried no market
    evidence at all.  What separates the two cases is whether a book was
    ever observed.
    """

    from bayes_basket_target.market_ingress import _quote_is_complete

    observed_empty = {
        "bids": [], "asks": [],
        "book_observed_at": "2026-07-23T05:00:00Z",
    }
    never_observed = {"bids": [], "asks": [], "book_observed_at": ""}
    one_sided = {
        "bids": [], "asks": [{"price": 0.42, "size": 10.0}],
        "book_observed_at": "2026-07-23T05:00:00Z",
    }
    assert _quote_is_complete(observed_empty) is True
    assert _quote_is_complete(one_sided) is True
    assert _quote_is_complete(never_observed) is False


def test_market_ingress_rejects_unmaterialized_selected_bucket(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, complete=False)
    inventory, order, cash = _inputs(tmp_path)
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        _registry(conn)
        with pytest.raises(ContractError, match="complete_quotes_missing"):
            materialize_market_evidence(
                conn, manifest_path=manifest, inventory_path=inventory,
                order_state_path=order, cash_phase_path=cash,
                **_freshness_args(),
            )


def test_market_ingress_can_select_an_explicit_complete_scope_subset(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    inventory, order, cash = _inputs(tmp_path)
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        _registry(conn)
        conn.execute(
            "INSERT INTO reported_trajectory_market_registry VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "highest-temperature-in-test-on-july-24-2026", "2026-07-24", "TEST", "UTC",
                "38°C", "missing-yes-38",
                json.dumps({"market_id": "m-24-38", "lower": 38.0, "upper": 38.0, **EXCHANGE_SIZING}), 1, 1, 1,
            ),
        )
        result = materialize_market_evidence(
            conn,
            manifest_path=manifest,
            inventory_payload=json.loads(inventory.read_text()),
            order_state_payload=json.loads(order.read_text()),
            cash_phase_payload=json.loads(cash.read_text()),
            scope_keys={("TEST", "2026-07-23")},
            **_freshness_args(),
            commit=True,
        )
        assert result["selected_market_count"] == 1
        assert conn.execute("SELECT COUNT(*) FROM basket_official_ws_snapshot").fetchone()[0] == 1


def test_manifest_heartbeat_does_not_change_quote_version(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    inventory, order, cash = _inputs(tmp_path)
    first_root, first_quotes = load_official_ws_quotes(manifest)
    heartbeat_root = json.loads(manifest.read_text())
    heartbeat_root["heartbeat_at"] = "2026-07-23T05:00:01Z"
    heartbeat_root["snapshot_sha256"] = hashlib.sha256(_canonical({
        key: value for key, value in heartbeat_root.items() if key != "snapshot_sha256"
    })).hexdigest()
    manifest.write_text(json.dumps(heartbeat_root), encoding="utf-8")
    second_root, second_quotes = load_official_ws_quotes(manifest)
    assert first_root["snapshot_sha256"] != second_root["snapshot_sha256"]
    assert first_quotes == second_quotes

    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        _registry(conn)
        first = materialize_market_evidence(
            conn, manifest_path=manifest, inventory_path=inventory,
            order_state_path=order, cash_phase_path=cash,
            **_freshness_args(), commit=True,
        )
        second = materialize_market_evidence(
            conn, manifest_path=manifest, inventory_path=inventory,
            order_state_path=order, cash_phase_path=cash,
            **_freshness_args(), commit=True,
        )
        assert first["receipts"][0]["wrote_quote"] is True
        assert second["receipts"][0]["wrote_quote"] is False
        assert conn.execute("SELECT COUNT(*) FROM basket_official_ws_snapshot").fetchone()[0] == 1


def test_one_hundred_stream_keepalives_write_no_new_market_facts(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    inventory, order, cash = _inputs(tmp_path)
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        _registry(conn)
        first = materialize_market_evidence(
            conn,
            manifest_path=manifest,
            inventory_path=inventory,
            order_state_path=order,
            cash_phase_path=cash,
            **_freshness_args(),
            commit=True,
        )
        assert first["receipts"][0]["wrote_quote"] is True

        for second in range(1, 101):
            root = json.loads(manifest.read_text())
            root["connection_observed_at"] = (
                f"2026-07-23T05:00:{second % 60:02d}Z"
            )
            root["active_shard_connection_observed_at"] = {
                "0": root["connection_observed_at"]
            }
            root["snapshot_sha256"] = hashlib.sha256(
                _canonical(
                    {
                        key: value
                        for key, value in root.items()
                        if key != "snapshot_sha256"
                    }
                )
            ).hexdigest()
            manifest.write_text(json.dumps(root), encoding="utf-8")
            replay = materialize_market_evidence(
                conn,
                manifest_path=manifest,
                inventory_path=inventory,
                order_state_path=order,
                cash_phase_path=cash,
                **_freshness_args(),
                commit=True,
            )
            assert replay["receipts"][0]["wrote_quote"] is False

        assert conn.execute(
            "SELECT COUNT(*) FROM basket_official_ws_snapshot"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_confirmed_inventory_snapshot"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_order_state_snapshot"
        ).fetchone()[0] == 1


def test_real_book_low_high_low_appends_exactly_one_snapshot_per_event(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    inventory, order, cash = _inputs(tmp_path)
    root = json.loads(manifest.read_text())
    shard_path = Path(root["quote_shard_dir"]) / "yes.json"

    def set_book(*, bid: float, ask: float, observed_at: str) -> None:
        shard = json.loads(shard_path.read_text())
        shard["quote"]["best_bid"] = bid
        shard["quote"]["best_ask"] = ask
        shard["quote"]["bids"] = [{"price": bid, "size": 10.0}]
        shard["quote"]["asks"] = [{"price": ask, "size": 10.0}]
        shard["quote"]["book_observed_at"] = observed_at
        shard["quote"]["created_at"] = observed_at
        shard["snapshot_sha256"] = hashlib.sha256(
            _canonical(
                {
                    key: value
                    for key, value in shard.items()
                    if key != "snapshot_sha256"
                }
            )
        ).hexdigest()
        shard_path.write_text(json.dumps(shard), encoding="utf-8")

    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        _registry(conn)
        for bid, ask, observed_at in (
            (0.40, 0.42, "2026-07-23T05:00:00Z"),
            (0.50, 0.52, "2026-07-23T05:00:01Z"),
            (0.40, 0.42, "2026-07-23T05:00:02Z"),
        ):
            set_book(bid=bid, ask=ask, observed_at=observed_at)
            receipt = materialize_market_evidence(
                conn,
                manifest_path=manifest,
                inventory_path=inventory,
                order_state_path=order,
                cash_phase_path=cash,
                **_freshness_args(),
                commit=True,
            )
            assert receipt["receipts"][0]["wrote_quote"] is True

        rows = conn.execute(
            "SELECT sequence,quote_observed_at FROM "
            "basket_official_ws_snapshot ORDER BY sequence"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            (0, "2026-07-23T05:00:00Z"),
            (1, "2026-07-23T05:00:01Z"),
            (2, "2026-07-23T05:00:02Z"),
        ]


def test_a_sibling_book_no_longer_speaks_for_this_token_s_tick() -> None:
    # The EGLC 2026-09-07 shape, and the reversal of the ZUUU-era rule that
    # pooled a scope's levels.  22C is stated 0.01 by the registry while the
    # same market's tail buckets are stated 0.001; a sibling quoting 0.004
    # must not drag 22C onto the finer grid, because the exchange floors
    # every rung it is built on back to 0.01.
    from bayes_basket_target.market_ingress import (
        derive_official_ws_price_tick_size,
    )

    book = {"asks": [], "bids": []}
    asks = [{"price": 0.03, "shares": 17.0}]
    bids = [{"price": 0.01, "shares": 516.14}]

    tick, source = derive_official_ws_price_tick_size(
        book, asks=asks, bids=bids, registry_tick=0.01
    )

    assert tick == 0.01
    assert source == "OFFICIAL_REGISTRY_TICK_SIZE"


def test_a_tail_bucket_keeps_the_finer_grid_the_registry_states_for_it() -> None:
    # The other half of the same market: the venue moves one token to 0.001
    # when its price sinks into the tail, and that token keeps it.
    from bayes_basket_target.market_ingress import (
        derive_official_ws_price_tick_size,
    )

    tick, source = derive_official_ws_price_tick_size(
        {"asks": [], "bids": []},
        asks=[{"price": 0.009, "shares": 40.0}],
        bids=[{"price": 0.004, "shares": 60.0}],
        registry_tick=0.001,
    )

    assert tick == 0.001
    assert source == "OFFICIAL_REGISTRY_TICK_SIZE"


def test_a_registry_tick_the_book_has_moved_past_falls_through() -> None:
    # The registry is a snapshot and can lag a tick_size_change.  Levels the
    # stated tick cannot express are better evidence than the stale row, and
    # refusing the scope over it would be worse than inferring.
    from bayes_basket_target.market_ingress import (
        derive_official_ws_price_tick_size,
    )

    tick, source = derive_official_ws_price_tick_size(
        {"asks": [], "bids": []},
        asks=[{"price": 0.004, "shares": 40.0}],
        bids=[{"price": 0.002, "shares": 60.0}],
        registry_tick=0.01,
    )

    assert tick == 0.001
    assert source == "OFFICIAL_WS_BOOK_GRID_DERIVED"


def test_inference_never_selects_a_tick_this_token_disproves() -> None:
    # With no stated tick at all, still the coarsest grid this token's own
    # levels prove -- never a finer, more permissive one.
    from bayes_basket_target.market_ingress import (
        derive_official_ws_price_tick_size,
    )

    tick, source = derive_official_ws_price_tick_size(
        {"asks": [], "bids": []},
        asks=[{"price": 0.28, "shares": 100.0}],
        bids=[{"price": 0.27, "shares": 100.0}],
    )

    assert tick == 0.01
    assert source == "OFFICIAL_WS_BOOK_GRID_DERIVED"


def test_an_explicit_exchange_tick_still_outranks_inference() -> None:
    from bayes_basket_target.market_ingress import (
        derive_official_ws_price_tick_size,
    )

    tick, source = derive_official_ws_price_tick_size(
        {"tick_size": 0.01, "asks": [], "bids": []},
        asks=[{"price": 0.28, "shares": 100.0}],
        bids=[{"price": 0.27, "shares": 100.0}],
        registry_tick=0.001,
    )

    assert tick == 0.01
    assert source == "OFFICIAL_WS_TICK_SIZE"


def test_bucket_without_an_exchange_sizing_rule_fails_closed() -> None:
    """A snapshot that cannot state the minimum lot is refused, not defaulted.

    The collector blocks a market that will not state ``orderMinSize``, and
    only ``source_eligible=1`` rows are read, so an eligible row without one
    can only come from a registry snapshot taken before the rule was
    collected.  Sizing against it would mint exactly the undersized legs the
    rule exists to stop, and the last good snapshot still carries the value,
    so refusing the new snapshot is the safe direction.
    """
    def _rows(sizing: dict) -> list[dict]:
        return [{
            "station_id": "TEST",
            "event_date": "2026-07-23",
            "outcome": "38°C",
            "yes_token": "yes-38",
            "lower": 38,
            "upper": 38,
            **sizing,
        }]

    quotes = {
        "yes-38": {
            "token_id": "yes-38",
            "side": "YES",
            "best_bid": 0.41,
            "best_ask": 0.42,
            "bids": [{"price": 0.41, "size": 100.0}],
            "asks": [{"price": 0.42, "size": 100.0}],
            "book_observed_at": "2026-07-23T05:00:00Z",
        },
    }

    buckets, _ = _scope_quote_values(_rows({"order_min_size": 5}), quotes)
    assert buckets[0]["min_order_size"] == 5.0

    for label, sizing in (
        ("absent", {}),
        ("null", {"order_min_size": None}),
        ("zero", {"order_min_size": 0}),
        ("negative", {"order_min_size": -5}),
        ("boolean", {"order_min_size": True}),
        ("unparseable", {"order_min_size": "five"}),
    ):
        with pytest.raises(ContractError, match="order_min_size"), \
                _subtest(label):
            _scope_quote_values(_rows(sizing), quotes)


class _subtest:
    """Name the shape under test in the traceback without a pytest fixture."""

    def __init__(self, label: str) -> None:
        self.label = label

    def __enter__(self) -> "_subtest":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            exc.args = (f"{self.label}: {exc.args[0] if exc.args else ''}",)
        return False


# ------------------------------------------------ no_registry_scope says which
#
# The fleet-wide fail-closed of 2026-09-13T12:09Z named 44 stations with the
# bare reason ``no_registry_scope``, while the persisted registry held tradable
# rows for every one of them in every snapshot either side.  That reason names
# three different faults; the message now says which one it was.  None of
# these tests change what is materialized or refused -- only what the refusal
# says.


def _miss_reason(conn, manifest, **kwargs) -> str:
    with pytest.raises(ContractError) as caught:
        materialize_official_ws_evidence_cache(conn, manifest_path=manifest, **kwargs)
    return str(caught.value)


def test_a_scope_with_no_registry_row_says_active_zero(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        _registry(conn)

        reason = _miss_reason(conn, manifest, station_id="NOPE", event_date="2026-07-23")

    assert reason.endswith(":no_registry_scope[2026-07-23 active=0]")


def test_a_day_that_has_not_started_says_not_started(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        _registry(conn)

        reason = _miss_reason(
            conn, manifest, station_id="TEST", event_date="2026-07-23",
            as_of_utc="2026-07-22T12:00:00Z",
        )

    assert reason.endswith(":no_registry_scope[2026-07-23 active=1 not_started=1]")


def test_a_day_state_that_raised_is_named_not_hidden(tmp_path: Path, monkeypatch) -> None:
    """``load_started_registry_rows`` swallows any exception from the day-state
    computation and drops the row as though its day had not begun.  The
    diagnosis judges each row with the same function, so the swallowed error
    shows up by type rather than disappearing into ``no_registry_scope``."""

    import bayes_basket_target.market_registry as registry

    def broken(*_args, **_kwargs):
        raise OSError("[Errno 24] Too many open files")

    monkeypatch.setattr(registry, "settlement_day_state", broken)
    manifest = _manifest(tmp_path)
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        _registry(conn)

        reason = _miss_reason(
            conn, manifest, station_id="TEST", event_date="2026-07-23",
            as_of_utc="2026-07-23T12:00:00Z",
        )

    assert reason.endswith(":no_registry_scope[2026-07-23 active=1 error_OSError=1]")


def test_the_diagnosis_survives_the_boards_colon_condenser(tmp_path: Path) -> None:
    """The board splits a production reason on colons and drops repeats; a
    colon inside the diagnosis would cut it apart before anyone read it."""

    manifest = _manifest(tmp_path)
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        _registry(conn)
        reason = _miss_reason(
            conn, manifest, station_id="TEST", event_date="2026-07-23",
            as_of_utc="2026-07-22T12:00:00Z",
        )

    token = reason[reason.index("["):]
    assert ":" not in token
    assert "scope_missing" in reason  # the production classifier matches on this
