"""Recompile a retained P0 packet, optionally under a counterfactual constant.

This is the half of the loop the paper ledger cannot do.  The ledger reads
compiled envelopes and scores the book they produce; it has no way to produce
an envelope, so every parameter upstream of the compile -- the bias
half-life, the climatology blend, the spread cap, the weights -- has only ever
been defensible on residual metrics.  This session measured what that is
worth: a layer that improved the fleet's mean residual 24% cost the book 7.3
points of return, and the sign was only visible on the book.

What this does: take a packet exactly as production was handed it, hand it to
production's own ``compile_native_ensemble_p0``, and emit the envelope.  Run
it twice with one constant changed and the two envelopes are a controlled
pair -- same evidence, same compiler, one difference.

What it deliberately does not do: rebuild the packet.  A packet reconstructed
from the member tables was verified this session to carry identical members
(CYYZ 2026-08-27, four systems, 142 members, exact), but a packet is more
than its members -- bucket contracts, availability evidence, per-layer
hashes -- and rebuilding those is rebuilding the collector.  Packets are
retained instead, by ``forward_packet_retention_sweep``.  The cost is that
this lane can only score days from the day retention started; the benefit is
that what it scores is what production actually compiled.

What is still missing before an upstream constant can be scored on the book,
stated here because the ``--set-constant`` flag would otherwise imply
otherwise: a constant that lives in the *fit* does not reach a *recompile*.
``BIAS_HALF_LIFE_DAYS`` produces ``bias_c``, ``bias_c`` is baked into a stored
artifact, and the compiler reads that artifact rather than refitting it.  So
setting it here changes nothing.  The flag is real for constants the compiler
itself reads at serve time -- the centre/width entitlement bounds, the
regime shift caps, the spread clamp it validates against -- and inert for
everything the trainer decided earlier.  Closing that half needs
counterfactual artifacts fitted under the constant and made visible to the
compiler, which is the next piece of this lane.

Fidelity, measured 2026-08-27 on CYYZ 2026-08-27 at the true historical
decision instant: the recompiled distribution tracks the served one to
6.5e-3 per bucket, and the residual difference is artifact drift rather than
replay error -- 1,461 artifacts were valid at that decision against 1,717
now, so the recompile legitimately reads a later generation.  Pinning the
artifact set is the same missing piece.

Usage:
  p0_recompile_replay.py --event-date 2026-08-27 [--station CYYZ]
      --decision-at-utc 2026-08-26T06:22:01.413Z
      [--set-constant native_ensemble_p0.SOME_SERVE_TIME_CONSTANT=1.0]
      [--out tmp/recompile_baseline.json]

  The decision instant matters: the compiler enforces as-of itself, refusing
  a capture taken after it (``native_ensemble_p0_future_capture_forbidden``)
  and a snapshot older than its freshness window
  (``native_ensemble_snapshot_stale``).  Neither guard is reimplemented here.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.native_ensemble_p0 import (  # noqa: E402
    compile_native_ensemble_p0,
)

ARCHIVE = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "forward_packet_archive"
)
DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def _apply_constants(assignments: list[str]) -> list[tuple]:
    """Set module constants for this run, returning how to undo them.

    Constants rather than a config object because that is what these are: the
    parameters under test are module-level names the fit and the compiler read
    directly, and an arm that reached them any other way would not be testing
    the code production runs.
    """

    import importlib

    undo = []
    for assignment in assignments:
        target, _, raw = assignment.partition("=")
        module_name, _, attribute = target.strip().rpartition(".")
        if not module_name or not attribute:
            raise SystemExit(f"bad --set-constant: {assignment}")
        module = importlib.import_module(
            f"bayes_basket_target.{module_name}"
            if not module_name.startswith("bayes_basket_target")
            else module_name
        )
        if not hasattr(module, attribute):
            raise SystemExit(
                f"{module.__name__} has no {attribute}"
            )
        previous = getattr(module, attribute)
        try:
            value = type(previous)(raw)
        except (TypeError, ValueError):
            value = raw
        setattr(module, attribute, value)
        undo.append((module, attribute, previous))
        print(f"  set {module.__name__}.{attribute} = {value!r}"
              f" (was {previous!r})")
    return undo


def _load_packets(event_date: str, station: str | None) -> list[dict]:
    day_dir = ARCHIVE / event_date
    if not day_dir.is_dir():
        raise SystemExit(
            f"no retained packets for {event_date};"
            f" retention began later than this day"
        )
    packets = []
    for path in sorted(day_dir.glob("*.json.gz")):
        if station and not path.name.startswith(f"{station}-"):
            continue
        with gzip.open(path, "rb") as handle:
            packets.append(json.loads(handle.read().decode("utf-8")))
    return packets


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--station")
    parser.add_argument("--decision-at-utc")
    parser.add_argument(
        "--set-constant", action="append", default=[],
        help="module.NAME=value, applied for this run only",
    )
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--band-axis",
        choices=("claim", "centre_distance"),
        help=(
            "Recompile with a band table fitted on this axis instead of the"
            " one the artifact pins (charter 105).  Fitted read-only from the"
            " live sample at run time and handed to the compiler as"
            " (factors, sha, axis); nothing is persisted and nothing is"
            " activated.  Omit for the served table, which is the baseline"
            " arm."
        ),
    )
    args = parser.parse_args()

    undo = _apply_constants(args.set_constant)
    packets = _load_packets(args.event_date, args.station)
    print(f"  retained packets: {len(packets)}")

    decision = args.decision_at_utc or (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60.0)
    conn.execute("PRAGMA query_only=ON")
    # The counterfactual arm's table, fitted here rather than read from the
    # store: activating one to score it is the thing this lane exists to
    # avoid.  The connection is read-only and query_only, so a fit that
    # tried to persist would fail loudly rather than quietly changing what
    # production serves.
    band_table_override = None
    if args.band_axis:
        from bayes_basket_target.band_reliability_store import (  # noqa: PLC0415
            band_table_sha256,
            fit_band_reliability_table,
        )

        fitted = fit_band_reliability_table(
            conn, stage="P0", axis=args.band_axis,
        )
        band_table_override = (
            fitted["factors"],
            band_table_sha256(
                fitted["factors"], stage="P0", axis=args.band_axis,
            ),
            args.band_axis,
        )
        live = sum(
            1 for _band, factor in fitted["factors"]
            if abs(factor - 1.0) > 1e-9
        )
        print(
            f"  band arm: axis={args.band_axis} cells={len(fitted['factors'])}"
            f" live={live} sha={band_table_override[1][:12]}"
        )
    results: list[dict] = []
    failures: list[dict] = []
    try:
        for packet in packets:
            station_id = str(packet.get("station_id") or "")
            try:
                compiled = compile_native_ensemble_p0(
                    conn,
                    forward_packet=packet,
                    decision_at_utc=decision,
                    band_table_override=band_table_override,
                )
            except Exception as exc:  # noqa: BLE001
                failures.append({
                    "station_id": station_id,
                    "reason": f"{type(exc).__name__}: {exc}"[:220],
                })
                continue
            # The compiler returns the envelope under ``forward_prediction``
            # with its maps as JSON text, which is how they reach the table.
            prediction = compiled.get("forward_prediction") or {}
            try:
                probabilities = json.loads(
                    str(prediction.get("probabilities_json") or "{}")
                )
                centers = json.loads(
                    str(prediction.get("bucket_centers_c_json") or "{}")
                )
            except (TypeError, ValueError):
                probabilities, centers = {}, {}
            mean_c = None
            if probabilities and centers:
                total = sum(float(v) for v in probabilities.values())
                if total > 0:
                    mean_c = sum(
                        float(centers[k]) * float(v)
                        for k, v in probabilities.items()
                        if k in centers
                    ) / total
            results.append({
                "station_id": station_id,
                "event_date": str(packet.get("event_date") or ""),
                "probabilities": {
                    str(k): float(v) for k, v in probabilities.items()
                },
                "distribution_mean_c": mean_c,
                "content_sha256": prediction.get("content_sha256"),
                "season_bin": prediction.get("season_bin"),
            })
    finally:
        conn.close()
        for module, attribute, previous in reversed(undo):
            setattr(module, attribute, previous)

    print(f"  compiled {len(results)}, failed {len(failures)}")
    for failure in failures[:8]:
        print(f"    {failure['station_id']}: {failure['reason']}")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "event_date": args.event_date,
                    "decision_at_utc": decision,
                    "constants": args.set_constant,
                    "compiled": results,
                    "failures": failures,
                },
                ensure_ascii=False, sort_keys=True,
            ) + "\n",
            encoding="utf-8",
        )
        print(f"  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
