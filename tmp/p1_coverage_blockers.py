#!/usr/bin/env python3
"""Why is the soft likelihood dark on most station-days at a fixed local hour?

For every station-day, the P1 belief STANDING at a local hour is taken, and
the zero-strength ones are asked one question: was there an activation this
step could have used?  The worker resolves a single artifact from the wall
clock, while the compiler walks a prefix whose steps span the local-time
buckets, so a step can be dark while its own bucket is fully covered.  That
split is the difference between "no evidence exists" and "the evidence exists
and the resolution missed it".

The local-time bucket comes from production (``_local_time_bucket``).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.metar_trajectory import _local_time_bucket

SSOT = f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro"
ANCHORS = (("正午", 12), ("15:00", 15), ("日终前一小时", 23))


def main() -> None:
    conn = sqlite3.connect(SSOT, uri=True)
    conn.row_factory = sqlite3.Row

    truth = {
        (row["station_id"], row["local_date"])
        for row in conn.execute(
            "select distinct station_id, local_date"
            " from calibration_station_daily_high_truth"
        )
    }

    # Activations, grouped by station and the bucket named inside the scope key.
    activations: dict[tuple[str, str], list[tuple[str, str, str]]] = defaultdict(
        list
    )
    for row in conn.execute(
        "select station_id, scope_key, valid_from_utc, valid_until_utc,"
        " authority_scope from basket_probability_evidence_activation"
        " where artifact_kind='METAR_TRAJECTORY_LIKELIHOOD'"
    ):
        parts = str(row["scope_key"]).split("|")
        bucket = parts[2] if len(parts) > 2 else "?"
        activations[(row["station_id"], bucket)].append((
            row["valid_from_utc"],
            row["valid_until_utc"],
            row["authority_scope"],
        ))

    # Artifacts that exist for a station+bucket regardless of activation, so
    # "never trained" and "trained but never activated" stay distinguishable.
    artifacts: dict[tuple[str, str], int] = Counter()
    for row in conn.execute(
        "select station_id, scope_key from basket_probability_evidence_artifact"
        " where artifact_kind='METAR_TRAJECTORY_LIKELIHOOD'"
    ):
        parts = str(row["scope_key"]).split("|")
        bucket = parts[2] if len(parts) > 2 else "?"
        artifacts[(row["station_id"], bucket)] += 1

    timezones = {}
    for row in conn.execute(
        "select distinct station_id, event_date, settlement_timezone"
        " from basket_probability_update_proof"
        " where proof_kind='METAR_TRAJECTORY_UPDATE'"
    ):
        timezones[(row["station_id"], row["event_date"])] = (
            row["settlement_timezone"]
        )

    conn.execute(
        "create temp table cutoffs (station text, day text, label text,"
        " cutoff_utc text, primary key (station, day, label))"
    )
    rows = []
    for (station, day), tz_name in timezones.items():
        if (station, day) not in truth:
            continue
        try:
            zone = ZoneInfo(tz_name)
        except Exception:
            continue
        base = datetime.fromisoformat(day)
        for label, hour in ANCHORS:
            local = base.replace(hour=hour, tzinfo=zone)
            rows.append((
                station, day, label,
                local.astimezone(ZoneInfo("UTC")).strftime(
                    "%Y-%m-%dT%H:%M:%S.%fZ"
                ),
            ))
    conn.executemany("insert into cutoffs values (?,?,?,?)", rows)

    picked = conn.execute(
        "select p.station_id, p.event_date, c.label, p.settlement_timezone,"
        " p.decision_at_utc,"
        " json_extract(p.proof_json,'$.applied_strength') as strength,"
        " json_extract(p.proof_json,'$.as_of_utc') as as_of,"
        " json_extract(p.proof_json,'$.degradation_reasons') as reasons,"
        " json_extract(p.proof_json,'$.canonical_observation_ids') as obs"
        " from basket_probability_update_proof p"
        " join (select p2.station_id s, p2.event_date d, c2.label l,"
        "       max(p2.decision_at_utc) m"
        "       from basket_probability_update_proof p2"
        "       join cutoffs c2 on c2.station=p2.station_id"
        "        and c2.day=p2.event_date"
        "       where p2.proof_kind='METAR_TRAJECTORY_UPDATE'"
        "        and p2.decision_at_utc <= c2.cutoff_utc"
        "       group by 1,2,3) q"
        " on p.station_id=q.s and p.event_date=q.d and p.decision_at_utc=q.m"
        " join cutoffs c on c.station=p.station_id and c.day=p.event_date"
        "  and c.label=q.l"
        " where p.proof_kind='METAR_TRAJECTORY_UPDATE'"
    ).fetchall()

    # Activations only started appearing 2026-08-12, so a window that covers
    # nothing on 2026-08-03 says the layer had not been built yet, not that
    # it is blocked now.  The recent window is the one that describes today.
    since = "2026-08-18"
    print(f"（只算 {since} 起的事件日；更早的站日在激活建立之前，"
          f"其“无覆盖”是历史而非当前缺陷）\n")
    for label, hour in ANCHORS:
        subset = [
            r for r in picked
            if r["label"] == label and r["event_date"] >= since
        ]
        if not subset:
            continue
        dark = [r for r in subset if (r["strength"] or 0.0) <= 0.0]
        print(f"===== 当地 {label} 站立 (n={len(subset)}) =====")
        print(f"  层生效 {len(subset) - len(dark)}"
              f" / 层全黑 {len(dark)}"
              f" ({100.0 * len(dark) / len(subset):.0f}%)")
        if not dark:
            print()
            continue
        blockers = Counter()
        examples = defaultdict(list)
        for row in dark:
            station = row["station_id"]
            as_of = str(row["as_of"] or row["decision_at_utc"])
            try:
                bucket = _local_time_bucket(as_of, row["settlement_timezone"])
            except Exception:
                blockers["时区/时刻不可解析"] += 1
                continue
            key = (station, bucket)
            covering = [
                entry for entry in activations.get(key, ())
                if entry[0] <= as_of < entry[1]
            ]
            if covering:
                reason = "本步时段有效激活存在(可救:解析按墙钟选错时段)"
            elif activations.get(key):
                # A past station-day whose bucket only got activated later is
                # not a defect in today's chain: nothing existed to resolve.
                # Only an activation that has since lapsed is one.
                later = [
                    entry for entry in activations[key] if entry[0] > as_of
                ]
                reason = (
                    "本步时段的激活晚于该日才建(历史日,非当前缺陷)"
                    if later and len(later) == len(activations[key])
                    else "本步时段有激活但已过期"
                )
            elif artifacts.get(key):
                reason = "本步时段训出工件但从未激活"
            else:
                reason = (
                    "本步时段全库零工件"
                    + ("(NIGHT,训练端不产)" if bucket == "NIGHT" else "")
                )
            blockers[f"{bucket}: {reason}"] += 1
            if len(examples[reason]) < 3:
                examples[reason].append(
                    f"{station} {row['event_date']} 步在 {bucket}"
                )
        for reason, count in blockers.most_common():
            print(f"    {count:4d} ({100.0 * count / len(dark):4.0f}%)  {reason}")
        print("  举例:")
        for reason, cases in examples.items():
            print(f"    {reason[:40]}: {', '.join(cases)}")
        print()


if __name__ == "__main__":
    main()
