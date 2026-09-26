#!/usr/bin/env python3
"""Refit the L4 posterior layer per served stage and activate it (charter 230).

Thin CLI over ``bayes_basket_target.posterior_store``: the population (every
labelled decision, every bucket of its ladder), the logit-space fit of
``(alpha, gamma, w)`` with the market as prior, the stage's whole row and one
row per lead band, the by-event-day draws, the persistence, the journal, and
the forward skill score read off the journal.  Without ``--commit`` it
reports only.

Charter 230: every row carries its walk-forward verdict (both of charter 211's
scores against the market); under ``PM_POSTERIOR_LANE=on`` a row serves its
parameters only when admitted, else ``b = m`` and nothing is bought.
``forward skill`` (each decision scored on the fit active when it was taken)
stays the diagnostic beside it.

    python3 scripts/refit_posterior.py                        # report
    python3 scripts/refit_posterior.py --commit --as-of ...   # fit + activate

One stage per invocation keeps a sweep slot under its budget.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.posterior_store import (  # noqa: E402
    STAGE_SCOPES,
    WHOLE_STAGE,
    fit_stage_and_bands,
    forward_skill,
    journal_fit,
    load_posterior_as_of,
    persist_and_activate,
    register_hypotheses,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def _f(x, fmt="+.3f") -> str:
    return "—" if x is None else format(float(x), fmt)


def _line(fit: dict) -> str:
    ev = fit["evidence"]
    band = fit.get("lead_band") or WHOLE_STAGE
    name = f"{fit['stage_scope']}{'' if band == WHOLE_STAGE else ' ' + band}"
    head = (
        f"  {name:12s} {fit['decision_count']:5d} decisions /"
        f" {fit['station_day_count']:4d} station-days / {fit['day_count']:2d} days"
    )
    if ev.get("error"):
        return head + f" -- REFUSED ({ev['error']})"
    if fit["accumulating"]:
        return (
            head + f" -- ACCUMULATING (needs {ev['min_station_days']} /"
            f" {ev['min_days']}); serves b = m"
            + _verdict(ev.get("verdict") or {})
        )
    pt = ev["point"]
    ci = ev["ci"]
    sk = ev["skill"]
    return (
        head
        + f" -- w {_f(pt['w'])} [{_f(ci['w'][0])}, {_f(ci['w'][1])}]"
        + f" gamma {_f(pt['gamma'])} alpha {_f(pt['alpha'])}"
        + f"; S in-sample {_f(sk['s_in_sample'], '+.4f')}"
        + f" [{_f(sk['s_in_sample_ci'][0], '+.4f')}, {_f(sk['s_in_sample_ci'][1], '+.4f')}]"
        + f" IC {_f(sk['ic_in_sample'])}"
        + f"; draws w>0 {_f(sk['w_draw_positive_share'], '.2f')}"
        + _verdict(ev.get("verdict") or {})
    )


def _verdict(v: dict) -> str:
    """Charter 230: the row's walk-forward against the market, both scores."""

    ls = v.get("d_logscore") or [None, None, None]
    br = v.get("d_brier") or [None, None, None]
    return (
        f"\n      verdict {'ADMITTED' if v.get('admitted') else 'b = m'} ({v.get('reason')}):"
        f" {v.get('test_days', 0)} test days / {v.get('station_days', 0)} station-days;"
        f" LogScore {_f(ls[0], '+.4f')} [{_f(ls[1], '+.4f')}, {_f(ls[2], '+.4f')}]"
        f" Brier {_f(br[0], '+.4f')} [{_f(br[1], '+.4f')}, {_f(br[2], '+.4f')}]"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument(
        "--as-of", default=None,
        help="activation stamp and label cutoff; required to commit",
    )
    parser.add_argument(
        "--stage", choices=sorted(STAGE_SCOPES) + ["all"], default="all",
    )
    parser.add_argument(
        "--anchored", action="store_true",
        help="research reading only: cut the population at the stage's"
             " serving-arithmetic anchor (charter 230 serves the rolling"
             " window); refused together with --commit",
    )
    parser.add_argument(
        "--no-forward", action="store_true",
        help="skip the forward skill reading (it re-reads the population)",
    )
    args = parser.parse_args()
    if args.commit and args.anchored:
        print("--anchored is a research reading and may not be committed")
        return 2
    database = args.db.expanduser().resolve(strict=True)
    if args.commit and not args.as_of:
        print("--commit needs --as-of (scripts may not read the clock)")
        return 2
    conn = (
        sqlite3.connect(str(database), timeout=180.0)
        if args.commit
        else sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=180.0)
    )
    conn.execute("PRAGMA busy_timeout=180000")
    scopes = sorted(STAGE_SCOPES) if args.stage == "all" else [args.stage]
    receipts = []
    try:
        if args.commit:
            new = register_hypotheses(conn, now=str(args.as_of))
            if new:
                print(f"  registered {new} hypotheses (E11)")
        for scope in scopes:
            fits = fit_stage_and_bands(
                conn, stage_scope=scope, cutoff_utc=args.as_of,
                anchored=bool(args.anchored),
            )
            for fit in fits:
                if args.commit and not fit["evidence"].get("error"):
                    before = load_posterior_as_of(conn, stage_scope=scope)
                    band = fit.get("lead_band") or WHOLE_STAGE
                    prior_sha = None
                    if before is not None:
                        prior_row = before if band == WHOLE_STAGE else (
                            before.get("lead_bands") or {}
                        ).get(band)
                        prior_sha = None if prior_row is None else prior_row.get("sha256")
                    fit.update(persist_and_activate(conn, fit, now=str(args.as_of)))
                    outcome = (
                        "UNCHANGED" if prior_sha == fit["content_sha256"] else "ACTIVATED"
                    )
                    fit["fit_id"] = journal_fit(
                        conn, fit=fit, outcome=outcome, fitted_at_utc=str(args.as_of),
                    )
                    fit["outcome"] = outcome
                print(_line(fit))
                receipt = {k: v for k, v in fit.items() if k != "table"}
                receipt["evidence"] = {
                    k: v for k, v in fit["evidence"].items() if k != "draws"
                }
                receipts.append(receipt)
            if not args.no_forward:
                fwd = forward_skill(
                    conn, stage_scope=scope, anchored=bool(args.anchored),
                )
                print(
                    f"  {scope:12s} forward skill: {fwd['decisions']} decisions /"
                    f" {fwd['days']} days; S {_f(fwd['s_forward'], '+.4f')}"
                    f" [{_f(fwd['s_forward_ci'][0], '+.4f')},"
                    f" {_f(fwd['s_forward_ci'][1], '+.4f')}]"
                    f" IC {_f(fwd['ic_forward'])};"
                    f" before first fit {fwd['before_first_fit']},"
                    f" served market {fwd['served_market']}"
                )
                receipts.append({"forward_skill": fwd})
        print(json.dumps(receipts, ensure_ascii=False, indent=1, default=str))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
