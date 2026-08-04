from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from copy_follow_production import (
    CopyFollowProductionEngine,
    _hard_building_gate_reasons,
    _local_entry_window_reason,
    _copy_follow_ladder_levels,
    _matched_size,
    _order_status,
    _quote_book,
    _response_mapping,
    _target_quote_from_book,
)
from copy_follow_schema import apply_copy_follow_schema
from copy_follow_strategy import (
    extract_four_bucket_candidates,
    extract_four_bucket_candidates_from_positions,
    fetch_positions,
)
from weather_basket_yes_schema import apply_weather_basket_yes_schema
from scripts.market_ws_listener import LatestQuoteSnapshot


class FakeGateway:
    def __init__(self) -> None:
        self.payloads = []
        self.orders = {}
        self.fill_sizes = {}
        self.lookup_error = False
        self.cancel_calls = []
        self.terminal_ids = set()

    def place_order(self, payload):
        self.payloads.append(dict(payload))
        order_id = f"order-{len(self.payloads)}"
        self.orders[order_id] = dict(payload)
        return {"ok": True, "orderID": order_id, "status": "live", "size_matched": 0}

    def lookup_order(self, request):
        if self.lookup_error:
            raise RuntimeError("lookup unavailable")
        order_id = str(request.get("order_id") or "")
        payload = self.orders[order_id]
        matched = float(self.fill_sizes.get(order_id, 0.0))
        status = "cancelled" if order_id in self.terminal_ids else "filled" if matched + 0.005 >= float(payload["size"]) else "partially_filled" if matched > 0 else "live"
        return {"ok": True, "orderID": order_id, "status": status, "size_matched": matched, "price": payload["price"], "token_id": payload["token_id"]}

    def cancel_order(self, request):
        self.cancel_calls.append(dict(request))
        return {"ok": True, "orderID": request.get("order_id"), "cancelled": True, "status": "cancelled"}


def _config(path: Path, event_file: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": "weather_copy_follow_config_v1",
                "strategy_id": "weather_copy_follow_v1",
                "state": "enabled",
                "source": {"user": "0xabc", "activity_url": "https://data-api.example/activity", "page_size": 500, "max_pages": 1, "timeout_sec": 1},
                "required_bucket_count": 4,
                "lookahead_days": 2,
                "city_cap_usdc": 30,
                "execution": {
                    "order_type": "GTC",
                    "all_legs_required": False,
                    "buy_price_mode": "best_bid",
                    "sell_price_mode": "best_bid",
                    "active_building_max_age_sec": 900,
                    "main_bucket_min_source_notional_usdc": 5.0,
                    "main_bucket_min_source_share": 0.35,
                    "minimum_rebalance_notional_usdc": 0.25,
                    "allow_reductions": True,
                    "maximum_buy_price": 0.99,
                    "max_quote_age_sec": 5,
                    "minimum_order_shares": 1,
                    "strict_share_gap": 1,
                    "safety_floor_usdc": 0.01,
                    "settlement_floor_hard_gate": True,
                },
                "notifications": {"enabled": True, "channel": "codex_thread", "event_file": str(event_file)},
            }
        ),
        encoding="utf-8",
    )


def _dynamic_fixture(tmp_path: Path, amounts=(12, 9, 6, 3), *, stale: bool = False):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE reported_trajectory_market_registry(
          slug TEXT,event_date TEXT,outcome TEXT,city_slug TEXT,station_id TEXT,
          yes_token TEXT,source_eligible INTEGER,market_active INTEGER,market_accepting_orders INTEGER)"""
    )
    apply_copy_follow_schema(conn)
    event_date = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    parsed = datetime.fromisoformat(event_date)
    month, day = parsed.strftime("%B %d").split()
    event_slug = f"highest-temperature-in-test-city-on-{month.lower()}-{int(day)}-{event_date[:4]}"
    buckets = ["30°C", "31°C", "32°C", "33°C"]
    for index, bucket in enumerate(buckets):
        conn.execute(
            "INSERT INTO reported_trajectory_market_registry VALUES(?,?,?,?,?,?,?,?,?)",
            (event_slug, event_date, bucket, "test-city", "TEST", f"token-{index}", 1, 1, 1),
        )
    timestamp = int(datetime.now(timezone.utc).timestamp()) - (2000 if stale else 0)
    activity = [
        {
            "type": "TRADE",
            "side": "BUY",
            "outcome": "Yes",
            "eventSlug": event_slug,
            "title": f"Will the highest temperature in Test City be {bucket} on {month} {int(day)}?",
            "transactionHash": f"fixture-tx-{index}",
            "asset": f"fixture-asset-{index}",
            "timestamp": timestamp + index,
            "size": amount * 10,
            "price": 0.1,
            "usdcSize": amount,
        }
        for index, (bucket, amount) in enumerate(zip(buckets, amounts))
    ]
    event_file = tmp_path / "dynamic-events.jsonl"
    config_path = tmp_path / "dynamic-config.json"
    pause_file = tmp_path / "dynamic-paused"
    _config(config_path, event_file)
    gateway = FakeGateway()
    quote_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def fetcher(**_kwargs):
        return activity

    def quote_reader(token_id, *, max_age_sec):
        return {
            "token_id": token_id,
            "bids": [(0.08, 1000.0)],
            "asks": [(0.1, 1000.0)],
            "best_bid": 0.08,
            "best_ask": 0.1,
            "observed_at": quote_time,
        }

    engine = CopyFollowProductionEngine(
        conn,
        gateway=gateway,
        config_path=config_path,
        pause_file=pause_file,
        activity_fetcher=fetcher,
        quote_reader=quote_reader,
    )
    return engine, gateway, conn, activity


def test_dynamic_existing_lot_re_solves_floor_and_never_emits_micro_buy(tmp_path: Path) -> None:
    engine, _gateway, _conn, activity = _dynamic_fixture(tmp_path)
    config = json.loads(engine.config_path.read_text(encoding="utf-8"))
    config["execution"]["minimum_order_shares"] = 5
    candidate = extract_four_bucket_candidates(activity, source_user="0xabc")[0]
    prepared = {
        "target_quantities": {"30°C": 13.58, "31°C": 13.58, "32°C": 14.58, "33°C": 15.58},
        "main_bucket": "33°C",
        "secondary_bucket": "32°C",
        "safety_floor_usdc": 0.01,
        "initial_entry": False,
        "quotes_by_bucket": {
            "30°C": {"best_bid": 0.16},
            "31°C": {"best_bid": 0.40},
            "32°C": {"best_bid": 0.27},
            "33°C": {"best_bid": 0.09},
        },
        "token_by_bucket": {bucket: f"token-{index}" for index, bucket in enumerate(candidate.buckets)},
    }
    managed = {
        "rows": [{"bucket": "32°C", "size_matched": 13.25}],
        "holdings_by_bucket": {"32°C": 13.25},
        "projected_holdings_by_bucket": {"32°C": 13.25},
        "projected_net_cost_usdc": 3.71,
        "open_rows": [],
    }

    result = engine._desired_actions(candidate, prepared, managed, config)

    assert result["actions"]
    assert all(item["side"] == "BUY" for item in result["actions"])
    assert all(float(item["quantity"]) >= 5.0 for item in result["actions"])
    assert result["actual_settlement_floor_passed"] is False
    assert result["post_fill_settlement_floor_passed"] is True
    simulated_cost = managed["projected_net_cost_usdc"] + sum(
        float(item["estimated_notional_usdc"]) for item in result["actions"]
    )
    simulated_holdings = dict(managed["projected_holdings_by_bucket"])
    for item in result["actions"]:
        simulated_holdings[item["bucket"]] = simulated_holdings.get(item["bucket"], 0.0) + float(item["quantity"])
    assert min(simulated_holdings[bucket] - simulated_cost for bucket in candidate.buckets) >= 0.01


def test_incomplete_basket_cancels_reductions_but_never_sells_a_held_leg(tmp_path: Path) -> None:
    engine, _gateway, _conn, activity = _dynamic_fixture(tmp_path)
    config = json.loads(engine.config_path.read_text(encoding="utf-8"))
    candidate = extract_four_bucket_candidates(activity, source_user="0xabc")[0]
    prepared = {
        "target_quantities": {"30°C": 5.0, "31°C": 5.0, "32°C": 6.0, "33°C": 7.0},
        "main_bucket": "33°C",
        "secondary_bucket": "32°C",
        "safety_floor_usdc": 0.01,
        "initial_entry": False,
        "quotes_by_bucket": {
            bucket: {"best_bid": 0.08, "best_ask": 0.10}
            for bucket in candidate.buckets
        },
        "token_by_bucket": {bucket: f"token-{index}" for index, bucket in enumerate(candidate.buckets)},
    }
    managed = {
        "rows": [{"bucket": "32°C", "side": "BUY", "size_matched": 10.0}],
        "holdings_by_bucket": {"32°C": 10.0, "33°C": 10.0},
        "projected_holdings_by_bucket": {"32°C": 10.0, "33°C": 10.0},
        "net_cost_usdc": 1.6,
        "projected_net_cost_usdc": 1.6,
        "open_rows": [{
            "local_order_id": "pending-sell-32",
            "bucket": "32°C",
            "side": "SELL",
            "requested_size": 4.0,
            "size_matched": 0.0,
            "limit_price": 0.08,
        }],
    }

    result = engine._desired_actions(candidate, prepared, managed, config)

    assert result["actions"] == []
    assert [row["local_order_id"] for row in result["cancellations"]] == ["pending-sell-32"]
    assert "copy_follow_preserve_incomplete_basket" in result["reason_codes"]
    assert "copy_follow_waiting_for_fill_reconciliation" in result["reason_codes"]


def test_nested_exchange_lookup_response_preserves_cancelled_zero_fill() -> None:
    raw = {
        "ok": True,
        "orderID": "exchange-1",
        "status": "CANCELED",
        "response": {
            "id": "exchange-1",
            "status": "CANCELED",
            "original_size": "6.37",
            "size_matched": "0",
        },
    }

    normalized = _response_mapping(raw)

    assert _order_status(normalized) == "canceled"
    assert _matched_size(normalized) == 0.0


def test_submitted_unconfirmed_reconcile_uses_exact_exchange_order_lookup(tmp_path: Path) -> None:
    class SubmittedUnconfirmedGateway(FakeGateway):
        def lookup_order(self, request):
            order_id = str(request.get("order_id") or "")
            return {
                "ok": True,
                "orderID": order_id,
                "status": "submitted_unconfirmed",
                "reconcile_required": False,
            }

        def lookup_exchange_order(self, request):
            order_id = str(request.get("order_id") or "")
            return {
                "ok": True,
                "orderID": order_id,
                "status": "open",
                "response": None,
            }

    engine, _gateway, _conn, _activity = _dynamic_fixture(tmp_path)
    gateway = SubmittedUnconfirmedGateway()
    engine.gateway = gateway

    first = engine.run_once()
    assert first["places_orders"] is True
    plan_id = first["new_plan_results"][0]["plan_id"]

    sync = engine._sync_open_orders(plan_id)

    assert sync["ok"] is True
    assert sync["reason_codes"] == []


def test_missing_idempotency_fill_reconciles_by_exact_exchange_identity(tmp_path: Path) -> None:
    class EvictedIdempotencyGateway(FakeGateway):
        def lookup_order(self, request):
            return {
                "ok": False,
                "found": False,
                "pending": False,
                "idempotency_key": request.get("idempotency_key"),
            }

        def lookup_exchange_order(self, request):
            order_id = str(request.get("order_id") or "")
            return {"ok": True, "orderID": order_id, "status": "open", "response": None}

    engine, _gateway, _conn, _activity = _dynamic_fixture(tmp_path)
    engine.gateway = EvictedIdempotencyGateway()

    first = engine.run_once()
    assert first["places_orders"] is True
    plan_id = first["new_plan_results"][0]["plan_id"]

    sync = engine._sync_open_orders(plan_id)

    assert sync["ok"] is True
    assert sync["reason_codes"] == []


def test_copy_follow_dynamic_buy_uses_three_official_bid_levels_and_reprices(tmp_path: Path) -> None:
    engine, gateway, _conn, _activity = _dynamic_fixture(tmp_path)
    config = json.loads(engine.config_path.read_text(encoding="utf-8"))
    config["execution"]["buy_ladder"] = {
        "enabled": True,
        "max_levels": 3,
        "allocation_weights": [0.5, 0.3, 0.2],
        "source": "official_ws_bid_levels",
        "reprice_on_quote_change": True,
    }
    config["execution"]["minimum_rebalance_notional_usdc"] = 0.01
    config["execution"]["minimum_order_shares"] = 0.1
    engine.config_path.write_text(json.dumps(config), encoding="utf-8")
    quote_state = {"bids": [(0.08, 1000.0), (0.07, 1000.0), (0.06, 1000.0)]}

    def ladder_quote(token_id, *, max_age_sec):
        return {
            "token_id": token_id,
            "bids": list(quote_state["bids"]),
            "asks": [(0.1, 1000.0)],
            "best_bid": quote_state["bids"][0][0],
            "best_ask": 0.1,
            "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    engine.quote_reader = ladder_quote
    first = engine.run_once()
    assert first["places_orders"] is True
    assert all(payload["order_type"] == "FAK" for payload in gateway.payloads)
    for order_id in gateway.orders:
        gateway.terminal_ids.add(order_id)

    second = engine.run_once()
    dynamic_payloads = gateway.payloads[4:]
    assert second["places_orders"] is True
    assert dynamic_payloads
    assert all(payload["order_type"] == "GTC" for payload in dynamic_payloads)
    assert {payload["price"] for payload in dynamic_payloads} == {0.08, 0.07, 0.06}
    assert 8 <= len(dynamic_payloads) <= 12

    quote_state["bids"] = [(0.075, 1000.0), (0.065, 1000.0), (0.055, 1000.0)]
    third = engine.run_once()
    assert third["places_orders"] is False
    assert len(gateway.payloads) == 4 + len(dynamic_payloads)
    fourth = engine.run_once()
    assert fourth["places_orders"] is False
    assert len(gateway.payloads) == 4 + len(dynamic_payloads)
    assert "copy_follow_waiting_for_fill_reconciliation" in fourth["new_plan_results"][0]["rebalance"]["reason_codes"]
    assert _copy_follow_ladder_levels(
        {"best_bid": 0.08, "bids": [(0.08, 10), (0.07, 10), (0.06, 10)]},
        config["execution"],
    ) == [
        {"price": 0.08, "weight": 0.5, "level": 0.0},
        {"price": 0.07, "weight": 0.3, "level": 1.0},
        {"price": 0.06, "weight": 0.2, "level": 2.0},
    ]


def test_copy_follow_heavy_bucket_falls_back_to_best_bid_after_stale_ladder(tmp_path: Path) -> None:
    engine, gateway, conn, _activity = _dynamic_fixture(tmp_path)
    config = json.loads(engine.config_path.read_text(encoding="utf-8"))
    config["execution"]["buy_ladder"] = {
        "enabled": True,
        "max_levels": 3,
        "allocation_weights": [0.5, 0.3, 0.2],
        "source": "official_ws_bid_levels",
        "reprice_on_quote_change": True,
    }
    config["execution"]["heavy_bucket_best_bid_fallback"] = {
        "enabled": True,
        "after_sec": 1.0,
        "bucket_role": "main_bucket",
    }
    config["execution"]["minimum_rebalance_notional_usdc"] = 0.01
    config["execution"]["minimum_order_shares"] = 0.1
    config["execution"]["settlement_floor_hard_gate"] = False
    engine.config_path.write_text(json.dumps(config), encoding="utf-8")

    first = engine.run_once()
    assert first["places_orders"] is True
    for order_id in list(gateway.orders):
        gateway.terminal_ids.add(order_id)

    second = engine.run_once()
    dynamic_payloads = gateway.payloads[4:]
    assert dynamic_payloads
    assert sum(payload["price"] == 0.08 for payload in dynamic_payloads) >= 4

    old = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
    conn.execute(
        "UPDATE copy_follow_orders SET created_at=? WHERE state='open' AND side='BUY'",
        (old,),
    )
    conn.commit()

    third = engine.run_once()
    assert third["places_orders"] is True
    assert gateway.cancel_calls
    main_bucket = "30°C"
    assert conn.execute(
        "SELECT state FROM copy_follow_orders WHERE bucket=? AND limit_price=? ORDER BY created_at DESC LIMIT 1",
        (main_bucket, 0.08),
    ).fetchone()[0] == "cancelled"

    before_fallback = len(gateway.payloads)
    fourth = engine.run_once()
    fallback_payloads = gateway.payloads[before_fallback:]
    assert len(fallback_payloads) == 1
    assert fallback_payloads[0]["side"] == "BUY"
    assert fallback_payloads[0]["price"] == 0.08
    assert fallback_payloads[0]["order_type"] == "GTC"
    assert fallback_payloads[0]["allow_market"] is False
    assert fourth["places_orders"] is True


def test_copy_follow_pending_orders_are_not_used_for_floor_or_replenishment(tmp_path: Path) -> None:
    engine, _gateway, _conn, activity = _dynamic_fixture(tmp_path)
    candidate = extract_four_bucket_candidates(activity, source_user="0xabc")[0]
    prepared = {
        "target_quantities": {bucket: 10.0 for bucket in candidate.buckets},
        "main_bucket": "33°C",
        "secondary_bucket": "32°C",
        "safety_floor_usdc": 0.01,
        "initial_entry": False,
        "quotes_by_bucket": {
            bucket: {"best_bid": 0.08, "best_ask": 0.10}
            for bucket in candidate.buckets
        },
        "token_by_bucket": {bucket: f"token-{index}" for index, bucket in enumerate(candidate.buckets)},
    }
    managed = {
        "rows": [{"bucket": "30°C", "side": "BUY", "size_matched": 10.0}],
        "holdings_by_bucket": {"30°C": 10.0},
        "net_cost_usdc": 0.8,
        # This intentionally includes a fictional future fill.  It must not
        # authorize any new action while the exchange order remains open.
        "projected_holdings_by_bucket": {bucket: 10.0 for bucket in candidate.buckets},
        "projected_net_cost_usdc": 3.2,
        "open_rows": [{
            "local_order_id": "pending-buy-31",
            "bucket": "31°C",
            "side": "BUY",
            "requested_size": 10.0,
            "size_matched": 0.0,
            "limit_price": 0.08,
        }],
    }

    result = engine._desired_actions(
        candidate,
        prepared,
        managed,
        json.loads(engine.config_path.read_text()),
    )

    assert result["actions"] == []
    assert "copy_follow_waiting_for_fill_reconciliation" in result["reason_codes"]
    assert result["actual_holdings_by_bucket"]["31°C"] == 0.0
    assert result["actual_net_cost_usdc"] == 0.8
    assert result["actual_settlement_floor_passed"] is False


def test_stale_source_activity_is_warning_but_core_and_four_buckets_remain_hard() -> None:
    assert _hard_building_gate_reasons({"reason_codes": ["copy_follow_source_not_actively_building"]}) == []
    assert _hard_building_gate_reasons(
        {
            "reason_codes": [
                "copy_follow_source_not_actively_building",
                "copy_follow_main_bucket_share_not_large_enough",
            ]
        }
    ) == []


def test_local_peak_window_blocks_same_day_entry_without_weather_fields() -> None:
    before_cutoff = datetime(2026, 7, 20, 9, 59, tzinfo=timezone.utc)
    at_cutoff = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)
    assert _local_entry_window_reason(
        "2026-07-20", "Asia/Shanghai", now=before_cutoff
    ) is None
    assert _local_entry_window_reason(
        "2026-07-20", "Asia/Shanghai", now=at_cutoff
    ) == "copy_follow_local_peak_window_passed"
    assert _local_entry_window_reason(
        "2026-07-19", "Asia/Shanghai", now=before_cutoff
    ) == "copy_follow_local_event_date_expired"
    assert _local_entry_window_reason(
        "2026-07-21", "Asia/Shanghai", now=at_cutoff
    ) is None


def test_copy_follow_persists_plan_submits_four_legs_and_writes_codex_event(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE reported_trajectory_market_registry(
          slug TEXT,event_date TEXT,outcome TEXT,city_slug TEXT,station_id TEXT,
          yes_token TEXT,source_eligible INTEGER,market_active INTEGER,market_accepting_orders INTEGER)"""
    )
    apply_copy_follow_schema(conn)
    apply_weather_basket_yes_schema(conn)
    event_date = (datetime.now() + timedelta(days=1)).date().isoformat()
    month, day = datetime.fromisoformat(event_date).strftime("%B %d").split()
    event_slug = f"highest-temperature-in-test-city-on-{month.lower()}-{int(day)}-{event_date[:4]}"
    buckets = ["30°C", "31°C", "32°C", "33°C"]
    for index, bucket in enumerate(buckets):
        conn.execute(
            "INSERT INTO reported_trajectory_market_registry VALUES(?,?,?,?,?,?,?,?,?)",
            (event_slug, event_date, bucket, "test-city", "TEST", f"token-{index}", 1, 1, 1),
        )
    # The newest TAF revision must win; MAX(peak_c) would incorrectly retain
    # the stale 40°C revision.  ECMWF must likewise use the newest model run,
    # then take that run's daily maximum rather than MAX across old runs.
    conn.execute(
        """INSERT INTO amsc_taf_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("taf-old", event_date, "TEST", "amsc_taf", "normal", "", "2026-07-19T09:00:00Z", "2026-07-19T09:01:00Z", "", 40.0, "2026-07-21T12:00:00Z", "normal", "{}", "old", "TAF OLD", "", "{}", "available", "2026-07-19T09:01:00Z"),
    )
    conn.execute(
        """INSERT INTO amsc_taf_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("taf-new", event_date, "TEST", "amsc_taf", "normal", "", "2026-07-20T09:00:00Z", "2026-07-20T09:01:00Z", "", 31.0, "2026-07-21T12:00:00Z", "normal", "{}", "new", "TAF NEW", "", "{}", "available", "2026-07-20T09:01:00Z"),
    )
    conn.execute(
        """INSERT INTO official_nwp_station_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("nwp-old", "ecmwf", "TEST", "TEST", event_date, "2026-07-19T00:00:00Z", "2026-07-21T12:00:00Z", 40.0, "{}", "old", "old", "old", "{}", "available", "2026-07-19T01:00:00Z"),
    )
    conn.execute(
        """INSERT INTO official_nwp_station_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("nwp-new-a", "ecmwf", "TEST", "TEST", event_date, "2026-07-20T00:00:00Z", "2026-07-21T12:00:00Z", 29.0, "{}", "new-a", "new-a", "new-a", "{}", "available", "2026-07-20T01:00:00Z"),
    )
    conn.execute(
        """INSERT INTO official_nwp_station_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("nwp-new-b", "ecmwf", "TEST", "TEST", event_date, "2026-07-20T00:00:00Z", "2026-07-21T15:00:00Z", 30.0, "{}", "new-b", "new-b", "new-b", "{}", "available", "2026-07-20T01:00:00Z"),
    )
    conn.commit()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    now_timestamp = int(datetime.now(timezone.utc).timestamp())
    activity = [
        {"type": "TRADE", "side": "BUY", "outcome": "Yes", "eventSlug": event_slug,
         "title": f"Will the highest temperature in Test City be {bucket} on {month} {int(day)}?",
         "transactionHash": f"tx-{index}", "asset": f"asset-{index}", "timestamp": now_timestamp + index,
         "size": amount * 10, "price": 0.1, "usdcSize": amount}
        for index, (bucket, amount) in enumerate(zip(buckets, (12, 9, 6, 3)))
    ]
    event_file = tmp_path / "copy-follow-events.jsonl"
    config_path = tmp_path / "copy-follow.json"
    pause_file = tmp_path / "paused"
    _config(config_path, event_file)
    gateway = FakeGateway()

    def fetcher(**_kwargs):
        return activity

    def quote_reader(token_id, *, max_age_sec):
        return {"token_id": token_id, "bids": [(0.08, 1000.0)], "asks": [(0.1, 1000.0)], "best_bid": 0.08, "best_ask": 0.1, "observed_at": now}

    engine = CopyFollowProductionEngine(
        conn,
        gateway=gateway,
        config_path=config_path,
        pause_file=pause_file,
        activity_fetcher=fetcher,
        quote_reader=quote_reader,
    )
    result = engine.run_once()
    assert result["ok"] is True
    assert result["new_plan_results"][0]["state"] == "submitted"
    assert len(gateway.payloads) == 4
    assert all(payload["order_type"] == "FAK" for payload in gateway.payloads)
    assert all(payload["price"] == 0.1 for payload in gateway.payloads)
    assert all(payload["allow_market"] is True for payload in gateway.payloads)
    assert all(payload["side"] == "BUY" for payload in gateway.payloads)
    row = conn.execute("SELECT state,total_budget_usdc FROM copy_follow_plans").fetchone()
    assert row[0] == "submitted"
    assert row[1] <= 30
    assert event_file.exists()
    notice_text = event_file.read_text(encoding="utf-8")
    assert "TAF=" in notice_text
    assert "inside_main_bucket" in notice_text
    assert "ECMWF=" in notice_text
    notice = conn.execute("SELECT channel FROM copy_follow_notifications ORDER BY created_at LIMIT 1").fetchone()
    assert notice[0] == "codex_thread"
    saved = json.loads(conn.execute("SELECT plan_json FROM copy_follow_plans").fetchone()[0])
    context = saved["forecast_context"]
    assert context["taf"]["peak_c"] == 31.0
    assert context["ecmwf"]["peak_c"] == 30.0
    assert context["taf"]["comparison"]["relation"] == "above_main_bucket"
    assert context["ecmwf"]["comparison"]["relation"] == "inside_main_bucket"
    second = engine.run_once()
    assert second["new_plan_results"][0]["rebalance"]["actions"] == []
    assert len(gateway.payloads) == 4


def test_copy_follow_pause_is_independent_of_bayes(tmp_path: Path) -> None:
    config_path = tmp_path / "copy-follow.json"
    event_file = tmp_path / "events.jsonl"
    pause_file = tmp_path / "paused"
    _config(config_path, event_file)
    pause_file.write_text("pause\n", encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    engine = CopyFollowProductionEngine(conn, config_path=config_path, pause_file=pause_file, activity_fetcher=lambda **_: (_ for _ in ()).throw(AssertionError("paused line must not fetch")))
    result = engine.run_once()
    assert result["state"] == "disabled"
    assert result["places_orders"] is False


def test_copy_follow_stale_source_is_allowed_without_copying_order_type(tmp_path: Path) -> None:
    engine, gateway, conn, activity = _dynamic_fixture(tmp_path, stale=True)
    first = engine.run_once()
    assert first["places_orders"] is True
    assert len(gateway.payloads) == 4
    assert conn.execute("SELECT state FROM copy_follow_plans").fetchone()[0] == "submitted"
    assert all(payload["order_type"] == "FAK" and payload["post_only"] is False for payload in gateway.payloads)

    fresh = int(datetime.now(timezone.utc).timestamp())
    for index, row in enumerate(activity):
        row["timestamp"] = fresh + index
    second = engine.run_once()
    assert second["places_orders"] is False
    assert len(gateway.payloads) == 4


def test_copy_follow_initial_market_entry_does_not_require_a_best_bid(tmp_path: Path) -> None:
    engine, gateway, _conn, _activity = _dynamic_fixture(tmp_path)

    def asks_only(token_id, *, max_age_sec):
        return {
            "token_id": token_id,
            "asks": [(0.1, 1000.0)],
            "best_ask": 0.1,
            "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    engine.quote_reader = asks_only
    result = engine.run_once()
    assert result["places_orders"] is True
    assert len(gateway.payloads) == 4
    assert all(payload["order_type"] == "FAK" and payload["allow_market"] is True for payload in gateway.payloads)


def test_settlement_floor_solver_failure_is_a_hard_gate_for_new_plan(tmp_path: Path) -> None:
    engine, gateway, conn, _activity = _dynamic_fixture(tmp_path)

    def invalid_floor_quote(token_id, *, max_age_sec):
        return {
            "token_id": token_id,
            "bids": [(0.25, 1000.0)],
            "asks": [(0.25, 1000.0)],
            "best_bid": 0.25,
            "best_ask": 0.25,
            "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    engine.quote_reader = invalid_floor_quote
    result = engine.run_once()

    assert result["places_orders"] is False
    assert result["new_plan_results"][0]["state"] == "blocked"
    assert "copy_follow_floor_solver_not_converged" in result["new_plan_results"][0]["reason_codes"]
    assert gateway.payloads == []
    assert conn.execute("SELECT state FROM copy_follow_plans").fetchone()[0] == "blocked"


def test_settlement_floor_failure_cancels_open_orders_and_freezes_plan(tmp_path: Path) -> None:
    engine, gateway, conn, _activity = _dynamic_fixture(tmp_path)
    first = engine.run_once()
    assert first["places_orders"] is True
    assert len(gateway.payloads) == 4

    def invalid_floor_quote(token_id, *, max_age_sec):
        return {
            "token_id": token_id,
            "bids": [(0.25, 1000.0)],
            "asks": [(0.25, 1000.0)],
            "best_bid": 0.25,
            "best_ask": 0.25,
            "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    engine.quote_reader = invalid_floor_quote
    second = engine.run_once()

    assert second["places_orders"] is True
    assert second["new_plan_results"][0]["state"] == "blocked"
    assert second["new_plan_results"][0]["execution_status"] == "blocked_by_settlement_floor"
    assert "copy_follow_floor_solver_not_converged" in second["new_plan_results"][0]["reason_codes"]
    assert len(gateway.payloads) == 4
    assert len(gateway.cancel_calls) == 4
    assert {row[0] for row in conn.execute("SELECT state FROM copy_follow_orders")} == {"cancelled"}
    assert conn.execute("SELECT state FROM copy_follow_plans").fetchone()[0] == "blocked"


def test_dynamic_settlement_floor_failure_cancels_orders_before_any_new_action(tmp_path: Path) -> None:
    engine, gateway, conn, _activity = _dynamic_fixture(tmp_path)
    first = engine.run_once()
    assert first["places_orders"] is True

    with patch.object(
        engine,
        "_desired_actions",
        return_value={
            "actions": [],
            "cancellations": [],
            "reason_codes": ["copy_follow_rebalance_settlement_floor_not_met"],
        },
    ):
        second = engine.run_once()

    assert second["places_orders"] is True
    assert second["new_plan_results"][0]["state"] == "blocked"
    assert len(gateway.payloads) == 4
    assert len(gateway.cancel_calls) == 4
    assert conn.execute("SELECT state FROM copy_follow_plans").fetchone()[0] == "blocked"


def test_copy_follow_publishes_new_candidate_before_quote_precheck(tmp_path: Path) -> None:
    engine, _gateway, conn, _activity = _dynamic_fixture(tmp_path)

    def quote_reader(_token_id, *, max_age_sec):
        assert conn.execute("SELECT 1 FROM copy_follow_plans").fetchone() is not None
        return None

    engine.quote_reader = quote_reader

    result = engine.run_once()

    assert result["places_orders"] is False
    assert result["new_plan_results"][0]["state"] == "planned"
    assert result["new_plan_results"][0]["execution_status"] == "waiting_for_clob_evidence"
    assert result["new_plan_results"][0]["reason_codes"] == [
        "copy_follow_clob_quote_missing:30°C",
        "copy_follow_clob_quote_missing:31°C",
        "copy_follow_clob_quote_missing:32°C",
        "copy_follow_clob_quote_missing:33°C",
    ]
    row = conn.execute(
        "SELECT state,buckets_json,plan_json FROM copy_follow_plans"
    ).fetchone()
    assert row[0] == "planned"
    assert json.loads(row[1]) == ["30°C", "31°C", "32°C", "33°C"]


def test_positions_source_extracts_exact_four_buckets_without_activity_fetch() -> None:
    positions = [
        {
            "eventSlug": "highest-temperature-in-test-city-on-july-22-2026",
            "outcome": bucket,
            "size": 10 + index,
            "avgPrice": 0.1,
            "initialValue": amount,
        }
        for index, (bucket, amount) in enumerate(
            zip(("30°C", "31°C", "32°C", "33°C"), (12, 9, 6, 3))
        )
    ]
    candidates = extract_four_bucket_candidates_from_positions(
        positions,
        source_user="0xabc",
        today_et=datetime(2026, 7, 20).date(),
        lookahead_days=2,
    )
    assert len(candidates) == 1
    assert candidates[0].buckets == ("30°C", "31°C", "32°C", "33°C")
    assert candidates[0].source_notional_by_bucket["30°C"] == 12
    assert candidates[0].main_bucket == "30°C"


def test_production_engine_defaults_to_positions_source(tmp_path: Path) -> None:
    engine, gateway, _conn, activity = _dynamic_fixture(tmp_path)
    positions = [
        {
            "eventSlug": row["eventSlug"],
            "outcome": row["title"].split(" be ", 1)[1].split(" on ", 1)[0],
            "size": row["size"],
            "avgPrice": row["price"],
            "initialValue": row["usdcSize"],
        }
        for row in activity
    ]
    engine.activity_fetcher = None
    engine.positions_fetcher = lambda **_kwargs: positions
    result = engine.run_once()
    assert result["source_mode"] == "positions"
    assert result["places_orders"] is True
    assert len(gateway.payloads) == 4


def test_production_copy_mode_uses_source_shares_without_settlement_floor_gate(tmp_path: Path) -> None:
    engine, gateway, _conn, activity = _dynamic_fixture(tmp_path)
    config = json.loads(engine.config_path.read_text(encoding="utf-8"))
    config["execution"]["settlement_floor_hard_gate"] = False
    config["execution"]["copy_weight_mode"] = "source_capital_scale_and_share_ratio"
    config["execution"]["allow_reductions"] = False
    config["execution"]["enforce_city_cap"] = False
    engine.config_path.write_text(json.dumps(config), encoding="utf-8")

    positions = [
        {
            "eventSlug": row["eventSlug"],
            "outcome": row["title"].split(" be ", 1)[1].split(" on ", 1)[0],
            "size": 10 + index,
            "avgPrice": 0.1,
            "initialValue": amount,
        }
        for index, (row, amount) in enumerate(zip(activity, (12, 9, 6, 3)))
    ]
    engine.activity_fetcher = None
    engine.positions_fetcher = lambda **_kwargs: positions

    def one_quarter_quote(token_id, *, max_age_sec):
        return {
            "token_id": token_id,
            "bids": [(0.9, 1000.0)],
            "asks": [(0.9, 1000.0)],
            "best_bid": 0.9,
            "best_ask": 0.9,
            "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    engine.quote_reader = one_quarter_quote
    result = engine.run_once()

    assert result["places_orders"] is True
    # Independent copy-follow legs are submitted concurrently; completion
    # order is intentionally not a contract.
    assert sorted(payload["size"] for payload in gateway.payloads) == [10.0, 11.0, 12.0, 13.0]
    assert all("floor" not in str(payload.get("reason_codes") or []).lower() for payload in result["new_plan_results"])
    # The projected settlement profit is still reported for audit, but a
    # negative floor projection must not block source-ratio copy mode.
    assert result["new_plan_results"][0]["minimum_settlement_profit_usdc"] < 0


def test_copy_mode_never_emits_sell_when_source_target_is_lower(tmp_path: Path) -> None:
    engine, _gateway, _conn, activity = _dynamic_fixture(tmp_path)
    candidate = extract_four_bucket_candidates(
        activity,
        source_user="0xabc",
        lookahead_days=2,
    )[0]
    config = json.loads(engine.config_path.read_text(encoding="utf-8"))
    config["execution"].update(
        {
            "settlement_floor_hard_gate": False,
            "allow_reductions": False,
            "enforce_city_cap": False,
        }
    )
    quotes = {
        bucket: {
            "best_bid": 0.5,
            "best_ask": 0.5,
            "bids": [[0.5, 1000.0]],
        }
        for bucket in candidate.buckets
    }
    prepared = {
        "target_quantities": {bucket: 1.0 for bucket in candidate.buckets},
        "safety_floor_usdc": 0.0,
        "main_bucket": candidate.buckets[0],
        "secondary_bucket": candidate.buckets[1],
        "initial_entry": False,
        "quotes_by_bucket": quotes,
        "market_price_cap_by_bucket": {bucket: 0.5 for bucket in candidate.buckets},
        "token_by_bucket": {bucket: f"token-{index}" for index, bucket in enumerate(candidate.buckets)},
    }
    managed = {
        "holdings_by_bucket": {bucket: 10.0 for bucket in candidate.buckets},
        "net_cost_usdc": 4.0,
        "open_rows": [],
        "rows": [],
    }
    rebalance = engine._desired_actions(candidate, prepared, managed, config)
    assert rebalance["actions"] == []
    assert all(item.get("side") != "SELL" for item in rebalance["actions"])
    assert any("sell_disabled" in reason for reason in rebalance["reason_codes"])


def test_exit_liquidity_posts_fixed_gtc_sells_without_rebalance_reduction(tmp_path: Path) -> None:
    engine, _gateway, _conn, activity = _dynamic_fixture(tmp_path)
    candidate = extract_four_bucket_candidates(
        activity,
        source_user="0xabc",
        lookahead_days=2,
    )[0]
    config = json.loads(engine.config_path.read_text(encoding="utf-8"))
    config["execution"].update(
        {
            "settlement_floor_hard_gate": False,
            "allow_reductions": False,
            "enforce_city_cap": False,
            "exit_liquidity": {"enabled": True, "price": 0.997, "order_type": "GTC"},
        }
    )
    prepared = {
        "token_by_bucket": {bucket: f"token-{index}" for index, bucket in enumerate(candidate.buckets)},
    }
    managed = {
        "holdings_by_bucket": {bucket: 10.0 for bucket in candidate.buckets},
        "open_rows": [],
    }
    exit_plan = engine._desired_exit_liquidity(candidate, prepared, managed, config)
    assert len(exit_plan["actions"]) == 4
    assert all(item["side"] == "SELL" for item in exit_plan["actions"])
    assert all(item["limit_price"] == 0.997 for item in exit_plan["actions"])
    assert all(item["order_type"] == "GTC" and item["role"] == "exit_liquidity" for item in exit_plan["actions"])
    order_rows = engine._prepare_dynamic_order_rows(
        candidate,
        {"rebalance": {"actions": []}, "exit_liquidity": exit_plan},
        datetime.now(timezone.utc).isoformat(),
    )
    assert len(order_rows) == 4
    submitted = engine._submit_dynamic(candidate, order_rows)
    assert submitted["state"] == "submitted"
    assert all(payload["side"] == "SELL" for payload in engine.gateway.payloads)
    assert all(payload["price"] == 0.997 for payload in engine.gateway.payloads)
    assert all(payload["order_role"] == "exit_liquidity" for payload in engine.gateway.payloads)

    pending_exit = {
        "local_order_id": "exit-1",
        "idempotency_key": "copy_exit_liquidity_idem_existing",
        "bucket": candidate.buckets[0],
        "side": "SELL",
        "limit_price": 0.997,
        "requested_size": 10.0,
        "size_matched": 0.0,
    }
    prepared_rebalance = {
        "target_quantities": {bucket: 10.0 for bucket in candidate.buckets},
        "safety_floor_usdc": 0.0,
        "main_bucket": candidate.buckets[0],
        "secondary_bucket": candidate.buckets[1],
        "initial_entry": False,
        "quotes_by_bucket": {
            bucket: {"best_bid": 0.5, "best_ask": 0.5, "bids": [[0.5, 1000.0]]}
            for bucket in candidate.buckets
        },
    }
    rebalance = engine._desired_actions(
        candidate,
        prepared_rebalance,
        {**managed, "open_rows": [pending_exit], "net_cost_usdc": 4.0},
        config,
    )
    assert rebalance["cancellations"] == []
    assert "copy_follow_waiting_for_fill_reconciliation" not in rebalance["reason_codes"]


def test_fetch_positions_uses_one_bounded_positions_page() -> None:
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'[{"asset":"token","outcome":"38\xc2\xb0C","size":1}]'

    def opener(request, *, timeout):
        calls.append((request.full_url, timeout))
        return Response()

    rows = fetch_positions(
        user="0xABC",
        page_size=500,
        max_pages=2,
        timeout_sec=6,
        opener=opener,
    )
    assert len(rows) == 1
    assert len(calls) == 1
    assert "/positions?" in calls[0][0]
    assert "sizeThreshold=0" in calls[0][0]
    assert "redeemable=false" in calls[0][0]
    assert "/activity" not in calls[0][0]


def test_target_quote_book_keeps_full_ask_depth_for_floor_solver() -> None:
    quote = _target_quote_from_book(
        "token",
        {"bids": {0.08: 12}, "asks": {0.10: 3, 0.11: 4}},
        "2026-07-21T00:00:00Z",
    )
    assert quote is not None
    assert quote["asks"] == [(0.1, 3.0), (0.11, 4.0)]
    assert quote["best_bid"] == 0.08


def test_missing_local_quotes_call_one_four_bucket_target_prewarm(tmp_path: Path) -> None:
    engine, gateway, _conn, activity = _dynamic_fixture(tmp_path)
    quote_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    calls = []

    def target_reader(tokens, *, timeout_sec):
        calls.append((list(tokens), timeout_sec))
        return {
            token: {
                "token_id": token,
                "bids": [(0.08, 1000.0)],
                "asks": [(0.1, 1000.0)],
                "best_bid": 0.08,
                "best_ask": 0.1,
                "observed_at": quote_time,
            }
            for token in tokens
        }

    # Exercise the production default quote path while keeping the network
    # bounded by the fake target prewarm.
    engine.quote_reader = _quote_book
    engine._default_quote_reader = True
    engine.quote_batch_reader = target_reader
    result = engine.run_once()
    assert result["places_orders"] is True
    assert len(gateway.payloads) == 4
    assert len(calls) == 1
    assert sorted(calls[0][0]) == ["token-0", "token-1", "token-2", "token-3"]
    assert result["timings_ms"]["quote_prime"]["connection_count"] == 1
    assert result["timings_ms"]["quote_prime"]["target_token_count"] == 4
    assert result["timings_ms"]["quote_prime"]["resolved_token_count"] == 4
    assert result["new_plan_results"][0]["timing_ms"]["order_count"] == 4
    assert result["timings_ms"]["submit_ms"] >= 0


def test_copy_follow_quote_reader_keeps_committed_book_during_reconnect(tmp_path: Path, monkeypatch) -> None:
    manifest = tmp_path / "latest.json"
    shard_dir = tmp_path / "shards"
    monkeypatch.setenv("PM_MARKET_WS_SHARDED_SNAPSHOT", "1")
    monkeypatch.setenv("PM_MARKET_WS_LATEST_QUOTES_PATH", str(manifest))
    monkeypatch.setenv("PM_MARKET_WS_QUOTE_SHARD_DIR", str(shard_dir))
    state = LatestQuoteSnapshot(manifest, min_write_interval_sec=0)
    state.connected(0, ["stable"])
    observed_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    state.observe_market_message(
        {
            "event_type": "book", "asset_id": "stable",
            "bids": [{"price": "0.60", "size": "2"}],
            "asks": [{"price": "0.61", "size": "3"}],
        }, observed_at=observed_at, depth_levels=5,
    )
    state.observe(
        [{
            "token_id": "stable", "slug": "slug", "event_date": "2026-07-21",
            "outcome": "31°C", "side": "YES", "best_bid": 0.60, "best_ask": 0.61,
            "source": "clob_ws", "source_detail": "book", "created_at": observed_at,
        }], observed_at=observed_at,
    )
    assert _quote_book("stable", max_age_sec=5)["best_ask"] == 0.61

    # The replacement has started but has not delivered its complete book.
    # The reader must continue using the committed file instead of reporting
    # a false copy_follow_clob_quote_missing.
    state.connecting(0, ["stable", "new-token"], expected_shards=1)
    assert _quote_book("stable", max_age_sec=5)["best_ask"] == 0.61
    assert _quote_book("new-token", max_age_sec=5) is None

    state.disconnected(0)
    assert _quote_book("stable", max_age_sec=5)["best_ask"] == 0.61


def test_passed_local_window_still_reads_quotes_and_solves_floor_but_hides_city(tmp_path: Path) -> None:
    engine, gateway, conn, _activity = _dynamic_fixture(tmp_path)
    conn.execute("ALTER TABLE reported_trajectory_market_registry ADD COLUMN settlement_timezone TEXT")
    conn.execute("UPDATE reported_trajectory_market_registry SET settlement_timezone='Asia/Shanghai'")
    conn.commit()
    quote_calls: list[str] = []
    original_quote_reader = engine.quote_reader

    def recording_quote_reader(token_id, *, max_age_sec):
        quote_calls.append(str(token_id))
        return original_quote_reader(token_id, max_age_sec=max_age_sec)

    engine.quote_reader = recording_quote_reader
    with patch(
        "copy_follow_production._local_entry_window_reason",
        return_value="copy_follow_local_peak_window_passed",
    ):
        result = engine.run_once()

    assert result["places_orders"] is False
    assert result["new_plan_results"] == []
    assert result["expired_plan_count"] == 1
    assert sorted(quote_calls) == ["token-0", "token-1", "token-2", "token-3"]
    assert gateway.payloads == []
    saved = json.loads(conn.execute("SELECT plan_json FROM copy_follow_plans").fetchone()[0])
    assert saved["execution_status"] == "blocked_by_local_peak_window"
    assert saved["settlement_profit_by_bucket"]
    assert saved["minimum_settlement_profit_usdc"] >= 0.01
    assert saved["maximum_settlement_profit_usdc"] >= saved["minimum_settlement_profit_usdc"]


def test_copy_follow_partial_gtc_fill_is_reconciled_without_duplicate_orders(tmp_path: Path) -> None:
    engine, gateway, conn, _activity = _dynamic_fixture(tmp_path)
    first = engine.run_once()
    assert len(gateway.payloads) == 4
    for order_id, payload in gateway.orders.items():
        gateway.fill_sizes[order_id] = float(payload["size"]) / 2.0
    second = engine.run_once()
    assert second["places_orders"] is False
    assert len(gateway.payloads) == 4
    states = {row[0] for row in conn.execute("SELECT state FROM copy_follow_orders")}
    assert states == {"partially_filled"}


def test_copy_follow_partial_market_entry_tops_up_remaining_with_best_bid(tmp_path: Path) -> None:
    engine, gateway, _conn, _activity = _dynamic_fixture(tmp_path)
    engine.run_once()
    for order_id, payload in gateway.orders.items():
        gateway.fill_sizes[order_id] = float(payload["size"]) / 2.0
        gateway.terminal_ids.add(order_id)
    second = engine.run_once()
    assert second["places_orders"] is True
    assert len(gateway.payloads) == 8
    assert all(payload["order_type"] == "GTC" and payload["price"] == 0.08 for payload in gateway.payloads[4:])


def test_copy_follow_source_ratio_change_cancels_old_direction_then_rebalances(tmp_path: Path) -> None:
    engine, gateway, conn, activity = _dynamic_fixture(tmp_path)
    engine.run_once()
    # Keep the original orders open, then make the former main bucket much
    # smaller.  The first pass must cancel the outstanding BUY before issuing
    # a SELL in the next pass.
    activity[0]["usdcSize"] = 3
    activity[1]["usdcSize"] = 12
    activity[2]["usdcSize"] = 9
    activity[3]["usdcSize"] = 6
    fresh = int(datetime.now(timezone.utc).timestamp())
    for index, row in enumerate(activity):
        row["timestamp"] = fresh + index
    second = engine.run_once()
    assert second["places_orders"] is True
    assert gateway.cancel_calls
    assert conn.execute("SELECT COUNT(*) FROM copy_follow_orders WHERE state='cancelled'").fetchone()[0] >= 1
    assert len(gateway.payloads) == 4

    # The following pass sees confirmed cancellations and is then allowed to
    # submit the new direction at best bid.
    third = engine.run_once()
    assert len(gateway.payloads) > 4
    assert any(payload["side"] == "BUY" and payload["price"] == 0.08 for payload in gateway.payloads[4:])


def test_existing_plan_can_repair_four_legs_at_best_bid_when_asks_cannot_prove_floor(tmp_path: Path) -> None:
    engine, _gateway, _conn, activity = _dynamic_fixture(tmp_path)
    candidate = extract_four_bucket_candidates(activity, source_user="0xabc")[0]
    exact_quotes = {
        "30°C": {"bids": [(0.30, 5.0)], "asks": [(0.33, 39.62)], "best_bid": 0.30, "best_ask": 0.33},
        "31°C": {"bids": [(0.45, 5.0)], "asks": [(0.46, 10.0)], "best_bid": 0.45, "best_ask": 0.46},
        "32°C": {"bids": [(0.10, 5.0)], "asks": [(0.16, 7.14)], "best_bid": 0.10, "best_ask": 0.16},
        "33°C": {"bids": [(0.02, 5.0)], "asks": [(0.036, 10.0)], "best_bid": 0.02, "best_ask": 0.036},
    }
    quote_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def quote_reader(token_id, *, max_age_sec):
        bucket = candidate.buckets[int(str(token_id).split("-")[-1])]
        return {"token_id": token_id, **exact_quotes[bucket], "observed_at": quote_time}

    engine.quote_reader = quote_reader
    config = json.loads(engine.config_path.read_text(encoding="utf-8"))
    managed = {
        "rows": [{"bucket": "31°C", "side": "BUY", "size_matched": 25.5}],
        "holdings_by_bucket": {"31°C": 25.5},
        "projected_holdings_by_bucket": {"31°C": 25.5},
        "projected_net_cost_usdc": 11.73,
        "open_rows": [],
    }

    prepared = engine._prepare(candidate, config, managed=managed)

    assert prepared["state"] == "ready"
    assert prepared["floor_proof_price_mode"] == "best_bid_gtc"
    assert prepared["floor_proof_best_bid_sum"] == 0.87
    assert prepared["minimum_settlement_profit_usdc"] >= 0.01


def test_copy_follow_unknown_lookup_stops_new_orders_for_reconciliation(tmp_path: Path) -> None:
    engine, gateway, conn, _activity = _dynamic_fixture(tmp_path)
    engine.run_once()
    gateway.lookup_error = True
    second = engine.run_once()
    assert second["new_plan_results"][0]["state"] == "reconcile_required"
    assert conn.execute("SELECT state FROM copy_follow_plans").fetchone()[0] == "reconcile_required"
    assert len(gateway.payloads) == 4


def test_copy_follow_reduces_filled_positions_when_source_core_moves(tmp_path: Path) -> None:
    engine, gateway, _conn, activity = _dynamic_fixture(tmp_path)
    engine.run_once()
    for order_id, payload in gateway.orders.items():
        gateway.fill_sizes[order_id] = float(payload["size"])
    activity[0]["usdcSize"] = 3
    activity[1]["usdcSize"] = 12
    activity[2]["usdcSize"] = 9
    activity[3]["usdcSize"] = 6
    fresh = int(datetime.now(timezone.utc).timestamp())
    for index, row in enumerate(activity):
        row["timestamp"] = fresh + index
    second = engine.run_once()
    assert second["places_orders"] is True
    assert any(payload["side"] == "SELL" and payload["price"] == 0.08 for payload in gateway.payloads[4:])
