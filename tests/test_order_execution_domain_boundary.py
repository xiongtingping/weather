import json
import subprocess
from pathlib import Path
from unittest import mock

import scan_weather_market as swm


def _text(path: str) -> str:
    return Path(path).read_text()


def test_run_node_order_dry_run_attaches_execution_domain_context():
    with mock.patch.dict("os.environ", {"PM_AUTO_TRADE_DRY_RUN": "1"}, clear=False):
        ok, payload = swm._run_node_order(
            "place-order",
            "tok-domain",
            "BUY",
            price=0.42,
            size=2.0,
            order_type="GTC",
            order_context={
                "execution_domain": swm.ORDER_EXECUTION_DOMAIN_MIRROR_FOLLOW,
                "order_execution_source": "unit_test",
                "intent_action": "entry",
            },
        )

    assert ok is True
    assert payload["dry_run"] is True
    assert payload["order_execution_context_version"] == swm.ORDER_EXECUTION_CONTEXT_VERSION
    assert payload["order_execution_domain"] == swm.ORDER_EXECUTION_DOMAIN_MIRROR_FOLLOW
    assert payload["execution_domain"] == swm.ORDER_EXECUTION_DOMAIN_MIRROR_FOLLOW
    assert payload["order_execution_source"] == "unit_test"
    assert payload["order_intent_action"] == "entry"


def test_run_node_order_clob_service_request_carries_execution_domain_context():
    captured = {}

    def _fake_service(payload, timeout_sec=0):
        captured["payload"] = dict(payload)
        captured["timeout_sec"] = timeout_sec
        return True, {"orderID": "oid-domain", "status": "submitted"}

    with mock.patch.dict(
        "os.environ",
        {
            "PM_AUTO_TRADE_DRY_RUN": "0",
            "PM_CLOB_ORDER_SERVICE_ENABLED": "1",
            "PM_MAX_ORDER_USDC": "10",
            "PM_AUTO_TRADE_ENTRY_BUDGET_RATIO": "1",
        },
        clear=False,
    ):
        with mock.patch.object(swm, "ensure_system_proxy_settings", return_value=None):
            with mock.patch.object(swm, "_resolve_node_bin", return_value="node"):
                with mock.patch.object(
                    swm,
                    "_run_clob_order_service_place_order",
                    side_effect=_fake_service,
                ):
                    ok, payload = swm._run_node_order(
                        "place-order",
                        "tok-service-domain",
                        "SELL",
                        price=0.58,
                        size=1.25,
                        order_type="GTC",
                        order_context={
                            "execution_domain": swm.ORDER_EXECUTION_DOMAIN_MANUAL_SMOKE_TEST,
                            "order_execution_source": "ui_auto_trade_smoke_test_http",
                            "order_execution_reason": "manual_real_money_smoke_test",
                            "manual_confirm_source": "ui_auto_trade_smoke_test_http",
                            "intent_action": "exit",
                        },
                    )

    assert ok is True
    assert payload["orderID"] == "oid-domain"
    assert payload["order_execution_domain"] == swm.ORDER_EXECUTION_DOMAIN_MANUAL_SMOKE_TEST
    assert captured["payload"]["order_execution_domain"] == swm.ORDER_EXECUTION_DOMAIN_MANUAL_SMOKE_TEST
    assert captured["payload"]["execution_domain"] == swm.ORDER_EXECUTION_DOMAIN_MANUAL_SMOKE_TEST
    assert captured["payload"]["order_execution_source"] == "ui_auto_trade_smoke_test_http"
    assert captured["payload"]["order_intent_action"] == "exit"
    assert captured["payload"]["manual_confirm_source"] == "ui_auto_trade_smoke_test_http"


def test_execution_domain_metadata_does_not_change_python_service_idempotency_key():
    base_payload = {
        "token_id": "tok-idem",
        "side": "BUY",
        "order_type": "GTC",
        "price": 0.44,
        "size": 2.0,
        "amount": None,
        "allow_market": False,
    }
    key_base = swm._clob_order_service_idempotency_key(base_payload)
    key_with_audit = swm._clob_order_service_idempotency_key(
        {
            **base_payload,
            "execution_domain": "mirror_follow",
            "order_execution_domain": "mirror_follow",
            "order_execution_source": "run_mirror_follow_trades",
            "order_execution_context_version": swm.ORDER_EXECUTION_CONTEXT_VERSION,
            "order_execution_reason": "source_wallet_mirror_strategy",
            "order_intent_action": "entry",
            "manual_confirm_source": "unit",
        }
    )
    assert key_with_audit == key_base


def test_clob_service_stable_key_ignores_execution_domain_metadata_static():
    source = _text("scripts/clob_order_service.mjs")
    stable_key_block = source[source.index("function stableKey(payload)") : source.index("function isHttpErrorStatus")]
    for field in (
        "execution_domain",
        "order_execution_domain",
        "order_execution_source",
        "order_execution_context_version",
        "order_execution_reason",
        "order_intent_action",
        "manual_confirm_source",
    ):
        assert f"delete copy.{field};" in stable_key_block


def test_gateway_uses_only_target_execution_hard_deadline_for_direct_entry():
    source = _text("scripts/clob_order_service.mjs")
    assert "export function targetDirectEntryHardDeadline(payload, nowMs = Date.now())" in source
    assert 'const rawDeadline = payload?.target_execution_hard_deadline_at;' in source
    assert 'reason: "target_execution_hard_deadline_missing_or_invalid"' in source
    assert 'reason: "target_execution_hard_deadline_expired"' in source
    assert "sequential_bayes_execution_contract" not in source
    assert "observation_to_order_" not in source
    assert "signal_to_order_" not in source
    assert "dynamic_three_leg" not in source
    assert "target_realization" not in source


def test_gateway_target_deadline_is_fail_closed_and_not_required_for_exit():
    service_uri = (Path(__file__).resolve().parents[1] / "scripts" / "clob_order_service.mjs").as_uri()
    script = f"""
      import {{ targetDirectEntryHardDeadline }} from {json.dumps(service_uri)};
      const future = targetDirectEntryHardDeadline({{
        direct_target_order_context:true,
        side:'BUY',
        target_execution_hard_deadline_at:'2099-01-01T00:00:00.000Z',
      }}, Date.parse('2026-01-01T00:00:00.000Z'));
      const missing = targetDirectEntryHardDeadline({{
        direct_target_order_context:true, side:'BUY',
      }}, Date.parse('2026-01-01T00:00:00.000Z'));
      const expired = targetDirectEntryHardDeadline({{
        direct_target_order_context:true, side:'BUY',
        target_execution_hard_deadline_at:'2025-01-01T00:00:00.000Z',
      }}, Date.parse('2026-01-01T00:00:00.000Z'));
      const exit = targetDirectEntryHardDeadline({{
        direct_target_order_context:true, direct_target_exit_order:true, side:'SELL',
      }}, Date.parse('2026-01-01T00:00:00.000Z'));
      if (!future.ok || !future.required
          || missing.ok || missing.reason !== 'target_execution_hard_deadline_missing_or_invalid'
          || expired.ok || expired.reason !== 'target_execution_hard_deadline_expired'
          || !exit.ok || exit.required) {{
        throw new Error(JSON.stringify({{future,missing,expired,exit}}));
      }}
      console.log(JSON.stringify({{ok:true}}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"ok": True}


def test_clob_service_checks_target_deadline_at_each_exchange_post_static():
    source = _text("scripts/clob_order_service.mjs")
    assert "const assertTargetDirectEntryDeadline = () => {" in source
    assert source.count("assertTargetDirectEntryDeadline();") == 2
    for post_call in (
        "this.client.postOrder(signedOrder, orderType, false, deferExec)",
        "this.client.postOrder(signedOrder, orderType, postOnly, deferExec)",
    ):
        post_index = source.index(post_call)
        assert source.rfind("assertTargetDirectEntryDeadline();", 0, post_index) > source.rfind(
            "persistSignedPrepared();", 0, post_index
        )
    assert "exchange_post_started_at: exchangePostStartedAt" in source
    assert "order_submitted_at: exchangePostStartedAt" in source


def test_exchange_post_t5_requires_gateway_attestation():
    t5 = "2026-07-13T04:00:02.500Z"
    assert swm._exchange_post_t5_from_order_payload({"order_submitted_at": t5}) == ""
    assert swm._exchange_post_t5_from_order_payload(
        {
            "exchange_post_started": True,
            "exchange_post_started_at": t5,
            "order_submitted_at": t5,
        }
    ) == t5
    assert swm._exchange_post_t5_from_order_payload(
        {
            "exchange_post_started": True,
            "exchange_post_started_at": t5,
            "order_submitted_at": "2026-07-13T04:00:02.600Z",
        }
    ) == ""


def test_submitted_terminal_does_not_invent_missing_exchange_t5():
    details = {
        "signal_id": "sig-no-fake-t5",
        "awc_hard_source_signal_at": "2026-07-13T04:00:00Z",
        "signal_to_order_timing": {
            "signal_id": "sig-no-fake-t5",
            "awc_hard_source_signal_at": "2026-07-13T04:00:00Z",
        },
    }
    swm._apply_signal_to_order_terminal(
        details,
        terminal_state="submitted",
        terminal_reason="order_submitted",
        terminal_at="2026-07-13T04:00:02Z",
    )
    timing = details["signal_to_order_timing"]
    assert "order_submitted_at" not in timing
    assert timing["signal_to_order_slo_met"] is False
    assert timing["signal_to_order_slo_violation_reason"] == "exchange_post_t5_missing"


def test_clob_service_passes_sdk_tick_size_as_supported_string_static():
    source = _text("scripts/clob_order_service.mjs")

    assert "function normalizeTickSize(value, fallback = \"0.01\")" in source
    assert 'new Set(["0.1", "0.01", "0.001", "0.0001"])' in source
    assert "rememberTokenMetadata(tokenID, tickSize, negRisk" in source
    assert "this.client.tickSizes[normalizedTokenID] = normalizedTickSize" in source
    assert "this.client.negRisk[normalizedTokenID] = normalizedNegRisk" in source
    assert "let tickSize = normalizeTickSize(rawPayload.tick_size ?? rawPayload.tickSize, \"\")" in source
    assert "positiveNumber(env(\"PM_CLOB_DEFAULT_TICK_SIZE\", \"0.01\"), 0.01)" not in source


def test_clob_service_does_not_replay_local_order_build_to_string_errors_static():
    source = _text("scripts/clob_order_service.mjs")

    assert "function isRetryableLocalOrderBuildError(text)" in source
    assert "Cannot read properties of undefined (reading 'toString')" in source
    assert "cachedResponse?.ok === false && isRetryableLocalOrderBuildError" in source
    assert "if (!isRetryableLocalOrderBuildError(response.error))" in source


def test_clob_service_classifies_order_submit_failures_static():
    source = _text("scripts/clob_order_service.mjs")

    assert "function classifyClobOrderError(err)" in source
    for error_class in (
        "clob_order_geoblocked",
        "clob_order_gateway_error",
        "clob_order_timeout",
        "clob_order_rate_limited",
        "clob_order_service_local_failure",
        "clob_order_rejected",
    ):
        assert error_class in source
    assert "last_place_order_error_class" in source
    assert "last_place_order_geoblocked" in source


def test_scan_weather_market_writes_classified_place_order_failures_static():
    source = _text("scan_weather_market.py")

    assert "def _order_failure_classification(payload: Dict[str, Any])" in source
    assert "def _order_failure_reason_codes(payload: Dict[str, Any])" in source
    assert "def _order_failure_last_status(payload: Dict[str, Any])" in source
    assert "reason_codes=_order_failure_reason_codes(payload)" in source
    assert "last_status=_order_failure_last_status(payload)" in source


def test_weather_main_chain_place_orders_use_bayes_execution_context_static():
    lines = _text("scan_weather_market.py").splitlines()
    mirror_start = next(
        idx
        for idx, line in enumerate(lines, start=1)
        if line.startswith("def run_mirror_follow_trades(")
    )
    missing = []
    for idx, line in enumerate(lines, start=1):
        if idx >= mirror_start or "_run_node_order(" not in line or "cancel-orders" in line:
            continue
        window = "\n".join(lines[idx - 1 : idx + 22])
        if '"place-order"' not in window:
            continue
        before = "\n".join(lines[max(0, idx - 300) : idx + 22])
        if (
            "_bayes_weather_order_context(" not in window
            and not (
                "order_context=" in window
                and any(
                    marker in before
                    for marker in (
                        "_bayes_weather_order_context(",
                        "_persist_pending_submit_intent(",
                        "_persist_external_pending_submit_intent(",
                    )
                )
            )
        ):
            missing.append(idx)
    assert missing == []


def test_mirror_and_smoke_place_orders_use_separate_execution_domains_static():
    scan_source = _text("scan_weather_market.py")
    mirror_source = scan_source[
        scan_source.index("def run_mirror_follow_trades(") : scan_source.index("def run_mirror_follow_trades_with_auto_refresh")
    ]
    assert "ORDER_EXECUTION_DOMAIN_MIRROR_FOLLOW" in mirror_source
    assert 'order_context=_mirror_order_context("entry")' in mirror_source
    assert 'order_context=_mirror_order_context("entry_improve")' in mirror_source
    assert 'order_context=_mirror_order_context("exit")' in mirror_source

    ui_source = _text("ui/server.py")
    smoke_source = ui_source[
        ui_source.index("def _smoke_order_context") : ui_source.index("def _wait_matched")
    ]
    assert '"execution_domain": "manual_smoke_test"' in smoke_source
    assert '"order_execution_source": "ui_auto_trade_smoke_test_http"' in smoke_source
    assert 'order_context=_smoke_order_context("entry")' in ui_source
    assert 'order_context=_smoke_order_context("exit")' in ui_source
