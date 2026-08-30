import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE_URI = (ROOT / "scripts" / "clob_order_service.mjs").as_uri()


def _run(body: str, tmp_path: Path) -> dict:
    # Pin the neighbouring gates: importing the repo's runtime modules exports
    # proxy env into the pytest process, and an inherited PM_CLOB_FORCE_PROXY
    # would reject ahead of the gate under test.
    script = f"""
      process.env.PM_DATA_DIR = {json.dumps(str(tmp_path))};
      process.env.PM_CLOB_FORCE_PROXY = '0';
      process.env.PM_CLOB_IGNORE_REGION_BLOCK = '0';
      const {{ ClobOrderService }} = await import({json.dumps(SERVICE_URI)});
      {body}
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT, text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_reachability_alone_never_attests_the_exchange_visible_region():
    source = (ROOT / "scripts" / "clob_order_service.mjs").read_text(encoding="utf-8")
    selection = source.split("async function configureProxy()", 1)[1].split(
        "\n}\n", 1
    )[0]
    # Reading the public CLOB succeeds from a restricted region, so a route may
    # never be selected on reachability when an admitting one is available.
    assert "probeRegion(candidate).blocked === false" in selection
    assert "region: probeRegion(proxy)" in selection


def test_undecided_probe_never_reports_a_fresh_verdict(tmp_path):
    payload = _run(
        """
      const service = new ClobOrderService();
      service.recordRegion({ blocked: true, country: 'US', region: 'MA' });
      const blockedFresh = service.regionBlockedNow();
      service.recordRegion({ blocked: null, error: 'region_probe_failed' });
      console.log(JSON.stringify({
        blockedFresh,
        afterFailedProbe: service.regionBlockedNow(),
        observedAt: service.regionObservedAt,
      }));
    """,
        tmp_path,
    )
    assert payload["blockedFresh"] is True
    assert payload["afterFailedProbe"] is False
    assert payload["observedAt"] == ""


def test_stale_blocked_verdict_stops_rejecting_orders(tmp_path):
    payload = _run(
        """
      process.env.PM_CLOB_REGION_MAX_AGE_SEC = '60';
      const service = new ClobOrderService();
      service.recordRegion({ blocked: true, country: 'US', region: 'MA' });
      service.regionObservedAt = new Date(Date.now() - 3600_000).toISOString();
      console.log(JSON.stringify({ blocking: service.regionBlockedNow() }));
    """,
        tmp_path,
    )
    assert payload["blocking"] is False


def test_fresh_region_block_rejects_before_persistence_signing_or_posting(tmp_path):
    payload = _run(
        """
      const service = new ClobOrderService();
      service.persistIntent = () => { throw new Error('must not persist'); };
      service.client = {
        createOrder: async () => { throw new Error('must not sign'); },
        postOrder: async () => { throw new Error('must not post'); },
      };
      service.recordRegion({ blocked: true, country: 'US', region: 'MA' });
      console.log(JSON.stringify(await service.placeOrderWithIdempotency({
        idempotency_key: 'region-test', order_intent_id: 'region-intent',
        token_id: '1', side: 'BUY', price: 0.5, size: 2, order_type: 'FOK',
      })));
    """,
        tmp_path,
    )
    assert payload["error"] == "clob_region_blocked:US"
    assert payload["error_class"] == "clob_order_geoblocked"
    assert payload["fatal"] is True
    # A rejection that never reached the exchange must not read as in-flight:
    # the caller reserves budget for anything it has to reconcile later.
    assert payload["exchange_post_started"] is False
    assert payload["reconcile_required"] is False
    assert payload["pending"] is False
    assert payload["accepted"] is False
    assert not payload.get("orderID")


def test_region_block_rejection_is_not_reconcilable(tmp_path):
    """A route the gateway refused before signing owes nobody a reconcile.

    This used to assert the ASOS execution lane's budget states too -- that a
    region rejection landed in a state which released the reservation rather
    than one which held it pending reconcile.  That lane was retired on
    2026-08-31 (one mainline, the basket ladder), and its module went with
    it.  What the gateway itself promises is unchanged and is what remains
    here: nothing was posted, so nothing is in flight.
    """

    response = _run(
        """
      const service = new ClobOrderService();
      service.recordRegion({ blocked: true, country: 'US', region: 'MA' });
      console.log(JSON.stringify(await service.placeOrderWithIdempotency({
        idempotency_key: 'region-test', order_intent_id: 'region-intent',
      })));
    """,
        tmp_path,
    )
    reconcile_required = (
        response.get("reconcile_required")
        or response.get("exchange_post_started") is True
        or response.get("ok") is True
        or response.get("orderID")
    )
    assert not reconcile_required
    assert response.get("exchange_post_started") is False
