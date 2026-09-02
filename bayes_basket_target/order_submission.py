"""Deliver prepared order intents to the CLOB order service and record receipts.

This is the link the target lane never had.  Everything on either side of it
already existed: the atomic ledger writes ``prepared`` intents with a
content-addressed idempotency key, and ``execution_reconciliation`` turns
exchange events into reconciliation and intent state.  Nothing carried an
intent across, so ``basket_target_order_events`` and
``basket_target_order_reconciliations`` stayed empty from the first day.

Authorisation is read, never asserted.  The activation contract decides
whether a submission may leave the process, and a run that is not authorised
still walks the whole path and records what it would have sent -- which is the
only way to prove the chain closes before anything is live.  The transport is
injected so no import of this module can reach the network on its own.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import json
from pathlib import Path
import sqlite3
from typing import Any

from .contracts import ContractError, content_sha256
from .execution_reconciliation import (
    can_submit_next_order,
    record_order_event,
)


SCHEMA = "bayes_basket_target_order_submission_v1"
# The operator's arming flag, read here and nowhere cached.
LIVE_INTENT_PATH = (
    Path(__file__).resolve().parents[1] / "config/trading/live_intent_v1.json"
)
PLACE_ORDER_PATH = "/place-order"
BASKET_STRATEGY_ID = "weather_basket_yes_v2"
SUBMITTABLE_INTENT_STATE = "prepared"
# The service answers with these shapes; each maps to exactly one ledger event.
# ``UNKNOWN_POST`` is the one that matters: the request left the process and
# the reply did not say whether the exchange took it, so the bucket must stay
# blocked until a reconciliation resolves it rather than be retried blind.
_ACCEPTED_EVENT = "POST_ACK"
_UNKNOWN_EVENT = "UNKNOWN_POST"
# The gateway answered and declined to forward the order.  Recorded as an
# event like every other outcome: until 2026-09-01 this branch wrote
# nothing at all -- no event, no state change, no receipt row -- so a leg
# refused here sat at ``prepared`` until the stranded-intent sweep retired
# it as ``cancelled``, and the board showed a cancellation with no cause.
# Two of the five live intents ever minted ended that way and their reason
# is now unrecoverable: the gateway logs that held it were rotated away.
_REFUSED_EVENT = "SUBMIT_REFUSED"

Transport = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]


class OrderSubmissionError(ContractError):
    """Raised when an intent or a service reply breaks the submission contract."""


def _text(field: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise OrderSubmissionError(f"order_submission_{field}_missing")
    return result


def _positive(field: str, value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise OrderSubmissionError(
            f"order_submission_{field}_invalid"
        ) from exc
    if not result > 0.0:
        raise OrderSubmissionError(f"order_submission_{field}_invalid")
    return result


def build_place_order_payload(
    intent: Mapping[str, Any],
    *,
    strategy_id: str = BASKET_STRATEGY_ID,
) -> dict[str, Any]:
    """Map one intent row onto the order service's request contract."""

    price = _positive("limit_price", intent.get("limit_price"))
    if price >= 1.0:
        raise OrderSubmissionError("order_submission_limit_price_invalid")
    # The ledger's ``side`` is the outcome leg, which the retired-NO trigger
    # pins to YES; the exchange's ``side`` is the direction, which the ledger
    # calls ``order_action``.  The two words mean opposite halves of the same
    # order, and crossing them sends a sell as a buy.
    outcome_side = _text("side", intent.get("side")).upper()
    if outcome_side != "YES":
        raise OrderSubmissionError("order_submission_outcome_side_invalid")
    direction = _text("order_action", intent.get("order_action")).upper()
    if direction not in {"BUY", "SELL"}:
        raise OrderSubmissionError("order_submission_order_action_invalid")
    return {
        "strategy_id": _text("strategy_id", strategy_id),
        "order_intent_id": _text("order_intent_id", intent.get("order_intent_id")),
        "idempotency_key": _text(
            "idempotency_key", intent.get("idempotency_key")
        ),
        "token_id": _text("token_id", intent.get("token_id")),
        "outcome_side": outcome_side,
        "side": direction,
        "price": price,
        "size": _positive("requested_size", intent.get("requested_size")),
        "order_type": _text(
            "order_type", intent.get("order_type") or "GTC"
        ).upper(),
    }


def classify_submission_response(
    response: Mapping[str, Any],
) -> dict[str, Any]:
    """Decide which ledger event one service reply is.

    A reply that is not ``ok`` but reports the request reached the exchange,
    or that asks for reconciliation, is not a rejection: the order may be
    live.  Treating that as "nothing happened" is how a duplicate gets sent,
    so it becomes ``UNKNOWN_POST`` and the bucket stays blocked.
    """

    if not isinstance(response, Mapping):
        raise OrderSubmissionError("order_submission_response_invalid")
    accepted = response.get("accepted") is True
    started = response.get("exchange_post_started") is True
    reconcile = response.get("reconcile_required") is True
    pending = response.get("pending") is True
    # No answer came back at all, so this layer does not know whether the
    # gateway forwarded the order.  The transport says so itself rather
    # than being inferred from the absence of the flags above -- absence
    # is exactly what a timeout and a refusal have in common, and reading
    # one as the other was how a placed order came to be recorded as
    # nothing having happened.
    ambiguous = response.get("transport_ambiguous") is True
    error = str(response.get("error") or "").strip()
    if accepted:
        return {
            "event_type": _ACCEPTED_EVENT,
            "submitted": True,
            "reason": "",
        }
    if reconcile or started or pending or ambiguous:
        return {
            "event_type": _UNKNOWN_EVENT,
            "submitted": True,
            "reason": error or "exchange_state_unknown",
        }
    return {
        "event_type": _REFUSED_EVENT,
        "submitted": False,
        "reason": error or "order_service_refused",
    }


def _submittable_intents(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT order_intent_id,bucket,token_id,side,order_action,"
        "limit_price,requested_size,idempotency_key,state "
        "FROM basket_target_order_intents WHERE cycle_id=? "
        "ORDER BY order_intent_id",
        (_text("cycle_id", cycle_id),),
    ).fetchall()
    return [
        {
            "order_intent_id": row[0],
            "bucket": row[1],
            "token_id": row[2],
            "side": row[3],
            "order_action": row[4],
            "limit_price": row[5],
            "requested_size": row[6],
            "idempotency_key": row[7],
            "state": row[8],
        }
        for row in rows
        if str(row[8]) == SUBMITTABLE_INTENT_STATE
    ]


def live_intent_armed(path: Path | str | None = None) -> bool:
    """Whether the operator's arming flag is up, read fresh on every pass.

    The flag was the go-live migration's precondition and nothing else, which
    left a gap somebody could fall into: once the migration had minted the
    live contracts, lowering the flag stopped nothing.  On 2026-08-31 the
    flag was lowered at 13:29 and a real order left the process at 21:25 --
    the contracts, the runtime config, the worker and the gateway were all
    still open, and the one control that reads like a stop had become a
    record of an intention nobody consulted.  A switch that does not stop
    the thing it names is worse than no switch, because people rely on it.

    Never cached.  A cached answer would keep sending after the operator had
    stopped the lane, which is the entire failure this closes.

    Fails CLOSED -- missing, unreadable or malformed reads as disarmed.  The
    station exclusion list fails open for the opposite reason: it only
    refuses admissions, while this one authorises spending, and the two
    directions are not symmetric.  A flag nobody can read is not permission.
    """

    try:
        raw = Path(path or LIVE_INTENT_PATH).read_text(encoding="utf-8")
        return json.loads(raw).get("live_enabled") is True
    except (OSError, ValueError, AttributeError):
        return False


def submit_prepared_intents(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    transport: Transport,
    live_submit_authorized: bool,
    observed_at_utc: str,
    strategy_id: str = BASKET_STRATEGY_ID,
) -> dict[str, Any]:
    """Submit every prepared intent on one cycle, in bucket-serial order.

    Returns a receipt naming what each intent did.  No exception escapes for a
    single refused intent: one bucket losing its quote must not abandon the
    rest of a basket that is still fillable, and the refusal is on the receipt.
    """

    observed_at = _text("observed_at_utc", observed_at_utc)
    # Once per cycle, before the walk: the flag governs this pass, and asking
    # it per intent would let it change halfway through one basket.
    armed = live_intent_armed()
    submissions: list[dict[str, Any]] = []
    submitted = 0
    for intent in _submittable_intents(conn, cycle_id=cycle_id):
        intent_id = str(intent["order_intent_id"])
        gate = can_submit_next_order(
            conn,
            bucket=str(intent["bucket"]),
            token_id=str(intent["token_id"]),
            exclude_order_intent_id=intent_id,
        )
        if gate.get("allowed") is not True:
            submissions.append({
                "order_intent_id": intent_id,
                "state": "blocked",
                "reason": str(gate.get("reason") or "sequential_gate_blocked"),
                "blockers": list(gate.get("blockers") or []),
                "exchange_post_started": False,
            })
            continue
        payload = build_place_order_payload(intent, strategy_id=strategy_id)
        if not live_submit_authorized or not armed:
            # The whole path runs except the send.  The payload hash is on the
            # receipt so a no-live run can be compared byte for byte against
            # the live one it is standing in for.
            submissions.append({
                "order_intent_id": intent_id,
                "state": "not_authorized",
                # Told apart on purpose: one says the contract never
                # authorised this lane, the other says a person stopped it.
                # Reading a hand on the switch as a missing contract is how
                # an operator's stop gets diagnosed as a plumbing fault.
                "reason": (
                    "live_submit_not_authorized"
                    if not live_submit_authorized
                    else "live_intent_disarmed"
                ),
                "exchange_post_started": False,
                "payload_sha256": content_sha256(payload),
            })
            continue
        response = transport(PLACE_ORDER_PATH, payload)
        outcome = classify_submission_response(response)
        if not outcome["submitted"]:
            # A refusal is an outcome, so it is recorded like one.  Writing
            # the event moves the intent to ``failed`` and frees its bucket
            # and token immediately; leaving it at ``prepared``, as this
            # branch used to, blocked that bucket until a sweep retired the
            # intent and then reported the retirement as the cause.
            refusal = {
                "event_type": outcome["event_type"],
                "exchange_order_id": "",
                "observed_at": observed_at,
                "matched_size": 0.0,
                "matched_notional_usdc": 0.0,
                "event_payload": {
                    "idempotency_key": payload["idempotency_key"],
                    "payload_sha256": content_sha256(payload),
                    "reason": outcome["reason"],
                    "source": str(response.get("source") or ""),
                    "http_status": response.get("http_status"),
                },
            }
            receipt = record_order_event(
                conn,
                order_intent_id=intent_id,
                event=refusal,
            )
            submissions.append({
                "order_intent_id": intent_id,
                "state": receipt.get("state"),
                "reconciliation_state": receipt.get("reconciliation_state"),
                "event_type": outcome["event_type"],
                "event_id": receipt.get("event_id"),
                "reason": outcome["reason"],
                # The request did leave this process; what it did not do is
                # reach the exchange.  Reporting it as never started would
                # lose the one fact that separates a refusal from a leg
                # that was never attempted.
                "exchange_post_started": False,
                "payload_sha256": content_sha256(payload),
            })
            continue
        event = {
            "event_type": outcome["event_type"],
            "exchange_order_id": str(
                response.get("exchange_order_id") or ""
            ),
            "observed_at": observed_at,
            "matched_size": 0.0,
            "matched_notional_usdc": 0.0,
            "event_payload": {
                "idempotency_key": payload["idempotency_key"],
                "payload_sha256": content_sha256(payload),
                "reason": outcome["reason"],
                "source": str(response.get("source") or ""),
            },
        }
        receipt = record_order_event(
            conn,
            order_intent_id=intent_id,
            event=event,
        )
        submitted += 1
        submissions.append({
            "order_intent_id": intent_id,
            "state": receipt.get("state"),
            "reconciliation_state": receipt.get("reconciliation_state"),
            "event_type": outcome["event_type"],
            "event_id": receipt.get("event_id"),
            "reason": outcome["reason"],
            "exchange_post_started": True,
            "payload_sha256": content_sha256(payload),
        })
    return {
        "schema": SCHEMA,
        "cycle_id": cycle_id,
        "live_submit_authorized": bool(live_submit_authorized),
        "live_intent_armed": bool(armed),
        # What this pass could actually do, not what the contract allows: a
        # receipt that says the gateway was callable while a lowered flag
        # held every send is a receipt that describes the wrong system.
        "calls_order_gateway": bool(live_submit_authorized and armed),
        "intent_count": len(submissions),
        "orders_submitted": submitted,
        "submissions": submissions,
    }


__all__ = [
    "BASKET_STRATEGY_ID",
    "LIVE_INTENT_PATH",
    "live_intent_armed",
    "OrderSubmissionError",
    "PLACE_ORDER_PATH",
    "SCHEMA",
    "build_place_order_payload",
    "classify_submission_response",
    "submit_prepared_intents",
]
