"""Lowering the arming flag must stop sending, not merely record a wish.

The flag was the go-live migration's precondition and nothing else, so once
the migration had minted the live contracts it stopped nothing.  Measured on
2026-08-31: the flag went down at 13:29 and a real order left the process at
21:25, because the contracts, the runtime config, the worker and the gateway
were all still open.  A control that reads like a stop and is not one is
worse than no control, because people rely on it.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import order_submission as sub  # noqa: E402


def _armed(tmp_path: Path, value) -> Path:
    path = tmp_path / "intent.json"
    path.write_text(json.dumps({"live_enabled": value}), encoding="utf-8")
    return path


def test_the_flag_is_read_not_cached(tmp_path) -> None:
    """A cached answer keeps sending after the operator has stopped the lane."""

    path = _armed(tmp_path, True)
    assert sub.live_intent_armed(path) is True
    path.write_text(json.dumps({"live_enabled": False}), encoding="utf-8")
    assert sub.live_intent_armed(path) is False


def test_it_fails_closed(tmp_path) -> None:
    """Missing, unreadable or malformed is not permission.

    The station exclusion list fails open for the opposite reason -- it only
    refuses admissions -- while this one authorises spending.
    """

    assert sub.live_intent_armed(tmp_path / "absent.json") is False
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert sub.live_intent_armed(broken) is False
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    assert sub.live_intent_armed(empty) is False


def test_only_true_arms_it(tmp_path) -> None:
    """Truthy is not armed; the flag is a boolean and is read as one."""

    for value in (False, None, 0, 1, "true", "yes", {}):
        assert sub.live_intent_armed(_armed(tmp_path, value)) is False
    assert sub.live_intent_armed(_armed(tmp_path, True)) is True


def _book(path: Path) -> sqlite3.Connection:
    """One prepared intent on production's own schema.

    Built from ``PRODUCTION_LEDGER_DDL`` rather than a hand-written subset:
    the submitter's first act is a schema guard over a dozen tables, and a
    fixture that transcribes them is a fixture that drifts.
    """

    from bayes_basket_target.ssot import PRODUCTION_LEDGER_DDL  # noqa: PLC0415

    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(PRODUCTION_LEDGER_DDL)
    conn.execute(
        "INSERT INTO basket_target_order_intents("
        " order_intent_id, decision_id, cycle_id, bucket, token_id, side,"
        " order_action, limit_price, requested_size,"
        " requested_notional_usdc, state, live_submit_authorized,"
        " idempotency_key, intent_json, intent_sha256, created_at)"
        " VALUES('i1','d1','c1','31°C','tok','YES','BUY',0.02,25.0,0.5,"
        " 'prepared',0,'k1',?,?,'2026-09-01T00:00:00Z')",
        # What production mints beside the columns.  The execution family
        # lives here and nowhere else, and the submit walk reads it to decide
        # whether the order may cross; a row without it is refused by name.
        ('{"execution_candidate": "MAKER", "post_only": true, "order_type": "GTD", "expiration": 1789088400}', "a" * 64),
    )
    return conn


def test_a_lowered_flag_holds_every_send(tmp_path, monkeypatch) -> None:
    """The transport must not be reached at all, and the receipt must say why."""

    conn = _book(tmp_path / "book.db")
    monkeypatch.setattr(sub, "LIVE_INTENT_PATH", _armed(tmp_path, False))
    sent: list = []

    def transport(path, payload):
        sent.append(path)
        return {"ok": True, "accepted": True}

    receipt = sub.submit_prepared_intents(
        conn, cycle_id="c1", transport=transport,
        live_submit_authorized=True,
        observed_at_utc="2026-09-01T00:00:00Z",
    )
    assert sent == [], "a lowered flag must not reach the transport"
    assert receipt["orders_submitted"] == 0
    assert receipt["live_intent_armed"] is False
    # The receipt must not claim the gateway was callable while every send
    # was held -- that describes a different system than the one that ran.
    assert receipt["calls_order_gateway"] is False
    reasons = {row.get("reason") for row in receipt["submissions"]}
    assert "live_intent_disarmed" in reasons
    # And it must be told apart from an unauthorised contract: reading a hand
    # on the switch as a plumbing fault sends somebody to debug the wrong lane.
    assert "live_submit_not_authorized" not in reasons


def test_the_two_refusals_stay_distinguishable(tmp_path, monkeypatch) -> None:
    conn = _book(tmp_path / "book2.db")
    monkeypatch.setattr(sub, "LIVE_INTENT_PATH", _armed(tmp_path, True))
    receipt = sub.submit_prepared_intents(
        conn, cycle_id="c1", transport=lambda *a, **k: {},
        live_submit_authorized=False,
        observed_at_utc="2026-09-01T00:00:00Z",
    )
    reasons = {row.get("reason") for row in receipt["submissions"]}
    assert reasons == {"live_submit_not_authorized"}
    assert receipt["live_intent_armed"] is True
