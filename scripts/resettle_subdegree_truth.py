#!/usr/bin/env python3
"""Re-derive settled truth for the days that settled at the wrong resolution.

``calibration_station_daily_high_truth.official_tmax_c`` is the single
settlement authority: the delta-v2 labeller maps it against Celsius bucket
edges and the paper ledger scores positions against it.  Until 2026-08-18 the
AWC lane wrote it from the METAR body group alone, which is whole degrees.

That is enough for the thirty-four stations whose books quote 1C buckets and
is not enough for the eleven that quote 2F.  A 2F bucket is 1.111C, so the
half-degree either side of a whole Celsius reading is a 1.8F window inside a
2F bucket: it fits only when the reading lands in the middle fifth, so eight
integer Celsius values in ten reach across an edge.  Measured on 2026-08-18
over the 178 settled Fahrenheit station-days, 137 admitted two different
winning buckets and the code picked one of them without any way of knowing
which.  It picked wrong on 32.

The reports themselves were never that coarse.  Every one of these eleven
stations is a United States ASOS that restates its temperature in tenths in
the ``Tsnnnsnnn`` remark group -- present on 7826 of the 7959 archived AWC
reports for them, 98.3% -- and the raw bytes have been stored in
``reported_trajectory_observations.raw_text``, hash-pinned, all along.  So
nothing has to be re-fetched and no archived row has to be rewritten: the
settled-truth writer now reads the tenth out of the bytes it already had.

What this pass does is ask the writer to answer those days again.  The truth
table is append-only behind ``calibration_truth_no_update`` and
``calibration_truth_no_delete``; a better answer arrives as revision N+1 with
its own evidence payload, and the revision it replaces stays exactly where it
is.  Readers take ``MAX(revision_no)``, so the improvement propagates without
anything being erased -- and the record of what was believed on the day, and
of what it was believed from, survives the correction.

The resident writer would reach these scopes on its own eventually: its
training-fact cursor wraps and revisits every historical scope.  This pass
exists so the correction happens at a known moment, with a receipt that says
which days moved and which of them moved across a bucket edge, rather than
drifting in one scope at a time.

    python3 scripts/resettle_subdegree_truth.py              # report only
    python3 scripts/resettle_subdegree_truth.py --apply      # append revisions
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.p0_producer import (  # noqa: E402
    bucket_intervals_from_registry_rows,
)
from bayes_basket_target.settlement_truth import (  # noqa: E402
    CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1,
    blocker_class,
    materialize_settled_station_truth,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
DEFAULT_CANONICAL = ROOT / "data" / "archive.db"
STATUS_JSON = ROOT / "data" / "resettle_subdegree_truth_status.json"
SCHEMA = "resettle_subdegree_truth_v1"
# Bounded like the sweep beside it, for the same reason: the resident gives up
# on a busy database after five seconds rather than stall a cadence, so a
# maintenance writer must never hold the lock longer than one scope.
DEFAULT_MAX_SCOPES = 400
REASON_LABELS = {
    "settlement_truth_hourly_observation_missing": "该日没有合格的报文",
    "settlement_truth_corrections_unresolved": "有疑似更正的报文等人工审核",
    "settlement_truth_daytime_coverage_insufficient": "白天时段的报文不足",
    "settlement_truth_hourly_revision_ambiguous": "同一整点有两份互不相同的报文",
    "settlement_truth_raw_text_sha256_mismatch": "报文原文与其哈希不符",
    "settlement_truth_canonical_revision_after_asof": "已有比本次决策时刻更新的修订",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _reason_of(error: Exception) -> str:
    return str(error).split(":", 1)[0].strip() or "unknown"


def _settled_scopes(
    conn: sqlite3.Connection,
    *,
    stations: tuple[str, ...],
    since: str,
) -> list[tuple[str, str, float, int]]:
    """Already-settled station-days on the live role, with their latest value.

    Restricted to days that already have truth: a day with none is the other
    script's job (``settlement_truth_sweep.py``), and mixing the two would
    make one receipt answer two different questions.
    """

    placeholders = ",".join("?" for _ in stations) if stations else ""
    station_clause = (
        f" AND t.station_id IN ({placeholders})" if stations else ""
    )
    return [
        (str(station), str(day), float(value), int(revision))
        for station, day, value, revision in conn.execute(
            "SELECT t.station_id,t.local_date,t.official_tmax_c,t.revision_no"
            " FROM calibration_station_daily_high_truth t"
            " WHERE t.source_role=? AND t.qc_status='ACCEPTED'"
            "  AND t.local_date>=?" + station_clause +
            "  AND t.revision_no=("
            "   SELECT MAX(r.revision_no)"
            "   FROM calibration_station_daily_high_truth r"
            "   WHERE r.station_id=t.station_id AND r.local_date=t.local_date"
            "    AND r.source_role=t.source_role)"
            " ORDER BY t.local_date,t.station_id",
            (CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1, since, *stations),
        )
    ]


def _bucket_geometry(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
) -> tuple[str, dict[str, tuple[float | None, float | None]]] | None:
    """This station-day's settlement unit and its bucket edges in Celsius.

    Read from the market registry through the same converter the P0 producer
    uses, so "which bucket won" here means exactly what it means downstream
    rather than something this script decided for itself.
    """

    # One station-day carries many registry snapshots -- KATL 2026-08-14 has
    # 26, all quoting the same eleven buckets -- and several of them share the
    # newest ``refreshed_at``.  Reading "the newest rows" therefore returns
    # every copy of the ladder at once, which is not a ladder.  The settled
    # truth writer resolves this with ``ORDER BY refreshed_at DESC,
    # snapshot_sha256 DESC`` and then keeps only that one snapshot; the same
    # tie-break is used here so both answer for the same market.
    newest = conn.execute(
        "SELECT snapshot_sha256 FROM basket_target_market_registry"
        " WHERE station_id=? AND event_date=? AND source_eligible=1"
        " ORDER BY refreshed_at DESC,snapshot_sha256 DESC LIMIT 1",
        (station_id, event_date),
    ).fetchone()
    if newest is None:
        return None
    rows = [
        {
            "station_id": str(station),
            "event_date": str(day),
            "settlement_unit": str(unit or "").upper(),
            "outcome": str(outcome or ""),
            "registry_json": str(registry_json or ""),
        }
        for station, day, unit, outcome, registry_json in conn.execute(
            "SELECT station_id,event_date,settlement_unit,outcome,registry_json"
            " FROM basket_target_market_registry"
            " WHERE station_id=? AND event_date=? AND source_eligible=1"
            " AND snapshot_sha256=?",
            (station_id, event_date, str(newest[0])),
        )
    ]
    if not rows:
        return None
    try:
        return rows[0]["settlement_unit"], bucket_intervals_from_registry_rows(
            rows
        )
    except ContractError:
        return None


def _winning_bucket(
    intervals: dict[str, tuple[float | None, float | None]],
    truth_c: float,
) -> str | None:
    matches = [
        bucket
        for bucket, (lower, upper) in intervals.items()
        if (lower is None or truth_c >= lower)
        and (upper is None or truth_c < upper)
    ]
    return matches[0] if len(matches) == 1 else None


def resettle(
    db: Path,
    canonical: Path,
    *,
    stations: tuple[str, ...],
    since: str,
    max_scopes: int,
    apply_changes: bool,
    verbose: bool,
) -> dict[str, Any]:
    started = time.time()
    target = sqlite3.connect(str(db), timeout=120.0)
    target.execute("PRAGMA busy_timeout=120000")
    source = sqlite3.connect(
        f"file:{canonical}?mode=ro", uri=True, timeout=120.0
    )
    try:
        scopes = _settled_scopes(target, stations=stations, since=since)
        outcomes: Counter[str] = Counter()
        refusals: Counter[str] = Counter()
        refusal_class: dict[str, str] = {}
        changes: list[dict[str, Any]] = []
        as_of = _now()
        for station, day, previous_value, previous_revision in (
            scopes[:max_scopes]
        ):
            geometry = _bucket_geometry(
                target, station_id=station, event_date=day
            )
            # Owning the transaction makes the writer stage instead of commit,
            # so a report-only pass sees the exact value a real one would
            # write and then discards it.  The lock is held for one scope
            # either way; what changes is whether it ends in COMMIT.
            #
            # Taking the lock is itself the step most likely to lose the race:
            # the resident holds the write lock for roughly half of every
            # production cycle, so a pass that let a busy database raise here
            # would abandon the scopes queued behind it rather than the one it
            # collided with.
            try:
                target.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc) or "busy" in str(exc):
                    outcomes["deferred_locked"] += 1
                    continue
                raise
            try:
                result = materialize_settled_station_truth(
                    target,
                    source,
                    station_id=station,
                    event_date=day,
                    as_of_utc=as_of,
                )
            except sqlite3.OperationalError as exc:
                target.rollback()
                if "locked" in str(exc) or "busy" in str(exc):
                    outcomes["deferred_locked"] += 1
                    continue
                raise
            except ContractError as exc:
                target.rollback()
                reason = _reason_of(exc)
                refusals[reason] += 1
                refusal_class[reason] = blocker_class(str(exc))
                outcomes["refused"] += 1
                continue
            new_value = float(result["official_tmax_c"])
            moved = abs(new_value - previous_value) > 1e-9
            record: dict[str, Any] = {
                "station_id": station,
                "event_date": day,
                "previous_official_tmax_c": previous_value,
                "official_tmax_c": new_value,
                "previous_revision_no": previous_revision,
                "revision_no": int(result["revision_no"]),
            }
            if geometry is not None:
                unit, intervals = geometry
                previous_bucket = _winning_bucket(intervals, previous_value)
                new_bucket = _winning_bucket(intervals, new_value)
                record["settlement_unit"] = unit
                record["previous_bucket"] = previous_bucket
                record["bucket"] = new_bucket
                record["bucket_changed"] = (
                    previous_bucket != new_bucket
                    and previous_bucket is not None
                    and new_bucket is not None
                )
            if moved:
                outcomes["value_changed"] += 1
                if record.get("bucket_changed"):
                    outcomes["bucket_changed"] += 1
                changes.append(record)
            else:
                outcomes["unchanged"] += 1
            if apply_changes and result.get("wrote"):
                target.commit()
                outcomes["committed"] += 1
                if verbose:
                    print(
                        f"  {station} {day} "
                        f"{previous_value} -> {new_value} "
                        f"(rev {previous_revision} -> {result['revision_no']})"
                    )
            else:
                target.rollback()
        report = {
            "schema": SCHEMA,
            "resettled_at_utc": as_of,
            "applied": bool(apply_changes),
            "since": since,
            "stations": list(stations),
            "scope_count": len(scopes),
            "attempted": min(len(scopes), max_scopes),
            "value_changed": int(outcomes.get("value_changed", 0)),
            "bucket_changed": int(outcomes.get("bucket_changed", 0)),
            "unchanged": int(outcomes.get("unchanged", 0)),
            "committed": int(outcomes.get("committed", 0)),
            "refused": int(outcomes.get("refused", 0)),
            "deferred_locked": int(outcomes.get("deferred_locked", 0)),
            "refusals": [
                {
                    "reason": reason,
                    "label": REASON_LABELS.get(reason, ""),
                    "count": count,
                    "blocker_class": refusal_class.get(reason, "CONSISTENCY"),
                }
                for reason, count in refusals.most_common()
            ],
            "changes": changes,
            "elapsed_seconds": round(time.time() - started, 2),
        }
        return report
    finally:
        target.close()
        source.close()


def _write_status(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--canonical", type=Path, default=DEFAULT_CANONICAL)
    parser.add_argument(
        "--station",
        action="append",
        default=[],
        help="Limit to these stations; default is every settled station.",
    )
    parser.add_argument("--since", default="2026-01-01")
    parser.add_argument("--max-scopes", type=int, default=DEFAULT_MAX_SCOPES)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--status-out", type=Path, default=STATUS_JSON)
    args = parser.parse_args()
    report = resettle(
        args.db.expanduser().resolve(strict=True),
        args.canonical.expanduser().resolve(strict=True),
        stations=tuple(
            sorted({str(x).strip().upper() for x in args.station if str(x).strip()})
        ),
        since=str(args.since),
        max_scopes=max(1, int(args.max_scopes)),
        apply_changes=bool(args.apply),
        verbose=bool(args.verbose),
    )
    if args.apply:
        _write_status(report, args.status_out)
    print(json.dumps(
        {key: value for key, value in report.items() if key != "changes"},
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
