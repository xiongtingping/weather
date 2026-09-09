"""Restart-safe bounded rotation for forward TAF training-fact capture."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Collection, Mapping, Sequence
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .contracts import ContractError, content_sha256
from .resource_bounds import load_bounded_json, write_bounded_json_if_changed
from .settlement_time import settlement_day_state
from .fleet import expected_station_count


SCHEMA = "bayes_basket_target_taf_rotation_state_v1"
MAX_CATALOG_SCOPES = expected_station_count() * 3
# Twelve, from the lap arithmetic rather than a preference.  The poll does not
# run on its own clock: the resident serialised its three writers on
# 2026-08-26 (af0e5084) because the forecast prefetch and the production cycle
# were writing calibration_forward_taf_capture at the same time and every
# ingress came back "database is locked".  Correct, and it halved this lane:
# measured over rounds clustered at a 120s gap, the poll went from one every
# 11-13 minutes before that commit to one every 19-33 minutes after, 44-46 a
# day now.  Round frequency is therefore not ours to raise -- the serialisation
# is load-bearing -- so the lap is set by what one round covers.  At four
# scopes a round that is 180 slots a day against a ninety-scope catalogue: a
# twelve-hour lap, against a TAF window only ten hours wide (the bulletin
# carrying the day's TX is issued about 21Z the evening before, and the scope
# leaves the catalogue at 15:00 local).  Twelve scopes a round makes the lap
# four hours, so that window holds two or three polls instead of most of one.
# The cost is bounded: capture_forward_awc_taf times out at 12s, and the caller
# stops the round on its own budget before the writer turn stretches.
MAX_SELECTED_SCOPES = 12
# Charter 85 (b) (user ruling, 2026-09-06: "TAF 轮询改成已持仓站日优先、d1 带
# TX 的 17 站优先"): each lap fills its priority slots first -- station-days
# the book holds, then tomorrow's scope at the stations whose TAFs carry a TX
# group -- and only then the general least-recently-served queue.  The cap
# keeps the rest of the catalogue moving: with ~14 held station-days and ~18
# TX stations the priority pool alone is larger than a lap, and a queue that
# never reaches the general block is the ZUUU starvation with a new head.
PRIORITY_SLOTS_PER_LAP = 8
# A station is in the TX cohort while one of its captured TAFs in this many
# trailing days carried a TX group; US TAFs never do, and the cohort is read
# off the capture table rather than hard-coded so it follows the bulletins.
TX_COHORT_LOOKBACK_DAYS = 30
PRIORITY_HELD = 0
# A tradeable scope whose newest bulletin is older than the issuing cadence.
#
# Charter 85 (b) ranked by what a scope is -- held, or tomorrow at a TX
# station -- and never by what it has.  Measured 2026-09-09 04:15Z, that is
# the whole starvation: the round selected twelve scopes and eight of them
# were class 0, so the held pool took every priority slot it had, and the
# general queue moved four scopes a round against a ninety-scope catalogue.
# The stations outside both classes went from eight to eleven bulletins a
# day (09-03) to one (09-08): KLGA 10 -> 10 -> 6 -> 3 -> 1 -> 1, KORD
# 9 -> 10 -> 10 -> 8 -> 4 -> 2, while ZUUU and ZGSZ, inside the classes,
# held at five to eight.
#
# The cost of that lands two layers down.  A station-day whose newest TAF
# predates the run its P0 root is built on cannot re-seat its P0-M -- the
# 2026-09-03 seatability ruling forbids it -- and the stale P0-M then
# refuses the whole scope on ``resident_p0m_replay_required``.  Nineteen of
# the sixty station-days carrying a P0-M were in that state, and their TAFs
# were not missing at the source: KMIA issues one about every three hours
# and we hold exactly one for 2026-09-09, received 2026-09-08T20:03Z.
PRIORITY_STALE_CONFIRMATION = 1
PRIORITY_D1_TX = 2
PRIORITY_GENERAL = 3
# How long a scope's newest bulletin must have been in hand before a
# priority slot is spent on it again.
#
# Not a cadence for the poll -- it is what makes a class a claim on a slot
# rather than a standing reservation.  A held station-day that received a
# bulletin twenty minutes ago has nothing to gain from this round, and the
# starved tail has everything; the same eight scopes taking the same eight
# slots every round is the shape the class cap was meant to prevent and
# could not, because the cap bounds the pool and not its turnover.
PRIORITY_REPOLL_MIN_AGE_SEC = 3600.0
# When a tradeable scope's silence stops being ordinary.
#
# TAFs are issued about every three to six hours and amended between, so a
# scope with nothing new in three hours is either being starved by us or is
# genuinely quiet at the source -- and the two are told apart by fetching,
# which is what this class buys it.
STALE_CONFIRMATION_AGE_SEC = 3.0 * 3600.0
# The served ledger outlives the catalogue on purpose (see build_rotation_state),
# so it is bounded on its own terms rather than by today's scope count.
MAX_SERVED_LEDGER_SCOPES = MAX_CATALOG_SCOPES * 2
MAX_BYTES = 1024 * 1024
# The forward TAF physical summary uses this shared local window.  It is not a
# temperature threshold; it merely prevents a post-peak bulletin from being
# scheduled as new P0-M evidence for a daily high that has already passed its
# forecast feature window.
TAF_PHYSICAL_PEAK_WINDOW_START_HOUR_LOCAL = 6
TAF_PHYSICAL_PEAK_WINDOW_END_HOUR_LOCAL = 15


def scope_key(scope: Mapping[str, Any]) -> str:
    station = str(scope.get("station_id") or "").strip().upper()
    event_date = str(scope.get("event_date") or "").strip()
    timezone_name = str(
        scope.get("settlement_timezone") or ""
    ).strip()
    if not station or not event_date or not timezone_name:
        raise ContractError("taf_rotation_scope_invalid")
    return f"{station}|{event_date}|{timezone_name}"


def build_catalog(
    rows: Sequence[Mapping[str, Any]],
    *,
    as_of_utc: str,
    station_ids: Sequence[str] = (),
) -> list[dict[str, str]]:
    """Deduplicate active registry rows and retain only current/future days."""

    allowed = {
        str(value or "").strip().upper()
        for value in station_ids
        if str(value or "").strip()
    }
    scopes: dict[str, dict[str, str]] = {}
    for row in rows:
        scope = {
            "station_id": str(
                row.get("station_id") or ""
            ).strip().upper(),
            "event_date": str(row.get("event_date") or "").strip(),
            "settlement_timezone": str(
                row.get("settlement_timezone") or ""
            ).strip(),
        }
        if allowed and scope["station_id"] not in allowed:
            continue
        try:
            day_state = settlement_day_state(
                scope["event_date"],
                as_of_utc=as_of_utc,
                settlement_timezone=scope["settlement_timezone"],
            )["state"]
            key = scope_key(scope)
        except Exception:
            continue
        if day_state == "closed":
            continue
        if day_state == "open":
            try:
                as_of = datetime.fromisoformat(
                    str(as_of_utc).replace("Z", "+00:00")
                )
                if as_of.tzinfo is None:
                    raise ValueError("timezone required")
                local_now = as_of.astimezone(
                    ZoneInfo(scope["settlement_timezone"])
                )
                peak_end = datetime.combine(
                    local_now.date(),
                    time(TAF_PHYSICAL_PEAK_WINDOW_END_HOUR_LOCAL),
                    tzinfo=local_now.tzinfo,
                )
            except Exception:
                continue
            if local_now >= peak_end:
                # This is an intentionally silent catalog exclusion, not a
                # failed TAF receipt.  A later capture cannot establish
                # strict-as-of physical evidence for the finished window.
                continue
        scope["day_state"] = day_state
        prior = scopes.get(key)
        if prior is not None and prior != scope:
            raise ContractError("taf_rotation_scope_collision")
        scopes[key] = scope
    if len(scopes) > MAX_CATALOG_SCOPES:
        raise ContractError("taf_rotation_catalog_scope_cap_exceeded")
    # Forward training is most valuable before the settlement day starts:
    # the bulletin still spans the full local warming window and can later
    # satisfy decision < TAF arrival < truth close.  Open scopes remain in
    # bounded rotation after the nearest future catalog.
    priority = {"not_started": 0, "open": 1}
    return sorted(
        scopes.values(),
        key=lambda item: (
            priority[item["day_state"]],
            item["event_date"],
            item["station_id"],
            item["settlement_timezone"],
        ),
    )


def held_scope_prefixes(conn: Any) -> frozenset[str]:
    """``STATION|EVENT_DATE`` for every station-day the ledger says is held.

    The same read charter 85's forward rotation uses: a settled live BUY not
    yet sold flat.  Empty when the tables are absent or the read fails, and
    the rotation then falls back to its plain least-recently-served order.
    """

    try:
        rows = conn.execute(
            "SELECT c.station_id, c.event_date,"
            " SUM(CASE WHEN upper(i.order_action)='SELL'"
            "          THEN -r.filled_size ELSE r.filled_size END) AS net"
            " FROM basket_target_order_reconciliations r"
            " JOIN basket_target_order_intents i"
            "   ON i.order_intent_id=r.order_intent_id"
            " JOIN basket_target_execution_cycles c"
            "   ON c.cycle_id=i.cycle_id"
            " WHERE r.state IN ('resolved_filled','resolved_partial')"
            " GROUP BY c.station_id, c.event_date"
        ).fetchall()
    except Exception:  # noqa: BLE001 - a scheduling hint, never a blocker
        return frozenset()
    return frozenset(
        f"{str(station).strip().upper()}|{str(day).strip()}"
        for station, day, net in rows
        if float(net or 0.0) > 1e-9
    )


def tx_bearing_station_ids(conn: Any, *, as_of_utc: str) -> frozenset[str]:
    """Stations whose captured forward TAFs carried a TX group recently."""

    try:
        since = (
            datetime.fromisoformat(str(as_of_utc).replace("Z", "+00:00"))
            - timedelta(days=TX_COHORT_LOOKBACK_DAYS)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = conn.execute(
            "SELECT DISTINCT station_id FROM calibration_forward_taf_capture"
            " WHERE tx_available=1 AND received_at_utc>=?",
            (since,),
        ).fetchall()
    except Exception:  # noqa: BLE001 - a scheduling hint, never a blocker
        return frozenset()
    return frozenset(
        str(row[0]).strip().upper() for row in rows if str(row[0] or "").strip()
    )


def _finite_or_none(value: float) -> float | None:
    """``inf`` is not JSON, and "never" is the thing worth recording."""

    return None if value == float("inf") else round(float(value), 1)


def newest_capture_at_by_scope(conn: Any) -> dict[str, str]:
    """``STATION|EVENT_DATE`` -> the newest bulletin we hold for it.

    Arrival, not attempt.  The served ledger already answers "when did this
    scope last get a slot", and that is the question that let the lane
    starve while looking fair: a slot spent on a scope that had nothing new
    to give counts the same as one that returned a bulletin, so eight held
    station-days could take eight slots a round forever and the ledger would
    report perfect rotation.  What a scheduler needs is when something last
    arrived.

    Empty when the table is absent or the read fails, and every scope then
    reads as never-captured -- which is the safe direction: it makes the
    lane poll more, not less.
    """

    try:
        rows = conn.execute(
            "SELECT station_id, event_date, MAX(received_at_utc)"
            " FROM calibration_forward_taf_capture"
            " GROUP BY station_id, event_date"
        ).fetchall()
    except Exception:  # noqa: BLE001 - a scheduling hint, never a blocker
        return {}
    return {
        f"{str(station).strip().upper()}|{str(day).strip()}": str(received)
        for station, day, received in rows
        if str(station or "").strip() and str(received or "").strip()
    }


def capture_age_seconds(
    newest_capture_at: Mapping[str, str],
    *,
    station_id: str,
    event_date: str,
    as_of_utc: str | None,
) -> float:
    """Seconds since this scope's newest bulletin; ``inf`` when it has none."""

    if not as_of_utc:
        return float("inf")
    received = newest_capture_at.get(
        f"{str(station_id).strip().upper()}|{str(event_date).strip()}"
    )
    if not received:
        return float("inf")
    try:
        as_of = datetime.fromisoformat(str(as_of_utc).replace("Z", "+00:00"))
        arrived = datetime.fromisoformat(str(received).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001 - unreadable reads as never-captured
        return float("inf")
    if as_of.tzinfo is None or arrived.tzinfo is None:
        return float("inf")
    return max(0.0, (as_of - arrived).total_seconds())


def _local_date_after(as_of_utc: str, timezone_name: str, days: int) -> str | None:
    try:
        as_of = datetime.fromisoformat(str(as_of_utc).replace("Z", "+00:00"))
        if as_of.tzinfo is None:
            return None
        local = as_of.astimezone(ZoneInfo(timezone_name))
    except Exception:  # noqa: BLE001
        return None
    return (local.date() + timedelta(days=days)).isoformat()


def scope_priority_class(
    scope: Mapping[str, Any],
    *,
    held_prefixes: Collection[str] = (),
    tx_station_ids: Collection[str] = (),
    as_of_utc: str | None = None,
    newest_capture_at: Mapping[str, str] | None = None,
) -> int:
    """0 held, 1 nothing arrived in hours, 2 tomorrow at a TX station, 3 rest.

    A class is a claim on this round's slot, not a standing reservation.  A
    scope whose bulletin arrived inside the last hour is asking for a slot it
    cannot use, so it steps back into the general queue however it is
    classed -- which is the only reason the priority pool can be smaller than
    its membership and the tail can move at all.

    ``newest_capture_at`` absent keeps the pre-2026-09-09 behaviour exactly:
    every scope reads as never-captured, no scope is held back for freshness,
    and the stale class then covers everything the two old classes did not.
    """

    station = str(scope.get("station_id") or "").strip().upper()
    event_date = str(scope.get("event_date") or "").strip()
    age = capture_age_seconds(
        newest_capture_at or {},
        station_id=station,
        event_date=event_date,
        as_of_utc=as_of_utc,
    )
    if newest_capture_at is not None and age < PRIORITY_REPOLL_MIN_AGE_SEC:
        return PRIORITY_GENERAL
    if f"{station}|{event_date}" in set(held_prefixes):
        return PRIORITY_HELD
    if newest_capture_at is not None and age >= STALE_CONFIRMATION_AGE_SEC:
        return PRIORITY_STALE_CONFIRMATION
    if station in {str(v).strip().upper() for v in tx_station_ids} and as_of_utc:
        tomorrow = _local_date_after(
            as_of_utc, str(scope.get("settlement_timezone") or ""), 1
        )
        if tomorrow is not None and event_date == tomorrow:
            return PRIORITY_D1_TX
    return PRIORITY_GENERAL


def _without_hash(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): child
        for key, child in value.items()
        if key != "content_sha256"
    }


def _served_sequence_by_scope_key(
    previous: Mapping[str, Any] | None,
) -> dict[str, int]:
    """When each catalogued scope was last handed a poll slot.

    The rotation used to be an integer index into the catalogue, resumed only
    when the catalogue hashed the same as last round.  A fleet spanning
    twenty-odd timezones rehashes its catalogue continuously -- every station
    crossing local midnight moves between the ``not_started`` and ``open``
    blocks, and every station passing its local peak hour drops out -- so the
    index reset to zero roughly a hundred times a day and the rotation only
    ever replayed the head of the list.

    That is not a slow tail, it is an unreachable one.  On 2026-09-06 the
    catalogue held ninety scopes and ``ZUUU|2026-09-06`` sat at index 89: last
    of the alphabet, inside the lower-priority ``open`` block.  Reaching it
    needed twenty-three uninterrupted rounds, which had not happened since the
    cycle time doubled on 2026-08-26.  ZUUU's last forward TAF was captured
    2026-09-02T02:47Z while AWC was publishing ``TX35/0608Z`` for the day, and
    the whole TX-issuing cohort had fallen from 17/17 daily coverage to 5/17.

    Least-recently-served is the same round robin with a resume rule that does
    not depend on the catalogue holding still.  On a stable catalogue it is
    indistinguishable from the old cursor; under churn no scope waits longer
    than one lap, whatever moves around it.
    """

    raw = (previous or {}).get("served_sequence_by_scope_key")
    if not isinstance(raw, Mapping):
        return {}
    served: dict[str, int] = {}
    for key, value in raw.items():
        try:
            sequence = int(value)
        except (TypeError, ValueError):
            continue
        if sequence > 0:
            served[str(key)] = sequence
    return served


def load_rotation_state(path: Path) -> dict[str, Any] | None:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise ContractError("taf_rotation_state_symlink_forbidden")
    if not candidate.is_file():
        return None
    raw = load_bounded_json(candidate, limit=MAX_BYTES)
    if not isinstance(raw, Mapping) or raw.get("schema") != SCHEMA:
        raise ContractError("taf_rotation_state_invalid")
    result = dict(raw)
    supplied = str(result.get("content_sha256") or "")
    if supplied != content_sha256(_without_hash(result)):
        raise ContractError("taf_rotation_state_hash_mismatch")
    if (
        not isinstance(result.get("cursor"), int)
        or int(result["cursor"]) < 0
        or int(result.get("generation") or 0) < 1
        or not isinstance(result.get("catalog_scope_keys"), list)
        or len(result["catalog_scope_keys"]) > MAX_CATALOG_SCOPES
        # Absent on state written before least-recently-served scheduling.
        # An empty ledger reads as "nothing served yet", so the first round
        # after the upgrade walks the catalogue in order and the rotation is
        # converged one lap later -- no migration, no fail-closed window.
        or not isinstance(
            result.get("served_sequence_by_scope_key") or {}, Mapping
        )
        or len(result.get("served_sequence_by_scope_key") or {})
        > MAX_SERVED_LEDGER_SCOPES
        or not isinstance(result.get("latest_selected_scope_keys"), list)
        # Bounded by the catalogue, not by the batch.  The batch is a tuning
        # knob -- it went 4 to 12 on 2026-09-06 -- and a shape guard keyed to
        # it would turn any later reduction into a fail-closed lane: every
        # state file written under the wider batch would read as malformed
        # until someone deleted it.  A selection can never exceed the
        # catalogue, which bounds the file just as well and survives retuning.
        or len(result["latest_selected_scope_keys"])
        > MAX_CATALOG_SCOPES
        or result.get("production_no_live") is not True
        or result.get("calls_order_gateway") is not False
        or result.get("places_orders") is not False
        or int(result.get("orders_submitted") or 0) != 0
    ):
        raise ContractError("taf_rotation_state_shape_invalid")
    return result


def select_scopes(
    catalog: Sequence[Mapping[str, Any]],
    *,
    previous: Mapping[str, Any] | None,
    limit: int = MAX_SELECTED_SCOPES,
    held_scope_prefixes: Collection[str] = (),
    tx_station_ids: Collection[str] = (),
    as_of_utc: str | None = None,
    priority_slots: int = PRIORITY_SLOTS_PER_LAP,
    newest_capture_at: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], int, str]:
    keys = [scope_key(scope) for scope in catalog]
    catalog_sha = content_sha256(keys)
    if not keys:
        return [], 0, catalog_sha
    if limit < 1 or limit > MAX_SELECTED_SCOPES:
        raise ContractError("taf_rotation_selection_limit_invalid")
    served = _served_sequence_by_scope_key(previous)
    count = min(limit, len(keys))
    classes = [
        scope_priority_class(
            scope,
            held_prefixes=held_scope_prefixes,
            tx_station_ids=tx_station_ids,
            as_of_utc=as_of_utc,
            newest_capture_at=newest_capture_at,
        )
        for scope in catalog
    ]
    # Charter 85 (b): the priority pool -- held station-days, then tomorrow
    # at the TX stations -- fills up to ``priority_slots`` first, each class
    # least-recently-served within itself.  The remaining slots come from the
    # general least-recently-served queue over the whole catalogue, so the
    # priority pool can never freeze the tail the way the old head did.
    priority_order = sorted(
        (
            index for index in range(len(keys))
            if classes[index] != PRIORITY_GENERAL
        ),
        key=lambda index: (classes[index], served.get(keys[index], 0), index),
    )[:max(0, min(int(priority_slots), count))]
    taken = set(priority_order)
    # Never-served scopes sort first (sequence 0), then the longest-waiting.
    # Catalogue position only breaks ties, which is what keeps the declared
    # order -- ASOS stations, then days that have not started -- meaningful
    # without letting it monopolise the poll.
    general_order = [
        index for index in sorted(
            range(len(keys)),
            key=lambda index: (served.get(keys[index], 0), index),
        )
        if index not in taken
    ][:count - len(priority_order)]
    order = priority_order + general_order
    selected = [
        dict(
            catalog[index],
            rotation_priority_class=classes[index],
            # What the class was decided on, carried beside it: a round that
            # picks a scope for its silence should say how long the silence
            # was, or the next reader has to re-derive it from a table.
            capture_age_seconds=(
                None if newest_capture_at is None else _finite_or_none(
                    capture_age_seconds(
                        newest_capture_at,
                        station_id=str(catalog[index].get("station_id") or ""),
                        event_date=str(catalog[index].get("event_date") or ""),
                        as_of_utc=as_of_utc,
                    )
                )
            ),
        )
        for index in order
    ]
    # Retained as telemetry only: where in the catalogue this round's last
    # selection landed.  Nothing schedules off it any more.
    return selected, (order[-1] + 1) % len(keys), catalog_sha


def build_rotation_state(
    *,
    catalog: Sequence[Mapping[str, Any]],
    selected: Sequence[Mapping[str, Any]],
    next_cursor: int,
    catalog_sha256: str,
    outcomes: Sequence[Mapping[str, Any]],
    previous: Mapping[str, Any] | None,
    updated_at_utc: str,
) -> dict[str, Any]:
    selected_keys = [scope_key(scope) for scope in selected]
    if len(selected_keys) > MAX_SELECTED_SCOPES:
        raise ContractError("taf_rotation_selected_scope_cap_exceeded")
    outcome_rows = [
        {
            "scope_key": str(row.get("scope_key") or ""),
            "state": str(row.get("state") or ""),
            "reason": str(row.get("reason") or ""),
        }
        for row in outcomes
    ]
    if {row["scope_key"] for row in outcome_rows} != set(selected_keys):
        raise ContractError("taf_rotation_outcome_scope_mismatch")
    catalog_keys = [scope_key(scope) for scope in catalog]
    # A slot spent is a slot spent: a scope that failed its fetch is stamped
    # exactly like one that captured.  Stamping only successes would hand a
    # permanently broken scope -- EGLC has answered
    # ``awc_taf_history_row_missing`` on every attempt for days -- the front of
    # the queue every round, which is the starvation this replaced.
    served = dict(_served_sequence_by_scope_key(previous))
    sequence = max([0, *served.values()])
    for key in selected_keys:
        sequence += 1
        served[key] = sequence
    # Bounded by age, not by catalogue membership.  Dropping the entries for
    # scopes that are not in today's catalogue would reset a scope that left
    # for one round -- a registry read that briefly came back short, say -- and
    # an unstamped scope outranks every waiting one, so it would jump the queue
    # each time it flickered.  Keeping the most recently served instead means
    # only a scope genuinely absent for a full ledger's worth of slots is
    # forgotten, and forgetting it then costs nothing: it was next anyway.
    if len(served) > MAX_SERVED_LEDGER_SCOPES:
        served = dict(
            sorted(
                served.items(),
                key=lambda item: (-item[1], item[0]),
            )[:MAX_SERVED_LEDGER_SCOPES]
        )
    identity = {
        "schema": SCHEMA,
        "generation": int((previous or {}).get("generation") or 0) + 1,
        "updated_at_utc": str(updated_at_utc),
        "catalog_sha256": str(catalog_sha256),
        "catalog_scope_keys": catalog_keys,
        "catalog_scope_count": len(catalog),
        "cursor": int(next_cursor),
        "served_sequence_by_scope_key": served,
        "latest_selected_scope_keys": selected_keys,
        # Charter 85 (b): which priority class each selected scope came in
        # under -- telemetry, so a lap can be read as "held / d1 TX / general".
        "latest_selected_priority_classes": {
            scope_key(scope): int(scope.get("rotation_priority_class", PRIORITY_GENERAL))
            for scope in selected
        },
        "latest_outcomes": outcome_rows,
        "production_no_live": True,
        "calls_order_gateway": False,
        "places_orders": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def persist_rotation_state(path: Path, value: Mapping[str, Any]) -> bool:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise ContractError("taf_rotation_state_symlink_forbidden")
    return write_bounded_json_if_changed(
        candidate,
        value,
        limit=MAX_BYTES,
    )


__all__ = [
    "MAX_SELECTED_SCOPES",
    "PRIORITY_SLOTS_PER_LAP",
    "PRIORITY_HELD",
    "PRIORITY_D1_TX",
    "PRIORITY_GENERAL",
    "TX_COHORT_LOOKBACK_DAYS",
    "held_scope_prefixes",
    "tx_bearing_station_ids",
    "scope_priority_class",
    "MAX_SERVED_LEDGER_SCOPES",
    "TAF_PHYSICAL_PEAK_WINDOW_START_HOUR_LOCAL",
    "TAF_PHYSICAL_PEAK_WINDOW_END_HOUR_LOCAL",
    "SCHEMA",
    "build_catalog",
    "build_rotation_state",
    "load_rotation_state",
    "persist_rotation_state",
    "scope_key",
    "select_scopes",
    "newest_capture_at_by_scope",
    "capture_age_seconds",
    "PRIORITY_STALE_CONFIRMATION",
]
