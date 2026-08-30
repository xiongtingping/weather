"""An order the account cannot fund is refused before it is signed.

The third constraint found on 2026-08-31 that lived only at the exchange.
The objective sizes against ``target_remaining_budget_usdc`` -- a number in a
config file, 1014.29 at the time -- and nothing between it and the chain
compared that to the balance.  The guard could not help: ``cachedCollateral``
was a field nothing ever wrote, so the snapshot reported null collateral and
``ok`` together, while health advertised the cache ready off a hardcoded
``const collateralCacheReady = true``.  The account held 0.077345 USDC.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "scripts" / "clob_order_service.mjs"
TOKEN = "123456"


def _run(script: str, tmp_path: Path) -> dict:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT,
        env={
            **os.environ,
            "PM_DATA_DIR": str(tmp_path),
            "PM_ARCHIVE_DB": str(tmp_path / "absent.db"),
            "PM_CLOB_ORDER_SERVICE_READ_ONLY": "0",
        },
        text=True, capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_effective_notional_covers_every_order_shape(tmp_path: Path) -> None:
    script = f"""
      const {{ effectiveOrderNotional }} = await import({json.dumps(SERVICE.as_uri())});
      console.log(JSON.stringify({{
        explicit: effectiveOrderNotional({{notional: 2.5}}),
        marketAmount: effectiveOrderNotional({{amount: 0.5, price: 0.66}}),
        sizeTimesPrice: effectiveOrderNotional({{size: 10, price: 0.05}}),
        explicitWins: effectiveOrderNotional({{notional: 3, size: 10, price: 0.5}}),
        sizeWithoutPrice: effectiveOrderNotional({{size: 10}}),
        empty: effectiveOrderNotional({{}}),
      }}));
    """
    value = _run(script, tmp_path)

    assert value["explicit"] == 2.5
    assert value["marketAmount"] == 0.5
    assert value["sizeTimesPrice"] == 0.5
    assert value["explicitWins"] == 3
    # An order whose cost cannot be determined cannot be checked against a
    # balance.  NaN is not JSON, so it lands as null and reads fail-closed.
    assert value["sizeWithoutPrice"] is None
    assert value["empty"] is None


def test_a_buy_beyond_the_balance_is_refused_before_signing(tmp_path: Path) -> None:
    script = f"""
      const {{ ClobOrderService }} = await import({json.dumps(SERVICE.as_uri())});
      const service = new ClobOrderService();
      service.persistIntent = () => {{ throw new Error('must not persist'); }};
      service.client = {{
        createOrder: async () => {{ throw new Error('must not sign'); }},
        postOrder: async () => {{ throw new Error('must not post'); }},
      }};
      // The real reading taken from the account on 2026-08-31.
      service.cachedCollateral = {{
        value: {{ balance: '77345', allowance: '115792089237316195423570985008687907853269984665640564039457584007913129639935' }},
        observed_at: new Date().toISOString(),
        latency_ms: 1701,
      }};
      const base = {{
        strategy_id: 'bayes_basket_target_v1', token_id: {json.dumps(TOKEN)},
        side: 'BUY', order_type: 'GTC', post_only: true,
      }};
      const out = {{}};
      // The smallest clip on production's grid, three times the balance.
      out.smallestClip = await service.placeOrderWithIdempotency({{
        ...base, notional: 0.25, price: 0.05, size: 5,
        idempotency_key: 'k-clip', order_intent_id: 'i-clip',
      }});
      // Just inside what the account holds.
      out.affordable = await service.placeOrderWithIdempotency({{
        ...base, notional: 0.07, price: 0.01, size: 7,
        idempotency_key: 'k-ok', order_intent_id: 'i-ok',
      }});
      // Cost undeterminable: refused for the same reason.
      out.unknownCost = await service.placeOrderWithIdempotency({{
        ...base, size: 10,
        idempotency_key: 'k-unknown', order_intent_id: 'i-unknown',
      }});
      // A SELL spends conditional tokens, not collateral.
      out.sell = await service.placeOrderWithIdempotency({{
        ...base, side: 'SELL', notional: 900, price: 0.9, size: 1000,
        idempotency_key: 'k-sell', order_intent_id: 'i-sell',
      }});
      console.log(JSON.stringify(out));
    """
    value = _run(script, tmp_path)

    refused = value["smallestClip"]
    assert refused["error"] == "clob_insufficient_collateral:0.077345"
    assert refused["error_class"] == "collateral_insufficient"
    assert refused["ok"] is False
    assert refused["accepted"] is False
    assert refused["fatal"] is True
    assert refused["exchange_post_started"] is False
    assert refused["collateral_available_usdc"] == 0.077345
    assert refused["required_notional_usdc"] == 0.25

    assert value["unknownCost"]["error_class"] == "collateral_insufficient"
    assert value["unknownCost"]["required_notional_usdc"] is None

    # Past the collateral gate and stopped further down for want of exchange
    # credentials -- which is what proves the refusals above are this gate.
    for key in ("affordable", "sell"):
        assert value[key].get("error_class") != "collateral_insufficient", key
        assert value[key].get("error_class") != "collateral_evidence_missing", key
        assert value[key].get("exchange_post_started") is not True, key


def test_missing_or_stale_collateral_evidence_fails_closed(tmp_path: Path) -> None:
    script = f"""
      const {{ ClobOrderService }} = await import({json.dumps(SERVICE.as_uri())});
      const payload = {{
        strategy_id: 'bayes_basket_target_v1', token_id: {json.dumps(TOKEN)},
        side: 'BUY', order_type: 'GTC', post_only: true,
        notional: 0.01, price: 0.01, size: 1,
      }};
      const out = {{}};

      const never = new ClobOrderService();
      out.neverFetched = await never.placeOrderWithIdempotency(
        {{...payload, idempotency_key: 'k1', order_intent_id: 'i1'}});

      const stale = new ClobOrderService();
      stale.cachedCollateral = {{
        value: {{ balance: '1000000000', allowance: '1000000000' }},
        observed_at: new Date(Date.now() - 3600_000).toISOString(),
        latency_ms: 1,
      }};
      out.stale = await stale.placeOrderWithIdempotency(
        {{...payload, idempotency_key: 'k2', order_intent_id: 'i2'}});

      const malformed = new ClobOrderService();
      malformed.cachedCollateral = {{
        value: {{ balance: 'not-a-number' }},
        observed_at: new Date().toISOString(), latency_ms: 1,
      }};
      out.malformed = await malformed.placeOrderWithIdempotency(
        {{...payload, idempotency_key: 'k3', order_intent_id: 'i3'}});

      console.log(JSON.stringify(out));
    """
    value = _run(script, tmp_path)

    # A balance is spendable state: an hour-old reading can approve an order
    # the account has since spent out from under.  Amply funded on paper and
    # still refused.
    for key in ("neverFetched", "stale", "malformed"):
        assert value[key]["error"] == "clob_collateral_evidence_unavailable", key
        assert value[key]["error_class"] == "collateral_evidence_missing", key
        assert value[key]["exchange_post_started"] is False, key
        # Retryable: the next prefetch cycle may supply it.
        assert value[key]["retryable"] is True, key
