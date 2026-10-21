#!/usr/bin/env python3
"""Materialize every settled station-day that still has no truth row.

The observations were never the problem.  On 2026-08-17 the truth table held
13 of 45 stations for 2026-08-11, 28 for 08-12, 25 for 08-13 -- while
``data/archive.db`` held all 45 stations on every one of those days, about
1650 METAR reports apiece.  A read-only replay of forty of the missing scopes
reached the write on all forty: nothing refused them, nothing was missing.
Nobody had asked.

The resident does materialize truth, and since 2026-08-10 it rotates rather
than serving the same alphabetical prefix forever.  What it rotates over is
the wrong set.  Its candidate query reads the two *candidate member run*
tables, and those carry only the days the no-live republisher lane wrote --
55 station-days as of this writing, nearly all of them today and yesterday.
The days the calibration lane actually needs truth for live in the historical
member table and in the market registry, and no rotation over the candidate
tables will ever reach them.  A day missed is not retried by anything.

That costs more than a gap on a page.  Every fit downstream is gated on
settled days: the forward-accumulation window needs five training days and a
three-day shared holdout, so a hole does not delay one scope by one day, it
delays every cohort that would have shared that day.  ICON pays the most --
it is the one family whose archive cannot be backfilled, so a truth hole
inside its short history is a day it can never buy back, and it has been
excluded from every P0 for want of eight settled days it should already have.

So: discovery from the market registry, which is the writer's own
precondition rather than a downstream lane's leftovers; the production writer
per scope, which is append-only and returns ``idempotent`` on a day already
done; and a receipt naming what refused and whether the refusal was about
availability or about trust.

Safe beside the resident.  The scan is all reads, which in WAL mode do not
block a writer, and the write transaction opens at the first INSERT and
closes one scope later -- milliseconds, not the length of the sweep.  That
matters because the resident deliberately gives up on a busy database after
five seconds rather than stall a production cadence, so a maintenance writer
that held the lock across its own scan would not slow the resident down, it
would fail-close P0 and P1 for that whole cycle.

    python3 scripts/settlement_truth_sweep.py            # sweep and commit
    python3 scripts/settlement_truth_sweep.py --dry-run  # classify only
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL  # noqa: E402
from bayes_basket_target.pass_deadline import (  # noqa: E402
    HOSTED_SWEEP_CONTINUE_MARKER,
    writer_lease_seconds,
)
from bayes_basket_target.settlement_truth import (  # noqa: E402
    CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1,
    blocker_class,
    materialize_settled_station_truth,
    registry_scope_still_eligible,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
DEFAULT_CANONICAL = ROOT / "data" / "archive.db"
STATUS_JSON = ROOT / "data" / "settlement_truth_sweep_status.json"
SCHEMA = "settlement_truth_sweep_v1"
# How far back a pass looks.  Deliberately wider than the calibration window
# it feeds, because a day that falls out of the window is a day nothing will
# ever retry -- and unlike a model archive, truth cannot be re-derived later
# from anything but these same observations.
DEFAULT_WINDOW_DAYS = 45
# A pass is bounded so it cannot run long enough to matter to the resident
# even in the worst case.  The backlog drains over consecutive passes; the
# oldest gap is always served first, so nothing starves.
DEFAULT_MAX_SCOPES = 400
# Plain-language names for the refusals worth reading on a page.  Anything
# unlisted is reported by its raw code rather than guessed at.
REASON_LABELS = {
    "settlement_truth_hourly_observation_missing": "该日没有合格的整点报文",
    "settlement_truth_canonical_ssot_fact_missing": "上游观测事实还没入库",
    "settlement_truth_scope_missing": "站点或日期标识缺失",
    "settlement_truth_corrections_unresolved": "有疑似更正的报文等人工审核",
    "settlement_truth_formal_source_lane_must_be_awc": "该日观测不走 AWC 道",
    "settlement_truth_registry_scope_missing": "当天没有市场清单行",
    "settlement_truth_observation_cap_exceeded": "报文条数超过单日上限",
    "settlement_truth_hourly_revision_ambiguous": "同一整点有两份互不相同的报文",
    "settlement_truth_source_timestamp_order_invalid": "报文的时间戳先后矛盾",
    "settlement_truth_daytime_coverage_insufficient": "白天时段的报文不足以定最高温",
    "settlement_truth_correction_requires_manual_review": "更正报文等人工裁定",
}
# How far back AWC will still answer for.  Past it the endpoint returns 400 for
# the date, so a missing report is missing for good and no amount of sweeping
# or backfilling will produce one -- measured 2026-09-02: 08-04 answered with
# 120 reports, 07-30 answered 400.
#
# The page needs this because "the evidence is incomplete" and "the evidence
# will fill in" are not the same claim.  Every availability blocker used to be
# reported as the second, which sends a reader to wait for a backfill that
# cannot happen: the nine 2026-07-30 Asian scopes predate their own AWC capture
# by a day and have been outside the retention window since late August.
AWC_RETENTION_DAYS = 30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _reason_of(error: Exception) -> str:
    return str(error).split(":", 1)[0].strip() or "unknown"


def _backlog(
    conn: sqlite3.Connection, window_start: str
) -> list[tuple[str, str]]:
    """Registry-eligible station-days with no accepted AWC truth, oldest first.

    The market registry is the right universe because it is exactly what the
    writer itself demands: no registry row means no settlement timezone and no
    scope, so a station-day outside it could not be written even if every
    observation for it were present.
    """

    candidates = [
        (str(station), str(day))
        for station, day in conn.execute(
            "SELECT DISTINCT r.station_id, r.event_date"
            " FROM basket_target_market_registry r"
            " WHERE r.source_eligible=1 AND r.event_date>=?"
            " AND NOT EXISTS ("
            "  SELECT 1 FROM calibration_station_daily_high_truth t"
            "  WHERE t.station_id=r.station_id AND t.local_date=r.event_date"
            "   AND t.source_lane IN " + FORMAL_TRUTH_LANES_SQL + " AND t.qc_status='ACCEPTED')"
            " ORDER BY r.event_date, r.station_id",
            (window_start,),
        )
    ]
    # "Was eligible once" is not "is owed a truth row".  The scan above finds
    # any row that ever said eligible, which keeps asking about a station-day
    # whose market the fleet has had no source route for ever since -- the
    # eligible rows stay in the registry, and nothing later contradicts them
    # because nothing later mentions the scope at all.  The second read costs
    # about 2.5s over a 112-scope backlog and is indexed on (station,event).
    owed = [
        scope for scope in candidates
        if registry_scope_still_eligible(
            conn, station_id=scope[0], event_date=scope[1]
        )
    ]
    # A station's days before its first market (KDEN, ruling 2026-10-21) have
    # no registry row to be found by; the allowance names them instead.
    return sorted(set(owed) | set(_prelisting_backlog(conn, window_start)), key=lambda s: (s[1], s[0]))


def _prelisting_backlog(conn: sqlite3.Connection, window_start: str) -> list[tuple[str, str]]:
    from datetime import date, timedelta  # noqa: PLC0415

    from bayes_basket_target.settlement_basis import prelisting_truth_days  # noqa: PLC0415

    out: list[tuple[str, str]] = []
    today = datetime.now(timezone.utc).date()
    for station, start, through in prelisting_truth_days():
        day = max(date.fromisoformat(start), date.fromisoformat(window_start))
        last = min(date.fromisoformat(through), today - timedelta(days=1))
        while day <= last:
            if conn.execute(
                "SELECT 1 FROM calibration_station_daily_high_truth"
                " WHERE station_id=? AND local_date=? AND source_lane IN " + FORMAL_TRUTH_LANES_SQL +
                " AND qc_status='ACCEPTED' LIMIT 1",
                (station, day.isoformat()),
            ).fetchone() is None:
                out.append((station, day.isoformat()))
            day += timedelta(days=1)
    return out


# Charter 207: a settled day whose evidence has since grown.  The resident
# settles a station-day the moment its window closes, and until 2026-09-23
# nothing looked at it again -- so a report that arrived after the close (the
# AWC outage catch-up, a late relay, an operator backfill) never reached the
# truth: 35 settled station-days inside AWC's retention were short of their
# afternoon, ten of them settled on a different bucket from the one the market
# paid.  The writer already appends a revision when its evidence changes; this
# is the pass that asks it to.
DEFAULT_MAX_REVISION_SCOPES = 200


def _parse_utc(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _second(value: datetime | None) -> datetime | None:
    # The truth row stamps whole seconds; the archive keeps milliseconds.
    return None if value is None else value.replace(microsecond=0)


def _grown_evidence(
    target: sqlite3.Connection,
    source: sqlite3.Connection,
    since: str = "",
) -> tuple[list[tuple[str, str]], int]:
    """Accepted canonical truths the archive now holds newer reports for.

    A cheap filter, not the rule: a scope it names is handed to the writer,
    which recomputes the day from scratch and answers ``idempotent`` when
    nothing that counts has changed.  The signal is the newest commit time of
    the station's AWC reports inside the settled window against the newest
    one the standing revision was built from -- a new slot and a replaced
    report both move it.

    Returns the candidates and how many of them the market registry no longer
    carries rows for; the writer re-settles those under the lineage their
    standing revision pins (``settlement_truth._standing_truth_lineage``).
    Until 2026-09-24 those were only counted -- 85 of them on the 09-24
    09:22Z pass, the eight 2026-09-09 station-days whose backfilled afternoon
    moved the maximum (KATL 30.6 -> 31.7, KLAX 33.9 -> 36.1) among them --
    and the scan stopped at AWC's 30-day retention, although what grows is
    the archive, which a backfill can reach for any day it holds.  ``since``
    is empty by default: every accepted canonical truth is read, one indexed
    archive seek each (about 4.5s over 2,800 truths).
    """

    grown: list[tuple[str, str]] = []
    past_registry = 0
    for station, day, received_at, evidence_json in target.execute(
        "SELECT t.station_id, t.local_date, t.received_at_utc, t.observation_ids_json"
        " FROM calibration_station_daily_high_truth t"
        " WHERE t.qc_status='ACCEPTED' AND t.source_role=? AND t.local_date>=?"
        "   AND t.revision_no=("
        "     SELECT MAX(x.revision_no) FROM calibration_station_daily_high_truth x"
        "     WHERE x.station_id=t.station_id AND x.local_date=t.local_date"
        "       AND x.qc_status='ACCEPTED')"
        " ORDER BY t.local_date, t.station_id",
        (CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1, since),
    ):
        try:
            window = json.loads(evidence_json).get("settlement_window") or {}
        except (TypeError, ValueError, AttributeError):
            continue
        standing = _second(_parse_utc(received_at))
        if not window.get("start_utc") or not window.get("end_utc") or standing is None:
            continue
        newest = source.execute(
            "SELECT MAX(canonical_committed_at) FROM reported_trajectory_observations"
            " WHERE station_id=? AND source_lane='awc'"
            "   AND valid_at>=? AND valid_at<?",
            (str(station), str(window["start_utc"]), str(window["end_utc"])),
        ).fetchone()[0]
        newest_at = _second(_parse_utc(newest))
        if newest_at is None or newest_at <= standing:
            continue
        # The registry keeps its rows about three event days; past that the
        # writer re-settles on the day's own standing lineage.  Counted so a
        # page can tell the two apart, never skipped.
        if target.execute(
            "SELECT 1 FROM basket_target_market_registry"
            " WHERE station_id=? AND event_date=? LIMIT 1",
            (str(station), str(day)),
        ).fetchone() is None:
            past_registry += 1
        grown.append((str(station), str(day)))
    return grown, past_registry


def _coverage(
    conn: sqlite3.Connection, window_start: str
) -> list[dict[str, Any]]:
    """Per day: how many eligible stations have truth, and how many should."""

    eligible = {
        str(day): int(count)
        for day, count in conn.execute(
            "SELECT event_date, COUNT(DISTINCT station_id)"
            " FROM basket_target_market_registry"
            " WHERE source_eligible=1 AND event_date>=?"
            " GROUP BY event_date",
            (window_start,),
        )
    }
    settled = {
        str(day): int(count)
        for day, count in conn.execute(
            "SELECT local_date, COUNT(DISTINCT station_id)"
            " FROM calibration_station_daily_high_truth"
            " WHERE qc_status='ACCEPTED' AND local_date>=?"
            " GROUP BY local_date",
            (window_start,),
        )
    }
    return [
        {
            "event_date": day,
            "eligible_stations": eligible[day],
            "truth_stations": settled.get(day, 0),
            "missing_stations": max(0, eligible[day] - settled.get(day, 0)),
        }
        for day in sorted(eligible, reverse=True)
    ]


def sweep(
    db: Path,
    canonical: Path,
    *,
    window_days: int,
    max_scopes: int,
    dry_run: bool,
    verbose: bool,
    budget_sec: float | None = None,
    max_revision_scopes: int = DEFAULT_MAX_REVISION_SCOPES,
) -> dict[str, Any]:
    started = time.time()
    # The writer lease the resident granted: stop between scopes once it is
    # spent, and say so, so the next pass is asked for at once rather than
    # in three hours.  A pass by hand has no lease and walks its whole cap.
    deadline = (
        None if budget_sec is None else time.monotonic() + float(budget_sec)
    )
    stopped_for_time = False
    window_start = str(
        (datetime.now(timezone.utc) - timedelta(days=window_days)).date()
    )
    # The target connection is opened read-only for a dry run so a classifier
    # pass cannot write by accident.  A scope that would have succeeded then
    # surfaces as "attempt to write a readonly database", which is the whole
    # diagnostic: a refusal that names a gate is a gate, and a refusal that
    # names the read-only file is a scope nobody had asked about.
    if dry_run:
        target = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=120.0)
    else:
        target = sqlite3.connect(str(db), timeout=120.0)
        target.execute("PRAGMA busy_timeout=120000")
    source = sqlite3.connect(
        f"file:{canonical}?mode=ro", uri=True, timeout=120.0
    )
    try:
        pending = _backlog(target, window_start)
        outcomes: Counter[str] = Counter()
        refusals: Counter[str] = Counter()
        refusal_class: dict[str, str] = {}
        # Per reason, how many of its scopes fall inside AWC's retention and
        # how many are past it.  A count alone cannot say whether waiting is
        # the right response, and the same reason legitimately holds both:
        # "no eligible hourly report" is a backfill away on a recent day and
        # permanent on one AWC no longer answers for.
        refusal_recoverable: Counter[str] = Counter()
        refusal_expired: Counter[str] = Counter()
        retention_start = str(
            (
                datetime.now(timezone.utc) - timedelta(days=AWC_RETENTION_DAYS)
            ).date()
        )
        examples: dict[str, str] = {}
        committed_days: dict[str, int] = defaultdict(int)
        open_days: dict[str, int] = defaultdict(int)
        as_of = _now()
        for station, day in pending[:max_scopes]:
            if deadline is not None and time.monotonic() >= deadline:
                stopped_for_time = True
                break
            try:
                result = materialize_settled_station_truth(
                    target,
                    source,
                    station_id=station,
                    event_date=day,
                    as_of_utc=as_of,
                )
            except sqlite3.OperationalError as exc:
                if "readonly database" in str(exc):
                    outcomes["would_commit"] += 1
                    examples.setdefault("would_commit", f"{station} {day}")
                    continue
                if "locked" in str(exc) or "busy" in str(exc):
                    # The resident got there first.  This is the one failure
                    # that must not end the pass: letting it propagate loses
                    # the receipt for the whole sweep, and a page reading a
                    # stale receipt cannot tell a busy database from a dead
                    # lane.  The scope keeps its place in the backlog and the
                    # next pass, half an hour later, asks again.
                    outcomes["deferred_locked"] += 1
                    examples.setdefault(
                        "deferred_locked", f"{station} {day}"
                    )
                    target.rollback()
                    continue
                raise
            except ContractError as exc:
                reason = _reason_of(exc)
                refusals[reason] += 1
                refusal_class[reason] = blocker_class(str(exc))
                if day >= retention_start:
                    refusal_recoverable[reason] += 1
                else:
                    refusal_expired[reason] += 1
                examples.setdefault(reason, f"{station} {day}")
                outcomes["refused"] += 1
                # A refusal is this scope's answer, not the sweep's.  The
                # resident stops a whole batch on a consistency failure
                # because its batch shares one transaction; here each scope
                # is its own, so one contradictory day must not cost the
                # forty settled days queued behind it.
                target.rollback()
                continue
            state = str(result.get("state") or "unknown")
            outcomes[state] += 1
            examples.setdefault(state, f"{station} {day}")
            if state == "skipped_open_settlement_day":
                # Recorded per day rather than as one total so the page can
                # tell "still settling" from "missing" without doing date
                # arithmetic of its own.  The fleet spans every timezone, so
                # any rule the page invented would be wrong for some station
                # at some hour; the writer's own answer never is.
                open_days[day] += 1
            if result.get("wrote"):
                target.commit()
                committed_days[day] += 1
                if verbose:
                    print(f"  committed {station} {day}")
            else:
                target.rollback()
        revision_outcomes: Counter[str] = Counter()
        revised: list[dict[str, Any]] = []
        # The re-read comes after the backlog and only on lease left over: a
        # day with no truth at all outranks a day whose truth may be short.
        if stopped_for_time or (deadline is not None and time.monotonic() >= deadline):
            stopped_for_time = True
            grown, grown_on_standing_lineage = [], 0
        else:
            grown, grown_on_standing_lineage = _grown_evidence(target, source)
        for station, day in grown[:max_revision_scopes]:
            if deadline is not None and time.monotonic() >= deadline:
                stopped_for_time = True
                break
            try:
                # Every scope here is a closed day that will write unless it
                # comes back idempotent, so the write lock is taken before the
                # writer reads.  Under the writer's own deferred BEGIN a read
                # that then upgrades while the resident holds the lock gets
                # SQLITE_BUSY at once -- WAL never waits for a stale snapshot
                # -- and on 2026-09-23 14:45Z all 37 re-settlements of a pass
                # failed that way while each succeeded on its own.  IMMEDIATE
                # queues on busy_timeout instead; the writer sees the caller's
                # transaction and stages into it.
                if not dry_run:
                    target.execute("BEGIN IMMEDIATE")
                result = materialize_settled_station_truth(
                    target, source, station_id=station, event_date=day, as_of_utc=as_of,
                )
            except sqlite3.OperationalError as exc:
                target.rollback()
                if "readonly database" in str(exc):
                    revision_outcomes["would_revise"] += 1
                    continue
                if "locked" in str(exc) or "busy" in str(exc):
                    revision_outcomes["deferred_locked"] += 1
                    continue
                raise
            except ContractError as exc:
                target.rollback()
                revision_outcomes["refused:" + _reason_of(exc)] += 1
                continue
            if result.get("wrote"):
                target.commit()
                revision_outcomes["revised"] += 1
                revised.append({
                    "station_id": station, "event_date": day,
                    "revision_no": result.get("revision_no"),
                    "official_tmax_c": result.get("official_tmax_c"),
                })
                if verbose:
                    print(f"  revised {station} {day} -> r{result.get('revision_no')}")
            else:
                target.rollback()
                revision_outcomes[str(result.get("state") or "unknown")] += 1
        report = {
            "schema": SCHEMA,
            "swept_at_utc": as_of,
            "dry_run": bool(dry_run),
            "window_start": window_start,
            "window_days": window_days,
            "backlog_scopes": len(pending),
            "attempted": min(len(pending), max_scopes),
            "committed": int(outcomes.get("committed", 0)),
            "idempotent": int(outcomes.get("idempotent", 0)),
            "open_day_skipped": int(
                outcomes.get("skipped_open_settlement_day", 0)
            ),
            "would_commit": int(outcomes.get("would_commit", 0)),
            "deferred_locked": int(outcomes.get("deferred_locked", 0)),
            "refused": int(outcomes.get("refused", 0)),
            "outcomes": dict(outcomes),
            "committed_by_day": dict(sorted(committed_days.items())),
            "open_by_day": dict(sorted(open_days.items())),
            "refusals": [
                {
                    "reason": reason,
                    "label": REASON_LABELS.get(reason, ""),
                    "count": count,
                    "blocker_class": refusal_class.get(reason, "CONSISTENCY"),
                    # Split so the page can say which of these a backfill
                    # could still reach.  Both counts are reported even when
                    # one is zero: "0 past retention" is the sentence that
                    # makes "waiting will fix this" true.
                    "recoverable_count": int(refusal_recoverable.get(reason, 0)),
                    "expired_count": int(refusal_expired.get(reason, 0)),
                    "example": examples.get(reason, ""),
                }
                for reason, count in refusals.most_common()
            ],
            "awc_retention_start": retention_start,
            # Charter 207: settled days re-read because their evidence grew.
            "revision_candidates": len(grown),
            # Of those, the ones the registry no longer carries: re-settled
            # on the lineage their standing revision pins, not skipped.
            "revision_on_standing_lineage": grown_on_standing_lineage,
            "revision_outcomes": dict(revision_outcomes),
            "revised": revised,
            "elapsed_seconds": round(time.time() - started, 2),
            "budget_sec": budget_sec,
            "stopped_for_time": stopped_for_time,
        }
        # Coverage is read after the writes so the receipt describes the
        # database the sweep is leaving behind, not the one it found.
        report["coverage"] = _coverage(target, window_start)
        report["backlog_after"] = len(_backlog(target, window_start))
        return report
    finally:
        target.close()
        source.close()


def _write_status(report: dict[str, Any], path: Path) -> None:
    """Replace the receipt atomically; a torn file reads as a dead lane."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    os.replace(temporary, path)


# Under the hosted timeout (900s) with room for the last scope in flight; the
# resident's writer lease clamps it further.
DEFAULT_PASS_BUDGET_SEC = 600.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--canonical", type=Path, default=DEFAULT_CANONICAL)
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--max-scopes", type=int, default=DEFAULT_MAX_SCOPES)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--status-out", type=Path, default=STATUS_JSON)
    args = parser.parse_args()
    report = sweep(
        args.db.expanduser().resolve(strict=True),
        args.canonical.expanduser().resolve(strict=True),
        window_days=max(1, int(args.window_days)),
        max_scopes=max(1, int(args.max_scopes)),
        dry_run=bool(args.dry_run),
        verbose=bool(args.verbose),
        budget_sec=writer_lease_seconds(DEFAULT_PASS_BUDGET_SEC),
    )
    if not args.dry_run:
        _write_status(report, args.status_out)
    if report.get("stopped_for_time"):
        print(HOSTED_SWEEP_CONTINUE_MARKER)
    print(json.dumps(
        {
            key: value
            for key, value in report.items()
            if key not in {"coverage"}
        },
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
