from __future__ import annotations

import json
import sqlite3
import threading
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from reported_trajectory_market_registry import load_config
from reported_trajectory_no_engine import ReportedTrajectoryNoEngine
from reported_trajectory_no_schema import apply_reported_trajectory_no_schema


class Gateway:
    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self.placed = []
        self.conn = conn
        self.freeze_called_in_transaction = []
        self.exit_filled = False
        self.yes_balance_microunits = 0

    def freeze_execution_guard_snapshot(self, request):
        self.freeze_called_in_transaction.append(bool(self.conn and self.conn.in_transaction))
        snapshot = {
            "schema": "clob_execution_guard_snapshot_v1",
            "ok": True,
            "amount_decimals": 6,
            "order_book": {
                "asks": [{"price": "0.80", "size": "100"}],
                "bids": [{"price": "0.79", "size": "100"}],
            },
            "collateral_balance_allowance": {
                "balance": "100000000",
                "allowances": {"exchange": "100000000"},
            },
            "token_metadata": {"tick_size": "0.001"},
        }
        if str(request.get("side") or "BUY") == "SELL":
            snapshot["collateral_balance_allowance"] = None
            snapshot["conditional_balance_allowance"] = {
                "balance": str(self.yes_balance_microunits),
                "allowances": {"exchange": str(self.yes_balance_microunits)},
            }
        return snapshot

    def place_order(self, request):
        self.placed.append(dict(request))
        if request.get("side") == "SELL":
            if request.get("pre_entry_yes_liquidation") is True:
                return {
                    "ok": True,
                    "orderID": "yes-liquidation-order-1",
                    "status": "matched",
                    "size_matched": str(request["size"]),
                    "idempotency_key": request["idempotency_key"],
                }
            return {
                "ok": True,
                "orderID": "exit-order-1",
                "status": "live",
                "idempotency_key": request["idempotency_key"],
            }
        return {
            "ok": True,
            "accepted": True,
            "pending": True,
            "idempotency_key": request["idempotency_key"],
        }

    def lookup_order(self, request):
        size = self.placed[0]["size"]
        return {
            "ok": True, "state": "exchange_accepted", "orderID": "entry-order-1",
            "status": "matched", "original_size": size, "size_matched": size,
        }

    def lookup_exchange_order(self, request):
        return {
            "ok": True, "orderID": request["order_id"],
            "status": "filled" if self.exit_filled else "live",
            "size_matched": self.placed[1]["size"] if self.exit_filled and len(self.placed) > 1 else 0,
        }


class ConcurrentGateway(Gateway):
    def __init__(self) -> None:
        super().__init__()
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def freeze_execution_guard_snapshot(self, request):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.03)
            return super().freeze_execution_guard_snapshot(request)
        finally:
            with self.lock:
                self.active -= 1


class FirstFrameGateway(Gateway):
    def __init__(self, *, recover: bool) -> None:
        super().__init__()
        self.recover = recover
        self.calls = 0

    def freeze_execution_guard_snapshot(self, request):
        self.calls += 1
        if self.recover and self.calls > 1:
            return super().freeze_execution_guard_snapshot(request)
        return {
            "schema": "clob_execution_guard_snapshot_v1",
            "ok": False,
            "evidence_errors": [
                "clob_ws_order_book_depth_missing",
                "ENOENT: quote_shards/token.json",
            ],
        }


def prepare_db(source_lane: str = "amsc") -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE auto_trade_decisions(
          id INTEGER PRIMARY KEY,run_id,run_date,slug,event_date,title,outcome,side,action,
          decided_state,will_order,reason_codes_json,reasons_json,details_json,created_at,
          correlation_id,fact_snapshot_id,posterior_run_id,decision_id,signal_id,order_intent_id)"""
    )
    conn.execute(
        """CREATE TABLE market_tokens(
          slug TEXT,event_date TEXT,outcome TEXT,yes_token TEXT,no_token TEXT,outcome_volume REAL,
          liquidity REAL,market_active INTEGER,market_closed INTEGER,market_archived INTEGER,
          market_accepting_orders INTEGER,market_enable_orderbook INTEGER,updated_at TEXT,
          PRIMARY KEY(slug,event_date,outcome))"""
    )
    apply_reported_trajectory_no_schema(conn)
    for outcome, lower, upper, kind, no_token in (
        ("29°C or below", None, 29, "lower_tail", "no29"),
        ("30°C", 30, 30, "single", "no30"),
        ("31°C or higher", 31, None, "upper_tail", "no31"),
    ):
        registry = {
            "slug": "highest-temperature-in-beijing-on-july-18-2026",
            "event_date": "2026-07-18",
            "city_slug": "beijing",
            "station_id": "ZBAA",
            "settlement_timezone": "Asia/Shanghai",
            "settlement_unit": "C",
            "source_lane": source_lane,
            "source_eligible": True,
            "outcome": outcome,
            "lower": lower,
            "upper": upper,
            "kind": kind,
            "yes_token": "yes" + no_token,
            "no_token": no_token,
        }
        refreshed_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute(
            """INSERT INTO reported_trajectory_market_registry(
              slug,event_date,outcome,city_slug,station_id,settlement_timezone,settlement_unit,
              source_lane,source_eligible,source_block_reason,resolution_url,rules_sha256,
              yes_token,no_token,market_active,market_accepting_orders,registry_json,refreshed_at
            ) VALUES(?,?,?,?,?,?,?,?,1,'','url','sha',?,?,1,1,?,?)""",
            (
                registry["slug"],registry["event_date"],outcome,"beijing","ZBAA","Asia/Shanghai","C",source_lane,
                registry["yes_token"],no_token,json.dumps(registry),refreshed_at,
            ),
        )
    conn.commit()
    return conn


class ReportedTrajectoryNoEngineTests(unittest.TestCase):
    def test_transient_target_book_gap_gets_one_bounded_local_reread(self) -> None:
        conn = prepare_db()
        gateway = FirstFrameGateway(recover=True)
        engine = ReportedTrajectoryNoEngine(conn, config=load_config(), gateway=gateway)
        snapshot, blocker = engine._freeze_quote_snapshot(token_id="token", max_price=0.99)
        self.assertEqual(gateway.calls, 2)
        self.assertIsNotNone(snapshot)
        self.assertEqual(blocker, "")

    def test_persistent_target_book_gap_has_specific_fail_closed_reason(self) -> None:
        conn = prepare_db()
        gateway = FirstFrameGateway(recover=False)
        engine = ReportedTrajectoryNoEngine(conn, config=load_config(), gateway=gateway)
        snapshot, blocker = engine._freeze_quote_snapshot(token_id="token", max_price=0.99)
        self.assertEqual(gateway.calls, 2)
        self.assertIsNone(snapshot)
        self.assertEqual(blocker, "clob_ws_target_book_not_materialized")

    def test_target_book_gap_reason_is_persisted_in_decision_ssot(self) -> None:
        conn = prepare_db()
        gateway = FirstFrameGateway(recover=False)
        engine = ReportedTrajectoryNoEngine(conn, config=load_config(), gateway=gateway)
        conn.execute(
            """INSERT INTO reported_trajectory_activation_state(
              singleton,generation,state,activation_sha256,config_sha256,
              old_bayes_execution_disabled,awc_amsc_disjoint,updated_at
            ) VALUES(1,11,'enabled','activation',?,1,1,'2026-07-18T00:00:00Z')""",
            (engine.config_sha256,),
        )
        conn.commit()
        poll = {
            "source_lane": "amsc", "station_ids": ["ZBAA"], "status": 200,
            "response_started_at": "2026-07-18T02:00:00Z", "station_errors": [],
            "taf_requested": False,
            "reports": [{
                "station_id": "ZBAA",
                "raw_text": "METAR ZBAA 180200Z CAVOK 31/20 Q1000",
                "source_report_time": "2026-07-18T02:00:00Z",
            }],
        }
        engine.process_source_poll(
            poll,
            request_started_at="2026-07-18T02:00:00Z",
            response_completed_at="2026-07-18T02:00:00.100Z",
        )
        reason_codes = json.loads(
            conn.execute("SELECT reason_codes_json FROM auto_trade_decisions").fetchone()[0]
        )
        self.assertEqual(reason_codes, ["clob_ws_target_book_not_materialized"])
        self.assertEqual(gateway.placed, [])

    def test_independent_quote_snapshots_are_frozen_concurrently(self) -> None:
        conn = prepare_db()
        gateway = ConcurrentGateway()
        engine = ReportedTrajectoryNoEngine(conn, config=load_config(), gateway=gateway)
        requests = {
            ("observation-1", "market-1"): "token-1",
            ("observation-2", "market-2"): "token-2",
            ("observation-3", "market-3"): "token-3",
        }
        snapshots = engine._freeze_quote_snapshots(requests, max_price=0.99)
        self.assertEqual(set(snapshots), set(requests))
        self.assertTrue(all(value[0] is not None and value[1] == "" for value in snapshots.values()))
        self.assertGreaterEqual(gateway.max_active, 2)

    def test_candidate_prewarm_reads_next_two_steps_without_writes_or_orders(self) -> None:
        conn = prepare_db()
        gateway = ConcurrentGateway()
        engine = ReportedTrajectoryNoEngine(conn, config=load_config(), gateway=gateway)
        conn.execute(
            """INSERT INTO reported_trajectory_station_day_bounds(
                 source_lane,station_id,event_date,settlement_timezone,reported_max_c,
                 max_observation_id,max_valid_at,bound_generation,updated_at
               ) VALUES('amsc','ZBAA','2026-07-18','Asia/Shanghai',29,
                        'obs','2026-07-18T02:00:00Z',1,'2026-07-18T02:00:00Z')"""
        )
        conn.commit()
        # The prewarm contract intentionally ignores settlement books that are
        # no longer current.  Freeze the clock to the fixture's event day so
        # this test does not become date-dependent.
        with patch(
            "reported_trajectory_no_engine.datetime",
            **{"now.return_value": datetime(2026, 7, 18, 2, 0, tzinfo=timezone.utc)},
        ):
            result = engine.prewarm_next_candidates(source_lane="amsc")
        self.assertTrue(result["ok"])
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(result["no_ready_count"], 2)
        self.assertEqual(result["yes_position_ready_count"], 2)
        self.assertFalse(result["writes_or_orders"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM reported_trajectory_no_intents").fetchone()[0], 0)
        self.assertEqual(gateway.placed, [])

    def test_0_99_cap_is_tick_aligned_without_ever_exceeding_it(self) -> None:
        conn = prepare_db()
        engine = ReportedTrajectoryNoEngine(conn, config=load_config(), gateway=Gateway(conn))
        snapshot = Gateway(conn).freeze_execution_guard_snapshot({})
        snapshot["token_metadata"] = {"tick_size": "0.01"}
        snapshot["collateral_balance_allowance"] = None
        capital, blocker = engine._capital_from_snapshot(
            source_lane="amsc", slug="market", snapshot=snapshot, max_price=0.99
        )
        self.assertEqual(blocker, "")
        self.assertEqual(capital["configured_maximum_buy_price"], 0.99)
        self.assertEqual(capital["effective_tick_aligned_limit_price"], 0.99)
        self.assertEqual(capital["frozen_order_notional_usdc"], 50.0)
        self.assertFalse(capital["collateral_balance_check"])

    def test_thin_depth_requests_fixed_50_and_does_not_read_yes_position(self) -> None:
        conn = prepare_db()
        engine = ReportedTrajectoryNoEngine(conn, config=load_config(), gateway=Gateway(conn))
        snapshot = Gateway(conn).freeze_execution_guard_snapshot({})
        snapshot["order_book"]["asks"] = [{"price": "0.99", "size": "15.75"}]
        snapshot["existing_positions"] = {"YES": 1000000, "NO": 0}
        capital, blocker = engine._capital_from_snapshot(
            source_lane="amsc", slug="market", snapshot=snapshot, max_price=0.99
        )
        self.assertEqual(blocker, "")
        self.assertEqual(capital["frozen_order_notional_usdc"], 50.0)
        self.assertFalse(capital["existing_yes_positions_sizing_impact"])

    def test_materialized_empty_ask_book_is_classified_as_no_executable_ask(self) -> None:
        conn = prepare_db()
        engine = ReportedTrajectoryNoEngine(conn, config=load_config(), gateway=Gateway(conn))
        snapshot = Gateway(conn).freeze_execution_guard_snapshot({})
        snapshot["order_book"]["asks"] = []
        capital, blocker = engine._capital_from_snapshot(
            source_lane="amsc", slug="market", snapshot=snapshot, max_price=0.99
        )
        self.assertIsNone(capital)
        self.assertEqual(blocker, "no_executable_no_ask_at_or_below_max_buy_price")

    def test_rounded_size_and_actual_notional_never_exceed_50_usdc(self) -> None:
        conn = prepare_db()
        gateway = Gateway(conn)
        engine = ReportedTrajectoryNoEngine(conn, config=load_config(), gateway=gateway)
        conn.execute(
            """INSERT INTO reported_trajectory_activation_state(
              singleton,generation,state,activation_sha256,config_sha256,
              old_bayes_execution_disabled,awc_amsc_disjoint,updated_at
            ) VALUES(1,1,'enabled','activation',?,1,1,'2026-07-17T00:00:00Z')""",
            (engine.config_sha256,),
        )
        conn.commit()
        poll = {
            "source_lane": "amsc", "station_ids": ["ZBAA"], "status": 200,
            "response_started_at": "2026-07-18T02:00:00.050Z", "station_errors": [],
            "taf_requested": False, "reports": [{
                "station_id": "ZBAA", "raw_text": "METAR ZBAA 180200Z 18004MPS CAVOK 31/20 Q1000",
                "source_report_time": "2026-07-18T02:00:00Z",
            }],
        }
        engine.process_source_poll(
            poll, request_started_at="2026-07-18T02:00:00.010Z",
            response_completed_at="2026-07-18T02:00:00.100Z",
        )
        order = gateway.placed[0]
        self.assertLessEqual(order["notional"], 50.0)
        self.assertAlmostEqual(order["notional"], order["size"] * order["price"], places=6)

    def test_existing_same_bucket_yes_sell_is_submitted_with_no_buy(self) -> None:
        conn = prepare_db()
        gateway = Gateway(conn)
        gateway.yes_balance_microunits = 10_000_000
        engine = ReportedTrajectoryNoEngine(
            conn, config=load_config(), gateway=gateway,
            held_yes_inventory_tokens={"yesno30"},
        )
        conn.execute(
            """INSERT INTO reported_trajectory_activation_state(
              singleton,generation,state,activation_sha256,config_sha256,
              old_bayes_execution_disabled,awc_amsc_disjoint,updated_at
            ) VALUES(1,1,'enabled','activation',?,1,1,'2026-07-17T00:00:00Z')""",
            (engine.config_sha256,),
        )
        conn.commit()
        poll = {
            "source_lane": "amsc", "station_ids": ["ZBAA"], "status": 200,
            "response_started_at": "2026-07-18T02:00:00Z", "station_errors": [],
            "taf_requested": False, "reports": [{
                "station_id": "ZBAA",
                "raw_text": "METAR ZBAA 180200Z CAVOK 31/20 Q1000",
                "source_report_time": "2026-07-18T02:00:00Z",
            }],
        }
        engine.process_source_poll(
            poll, request_started_at="2026-07-18T02:00:00Z",
            response_completed_at="2026-07-18T02:00:00.1Z",
        )
        self.assertEqual([row["side"] for row in gateway.placed], ["SELL", "BUY"])
        self.assertEqual(gateway.placed[0]["outcome_side"], "YES")
        self.assertEqual(gateway.placed[0]["order_type"], "FAK")
        self.assertTrue(gateway.placed[0]["pre_entry_yes_liquidation"])
        self.assertEqual(gateway.placed[0]["size"], 10.0)
        self.assertEqual(gateway.placed[1]["outcome_side"], "NO")
        self.assertFalse(gateway.placed[1]["allow_market"])
        self.assertEqual(
            conn.execute("SELECT state FROM reported_trajectory_yes_liquidations").fetchone()[0],
            "filled",
        )

    def test_partial_yes_liquidation_blocks_no_buy(self) -> None:
        class PartialGateway(Gateway):
            def place_order(self, request):
                self.placed.append(dict(request))
                if request.get("pre_entry_yes_liquidation") is True:
                    return {
                        "ok": True, "orderID": "partial", "status": "matched",
                        "size_matched": str(request["size"] - 1),
                    }
                return {"ok": True, "accepted": True, "pending": True}

        conn = prepare_db()
        gateway = PartialGateway(conn)
        gateway.yes_balance_microunits = 10_000_000
        engine = ReportedTrajectoryNoEngine(
            conn, config=load_config(), gateway=gateway,
            held_yes_inventory_tokens={"yesno30"},
        )
        conn.execute(
            """INSERT INTO reported_trajectory_activation_state(
              singleton,generation,state,activation_sha256,config_sha256,
              old_bayes_execution_disabled,awc_amsc_disjoint,updated_at
            ) VALUES(1,1,'enabled','activation',?,1,1,'2026-07-17T00:00:00Z')""",
            (engine.config_sha256,),
        )
        conn.commit()
        poll = {
            "source_lane": "amsc", "station_ids": ["ZBAA"], "status": 200,
            "response_started_at": "2026-07-18T02:00:00Z", "station_errors": [],
            "taf_requested": False, "reports": [{
                "station_id": "ZBAA", "raw_text": "METAR ZBAA 180200Z CAVOK 31/20 Q1000",
                "source_report_time": "2026-07-18T02:00:00Z",
            }],
        }
        engine.process_source_poll(
            poll, request_started_at="2026-07-18T02:00:00Z",
            response_completed_at="2026-07-18T02:00:00.1Z",
        )
        self.assertEqual([row["side"] for row in gateway.placed], ["SELL"])
        self.assertEqual(
            conn.execute("SELECT state,last_error FROM reported_trajectory_no_intents").fetchone(),
            ("blocked", "yes_liquidation_not_fully_filled"),
        )

    def test_async_yes_gateway_ack_blocks_no_submit_until_reconciled(self) -> None:
        class AsyncYesGateway(Gateway):
            def place_order(self, request):
                self.placed.append(dict(request))
                if request.get("pre_entry_yes_liquidation") is True:
                    return {"ok": True, "accepted": True, "pending": True}
                return {"ok": True, "accepted": True, "pending": True}

        conn = prepare_db()
        gateway = AsyncYesGateway(conn)
        gateway.yes_balance_microunits = 10_000_000
        engine = ReportedTrajectoryNoEngine(
            conn, config=load_config(), gateway=gateway,
            held_yes_inventory_tokens={"yesno30"},
        )
        conn.execute(
            """INSERT INTO reported_trajectory_activation_state(
              singleton,generation,state,activation_sha256,config_sha256,
              old_bayes_execution_disabled,awc_amsc_disjoint,updated_at
            ) VALUES(1,1,'enabled','activation',?,1,1,'2026-07-17T00:00:00Z')""",
            (engine.config_sha256,),
        )
        conn.commit()
        poll = {
            "source_lane": "amsc", "station_ids": ["ZBAA"], "status": 200,
            "response_started_at": "2026-07-18T02:00:00Z", "station_errors": [],
            "taf_requested": False, "reports": [{
                "station_id": "ZBAA", "raw_text": "METAR ZBAA 180200Z CAVOK 31/20 Q1000",
                "source_report_time": "2026-07-18T02:00:00Z",
            }],
        }
        engine.process_source_poll(
            poll, request_started_at="2026-07-18T02:00:00Z",
            response_completed_at="2026-07-18T02:00:00.1Z",
        )
        self.assertEqual([row["side"] for row in gateway.placed], ["SELL"])
        self.assertEqual(
            conn.execute("SELECT state FROM reported_trajectory_yes_liquidations").fetchone()[0],
            "submitting",
        )
        self.assertEqual(
            conn.execute("SELECT state FROM reported_trajectory_no_intents").fetchone()[0],
            "prepared",
        )

    def test_fresh_complete_inventory_attests_unheld_yes_without_balance_probe(self) -> None:
        class NoSellProbeGateway(Gateway):
            def freeze_execution_guard_snapshot(self, request):
                if request.get("side") == "SELL":
                    raise AssertionError("unheld YES must not hit conditional balance endpoint")
                return super().freeze_execution_guard_snapshot(request)

        conn = prepare_db()
        gateway = NoSellProbeGateway(conn)
        engine = ReportedTrajectoryNoEngine(
            conn, config=load_config(), gateway=gateway, held_yes_inventory_tokens=set()
        )
        conn.execute(
            """INSERT INTO reported_trajectory_activation_state(
              singleton,generation,state,activation_sha256,config_sha256,
              old_bayes_execution_disabled,awc_amsc_disjoint,updated_at
            ) VALUES(1,1,'enabled','activation',?,1,1,'2026-07-17T00:00:00Z')""",
            (engine.config_sha256,),
        )
        conn.commit()
        poll = {
            "source_lane": "amsc", "station_ids": ["ZBAA"], "status": 200,
            "response_started_at": "2026-07-18T02:00:00Z", "station_errors": [],
            "taf_requested": False, "reports": [{
                "station_id": "ZBAA", "raw_text": "METAR ZBAA 180200Z CAVOK 31/20 Q1000",
                "source_report_time": "2026-07-18T02:00:00Z",
            }],
        }
        engine.process_source_poll(
            poll, request_started_at="2026-07-18T02:00:00Z",
            response_completed_at="2026-07-18T02:00:00.1Z",
        )
        self.assertEqual([row["side"] for row in gateway.placed], ["BUY"])
        liquidation = conn.execute(
            "SELECT state,gateway_response_json FROM reported_trajectory_yes_liquidations"
        ).fetchone()
        self.assertEqual(liquidation[0], "not_held")
        self.assertEqual(json.loads(liquidation[1])["schema"], "reported_trajectory_unheld_yes_attestation_v1")

    def test_late_awc_hard_fact_still_advances_and_can_order(self) -> None:
        conn = prepare_db("awc")
        gateway = Gateway(conn)
        engine = ReportedTrajectoryNoEngine(conn, config=load_config(), gateway=gateway)
        conn.execute(
            """INSERT INTO reported_trajectory_activation_state(
              singleton,generation,state,activation_sha256,config_sha256,
              old_bayes_execution_disabled,awc_amsc_disjoint,updated_at
            ) VALUES(1,1,'enabled','activation',?,1,1,'2026-07-17T00:00:00Z')""",
            (engine.config_sha256,),
        )
        conn.commit()
        poll = {
            "source_lane": "awc",
            "station_ids": ["ZBAA"],
            "status": 200,
            "response_started_at": "2026-07-18T04:00:00Z",
            "station_errors": [],
            "taf_requested": False,
            "reports": [{
                "station_id": "ZBAA",
                "raw_text": "METAR ZBAA 180000Z 18004MPS CAVOK 31/20 Q1000",
                "source_report_time": "2026-07-18T00:00:00Z",
            }],
        }
        with patch("reported_trajectory_no_engine.utc_now", return_value="2026-07-18T04:00:00.100Z"):
            result = engine.process_source_poll(
                poll,
                request_started_at="2026-07-18T04:00:00.010Z",
                response_completed_at="2026-07-18T04:00:00.100Z",
            )
        self.assertEqual(result["new_observation_count"], 1)
        self.assertEqual(len(gateway.placed), 1)
        self.assertEqual(gateway.placed[0]["side"], "BUY")
        self.assertEqual(gateway.placed[0]["outcome_side"], "NO")
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM reported_trajectory_station_day_bounds").fetchone()[0], 1
        )
        row = conn.execute(
            "SELECT will_order,reason_codes_json,details_json FROM auto_trade_decisions"
        ).fetchone()
        self.assertEqual(row[0], 1)
        self.assertIn("reported_trajectory_no_hard_lower_bucket", row[1])

    def test_strict_bound_creates_only_previous_bucket_no_and_submits_once(self) -> None:
        conn = prepare_db()
        gateway = Gateway(conn)
        engine = ReportedTrajectoryNoEngine(conn, config=load_config(), gateway=gateway)
        conn.execute(
            """INSERT INTO reported_trajectory_activation_state(
              singleton,generation,state,activation_sha256,config_sha256,
              old_bayes_execution_disabled,awc_amsc_disjoint,updated_at
            ) VALUES(1,1,'enabled','activation',?,1,1,'2026-07-17T00:00:00Z')""",
            (engine.config_sha256,),
        )
        conn.commit()
        poll = {
            "source_lane": "amsc",
            "station_ids": ["ZBAA"],
            "status": 200,
            "response_started_at": "2026-07-18T02:00:00.050Z",
            "station_errors": [],
            "taf_requested": False,
            "reports": [{
                "station_id": "ZBAA",
                "raw_text": "METAR ZBAA 180200Z 18004MPS CAVOK 31/20 Q1000",
                "source_report_time": "2026-07-18T02:00:00Z",
            }],
        }
        first = engine.process_source_poll(
            poll,
            request_started_at="2026-07-18T02:00:00.010Z",
            response_completed_at="2026-07-18T02:00:00.100Z",
        )
        self.assertEqual(first["new_observation_count"], 1)
        self.assertEqual(len(gateway.placed), 1)
        self.assertEqual(gateway.placed[0]["outcome_side"], "NO")
        self.assertEqual(gateway.placed[0]["price"], 0.99)
        self.assertEqual(gateway.placed[0]["order_type"], "FAK")
        self.assertFalse(gateway.placed[0]["allow_market"])
        self.assertEqual(gateway.placed[0]["strategy_id"], "reported_trajectory_no_v1")
        self.assertNotIn("observation_to_order_hard_deadline_at", gateway.placed[0])
        self.assertEqual(gateway.freeze_called_in_transaction, [False])
        row = conn.execute("SELECT outcome,side,will_order,posterior_run_id FROM auto_trade_decisions").fetchone()
        self.assertEqual(row[:3], ("30°C", "NO", 1))
        self.assertIsNone(row[3])
        second = engine.process_source_poll(
            poll,
            request_started_at="2026-07-18T02:00:01.010Z",
            response_completed_at="2026-07-18T02:00:01.100Z",
        )
        self.assertEqual(second["new_observation_count"], 0)
        self.assertTrue(second["duplicate_poll_zero_write"])
        self.assertEqual(len(gateway.placed), 1)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM reported_trajectory_source_polls").fetchone()[0], 1
        )
        origin = conn.execute(
            """SELECT origin_source_id,origin_country_code,direct_country_official,fallback_used
               FROM reported_trajectory_observation_origins"""
        ).fetchone()
        self.assertEqual(origin, ("cn_amsc", "CN", 1, 0))

    def test_disabled_activation_writes_block_decision_and_never_orders(self) -> None:
        conn = prepare_db()
        gateway = Gateway()
        engine = ReportedTrajectoryNoEngine(conn, config=load_config(), gateway=gateway)
        poll = {
            "source_lane": "amsc", "station_ids": ["ZBAA"], "status": 200,
            "response_started_at": "2026-07-18T02:00:00Z", "station_errors": [], "taf_requested": False,
            "reports": [{"station_id": "ZBAA", "raw_text": "METAR ZBAA 180200Z CAVOK 31/20 Q1000", "source_report_time": "2026-07-18T02:00:00Z"}],
        }
        engine.process_source_poll(poll, request_started_at="2026-07-18T02:00:00Z", response_completed_at="2026-07-18T02:00:00.1Z")
        self.assertEqual(gateway.placed, [])
        row = conn.execute("SELECT will_order,reason_codes_json FROM auto_trade_decisions").fetchone()
        self.assertEqual(row[0], 0)
        self.assertIn("activation_missing", row[1])

    def test_target_no_live_boundary_blocks_source_submit_without_gateway_reads(self) -> None:
        conn = prepare_db()
        gateway = Gateway(conn)
        poll = {
            "source_lane": "amsc", "station_ids": ["ZBAA"], "status": 200,
            "response_started_at": "2026-07-18T02:00:00Z", "station_errors": [],
            "taf_requested": False, "reports": [{
                "station_id": "ZBAA", "raw_text": "METAR ZBAA 180200Z CAVOK 31/20 Q1000",
                "source_report_time": "2026-07-18T02:00:00Z",
            }],
        }
        with patch.dict("os.environ", {"PM_TARGET_EXECUTION_MODE": "production_no_live"}, clear=False):
            engine = ReportedTrajectoryNoEngine(conn, config=load_config(), gateway=gateway)
            result = engine.process_source_poll(
                poll,
                request_started_at="2026-07-18T02:00:00Z",
                response_completed_at="2026-07-18T02:00:00.1Z",
            )
        self.assertEqual(gateway.placed, [])
        self.assertEqual(gateway.freeze_called_in_transaction, [])
        self.assertEqual(result["submit_results"], [])
        row = conn.execute("SELECT will_order,reason_codes_json,details_json FROM auto_trade_decisions").fetchone()
        self.assertEqual(row[0], 0)
        self.assertIn("production_no_live", row[1])
        details = json.loads(row[2])
        self.assertFalse(details["live_submit_authorized"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM reported_trajectory_no_intents").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM reported_trajectory_capital_ledger").fetchone()[0], 0)

    def test_entry_fill_immediately_places_0_99_gtc_exit_and_releases_after_fill(self) -> None:
        conn = prepare_db()
        gateway = Gateway(conn)
        engine = ReportedTrajectoryNoEngine(conn, config=load_config(), gateway=gateway)
        conn.execute(
            """INSERT INTO reported_trajectory_activation_state(
              singleton,generation,state,activation_sha256,config_sha256,
              old_bayes_execution_disabled,awc_amsc_disjoint,updated_at
            ) VALUES(1,1,'enabled','activation',?,1,1,'2026-07-17T00:00:00Z')""",
            (engine.config_sha256,),
        )
        conn.commit()
        poll = {
            "source_lane": "amsc", "station_ids": ["ZBAA"], "status": 200,
            "response_started_at": "2026-07-18T02:00:00Z", "station_errors": [], "taf_requested": False,
            "reports": [{
                "station_id": "ZBAA",
                "raw_text": "METAR ZBAA 180200Z CAVOK 31/20 Q1000",
                "source_report_time": "2026-07-18T02:00:00Z",
            }],
        }
        engine.process_source_poll(
            poll,
            request_started_at="2026-07-18T02:00:00Z",
            response_completed_at="2026-07-18T02:00:00.1Z",
        )
        result = engine.reconcile_one()
        self.assertEqual(result["exit_result"]["state"], "exchange_accepted")
        self.assertEqual(len(gateway.placed), 2)
        self.assertEqual(gateway.placed[1]["side"], "SELL")
        self.assertEqual(gateway.placed[1]["price"], 0.99)
        self.assertEqual(gateway.placed[1]["order_type"], "GTC")
        self.assertFalse(gateway.placed[1]["post_only"])
        exit_row = conn.execute(
            "SELECT state,limit_price,order_type FROM reported_trajectory_exit_orders"
        ).fetchone()
        self.assertEqual(exit_row, ("exchange_accepted", 0.99, "GTC"))
        self.assertEqual(
            conn.execute("SELECT state FROM reported_trajectory_capital_ledger").fetchone()[0], "filled"
        )
        gateway.exit_filled = True
        conn.execute("UPDATE reported_trajectory_exit_orders SET next_check_at='2000-01-01T00:00:00Z'")
        conn.commit()
        self.assertEqual(engine.reconcile_one()["state"], "filled")
        self.assertEqual(
            conn.execute("SELECT state FROM reported_trajectory_capital_ledger").fetchone()[0], "released"
        )

    def test_correction_never_advances_hard_bound(self) -> None:
        conn = prepare_db()
        gateway = Gateway()
        engine = ReportedTrajectoryNoEngine(conn, config=load_config(), gateway=gateway)
        poll = {
            "source_lane": "amsc", "station_ids": ["ZBAA"], "status": 200,
            "response_started_at": "2026-07-18T02:00:00Z", "station_errors": [], "taf_requested": False,
            "reports": [{"station_id": "ZBAA", "raw_text": "METAR COR ZBAA 180200Z CAVOK 31/20 Q1000", "source_report_time": "2026-07-18T02:00:00Z"}],
        }
        engine.process_source_poll(poll, request_started_at="2026-07-18T02:00:00Z", response_completed_at="2026-07-18T02:00:00.1Z")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM reported_trajectory_station_day_bounds").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT state FROM reported_trajectory_station_halts").fetchone()[0], "active")
        self.assertEqual(gateway.placed, [])
        self.assertIn("correction", conn.execute("SELECT reason_codes_json FROM auto_trade_decisions").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
