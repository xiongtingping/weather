"""Replace a replay database's envelopes with counterfactual ones.

The last piece of the loop.  Retention keeps the packet, the recompile turns
a packet plus an artifact parameter set into an envelope, the extractor
builds a database the ledger can read -- this writes the second arm's
envelopes into that database so the ledger can score it.

The counterfactual is expressed as an artifact parameter set, not as a
constant, because a constant that lives in the trainer never reaches a
compile: the half-life produces ``bias_c`` and ``bias_c`` is already baked in
by the time the compiler runs.  So each system's parameters are refitted here
under the counterfactual constant, using production's own ``_load_cases`` and
``_fit_parameters``, and handed to the compiler through the narrow override
it grew for exactly this.

Two consistency rules the replay database enforces on its own, and the reason
this writes both places rather than one.  ``history_envelope_payload``
rebuilds the body out of ``basket_probability_envelope`` and refuses it
unless the rebuilt payload hashes to the ``envelope_content_sha256`` the
history row carries -- so a body swapped without its hash fails loudly rather
than serving quietly.  And the bucket projection is read separately by the
ledger, so it has to move with the body or the two disagree.

Only ever pointed at a replay database.  It rewrites committed evidence,
which is a thing production's own tables forbid by trigger and this one
permits only because the extractor deliberately leaves those triggers behind.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import native_ensemble_oos as OOS  # noqa: E402
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.native_ensemble_p0 import (  # noqa: E402
    compile_native_ensemble_p0,
)

ARCHIVE = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "forward_packet_archive"
)
SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"


_CASES: dict[tuple[str, str], dict] = {}


def _refit_parameters(
    ssot: sqlite3.Connection,
    *,
    station: str,
    event_date: str,
    as_of_utc: str,
    half_life: float,
) -> dict[str, dict]:
    """Each system's parameters as the counterfactual half-life would fit them.

    Strictly earlier days only, and production's own loader and fit, so the
    only difference from what was served is the constant under test.

    The case load is cached per station-day rather than per envelope.  A
    station-day carries dozens of P0 decisions and loading every case afresh
    for each of them made a 22-chain run take a quarter of an hour.  The cache
    is sound because of what the fit is allowed to see: every case it uses is
    from a day strictly before the event date and therefore already closed, so
    it was equally visible to the first decision of that day and to the last.
    The as-of cut still bounds the load; it just does not need re-cutting per
    decision.
    """

    key = (station, event_date)
    if key in _CASES:
        by_scope = _CASES[key]
    else:
        try:
            by_scope = OOS._load_cases(
                ssot, station_id=station, as_of_utc=as_of_utc
            )
        except Exception:
            by_scope = {}
        _CASES[key] = by_scope
    if not by_scope:
        return {}
    truth = {
        str(row[0]): float(row[1])
        for row in ssot.execute(
            "SELECT local_date, official_tmax_c"
            " FROM calibration_station_daily_high_truth t"
            " WHERE station_id=? AND revision_no = ("
            "  SELECT MAX(revision_no)"
            "  FROM calibration_station_daily_high_truth x"
            "  WHERE x.station_id=t.station_id AND x.local_date=t.local_date)",
            (station,),
        )
    }
    floor = (
        date.fromisoformat(event_date)
        - timedelta(days=OOS.ROLLING_TRAIN_WINDOW_DAYS)
    ).isoformat()
    out: dict[str, dict] = {}
    for scope, cases in by_scope.items():
        system_id = str(scope[0])
        history = [
            case for case in cases
            if floor <= str(case.get("event_date") or "")[:10] < event_date
            and str(case.get("event_date") or "")[:10] in truth
            and case.get("values")
        ]
        if len(history) < 10:
            continue
        original = OOS.BIAS_HALF_LIFE_DAYS
        OOS.BIAS_HALF_LIFE_DAYS = half_life
        try:
            fitted = OOS._fit_parameters(history)
        except Exception:
            continue
        finally:
            OOS.BIAS_HALF_LIFE_DAYS = original
        # Only the three maps the compiler's override accepts, and only the
        # keys the artifact carries -- a parameter set with extra keys would
        # not be one the serving path could have produced.
        out.setdefault(system_id, {})["bias_parameters"] = {
            "bias_c": float(fitted["bias_c"]),
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--event-date", required=True)
    parser.add_argument(
        "--bias-half-life", type=float, required=True,
        help="the counterfactual value to refit each system's bias under",
    )
    args = parser.parse_args()

    day_dir = ARCHIVE / args.event_date
    if not day_dir.is_dir():
        raise SystemExit(f"no retained packets for {args.event_date}")

    replay = sqlite3.connect(str(args.db.resolve(strict=True)), timeout=120.0)
    ssot = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=120.0)
    ssot.execute("PRAGMA query_only=ON")

    swapped = 0
    skipped: dict[str, int] = {}
    try:
        rows = replay.execute(
            "SELECT history_id, station_id, checkpoint_id, decision_at_utc"
            " FROM basket_probability_bridge_history"
            " WHERE event_date=? AND stage='P0'"
            " ORDER BY station_id, decision_at_utc",
            (args.event_date,),
        ).fetchall()
        print(f"  P0 envelopes in window for {args.event_date}: {len(rows)}")

        packets: dict[str, list[dict]] = {}
        for path in sorted(day_dir.glob("*.json.gz")):
            with gzip.open(path, "rb") as handle:
                packet = json.loads(handle.read().decode("utf-8"))
            packets.setdefault(
                str(packet.get("station_id") or ""), []
            ).append(packet)

        for history_id, station, checkpoint, decided in rows:
            station = str(station)
            candidates = packets.get(station) or []
            if not candidates:
                skipped["no_retained_packet"] = (
                    skipped.get("no_retained_packet", 0) + 1
                )
                continue
            override = _refit_parameters(
                ssot, station=station, event_date=args.event_date,
                as_of_utc=str(decided), half_life=args.bias_half_life,
            )
            if not override:
                skipped["no_refit"] = skipped.get("no_refit", 0) + 1
                continue
            compiled = None
            for packet in candidates:
                try:
                    compiled = compile_native_ensemble_p0(
                        ssot,
                        forward_packet=packet,
                        decision_at_utc=str(decided),
                        artifact_parameter_override=override,
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    reason = str(exc).split(":")[-1][:40]
                    skipped[reason] = skipped.get(reason, 0) + 1
            if compiled is None:
                continue
            prediction = compiled.get("forward_prediction") or {}
            probabilities = json.loads(
                str(prediction.get("probabilities_json") or "{}")
            )
            if not probabilities:
                skipped["empty_probabilities"] = (
                    skipped.get("empty_probabilities", 0) + 1
                )
                continue

            body = replay.execute(
                "SELECT stage, checkpoint_id, station_id, event_date,"
                " settlement_timezone, decision_at_utc, fresh_until_utc,"
                " bucket_ordinals_json, probabilities_json,"
                " posterior_version, posterior_sha256, bridge_sha256,"
                " lineage_json FROM basket_probability_envelope"
                " WHERE envelope_id=?",
                (str(checkpoint),),
            ).fetchone()
            if body is None:
                skipped["no_body_row"] = skipped.get("no_body_row", 0) + 1
                continue
            existing = json.loads(str(body[8]))
            # Keep the served bucket set: a counterfactual that changed which
            # buckets exist would be testing the axis, not the parameter.
            merged = {
                bucket: float(probabilities.get(bucket, 0.0))
                for bucket in existing
            }
            total = sum(merged.values())
            if total <= 0:
                skipped["counterfactual_off_axis"] = (
                    skipped.get("counterfactual_off_axis", 0) + 1
                )
                continue
            merged = {k: v / total for k, v in merged.items()}

            # ``schema`` first, exactly as ``history_envelope_payload``
            # builds it.  Omitting it produced a hash that did not match and
            # the rebuild refused all 240 swapped envelopes -- the contract
            # failing loudly rather than serving a body the projection was
            # not computed from, which is what it is for.
            payload = {
                "schema": "bayes_basket_probability_envelope_v1",
                "stage": str(body[0]), "checkpoint_id": str(body[1]),
                "station_id": str(body[2]), "event_date": str(body[3]),
                "settlement_timezone": str(body[4]),
                "decision_at_utc": str(body[5]),
                "fresh_until_utc": str(body[6]),
                "bucket_ordinals": json.loads(str(body[7])),
                "probabilities": merged,
                "posterior_version": str(body[9]),
                "posterior_sha256": str(body[10]),
                "bridge_sha256": str(body[11]),
                "lineage": json.loads(str(body[12])),
            }
            replay.execute(
                "UPDATE basket_probability_envelope SET probabilities_json=?"
                " WHERE envelope_id=?",
                (json.dumps(merged, sort_keys=True), str(checkpoint)),
            )
            replay.execute(
                "UPDATE basket_probability_bridge_history"
                " SET envelope_content_sha256=? WHERE history_id=?",
                (content_sha256(payload), str(history_id)),
            )
            for bucket, value in merged.items():
                replay.execute(
                    "UPDATE basket_probability_bridge_bucket_probability"
                    " SET probability=? WHERE history_id=? AND bucket=?",
                    (float(value), str(history_id), str(bucket)),
                )
            swapped += 1
        replay.commit()
    finally:
        replay.close()
        ssot.close()

    print(f"  swapped {swapped} envelopes to the counterfactual")
    for reason, count in sorted(skipped.items(), key=lambda kv: -kv[1]):
        print(f"    skipped {count}: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
