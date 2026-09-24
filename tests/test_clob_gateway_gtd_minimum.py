"""The gateway's GTD floor is the venue's three minutes after its one-minute
threshold: 240 seconds.

It read 60 seconds -- the one-minute security threshold taken for the
minimum -- until 2026-09-14, then 180, the documented three minutes taken on
the stamp itself.  The venue takes the threshold off first: every one of the
consensus lane's 19 rungs, stamped 209 s ahead on arrival, came back
"expiration is less than 180 seconds in the future" (2026-09-23).  Production
signs its floor 30 seconds above the venue's number so the gateway's
arrival-time check cannot refuse it in transit; both numbers are pinned here
against the one production exports.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from bayes_basket_target.production import (
    GTD_SIGNING_MARGIN_SECONDS,
    VENUE_GTD_MINIMUM_HORIZON_SECONDS,
    _venue_order_fields,
)


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "scripts" / "clob_order_service.mjs"


def _place_gtd(seconds_ahead: int) -> dict:
    script = f"""
      process.env.PM_CLOB_FORCE_PROXY = '0';
      import {{ ClobOrderService }} from {json.dumps(SERVICE.as_uri())};
      const zero = '0x0000000000000000000000000000000000000000';
      const bytes32 = `0x${{'00'.repeat(32)}}`;
      const calls = [];
      const service = new ClobOrderService();
      service.persistIntent = () => {{}};
      service.client = {{
        tickSizes: {{}}, negRisk: {{}},
        createOrder: async (order) => {{
          calls.push('createOrder');
          return {{
            salt:'1', maker:zero, signer:zero, tokenId:order.tokenID,
            makerAmount:'1000000', takerAmount:'5000000', side:'BUY', signatureType:0,
            timestamp:'1720000000', metadata:bytes32, builder:bytes32, signature:'0x00',
          }};
        }},
        postOrder: async () => {{ calls.push('postOrder'); return {{orderID:'gtd',status:'live'}}; }},
      }};
      let result;
      try {{
        result = await service.placeOrder({{
          token_id:'99', price:.20, size:5, side:'BUY', order_type:'GTD', post_only:true,
          expiration: Math.floor(Date.now() / 1000) + {int(seconds_ahead)},
          order_intent_id:'gtd-intent',
        }}, 'idem-gtd', {{acceptedMs:Date.now(),lane:'default_serial'}});
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


def test_the_floor_is_four_minutes_on_both_sides() -> None:
    assert VENUE_GTD_MINIMUM_HORIZON_SECONDS == 240.0
    source = SERVICE.read_text(encoding="utf-8")
    assert "const GTD_MINIMUM_HORIZON_SECONDS = 240;" in source


def test_an_expiry_inside_three_minutes_is_refused_before_signing() -> None:
    out = _place_gtd(120)
    assert out["result"]["thrown"].startswith("gtd_expiration_too_soon")
    assert out["calls"] == []


def test_the_expiry_the_venue_refused_is_refused_here_first() -> None:
    """What the consensus lane signed on 2026-09-23: 209 s ahead on arrival."""

    out = _place_gtd(209)
    assert out["result"]["thrown"].startswith("gtd_expiration_too_soon")
    assert out["calls"] == []


def test_what_production_signs_at_the_floor_clears_the_gateway() -> None:
    fields = _venue_order_fields(
        "MAKER",
        created_at_utc="2026-09-11T00:00:00Z",
        limit_price=0.20,
        belief_fresh_until_utc="2026-09-11T00:00:20Z",
    )
    assert fields["expiration_basis"] == "venue_minimum_horizon"
    signed_ahead = fields["expiration"] - 1789084800  # 2026-09-11T00:00:00Z
    assert signed_ahead == VENUE_GTD_MINIMUM_HORIZON_SECONDS + GTD_SIGNING_MARGIN_SECONDS

    # Arriving up to the intent's three-second execution deadline later.
    out = _place_gtd(int(signed_ahead) - 3)
    assert out["result"].get("ok") is True, out
    assert out["calls"] == ["createOrder", "postOrder"]
