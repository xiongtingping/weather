"""The exchange's minimum lot, enforced before anything is signed.

Polymarket refuses an order below the market's ``orderMinSize``.  Until
2026-08-30 nothing local held that rule, so an undersized order was discovered
by building it, signing it, posting it and reading the refusal -- by which
point a durable idempotency record already existed for an order that never had
a chance.  On the intents on disk when this was written that was the common
case, not the edge: 275 of 1,406 basket intents and 27 of 31 ASOS argmax
intents were under a 5-share lot, and no ASOS intent ever came back with an
exchange order id.

These tests pin both halves: the collector persists the rule, and the gateway
refuses locally when an order does not meet it -- including when the rule
cannot be read at all, which must fail closed rather than assume no minimum.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

from weather_basket_yes_schema import apply_weather_basket_yes_schema


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "123456"


def _registry_db(db_path: Path, registry_extra: dict | None = None) -> None:
    conn = sqlite3.connect(db_path)
    apply_weather_basket_yes_schema(conn)
    registry = {
        "schema": "reported_trajectory_market_registry_v1",
        "condition_id": "0xcondition",
        "yes_token": TOKEN,
        "no_token": "654321",
    }
    registry.update(registry_extra or {})
    conn.execute(
        """INSERT INTO reported_trajectory_market_registry(
             slug,event_date,outcome,city_slug,station_id,settlement_timezone,
             settlement_unit,source_lane,source_eligible,source_block_reason,
             resolution_url,rules_sha256,yes_token,no_token,market_active,
             market_accepting_orders,registry_json,refreshed_at
           ) VALUES('slug','2099-08-05','82-83°F','atlanta','KATL','America/New_York',
                    'F','awc',1,NULL,'url',?,?,'654321',1,1,?,
                    '2099-08-05T00:00:00Z')""",
        ("a" * 64, TOKEN, json.dumps(registry)),
    )
    conn.commit()
    conn.close()


def _run_node(script: str, db_path: Path, tmp_path: Path) -> dict:
    env = {
        **os.environ,
        "PM_DATA_DIR": str(tmp_path),
        "PM_ARCHIVE_DB": str(db_path),
        "PM_CLOB_ORDER_SERVICE_READ_ONLY": "0",
    }
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT, env=env,
        text=True, capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_sizing_rule_is_read_from_the_registry_and_fails_closed_without_it(
    tmp_path: Path,
) -> None:
    present = tmp_path / "present.db"
    _registry_db(present, {"order_min_size": 5, "order_price_min_tick_size": 0.001})
    absent = tmp_path / "absent.db"
    _registry_db(absent)

    service_uri = (ROOT / "scripts" / "clob_order_service.mjs").as_uri()
    script = f"""
      const {{ exchangeOrderSizingRule }} = await import({json.dumps(service_uri)});
      console.log(JSON.stringify({{
        present: exchangeOrderSizingRule({json.dumps(TOKEN)}, {json.dumps(str(present))}),
        absent: exchangeOrderSizingRule({json.dumps(TOKEN)}, {json.dumps(str(absent))}),
        unknownToken: exchangeOrderSizingRule('999999', {json.dumps(str(present))}),
        malformedToken: exchangeOrderSizingRule('not-a-token', {json.dumps(str(present))}),
        noDatabase: exchangeOrderSizingRule(
          {json.dumps(TOKEN)}, {json.dumps(str(tmp_path / "missing.db"))}),
      }}));
    """
    value = _run_node(script, present, tmp_path)

    assert value["present"] == {"ok": True, "minSize": 5, "tickSize": 0.001}
    # A row that predates the collector change carries no rule.  It must not
    # read as "no minimum".
    assert value["absent"]["ok"] is False
    assert value["absent"]["reason"] == "exchange_order_sizing_rule_missing"
    assert value["unknownToken"]["reason"] == "exchange_order_sizing_market_unknown"
    assert value["malformedToken"]["reason"] == "exchange_order_sizing_token_invalid"
    assert value["noDatabase"]["reason"] == "exchange_order_sizing_registry_unavailable"


def test_effective_shares_covers_every_order_shape(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _registry_db(db_path, {"order_min_size": 5, "order_price_min_tick_size": 0.001})
    service_uri = (ROOT / "scripts" / "clob_order_service.mjs").as_uri()
    script = f"""
      const {{ effectiveOrderShares }} = await import({json.dumps(service_uri)});
      console.log(JSON.stringify({{
        limit: effectiveOrderShares({{size: 12.5, price: 0.16}}),
        buyMarket: effectiveOrderShares({{amount: 2, price: 0.5, side: 'BUY'}}),
        sellMarket: effectiveOrderShares({{amount: 9, side: 'SELL'}}),
        sizeWins: effectiveOrderShares({{size: 7, amount: 100, price: 0.5}}),
        noPrice: effectiveOrderShares({{amount: 2, side: 'BUY'}}),
        empty: effectiveOrderShares({{}}),
      }}));
    """
    value = _run_node(script, db_path, tmp_path)

    assert value["limit"] == 12.5
    assert value["buyMarket"] == 4
    assert value["sellMarket"] == 9
    assert value["sizeWins"] == 7
    # A BUY notional with no price does not determine a share count.  NaN is
    # not JSON, so it lands as null -- and the gateway treats it as fail-closed.
    assert value["noPrice"] is None
    assert value["empty"] is None


def test_undersized_order_is_refused_before_anything_is_signed(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "archive.db"
    _registry_db(db_path, {"order_min_size": 5, "order_price_min_tick_size": 0.001})
    service_uri = (ROOT / "scripts" / "clob_order_service.mjs").as_uri()
    base = {
        "strategy_id": "bayes_basket_target_v1",
        "token_id": TOKEN, "side": "BUY", "order_type": "GTC",
        "price": 0.66, "post_only": True,
    }
    script = f"""
      const {{ ClobOrderService }} = await import({json.dumps(service_uri)});
      const base = {json.dumps(base)};
      const service = new ClobOrderService();
      const under = await service.placeOrderWithIdempotency(
        {{...base, size: 3.03, idempotency_key: 'k-under', order_intent_id: 'i-under'}});
      const exact = await service.placeOrderWithIdempotency(
        {{...base, size: 5, idempotency_key: 'k-exact', order_intent_id: 'i-exact'}});
      const over = await service.placeOrderWithIdempotency(
        {{...base, size: 12.5, idempotency_key: 'k-over', order_intent_id: 'i-over'}});
      console.log(JSON.stringify({{under, exact, over}}));
    """
    value = _run_node(script, db_path, tmp_path)

    under = value["under"]
    assert under["error"] == "exchange_min_order_size_not_met:5"
    assert under["error_class"] == "exchange_order_below_minimum_lot"
    assert under["ok"] is False
    assert under["accepted"] is False
    assert under["fatal"] is True
    assert under["retryable"] is False
    assert under["exchange_post_started"] is False
    assert under["exchange_min_order_size"] == 5
    assert under["exchange_price_min_tick_size"] == 0.001
    assert under["requested_size"] == 3.03
    assert under["order_intent_id"] == "i-under"

    # The boundary is inclusive, and the gate must not be the thing that stops
    # a large enough order.  Both of these get past it and fail further down
    # for want of exchange credentials -- which is what proves the rejection
    # above is the minimum-lot gate and not something the fixture broke.
    for key in ("exact", "over"):
        assert value[key].get("error") != "exchange_min_order_size_not_met:5"
        assert value[key].get("error_class") != "exchange_order_below_minimum_lot"
        assert value[key].get("exchange_post_started") is not True


def test_order_is_refused_when_the_registry_carries_no_sizing_rule(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "archive.db"
    _registry_db(db_path)
    service_uri = (ROOT / "scripts" / "clob_order_service.mjs").as_uri()
    script = f"""
      const {{ ClobOrderService }} = await import({json.dumps(service_uri)});
      const service = new ClobOrderService();
      const result = await service.placeOrderWithIdempotency({{
        strategy_id: 'bayes_basket_target_v1', token_id: {json.dumps(TOKEN)},
        side: 'BUY', order_type: 'GTC', price: 0.66, size: 1000,
        post_only: true, idempotency_key: 'k', order_intent_id: 'i',
      }});
      console.log(JSON.stringify({{result}}));
    """
    value = _run_node(script, db_path, tmp_path)

    # Comfortably above any plausible minimum, and still refused: an unreadable
    # sizing rule is not permission to guess.
    assert value["result"]["error"] == "exchange_order_sizing_rule_missing"
    assert value["result"]["error_class"] == "exchange_order_sizing_unknown"
    assert value["result"]["exchange_post_started"] is False


def test_a_market_order_sized_in_cash_is_not_judged_by_the_share_minimum(
    tmp_path: Path,
) -> None:
    """The rule is scoped to the shape the exchange has actually judged.

    All 137 rejections on record read "Size (0.32) lower than the minimum: 5"
    and every one of them is a GTC order.  A market order is sized in USDC
    and swept through the ask levels; what the exchange requires of one is a
    dollar figure nothing here has observed.  Asserting five shares against
    it would be inventing the exchange's rule rather than enforcing it.
    """
    db_path = tmp_path / "archive.db"
    _registry_db(db_path, {"order_min_size": 5, "order_price_min_tick_size": 0.001})
    service_uri = (ROOT / "scripts" / "clob_order_service.mjs").as_uri()
    script = f"""
      const {{ ClobOrderService }} = await import({json.dumps(service_uri)});
      const service = new ClobOrderService();
      const base = {{
        strategy_id: 'bayes_basket_target_v1', token_id: {json.dumps(TOKEN)},
        side: 'BUY', allow_market: true,
      }};
      // 0.50 USDC at 0.66 is well under a five-share lot either way.
      const marketOrder = await service.placeOrderWithIdempotency({{
        ...base, order_type: 'FOK', amount: 0.5, price: 0.66,
        idempotency_key: 'k-market', order_intent_id: 'i-market',
      }});
      const limitOrder = await service.placeOrderWithIdempotency({{
        ...base, order_type: 'GTC', size: 0.76, price: 0.66, post_only: true,
        idempotency_key: 'k-limit', order_intent_id: 'i-limit',
      }});
      console.log(JSON.stringify({{marketOrder, limitOrder}}));
    """
    value = _run_node(script, db_path, tmp_path)

    # Same token, same money, same book: only the shape differs.
    assert value["limitOrder"]["error"] == "exchange_min_order_size_not_met:5"
    assert value["limitOrder"]["exchange_post_started"] is False

    assert value["marketOrder"].get("error") != "exchange_min_order_size_not_met:5"
    assert value["marketOrder"].get("error_class") != "exchange_order_below_minimum_lot"
    assert value["marketOrder"].get("error_class") != "exchange_order_sizing_unknown"
