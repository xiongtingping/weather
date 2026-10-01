#!/usr/bin/env python3
"""Charter 242: fit the P0 fusion layer's station x model weights, daily.

Once per UTC day (the hourly sweep skips once today's fit exists).  Reads
``station_model_weight_fact`` (replay backfill before 2026-10-01, P0
envelopes after) from data/fusion_blend_training.db, and appends, as of now:

* one ``station_model_weight_params`` row per station -- the station's
  log-score-optimal mixture logits on every fact whose truth is known, the
  station-days each system was scored on, and the fleet's tau
  (``fusion_blend.choose_tau``);
* one ``station_model_weight_veto`` row -- served against the seat on the
  decisions this rule has served (``fusion_blend.veto_evidence``).

Serving reads both as of the decision.  Never opens the SSOT; writes only the
training file, append-only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import fusion_blend as FB  # noqa: E402


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha(payload: object) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def run(db: Path, *, now: datetime, dry_run: bool, force: bool = False) -> dict[str, object]:
    started = time.monotonic()
    conn = sqlite3.connect(db, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    for statement in FB.TRAINING_DDL + FB.STATION_WEIGHT_DDL:
        conn.execute(statement)
    conn.commit()
    conn.row_factory = sqlite3.Row
    # Once per UTC day: the hourly sweep only lands new facts, and every fit
    # appends 45 rows that serving parses whenever the file moves.
    latest = conn.execute(
        "SELECT MAX(computed_at_utc) FROM station_model_weight_params WHERE rule_version=?",
        (FB.FUSION_BLEND_RULE_VERSION,),
    ).fetchone()[0]
    if latest and str(latest)[:10] == iso(now)[:10] and not force:
        conn.close()
        return {"skipped": "already_fitted_today", "latest_computed_at_utc": latest}
    facts = [FB.WeightFact(row) for row in conn.execute("SELECT * FROM station_model_weight_fact")]
    systems = sorted({system for fact in facts for system in fact.pool})
    tau, tau_evidence = FB.choose_tau(facts, systems, now)
    by_station: dict[str, list[FB.WeightFact]] = {}
    for fact in facts:
        by_station.setdefault(fact.station, []).append(fact)
    computed_at = iso(now)
    written = 0
    for station, rows in sorted(by_station.items()):
        known = FB.knowable_facts(rows, now, strict=False)
        if not known:
            continue
        theta = FB.fit_theta(known, systems)
        days = FB.days_by_system(known)
        facts_sha = sha([(f.event_date, f.horizon_bin, f.revision) for f in known])
        body = {
            "station_id": station, "computed_at_utc": computed_at,
            "rule_version": FB.FUSION_BLEND_RULE_VERSION, "tau": tau,
            "theta_json": json.dumps({k: round(v, 12) for k, v in theta.items()}, sort_keys=True),
            "days_by_system_json": json.dumps(days, sort_keys=True),
            "fact_count": len(known), "facts_sha256": facts_sha,
            "tau_evidence_json": json.dumps(tau_evidence, sort_keys=True),
            "schema": FB.STATION_WEIGHT_PARAMS_SCHEMA,
        }
        content = sha(body)
        row = {"params_id": "smw-" + content[:24], **body, "content_sha256": content}
        if not dry_run:
            conn.execute(
                "INSERT OR IGNORE INTO station_model_weight_params (" + ",".join(row)
                + ") VALUES (" + ",".join("?" * len(row)) + ")", tuple(row.values()),
            )
        written += 1
    veto = FB.veto_evidence(facts, now)
    veto_body = {
        "computed_at_utc": computed_at, "rule_version": FB.FUSION_BLEND_RULE_VERSION,
        "vetoed": int(bool(veto["vetoed"])), "evidence_json": json.dumps(veto, sort_keys=True),
        "schema": FB.STATION_WEIGHT_VETO_SCHEMA,
    }
    veto_content = sha(veto_body)
    if not dry_run:
        conn.execute(
            "INSERT OR IGNORE INTO station_model_weight_veto (veto_id, computed_at_utc, rule_version,"
            " vetoed, evidence_json, schema, content_sha256) VALUES (?,?,?,?,?,?,?)",
            ("smv-" + veto_content[:24], veto_body["computed_at_utc"], veto_body["rule_version"],
             veto_body["vetoed"], veto_body["evidence_json"], veto_body["schema"], veto_content),
        )
        conn.commit()
    conn.close()
    return {
        "facts": len(facts), "systems": systems, "tau": tau,
        "tau_window_decisions": tau_evidence.get("window_decisions"),
        "params_rows": written, "veto": {k: veto[k] for k in ("station_days", "event_days", "mean", "ci", "vetoed")},
        "seconds": round(time.monotonic() - started, 1), "dry_run": dry_run,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(FB.FUSION_BLEND_TRAINING_DB))
    parser.add_argument("--as-of", help="ISO instant to fit as of (default: now)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="fit even if today already has params")
    args = parser.parse_args()
    now = FB._stamp(args.as_of) if args.as_of else datetime.now(timezone.utc)
    print(json.dumps(run(Path(args.db), now=now, dry_run=args.dry_run, force=args.force), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
