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

from .basket_kernel.contracts import ContractError as KernelContractError
from .basket_kernel.delta_v2 import (
    ORDER_TYPE_FOK,
    execution_order_binding_v1,
)
from .contracts import ContractError, content_sha256
from .execution_reconciliation import (
    can_submit_next_order,
    record_order_event,
)
from .live_station_scope import admission as live_station_admission


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
_FILL_EVENT = "FILL"
_UNKNOWN_EVENT = "UNKNOWN_POST"
# The gateway answered and declined to forward the order.  Recorded as an
# event like every other outcome: until 2026-09-01 this branch wrote
# nothing at all -- no event, no state change, no receipt row -- so a leg
# refused here sat at ``prepared`` until the stranded-intent sweep retired
# it as ``cancelled``, and the board showed a cancellation with no cause.
# Two of the five live intents ever minted ended that way and their reason
# is now unrecoverable: the gateway logs that held it were rotated away.
_REFUSED_EVENT = "SUBMIT_REFUSED"
# An order that existed at the exchange and stopped existing without filling.
# The vocabulary and the state machine have carried it since the ledger was
# written and nothing ever produced one: there was no order type that could
# expire and no order type that could be killed.  Both arrive together --
# GTD's expiry and FOK's kill are the same fact about an order's end.
_EXPIRED_EVENT = "EXPIRED"

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


# Order types the exchange fills by crossing.  Post-only contradicts them by
# construction and the gateway refuses the pair outright
# (``post_only_market_order_forbidden``); naming them here refuses it one
# process earlier, where the intent that carries the contradiction is still
# in hand.
_CROSSING_ORDER_TYPES = frozenset({"FOK", "FAK"})


def _boolean_flag(field: str, value: Any) -> bool:
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        raise OrderSubmissionError(f"order_submission_{field}_invalid")
    return bool(value)


def _expiration_unix_seconds(field: str, value: Any) -> int:
    """A venue expiry, in whole unix seconds, or a refusal by name.

    Whole seconds because that is the unit the order's signature commits to:
    the expiry is a field of the EIP-712 struct the client signs, so a value
    this side rounds differently from the signer is a different order.
    """

    if isinstance(value, bool):
        raise OrderSubmissionError(f"order_submission_{field}_invalid")
    try:
        seconds = int(value)
    except (TypeError, ValueError) as exc:
        raise OrderSubmissionError(
            f"order_submission_{field}_invalid"
        ) from exc
    if seconds <= 0:
        raise OrderSubmissionError(f"order_submission_{field}_invalid")
    return seconds


def _post_only(intent: Mapping[str, Any]) -> bool:
    """Whether this order may only rest, read off the intent that minted it.

    Charter 94 makes every entry a resting rung one tick inside the bid, and
    the compile records that as ``post_only`` on the intent
    (``production.py``: ``execution_candidate == "MAKER"``).  The payload
    this module builds never carried the flag, so the gateway fell through to
    ``PM_CLOB_POST_ONLY`` -- unset, therefore ``0`` -- and every one of the
    2,336 maker intents minted to 2026-09-10 went to the exchange as an
    ordinary limit order.  The rung is priced under the ask, so it rested in
    almost every case; what was missing is the guarantee.  Between the
    compile and the post there is a three-second deadline window, and an ask
    that falls onto the rung inside it turns a decision that was judged,
    sized and floor-checked at the resting price into a crossing buy at a
    price nothing in the chain ever priced.

    Read, never inferred.  The family is a decision the optimizer makes and
    writes down; an intent that states neither the flag nor the family is
    refused rather than guessed at, because the two guesses are opposite
    orders and the wrong one spends money at a price no gate saw.
    """

    value = intent.get("post_only")
    if value is not None:
        return _boolean_flag("post_only", value)
    family = str(intent.get("execution_candidate") or "").strip().upper()
    if family == "MAKER":
        return True
    if family:
        return False
    raise OrderSubmissionError("order_submission_post_only_missing")


def _execution_binding(intent: Mapping[str, Any]) -> dict[str, Any]:
    """The venue fields this intent's execution family is bound to.

    The binding lives in the kernel so the compile that mints an intent and
    the submission that sends it cannot hold different opinions about which
    of the two things this lane does is happening.  The intent's own
    ``post_only`` is then checked against it rather than obeyed blindly: a
    row that says MAKER and post-only false is not a payload with an unusual
    flag, it is a resting rung that would cross, and the family it claims is
    the one the seat was priced under.
    """

    family = str(intent.get("execution_candidate") or "").strip().upper()
    if not family:
        # Legacy rows and direct callers: the flag alone still names the
        # family, because the two families disagree about exactly it.
        family = "MAKER" if _post_only(intent) else "TAKER_FOK"
    try:
        binding = execution_order_binding_v1(family)
    except KernelContractError as exc:
        raise OrderSubmissionError(
            "order_submission_execution_family_unknown"
        ) from exc
    if intent.get("post_only") is not None and _boolean_flag(
        "post_only", intent.get("post_only")
    ) is not bool(binding["post_only"]):
        raise OrderSubmissionError(
            "order_submission_post_only_contradicts_execution_intent"
        )
    return binding


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
    binding = _execution_binding(intent)
    post_only = bool(binding["post_only"])
    order_type = _text(
        "order_type", intent.get("order_type") or binding["order_type"]
    ).upper()
    if order_type != str(binding["order_type"]):
        # The intent may state the type, and it may not state a different one
        # than the family it declares.  A GTC written on a MAKER row is the
        # shape this lane sent for its whole life: an order asked to rest,
        # with no expiry and nothing stopping it from crossing.
        raise OrderSubmissionError(
            "order_submission_order_type_contradicts_execution_intent"
        )
    if post_only and order_type in _CROSSING_ORDER_TYPES:
        # A crossing family carrying the resting flag is not a payload with a
        # debatable field; it is two contradictory instructions about what to
        # do with the money, and neither one of them can be picked here.
        raise OrderSubmissionError(
            "order_submission_post_only_contradicts_order_type"
        )
    payload = {
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
        "order_type": order_type,
        "post_only": post_only,
    }
    if binding["requires_expiration"]:
        # GTD is a promise the venue keeps for us: an order this lane loses
        # track of stops existing on its own.  The expiry is signed into the
        # order, so it has to travel with the payload rather than be applied
        # to it -- a gateway that supplied its own would be signing a
        # different order than the one the compile authorized.
        payload["expiration"] = _expiration_unix_seconds(
            "expiration", intent.get("expiration")
        )
    if binding["requires_price_cap"]:
        # An immediate take states the worst price it is allowed to pay, and
        # states it separately from the limit it is sending.  The two are
        # usually the same number; requiring both means a completion whose
        # walked price has drifted past what authorized it is refused here
        # rather than discovered in the fill.
        cap = _positive("fok_price_cap", intent.get("fok_price_cap"))
        if cap >= 1.0:
            raise OrderSubmissionError("order_submission_fok_price_cap_invalid")
        if price > cap + 1e-12:
            raise OrderSubmissionError(
                "order_submission_limit_price_exceeds_fok_price_cap"
            )
        payload["fok_price_cap"] = cap
    return payload


def _number_or_zero(*values: Any) -> float:
    for value in values:
        if value in (None, ""):
            continue
        try:
            result = float(value)
        except (TypeError, ValueError):
            continue
        if result == result:  # not NaN
            return result
    return 0.0


def fill_from_gateway_response(response: Mapping[str, Any]) -> dict[str, Any]:
    """What the gateway says the exchange did with the order, if anything.

    The gateway's synchronous success reply carries ``ok``, ``orderID`` and
    the exchange's own ``status``/``size_matched``; its stored reply for a
    settled idempotency key carries the same fields.  Read both the same
    way, so a fill looks identical whether it was seen at submit time or
    recovered by reconciliation.  For a BUY the exchange's ``makingAmount``
    is the USDC paid and ``takingAmount`` the shares received; a SELL is the
    mirror.
    """

    exchange = response.get("exchange_response")
    exchange = exchange if isinstance(exchange, Mapping) else {}
    exchange_order_id = str(
        response.get("exchange_order_id")
        or response.get("orderID")
        or exchange.get("orderID")
        or ""
    ).strip()
    status = str(response.get("status") or exchange.get("status") or "")
    return {
        "exchange_order_id": exchange_order_id,
        "status": status.strip().lower(),
        "shares_matched": _number_or_zero(
            response.get("shares_matched"),
            response.get("size_matched"),
            exchange.get("size_matched"),
        ),
        "making_amount": _number_or_zero(
            exchange.get("makingAmount"), exchange.get("making_amount"),
        ),
        "taking_amount": _number_or_zero(
            exchange.get("takingAmount"), exchange.get("taking_amount"),
        ),
    }


def fill_quantities(
    fill: Mapping[str, Any],
    *,
    event_type: str,
    order_action: str,
    limit_price: float,
) -> tuple[float, float]:
    """Matched shares and matched USDC for a fill event, zero for any other.

    The cost comes from the exchange's own amounts (USDC paid for a BUY,
    USDC received for a SELL); when the reply carries shares but no amount,
    the limit price bounds it.
    """

    if event_type != _FILL_EVENT:
        return 0.0, 0.0
    shares = float(fill.get("shares_matched") or 0.0)
    if shares <= 0.0:
        return 0.0, 0.0
    direction = str(order_action or "").upper()
    notional = float(
        (fill.get("making_amount") if direction == "BUY" else fill.get("taking_amount"))
        or 0.0
    )
    if notional <= 0.0:
        notional = shares * float(limit_price)
    return shares, notional


def classify_submission_response(
    response: Mapping[str, Any],
    *,
    order_type: str = "",
) -> dict[str, Any]:
    """Decide which ledger event one service reply is.

    A reply that is not ``ok`` but reports the request reached the exchange,
    or that asks for reconciliation, is not a rejection: the order may be
    live.  Treating that as "nothing happened" is how a duplicate gets sent,
    so it becomes ``UNKNOWN_POST`` and the bucket stays blocked.

    A reply the exchange answered is an acceptance whether or not the
    gateway spelled ``accepted``: its synchronous success reply says ``ok``
    and names the ``orderID`` and never carried that flag, so the first five
    live fills (2026-09-05 10:11Z, all ``status: matched``) were recorded as
    ``UNKNOWN_POST`` and sat in ``reconcile_required`` -- which the order
    state producer reads as the whole book being non-quiescent, and which
    froze every quote refresh.  A matched reply is a ``FILL`` with the
    exchange's own size and cost; an accepted reply not yet matched is a
    ``POST_ACK``.
    """

    if not isinstance(response, Mapping):
        raise OrderSubmissionError("order_submission_response_invalid")
    fill = fill_from_gateway_response(response)
    accepted = response.get("accepted") is True or (
        response.get("ok") is True and bool(fill["exchange_order_id"])
    )
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
        matched = fill["status"] == "matched" and fill["shares_matched"] > 0.0
        if not matched and str(order_type or "").upper() == ORDER_TYPE_FOK:
            # A fill-or-kill the exchange took and did not match is killed,
            # and killed is the end of it.  Read as a POST_ACK -- which is
            # what every reply without a match was until 2026-09-11 -- the
            # intent waits at ``pending`` for a fill this order type cannot
            # produce, and the sequential gate holds that bucket shut behind
            # it.  The ledger already had a terminal state for an order that
            # existed and stopped existing without filling; this is the first
            # thing that writes one.
            return {
                "event_type": _EXPIRED_EVENT,
                "submitted": True,
                "reason": "fok_killed",
                "fill": fill,
            }
        return {
            "event_type": _FILL_EVENT if matched else _ACCEPTED_EVENT,
            "submitted": True,
            "reason": "",
            "fill": fill,
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


def _intent_body(raw: Any) -> dict[str, Any]:
    """The two execution fields the intent's own JSON carries, if it parses.

    Only these two: everything else on that document belongs to the compile
    that wrote it, and a submission path that helped itself to the rest would
    become a second reader of a decision it does not make.  Unparseable JSON
    yields nothing and ``_post_only`` refuses the intent by name, which is
    the honest outcome -- a row whose family cannot be read is a row whose
    order type cannot be chosen.
    """

    if isinstance(raw, Mapping):
        body: Mapping[str, Any] = raw
    else:
        try:
            parsed = json.loads(str(raw or "") or "{}")
        except (TypeError, ValueError):
            return {}
        if not isinstance(parsed, Mapping):
            return {}
        body = parsed
    carried: dict[str, Any] = {}
    for field in (
        "post_only",
        "execution_candidate",
        "order_type",
        "expiration",
        "expiration_basis",
        "fok_price_cap",
    ):
        if body.get(field) is not None:
            carried[field] = body[field]
    return carried


def _submittable_intents(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT order_intent_id,bucket,token_id,side,order_action,"
        "limit_price,requested_size,idempotency_key,state,intent_json "
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
            # The execution family the compile chose, carried on the row's
            # own JSON.  The columns above are the exchange's vocabulary and
            # have never had a place for it, which is why the payload could
            # be built without ever seeing ``post_only``: the flag was
            # written at mint time and then read by nobody.
            **_intent_body(row[9]),
        }
        for row in rows
        if str(row[8]) == SUBMITTABLE_INTENT_STATE
    ]


def _cycle_station(conn: sqlite3.Connection, cycle_id: str) -> str:
    """Which station this cycle is trading, for the station gate below."""

    row = conn.execute(
        "SELECT station_id FROM basket_target_execution_cycles"
        " WHERE cycle_id=?",
        (_text("cycle_id", cycle_id),),
    ).fetchone()
    return str(row[0]).strip().upper() if row and row[0] else ""


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
    # The station gate, resolved once per pass for the same reason the flag
    # is: a cycle trades one station, and asking per intent would let the
    # answer change halfway through one basket.
    #
    # It binds here because until 2026-09-02 it bound nowhere on this path.
    # The bar list was consulted by the panel, drawn on the board and
    # checked by the go-live migration, and an order for a barred station
    # went out regardless -- EDDM, KSEA and LTAC all carry live-authorised
    # intents minted while barred.  A gate that only appears on a screen is
    # a label, and this one is a risk ruling that beat every automatic rule
    # ever swept against it (+15.6 points, p=0.001), so it is the last one
    # that should have been advisory.
    #
    # Deferred until there is something to send: the derivation reads two
    # aggregates off the book, and a pass with no submittable intent should
    # not pay for them.
    station = _cycle_station(conn, cycle_id)
    _admission: dict[str, Any] | None = None

    def _station_gate() -> dict[str, Any]:
        nonlocal _admission
        if _admission is None:
            _admission = live_station_admission(station, conn=conn)
        return _admission

    submissions: list[dict[str, Any]] = []
    submitted = 0
    for intent in _submittable_intents(conn, cycle_id=cycle_id):
        intent_id = str(intent["order_intent_id"])
        gate = can_submit_next_order(
            conn,
            bucket=str(intent["bucket"]),
            token_id=str(intent["token_id"]),
            exclude_order_intent_id=intent_id,
            # Charter 94: the rungs of one leg's ladder are written by one
            # compile and are not duplicates of each other.
            same_cycle_id=str(cycle_id or ""),
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
        try:
            payload = build_place_order_payload(intent, strategy_id=strategy_id)
        except OrderSubmissionError as exc:
            # A malformed intent is one leg's problem, and this function's
            # own contract is that no single refusal abandons the rest of a
            # basket that is still fillable.  It escaped anyway, because the
            # build sat outside every guard on the walk: a row that could not
            # state its execution family would have taken its whole cycle
            # down with it and named the reason nowhere the ledger keeps.
            submissions.append({
                "order_intent_id": intent_id,
                "state": "blocked",
                "reason": str(exc),
                "blockers": [str(exc)],
                "exchange_post_started": False,
            })
            continue
        # Entries only.  The bar refuses admissions -- it says do not put
        # money on this station -- and reading it as "touch nothing here"
        # would strand whatever is already open: a station barred while
        # holding a position could not trim, reseat or exit it, so the one
        # ruling meant to reduce risk would be the thing preventing the
        # reduction.  Exits are out of scope on the other path too, by
        # construction: certain_win_recycle only ever sells.
        admitted = (
            _station_gate() if str(intent["order_action"]).upper() == "BUY"
            else {"admitted": True, "reason": "exit_not_gated"}
        )
        if not live_submit_authorized or not armed or not admitted["admitted"]:
            # The whole path runs except the send.  The payload hash is on the
            # receipt so a no-live run can be compared byte for byte against
            # the live one it is standing in for.
            # Told apart on purpose: one says the contract never
            # authorised this lane, the other says a person stopped it.
            # Reading a hand on the switch as a missing contract is how
            # an operator's stop gets diagnosed as a plumbing fault.
            gate_reason = (
                "live_submit_not_authorized"
                if not live_submit_authorized
                else "live_intent_disarmed" if not armed
                # barred / held / still earning its way in -- three
                # different follow-ups, so three different reasons.
                else str(admitted["reason"])
            )
            entry: dict[str, Any] = {
                "order_intent_id": intent_id,
                "state": "not_authorized",
                "reason": gate_reason,
                "station_id": station,
                "exchange_post_started": False,
                "payload_sha256": content_sha256(payload),
            }
            if live_submit_authorized:
                # A live intent a person stopped -- the flag lowered, the
                # station barred or held -- is an outcome, and it is recorded
                # like the gateway's refusal below: the intent ends
                # ``failed`` with the reason on its event, instead of sitting
                # ``prepared`` until the stranded sweep retires it and the
                # board shows a cancellation with no cause (VILK, EPWA and
                # MMMX on 2026-09-05: nine legs, no reason anywhere).  A
                # no-live contract writes nothing: its intents belong to the
                # simulator's transition runtime.
                gate = {
                    "event_type": "NEVER_REACHED_EXCHANGE",
                    "exchange_order_id": "",
                    "observed_at": observed_at,
                    "matched_size": 0.0,
                    "matched_notional_usdc": 0.0,
                    "event_payload": {
                        "idempotency_key": payload["idempotency_key"],
                        "payload_sha256": content_sha256(payload),
                        "reason": gate_reason,
                        "source": "order_submission_gate",
                        "station_id": station,
                    },
                }
                receipt = record_order_event(
                    conn,
                    order_intent_id=intent_id,
                    event=gate,
                )
                entry.update({
                    "intent_state": receipt.get("state"),
                    "reconciliation_state": receipt.get(
                        "reconciliation_state"
                    ),
                    "event_type": "NEVER_REACHED_EXCHANGE",
                    "event_id": receipt.get("event_id"),
                })
            submissions.append(entry)
            continue
        response = transport(PLACE_ORDER_PATH, payload)
        outcome = classify_submission_response(
            response, order_type=str(payload.get("order_type") or ""),
        )
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
        fill = outcome.get("fill") or {}
        matched_size, matched_notional = fill_quantities(
            fill,
            event_type=outcome["event_type"],
            order_action=str(intent["order_action"]),
            limit_price=float(intent["limit_price"]),
        )
        event = {
            "event_type": outcome["event_type"],
            "exchange_order_id": str(
                fill.get("exchange_order_id")
                or response.get("exchange_order_id")
                or ""
            ),
            "observed_at": observed_at,
            "matched_size": matched_size,
            "matched_notional_usdc": matched_notional,
            "event_payload": {
                "idempotency_key": payload["idempotency_key"],
                "payload_sha256": content_sha256(payload),
                "reason": outcome["reason"],
                "source": str(response.get("source") or ""),
                "exchange_status": str(fill.get("status") or ""),
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
    "fill_from_gateway_response",
    "fill_quantities",
    "submit_prepared_intents",
]
