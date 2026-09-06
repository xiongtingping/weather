"""Restart-safe bounded rotation for forward TAF training-fact capture."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence
from datetime import datetime, time, timezone
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
) -> tuple[list[dict[str, Any]], int, str]:
    keys = [scope_key(scope) for scope in catalog]
    catalog_sha = content_sha256(keys)
    if not keys:
        return [], 0, catalog_sha
    if limit < 1 or limit > MAX_SELECTED_SCOPES:
        raise ContractError("taf_rotation_selection_limit_invalid")
    served = _served_sequence_by_scope_key(previous)
    count = min(limit, len(keys))
    # Never-served scopes sort first (sequence 0), then the longest-waiting.
    # Catalogue position only breaks ties, which is what keeps the declared
    # priority -- ASOS stations, then days that have not started -- meaningful
    # without letting it monopolise the poll.
    order = sorted(
        range(len(keys)),
        key=lambda index: (served.get(keys[index], 0), index),
    )[:count]
    selected = [dict(catalog[index]) for index in order]
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
]
