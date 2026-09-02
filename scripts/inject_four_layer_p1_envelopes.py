#!/usr/bin/env python3
"""Swap a replay database's P1 envelopes for the four layers' counterfactual.

The last piece of the loop for P1.  The walk-forward evaluation scores bucket
accuracy and the ladder's centre, but neither is money: the basket seats a
ladder off that centre, prices it against the book, and the return depends on
what it could buy at what spread.  Only the paper ledger answers that, and it
reads compiled envelopes -- so the counterfactual has to be written as
envelopes for it to score.

Each replay P1 envelope is matched to the sample row taken at the same instant,
the four layers are applied under artifacts fitted on strictly earlier days
than that envelope's event date, and the resulting distribution replaces the
committed one.  Same discipline as the evaluation, and literally the same fit
function, because two fits of one window that differ by a line are two
experiments wearing one name.

The reanchor is off by default here.  It is recorded in the shadow and not
promoted: switching it off changed the centre error by 0.006 C in the
walk-forward, and an arm nobody proposes to ship does not belong in the arm
that is being priced.  ``--with-reanchor`` runs it anyway, which is how that
verdict stays falsifiable.

Three tables move together, the way the P0 injector moves them: the envelope
body, the history row's content hash, and the bucket projection the ledger
actually reads.  Leaving any one stale scores a distribution nothing served.

    python3 scripts/build_ledger_replay_db.py --since 2026-08-24 --out /tmp/replay.db
    python3 scripts/paper_trading_ledger.py --db /tmp/replay.db --backfill  # baseline
    python3 scripts/inject_four_layer_p1_envelopes.py --replay-db /tmp/replay.db \
        --samples data/runtime/bayes_basket_target_nwp/metar_phase_hazard_samples.db
    python3 scripts/paper_trading_ledger.py --db /tmp/replay.db --backfill  # arm
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.materialization import (  # noqa: E402
    rehydrate_lineage_documents,
)
from bayes_basket_target.metar_four_layer import (  # noqa: E402
    compile_four_layer_posterior,
    feature_row,
)
from bayes_basket_target.metar_trajectory import (  # noqa: E402
    hard_bound_excluded_buckets,
)
from scripts.evaluate_metar_four_layer_walk_forward import (  # noqa: E402
    RootCache,
    walk_forward_artifacts,
)

# A P1 envelope's decision clock is the cycle's, not the report's: the worker
# compiles on its own schedule and consumes whatever has arrived.  So the state
# an envelope was compiled against is the newest sample at or before its clock,
# not the nearest one -- matching on nearest picked the handful of accidental
# coincidences (3,322 of 65,834) and skipped the rest.
#
# The envelope states the running high it was compiled under.  Requiring the
# matched sample to agree turns the time match into an identity check: a
# disagreement means the two are looking at different prefixes, whatever their
# clocks say.
RUNNING_HIGH_TOLERANCE_C = 0.05


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replay-db", required=True)
    parser.add_argument("--samples", required=True)
    parser.add_argument("--with-reanchor", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    samples = sqlite3.connect(f"file:{args.samples}?mode=ro", uri=True)
    samples.row_factory = sqlite3.Row
    all_rows = [
        dict(row)
        for row in samples.execute(
            "SELECT * FROM metar_phase_hazard_sample"
            " ORDER BY event_date, station_id, as_of_utc"
        )
    ]
    by_scope: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in all_rows:
        by_scope[
            (str(row["station_id"]), str(row["event_date"]))
        ].append(row)

    replay = sqlite3.connect(args.replay_db)
    replay.row_factory = sqlite3.Row
    roots = RootCache(replay)
    artifacts: dict[str, tuple[dict[str, Any], dict[str, Any]] | None] = {}
    swapped = 0
    skipped: dict[str, int] = defaultdict(int)
    centre_shift: list[float] = []

    rows = replay.execute(
        "SELECT h.history_id, h.checkpoint_id, h.station_id, h.event_date,"
        " h.decision_at_utc, e.stage, e.settlement_timezone,"
        " e.fresh_until_utc, e.bucket_ordinals_json, e.probabilities_json,"
        " e.posterior_version, e.posterior_sha256, e.bridge_sha256,"
        " e.lineage_json"
        " FROM basket_probability_bridge_history h"
        " JOIN basket_probability_envelope e"
        "   ON e.envelope_id = h.checkpoint_id"
        " WHERE h.stage='P1' AND h.state='COMMITTED'"
        " ORDER BY h.event_date, h.station_id, h.decision_at_utc"
    ).fetchall()
    print(f"  {len(rows)} committed P1 envelopes in the replay database")

    for row in rows:
        day = str(row["event_date"])
        if day not in artifacts:
            artifacts[day] = walk_forward_artifacts(all_rows, day)
        fitted = artifacts[day]
        if fitted is None:
            skipped["no_artifact_fitted_before_this_day"] += 1
            continue
        hazard, reanchor = fitted
        scope = by_scope.get((str(row["station_id"]), day)) or []
        if not scope:
            skipped["no_samples_for_scope"] += 1
            continue
        lineage = rehydrate_lineage_documents(
            replay, json.loads(str(row["lineage_json"]) or "{}")
        )
        try:
            served_high: float | None = float(
                lineage.get("metar_trajectory_running_high_c")
            )
        except (TypeError, ValueError):
            served_high = None
        # The running high identifies the prefix; the clock does not.  When a
        # root rebases, P1 replays the whole day and every intermediate step
        # is committed under a late decision clock while carrying the high it
        # had at the time.  Matching on the clock alone hands those steps the
        # afternoon's state -- 47,827 of 65,834 envelopes disagreed that way.
        # So the high selects the prefix and the clock only bounds it.
        decision = _parse(str(row["decision_at_utc"]))
        earlier = [
            candidate for candidate in scope
            if _parse(str(candidate["as_of_utc"])) <= decision
            and (
                served_high is None
                or abs(
                    float(candidate["running_high_c"]) - served_high
                ) <= RUNNING_HIGH_TOLERANCE_C
            )
        ]
        if not earlier:
            skipped["no_sample_shares_this_prefix"] += 1
            continue
        sample = max(earlier, key=lambda c: _parse(str(c["as_of_utc"])))
        root_checkpoint = str(
            lineage.get("prediction_root_checkpoint_id") or ""
        )
        view = roots.get(root_checkpoint)
        if view is None:
            skipped["root_not_in_replay_database"] += 1
            continue
        running_high = float(sample["running_high_c"])
        excluded = hard_bound_excluded_buckets(
            sorted(view["ordinals"], key=lambda b: view["ordinals"][b]),
            view["uppers"],
            running_high,
        )
        high_bucket = str(lineage.get("reported_high_bucket") or "")
        if high_bucket not in view["ordinals"]:
            skipped["reported_high_bucket_unknown"] += 1
            continue
        features = feature_row(
            remaining_hours=float(sample["remaining_hours"]),
            drawdown_from_high_c=float(sample["drawdown_from_high_c"]),
            momentum_c_per_hour=float(sample["momentum_c_per_hour"]),
            cloud_cover_fraction=float(sample["cloud_cover_fraction"]),
            precipitation_present=bool(sample["precipitation_present"]),
            dewpoint_depression_c=float(sample["dewpoint_depression_c"]),
            wind_speed_mps=float(sample["wind_speed_mps"]),
            warming_so_far_c=float(sample["warming_so_far_c"]),
            curve_shape_realized=sample["curve_shape_realized"],
        )
        try:
            counterfactual, _ = compile_four_layer_posterior(
                root_probabilities=view["probabilities"],
                bucket_ordinals=view["ordinals"],
                bucket_centers_c=view["centers"],
                bucket_intervals_c=view["intervals"],
                hard_excluded_buckets=excluded,
                running_high_c=running_high,
                running_high_bucket=high_bucket,
                features=features,
                hazard_artifact=hazard,
                reanchor_artifact=reanchor if args.with_reanchor else None,
                bias_recent_c=sample["root_bias_recent_c"],
                bias_report_count=int(sample["root_bias_report_count"]),
                reanchor_enabled=bool(args.with_reanchor),
            )
        except ContractError as exc:
            skipped[f"refused:{exc}"] += 1
            continue
        served = json.loads(str(row["probabilities_json"]))
        if set(counterfactual) != set(served):
            skipped["bucket_support_mismatch"] += 1
            continue
        centre = lambda dist: sum(  # noqa: E731
            float(value) * float(view["centers"][bucket])
            for bucket, value in dist.items()
        )
        centre_shift.append(centre(counterfactual) - centre(served))
        if args.dry_run:
            swapped += 1
            continue
        payload = {
            "schema": "bayes_basket_probability_envelope_v1",
            "stage": str(row["stage"]),
            "checkpoint_id": str(row["checkpoint_id"]),
            "station_id": str(row["station_id"]),
            "event_date": day,
            "settlement_timezone": str(row["settlement_timezone"]),
            "decision_at_utc": str(row["decision_at_utc"]),
            "fresh_until_utc": str(row["fresh_until_utc"]),
            "bucket_ordinals": json.loads(str(row["bucket_ordinals_json"])),
            "probabilities": counterfactual,
            "posterior_version": str(row["posterior_version"]),
            "posterior_sha256": str(row["posterior_sha256"]),
            "bridge_sha256": str(row["bridge_sha256"]),
            "lineage": lineage,
        }
        replay.execute(
            "UPDATE basket_probability_envelope SET probabilities_json=?"
            " WHERE envelope_id=?",
            (
                json.dumps(counterfactual, sort_keys=True),
                str(row["checkpoint_id"]),
            ),
        )
        replay.execute(
            "UPDATE basket_probability_bridge_history"
            " SET envelope_content_sha256=? WHERE history_id=?",
            (content_sha256(payload), str(row["history_id"])),
        )
        for bucket, value in counterfactual.items():
            replay.execute(
                "UPDATE basket_probability_bridge_bucket_probability"
                " SET probability=? WHERE history_id=? AND bucket=?",
                (float(value), str(row["history_id"]), str(bucket)),
            )
        swapped += 1
    if not args.dry_run:
        replay.commit()
    replay.close()

    moved = [value for value in centre_shift if abs(value) > 1e-9]
    print(
        f"  swapped {swapped} P1 envelopes"
        + (" (dry run, nothing written)" if args.dry_run else "")
    )
    if centre_shift:
        mean_shift = sum(centre_shift) / len(centre_shift)
        print(
            f"  ladder centre moved on {len(moved)} of {len(centre_shift)},"
            f" mean {mean_shift:+.3f} C,"
            f" max {max((abs(v) for v in centre_shift), default=0.0):.3f} C"
        )
    for reason, count in sorted(skipped.items(), key=lambda kv: -kv[1]):
        print(f"    skipped {count}: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
