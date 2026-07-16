from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts.dynamic_three_leg_execution_no_order_drill import run_drill
from dynamic_three_leg_continuation_schema_contract import apply_continuation_schema
from dynamic_three_leg_position_lot_schema_contract import apply_dynamic_three_leg_position_lot_schema
from dynamic_three_leg_position_lot_runtime import build_position_lot_identity
from dynamic_three_leg_clob_gateway_adapter import LocalDynamicThreeLegClobGateway
from resident_dynamic_three_leg_execution_loop import run_resident_dynamic_three_leg_execution_once
from sequential_bayes_schema_contract import apply_forward_schema


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _Handler(BaseHTTPRequestHandler):
    place_mode = "accepted"
    reconcile_mode = "filled"
    place_calls = 0
    reconcile_calls = 0

    def do_POST(self) -> None:  # noqa: N802
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        if self.path == "/execution-guard-snapshot":
            payload = {
                "schema": "clob_execution_guard_snapshot_v1", "ok": True,
                "evidence_only": True, "authorizes_order": False,
                "token_id": request["token_id"], "side": request["side"],
                "observed_at": _iso(datetime.now(timezone.utc)), "amount_decimals": 6,
                "order_book": {"asset_id": request["token_id"], "timestamp": str(int(time.time() * 1000)), "asks": [{"price": "0.50", "size": "100"}], "bids": [{"price": "0.49", "size": "100"}]},
                "conditional_balance_allowance": {"balance": "0", "allowance": "100000000"},
                "collateral_balance_allowance": {"balance": "100000000", "allowance": "100000000"},
            }
        elif self.path == "/place-order":
            type(self).place_calls += 1
            if type(self).place_mode == "ambiguous":
                payload = {"ok": False, "reconcile_required": True, "expected_order_id": "synthetic", "exchange_post_started": True, "exchange_post_started_at": _iso(datetime.now(timezone.utc))}
            else:
                payload = {"ok": True, "accepted": True, "orderID": "synthetic", "exchange_post_started": True, "exchange_post_started_at": _iso(datetime.now(timezone.utc))}
        elif self.path == "/reconcile-order":
            type(self).reconcile_calls += 1
            matched = 2 if type(self).reconcile_mode == "partial" else 4
            payload = {
                "ok": True,
                "status": "open" if matched < 4 else "filled",
                "idempotency_key": request.get("idempotency_key"),
                "orderID": "synthetic",
                "token_id": "token",
                "original_size": 4,
                "size_matched": matched,
                "price": 0.5,
            }
        else:
            payload = {"ok": False}
        body = json.dumps(payload).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, _format: str, *args: object) -> None:
        return


def _fixture(path: Path) -> None:
    now_dt = datetime.now(timezone.utc); now = _iso(now_dt); deadline = _iso(now_dt + timedelta(seconds=3))
    conn = sqlite3.connect(path); apply_forward_schema(conn)
    apply_dynamic_three_leg_position_lot_schema(conn, writer_quiescent=True)
    apply_continuation_schema(conn, writer_quiescent=True)
    conn.executescript("CREATE TABLE event_fact_history(id INTEGER PRIMARY KEY,slug TEXT,event_date TEXT,fact_snapshot_id TEXT,posterior_run_id TEXT); CREATE TABLE event_fact_latest(slug TEXT,event_date TEXT,fact_id INTEGER); CREATE TABLE auto_trade_decisions(decision_id TEXT PRIMARY KEY,decided_state TEXT,will_order INTEGER); INSERT INTO event_fact_history VALUES(1,'s','d','f','p'); INSERT INTO event_fact_latest VALUES('s','d',1); INSERT INTO auto_trade_decisions VALUES('decision','prepared',1);")
    conn.execute(
        """INSERT INTO posterior_outbox_execution_contracts(
           outbox_event_id,posterior_run_id,canonical_observation_event_id,
           first_actionable_seen_at,observation_to_order_hard_deadline_at,
           inference_decision_deadline_at,transaction_b_committed_at,
           execution_eligible_at_commit,execution_na_reason,deadline_policy,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "outbox", "p", "canonical", now, deadline, deadline, now,
            1, None, "canonical_first_actionable_plus_3000ms", now,
        ),
    )
    conn.execute("INSERT INTO dynamic_three_leg_transition_plans VALUES('transition','plan','p','r','f','position','artifact','market','capital','risk','epoch','reservation','BUY_NEW','{}','sha','prepared',?,?)", (now, now))
    conn.execute("INSERT INTO dynamic_three_leg_risk_reservations VALUES('reservation','plan','p','capital','risk','epoch','artifact',100,2,2,'bound','transition',?,'{}','sha',?,?)", (deadline, now, now))
    lot_id, lot_version = build_position_lot_identity(
        order_intent_id="intent", bucket="33C", outcome_side="YES", token_id="token"
    )
    payload = {"slug": "s", "event_date": "d", "token_id": "token", "side": "BUY", "price": .5, "size": 4, "notional": 2, "expected_token_balance": 0, "post_only": False, "observation_to_order_hard_deadline_at": deadline, "position_lot_id": lot_id, "position_lot_version": lot_version}
    values = {"order_intent_id":"intent","decision_id":"decision","transition_plan_id":"transition","trading_plan_id":"plan","posterior_run_id":"p","realization_state_id":"r","fact_snapshot_id":"f","position_snapshot_id":"position","position_lot_id":lot_id,"position_lot_version":lot_version,"strategy_artifact_sha256":"artifact","candidate_id":"candidate","market_snapshot_id":"market","capital_snapshot_id":"capital","risk_snapshot_id":"risk","capital_epoch_id":"epoch","risk_reservation_id":"reservation","bucket":"33C","outcome_side":"YES","order_action":"BUY","token_id":"token","limit_price":.5,"size":4,"notional":2,"reason_family":"strategy_leg","reason_code":"top_yes_wave","state":"prepared","live_submit_authorized":1,"observation_to_order_hard_deadline_at":deadline,"quote_expires_at":deadline,"idempotency_key":"idem","intent_json":json.dumps(payload),"intent_sha256":"sha","created_at":now,"updated_at":now}
    columns=list(values); conn.execute(f"INSERT INTO posterior_execution_intents({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", tuple(values[c] for c in columns)); conn.commit(); conn.close()


def test_no_order_http_drill_exercises_isolated_resident(tmp_path: Path) -> None:
    db = tmp_path / "isolated-drill.sqlite"; _fixture(db)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler); thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        result = run_drill(db_path=db, gateway_url=f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
    assert result["ok"] is True
    assert result["counts_as_production_slo_evidence"] is False
    assert result["wall_ms"] < 3000
    assert result["isolated_t1_to_t5"]["state"] == "passed"
    assert result["isolated_t1_to_t5"]["t1_to_t5_ms"] <= 3000
    assert result["isolated_t1_to_t5"]["counts_as_production_slo_evidence"] is False


def test_ambiguous_ack_restart_reconciles_partial_then_full_without_resubmit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "isolated-ack-loss.sqlite"
    _fixture(db)
    _Handler.place_mode = "ambiguous"
    _Handler.reconcile_mode = "partial"
    _Handler.place_calls = 0
    _Handler.reconcile_calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    gateway_url = f"http://127.0.0.1:{server.server_port}"
    try:
        ambiguous = run_drill(db_path=db, gateway_url=gateway_url)
        assert ambiguous["ok"] is False
        assert ambiguous["resident_result"]["result"]["state"] == "reconcile_required"
        assert _Handler.place_calls == 1

        monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED", "1")
        monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_LIVE_SUBMIT_ENABLED", "1")
        monkeypatch.setenv("PM_LIVE_PAUSE_FILE", str(tmp_path / "absent-pause"))
        gateway = LocalDynamicThreeLegClobGateway(gateway_url)
        partial = run_resident_dynamic_three_leg_execution_once(db_path=db, gateway=gateway)
        assert partial["state"] == "reconciled"
        assert partial["result"]["state"] == "partially_filled"
        assert partial["result"]["position_lot_update"]["applied_size"] == 2.0
        assert _Handler.place_calls == 1

        _Handler.reconcile_mode = "filled"
        filled = run_resident_dynamic_three_leg_execution_once(db_path=db, gateway=gateway)
        assert filled["state"] == "reconciled"
        assert filled["result"]["state"] == "filled"
        assert filled["result"]["position_lot_update"]["applied_size"] == 2.0
        assert _Handler.place_calls == 1
        assert _Handler.reconcile_calls == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        _Handler.place_mode = "accepted"
        _Handler.reconcile_mode = "filled"

    conn = sqlite3.connect(db)
    intent_state = conn.execute(
        "SELECT state FROM posterior_execution_intents WHERE order_intent_id='intent'"
    ).fetchone()[0]
    lot = conn.execute(
        "SELECT acquired_size,remaining_size,remaining_cost_usdc,state FROM dynamic_three_leg_position_lots"
    ).fetchone()
    transition_state = conn.execute(
        "SELECT state FROM dynamic_three_leg_transition_plans WHERE transition_plan_id='transition'"
    ).fetchone()[0]
    reservation_state = conn.execute(
        "SELECT state FROM dynamic_three_leg_risk_reservations WHERE risk_reservation_id='reservation'"
    ).fetchone()[0]
    event_count = conn.execute("SELECT COUNT(*) FROM dynamic_three_leg_position_lot_events").fetchone()[0]
    conn.close()
    assert intent_state == "filled"
    assert lot == (4.0, 4.0, 2.0, "open")
    assert transition_state == "completed"
    assert reservation_state == "consumed"
    assert event_count == 2


def test_no_order_drill_refuses_archive_name_and_nonlocal_gateway(tmp_path: Path) -> None:
    archive = tmp_path / "archive.db"; archive.touch()
    with pytest.raises(ValueError, match="non_archive"):
        run_drill(db_path=archive, gateway_url="http://127.0.0.1:1")
