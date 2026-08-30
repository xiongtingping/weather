"""Is there yet an event day on which H15 vs H5 can be scored on the book?

The closed loop exists -- retained packet, recompile under a counterfactual
artifact parameter set, replay database, ledger -- but it cannot score a day
unless three things are true of that day at once, and on 2026-08-27 none of
them were.  This says when they are, and refuses to guess.

  1. SETTLED.  The ledger scores closed books; an unsettled day has no
     outcome to score.
  2. DECIDED AFTER RETENTION BEGAN.  Packet retention started 2026-08-27; a
     day whose P0 decisions predate it has no packet to recompile from, and
     the compiler correctly refuses the later packets that do exist with
     ``native_ensemble_p0_future_capture_forbidden``.
  3. SERVED UNDER H5.  Artifacts refit by station rotation at roughly
     thirteen scopes a round, so the fleet takes about 2.6 days to turn over.
     A day served mostly by H15-fitted artifacts is not a test of H5.

Reporting only.  It does not start the extraction: that is several gigabytes
written unattended, and doing it without first checking free disk is how the
resident's storage guard got tripped on 2026-08-27.  The command to run is
printed when the day arrives, for a person to run while watching.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "bayes_basket_target_ssot.db"
ARCHIVE = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "forward_packet_archive"
)
# The change under test, and the instant it began being fitted.  Both are
# facts about a past deployment, so they are stated rather than derived.
H5_FROM_UTC = "2026-08-27T06:48:00Z"
# What the extraction of a five-day window measured, plus room to work in.
NEEDED_FREE_GIB = 6.0


def _retention_start() -> str | None:
    stamps = [p.stat().st_mtime for p in ARCHIVE.rglob("*.json.gz")]
    if not stamps:
        return None
    return datetime.fromtimestamp(
        min(stamps), tz=timezone.utc
    ).isoformat().replace("+00:00", "Z")


def main() -> int:
    if not DB.is_file():
        print("SSOT missing; nothing to check")
        return 0
    retention = _retention_start()
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60.0)
    conn.execute("PRAGMA query_only=ON")
    report: dict[str, object] = {
        "retention_started_utc": retention,
        "h5_fitting_from_utc": H5_FROM_UTC,
        "free_disk_gib": round(
            shutil.disk_usage(str(ROOT)).free / 2 ** 30, 1
        ),
        "days": [],
        "ready_days": [],
    }
    try:
        # Fingerprints, read once: which artifacts exist at all, and which
        # of them were fitted under H5.
        known_shas = {
            str(row[0]) for row in conn.execute(
                "SELECT content_sha256"
                " FROM calibration_native_ensemble_system_artifact"
            )
        }
        h5_shas = {
            str(row[0]) for row in conn.execute(
                "SELECT content_sha256"
                " FROM calibration_native_ensemble_system_artifact"
                " WHERE valid_from_utc>=?",
                (H5_FROM_UTC,),
            )
        }
        settled = [
            str(row[0]) for row in conn.execute(
                "SELECT DISTINCT event_date FROM paper_trade_settlement"
                " WHERE event_date >= ? ORDER BY event_date",
                (str(retention or "2026-08-27")[:10],),
            )
        ]
        for day in settled:
            earliest, latest, total = conn.execute(
                "SELECT MIN(decision_at_utc), MAX(decision_at_utc),"
                " COUNT(*) FROM basket_probability_bridge_history"
                " WHERE event_date=? AND stage='P0'",
                (day,),
            ).fetchone()
            if not earliest:
                continue
            after_retention = conn.execute(
                "SELECT COUNT(*) FROM basket_probability_bridge_history"
                " WHERE event_date=? AND stage='P0' AND decision_at_utc>=?",
                (day, retention or H5_FROM_UTC),
            ).fetchone()[0]
            # How much of what served that day had been refit under H5.
            #
            # Asked of the DECISIONS, not of the artifact pool.  Each P0
            # envelope records the artifact fingerprint that served each of
            # its systems (``system_artifact_shas_json``), so the question
            # has an exact answer and needs no proxy.  Two proxies were
            # tried and both lied: counting every artifact whose validity
            # window covers the instant understated coverage three-fold
            # (artifacts carry a thirty-day window and the fleet refits
            # daily, so a scope holds ~21 superseded generations at once --
            # CYYZ JJA/h00_12/00Z alone had 634 -- while production reads
            # exactly the one its highest-generation activation names), and
            # ranking by that activation fixed the denominator but still
            # measured the pool at ``latest``, which for a just-closed day
            # is now, not then.  Measured 2026-08-30, the three answers for
            # 2026-08-29 were 18%, 52%, and the true 97%.
            #
            # A day is served under H5 when every calibrated member of
            # every decision came from an H5-fitted artifact.  Provisional
            # members are excluded rather than counted against it: they
            # have no artifact of their own by design -- they enter at a
            # certified weight borrowing their station's calibrated peers'
            # parameters -- so their fingerprints are not in the artifact
            # table and belong to neither half-life.
            provisional_only = 0
            decisions = 0
            served_under_h5 = 0
            for (lineage_json,) in conn.execute(
                "SELECT lineage_json FROM basket_probability_envelope"
                " WHERE stage='P0' AND event_date=? AND state='COMMITTED'",
                (day,),
            ):
                try:
                    lineage = json.loads(lineage_json)
                except (TypeError, ValueError):
                    continue
                raw = lineage.get("system_artifact_shas_json")
                if not raw:
                    continue
                try:
                    shas = json.loads(raw) if isinstance(raw, str) else raw
                except (TypeError, ValueError):
                    continue
                if not isinstance(shas, dict) or not shas:
                    continue
                calibrated = [
                    value for value in shas.values() if value in known_shas
                ]
                if not calibrated:
                    provisional_only += 1
                    continue
                decisions += 1
                if all(value in h5_shas for value in calibrated):
                    served_under_h5 += 1
            h5_share = served_under_h5
            all_valid = decisions
            packets = (ARCHIVE / day)
            retained = (
                sum(1 for _ in packets.glob("*.json.gz"))
                if packets.is_dir() else 0
            )
            covered = after_retention / total if total else 0.0
            share = h5_share / all_valid if all_valid else 0.0
            entry = {
                "event_date": day,
                "p0_decisions": total,
                "decided_after_retention": after_retention,
                "retention_coverage": round(covered, 3),
                "retained_packets": retained,
                "h5_artifact_share": round(share, 3),
                "calibrated_decisions": all_valid,
                "calibrated_decisions_h5": h5_share,
                "provisional_only_decisions": provisional_only,
                "ready": bool(
                    covered >= 0.9 and retained > 0 and share >= 0.9
                ),
            }
            report["days"].append(entry)
            if entry["ready"]:
                report["ready_days"].append(day)
    finally:
        conn.close()

    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=1))
    ready = report["ready_days"]
    free = report["free_disk_gib"]
    print()
    if not ready:
        blockers = []
        for entry in report["days"][-3:]:
            reasons = []
            if entry["retention_coverage"] < 0.9:
                reasons.append(
                    f"只有 {100*entry['retention_coverage']:.0f}% 的决策在留档之后"
                )
            if not entry["retained_packets"]:
                reasons.append("无留档包")
            if entry["h5_artifact_share"] < 0.9:
                reasons.append(
                    f"H5 artifact 只占 {100*entry['h5_artifact_share']:.0f}%"
                )
            if reasons:
                blockers.append(
                    f"  {entry['event_date']}: " + "，".join(reasons)
                )
        print("尚无可打分的事件日。最近三天的阻断：")
        print("\n".join(blockers) or "  (无已结算日)")
        return 0

    print(f"✅ 可打分的事件日: {', '.join(ready)}")
    if free < NEEDED_FREE_GIB:
        print(
            f"⚠️ 空闲磁盘 {free} GiB 少于抽取所需的 {NEEDED_FREE_GIB} GiB；"
            "先清理再跑，不要无人值守地抽取"
        )
        return 0
    since = min(ready)
    print(f"空闲磁盘 {free} GiB，够跑。按顺序执行（约 12 分钟，看着跑）：")
    print(
        f"  python3 scripts/build_ledger_replay_db.py"
        f" --since {since} --out data/runtime/ledger_replay/replay.db"
    )
    print(
        "  python3 scripts/paper_trading_ledger.py"
        " --db data/runtime/ledger_replay/replay.db"
        " --backfill --rebuild --report   # H5 臂（现役）"
    )
    for day in ready:
        print(
            f"  python3 scripts/inject_counterfactual_envelopes.py"
            f" --db data/runtime/ledger_replay/replay.db"
            f" --event-date {day} --bias-half-life 15.0"
        )
    print(
        "  python3 scripts/paper_trading_ledger.py"
        " --db data/runtime/ledger_replay/replay.db"
        " --backfill --rebuild --report   # H15 反事实臂"
    )
    print("  rm -rf data/runtime/ledger_replay   # 用完即删")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
