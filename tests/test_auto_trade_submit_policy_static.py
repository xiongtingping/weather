from pathlib import Path


def _scan_weather_market_lines():
    return Path("scan_weather_market.py").read_text().splitlines()


def _mirror_follow_start_line(lines):
    for idx, line in enumerate(lines, start=1):
        if line.startswith("def run_mirror_follow_trades("):
            return idx
    return len(lines) + 1


def test_weather_auto_trade_place_order_calls_have_policy_guards():
    lines = _scan_weather_market_lines()
    mirror_start_line = _mirror_follow_start_line(lines)
    missing = []

    for idx, line in enumerate(lines, start=1):
        if idx >= mirror_start_line:
            continue
        if "_run_node_order(" not in line or "cancel-orders" in line:
            continue
        call_window = "\n".join(lines[idx - 1: idx + 9])
        if '"place-order"' not in call_window:
            continue
        before = "\n".join(lines[max(0, idx - 260): idx - 1])
        if '"BUY"' in call_window:
            required_guards = [
                "_entry_order_policy_block_reason",
                'reason_codes=["order_pending_submit"]',
            ]
            pending_submit_guard = True
        elif '"SELL"' in call_window:
            required_guards = ["_exit_order_policy_block_reason"]
            pending_submit_guard = any(
                marker in before
                for marker in (
                    "_persist_pending_submit_intent(",
                    "_persist_external_pending_submit_intent(",
                )
            )
        else:
            continue

        if not pending_submit_guard:
            missing.append((idx, "pending_submit_intent_persistence"))
        for required_guard in required_guards:
            if required_guard not in before:
                missing.append((idx, required_guard))

    assert missing == []


def test_clob_gateways_forward_post_only_to_sdk():
    cli = Path("scripts/polymarket_order.mjs").read_text(encoding="utf-8")
    service = Path("scripts/clob_order_service.mjs").read_text(encoding="utf-8")

    assert 'args["post-only"]' in cli
    assert "post_only_market_order_forbidden" in cli
    assert "orderType,\n            postOnly,\n            deferExec" in cli
    assert "rawPayload.post_only" in service
    assert "post_only_market_order_forbidden" in service
    assert "postOrder(signedOrder, orderType, postOnly, deferExec)" in service


def test_decision_flush_failure_is_not_swallowed_before_commit():
    source = Path("scan_weather_market.py").read_text(encoding="utf-8")

    assert "decision flush failed" not in source
    assert "durable CLOB outbox makes a retry idempotent" in source


def test_decision_buffer_does_not_reinsert_unchanged_payloads():
    source = Path("scan_weather_market.py").read_text(encoding="utf-8")

    assert "persisted_decision_fingerprints" in source
    assert "if payload_fingerprint in persisted_decision_fingerprints" in source
    assert "persisted_decision_fingerprints.add(payload_fingerprint)" in source


def test_clob_service_persists_intent_before_exchange_submit():
    service = Path("scripts/clob_order_service.mjs").read_text(encoding="utf-8")

    pending_idx = service.index('this.persistIntent(key, "pending_submit"')
    submitting_idx = service.index('this.persistIntent(key, "submitting"')
    exchange_idx = service.index(".then(() => this.placeOrder(payload, key")
    assert pending_idx < submitting_idx < exchange_idx
    assert "clob_order_service_idempotency.json" not in service
    assert "clob_order_service_idempotency.sqlite" in service
    assert "reconcile_required" in service


def test_clob_service_persists_deterministic_exchange_id_before_post():
    service = Path("scripts/clob_order_service.mjs").read_text(encoding="utf-8")

    signed_idx = service.index("persistSignedPrepared();")
    post_idx = service.index("this.client.postOrder(", signed_idx)
    assert signed_idx < post_idx
    assert "expectedOrderIdForSignedOrder" in service
    assert 'parsedUrl.pathname === "/reconcile-order"' in service
    assert "this.client.getOrder(expectedOrderID)" in service
