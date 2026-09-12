import re
from pathlib import Path


def _scan_weather_market_lines():
    return Path("scan_weather_market.py").read_text().splitlines()


def _mirror_follow_start_line(lines):
    for idx, line in enumerate(lines, start=1):
        if line.startswith("def run_mirror_follow_trades("):
            return idx
    return len(lines) + 1


def test_clob_gateways_forward_post_only_to_sdk():
    cli = Path("scripts/polymarket_order.mjs").read_text(encoding="utf-8")
    service = Path("scripts/clob_order_service.mjs").read_text(encoding="utf-8")

    assert 'args["post-only"]' in cli
    assert "post_only_market_order_forbidden" in cli
    assert "orderType,\n            postOnly,\n            deferExec" in cli
    assert "rawPayload.post_only" in service
    assert "post_only_market_order_forbidden" in service
    assert "postOrder(signedOrder, orderType, postOnly, deferExec)" in service


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


def test_the_gateway_remembers_a_submission_for_longer_than_two_hours():
    """Retention here is how long a lost order stays findable.

    A POST that times out leaves the ledger at reconcile_required with no
    exchange order id.  ``/reconcile-order`` recovers it by asking the
    exchange for the deterministic id the signer computed before the POST
    -- and it can only do that while the gateway's own row survives.

    At a two-hour TTL it does not.  Measured 2026-09-10: an order accepted
    at 2026-09-09T13:38:19Z was unrecoverable by 15:38 and still resting
    seventeen hours later; ``/order-status`` and ``/reconcile-order`` both
    answered ``found: false`` and the store held zero rows.

    The row cap is pinned with the TTL because pruning is by count as well
    as by age: at roughly 230 submissions a day the old 512 came due long
    before two hours did, so a longer TTL on its own would have changed
    nothing.  Retention is also the duplicate-submission guard -- a key the
    store has forgotten looks new -- so shorter is not the safer default.
    """

    service = Path("scripts/clob_order_service.mjs").read_text(encoding="utf-8")
    ttl = int(re.search(
        r'PM_CLOB_ORDER_SERVICE_IDEMPOTENCY_TTL_SEC", "(\d+)"', service,
    ).group(1))
    cap = int(re.search(
        r'PM_CLOB_ORDER_SERVICE_IDEMPOTENCY_MAX", "(\d+)"', service,
    ).group(1))

    assert ttl >= 24 * 3600, ttl
    assert cap >= 3 * 230, cap
    # ``positiveNumber(env(NAME, default), fallback)`` carries the number
    # twice.  Changing one and not the other reads as changed and behaves
    # as before for anyone whose env sets the variable to something
    # unparseable, which is the quietest way to undo this.
    assert f'PM_CLOB_ORDER_SERVICE_IDEMPOTENCY_TTL_SEC", "{ttl}"), {ttl})' in service
    assert f'PM_CLOB_ORDER_SERVICE_IDEMPOTENCY_MAX", "{cap}"), {cap})' in service
