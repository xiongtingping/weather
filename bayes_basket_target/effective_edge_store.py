"""DB-resident, content-addressed lambda tables for charter 68's effective edge.

Charter 68 has the objective read ``lambda(a) * p`` instead of ``p``, where
``a`` is the market's agreement with a basket -- its price over its claim --
and ``lambda`` is the fraction of that claim history says actually arrives.
The table it shipped with was fitted once, by hand, on the 189 opening seats
settled before 2026-08-27.  A fitted constant is a fitted constant: the
selection distribution moves and the number stops describing it.

The warning is older than this module and is written into
``SELECTION_DISCOUNT`` in the ledger: the ratio is endogenous to selection
intensity, so tightening changes which seats survive and the factor you
measured no longer holds.  The failure mode that warning implies is worse
than drift -- it is blindness.  A rule that refuses the seats it distrusts
stops observing exactly the band it most needs to keep measuring, and the
stalest number in the table becomes the one nobody can refresh.

That failure is closed structurally rather than statistically, and not by
anything in this file: the chain ticket keeps the seat when the shrink
sizes it to nothing.  Measured on the book the day charter 68 landed,
79-99% of opening seats are tickets and they still span all four agreement
bands -- 19 seats below 0.50, 52, 102, and 118 above 0.85.  The observation
stream survives the rule that would otherwise have cut it.  Should that
stop being true, ``fit_lambda_table`` says so per band rather than quietly
serving a number nobody can support any more.

Measured at the basket, deliberately.  Per-bucket agreement is the tempting
basis -- every priced bucket is an observation, and no selection touches it
-- but it answers a different question: on this book the top band reads
1.82 per bucket against 0.89 per basket, because a basket's agreement is a
cost-over-claim ratio across buckets whose individual agreements scatter
around it.  The rule shrinks baskets, so the estimator measures baskets.

Admission rule per band (fixed; the table changes, the rule does not):

* at least ``MIN_SEATS_PER_BAND`` settled opening seats inside the rolling
  window, and claim mass above ``MIN_CLAIM_MASS`` -- below either, the band
  keeps the incumbent value and is reported as accumulating rather than
  fitted to whatever three seats happened to land there;
* the fitted value is clamped to ``[LAMBDA_FLOOR, LAMBDA_CEILING]``, which
  is not a statistical statement but a blast radius: a band that reads 0.02
  on a bad fortnight would zero every basket in it, and one that reads 3.0
  would size on a claim nobody made.

P1 is excluded from the fit for the same reason charter 68 exempts it from
the rule: its realized ratio runs above one exactly where the market
disagrees, because its edge is the observation and not the shape.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any, Sequence

from .basket_kernel.delta_v2 import EFFECTIVE_EDGE_LAMBDA
from .contracts import content_sha256


# The rolling window the fit reads.  Long enough that a quiet week cannot
# empty a band, short enough that a regime two months gone stops voting.
WINDOW_DAYS = 28
MIN_SEATS_PER_BAND = 12
MIN_CLAIM_MASS = 4.0
LAMBDA_FLOOR = 0.05
LAMBDA_CEILING = 1.50
FIT_STAGES = ("P0", "P0-M")

TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS calibration_effective_edge_lambda ("
    " lambda_table_sha256 TEXT PRIMARY KEY,"
    " bands_json TEXT NOT NULL,"
    " evidence_json TEXT NOT NULL,"
    " fitted_at_utc TEXT NOT NULL,"
    " window_days INTEGER NOT NULL,"
    " seat_count INTEGER NOT NULL,"
    " activated INTEGER NOT NULL CHECK(activated IN (0,1)),"
    " activated_at_utc TEXT)"
)
JOURNAL_DDL = (
    "CREATE TABLE IF NOT EXISTS calibration_effective_edge_fit_journal ("
    " fit_id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " lambda_table_sha256 TEXT NOT NULL,"
    " fitted_at_utc TEXT NOT NULL,"
    " admitted INTEGER NOT NULL,"
    " accumulating_bands TEXT NOT NULL,"
    " evidence_json TEXT NOT NULL)"
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(TABLE_DDL)
    conn.execute(JOURNAL_DDL)


def _incumbent_bands() -> list[tuple[float, float]]:
    return [(float(bound), float(lam)) for bound, lam in EFFECTIVE_EDGE_LAMBDA]


def _band_index(agreement: float, bands: Sequence[tuple[float, float]]) -> int:
    for index, (bound, _lam) in enumerate(bands):
        if agreement < bound:
            return index
    return len(bands) - 1


def fit_lambda_table(
    conn: sqlite3.Connection,
    *,
    window_days: int = WINDOW_DAYS,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Fit one lambda per agreement band from the rolling settled window.

    Returns the table plus the evidence for every band, including the ones
    that could not be fitted.  A band short of evidence keeps the incumbent
    value: the alternative -- fitting it anyway -- is how a fortnight with
    four seats in it rewrites the number that governs the largest nominal
    edges on the book.
    """

    incumbent = _incumbent_bands()
    newest = conn.execute(
        "SELECT MAX(event_date) FROM paper_trade_settlement"
    ).fetchone()
    latest = str(as_of or (newest[0] if newest else "") or "")
    if not latest:
        raise ValueError("effective_edge_fit_no_settlements")
    floor_day = (
        datetime.fromisoformat(latest).date()
        - __import__("datetime").timedelta(days=int(window_days))
    ).isoformat()

    placeholders = ",".join("?" for _ in FIT_STAGES)
    rows = conn.execute(
        "SELECT p.win_probability, p.all_in_cost, s.hit"
        " FROM paper_trade_settlement s"
        " JOIN paper_trade_position p USING(position_id)"
        f" WHERE s.stage IN ({placeholders}) AND s.event_date >= ?",
        (*FIT_STAGES, floor_day),
    ).fetchall()

    tally = [
        {"seats": 0, "claim": 0.0, "hits": 0} for _ in incumbent
    ]
    for win, cost, hit in rows:
        claim = float(win or 0.0)
        price = float(cost or 0.0)
        if claim <= 0.0 or price <= 0.0:
            continue
        slot = tally[_band_index(price / claim, incumbent)]
        slot["seats"] += 1
        slot["claim"] += claim
        slot["hits"] += int(hit or 0)

    bands: list[tuple[float, float]] = []
    evidence: list[dict[str, Any]] = []
    accumulating: list[int] = []
    for index, (bound, incumbent_lambda) in enumerate(incumbent):
        slot = tally[index]
        enough = (
            slot["seats"] >= MIN_SEATS_PER_BAND
            and slot["claim"] >= MIN_CLAIM_MASS
        )
        raw = (slot["hits"] / slot["claim"]) if slot["claim"] > 0 else None
        if enough and raw is not None:
            served = min(LAMBDA_CEILING, max(LAMBDA_FLOOR, raw))
        else:
            served = incumbent_lambda
            accumulating.append(index)
        bands.append((bound, served))
        evidence.append({
            "band_upper_bound": bound if bound != float("inf") else None,
            "seats": slot["seats"],
            "claim_mass": round(slot["claim"], 3),
            "hits": slot["hits"],
            "raw_lambda": None if raw is None else round(raw, 4),
            "served_lambda": round(served, 4),
            "incumbent_lambda": incumbent_lambda,
            "clamped": bool(
                enough and raw is not None and abs(raw - served) > 1e-9
            ),
            "accumulating": index in accumulating,
        })

    payload = {
        "schema": "effective_edge_lambda_table_v1",
        "bands": [
            {"upper_bound": None if b == float("inf") else b, "lambda": lam}
            for b, lam in bands
        ],
        "window_days": int(window_days),
        "as_of_event_date": latest,
        "fit_floor_event_date": floor_day,
    }
    return {
        "table": payload,
        "content_sha256": content_sha256(payload),
        "bands": bands,
        "evidence": evidence,
        "accumulating_bands": accumulating,
        "seat_count": sum(slot["seats"] for slot in tally),
        # The alarm, and the reason this module exists: a band nobody can
        # measure any more is a band whose number is frozen at whatever it
        # was when the observations stopped.
        "blind_bands": [
            index for index in accumulating if tally[index]["seats"] == 0
        ],
    }


def persist_and_activate(
    conn: sqlite3.Connection, fit: dict[str, Any]
) -> dict[str, Any]:
    """Store the fit and make it the served table.

    Activation is unconditional once the fit exists, because the admission
    rule already ran band by band inside the fit: a band without evidence
    carries the incumbent value, so activating cannot serve a number the
    hurdles did not clear.  What activation buys is that the bands which DID
    clear stop being stale.
    """

    ensure_schema(conn)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    sha = str(fit["content_sha256"])
    conn.execute(
        "UPDATE calibration_effective_edge_lambda SET activated=0"
        " WHERE activated=1"
    )
    conn.execute(
        "INSERT INTO calibration_effective_edge_lambda("
        " lambda_table_sha256, bands_json, evidence_json, fitted_at_utc,"
        " window_days, seat_count, activated, activated_at_utc)"
        " VALUES(?,?,?,?,?,?,1,?)"
        " ON CONFLICT(lambda_table_sha256) DO UPDATE SET"
        "  activated=1, activated_at_utc=excluded.activated_at_utc,"
        "  evidence_json=excluded.evidence_json",
        (
            sha,
            json.dumps(fit["table"], ensure_ascii=False, sort_keys=True),
            json.dumps(fit["evidence"], ensure_ascii=False),
            now,
            int(fit["table"]["window_days"]),
            int(fit["seat_count"]),
            now,
        ),
    )
    conn.execute(
        "INSERT INTO calibration_effective_edge_fit_journal("
        " lambda_table_sha256, fitted_at_utc, admitted, accumulating_bands,"
        " evidence_json) VALUES(?,?,?,?,?)",
        (
            sha, now,
            len(fit["bands"]) - len(fit["accumulating_bands"]),
            json.dumps(fit["accumulating_bands"]),
            json.dumps(fit["evidence"], ensure_ascii=False),
        ),
    )
    conn.commit()
    return {"lambda_table_sha256": sha, "activated_at_utc": now}


# NOT yet wired to serving, and the reason is structural rather than a
# missing afternoon.  The compile chain holds no database handle by design
# -- compile_production_plan -> Optimizer.compile -> _compile_target_plan ->
# compile_delta_v2_runtime_plan -> _solve_over_support_windows all take
# artifacts, never a connection -- so serving this table means threading it
# through the production decision boundary, or hiding a database read inside
# a pure function.  Neither is a change to make on an armed book at the end
# of a session, and the house rule for the probability layer is explicit:
# batch such edits and run them in shadow first.
#
# So the layer measures, alarms and activates in the database, and the
# kernel constant is still what serves.  Promoting a fitted table is a small
# reviewable edit to that constant, informed by exactly the evidence this
# module journals -- the same shape as the station exclusion list, where the
# machine computes candidates and a person promotes them.
def load_active_lambda(
    conn: sqlite3.Connection,
) -> tuple[tuple[float, float], ...] | None:
    """The served table, or None when nothing has been fitted yet.

    None is the honest answer rather than the incumbent, so a caller can
    tell "no fit has ever run" from "a fit ran and reproduced the
    bootstrap"; the kernel constant is the fallback either way.
    """

    try:
        row = conn.execute(
            "SELECT bands_json FROM calibration_effective_edge_lambda"
            " WHERE activated=1 ORDER BY fitted_at_utc DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        payload = json.loads(str(row[0]))
        bands = payload["bands"]
    except (ValueError, KeyError, TypeError):
        return None
    out: list[tuple[float, float]] = []
    for band in bands:
        bound = band.get("upper_bound")
        out.append((
            float("inf") if bound is None else float(bound),
            float(band["lambda"]),
        ))
    return tuple(out) or None


__all__ = [
    "FIT_STAGES",
    "LAMBDA_CEILING",
    "LAMBDA_FLOOR",
    "MIN_CLAIM_MASS",
    "MIN_SEATS_PER_BAND",
    "WINDOW_DAYS",
    "ensure_schema",
    "fit_lambda_table",
    "load_active_lambda",
    "persist_and_activate",
]
