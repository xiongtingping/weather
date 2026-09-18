#!/usr/bin/env python3
"""Capture one official GEFS+ECMWF d2 forward snapshot for the 45 stations.

This is a bounded no-live operator command.  It fetches each official global
member field once, retains only the minimal content-addressed receipts and
station-member values, and then delegates packet construction to
``build_official_native_forward_inbox.py``.  It neither opens SQLite nor
reads a probability, TAF, METAR, CLOB, or order service.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.ecmwf_ifs_archive import (  # noqa: E402
    capture_ecmwf_station_days,
    ingest_ecmwf_station_days,
    resolve_run_cycle_hour as ecmwf_resolve_run_cycle_hour,
)
from bayes_basket_target.market_registry import (  # noqa: E402
    load_market_registry_snapshot,
)
from bayes_basket_target.noaa_gefs_archive import (  # noqa: E402
    capture_gefs_station_days,
    ingest_gefs_station_days,
    resolve_run_cycle_hour as gefs_resolve_run_cycle_hour,
)
from bayes_basket_target.eccc_geps_archive import (  # noqa: E402
    RUN_CYCLE_HOUR_BY_LEAD as ECCC_RUN_CYCLE_HOUR_BY_LEAD,
    capture_geps_station_days,
    ingest_geps_station_days,
)
from bayes_basket_target.dwd_icon_eps_archive import (  # noqa: E402
    RUN_CYCLE_HOUR_BY_LEAD as ICON_RUN_CYCLE_HOUR_BY_LEAD,
    capture_icon_eps_station_days,
    ingest_icon_eps_station_days,
)
from bayes_basket_target.future_temperature_curve import (  # noqa: E402
    MIN_CURVE_POINTS,
    MIN_SAME_DAY_CURVE_POINTS,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_connection_target_capacity,
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json_if_changed,
)
from bayes_basket_target.station_registry import (  # noqa: E402
    coordinate_registry_manifest,
    station_coordinates,
)


DEFAULT_RUNTIME_ROOT = ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
DEFAULT_REGISTRY = DEFAULT_RUNTIME_ROOT / "market_registry_forward.json"
DEFAULT_GEFS_OUTPUT = DEFAULT_RUNTIME_ROOT / "official_noaa_gefs_forward_archive.json"
DEFAULT_ECMWF_OUTPUT = DEFAULT_RUNTIME_ROOT / "official_ecmwf_ifs_forward_archive.json"
DEFAULT_INBOX_OUTPUT = DEFAULT_RUNTIME_ROOT / "official_native_ensemble_forward_inbox.json"
MAX_STATIONS = expected_station_count()
MIN_TIMEOUT_SECONDS = 5.0
MAX_TIMEOUT_SECONDS = 120.0
# ICON's fetches are whole objects, not byte ranges: one ~36MiB file per lead
# carrying all forty members.  The caller's ``--timeout-seconds`` is sized for
# the sub-megabyte range reads the other three families make, and reusing it
# here made a merely slow link read as a dead one.  This is the read timeout
# for a single streamed object, not a budget for the pass; the pass as a whole
# is still bounded by the caller's deadline.
ICON_OBJECT_TIMEOUT_SECONDS = 180.0
# Forward P0 consumes a bounded current snapshot.  A retained archive older
# than this is historical evidence, not a current snapshot, and must be
# refreshed from the official provider (or remain blocked if unavailable).
FORWARD_REUSE_TTL_SECONDS = 6 * 60 * 60


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _parse_utc_timestamp(value: object) -> datetime | None:
    """Parse a receipt timestamp without accepting local/naive time."""

    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


# Charter 148: the per-lead archive name the worker hands each rung,
# ``{family}_{event_date}_d{lead}.json``.  One family's runs for one event
# date sit in up to three of these, one per lead the ladder has read it at.
_LEAD_ARCHIVE_NAME = re.compile(
    r"^(?P<stem>.+_\d{4}-\d{2}-\d{2})_d(?P<lead>[0-2])\.json$"
)


def _other_lead_archive_paths(path: Path) -> list[Path]:
    """The same family's archives for the same event date at the other leads."""

    match = _LEAD_ARCHIVE_NAME.match(path.name)
    if match is None:
        return []
    lead = int(match["lead"])
    return [
        path.with_name(f"{match['stem']}_d{other}.json")
        for other in (0, 1, 2)
        if other != lead
    ]


def _load_retained_archive(
    path: Path,
    *,
    event_date: str,
    station_ids: Sequence[str],
    expected_schema: str,
    as_of_utc: str | None = None,
    max_reuse_age_seconds: float | None = None,
    expected_run_init_utc: str | None = None,
) -> dict[str, Any] | None:
    """Reuse one retained official archive instead of downloading it again.

    Reuse is conservative: malformed, tampered, wrong-date or wrong-scope
    files are treated as absent and the bounded capture path may refresh them.
    This keeps native precedence while preventing duplicate ECMWF/GEFS pulls.

    ``expected_run_init_utc`` makes reuse a question about run identity rather
    than about the clock.  The runs are immutable, so an archive drawn from the
    run this capture wants is current no matter how old the file is, and an
    archive drawn from any other run is wrong no matter how fresh -- the TTL
    got both directions wrong: it re-downloaded ICON's unchanged run once the
    file aged past six hours, and it served a stale 00Z archive to a capture
    asked for the just-published 06Z run for as long as the file stayed inside
    them.  The TTL path remains for callers that do not know the run.
    """

    raw_path = path.expanduser()
    if raw_path.is_symlink():
        return None
    candidate = raw_path.resolve()
    if not candidate.is_file() or candidate.is_symlink():
        return None
    try:
        payload = load_bounded_json(candidate)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    identity = {
        key: value for key, value in payload.items() if key != "content_sha256"
    }
    if (
        payload.get("schema") != expected_schema
        or payload.get("content_sha256") != content_sha256(identity)
        or str(payload.get("event_date") or "") != str(event_date)
        or payload.get("training_only") is not True
        or payload.get("holdout_eligible") is not False
    ):
        return None
    expected = tuple(sorted(str(value).strip().upper() for value in station_ids))
    captures = payload.get("captures")
    if not isinstance(captures, list):
        return None
    # The station set is read from the captures, falling back to the top-level
    # index only if they do not carry it.  That index is a convenience, not the
    # content: an archive whose captures name all forty-five stations holds all
    # forty-five whether or not it also lists them at the top.  Trusting the
    # index alone made one family -- the only one that omitted it -- fail this
    # check forever and re-download in full on every pass, with a complete and
    # perfectly reusable copy sitting on disk.
    actual = tuple(sorted(
        str(row.get("station_id") or "").strip().upper()
        for row in captures
        if isinstance(row, dict)
    ))
    if not actual or "" in actual:
        actual = tuple(sorted(
            str(value).strip().upper()
            for value in payload.get("station_ids") or ()
        ))
    # Coverage, not equality: a retained archive may hold more stations than
    # this pass asks for.  Same-day cycle passes serve only the stations
    # their run can still cover, while ECCC and ICON ride those passes on
    # the same 00Z run already retained for the full set -- demanding the
    # exact set would re-download an identical ICON run (~543MiB) to read a
    # subset of it.  The archive's internal consistency stays exact, and
    # every requested station must still be present.
    if (
        not actual
        or len(set(actual)) != len(actual)
        or int(payload.get("station_count") or 0) != len(actual)
        or len(captures) != len(actual)
        or not set(expected).issubset(set(actual))
    ):
        return None
    if expected_run_init_utc is not None:
        # Run identity decides outright; the clock is irrelevant both ways.
        # Malformed either side fails closed to a fresh capture.
        held = _parse_utc_timestamp(payload.get("run_init_utc"))
        wanted = _parse_utc_timestamp(expected_run_init_utc)
        if held is None or wanted is None or held != wanted:
            return None
        return payload
    # Forward reuse is freshness-gated.  Historical archives remain reusable
    # when callers omit the optional clock/TTL arguments (for example, an
    # offline unit probe), while the production forward path always supplies
    # the six-hour P0 TTL.  Missing or malformed query receipts fail closed.
    if max_reuse_age_seconds is not None:
        try:
            max_age = float(max_reuse_age_seconds)
        except (TypeError, ValueError):
            return None
        if max_age < 0:
            return None
        query_at = _parse_utc_timestamp(payload.get("archive_query_at_utc"))
        as_of = _parse_utc_timestamp(as_of_utc)
        if query_at is None or as_of is None or query_at > as_of:
            return None
        if (as_of - query_at).total_seconds() > max_age:
            return None
    return payload


def _canonical_station_ids() -> tuple[str, ...]:
    station_ids = tuple(
        str(value).strip().upper()
        for value in coordinate_registry_manifest()["station_ids"]
    )
    if len(station_ids) != MAX_STATIONS or len(set(station_ids)) != len(station_ids):
        raise ContractError("official_native_forward_canonical_station_set_invalid")
    return station_ids



def _capture_regional_point_archives(
    *,
    event_date: str,
    scopes: Sequence[dict[str, Any]],
    capture_at: str,
    lead_days: int,
) -> dict[str, Any]:
    """Charter 95: the regional deterministic models' freshest runs per station.

    Read-only against the SSOT, like the TAF projection: the regional capture
    lanes persisted the runs as train-only facts; this projects each
    station's newest admissible run per system into the packet archive shape.
    """

    from bayes_basket_target.native_ensemble_training_facts import (
        build_regional_point_forward_archives_for_station,
    )
    from bayes_basket_target.p0_producer import (
        bucket_intervals_from_registry_rows,
    )

    archives_by_station: dict[str, list[dict[str, Any]]] = {}
    conn = sqlite3.connect(
        f"file:{DEFAULT_TARGET_DB}?mode=ro", uri=True, timeout=30.0
    )
    try:
        for scope in scopes:
            station_id = str(scope["station_id"])
            registry_rows = list(scope.get("registry_rows") or [])
            timezone_name = str(
                scope.get("settlement_timezone")
                or (registry_rows[0].get("settlement_timezone") if registry_rows else "")
                or ""
            ).strip()
            if not timezone_name:
                continue
            intervals = bucket_intervals_from_registry_rows(registry_rows)
            frozen = {
                bucket: [
                    None if lower is None else float(lower),
                    None if upper is None else float(upper),
                ]
                for bucket, (lower, upper) in intervals.items()
            }
            archives = build_regional_point_forward_archives_for_station(
                conn,
                station_id=station_id,
                event_date=event_date,
                settlement_timezone=timezone_name,
                as_of_utc=capture_at,
                bucket_intervals=frozen,
            )
            if archives:
                archives_by_station[station_id] = archives
    finally:
        conn.close()
    return {
        "schema": "official_regional_point_forward_archives_v1",
        "event_date": event_date,
        "archive_lead_bucket": f"d{int(lead_days)}",
        "captured_at_utc": capture_at,
        "archives_by_station": archives_by_station,
    }


def build_station_scopes(
    registry: dict[str, Any],
    *,
    event_date: str,
    station_ids: Sequence[str] | None = None,
    skip_unlisted_stations: bool = False,
) -> list[dict[str, Any]]:
    """Resolve complete, source-eligible registry scopes before any fetch.

    ``skip_unlisted_stations`` captures whatever the registry currently lists
    for the day rather than insisting on the full canonical set.  A model run
    is downloaded once and read for every station at once, so the caller that
    wants the whole run cannot name its stations in advance -- an event date
    three days out legitimately lists 29 of 45.  Only "this station has no
    market on this day" is skipped; a station the registry does list but
    describes incoherently still fails closed, because that is a contract
    violation rather than a market that has not opened yet.
    """

    canonical = _canonical_station_ids()
    requested = canonical if station_ids is None else tuple(
        sorted({str(value).strip().upper() for value in station_ids if str(value).strip()})
    )
    if not requested or len(requested) > MAX_STATIONS:
        raise ContractError("official_native_forward_station_scope_invalid")
    unknown = sorted(set(requested) - set(canonical))
    if unknown:
        raise ContractError(
            "official_native_forward_station_not_canonical:" + ",".join(unknown)
        )
    rows = registry.get("rows")
    if not isinstance(rows, list):
        raise ContractError("official_native_forward_registry_rows_invalid")
    scopes: list[dict[str, Any]] = []
    for station_id in requested:
        matching = [
            dict(row)
            for row in rows
            if isinstance(row, dict)
            and str(row.get("station_id") or "").strip().upper() == station_id
            and str(row.get("event_date") or "") == event_date
            and int(row.get("market_active") or 0) == 1
            and int(row.get("source_eligible") or 0) == 1
        ]
        if not matching:
            if skip_unlisted_stations:
                continue
            raise ContractError(
                "official_native_forward_registry_scope_missing:"
                f"{station_id}:{event_date}"
            )
        timezones = {
            str(row.get("settlement_timezone") or "").strip()
            for row in matching
        }
        if len(timezones) != 1 or "" in timezones:
            raise ContractError(
                "official_native_forward_registry_timezone_invalid:"
                f"{station_id}:{event_date}"
            )
        outcomes = [str(row.get("outcome") or "").strip() for row in matching]
        if not outcomes or any(not outcome for outcome in outcomes) or len(outcomes) != len(set(outcomes)):
            raise ContractError(
                "official_native_forward_registry_outcome_invalid:"
                f"{station_id}:{event_date}"
            )
        coordinates = station_coordinates(station_id)
        scopes.append({
            "station_id": station_id,
            "settlement_timezone": next(iter(timezones)),
            "latitude": float(coordinates["latitude"]),
            "longitude": float(coordinates["longitude"]),
            "registry_rows": matching,
        })
    if not scopes:
        raise ContractError("official_native_forward_station_scope_invalid")
    return scopes


def _run_coverable_scopes(
    scopes: Sequence[dict[str, Any]],
    *,
    event_date: str,
    run_init: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split scopes into those this run can still cover and those it cannot.

    Deterministic from the run time and each station's settlement timezone,
    before any download: the sealed curve may only hold 3-hourly points whose
    local date is the event date, so a run issued mid-day serves a shrinking
    window that differs by timezone.  The capture batch is all-or-nothing
    across its stations, and a same-day 06Z rung once died whole on
    ``native_family_temperature_curve_point_count_invalid`` because the
    easternmost stations had two points left -- the stations that structurally
    cannot seal are excluded here so the ones that can are not held hostage.

    The floor mirrors the archives' own seal: the same-day relief applies
    when the run is issued on the event date in UTC or after the station's
    local day has begun, exactly as the collectors choose their
    ``min_points``.
    """

    event_day = date.fromisoformat(str(event_date))
    run_start = run_init.astimezone(timezone.utc)
    covered: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for scope in scopes:
        zone = ZoneInfo(str(scope["settlement_timezone"]))
        day_start = datetime.combine(
            event_day, dt_time(0), tzinfo=zone
        ).astimezone(timezone.utc)
        day_end = datetime.combine(
            event_day + timedelta(days=1), dt_time(0), tzinfo=zone
        ).astimezone(timezone.utc)
        # "Same day" is the station's day, not the UTC date.  A d1 18Z run
        # is issued at 03:00 Tokyo time on the event day: it cannot describe
        # the hours before it any more than a same-day 06Z run can, and the
        # archives' seal accepts a shorter curve for exactly that reason.
        # Judged by UTC date it counted as a run "preceding the day" and was
        # held to the full-day bar, which every station east of UTC+5 fails
        # -- so the d1 18Z rung failed whole, for the whole fleet, on every
        # night: ``ecmwf_archive_local_day_grid_incomplete`` on 2026-09-02
        # 18Z while the 12Z rung behind it captured 45 stations.  The UTC
        # test is kept alongside, so nothing the old rule admitted is lost.
        same_day = run_start.date() == event_day or run_start >= day_start
        floor = MIN_SAME_DAY_CURVE_POINTS if same_day else MIN_CURVE_POINTS
        # Count what the archives will actually seal: 3-hourly period ends
        # from the first step after the run, never the run instant itself.
        step = run_start + timedelta(hours=3)
        while step < day_start:
            step += timedelta(hours=3)
        points = 0
        while step < day_end:
            points += 1
            step += timedelta(hours=3)
        if points >= floor:
            covered.append(dict(scope))
        else:
            skipped.append({
                "station_id": str(scope["station_id"]),
                "settlement_timezone": str(scope["settlement_timezone"]),
                "reason": "run_cannot_cover_station_day",
                "coverable_point_count": points,
                "required_point_count": floor,
            })
    return covered, skipped


def _verify_official_inbox(
    path: Path,
    *,
    event_date: str,
    station_ids: Sequence[str],
    require_eccc: bool = False,
) -> dict[str, Any]:
    inbox = load_bounded_json(path)
    identity = {key: value for key, value in inbox.items() if key != "content_sha256"}
    packets = inbox.get("packets")
    if (
        inbox.get("schema") != "native_ensemble_forward_inbox_v1"
        or not isinstance(packets, list)
        or str(inbox.get("content_sha256") or "") != content_sha256(identity)
    ):
        raise ContractError("official_native_forward_inbox_invalid")
    scopes = {
        (str(packet.get("station_id") or "").strip().upper(), str(packet.get("event_date") or ""))
        for packet in packets
        if isinstance(packet, dict)
    }
    expected = {(station, event_date) for station in station_ids}
    if scopes != expected:
        raise ContractError("official_native_forward_inbox_scope_mismatch")
    for packet in packets:
        if not isinstance(packet, dict) or packet.get("schema") != "official_native_ensemble_forward_packet_v1":
            raise ContractError("official_native_forward_inbox_packet_schema_invalid")
        families = {
            str(system.get("independence_family_id") or "")
            for system in packet.get("systems") or []
            if isinstance(system, dict)
        }
        if not {"ECMWF", "NOAA_GEFS"}.issubset(families):
            raise ContractError("official_native_forward_inbox_cross_family_missing")
        if require_eccc and "ECCC_GEM" not in families:
            raise ContractError("official_native_forward_inbox_eccc_missing")
    return inbox


def capture_official_native_forward(
    *,
    registry_path: Path,
    event_date: str,
    gefs_output: Path,
    ecmwf_output: Path,
    inbox_output: Path,
    timeout_seconds: float,
    station_ids: Sequence[str] | None = None,
    skip_unlisted_stations: bool = False,
    eccc_geps_archive: Path | None = None,
    eccc_station_ids: Sequence[str] | None = None,
    eccc_output: Path | None = None,
    icon_output: Path | None = None,
    # P0 follows the freshest run issued for the event date, so the caller
    # states which lead this pass is for.  Two is the historical default.
    lead_days: int = 2,
    run_cycle_hour: int | None = None,
) -> dict[str, Any]:
    """Capture and materialize a formally admissible official-native inbox."""

    if not MIN_TIMEOUT_SECONDS <= float(timeout_seconds) <= MAX_TIMEOUT_SECONDS:
        raise ContractError("official_native_forward_timeout_invalid")
    registry = load_market_registry_snapshot(registry_path.expanduser().resolve(strict=True))
    scopes = build_station_scopes(
        registry,
        event_date=event_date,
        station_ids=station_ids,
        skip_unlisted_stations=skip_unlisted_stations,
    )
    # Coverability is judged against the caller's cycle.  ECCC and ICON
    # follow their own per-lead contracts instead: at same-day leads those
    # resolve 00Z, never later than the caller's cycle, and at d1/d2 their
    # run precedes every station's local day outright -- so a station the
    # caller's run can cover is coverable by every family in the batch.
    caller_run_init = datetime.combine(
        date.fromisoformat(str(event_date)) - timedelta(days=int(lead_days)),
        dt_time(hour=gefs_resolve_run_cycle_hour(int(lead_days), run_cycle_hour)),
        tzinfo=timezone.utc,
    )
    scopes, skipped_uncoverable = _run_coverable_scopes(
        scopes, event_date=event_date, run_init=caller_run_init
    )
    if not scopes:
        raise ContractError("official_native_forward_no_coverable_station")
    capture_at = _utc_now()
    requested_station_ids = [str(scope["station_id"]) for scope in scopes]
    gefs_path = gefs_output.expanduser().resolve()
    ecmwf_path = ecmwf_output.expanduser().resolve()

    # Each family's expected run for this pass, stated up front so reuse can be
    # decided by run identity.  ECMWF and GEFS follow the caller's cycle; ECCC
    # and ICON follow their own per-lead contracts and ignore the caller's
    # cycle, so their expected run is computed from their own mapping.
    def _expected_run(cycle_hour: int) -> str:
        return (
            datetime.combine(
                date.fromisoformat(str(event_date))
                - timedelta(days=int(lead_days)),
                dt_time(hour=int(cycle_hour)),
                tzinfo=timezone.utc,
            ).isoformat().replace("+00:00", "Z")
        )

    caller_cycle_run = _expected_run(
        gefs_resolve_run_cycle_hour(int(lead_days), run_cycle_hour)
    )
    gefs = _load_retained_archive(
        gefs_path,
        event_date=event_date,
        station_ids=requested_station_ids,
        expected_schema="noaa_gefs_official_historical_multistation_members_v2",
        as_of_utc=capture_at,
        max_reuse_age_seconds=FORWARD_REUSE_TTL_SECONDS,
        expected_run_init_utc=caller_cycle_run,
    )
    ecmwf = _load_retained_archive(
        ecmwf_path,
        event_date=event_date,
        station_ids=requested_station_ids,
        expected_schema="ecmwf_ifs_open_pf50_historical_multistation_members_v1",
        as_of_utc=capture_at,
        max_reuse_age_seconds=FORWARD_REUSE_TTL_SECONDS,
        expected_run_init_utc=_expected_run(
            ecmwf_resolve_run_cycle_hour(int(lead_days), run_cycle_hour)
        ),
    )
    # ECCC and ICON are captured actively here, not merely reused.  Leaving
    # them to a "supply an archive if you happen to have one" parameter meant
    # their history never grew: ECCC stalled at 2026-07-30 and ICON had none at
    # all, so neither could ever satisfy the exact holdout cohort that admits a
    # system to the cross-ensemble candidate set.  With only ECMWF and GEFS
    # qualifying, per-station model selection had nothing to choose between.
    eccc = None
    if eccc_output is not None:
        eccc = _load_retained_archive(
            eccc_output.expanduser().resolve(),
            event_date=event_date,
            station_ids=requested_station_ids,
            expected_schema=(
                "eccc_geps_official_historical_multistation_members_v1"
            ),
            as_of_utc=capture_at,
            max_reuse_age_seconds=FORWARD_REUSE_TTL_SECONDS,
            expected_run_init_utc=_expected_run(
                ECCC_RUN_CYCLE_HOUR_BY_LEAD[int(lead_days)]
            ),
        )
    icon = None
    if icon_output is not None:
        icon = _load_retained_archive(
            icon_output.expanduser().resolve(),
            event_date=event_date,
            station_ids=requested_station_ids,
            expected_schema=(
                "dwd_icon_eps_official_historical_multistation_members_v1"
            ),
            as_of_utc=capture_at,
            max_reuse_age_seconds=FORWARD_REUSE_TTL_SECONDS,
            expected_run_init_utc=_expected_run(
                ICON_RUN_CYCLE_HOUR_BY_LEAD[int(lead_days)]
            ),
        )
    reused_gefs = gefs is not None
    reused_ecmwf = ecmwf is not None
    reused_eccc = eccc is not None
    reused_icon = icon is not None
    # Charter 148 (2026-09-18).  A family that cannot serve this rung holds
    # "the run it already has" -- but it looked for that run only in this
    # lead's own file, and on the first same-day rung of an event date that
    # file does not exist yet for the family still uploading.  ECMWF's 00Z
    # lands about seven hours after init, GEFS's about four and ICON's about
    # three and a half, so from ~04Z every d0 pass banked GEFS (and ICON),
    # found no d0 ECMWF to hold, died on the required-family check, and the
    # ladder walked down to the d1 rung that re-served yesterday's 18Z GEFS.
    # 2026-09-18: GEFS 00Z sat banked at 04:13Z while P0 kept serving 18Z
    # until the whole 00Z set landed together around 08:30Z.  The run the
    # family is actually serving lives in another lead's file for the same
    # event date, so it is held from there -- read in place, never copied
    # into this lead's file and never re-ingested as this lead's history.
    # Only when this lead brought something of its own: a pass in which
    # every family would be held from another lead is the pass the ladder
    # already walks past, and it still fails as before.
    held_from_other_lead: dict[str, Path] = {}
    own_lead_families: set[str] = {
        name
        for name, value in (
            ("NOAA_GEFS_PDS_S3", gefs),
            ("ECMWF_OPEN_DATA_S3", ecmwf),
            ("ECCC_DD_WEATHER", eccc),
            ("DWD_OPENDATA_ICON_EPS", icon),
        )
        if value is not None
    }

    def _hold_from_other_lead(
        family: str,
        path: Path,
        *,
        expected_schema: str,
    ) -> dict[str, Any] | None:
        best: tuple[datetime, dict[str, Any], Path] | None = None
        for other in _other_lead_archive_paths(path):
            candidate = _load_retained_archive(
                other,
                event_date=event_date,
                station_ids=requested_station_ids,
                expected_schema=expected_schema,
            )
            if candidate is None:
                continue
            run = _parse_utc_timestamp(candidate.get("run_init_utc"))
            if run is None:
                continue
            if best is None or run > best[0]:
                best = (run, candidate, other)
        if best is None:
            return None
        held_from_other_lead[family] = best[2]
        return best[1]
    # A rung is a question asked of four publishers who neither serve the same
    # cycles nor finish uploading at the same hour.  Measured against a
    # completed day (2026-08-15): ECMWF 0p25 ENS and DWD ICON-EPS answer all
    # four cycles, GEFS likewise, and ECCC GEPS answers 00Z and 12Z only.  On
    # top of that sits publication delay -- ECMWF's 06Z index was still 404 at
    # 13:05Z and served at 13:35Z, about seven and a half hours after init.
    #
    # So at any moment a rung has landed for some families and not others, and
    # aborting the pass on the first refusal threw away every family that had
    # landed.  The ensemble could then only advance when its slowest member
    # did, which is what "the models are stuck twelve hours ago" was.  Let
    # each family answer for itself: one that cannot serve this rung -- not
    # published, or not yet uploaded -- keeps the run it already holds, which
    # is what it would have been serving had the rung never been offered, and
    # the packet becomes per-system, which this lane already speaks.  A family
    # that is merely late is picked up by a later pass on the same rung.
    family_capture_errors: dict[str, str] = {}

    def _advance_or_hold(
        family: str,
        path: Path,
        *,
        expected_schema: str,
        capture: Callable[[], dict[str, Any]],
    ) -> dict[str, Any] | None:
        try:
            assert_process_memory_budget()
            captured = capture()
            own_lead_families.add(family)
            return captured
        except Exception as exc:  # noqa: BLE001 - held, not lost
            family_capture_errors[family] = f"{type(exc).__name__}:{exc}"[:200]
            # Any run it holds, not the one this rung wanted: the point is to
            # keep serving the freshest run this family actually has.
            held = _load_retained_archive(
                path,
                event_date=event_date,
                station_ids=requested_station_ids,
                expected_schema=expected_schema,
            )
            if held is not None:
                own_lead_families.add(family)
                return held
            return _hold_from_other_lead(
                family, path, expected_schema=expected_schema
            )

    if gefs is None:
        gefs = _advance_or_hold(
            "NOAA_GEFS_PDS_S3",
            gefs_path,
            expected_schema=(
                "noaa_gefs_official_historical_multistation_members_v2"
            ),
            capture=lambda: capture_gefs_station_days(
                event_date=event_date,
                station_scopes=scopes,
                archive_query_at_utc=capture_at,
                timeout_seconds=float(timeout_seconds),
                lead_days=int(lead_days),
                run_cycle_hour=run_cycle_hour,
            ),
        )
    # Bank each family the moment it is in hand.  The archives used to be
    # written only after the last family, so a pass killed at the caller's
    # deadline left nothing behind and the retry started from zero: the
    # 2026-09-03 d1 pass reached GEFS 2h23m after the other three, on a
    # second attempt, and no 2026-09-04 d2 pass ever finished at all.  A
    # banked family is what the retry reuses (``_load_retained_archive``
    # above), so a long pass now converges across attempts instead of
    # repeating itself.
    if (
        gefs is not None
        and not reused_gefs
        and "NOAA_GEFS_PDS_S3" not in held_from_other_lead
    ):
        write_bounded_json_if_changed(gefs_path, gefs)
    if ecmwf is None:
        ecmwf = _advance_or_hold(
            "ECMWF_OPEN_DATA_S3",
            ecmwf_path,
            expected_schema=(
                "ecmwf_ifs_open_pf50_historical_multistation_members_v1"
            ),
            capture=lambda: capture_ecmwf_station_days(
                event_date=event_date,
                station_scopes=scopes,
                archive_query_at_utc=capture_at,
                timeout_seconds=float(timeout_seconds),
                lead_days=int(lead_days),
                run_cycle_hour=run_cycle_hour,
            ),
        )
    if (
        ecmwf is not None
        and not reused_ecmwf
        and "ECMWF_OPEN_DATA_S3" not in held_from_other_lead
    ):
        write_bounded_json_if_changed(ecmwf_path, ecmwf)
    # Holding is only a fallback for a family that has something to hold.  With
    # nothing on disk there is no ensemble to build, and failing here is what
    # keeps that distinct from a rung this publisher simply does not serve.
    if gefs is None or ecmwf is None:
        # Name the cause, not just the family.  The worker's ladder walks
        # rungs on this exit code and only ever sees the last 200 bytes of
        # stderr, so a rung refused before any download -- a contract check,
        # an unpublished cycle, a proxy fault -- read as the same
        # "unavailable" as a network failure, and the 18Z rung of 2026-09-03
        # failed silently for a day while the 12Z one behind it succeeded.
        raise ContractError(
            "official_native_forward_required_family_unavailable:"
            + ",".join(sorted(
                name
                for name, value in (
                    ("NOAA_GEFS_PDS_S3", gefs),
                    ("ECMWF_OPEN_DATA_S3", ecmwf),
                )
                if value is None
            ))
            + ":"
            + ";".join(
                f"{name}={family_capture_errors.get(name, '')[:160]}"
                for name in ("NOAA_GEFS_PDS_S3", "ECMWF_OPEN_DATA_S3")
                if name in family_capture_errors
            )
        )
    if eccc_output is not None and eccc is None:
        # Held like the two required families, not fatal like nothing else
        # is.  ECCC reads its own cycle for the lead -- 12Z at d2 -- and its
        # 12Z run uploads around 17:10Z, some ten hours after ECMWF's and
        # GEFS's 00Z runs are readable.  An unhandled refusal here killed the
        # whole pass after both of those had already been banked, so no d2
        # rung could produce a packet before ECCC did: measured 2026-09-05,
        # every rung for 2026-09-07 died on ``2026090512_P0xx ... 404`` from
        # 00Z until the afternoon, the worker throttled each death for thirty
        # minutes, and the date had no P0 until the evening.  A family that
        # cannot serve yet keeps whatever run it holds (nothing, at d2) and
        # the packet goes out as the ECMWF+GEFS pair this lane already
        # speaks; the stale re-read or the next rung picks ECCC up once it
        # is published, and the receipt names the hold either way.
        eccc = _advance_or_hold(
            "ECCC_DD_WEATHER",
            eccc_output.expanduser().resolve(),
            expected_schema=(
                "eccc_geps_official_historical_multistation_members_v1"
            ),
            capture=lambda: capture_geps_station_days(
                event_date=event_date,
                station_scopes=scopes,
                archive_query_at_utc=capture_at,
                timeout_seconds=float(timeout_seconds),
                lead_days=int(lead_days),
                # ECCC's own cycle for this lead, not the caller's.
                # ``run_cycle_hour`` carries the ECMWF/GEFS ladder's answer
                # -- 18Z at d2 -- which ECCC does not publish, and passing
                # nothing silently took 00Z at every lead.  That is how this
                # family's d2 history accumulated for weeks in a lead-hour
                # bucket no other family occupies.
                run_cycle_hour=ECCC_RUN_CYCLE_HOUR_BY_LEAD[int(lead_days)],
            ),
        )
        if eccc is not None and "ECCC_DD_WEATHER" not in held_from_other_lead:
            write_bounded_json_if_changed(
                eccc_output.expanduser().resolve(), eccc
            )
    icon_capture_error = ""
    if icon_output is not None and icon is None:
        assert_process_memory_budget()
        try:
            icon = capture_icon_eps_station_days(
                event_date=event_date,
                station_scopes=scopes,
                archive_query_at_utc=capture_at,
                # ICON does not read byte ranges.  DWD publishes one object per
                # lead holding all forty members, so every fetch is a whole
                # ~36MiB stream where the others take sub-megabyte slices, and
                # the caller's timeout is sized for those slices.  Applying it
                # here made ICON the family that fails on a merely slow link --
                # observed as ``ReadTimeout`` on a pass where the other three
                # reused their retained archives without touching the network.
                timeout_seconds=ICON_OBJECT_TIMEOUT_SECONDS,
                lead_days=int(lead_days),
            )
            own_lead_families.add("DWD_OPENDATA_ICON_EPS")
        except Exception as exc:  # noqa: BLE001 - one optional family
            # ICON is the one family with no backfill: DWD keeps only the
            # newest run, so a lane that is down now is down forever for this
            # day.  That argues for capturing it on every pass, which in turn
            # means its failure must not take ECMWF and GEFS with it -- those
            # two are what the ensemble actually requires, and losing a whole
            # capture because an optional family timed out would trade a
            # recoverable gap for an unrecoverable one.  Record and continue.
            icon = None
            icon_capture_error = f"{type(exc).__name__}:{exc}"[:200]
            # Named with the families that held, under its transport id.  The
            # worker copies only ``family_held_on_earlier_run`` off this
            # receipt, so an error left in ``icon_capture`` alone never left
            # the subprocess: the 2026-09-14 d0 pass lost ICON at 08:43Z and
            # nothing anywhere said why.  It is also the worker's cue to ask
            # again before DWD overwrites the run.
            family_capture_errors["DWD_OPENDATA_ICON_EPS"] = icon_capture_error
            # Charter 148: held like the other three -- this lead's own file
            # first, then the run it is serving from another lead's -- rather
            # than dropped from the packet, which took the ensemble's
            # heaviest member out of P0 for as long as DWD was slow.
            icon_schema = "dwd_icon_eps_official_historical_multistation_members_v1"
            icon = _load_retained_archive(
                icon_output.expanduser().resolve(),
                event_date=event_date,
                station_ids=requested_station_ids,
                expected_schema=icon_schema,
            )
            if icon is not None:
                own_lead_families.add("DWD_OPENDATA_ICON_EPS")
            else:
                icon = _hold_from_other_lead(
                    "DWD_OPENDATA_ICON_EPS",
                    icon_output.expanduser().resolve(),
                    expected_schema=icon_schema,
                )
    if held_from_other_lead and not own_lead_families:
        # Nothing at this lead is this lead's own: the pass would restate
        # another lead's packet under this rung's name.  Fail as the
        # required-family check always did, so the ladder walks down exactly
        # as before.
        raise ContractError(
            "official_native_forward_required_family_unavailable:"
            "nothing_new_at_this_lead:"
            + ",".join(sorted(held_from_other_lead))
        )
    assert_process_memory_budget()
    inbox_path = inbox_output.expanduser().resolve()
    # Charter 148: a family held from another lead's file stays in that file.
    wrote_gefs = (
        write_bounded_json_if_changed(gefs_path, gefs)
        if "NOAA_GEFS_PDS_S3" not in held_from_other_lead
        else False
    )
    wrote_ecmwf = (
        write_bounded_json_if_changed(ecmwf_path, ecmwf)
        if "ECMWF_OPEN_DATA_S3" not in held_from_other_lead
        else False
    )
    wrote_eccc = (
        write_bounded_json_if_changed(
            eccc_output.expanduser().resolve(), eccc
        )
        if eccc_output is not None
        and eccc is not None
        and "ECCC_DD_WEATHER" not in held_from_other_lead
        else False
    )
    wrote_icon = (
        write_bounded_json_if_changed(
            icon_output.expanduser().resolve(), icon
        )
        if icon_output is not None
        and icon is not None
        and "DWD_OPENDATA_ICON_EPS" not in held_from_other_lead
        else False
    )
    # Charter 95 (re-ruled 2026-09-06): the regional models are P1 evidence,
    # not P0 members; their runs are projected into the packet only if the
    # stage is ever moved back to P0.
    from bayes_basket_target.official_native_forward_packet import (  # noqa: PLC0415
        REGIONAL_MODELS_STAGE,
    )
    regional_point = (
        _capture_regional_point_archives(
            event_date=event_date,
            scopes=scopes,
            capture_at=capture_at,
            lead_days=int(lead_days),
        )
        if REGIONAL_MODELS_STAGE == "P0"
        else {"archives_by_station": {}}
    )
    regional_point_path = inbox_path.parent / (
        f"official_regional_point_{event_date}_d{int(lead_days)}.json"
    )
    wrote_regional_point = (
        write_bounded_json_if_changed(regional_point_path, regional_point)
        if regional_point["archives_by_station"]
        else False
    )
    command = [
        sys.executable,
        str(ROOT / "scripts" / "build_official_native_forward_inbox.py"),
        "--archive", str(held_from_other_lead.get("NOAA_GEFS_PDS_S3", gefs_path)),
        "--archive", str(held_from_other_lead.get("ECMWF_OPEN_DATA_S3", ecmwf_path)),
        "--market-registry", str(registry_path.expanduser().resolve()),
        "--output", str(inbox_path),
    ]
    if regional_point["archives_by_station"]:
        command.extend(["--regional-point-archive", str(regional_point_path)])
    # An actively captured ECCC archive is preferred over a supplied one; both
    # carry the same GEPS native 00Z lineage, so the builder treats them alike.
    resolved_eccc = (
        held_from_other_lead.get(
            "ECCC_DD_WEATHER", eccc_output.expanduser().resolve()
        )
        if eccc_output is not None and eccc is not None
        else (
            eccc_geps_archive.expanduser().resolve(strict=True)
            if eccc_geps_archive is not None
            else None
        )
    )
    if resolved_eccc is not None:
        command.extend(["--eccc-geps-archive", str(resolved_eccc)])
        for station_id in eccc_station_ids or ():
            command.extend(["--eccc-station-id", str(station_id)])
    if icon_output is not None and icon is not None:
        command.extend(["--archive", str(held_from_other_lead.get(
            "DWD_OPENDATA_ICON_EPS", icon_output.expanduser().resolve()
        ))])
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    inbox = _verify_official_inbox(
        inbox_path,
        event_date=event_date,
        station_ids=[str(scope["station_id"]) for scope in scopes],
        require_eccc=eccc_geps_archive is not None,
    )
    return {
        "schema": "official_native_ensemble_forward_capture_v1",
        "state": "COMMITTED_NO_LIVE",
        "event_date": event_date,
        "captured_at_utc": capture_at,
        "station_count": len(scopes),
        "skipped_uncoverable_stations": skipped_uncoverable,
        "archive_paths": {
            "NOAA_GEFS_PDS_S3": str(gefs_path),
            "ECMWF_OPEN_DATA_S3": str(ecmwf_path),
            **({
                "ECCC_DD_WEATHER": str(
                    eccc_geps_archive.expanduser().resolve(strict=True)
                )
            } if eccc_geps_archive is not None else {}),
        },
        "archive_wrote": {
            "NOAA_GEFS_PDS_S3": wrote_gefs,
            "ECMWF_OPEN_DATA_S3": wrote_ecmwf,
            **({"ECCC_DD_WEATHER": False}
               if eccc_geps_archive is not None else {}),
        },
        "archive_reused_retained": {
            "NOAA_GEFS_PDS_S3": reused_gefs,
            "ECMWF_OPEN_DATA_S3": reused_ecmwf,
            **({"ECCC_DD_WEATHER": reused_eccc}
               if eccc_output is not None else {}),
        },
        # Which families could not serve this rung and are therefore still on
        # an older run.  A pass that advanced two of four is the ordinary
        # outcome now, and without this the receipt would look identical to
        # one that advanced all four.
        "family_held_on_earlier_run": dict(
            sorted(family_capture_errors.items())
        ),
        # Charter 148: families this rung could not advance and held at the
        # run another lead's archive for the same event date is serving.
        "family_held_from_other_lead": {
            family: path.name
            for family, path in sorted(held_from_other_lead.items())
        },
        "run_init_utc_by_family": {
            "NOAA_GEFS_PDS_S3": str(gefs.get("run_init_utc") or ""),
            "ECMWF_OPEN_DATA_S3": str(ecmwf.get("run_init_utc") or ""),
            **({"ECCC_DD_WEATHER": str(eccc.get("run_init_utc") or "")}
               if eccc is not None else {}),
            **({"DWD_ICON_EPS": str(icon.get("run_init_utc") or "")}
               if icon is not None else {}),
        },
        # ICON carried no instrumentation at all, which is the wrong way round:
        # it is the family whose misses are permanent, so it is the one whose
        # outcome most needs to be visible on every pass.
        **({
            "icon_capture": {
                "requested": True,
                "wrote": wrote_icon,
                "reused_retained": reused_icon,
                "error": icon_capture_error,
            }
        } if icon_output is not None else {}),
        "inbox_path": str(inbox_path),
        "inbox_content_sha256": str(inbox["content_sha256"]),
        "inbox_packet_count": len(inbox["packets"]),
        "inbox_builder_stdout": completed.stdout.strip(),
        "production_no_live": True,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--lead-days", type=int, default=2, choices=[0, 1, 2])
    parser.add_argument(
        "--run-cycle-hour",
        type=int,
        default=None,
        choices=[0, 6, 12, 18],
        help=(
            "Read this cycle instead of the lead's default.  ECMWF and GEFS "
            "publish all four; ECCC and ICON take their own cycle for the "
            "lead regardless of this value.  A cycle that does not cover a "
            "station's local day is still rejected by the coverage check."
        ),
    )
    parser.add_argument("--market-registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--gefs-output", type=Path, default=DEFAULT_GEFS_OUTPUT)
    parser.add_argument("--ecmwf-output", type=Path, default=DEFAULT_ECMWF_OUTPUT)
    parser.add_argument("--inbox-output", type=Path, default=DEFAULT_INBOX_OUTPUT)
    parser.add_argument(
        "--eccc-output",
        type=Path,
        help=(
            "Actively capture official ECCC GEPS into this path instead of "
            "only reusing a supplied archive."
        ),
    )
    parser.add_argument(
        "--icon-output",
        type=Path,
        help=(
            "Actively capture official DWD ICON Global EPS into this path. "
            "DWD retains only the newest run, so this is forward-only: the "
            "run captured today becomes a d2 candidate two days later."
        ),
    )
    parser.add_argument("--station-id", action="append", default=[])
    parser.add_argument(
        "--registry-scope-stations",
        action="store_true",
        help=(
            "Capture every station the registry lists for this event date "
            "instead of a named subset, skipping canonical stations whose "
            "market has not opened for the day.  One model run is downloaded "
            "once and read for all of them, so this costs about twice a "
            "single-station pass rather than forty-five times it."
        ),
    )
    parser.add_argument("--timeout-seconds", type=float, default=45.0)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument(
        "--commit",
        action="store_true",
        help=(
            "Also ingest the captured archives into the historical member "
            "table.  Without this the capture only feeds the forward inbox, "
            "and the training archive's high-water mark stays wherever the "
            "last manual backfill left it: every system except ICON froze at "
            "2026-08-07 that way, because ICON's daily lane was the only one "
            "that committed what it captured."
        ),
    )
    parser.add_argument(
        "--eccc-geps-archive",
        type=Path,
        help=(
            "Optional existing official ECCC GEPS forward archive; reuse it "
            "without a second capture."
        ),
    )
    parser.add_argument(
        "--eccc-station-id",
        action="append",
        default=[],
        help=(
            "Restrict reused ECCC GEPS to these stations; other stations "
            "remain ECMWF+GEFS pair packets."
        ),
    )
    args = parser.parse_args(argv)
    try:
        result = capture_official_native_forward(
            registry_path=args.market_registry,
            event_date=str(args.event_date),
            gefs_output=args.gefs_output,
            ecmwf_output=args.ecmwf_output,
            inbox_output=args.inbox_output,
            timeout_seconds=float(args.timeout_seconds),
            station_ids=args.station_id or None,
            skip_unlisted_stations=bool(args.registry_scope_stations),
            eccc_geps_archive=args.eccc_geps_archive,
            eccc_output=args.eccc_output,
            icon_output=args.icon_output,
            lead_days=args.lead_days,
            run_cycle_hour=args.run_cycle_hour,
            eccc_station_ids=args.eccc_station_id,
        )
    except (ContractError, OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        print(json.dumps({
            "schema": "official_native_ensemble_forward_capture_v1",
            "state": "BLOCKED_FAIL_CLOSED",
            "reason": f"{type(exc).__name__}:{exc}",
            "production_no_live": True,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "live_submit_authorized": False,
        }, ensure_ascii=False, sort_keys=True))
        return 2
    if args.commit:
        # The capture stays DB-free; the written archives are re-read and
        # ingested here so the training history advances with every capture
        # instead of waiting for someone to run a backfill.  Ingest is a
        # frozen-fact replay: rows already present are verified, not repeated,
        # and a conflicting re-read fails closed.
        # ICON must be in this list: DWD keeps no archive, so capture time is
        # the only moment its training history can be written at all.
        ingest_by_path = [
            (args.gefs_output, ingest_gefs_station_days),
            (args.ecmwf_output, ingest_ecmwf_station_days),
            (args.eccc_output, ingest_geps_station_days),
            (args.icon_output, ingest_icon_eps_station_days),
        ]
        database = args.db.expanduser().resolve(strict=True)
        ingress: dict[str, Any] = {}
        # Writing the training history is a second act, not part of the
        # capture.  Left unguarded it decided the exit code for both: on
        # 2026-08-17 the target SSOT crossed its 12GiB ceiling, every ingest
        # raised ``target_database_bytes_exceeded``, and the process died with
        # a bare traceback -- so the caller read a rung that had in fact
        # downloaded all four families and built its inbox as a total failure,
        # never merged it, and the served runs froze at the previous day while
        # fresh ones sat unread on disk.  A database that cannot accept
        # history must not also stop the forecast that was already captured.
        ingress_blocked = ""
        try:
            with sqlite3.connect(database, timeout=60.0) as conn:
                conn.execute("PRAGMA busy_timeout=60000")
                assert_connection_target_capacity(
                    conn, recover_oversized_wal=True
                )
                for path, ingest in ingest_by_path:
                    if path is None:
                        continue
                    resolved = path.expanduser().resolve()
                    if not resolved.is_file():
                        continue
                    archive = load_bounded_json(resolved)
                    # A retained file can hold an older event's capture when
                    # this event reused nothing; ingesting it would be harmless
                    # replay, but skipping keeps each receipt about this event
                    # only.
                    if archive.get("event_date") != str(args.event_date):
                        continue
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        outcome = ingest(conn, archive)
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                    ingress[str(archive.get("ensemble_system_id"))] = {
                        "state": outcome["state"],
                        "station_count": outcome["station_count"],
                        "member_run_wrote_count": outcome[
                            "member_run_wrote_count"
                        ],
                    }
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            ingress_blocked = f"{type(exc).__name__}:{exc}"[:200]
        result = {
            **result,
            "historical_ingress": ingress,
            # Named on every capture, empty when nothing was refused, so a
            # reader never has to infer the difference between "ingested" and
            # "could not ingest" from a missing key.
            "historical_ingress_blocked_reason": ingress_blocked,
            "historical_ingress_state": (
                "BLOCKED_FAIL_CLOSED" if ingress_blocked else "COMMITTED"
            ),
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
