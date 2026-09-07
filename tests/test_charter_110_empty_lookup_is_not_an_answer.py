"""An empty reply is silence, not a verdict -- ask what IS live instead.

Charter 110's cancel rules all read a book, and none of them can run on an
order the venue will not talk about.  Measured 2026-09-07 on the live book:
of fourteen rungs the ledger held as resting, ``/lookup-exchange-order``
answered for two (one LIVE, one MATCHED at 25.00 of 25 the ledger had
recorded as zero) and returned an empty body for twelve.  The lane's first
line is ``if not view["ok"]: return unknown``, and unknown keeps -- so those
twelve could never be cancelled whatever the book did, and each one held
its bucket shut through ``can_submit_next_order`` (14/14 blocked, the
oldest since 2026-09-06T15:53).

The fix is not to infer from the silence, and not to age it out.  It is to
ask a different question.  ``/data/order/{id}`` is a POINT query whose empty
answer is ambiguous by construction -- the gateway's own comment says it
means proxy failure OR an order no longer queryable.  ``/data/orders`` is an
ENUMERATION: absence from a list the exchange itself completed is a fact.

Run against the same fourteen, the enumeration resolved all of them and
agreed with the point query wherever both spoke: one live, thirteen gone.

Which makes ``complete`` the whole contract.  Absence from a TRUNCATED
enumeration is worth nothing, so the gateway has to say which it produced.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_the_gateway_says_whether_the_enumeration_finished() -> None:
    source = (ROOT / "scripts" / "clob_order_service.mjs").read_text(
        encoding="utf-8"
    )
    # The flag exists, starts false, and is only raised where the cursor
    # chain actually ended.
    assert "let complete = false;" in source
    assert "complete," in source
    assert 'if (cursor === "LTE=") complete = true;' in source
    # And it is raised on each of the three ways the loop can legitimately
    # run out: a bare array reply, an exhausted cursor, an empty page.
    body = source.split("async lookupOpenOrders", 1)[1].split("\n  async ", 1)[0]
    assert body.count("complete = true") >= 3, (
        "every terminating branch must raise it, or a truncated list reads "
        "as a complete one"
    )


def test_the_client_can_ask_what_is_live_not_only_about_one_order() -> None:
    from clob_order_service_client import ClobOrderServiceClient

    assert hasattr(ClobOrderServiceClient, "lookup_open_orders")
    doc = ClobOrderServiceClient.lookup_open_orders.__doc__ or ""
    assert "silence" in doc

    sent: dict[str, object] = {}

    class _Probe(ClobOrderServiceClient):
        def __init__(self) -> None:  # noqa: D107 -- no transport wanted
            pass

        def _post(self, path, payload):  # type: ignore[override]
            sent["path"] = path
            sent["payload"] = dict(payload)
            return {"ok": True, "open_orders": [], "complete": True}

    out = _Probe().lookup_open_orders({"token_id": "tok-1"})
    assert sent["path"] == "/lookup-open-orders"
    # Scoped to the one token, because the account-wide page is slow behind
    # the proxy and an unbounded probe is one nobody dares run per rung.
    assert sent["payload"]["token_id"] == "tok-1"
    assert out["complete"] is True


def test_the_reprice_receipt_is_published_when_the_lane_runs(tmp_path) -> None:
    """A lane that cancels real orders must not be observable only on the
    far side of the cycle that contains it.

    Measured 2026-09-07: a production cycle ran 3,912 seconds against a
    720-second overdue threshold, and because ``state["production"]`` is
    only assigned when the whole cycle returns, every stage receipt inside
    it -- including this lane's -- was invisible for over an hour.  The
    daemon's status file showed ``production_inflight`` and nothing else.
    """

    import importlib
    import os

    target = tmp_path / "resting_reprice_latest.json"
    os.environ["PM_RESTING_REPRICE_RECEIPT_PATH"] = str(target)
    try:
        worker = importlib.reload(
            importlib.import_module("bayes_basket_target.production_worker")
        )
        assert worker.RESTING_REPRICE_RECEIPT_PATH == target
        worker._publish_resting_reprice_receipt(
            {"schema": "s", "state": "committed", "cancel_requested": 3},
            as_of_utc="2026-09-07T12:00:00Z",
        )
        written = json.loads(target.read_text(encoding="utf-8"))
        assert written["cancel_requested"] == 3
        assert written["published_at_utc"] == "2026-09-07T12:00:00Z"
    finally:
        os.environ.pop("PM_RESTING_REPRICE_RECEIPT_PATH", None)
        importlib.reload(
            importlib.import_module("bayes_basket_target.production_worker")
        )


def test_publishing_never_breaks_the_cycle(tmp_path) -> None:
    """An observability write may not be the reason a trading cycle fails."""

    from bayes_basket_target import production_worker as worker

    class _Unserializable:
        pass

    # A path that cannot be created, and a payload json would refuse.
    worker._publish_resting_reprice_receipt(
        {"bad": _Unserializable()}, as_of_utc="2026-09-07T12:00:00Z"
    )
