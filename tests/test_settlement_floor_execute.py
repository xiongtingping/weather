import importlib.util
import json
import sys
import time
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "settlement_floor_execute.py"
SPEC = importlib.util.spec_from_file_location("settlement_floor_execute_under_test", SCRIPT)
assert SPEC and SPEC.loader
execute = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = execute
SPEC.loader.exec_module(execute)


def test_plan_payload_emits_fak_for_visible_depth_and_records_gtc_remainder():
    result = execute._plan_payload(
        {
            "execution_mode": "staged_fak_then_gtc",
            "minimum_order_shares": 1.0,
            "target_rows": [
                {
                    "bucket": "40",
                    "role": "F",
                    "new_shares": 8.0,
                    "final_shares": 8.0,
                }
            ],
        },
        slug="market",
        event_date="2026-07-24",
        city="chengdu",
        quotes={"40": {"asks": [{"price": 0.20, "size": 5.0}]}},
        registry={"40": {"yes_token": "token-40"}},
    )

    assert result["staged"] is True
    assert len(result["orders"]) == 1
    order = result["orders"][0]
    assert order["order_type"] == "FAK"
    assert order["requested_shares"] == 5.0
    assert order["pending_gtc_shares"] == 3.0
    assert order["gtc_limit_price"] == 0.20


def test_staged_execution_is_default_with_strict_opt_out():
    with mock.patch.object(sys, "argv", ["settlement_floor_execute.py"]):
        assert execute._parse_args().staged is True
    with mock.patch.object(sys, "argv", ["settlement_floor_execute.py", "--no-staged"]):
        assert execute._parse_args().staged is False


def _run_main_with_place_responses(responses, open_orders=None):
    registry = {"40": {"yes_token": "token-40"}}
    calls = []

    real_plan_payload = execute._plan_payload

    def plan_payload_with_planned_depth_complete(*args, **kwargs):
        # Exercise the race where planning sees enough depth but the FAK is
        # partially matched after the book changes at submission time.
        result = real_plan_payload(*args, **kwargs)
        result["staged"] = False
        return result

    def post_local(path, payload, **_kwargs):
        calls.append((path, dict(payload)))
        if path == "/lookup-open-orders":
            return {"ok": True, "open_orders": list(open_orders or [])}
        if path == "/execution-guard-snapshot":
            return {"ok": True, "evidence_only": True, "authorizes_order": False, "evidence_errors": []}
        if path == "/place-order":
            return responses[len([item for item in calls if item[0] == "/place-order"]) - 1]
        raise AssertionError(path)

    class HealthResponse:
        def read(self):
            return json.dumps({
                "ready": True,
                "order_submission_enabled": True,
                "live_pause_present": False,
                "read_only": False,
                "proxy": "sakuracat",
            }).encode()

    argv = [
        "settlement_floor_execute.py",
        "--event-date", "2026-07-24",
        "--city", "chengdu",
        "--budget", "30",
        "--buckets", "40",
        "--roles-json", '{"40":"F"}',
        "--staged",
        "--execute",
    ]
    with mock.patch.object(sys, "argv", argv), \
         mock.patch.object(execute, "_registry", return_value=registry), \
         mock.patch.object(execute, "_quote", return_value={"asks": [{"price": 0.20, "size": 1.0}]}), \
         mock.patch.object(execute, "_fetch_positions", return_value={"token-40": {"shares": 0.0, "cost_usdc": 0.0}}), \
         mock.patch.object(execute, "_plan_payload", side_effect=plan_payload_with_planned_depth_complete), \
         mock.patch.object(execute, "_post_local", side_effect=post_local), \
         mock.patch.object(execute.urllib.request, "urlopen", return_value=HealthResponse()):
        output = mock.patch("builtins.print")
        with output as printed:
            code = execute.main()
    report = json.loads(printed.call_args.args[0])
    return code, report, calls


def test_known_partial_fak_places_only_actual_remainder_gtc():
    code, report, calls = _run_main_with_place_responses([
        {"ok": True, "orderID": "fak-1", "status": "matched", "size_matched": 0.4},
        {"ok": True, "orderID": "gtc-1", "status": "open", "size_matched": 0.0},
    ])

    assert code == 0
    assert report["status"] == "staged_pending_recompute_after_fill"
    place_calls = [payload for path, payload in calls if path == "/place-order"]
    assert [payload["order_type"] for payload in place_calls] == ["FAK", "GTC"]
    assert place_calls[0]["size"] > 0.0
    assert place_calls[0]["allow_market"] is True
    assert place_calls[1]["size"] > 0.0
    assert "amount" not in place_calls[1]


def test_unknown_fak_state_stops_without_gtc():
    code, report, calls = _run_main_with_place_responses([
        {"ok": True, "orderID": "fak-unknown", "status": "open", "size_matched": None},
    ])

    assert code == 4
    assert report["blocked"] == "settlement_floor_order_fill_state_unknown"
    assert len([path for path, _payload in calls if path == "/place-order"]) == 1


def test_existing_relevant_gtc_is_waited_on_without_duplicate_submission():
    code, report, calls = _run_main_with_place_responses(
        [],
        open_orders=[{"asset_id": "token-40", "id": "existing-gtc", "status": "LIVE"}],
    )

    assert code == 0
    assert report["staged_waiting_existing_orders"] is True
    assert report["status"] == "staged_pending_recompute_after_fill"
    assert not [path for path, _payload in calls if path == "/place-order"]


def test_fully_matched_remainder_gtc_is_not_left_as_pending():
    code, report, calls = _run_main_with_place_responses([
        {"ok": True, "orderID": "fak-2", "status": "matched", "size_matched": 0.4},
        {"ok": True, "orderID": "gtc-2", "status": "matched", "size_matched": 999999.0},
    ])

    assert code == 0
    assert report["completed"] is True
    assert report["pending_orders"] == []
    assert [payload["order_type"] for path, payload in calls if path == "/place-order"] == ["FAK", "GTC"]


def test_read_only_evidence_is_parallel_and_timed():
    registry = {"40": {"yes_token": "token-40"}}
    calls = []

    def slow_positions(_token_ids):
        calls.append("positions-start")
        time.sleep(0.04)
        calls.append("positions-end")
        return {"token-40": {"shares": 0.0, "cost_usdc": 0.0}}

    def slow_post(path, payload, **_kwargs):
        calls.append(path + "-start")
        time.sleep(0.04)
        calls.append(path + "-end")
        if path == "/lookup-open-orders":
            return {"ok": True, "open_orders": []}
        raise AssertionError(path)

    argv = [
        "settlement_floor_execute.py",
        "--event-date", "2026-07-24",
        "--city", "chengdu",
        "--budget", "30",
        "--buckets", "40",
        "--roles-json", '{"40":"F"}',
    ]
    with mock.patch.object(sys, "argv", argv), \
         mock.patch.object(execute, "_registry", return_value=registry), \
         mock.patch.object(execute, "_read_ws_snapshot", return_value={}), \
         mock.patch.object(execute, "_quote", return_value={"asks": [{"price": 0.20, "size": 100.0}]}), \
         mock.patch.object(execute, "_fetch_positions", side_effect=slow_positions), \
         mock.patch.object(execute, "_post_local", side_effect=slow_post), \
         mock.patch("builtins.print") as printed:
        code = execute.main()

    report = json.loads(printed.call_args.args[0])
    timings = report["timings_ms"]
    assert code == 0
    assert timings["positions_ms"] >= 30.0
    assert timings["open_orders_ms"] >= 30.0
    assert timings["evidence_parallel_ms"] < timings["positions_ms"] + timings["open_orders_ms"]
    assert timings["planning_ms"] >= 0.0
    assert timings["submit_ms"] == 0.0
    assert "total_ms" in timings
    assert "positions-start" in calls
    assert "/lookup-open-orders-start" in calls


def _rebalance_main_setup(place_response, open_orders=None):
    registry = {
        bucket: {"yes_token": f"token-{bucket}", "condition_id": "condition-1"}
        for bucket in ("37", "38", "39", "40")
    }
    positions = {
        "token-37": {"shares": 500.0, "cost_usdc": 10.0},
        "token-38": {"shares": 594.2725, "cost_usdc": 10.0},
        "token-39": {"shares": 620.5178, "cost_usdc": 10.0},
        "token-40": {"shares": 500.0, "cost_usdc": 10.0},
    }
    calls = []

    def post_local(path, payload, **_kwargs):
        calls.append((path, dict(payload)))
        if path == "/lookup-open-orders":
            return {"ok": True, "open_orders": list(open_orders or [])}
        if path == "/execution-guard-snapshot":
            return {
                "ok": True,
                "side": "SELL",
                "evidence_only": True,
                "authorizes_order": False,
                "evidence_errors": [],
                "conditional_balance_allowance": {"balance": "1000000000", "allowance": "1000000000"},
            }
        if path == "/place-order":
            return dict(place_response)
        raise AssertionError(path)

    argv = [
        "settlement_floor_execute.py",
        "--event-date", "2026-07-24",
        "--city", "chengdu",
        "--budget", "30",
        "--buckets", "37,38,39,40",
        "--roles-json", '{"37":"F","38":"M","39":"S","40":"F"}',
        "--rebalance-sell",
        "--execute",
    ]
    quotes = {
        bucket: {
            "asks": [{"price": 0.20, "size": 1000.0}],
            "bids": [{"price": 0.40, "size": 1000.0}],
        }
        for bucket in ("37", "38", "39", "40")
    }
    patches = [
        mock.patch.object(sys, "argv", argv),
        mock.patch.object(execute, "_registry", return_value=registry),
        mock.patch.object(execute, "_read_ws_snapshot", return_value={}),
        mock.patch.object(execute, "_quote", side_effect=lambda _token, **_kwargs: quotes["39"]),
        mock.patch.object(execute, "_fetch_positions", return_value=positions),
        mock.patch.object(execute, "_post_local", side_effect=post_local),
        mock.patch.object(execute, "_gateway_health", return_value={
            "ready": True,
            "order_submission_enabled": True,
            "live_pause_present": False,
            "read_only": False,
            "proxy": "sakuracat",
        }),
    ]
    return patches, calls


def test_rebalance_uses_sell_identity_and_returns_recompute_after_full_fill():
    patches, calls = _rebalance_main_setup({
        "ok": True,
        "orderID": "sell-1",
        "status": "filled",
        "size_matched": 27.25,
    })
    with mock.patch("builtins.print") as printed:
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            code = execute.main()
    report = json.loads(printed.call_args.args[0])
    assert code == 0
    assert report["status"] == "rebalance_completed_recompute_required"
    assert report["recompute_required"] is True
    guard_calls = [payload for path, payload in calls if path == "/execution-guard-snapshot"]
    place_calls = [payload for path, payload in calls if path == "/place-order"]
    assert guard_calls and all(payload["side"] == "SELL" for payload in guard_calls)
    assert len(place_calls) == 1
    assert place_calls[0]["side"] == "SELL"
    assert place_calls[0]["order_type"] == "FAK"
    assert place_calls[0]["allow_market"] is False
    assert place_calls[0]["order_intent_action"] == "controlled_rebalance"
    assert place_calls[0]["idempotency_key"] != execute._order_identity(
        slug="highest-temperature-in-chengdu-on-july-24-2026",
        event_date="2026-07-24",
        city="chengdu",
        bucket="39",
        token="token-39",
        order_type="FAK",
        price=0.40,
        shares=27.25,
        target_shares=27.25,
    )


def test_rebalance_partial_fill_stops_in_reconcile_without_next_sell():
    patches, calls = _rebalance_main_setup({
        "ok": True,
        "orderID": "sell-partial",
        "status": "partially_filled",
        "size_matched": 10.0,
    })
    with mock.patch("builtins.print") as printed:
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            code = execute.main()
    report = json.loads(printed.call_args.args[0])
    assert code == 4
    assert report["status"] == "reconcile_required_stop"
    assert report["reconcile_required"] is True
    assert report["blocked"] == "settlement_floor_rebalance_partial_fill_reconcile_required"
    assert len([path for path, _payload in calls if path == "/place-order"]) == 1


def test_rebalance_refuses_to_duplicate_relevant_open_order():
    patches, calls = _rebalance_main_setup(
        {"ok": True, "orderID": "unused", "status": "filled", "size_matched": 27.25},
        open_orders=[{"asset_id": "token-39", "id": "existing-sell", "status": "LIVE"}],
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        try:
            execute.main()
        except RuntimeError as exc:
            assert str(exc) == "settlement_floor_rebalance_relevant_open_orders_present"
        else:
            raise AssertionError("relevant open order must block controlled rebalance")
    assert not [path for path, _payload in calls if path == "/place-order"]
