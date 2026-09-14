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
#
# Thirty-six since 2026-09-14.  Twelve still left the lap at three and a half
# hours against a 117-scope catalogue, and the measured cost of that was a
# bulletin reaching the inbox a median 34-109 minutes after AWC published it
# (p90 2-4.7 hours, every day 09-04..09-13) while AWC itself had it inside six
# minutes -- the card read "57 of 64 scopes over three hours" on the morning of
# the 14th.  What made the wider round affordable is that a round now fetches
# each STATION once and parses its two or three days from the same response
# (capture_forward_awc_taf's response_cache): the budget pays per request, and
# a station's issuance makes all its days due together, so they are selected
# together.  The ninety-second round budget is unchanged and still stops a
# slow round; the unattempted remainder still leads the next one.
MAX_SELECTED_SCOPES = 36
# Charter 85 (b) (user ruling, 2026-09-06: "TAF 轮询改成已持仓站日优先、d1 带
# TX 的 17 站优先"): each lap fills its priority slots first -- station-days
# the book holds, then tomorrow's scope at the stations whose TAFs carry a TX
# group -- and only then the general least-recently-served queue.  The cap
# keeps the rest of the catalogue moving: with ~14 held station-days and ~18
# TX stations the priority pool alone is larger than a lap, and a queue that
# never reaches the general block is the ZUUU starvation with a new head.
# Two thirds of the round, the proportion the twelve-scope round was tuned
# at (eight of twelve), kept when the round widened to thirty-six.
PRIORITY_SLOTS_PER_LAP = 24
# Of those, how many are reserved for the stale class.
#
# The cap above bounds the priority pool against the general queue and says
# nothing about the pool's own composition, which is the whole of the second
# failure: the book holds eight to fourteen station-days, so the held class
# alone fills eight slots on every round, and a class ranked below it never
# receives one however long its silence.  Measured on the first two live
# rounds of the stale class -- zero slots, while KLGA and KMIA sat ten hours
# without a bulletin and refused every cycle on
# resident_p0m_replay_required.
#
# Three of eight, so the held majority survives and the starved scopes get a
# guaranteed share rather than a rank.  Unused reserved slots fall back to
# the rest of the pool, so a fleet with nothing stale loses nothing.
# Nine since the round widened to thirty-six: the same three in eight of the
# priority pool.
STALE_CONFIRMATION_SLOTS_PER_LAP = 9
# A station is in the TX cohort while one of its captured TAFs in this many
# trailing days carried a TX group; US TAFs never do, and the cohort is read
# off the capture table rather than hard-coded so it follows the bulletins.
TX_COHORT_LOOKBACK_DAYS = 30
PRIORITY_HELD = 0
# A scope whose station should have issued again by its own rhythm, and whose
# newest bulletin is still the one before that.
#
# A TAF is not published continuously; it is published on a schedule, and the
# schedule is the station's, not ours.  Measured over 2026-08-20..09-09, 1,108
# issuances across 45 stations: 36 of them run a strict six-hourly cadence --
# OEJN, OPKC, RCSS, RJTT, RPLL, SAEZ, WSSS and LTAC have a median gap of 6.00h
# with a 10th percentile of 6.00h, meaning every gap is six hours -- while
# eight amend on a two-to-three hour rhythm (the US majors, EFHK) and MMMX
# amends hourly.  The fleet publishes about 185 bulletins a day.
#
# The rotation was polling ~576 scope-turns a day and spreading them evenly,
# so it fired about three times per bulletin that existed and gave MMMX (8.5
# issuances a day) the same share as WSSS (1.9).  Asking a six-hourly station
# four times inside one of its cycles returns the bulletin we already hold,
# three times.
#
# This class is the difference between "we have not heard in a while", which
# may be correct because nothing was published, and "something was published
# and we do not have it".  Only the second is worth a priority slot.
PRIORITY_DUE_ISSUANCE = 1
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
PRIORITY_STALE_CONFIRMATION = 2
PRIORITY_D1_TX = 3
PRIORITY_GENERAL = 4
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
#
# It takes a scope that HAS a bulletin and stopped getting them.  A scope
# with none at all is not the same thing and was briefly treated as the
# extreme of it, which put every never-captured scope at the head of the
# class: measured on this rule's first live round, all four of its
# non-held slots went to D+2 scopes that had just entered the catalogue,
# and all four failed ``awc_taf_peak_window_features_missing`` -- a TAF
# spans 24 to 30 hours, so the bulletin covering a day after tomorrow's
# local peak does not exist yet.  Those belong in the general queue, which
# already sorts never-served first and reaches them without spending a
# priority slot on a fetch that cannot succeed.
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


# How far back the cadence is learned from, and the least evidence that will
# be trusted.  A station is asked about a handful of times a day, so a short
# window is a small sample of a rhythm that only repeats four times daily.
ISSUANCE_CADENCE_LOOKBACK_DAYS = 20
MIN_ISSUANCES_FOR_CADENCE = 8
# What a station with too little history is treated as.  Six hours is the
# scheduled TAF cycle and the fleet median; assuming it means an unknown
# station is asked on the ordinary schedule rather than constantly or never.
DEFAULT_ISSUANCE_CADENCE_SEC = 6.0 * 3600.0
# When holding nothing newer means a bulletin is missing.  Every station issues
# on the six-hour synoptic schedule whatever it amends in between, so a scope
# whose newest held bulletin was ISSUED more than one scheduled cycle and an
# hour ago has missed one.  Issue time, not receipt: an age measured from our
# own receipt is 0-6 hours for a perfectly served six-hourly station purely by
# where in the cycle it is read, and a three-hour threshold on it reads half of
# every cycle as stale -- 2026-09-14 05:49Z, one hour after the 05Z issuance,
# "50 of 65 scopes over three hours" for a lane whose only fault was a slow lap.
BULLETIN_OVERDUE_SEC = DEFAULT_ISSUANCE_CADENCE_SEC + 3600.0
#: How far back the capture-lag summary reads.
CAPTURE_LAG_LOOKBACK_HOURS = 24.0
# How early a scope may claim the slot before its next issuance is due.  The
# bulletin is written and filed a few minutes before it is fetchable, and a
# poll that arrives exactly on the boundary usually gets the previous one.
ISSUANCE_DUE_LEAD_SEC = 5.0 * 60.0
# The one failure that means "not yet", rather than "not working".
#
# ``awc_taf_peak_window_features_missing`` says the bulletin we can fetch does
# not reach that event day's local warming window.  For a day two out that is
# ordinary and temporary: TAFs span 24 to 30 hours, and the bulletin that will
# reach it has not been written.  Retrying before the station issues again
# cannot succeed, and the served ledger stamps a failed slot exactly like a
# successful one, so the scope returns on the next lap and fails again.
#
# Measured 2026-09-09 14:48Z: six of a twelve-scope round, every one a D+2
# scope -- KDAL, EDDM, LIMC, LTAC, MMMX, MPMG, all for 09-11 -- half the lap
# spent on bulletins that did not exist.  A static horizon cannot fix it: 20%
# of all successful captures came from more than 30 hours out and the furthest
# was 38.5, so whether a far scope is fetchable depends on when the current
# bulletin was issued, not on how far away the day is.  The failure itself is
# the only reliable evidence, and it is worth exactly one issuance cycle.
WINDOW_MISSING_REASON = "awc_taf_peak_window_features_missing"
MAX_WINDOW_MISSING_LEDGER_SCOPES = MAX_CATALOG_SCOPES * 2


def station_issuance_cadence_sec(
    conn: Any, *, as_of_utc: str | None = None
) -> dict[str, float]:
    """Each station's own publishing rhythm, in seconds, from its own history.

    The median gap between consecutive issuances, not a constant: measured
    2026-08-20..09-09 the fleet splits into a strict six-hourly majority and
    a handful that amend every two to three hours, with MMMX at one.  A
    rotation that does not know the difference spends the same number of
    turns on a station that publishes twice a day as on one that publishes
    eight times.

    Median rather than mean, because an amendment lands between two
    scheduled issuances and shortens exactly one gap; the median keeps the
    scheduled rhythm and the mean does not.

    Empty on any failure, which reads as "cadence unknown" everywhere and
    leaves the rotation exactly as it was.
    """

    try:
        since = (
            datetime.fromisoformat(str(as_of_utc).replace("Z", "+00:00"))
            - timedelta(days=ISSUANCE_CADENCE_LOOKBACK_DAYS)
        ).strftime("%Y-%m-%dT%H:%M:%SZ") if as_of_utc else "0000"
        rows = conn.execute(
            "SELECT station_id, issued_at_utc"
            " FROM calibration_forward_taf_capture"
            " WHERE issued_at_utc>=?",
            (since,),
        ).fetchall()
    except Exception:  # noqa: BLE001 - a scheduling hint, never a blocker
        return {}
    seen: dict[str, set[str]] = {}
    for station, issued in rows:
        key = str(station or "").strip().upper()
        text = str(issued or "").strip()
        if key and text:
            seen.setdefault(key, set()).add(text)
    cadence: dict[str, float] = {}
    for station, stamps in seen.items():
        moments = []
        for text in sorted(stamps):
            try:
                moments.append(
                    datetime.fromisoformat(text.replace("Z", "+00:00"))
                )
            except ValueError:
                continue
        if len(moments) < MIN_ISSUANCES_FOR_CADENCE:
            continue
        gaps = sorted(
            (later - earlier).total_seconds()
            for earlier, later in zip(moments, moments[1:])
            # A day-long gap is an outage of ours, not a rhythm of theirs.
            if 0.0 < (later - earlier).total_seconds() < 24.0 * 3600.0
        )
        if gaps:
            cadence[station] = gaps[len(gaps) // 2]
    return cadence


def latest_issued_at_by_scope(conn: Any) -> dict[str, str]:
    """``STATION|EVENT_DATE`` -> the newest bulletin we hold, by ISSUE time.

    Issue time, not receipt: whether a newer bulletin exists is a question
    about the forecaster's clock, and receipt is ours.  The same distinction
    the 2026-09-03 seatability ruling turned on one layer down.
    """

    try:
        rows = conn.execute(
            "SELECT station_id, event_date, MAX(issued_at_utc)"
            " FROM calibration_forward_taf_capture"
            " GROUP BY station_id, event_date"
        ).fetchall()
    except Exception:  # noqa: BLE001 - a scheduling hint, never a blocker
        return {}
    return {
        f"{str(station).strip().upper()}|{str(day).strip()}": str(issued)
        for station, day, issued in rows
        if str(station or "").strip() and str(issued or "").strip()
    }


def issuance_is_due(
    *,
    station_id: str,
    event_date: str,
    latest_issued_at: Mapping[str, str],
    cadence_sec: Mapping[str, float],
    as_of_utc: str | None,
) -> bool:
    """Whether this station should have published again since what we hold."""

    if not as_of_utc:
        return False
    station = str(station_id).strip().upper()
    held = latest_issued_at.get(f"{station}|{str(event_date).strip()}")
    if not held:
        # Nothing held for this scope is not evidence that something new
        # exists; the general queue reaches a never-captured scope on its
        # own turn without spending a priority slot on it.
        return False
    try:
        as_of = datetime.fromisoformat(str(as_of_utc).replace("Z", "+00:00"))
        issued = datetime.fromisoformat(str(held).replace("Z", "+00:00"))
    except ValueError:
        return False
    if as_of.tzinfo is None or issued.tzinfo is None:
        return False
    cadence = float(
        cadence_sec.get(station, DEFAULT_ISSUANCE_CADENCE_SEC)
    )
    due_at = issued + timedelta(seconds=cadence - ISSUANCE_DUE_LEAD_SEC)
    return as_of >= due_at


def _window_missing_at_by_scope_key(
    previous: Mapping[str, Any] | None,
) -> dict[str, str]:
    value = (previous or {}).get("window_missing_at_by_scope_key")
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): str(stamp)
        for key, stamp in value.items()
        if str(key) and str(stamp)
    }


def window_missing_backoff_keys(
    catalog: Sequence[Mapping[str, Any]],
    *,
    window_missing_at: Mapping[str, str],
    issuance_cadence_sec: Mapping[str, float],
    as_of_utc: str | None,
) -> set[str]:
    """Scopes whose bulletin cannot have been written since they last said so."""

    if not as_of_utc or not window_missing_at:
        return set()
    try:
        as_of = datetime.fromisoformat(str(as_of_utc).replace("Z", "+00:00"))
    except ValueError:
        return set()
    if as_of.tzinfo is None:
        return set()
    holding: set[str] = set()
    for scope in catalog:
        key = scope_key(scope)
        stamp = window_missing_at.get(key)
        if not stamp:
            continue
        try:
            failed_at = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            continue
        if failed_at.tzinfo is None:
            continue
        station = str(scope.get("station_id") or "").strip().upper()
        cadence = float(
            issuance_cadence_sec.get(station, DEFAULT_ISSUANCE_CADENCE_SEC)
        )
        if as_of < failed_at + timedelta(seconds=cadence):
            holding.add(key)
    return holding


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


def capture_lag_summary(
    conn: Any, *, as_of_utc: str, lookback_hours: float = CAPTURE_LAG_LOOKBACK_HOURS
) -> dict[str, Any]:
    """How long a published bulletin took to reach us, over the lookback.

    Receipt minus AWC's own availability stamp, one sample per scope and
    bulletin (the capture table holds each once).  This is the number a
    freshness count cannot show: a lane that captures every bulletin an hour
    late never has a scope past the overdue line, and it is still an hour
    behind the forecaster -- measured 09-04..09-13 at a median of 34-109
    minutes against AWC's six.

    Empty (sample_count 0) on any failure; a report, never a blocker.
    """

    try:
        since = (
            datetime.fromisoformat(str(as_of_utc).replace("Z", "+00:00"))
            - timedelta(hours=float(lookback_hours))
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = conn.execute(
            "SELECT received_at_utc, provider_available_at_utc"
            " FROM calibration_forward_taf_capture"
            " WHERE received_at_utc>=?",
            (since,),
        ).fetchall()
    except Exception:  # noqa: BLE001 - a report, never a blocker
        rows = []
    lags: list[float] = []
    for received, available in rows:
        try:
            lag = (
                datetime.fromisoformat(str(received).replace("Z", "+00:00"))
                - datetime.fromisoformat(str(available).replace("Z", "+00:00"))
            ).total_seconds()
        except (TypeError, ValueError):
            continue
        if lag >= 0.0:
            lags.append(lag)
    lags.sort()

    def _quantile(q: float) -> float | None:
        if not lags:
            return None
        return round(lags[int(q * (len(lags) - 1))] / 60.0, 1)

    return {
        "lookback_hours": float(lookback_hours),
        "sample_count": len(lags),
        "p50_minutes": _quantile(0.5),
        "p90_minutes": _quantile(0.9),
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
    latest_issued_at: Mapping[str, str] | None = None,
    issuance_cadence_sec: Mapping[str, float] | None = None,
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
    # Something was published and we do not have it -- the only case where a
    # turn is certain to return something, and the reason a six-hourly
    # station does not need asking four times inside one of its cycles.
    if latest_issued_at is not None and issuance_is_due(
        station_id=station,
        event_date=event_date,
        latest_issued_at=latest_issued_at,
        cadence_sec=issuance_cadence_sec or {},
        as_of_utc=as_of_utc,
    ):
        return PRIORITY_DUE_ISSUANCE
    if (
        newest_capture_at is not None
        and age != float("inf")
        and age >= STALE_CONFIRMATION_AGE_SEC
    ):
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
    stale_slots: int = STALE_CONFIRMATION_SLOTS_PER_LAP,
    latest_issued_at: Mapping[str, str] | None = None,
    issuance_cadence_sec: Mapping[str, float] | None = None,
) -> tuple[list[dict[str, Any]], int, str]:
    keys = [scope_key(scope) for scope in catalog]
    catalog_sha = content_sha256(keys)
    if not keys:
        return [], 0, catalog_sha
    # A scope that said "the bulletin does not reach that day yet" keeps its
    # place in the queue and gives up its turn until its station issues again.
    # It is held back rather than dropped: the served ledger is untouched, so
    # it returns at the front once the hold expires, and if holding back would
    # empty the round the hold is ignored -- a lane that fetches nothing
    # learns nothing.
    holding = window_missing_backoff_keys(
        catalog,
        window_missing_at=_window_missing_at_by_scope_key(previous),
        issuance_cadence_sec=issuance_cadence_sec or {},
        as_of_utc=as_of_utc,
    )
    if holding and len(holding) < len(keys):
        catalog = [
            scope for scope in catalog if scope_key(scope) not in holding
        ]
        keys = [scope_key(scope) for scope in catalog]
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
            latest_issued_at=latest_issued_at,
            issuance_cadence_sec=issuance_cadence_sec,
        )
        for scope in catalog
    ]
    # Charter 85 (b): the priority pool -- held station-days, then tomorrow
    # at the TX stations -- fills up to ``priority_slots`` first, each class
    # least-recently-served within itself.  The remaining slots come from the
    # general least-recently-served queue over the whole catalogue, so the
    # priority pool can never freeze the tail the way the old head did.
    # Within a class, least-recently-served -- except the stale class, which
    # is ordered by the silence that put it there.  Served sequence is the
    # wrong question for it twice over: it is the measure that reported
    # perfect rotation through the starvation, and a scope that has never
    # been served sorts first under it, which is how a catalogue entry an
    # hour old outranked a station that had gone ten hours without a
    # bulletin.
    def _within_class(index: int) -> float:
        if classes[index] not in (
            PRIORITY_DUE_ISSUANCE, PRIORITY_STALE_CONFIRMATION
        ):
            return float(served.get(keys[index], 0))
        return -capture_age_seconds(
            newest_capture_at or {},
            station_id=str(catalog[index].get("station_id") or ""),
            event_date=str(catalog[index].get("event_date") or ""),
            as_of_utc=as_of_utc,
        )

    priority_budget = max(0, min(int(priority_slots), count))
    # The reservation covers both supply classes, not just the older one.
    #
    # It was written for PRIORITY_STALE_CONFIRMATION alone, and inserting
    # PRIORITY_DUE_ISSUANCE above it would have handed the new class nothing:
    # the held pool is thirteen scopes against eight priority slots, so
    # anything outside the reservation is filled by held and never reaches a
    # class ranked below it.  That is the same trap the reservation was
    # created to escape, one class along.
    #
    # Due-issuance takes the reserved slots first -- "something was published
    # and we do not have it" is strictly better information than "we have not
    # heard in a while" -- and silence takes what it leaves.
    stale_budget = max(0, min(int(stale_slots), priority_budget))
    stale_order = sorted(
        (
            index for index in range(len(keys))
            if classes[index] in (
                PRIORITY_DUE_ISSUANCE, PRIORITY_STALE_CONFIRMATION
            )
        ),
        key=lambda index: (classes[index], _within_class(index), index),
    )[:stale_budget]
    # The reservation is a floor, not a ceiling: whatever the stale class
    # does not use is filled from the rest of the pool, and whatever the
    # rest does not use is left to the general queue below.
    rest_order = sorted(
        (
            index for index in range(len(keys))
            if classes[index] != PRIORITY_GENERAL
            and index not in set(stale_order)
        ),
        key=lambda index: (classes[index], _within_class(index), index),
    )[:priority_budget - len(stale_order)]
    priority_order = stale_order + rest_order
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
    # When each scope last reported that the bulletin does not reach its day.
    # Carried across rounds, because the fact it records outlives the round
    # that learned it; cleared the moment the scope captures, because that is
    # the bulletin having arrived.
    window_missing = dict(_window_missing_at_by_scope_key(previous))
    for row in outcome_rows:
        if WINDOW_MISSING_REASON in row["reason"]:
            window_missing[row["scope_key"]] = str(updated_at_utc)
        elif row["state"] == "captured":
            window_missing.pop(row["scope_key"], None)
    if len(window_missing) > MAX_WINDOW_MISSING_LEDGER_SCOPES:
        window_missing = dict(
            sorted(
                window_missing.items(),
                key=lambda item: (item[1], item[0]),
                reverse=True,
            )[:MAX_WINDOW_MISSING_LEDGER_SCOPES]
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
        "window_missing_at_by_scope_key": window_missing,
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
    "BULLETIN_OVERDUE_SEC",
    "capture_lag_summary",
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
    "PRIORITY_DUE_ISSUANCE",
    "station_issuance_cadence_sec",
    "latest_issued_at_by_scope",
    "issuance_is_due",
    "window_missing_backoff_keys",
    "WINDOW_MISSING_REASON",
    "STALE_CONFIRMATION_SLOTS_PER_LAP",
]
