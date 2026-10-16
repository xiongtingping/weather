"""Charter 127(五): a basket completion's limit FOK reaches the exchange.

The gateway's limit-only boundary treats FOK and FAK as market orders and
refuses them unless the caller sends ``allow_market`` -- which no caller on
the basket lane does.  So from go-live until 2026-09-14 every completion the
compile authorized died at the gateway: the first one (2026-09-13 08:46Z,
29°C, 7 shares at 0.16) was recorded ``SUBMIT_REFUSED`` with
``limit_only_order_type:FOK`` and never left this machine.

The fix admits exactly the shape the compile writes -- a BUY FOK signed with
an explicit price and size and carrying its own ``fok_price_cap`` -- and keeps
everything else behind the boundary.  The first test crosses the hop: the
payload is the one ``build_place_order_payload`` builds, not a hand-written
copy of it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from bayes_basket_target.order_submission import build_place_order_payload


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "scripts" / "clob_order_service.mjs"


def _run_gateway(payload: dict) -> dict:
    """Place one order through a gateway whose exchange client is a stub."""

    script = f"""
      process.env.PM_CLOB_FORCE_PROXY = '0';
      delete process.env.PM_ALLOW_MARKET_ORDERS;
      import {{ ClobOrderService }} from {json.dumps(SERVICE.as_uri())};
      const zero = '0x0000000000000000000000000000000000000000';
      const bytes32 = `0x${{'00'.repeat(32)}}`;
      const calls = [];
      const service = new ClobOrderService();
      service.persistIntent = () => {{}};
      service.client = {{
        tickSizes: {{}}, negRisk: {{}},
        createMarketOrder: async () => {{ throw new Error('market builder must not run'); }},
        createOrder: async (order) => {{
          calls.push({{call:'createOrder', order}});
          return {{
            salt:'789', maker:zero, signer:zero, tokenId:order.tokenID,
            makerAmount:'1120000', takerAmount:'7000000', side:'BUY', signatureType:0,
            timestamp:'1720000000', metadata:bytes32, builder:bytes32, signature:'0x00',
          }};
        }},
        postOrder: async (_signed, orderType, postOnly) => {{
          calls.push({{call:'postOrder', orderType, postOnly}});
          return {{orderID:'fok-order',status:'matched',size_matched:'7'}};
        }},
      }};
      let result;
      try {{
        result = await service.placeOrder({json.dumps(payload)}, 'idem', {{acceptedMs:Date.now(),lane:'default_serial'}});
      }} catch (err) {{
        result = {{thrown: String(err?.message || err)}};
      }}
      console.log(JSON.stringify({{result, calls}}));
    """
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT,
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _completion_payload(**overrides) -> dict:
    intent = {
        "order_intent_id": "intent_completion",
        "idempotency_key": "intentempotency_completion",
        "token_id": "99",
        "side": "YES",
        "order_action": "BUY",
        "limit_price": 0.16,
        "requested_size": 7.0,
        "execution_candidate": "TAKER_FOK",
        "post_only": False,
        "order_type": "FOK",
        "fok_price_cap": 0.16,
    }
    payload = build_place_order_payload(intent)
    payload.update(overrides)
    return payload


def test_the_payload_the_compile_builds_is_signed_as_a_limit_and_posted_as_fok() -> None:
    payload = _completion_payload()
    assert "allow_market" not in payload

    out = _run_gateway(payload)

    assert out["result"].get("ok") is True, out
    assert [row["call"] for row in out["calls"]] == ["createOrder", "postOrder"]
    order = out["calls"][0]["order"]
    assert order["price"] == 0.16 and order["size"] == 7 and order["side"] == "BUY"
    assert "expiration" not in order
    assert out["calls"][1]["orderType"] == "FOK"
    assert out["calls"][1]["postOnly"] is False


def test_a_fok_without_a_stated_cap_stays_behind_the_limit_only_boundary() -> None:
    payload = _completion_payload()
    payload.pop("fok_price_cap")

    out = _run_gateway(payload)

    assert out["result"] == {"thrown": "limit_only_order_type:FOK"}
    assert out["calls"] == []


def test_a_limit_above_its_own_cap_is_refused_before_signing() -> None:
    out = _run_gateway(_completion_payload(price=0.17))

    assert out["result"]["thrown"].startswith("fok_limit_price_exceeds_price_cap")
    assert out["calls"] == []


def test_the_cap_opens_a_buy_fak_but_not_a_sell() -> None:
    fak = _run_gateway(_completion_payload(order_type="FAK"))
    assert [row["call"] for row in fak["calls"]] == ["createOrder", "postOrder"]
    assert fak["calls"][1]["orderType"] == "FAK"
    assert _run_gateway(_completion_payload(order_type="FAK", price=0.17))["calls"] == []

    sell = _run_gateway(_completion_payload(side="SELL"))
    assert sell["result"] == {"thrown": "limit_only_order_type:FOK"}
    assert sell["calls"] == []


def test_a_malformed_cap_is_refused_by_name() -> None:
    for cap in (1.0, 0, "abc"):
        out = _run_gateway(_completion_payload(fok_price_cap=cap))
        assert out["result"]["thrown"].startswith("fok_price_cap_invalid"), (cap, out)
        assert out["calls"] == []
