#!/usr/bin/env python3
"""Read-only, bounded evidence gate for the frozen Proper edge protocol.

Historical eligibility is not a probability backtest. Current-version labels,
exact member receipts and confirmed execution are reported independently.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.strict_replay_clock import (EvidenceClockError, case_truth_known, day_end,
                                        exact_member_arrival, instant, select_truth_asof)

SYSTEMS = {"ECMWF_IFS_ENS_OPEN_PF50", "ECMWF_AIFS_ENS_GLOBAL", "DWD_ICON_GLOBAL_EPS",
           "NOAA_GEFS_05", "ECCC_GEM_GLOBAL_ENS", "NOAA_AIGEFS_025"}
HERE = ROOT / "docs/research/proper_strict_validation_20261003_codex"


class Budget:
    def __init__(self, seconds=180):
        self.started = time.monotonic()
        self.seconds = seconds
        self.query_until = self.started + 20
        self.bytes = 0
        self.rows = 0
        self.peak_rss = 0
        self.error = None
        resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024**2, 32 * 1024**2))
        self.probe()
        threading.Thread(target=self.watch, daemon=True).start()

    def probe(self):
        rss = int(subprocess.check_output(["ps", "-o", "rss=", "-p", str(os.getpid())], text=True, timeout=2)) * 1024
        self.peak_rss = max(self.peak_rss, rss)
        if rss > 1536 * 1024**2 or shutil.disk_usage(HERE).free < 2 * 1024**3 or time.monotonic() - self.started > self.seconds:
            raise EvidenceClockError("resource_budget_exceeded")

    def watch(self):
        while True:
            try:
                self.probe()
            except Exception as exc:
                os.write(2, f"resource probe failed: {exc}\n".encode())
                os._exit(77)
            time.sleep(.5)

    def account(self, row):
        if row is not None:
            size = sum(len(v) if isinstance(v, (str, bytes)) else 16 for v in row)
            self.bytes += size
            self.rows += 1
            if size > 4 * 1024**2 or self.bytes > 512 * 1024**2 or self.rows > 1_000_000:
                raise EvidenceClockError("query_result_budget_exceeded")
        return row


class Cursor(sqlite3.Cursor):
    def execute(self, *args, **kwargs):
        self.connection.budget.query_until = time.monotonic() + 20
        return super().execute(*args, **kwargs)

    def fetchone(self):
        return self.connection.budget.account(super().fetchone())

    def fetchall(self):
        rows = []
        while (row := self.fetchone()) is not None:
            rows.append(row)
        return rows

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row


class Connection(sqlite3.Connection):
    def execute(self, *args, **kwargs):
        return self.cursor(factory=Cursor).execute(*args, **kwargs)


def connect(path, budget):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None, timeout=2, factory=Connection)
    conn.budget = budget
    conn.row_factory = sqlite3.Row
    conn.set_progress_handler(lambda: int(time.monotonic() > budget.query_until), 1000)
    conn.execute("PRAGMA query_only=1")
    conn.execute("PRAGMA cache_size=-8000")
    return conn


def write(path, data):
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if len(text.encode()) > 16 * 1024**2:
        raise EvidenceClockError("output_budget_exceeded")
    partial = path.with_suffix(".partial")
    partial.write_text(text)
    partial.replace(path)


def prepare(protocol, budget):
    source = ROOT / "docs/research/proper_conditional_edge_20261003/decisions.jsonl.gz"
    first = {}
    keys = set()
    total = 0
    with gzip.open(source, "rt") as fh:
        for line in fh:
            total += len(line)
            if len(line) > 2 * 1024**2 or total > 256 * 1024**2:
                raise EvidenceClockError("input_budget_exceeded")
            row = json.loads(line)
            keys.add((row["st"], row["d"]))
            if row["stage"] == "P0":
                key = (row["st"], row["d"])
                if key not in first or instant(row["t"]) < instant(first[key]["t"]):
                    first[key] = {k: row[k] for k in ("st", "d", "t", "h", "tz", "pv")}
    stations = sorted({k[0] for k in keys})
    if len(stations) != protocol["scope"]["stations"]:
        raise EvidenceClockError("station_universe_mismatch")
    out = {"stations": stations, "selection_exposed_station_days": sorted(keys), "first_p0": list(first.values()),
           "input_sha256": hashlib.file_digest(source.open("rb"), "sha256").hexdigest()}
    write(HERE / "input_index.json", out)
    print(json.dumps({"stations": len(stations), "selection_exposed_station_days": len(keys), "first_p0": len(first)}))


def execution(conn, station, cutoff):
    output = []
    evaluations = conn.execute("SELECT * FROM proper_bet_lane_evaluation WHERE station_id=? ORDER BY event_date", (station,)).fetchall()
    for e in evaluations:
        if not e["plan_json"] or instant(e["evaluated_at_utc"]) > cutoff:
            continue
        plan = json.loads(e["plan_json"])
        label = conn.execute("SELECT * FROM basket_station_day_settlement_label WHERE station_id=? AND event_date=? ORDER BY truth_revision_no DESC", (station, e["event_date"])).fetchall()
        labels = [r for r in label if instant(r["known_at_utc"]) <= cutoff]
        settled = labels[0]["settled_bucket"] if labels else None
        intents = []
        seen = set()
        for legkey, leg in json.loads(e["legs_state_json"] or "{}").items():
            for iid in leg.get("intents", []):
                if iid in seen:
                    raise EvidenceClockError("duplicate_intent_in_plan")
                seen.add(iid)
                r = conn.execute("SELECT i.side,i.bucket,i.requested_size,i.limit_price,i.requested_notional_usdc,i.created_at,i.state AS intent_state,r.state AS reconciliation_state,r.filled_size,r.filled_notional_usdc,r.updated_at FROM basket_target_order_intents i LEFT JOIN basket_target_order_reconciliations r USING(order_intent_id) WHERE i.order_intent_id=?", (iid,)).fetchone()
                if r is None:
                    intents.append({"intent_id": iid, "evidence": "missing_intent"})
                    continue
                row = dict(r)
                row["intent_id"] = iid
                if instant(row["created_at"]) > cutoff:
                    raise EvidenceClockError("intent_after_cutoff")
                filled = row["filled_size"]
                if row["updated_at"] and instant(row["updated_at"]) > cutoff:
                    row["evidence"] = "reconciliation_after_cutoff"
                elif row["reconciliation_state"] not in {"resolved_filled", "resolved_partial", "resolved_cancelled", "resolved_not_placed"}:
                    row["evidence"] = "unresolved"
                else:
                    row["evidence"] = "confirmed"
                row["settled_pnl_before_unrecorded_fees"] = None
                if row["evidence"] == "confirmed" and filled is not None and filled > 0 and settled is not None:
                    hit = row["bucket"] == settled
                    payout = filled * (int(not hit) if row["side"] == "NO" else int(hit))
                    row["settled_pnl_before_unrecorded_fees"] = payout - row["filled_notional_usdc"]
                intents.append(row)
        output.append({"station": station, "date": e["event_date"], "settled_bucket": settled,
                       "lambda_shares": e["lambda_shares"], "budget_usdc": e["budget_usdc"],
                       "need_full_usdc": plan.get("need_full_usdc"), "capped_by_cash": plan.get("capped_by_cash"),
                       "planned_legs": e["planned_legs"], "plan_legs": [{k: v for k, v in l.items() if k in {"side", "bucket", "target_shares", "price", "s", "planned_price", "min_size"}} for l in plan["legs"]],
                       "intents": intents})
    return output


def station_run(conn, station, protocol, index):
    from bayes_basket_target import native_ensemble_oos as oos
    cutoff = instant(protocol["frozen_at_utc"])
    cutoff_s = cutoff.isoformat().replace("+00:00", "Z")
    # Probe actual stored domains before interpreting similarly named columns.
    domains = {"truth_lanes": [r[0] for r in conn.execute("SELECT DISTINCT source_lane FROM calibration_station_daily_high_truth WHERE station_id=?", (station,))],
               "member_scope": [list(r) for r in conn.execute("SELECT DISTINCT ensemble_system_id,archive_lead_bucket,run_cycle,transport_provider_id FROM calibration_native_ensemble_historical_member_run WHERE station_id=?", (station,))]}
    binding = conn.execute("SELECT settlement_timezone,market_registry_timezone_count FROM calibration_station_market_timezone_binding WHERE station_id=?", (station,)).fetchone()
    if binding is None or binding[1] != 1:
        raise EvidenceClockError("station_timezone_not_unique")
    tz = binding[0]
    truth_rows = conn.execute("SELECT truth_id,station_id,local_date,settlement_timezone,settlement_definition,revision_no,official_tmax_c,source_lane,qc_status,authority_scope,timestamps_complete,canonical_committed_at_utc FROM calibration_station_daily_high_truth WHERE station_id=?", (station,)).fetchall()
    truth_by_day = defaultdict(list)
    for row in truth_rows:
        truth_by_day[row["local_date"]].append(dict(row))
    cases = oos._load_cases(conn, station_id=station, as_of_utc=cutoff_s)
    cases = {scope: cs for scope, cs in cases.items() if scope[0] in SYSTEMS}
    receipts = {}
    required = {sha for cs in cases.values() for c in cs for sha in c["member_run_content_shas"]}
    first_case_day = min((c['event_date'] for cs in cases.values() for c in cs), default=cutoff.date().isoformat())
    for row in conn.execute("SELECT content_sha256,station_id,ensemble_system_id,target_local_date,archive_lead_bucket,run_cycle,member_id,qc_status,transport_provider_id,native_run_init_utc,archive_query_at_utc FROM calibration_native_ensemble_historical_member_run WHERE station_id=? AND target_local_date>=?", (station, first_case_day)):
        if row["content_sha256"] in required:
            receipts[row["content_sha256"]] = dict(row)
    arrival = {}
    errors = Counter()
    lags = []
    for scope, cs in cases.items():
        for c in cs:
            key = (scope, c["event_date"], c["member_set_sha256"])
            try:
                at = exact_member_arrival(c, receipts, station=station, system=scope[0], run_cycle=scope[3])
                arrival[key] = at
                lags.append((at - max(instant(t) for t in c["member_native_run_init_utcs"])).total_seconds()/3600)
            except EvidenceClockError as exc:
                errors[str(exc)] += 1
    systems = oos._load_systems(conn)
    evidence = []
    for first in index["first_p0"]:
        if first["st"] != station or first["h"] is None or first["h"] <= 36:
            continue
        at = instant(first["t"])
        scopes = []
        leaks = set()
        for scope, cs in cases.items():
            target = next((c for c in cs if c["event_date"] == first["d"]), None)
            if target is None:
                continue
            ar = arrival.get((scope, target["event_date"], target["member_set_sha256"]))
            if ar is None or ar > at:
                continue
            train = []
            strict_latest = 0
            start = (datetime.fromisoformat(first["d"]) - timedelta(days=oos.ROLLING_TRAIN_WINDOW_DAYS)).date().isoformat()
            for c in cs:
                if not start <= c["event_date"] < first["d"]:
                    continue
                ca = arrival.get((scope, c["event_date"], c["member_set_sha256"]))
                if ca is None or ca >= at:
                    continue
                truth = select_truth_asof(truth_by_day[c["event_date"]], station=station, event_date=c["event_date"], timezone_name=tz, settlement_definition=c["settlement_definition"], as_of=at)
                if truth is None:
                    continue
                train.append((c["event_date"], truth["truth_id"], truth["revision_no"]))
                if case_truth_known(c, at):
                    strict_latest += 1
                elif truth["official_tmax_c"] != c["actual_tmax_c"]:
                    leaks.add((c["event_date"], truth["revision_no"], c["truth_revision_no"]))
            scopes.append({"scope": scope, "train_asof_n": len(train), "latest_snapshot_ready_n": strict_latest,
                           "member_arrived_at": ar.isoformat(), "twenty_train_ready": len(train)>=20})
        ready = {s["scope"][0] for s in scopes if s["twenty_train_ready"]}
        families = {systems[s]["independence_family_id"] for s in ready}
        evidence.append({"day": first["d"], "at": first["t"], "available_scopes": len(scopes),
                         "systems_with_twenty_train": sorted(ready), "independence_families": sorted(families),
                         "at_least_two_families_necessary_only": len(families)>=2,
                         "future_revision_value_leaks_in_old_clock": sorted(leaks)})
    recent = conn.execute("SELECT event_date,decision_at_utc,posterior_version,probability_envelope_id,settlement_timezone FROM basket_policy_training_fact WHERE station_id=? AND event_date>=? AND stage='P0' ORDER BY decision_at_utc", (station,"2026-10-01")).fetchall()
    labels = conn.execute("SELECT event_date,known_at_utc FROM basket_station_day_settlement_label WHERE station_id=?", (station,)).fetchall()
    closed_label_days = sorted({r['event_date'] for r in labels if instant(r['known_at_utc'])<=cutoff and day_end(r['event_date'],tz)<=cutoff})
    current = []
    for row in recent:
        if row["posterior_version"] != protocol["model_version"] or instant(row["decision_at_utc"]) > cutoff:
            continue
        remain = (day_end(row["event_date"], row["settlement_timezone"])-instant(row["decision_at_utc"])).total_seconds()/3600
        if remain <= 36:
            continue
        current.append({"date":row["event_date"],"decision_at_utc":row["decision_at_utc"],"envelope_id":row["probability_envelope_id"],"closed_at_freeze":day_end(row["event_date"],row["settlement_timezone"])<=cutoff,
                        "evidence_class":"observational_candidate_not_oos"})
    return {"station": station,"timezone":tz,"domains":domains,"cases":sum(map(len,cases.values())),
            "exact_member_receipt_missing":dict(errors),"exact_arrival_cases":len(arrival),
            "arrival_lag_hours":{"minimum":min(lags,default=None),"maximum":max(lags,default=None),"over24h":sum(t>24 for t in lags)},
            "closed_label_days":closed_label_days,"first_decision_clock_audit":evidence,"current_version_window":current,"actual_proper_execution":execution(conn,station,cutoff),
            "meaning":"availability is necessary only; no historical activation, no current-model OOS promotion"}


def summarize(protocol,index):
    results=[];missing=[]
    for station in index["stations"]:
        p=HERE/"stations"/f"{station}.json"
        if not p.exists():missing.append(station)
        else:results.append(json.loads(p.read_text()))
    checks=[x for r in results for x in r["first_decision_clock_audit"]]
    current=[x for r in results for x in r["current_version_window"]]
    plans=[x for r in results for x in r["actual_proper_execution"]]
    intents=[i for p in plans for i in p["intents"]]
    confirmed=[i for i in intents if i.get("evidence")=="confirmed"]
    fills=[i for i in confirmed if i.get("filled_size",0)>0]
    exposed={tuple(k) for k in index['selection_exposed_station_days']}
    outside_report=sorted((r['station'],d) for r in results for d in r['closed_label_days'] if (r['station'],d) not in exposed)
    out={"model_version":protocol["model_version"],"frozen_at_utc":protocol["frozen_at_utc"],"stations_completed":len(results),"stations_missing":missing,
         "historical_clock":{"first_decisions":len(checks),"two_families_with_20_train_necessary_only":sum(x["at_least_two_families_necessary_only"] for x in checks),"decisions_with_old_clock_future_truth_values":sum(bool(x["future_revision_value_leaks_in_old_clock"]) for x in checks),"case_count":sum(r["cases"] for r in results),"exact_arrival_cases":sum(r["exact_arrival_cases"] for r in results)},
         "current_version":{"window_decisions":len(current),"closed_decisions":sum(x["closed_at_freeze"] for x in current),"event_dates":sorted({x["date"] for x in current}),"evidence_class":"observational_candidate_not_oos"},
         "execution":{"plans":len(plans),"unique_intents":len({i['intent_id'] for i in intents}),"sides":dict(Counter(i.get('side','missing') for i in intents)),"states":dict(Counter(i.get('evidence') for i in intents)),"filled_intents":len(fills),"filled_sides":dict(Counter(i['side'] for i in fills)),"filled_size":sum(i['filled_size'] for i in fills),"paid_usdc":sum(i['filled_notional_usdc'] for i in fills),"settled_pnl_before_unrecorded_fees":sum(i['settled_pnl_before_unrecorded_fees'] or 0 for i in fills)},
         "gate":{"current_model_edge":"INSUFFICIENT_EVIDENCE","proper_no_execution":"INSUFFICIENT_EXECUTION_EVIDENCE","certified_unseen_closed_historical_station_days":0,"historical_selection_exposed":len(index["selection_exposed_station_days"]),"closed_labels_outside_prior_report":outside_report,"no_promotion":True},
         "limitations":["Current version envelopes remain observational, not historical OOS.","Historical receipt/truth audit is not a current-model probability retrain.","Exact artifacts/activations and an untouched historical test set remain required.","Fee fields absent from reconciliation cannot be assumed zero."]}
    if missing:out['gate']['current_model_edge']='INCOMPLETE_STATION_COVERAGE'
    write(HERE/'summary.json',out)
    print(json.dumps(out,ensure_ascii=False))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['prepare','station','summarize'])
    parser.add_argument('--station')
    parser.add_argument('--db',default=str(ROOT/'data/bayes_basket_target_ssot.db'))
    args=parser.parse_args()
    budget=Budget()
    protocol=json.loads((HERE/'protocol.json').read_text())
    if args.mode=='prepare':return prepare(protocol,budget)
    index=json.loads((HERE/'input_index.json').read_text())
    if args.mode=='summarize':return summarize(protocol,index)
    if args.station not in index['stations']:raise EvidenceClockError('station_not_in_frozen_universe')
    conn=connect(args.db,budget)
    try:out=station_run(conn,args.station,protocol,index)
    finally:conn.close()
    out['resources']={'elapsed_s':time.monotonic()-budget.started,'peak_rss_bytes':budget.peak_rss,'query_bytes':budget.bytes,'query_rows':budget.rows}
    out['protocol_sha256']=hashlib.sha256((HERE/'protocol.json').read_bytes()).hexdigest()
    out['engine_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    (HERE/'stations').mkdir(exist_ok=True)
    write(HERE/'stations'/f'{args.station}.json',out)
    print(json.dumps({'station':args.station,'cases':out['cases'],'first_decisions':len(out['first_decision_clock_audit']),'resources':out['resources']}))


if __name__=='__main__':
    main()
