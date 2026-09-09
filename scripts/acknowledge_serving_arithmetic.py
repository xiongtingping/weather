"""Say that a stage's current serving fingerprint is inert.

The watchdog (``serving_arithmetic.watchdog``) lights when a stage's code
fingerprint was stamped after the version it serves started serving, and its
docstring has always said the only person who can tell an inert refactor from
a serving change is whoever wrote the commit.  Until 2026-09-08 there was no
place for that person to say it, so the lamp had been on for every stage on
every day since the mechanism landed.  This is that place.

    python3 scripts/acknowledge_serving_arithmetic.py --stage P0 \
        --reason "fingerprint scheme moved to call closure; served shape unchanged"

A real serving change is answered by bumping the stage's posterior_version,
never by acknowledging here: an acknowledgement asserts the version still
describes what is served, and a wrong one lets the calibration lanes keep
training on a shape nobody serves -- the exact failure the watchdog exists to
name.  The reason is required and is the audit's record.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target import serving_arithmetic  # noqa: E402
# What counts as "the served distribution moved" across a fingerprint change.
# Charter 86 -- the change this whole mechanism failed to catch -- moved P0's
# median served sigma by 30% (1.13 -> 0.72 bucket steps over two days) and its
# median centre by more than a fifth of a bucket.  A comment rewrite, a receipt
# field, an inert replay door move neither.  Below both thresholds the change
# is judged inert and acknowledged with the measurement as its reason; above
# either, a bump is owed and the line says by how much.
SHAPE_SHIFT_SIGMA_FRACTION = 0.10
SHAPE_SHIFT_CENTRE_BUCKETS = 0.20
# Bounded on purpose: this rides a daily sweep, and the question is whether
# the fleet's shape moved, which a few hundred vectors either side answers as
# well as every vector would.
# Hours either side, not days: a serving change is a step at the moment the
# code lands, and a window wide enough to hold several days of ordinary drift
# reports the drift instead.
SHAPE_SHIFT_WINDOW_HOURS = 12

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def _served_shape(
    conn: sqlite3.Connection, *, stage: str, lo: str, hi: str
) -> dict[str, Any] | None:
    """Median served width and centre over one side of a fingerprint change.

    Read off the envelopes themselves rather than any receipt: the question
    is what the fleet was actually handed, and a receipt records what a layer
    intended to hand it.
    """

    # Read the width the plan already wrote down rather than recomputing it
    # off the envelope bodies.  Two reasons, both learned the hard way on
    # 2026-09-09: the envelope table has no index this predicate can use and
    # the first cut took seven minutes a run, and a bounded LIMIT over an
    # unordered scan sampled whichever days the scan reached first -- it
    # compared 09-05 against 09-09 and called six days of drift a step.
    # ``sigma_buckets`` reaches plans from 2026-09-08, so a fingerprint
    # older than that has no sample and is reported as unjudgeable rather
    # than judged on a number that is not there.
    try:
        rows = conn.execute(
            "SELECT json_extract(plan_json,"
            "'$.compiled_plan.plan.skew_reading.sigma_buckets'),"
            " json_extract(plan_json,"
            "'$.compiled_plan.plan.skew_reading.mean_ordinal')"
            " FROM basket_p0_preselection_plan"
            " WHERE json_extract(plan_json,'$.compiled_plan.stage')=?"
            " AND created_at>=? AND created_at<?",
            (str(stage), str(lo), str(hi)),
        ).fetchall()
    except sqlite3.Error:
        return None
    sigmas: list[float] = []
    centres: list[float] = []
    for sigma, centre in rows:
        if sigma is None or centre is None:
            continue
        sigmas.append(float(sigma))
        centres.append(float(centre))
    if len(sigmas) < 20:
        return None
    return {
        "n": len(sigmas),
        "sigma": statistics.median(sigmas),
        "centre": statistics.median(centres),
    }


def _shape_shift(
    conn: sqlite3.Connection, *, stage: str, since: str
) -> dict[str, Any] | None:
    """Did the served shape move when the fingerprint did?"""

    try:
        moment = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    span = timedelta(hours=SHAPE_SHIFT_WINDOW_HOURS)

    def stamp(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")

    before = _served_shape(
        conn, stage=stage, lo=stamp(moment - span), hi=stamp(moment)
    )
    after = _served_shape(
        conn, stage=stage, lo=stamp(moment), hi=stamp(moment + span)
    )
    if not before or not after:
        return None
    sigma_fraction = (
        abs(after["sigma"] - before["sigma"]) / before["sigma"]
        if before["sigma"] > 0 else 0.0
    )
    centre_shift = abs(after["centre"] - before["centre"])
    moved = (
        sigma_fraction > SHAPE_SHIFT_SIGMA_FRACTION
        or centre_shift > SHAPE_SHIFT_CENTRE_BUCKETS
    )
    return {
        "before": before, "after": after, "moved": moved,
        "sigma_fraction": sigma_fraction, "centre_shift": centre_shift,
        "text": (
            f"服务 σ {before['sigma']:.2f}→{after['sigma']:.2f} 格"
            f"（{sigma_fraction:+.0%}）、中心移 {centre_shift:.2f} 格"
            f"（前后各 {before['n']}/{after['n']} 条）"
        ),
    }


def _audit(db: Path) -> int:
    """One line per stage: is a version bump owed, and since when.

    This is the other half of the watchdog's sentence reaching a person on a
    schedule.  Measured 2026-09-08, the discrepancy had been true for every
    stage on every day since the mechanism landed (P0: seventeen fingerprints
    against one version) and was visible only to someone who went looking --
    which is how charter 86 changed P0's served width on 2026-09-06 and its
    posterior_version did not move.  Now it lands in the daily receipt.

    An acknowledged fingerprint prints as settled: the lamp going out is the
    point of the acknowledgement, and a line that shouted anyway would be the
    always-on lamp again.
    """

    pending: list[tuple[str, str, str]] = []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for stage in ("P0", "P0-M", "P1"):
            try:
                reading = serving_arithmetic.watchdog(conn, stage=stage)
            except sqlite3.Error as exc:
                print(f"SUMMARY {stage}: 看门狗读不到 [{type(exc).__name__}]")
                continue
            fingerprint = str(reading.get("fingerprint") or "")[:10]
            ack = reading.get("acknowledged")
            if ack:
                print(
                    f"SUMMARY {stage}: 已确认为惰性改动 — 指纹 {fingerprint}"
                    f"，理由「{ack['reason']}」（{ack['acknowledged_at_utc']}）"
                )
            elif reading.get("discrepancy"):
                shift = _shape_shift(
                    conn, stage=stage,
                    since=str(reading.get("fingerprint_since_utc") or ""),
                )
                if shift is None:
                    print(
                        f"SUMMARY {stage}: 指纹 {fingerprint} 晚于它带的 "
                        f"{reading.get('served_version')}，但两侧样本不足，"
                        "判不了是不是惰性 — 人来看"
                    )
                elif shift["moved"]:
                    print(
                        f"SUMMARY {stage}: **欠一次版本 bump** — 指纹 "
                        f"{fingerprint} 自 "
                        f"{reading.get('fingerprint_since_utc')} 起服务，"
                        f"而 {reading.get('served_version')} 在那之前就在服务；"
                        f"跨这一刻 {shift['text']}，形状确实动了。"
                    )
                else:
                    print(
                        f"SUMMARY {stage}: 指纹 {fingerprint} 变了但形状没动"
                        f"（{shift['text']}）— 自动记为惰性"
                    )
                    pending.append((stage, fingerprint, shift["text"]))
            else:
                print(
                    f"SUMMARY {stage}: 版本与服务算术一致 — 指纹 {fingerprint}"
                )
    finally:
        conn.close()
    # Written after the read-only pass, on its own connection: an
    # acknowledgement is a judgement about a measurement, and doing it here
    # rather than by hand is the difference between a lamp that means
    # something and one that is on every morning.  A bump is never automatic
    # -- only the finding that none is owed is.
    for stage, fingerprint, text in pending:
        writer = sqlite3.connect(str(db), timeout=120.0)
        try:
            serving_arithmetic.acknowledge(
                writer, stage=stage,
                reason=f"served shape unchanged across the fingerprint: {text}",
            )
            writer.commit()
        except (sqlite3.Error, ValueError) as exc:
            print(f"SUMMARY {stage}: 自动确认写不进去 [{type(exc).__name__}: {exc}]")
        finally:
            writer.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    # Not required at the parser level: ``--audit`` asks about every stage
    # and names no reason.  Acknowledging still demands both, checked below,
    # because an acknowledgement without a reason is an unsigned judgement.
    parser.add_argument("--stage", choices=("P0", "P0-M", "P1"))
    parser.add_argument("--reason")
    parser.add_argument(
        "--fingerprint", default=None,
        help="a specific fingerprint sha256; default: the one served now",
    )
    parser.add_argument("--show", action="store_true",
                        help="print the watchdog reading and exit")
    parser.add_argument(
        "--audit", action="store_true",
        help=(
            "report every stage and exit 0.  Reported, never enforced: the"
            " 2026-08-27 ruling demoted this from a boundary precisely"
            " because a bound that moves on every commit costs a layer all"
            " its evidence, and a check that failed the sweep would be that"
            " bound again wearing an exit code."
        ),
    )
    args = parser.parse_args()
    if args.audit:
        return _audit(args.db)
    if not args.stage:
        parser.error("--stage is required unless --audit")
    if not args.show and not args.reason:
        parser.error("--reason is required to acknowledge")
    conn = sqlite3.connect(str(args.db), timeout=120.0)
    try:
        before = serving_arithmetic.watchdog(conn, stage=args.stage)
        if args.show:
            print(json.dumps(before, ensure_ascii=False, indent=1))
            return 0
        receipt = serving_arithmetic.acknowledge(
            conn, stage=args.stage, reason=args.reason,
            fingerprint_sha256=args.fingerprint,
        )
        conn.commit()
        after = serving_arithmetic.watchdog(conn, stage=args.stage)
    finally:
        conn.close()
    print(json.dumps({
        "receipt": receipt,
        "discrepancy_before": before.get("discrepancy"),
        "discrepancy_after": after.get("discrepancy"),
    }, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
