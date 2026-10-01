#!/usr/bin/env python3
"""Fit P0-M's TAF likelihood and decide, walk-forward, whether it may serve.

Charter 150 (user ruling 2026-09-19).  For every settled station-day with a
TAF TX, the day's first P0-M decision gives three numbers: the TX, the P0
root's centre ``c0`` and the root's bucket probabilities.  The likelihood is

    x = TX - c0  |  y = truth - c0   ~   N(a_s + b_s * y, tau_s)

with each station's line shrunk toward the pool by ``K = 15`` station-days.

The gate is walk-forward and strictly as-of (``AsOf``): each case is scored
with parameters fitted only on station-days whose truth had been committed
when that case's P0-M was decided, each at the truth revision standing then,
as ``root`` against ``root x L`` (both under the production zero floor), and
the per-day mean log-score gains are bootstrapped by event day.  The artifact is marked SERVE only when the lower end of
that interval is above zero; otherwise it is written with gate IDENTITY and
P0-M stays P0 -- the numbers are kept so the board can show how close it is.

Charter 158: the same line is then tempered per disagreement group
(``taf_likelihood.disagreement_group``).  Every forward TAF with a TX for a
settled station-day is paired with the P0 that was current when it became
available -- what P0-M multiplies at that moment -- and weighted 1/n within its
station-day.  A tempered group's lambda is the grid value with the best
weighted log score under the fitted line (``TEMPERED_GROUPS`` -- today only
the unreasoned cold TAF; every other group, and one under ``MIN_GROUP_WEIGHT``
station-days, keeps 1).  The tempering serves only when a walk-forward over the same
cases -- line and lambdas fitted, as above, on what was knowable when each TAF
arrived -- beats lambda = 1 on average; otherwise it is written with state
IDENTITY and every group reads 1.

    python3 scripts/fit_taf_likelihood.py --through 2026-09-18
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import sqlite3
import statistics
import sys
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import taf_likelihood as TL  # noqa: E402
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL  # noqa: E402
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.native_ensemble_training_facts import (  # noqa: E402
    taf_restatement_is_current,
)
from bayes_basket_target.materialization import (  # noqa: E402
    PROBABILITY_MODEL_ZERO_FLOOR,
    _anchor_bucket_for_temperature,
    _bucket_centers_c,
)

MIN_TRAIN_DAYS = 7
MIN_STATION_ROWS = 4
BOOTSTRAP_DRAWS = 4000
LAMBDA_GRID = tuple(round(0.25 * i, 2) for i in range(13))   # 0 .. 3
MIN_GROUP_WEIGHT = 12.0
# The groups whose lambda is fitted; every other group reads 1.  Measured
# 2026-09-19 (walk-forward against lambda = 1, four variants of line and
# window): the cold TAF without rain or overcast is the one group that pays,
# +0.075..+0.094 nat on its own station-days at lambda 0.5 every time and
# +0.013..+0.017 overall; fitting all four groups gave -0.004..+0.009, the
# other three trading noise.
TEMPERED_GROUPS: tuple[str, ...] = (TL.GROUP_UNREASONED_COLD,)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(text: Any) -> float:
    """An ISO instant as seconds -- compared as numbers, never as strings.

    ``...:09Z`` sorts above ``...:09.000001Z`` as text, so a string compare
    of two clocks with and without fractions reads the later one as earlier.
    """

    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()


# Review 2026-09-19 (P1): training for a prediction made at T reads only the
# station-days whose truth had been committed by T, at the revision standing
# at T.  The walk-forward used to cut on ``event_date < test day``, which let
# yesterday's (and, for a first P0-M two days out, the day before's) truth
# train a decision taken before those days had closed -- 142 of the 147
# scored cases on 2026-09-19.
TRAINING_CUT = "strict_as_of_truth_commit_v1"


def load_truth_revisions(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str], list[tuple[float, int, float]]]:
    """``(station, day) -> [(committed_at, revision, tmax_c), ...]`` by revision.

    The commit is when the truth lane wrote that revision -- the moment it
    became knowable (``label_knowability``); the last entry is today's truth.
    """

    out: dict[tuple[str, str], list[tuple[float, int, float]]] = defaultdict(list)
    for station, day, revision, tmax, committed in conn.execute(
        "SELECT station_id, local_date, revision_no, official_tmax_c,"
        " canonical_committed_at_utc"
        " FROM calibration_station_daily_high_truth"
        " WHERE source_lane IN " + FORMAL_TRUTH_LANES_SQL + " AND qc_status='ACCEPTED'"
        " AND official_tmax_c IS NOT NULL AND canonical_committed_at_utc IS NOT NULL"
    ):
        out[(str(station), str(day))].append(
            (_epoch(committed), int(revision), float(tmax))
        )
    return {key: sorted(rows, key=lambda r: r[1]) for key, rows in out.items()}


def _revision_labels(
    revisions: Sequence[tuple[float, int, float]],
    *,
    lineage: Mapping[str, Any],
    ordinals: Mapping[str, int],
) -> list[tuple[float, int, float, str]] | None:
    """Each truth revision as ``(committed_at, revision, tmax_c, bucket)``."""

    labels = []
    for committed, revision, tmax in revisions:
        bucket = _anchor_bucket_for_temperature(
            tmax, root_lineage=lineage, bucket_ordinals=ordinals
        )
        if bucket is None:
            return None
        labels.append((committed, revision, tmax, bucket))
    return labels


def as_of_label(
    case: Mapping[str, Any], at: float
) -> tuple[float, int, float, str] | None:
    """The truth revision standing at ``at``: the highest one committed by then."""

    standing = [label for label in case["truth_revisions"] if label[0] <= at]
    return max(standing, key=lambda label: label[1]) if standing else None


# --------------------------------------------------------------------------
# Where the cases' envelope numbers come from (2026-10-01 「改」).
#
# A case needs a handful of numbers off two envelopes -- the day's first
# P0-M with a TX, and its P0 root -- and the fit used to re-derive every one
# of them each night by walking every P0-M since 2026-08-16 through its
# ~18 KB lineage.  Those numbers never change once a day is over (envelopes
# are append-only), so a day FREEZE_LAG_DAYS past is copied once, verbatim,
# into a small case cache beside the P1 samples, and the fit reads frozen
# days from there.  What is cached is the raw rows the three queries below
# return, not cases: truth (which charter 225 may revise) is still read live,
# and the case selection is the same code on either source.
# --------------------------------------------------------------------------

P0M_ROWS_SQL = (
    "SELECT station_id, event_date, decision_at_utc,"
    " json_extract(lineage_json,'$.taf_observed_tx_c'),"
    " json_extract(lineage_json,'$.p0_root_checkpoint_id')"
    " FROM basket_probability_envelope"
    " WHERE stage='P0-M' AND state='COMMITTED'"
    " AND event_date>=? AND event_date<=?"
)
ROOT_SQL = (
    "SELECT json_extract(lineage_json,'$.model_center_c'), probabilities_json,"
    " bucket_ordinals_json, json_extract(lineage_json,'$.bucket_intervals_c_json')"
    " FROM basket_probability_envelope WHERE checkpoint_id=? LIMIT 1"
)
P0_ROOTS_SQL = (
    "SELECT decision_at_utc, checkpoint_id FROM basket_probability_envelope"
    " WHERE station_id=? AND event_date=? AND stage='P0' AND state='COMMITTED'"
)
DAY_P0_ROOTS_SQL = (
    "SELECT station_id, decision_at_utc, checkpoint_id FROM basket_probability_envelope"
    " WHERE event_date=? AND stage='P0' AND state='COMMITTED'"
)
DAY_COUNTS_SQL = (
    "SELECT stage, COUNT(*) FROM basket_probability_envelope"
    " WHERE state='COMMITTED' AND event_date=? AND stage IN ('P0','P0-M') GROUP BY stage"
)

#: Where the nightly fit's evidence begins (the sweep passes no --since).
DEFAULT_SINCE = "2026-08-16"
CASE_CACHE_PATH = ROOT / "data" / "runtime" / "bayes_basket_target_nwp" / "taf_likelihood_cases.db"
CASE_CACHE_SCHEMA = "taf_likelihood_case_cache_v1"
#: A day's P0 and P0-M stop being written once its local day is over; three
#: days past it the day is copied and, from then on, read from the cache.
FREEZE_LAG_DAYS = 3
#: Columns without a declared type keep each value's storage class, so a
#: cached row is the row the SSOT query returned, REAL, TEXT and NULL alike.
CASE_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS frozen_date (
  event_date TEXT PRIMARY KEY,
  p0m_rows INTEGER NOT NULL,
  p0_rows INTEGER NOT NULL,
  schema TEXT NOT NULL,
  frozen_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS p0m_row (
  event_date TEXT NOT NULL, seq INTEGER NOT NULL,
  station_id, decision_at_utc, tx, root_id,
  PRIMARY KEY(event_date, seq)
);
CREATE TABLE IF NOT EXISTS p0_root (
  event_date TEXT NOT NULL, seq INTEGER NOT NULL,
  station_id, decision_at_utc, checkpoint_id,
  PRIMARY KEY(event_date, seq)
);
CREATE INDEX IF NOT EXISTS p0_root_scope_idx ON p0_root(station_id, event_date);
CREATE TABLE IF NOT EXISTS root (
  event_date TEXT NOT NULL, checkpoint_id TEXT NOT NULL, present INTEGER NOT NULL,
  model_center_c, probabilities_sha TEXT, ordinals_sha TEXT, intervals_sha TEXT,
  PRIMARY KEY(event_date, checkpoint_id)
);
CREATE INDEX IF NOT EXISTS root_checkpoint_idx ON root(checkpoint_id);
-- The JSON texts by content: ~47k distinct probability vectors and 60 bucket
-- geometries stand behind ~170k roots (measured 2026-10-01, 44 days).
CREATE TABLE IF NOT EXISTS blob (sha TEXT PRIMARY KEY, body TEXT NOT NULL);
"""


class SsotSource:
    """The envelope rows, read off the SSOT as the fit always read them."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def p0m_rows(self, since: str, through: str) -> list[tuple]:
        return self.conn.execute(P0M_ROWS_SQL, (since, through)).fetchall()

    def root(self, checkpoint_id: str) -> tuple | None:
        return self.conn.execute(ROOT_SQL, (checkpoint_id,)).fetchone()

    def p0_roots(self, station_id: str, event_date: str) -> list[tuple]:
        return self.conn.execute(P0_ROOTS_SQL, (station_id, event_date)).fetchall()


def open_case_cache(path: Path | str = CASE_CACHE_PATH) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cache = sqlite3.connect(str(path), timeout=60.0, isolation_level=None)
    cache.execute("PRAGMA journal_mode=WAL")
    cache.executescript(CASE_CACHE_DDL)
    return cache


def _day_counts(conn: sqlite3.Connection, day: str) -> tuple[int, int]:
    counts = dict(conn.execute(DAY_COUNTS_SQL, (day,)).fetchall())
    return int(counts.get("P0-M", 0)), int(counts.get("P0", 0))


def freeze_day(conn: sqlite3.Connection, cache: sqlite3.Connection, day: str, *, now_utc: str) -> dict[str, int]:
    """Copy one event day's envelope rows into the cache, replacing any older copy."""

    rows = conn.execute(P0M_ROWS_SQL, (day, day)).fetchall()
    roots = conn.execute(DAY_P0_ROOTS_SQL, (day,)).fetchall()
    p0m_count, p0_count = _day_counts(conn, day)
    wanted = list(dict.fromkeys(
        [str(r[2]) for r in roots] + [str(r[4]) for r in rows if r[4]]
    ))
    geometry = [(cid, conn.execute(ROOT_SQL, (cid,)).fetchone()) for cid in wanted]
    cache.execute("BEGIN IMMEDIATE")
    try:
        for table in ("p0m_row", "p0_root", "root", "frozen_date"):
            cache.execute(f"DELETE FROM {table} WHERE event_date=?", (day,))
        cache.executemany(
            "INSERT INTO p0m_row VALUES(?,?,?,?,?,?)",
            [(day, seq, r[0], r[2], r[3], r[4]) for seq, r in enumerate(rows)],
        )
        cache.executemany(
            "INSERT INTO p0_root VALUES(?,?,?,?,?)",
            [(day, seq, r[0], r[1], r[2]) for seq, r in enumerate(roots)],
        )
        root_rows = []
        for cid, g in geometry:
            if g is None:
                root_rows.append((day, cid, 0, None, None, None, None))
                continue
            shas = []
            for text in g[1:]:
                if text is None:
                    shas.append(None)
                    continue
                if not isinstance(text, str):
                    raise TypeError(f"taf_case_cache_non_text_json:{cid}")
                sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
                cache.execute("INSERT OR IGNORE INTO blob VALUES(?,?)", (sha, text))
                shas.append(sha)
            root_rows.append((day, cid, 1, g[0], *shas))
        cache.executemany("INSERT INTO root VALUES(?,?,?,?,?,?,?)", root_rows)
        cache.execute(
            "INSERT INTO frozen_date VALUES(?,?,?,?,?)",
            (day, p0m_count, p0_count, CASE_CACHE_SCHEMA, now_utc),
        )
        cache.execute("COMMIT")
    except BaseException:
        if cache.in_transaction:
            cache.execute("ROLLBACK")
        raise
    return {"p0m_rows": len(rows), "p0_roots": len(roots), "roots": len(geometry)}


def _days(since: str, through: str) -> list[str]:
    from datetime import date, timedelta  # noqa: PLC0415

    first, last = date.fromisoformat(since), date.fromisoformat(through)
    return [(first + timedelta(days=n)).isoformat() for n in range((last - first).days + 1)]


def refresh_case_cache(
    conn: sqlite3.Connection, cache: sqlite3.Connection, *, since: str, frozen_through: str, now_utc: str,
) -> dict[str, Any]:
    """Freeze every day up to ``frozen_through``; re-freeze a day the SSOT has since added to.

    A frozen day whose rows have left the SSOT (the cold tier) is kept as it
    is: the counts can only be compared while the SSOT still holds the day.
    """

    receipt = {"frozen": 0, "refrozen": 0, "kept": 0}
    if frozen_through < since:
        return receipt
    known = {
        str(day): (int(p0m), int(p0), str(schema))
        for day, p0m, p0, schema in cache.execute(
            "SELECT event_date, p0m_rows, p0_rows, schema FROM frozen_date"
        )
    }
    for day in _days(since, frozen_through):
        held = known.get(day)
        if held is None:
            freeze_day(conn, cache, day, now_utc=now_utc)
            receipt["frozen"] += 1
            continue
        counts = _day_counts(conn, day)
        if held[2] != CASE_CACHE_SCHEMA or (any(counts) and counts != held[:2]):
            freeze_day(conn, cache, day, now_utc=now_utc)
            receipt["refrozen"] += 1
        else:
            receipt["kept"] += 1
    return receipt


class CachedSource:
    """Frozen days off the case cache, every other day off the SSOT."""

    def __init__(self, conn: sqlite3.Connection, cache: sqlite3.Connection) -> None:
        self.ssot = SsotSource(conn)
        self.cache = cache
        self.frozen = {str(r[0]) for r in cache.execute("SELECT event_date FROM frozen_date")}

    def p0m_rows(self, since: str, through: str) -> list[tuple]:
        out: list[tuple] = []
        run: list[str] = []

        def flush() -> None:
            if run:
                out.extend(self.ssot.p0m_rows(run[0], run[-1]))
                run.clear()

        for day in _days(since, through):
            if day in self.frozen:
                flush()
                out.extend(
                    (r[0], day, r[1], r[2], r[3]) for r in self.cache.execute(
                        "SELECT station_id, decision_at_utc, tx, root_id FROM p0m_row"
                        " WHERE event_date=? ORDER BY seq", (day,),
                    )
                )
            else:
                run.append(day)
        flush()
        return out

    def root(self, checkpoint_id: str) -> tuple | None:
        row = self.cache.execute(
            "SELECT r.present, r.model_center_c, p.body, o.body, i.body FROM root r"
            " LEFT JOIN blob p ON p.sha = r.probabilities_sha"
            " LEFT JOIN blob o ON o.sha = r.ordinals_sha"
            " LEFT JOIN blob i ON i.sha = r.intervals_sha"
            " WHERE r.checkpoint_id=? LIMIT 1", (checkpoint_id,),
        ).fetchone()
        if row is None:
            return self.ssot.root(checkpoint_id)
        return tuple(row[1:]) if row[0] else None

    def p0_roots(self, station_id: str, event_date: str) -> list[tuple]:
        if event_date not in self.frozen:
            return self.ssot.p0_roots(station_id, event_date)
        return self.cache.execute(
            "SELECT decision_at_utc, checkpoint_id FROM p0_root"
            " WHERE station_id=? AND event_date=? ORDER BY seq", (station_id, event_date),
        ).fetchall()


def load_cases(
    conn: sqlite3.Connection, *, since: str, through: str, source: Any = None,
) -> list[dict[str, Any]]:
    """The first P0-M with a TX per settled station-day, on its P0 root.

    ``c0`` is the root's centre, read off the root row -- the number serving
    subtracts (``materialize_p0m_observed`` reads ``p0_root``).  The P0-M
    row's own ``model_center_c`` is its served anchor once the likelihood
    applies, so reading it there fed each TAF's pull back into the next fit
    (review 2026-09-19: a 38C root read as 38.18C).
    """

    source = source if source is not None else SsotSource(conn)
    truth = load_truth_revisions(conn)
    first: dict[tuple[str, str], tuple[float, str, float, str]] = {}
    for station, day, at, tx, root_id in source.p0m_rows(since, through):
        if tx in (None, "", "NONE") or not root_id:
            continue
        key = (str(station), str(day))
        if key not in truth:
            continue
        moment = _epoch(at)
        if key not in first or moment < first[key][0]:
            first[key] = (moment, str(at), float(tx), str(root_id))
    cases: list[dict[str, Any]] = []
    for (station, day), (moment, at, tx, root_id) in sorted(first.items()):
        root = source.root(root_id)
        if root is None:
            continue
        center, probabilities_json, ordinals_json, intervals = root
        if not intervals or center in (None, "", "NONE"):
            continue
        probabilities = {str(k): float(v) for k, v in json.loads(probabilities_json).items()}
        ordinals = {str(k): int(v) for k, v in json.loads(ordinals_json).items()}
        lineage = {"bucket_intervals_c_json": intervals}
        axis = _bucket_centers_c(lineage, bucket_ordinals=ordinals)
        labels = _revision_labels(
            truth[(station, day)], lineage=lineage, ordinals=ordinals
        )
        if axis is None or not labels:
            continue
        cases.append({
            "station_id": station,
            "event_date": day,
            "decided_at": moment,
            "decided_at_utc": at,
            "tx_c": tx,
            "c0": float(center),
            # Scored against today's truth; trained on the revision standing
            # at the moment of the prediction being scored (``as_of_label``).
            "truth_c": labels[-1][2],
            "truth_bucket": labels[-1][3],
            "truth_revisions": labels,
            "probabilities": probabilities,
            "centers": dict(zip(axis[0], axis[1])),
        })
    return cases


def load_tempering_cases(
    conn: sqlite3.Connection, *, since: str, through: str, source: Any = None,
) -> list[dict[str, Any]]:
    """Every forward TAF with a TX and its weather, on the P0 current when it arrived."""

    source = source if source is not None else SsotSource(conn)
    truth = load_truth_revisions(conn)
    seen: set[tuple[str, str, str, float]] = set()
    raw: list[tuple[str, str, float, str, float, float, float]] = []
    fetched = conn.execute(
        "SELECT station_id, event_date, issued_at_utc, provider_available_at_utc,"
        " received_at_utc, tx_c, cloud_cover_fraction, precipitation_probability,"
        " revision_id"
        " FROM calibration_forward_taf_capture"
        " WHERE tx_c IS NOT NULL AND event_date>=? AND event_date<=?",
        (since, through),
    ).fetchall()
    # 「以最新的为准」 (2026-09-23): a restated revision is read as its newest
    # restatement only.  With none, every row is kept in the order read.
    ids_by_scope: dict[tuple[str, str], list[str]] = {}
    for item in fetched:
        ids_by_scope.setdefault((str(item[0]), str(item[1])), []).append(str(item[8]))
    for station, day, issued, available, received, tx, cloud, precip, rid in fetched:
        if not taf_restatement_is_current(rid, ids_by_scope[(str(station), str(day))]):
            continue
        key = (str(station), str(day), str(issued), float(tx))
        if (key[0], key[1]) not in truth or key in seen:
            continue
        seen.add(key)
        when = str(available or received or issued)
        raw.append((
            key[0], key[1], _epoch(when), when, float(tx),
            float(cloud), float(precip),
        ))
    roots: dict[tuple[str, str], list[tuple[float, str]]] = {}
    geometry: dict[str, dict[str, Any] | None] = {}
    cases: list[dict[str, Any]] = []
    for station, day, moment, when, tx, cloud, precip in sorted(raw):
        scope = (station, day)
        if scope not in roots:
            roots[scope] = sorted(
                (_epoch(at), str(cid)) for at, cid in source.p0_roots(*scope)
            )
        current = [cid for at, cid in roots[scope] if at <= moment]
        if not current:
            continue
        cid = current[-1]
        if cid not in geometry:
            row = source.root(cid)
            geometry[cid] = None
            if row is not None and row[0] not in (None, "", "NONE") and row[3]:
                probabilities = {
                    str(k): float(v) for k, v in json.loads(row[1]).items()
                }
                ordinals = {str(k): int(v) for k, v in json.loads(row[2]).items()}
                lineage = {"bucket_intervals_c_json": row[3]}
                axis = _bucket_centers_c(lineage, bucket_ordinals=ordinals)
                labels = _revision_labels(
                    truth[scope], lineage=lineage, ordinals=ordinals
                )
                mass = sum(probabilities.get(b, 0.0) for b in axis[0]) if axis else 0.0
                if axis is not None and labels and mass > 0.0:
                    centers = dict(zip(axis[0], axis[1]))
                    geometry[cid] = {
                        "c0": float(row[0]),
                        "probabilities": probabilities,
                        "centers": centers,
                        "root_mean_c": sum(
                            probabilities.get(b, 0.0) * c for b, c in centers.items()
                        ) / mass,
                        "truth_c": labels[-1][2],
                        "truth_bucket": labels[-1][3],
                        "truth_revisions": labels,
                    }
        shape = geometry[cid]
        if shape is None:
            continue
        cases.append({
            "station_id": station,
            "event_date": day,
            "decided_at": moment,
            "decided_at_utc": when,
            "tx_c": tx,
            "cloud_cover": cloud,
            "precip_prob": precip,
            "group": TL.disagreement_group(tx - shape["root_mean_c"], cloud, precip),
            **shape,
        })
    per_day: dict[tuple[str, str], int] = defaultdict(int)
    for case in cases:
        per_day[(case["station_id"], case["event_date"])] += 1
    for case in cases:
        case["weight"] = 1.0 / per_day[(case["station_id"], case["event_date"])]
    return cases


class AsOf:
    """What a fit standing at a moment could read.

    A case trains a prediction made at ``at`` only when its own decision was
    taken and a truth revision for its station-day had been committed by
    ``at``; it trains on that standing revision.  The set changes only when a
    revision commits (or a decision lands after its own day's truth, which
    ``walk_forward`` counts and never scores), so fits are cached on
    ``key(at)``.
    """

    def __init__(self, cases: Sequence[Mapping[str, Any]]) -> None:
        self.cases = list(cases)
        self._commits = sorted({
            label[0] for case in self.cases for label in case["truth_revisions"]
        })
        self._late = sorted({
            case["decided_at"] for case in self.cases
            if case["decided_at"] > min(l[0] for l in case["truth_revisions"])
        })

    def key(self, at: float) -> tuple[int, int]:
        return bisect_right(self._commits, at), bisect_right(self._late, at)

    def training(self, at: float) -> list[dict[str, Any]]:
        rows = []
        for case in self.cases:
            if case["decided_at"] > at:
                continue
            label = as_of_label(case, at)
            if label is None:
                continue
            rows.append({**case, "truth_c": label[2], "truth_bucket": label[3]})
        return rows


def _line(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float, float]:
    my, mx = statistics.mean(ys), statistics.mean(xs)
    vy = sum((y - my) ** 2 for y in ys)
    b = (
        sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / vy
        if vy > 0 else 0.0
    )
    a = mx - b * my
    resid = sum((x - a - b * y) ** 2 for x, y in zip(xs, ys))
    tau = math.sqrt(max(resid / max(len(xs) - 2, 1), TL.MIN_TAU_C ** 2))
    return a, b, tau


def fit(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    xs = [c["tx_c"] - c["c0"] for c in cases]
    ys = [c["truth_c"] - c["c0"] for c in cases]
    pa, pb, pt = _line(xs, ys)
    by_station: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for case in cases:
        by_station[case["station_id"]].append(case)
    stations: dict[str, dict[str, float]] = {}
    for station, rows in sorted(by_station.items()):
        if len(rows) < MIN_STATION_ROWS:
            continue
        a, b, tau = _line(
            [r["tx_c"] - r["c0"] for r in rows],
            [r["truth_c"] - r["c0"] for r in rows],
        )
        weight = len(rows) / (len(rows) + TL.SHRINK_K)
        stations[station] = {
            "a": pa + (a - pa) * weight,
            "b": pb + (b - pb) * weight,
            "tau": pt + (tau - pt) * weight,
            "n": len(rows),
        }
    return {
        "pool": {"a": pa, "b": pb, "tau": pt, "n": len(cases)},
        "stations": stations,
        "shrink_k": TL.SHRINK_K,
    }


def _floored_log(probabilities: Mapping[str, float], truth: str) -> float:
    lifted = {
        bucket: max(float(value), PROBABILITY_MODEL_ZERO_FLOOR)
        for bucket, value in probabilities.items()
    }
    return math.log(lifted[truth] / sum(lifted.values()))


def _floored_guard_scores(
    probabilities: Mapping[str, float], centers: Mapping[str, float], truth: str
) -> tuple[float, float]:
    """Charter 240's guardrails on the served (floored) vector: (bucket Brier,
    ladder CRPS), buckets ordered by centre, rungs weighted by the distance
    between adjacent centres -- the OOS gate's arithmetic."""

    order = sorted(probabilities, key=lambda b: (float(centers[b]), b))
    lifted = {b: max(float(probabilities[b]), PROBABILITY_MODEL_ZERO_FLOOR) for b in order}
    total = sum(lifted.values())
    served = {b: v / total for b, v in lifted.items()}
    brier = sum((served[b] - (1.0 if b == truth else 0.0)) ** 2 for b in order)
    hit = order.index(truth)
    cumulative = crps = 0.0
    for index in range(len(order) - 1):
        cumulative += served[order[index]]
        crps += (cumulative - (1.0 if index >= hit else 0.0)) ** 2 * (
            float(centers[order[index + 1]]) - float(centers[order[index]])
        )
    return brier, crps


def _day_bootstrap(means: Sequence[float], seed: int) -> tuple[float, float]:
    rng = random.Random(seed)
    boot = sorted(
        statistics.mean(rng.choice(means) for _ in means)
        for _ in range(BOOTSTRAP_DRAWS)
    )
    return boot[int(0.025 * BOOTSTRAP_DRAWS)], boot[int(0.975 * BOOTSTRAP_DRAWS)]


def _scored(
    line_artifact: Mapping[str, Any], cases: Sequence[Mapping[str, Any]]
) -> list[tuple[Mapping[str, Any], dict[str, float]]]:
    """Each case with its untempered exponents under the line -- serving's function."""

    out = []
    for case in cases:
        exponents, _ = TL.exponent_by_bucket(
            line_artifact, station_id=case["station_id"], tx_c=case["tx_c"],
            root_center_c=case["c0"], bucket_centers_c=case["centers"],
        )
        out.append((case, exponents))
    return out


def _tempered_gain(
    case: Mapping[str, Any], exponents: Mapping[str, float], lam: float
) -> float | None:
    """Log score of P0 x L^lambda over P0, as the walk-forward above scores it."""

    probabilities = case["probabilities"]
    low = min(exponents.values())
    weighted = {
        b: p * math.exp(-lam * (exponents[b] - low))
        for b, p in probabilities.items() if b in exponents
    }
    total = sum(weighted.values())
    if total <= 0.0:
        return None
    posterior = {b: v / total for b, v in weighted.items()}
    return (
        _floored_log(posterior, case["truth_bucket"])
        - _floored_log(probabilities, case["truth_bucket"])
    )


def fit_tempering(
    line_artifact: Mapping[str, Any], cases: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, float], dict[str, float]]:
    """Per group, the grid lambda with the best weighted log score (ties toward 1)."""

    scored = _scored(line_artifact, cases)
    lambdas: dict[str, float] = {}
    weights: dict[str, float] = {}
    for group in TL.TEMPERING_GROUPS:
        members = [(c, e) for c, e in scored if c["group"] == group]
        weights[group] = sum(c["weight"] for c, _ in members)
        if group not in TEMPERED_GROUPS or weights[group] < MIN_GROUP_WEIGHT:
            lambdas[group] = 1.0
            continue

        def total(lam: float) -> float:
            return sum(
                c["weight"] * gain
                for c, e in members
                if (gain := _tempered_gain(c, e, lam)) is not None
            )

        lambdas[group] = max(
            LAMBDA_GRID, key=lambda lam: (round(total(lam), 9), -abs(lam - 1.0))
        )
    return lambdas, weights


def walk_forward_tempering(
    line_cases: Sequence[Mapping[str, Any]],
    tempering_cases: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Tempered against lambda = 1, each TAF on a line and lambdas it could have had.

    Strict as-of (``AsOf``): the line and the lambdas scoring a TAF are fitted
    on the station-days whose truth had been committed when that TAF arrived.
    Gains are still pooled by event day, weighted 1/n within a station-day.
    """

    line_as_of, tempering_as_of = AsOf(line_cases), AsOf(tempering_cases)
    fits: dict[tuple, tuple[dict[str, Any], dict[str, float]] | None] = {}
    sums: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    by_group: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    after_truth = 0
    for case in sorted(tempering_cases, key=lambda c: c["decided_at"]):
        at = case["decided_at"]
        if as_of_label(case, at) is not None:
            after_truth += 1
            continue
        key = (line_as_of.key(at), tempering_as_of.key(at))
        if key not in fits:
            train_lines = line_as_of.training(at)
            if len({c["event_date"] for c in train_lines}) < MIN_TRAIN_DAYS:
                fits[key] = None
            else:
                line = {"schema": TL.SCHEMA, "gate": {"state": TL.GATE_SERVE},
                        **fit(train_lines)}
                fits[key] = (line, fit_tempering(line, tempering_as_of.training(at))[0])
        if fits[key] is None:
            continue
        line, lambdas = fits[key]
        exponents = _scored(line, [case])[0][1]
        tempered = _tempered_gain(case, exponents, lambdas[case["group"]])
        plain = _tempered_gain(case, exponents, 1.0)
        if tempered is None or plain is None:
            continue
        sums[case["event_date"]][0] += case["weight"] * (tempered - plain)
        sums[case["event_date"]][1] += case["weight"]
        by_group[case["group"]][0] += case["weight"] * (tempered - plain)
        by_group[case["group"]][1] += case["weight"]
    gains_by_day = {day: v[0] / v[1] for day, v in sums.items() if v[1] > 0.0}
    means = list(gains_by_day.values())
    if len(means) < 5:
        return {"state": TL.GATE_IDENTITY, "reason": "too_few_days",
                "days": len(means), "training_cut": TRAINING_CUT}
    rng = random.Random(20260919)
    boot = sorted(
        statistics.mean(rng.choice(means) for _ in means)
        for _ in range(BOOTSTRAP_DRAWS)
    )
    mean = statistics.mean(means)
    return {
        # Tempering replaces lambda = 1 only where it has done better on days
        # it had not seen; the interval is reported, not required.
        "state": TL.GATE_SERVE if mean > 0.0 else TL.GATE_IDENTITY,
        "by_day_mean_gain_vs_lambda_one_nat": mean,
        "ci_low": boot[int(0.025 * BOOTSTRAP_DRAWS)],
        "ci_high": boot[int(0.975 * BOOTSTRAP_DRAWS)],
        "days": len(means),
        "first_day": min(gains_by_day),
        "last_day": max(gains_by_day),
        "gain_by_group_nat": {
            g: (v[0] / v[1] if v[1] > 0 else None) for g, v in by_group.items()
        },
        "station_days_by_group": {g: v[1] for g, v in by_group.items()},
        "training_cut": TRAINING_CUT,
        "fits": sum(1 for value in fits.values() if value is not None),
        "tafs_after_own_truth_unscored": after_truth,
    }


def walk_forward(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The line against the root, each case on a fit it could have had (``AsOf``)."""

    as_of = AsOf(cases)
    fits: dict[tuple, dict[str, Any] | None] = {}
    gains_by_day: dict[str, list[float]] = {}
    guard_by_day: dict[str, list[tuple[float, float]]] = {}
    hits = {"root": 0, "taf": 0}
    scored = 0
    after_truth = 0
    for case in sorted(cases, key=lambda c: c["decided_at"]):
        at = case["decided_at"]
        if as_of_label(case, at) is not None:
            after_truth += 1
            continue
        key = as_of.key(at)
        if key not in fits:
            train = as_of.training(at)
            fits[key] = (
                {"schema": TL.SCHEMA, "gate": {"state": TL.GATE_SERVE}, **fit(train)}
                if len({c["event_date"] for c in train}) >= MIN_TRAIN_DAYS else None
            )
        artifact = fits[key]
        if artifact is None:
            continue
        day = case["event_date"]
        likelihood, receipt = TL.compile_taf_likelihood(
            artifact, station_id=case["station_id"], tx_c=case["tx_c"],
            root_center_c=case["c0"], bucket_centers_c=case["centers"],
            buckets=case["probabilities"],
        )
        if receipt["state"] != "APPLIED":
            continue
        weighted = {
            b: p * likelihood[b] for b, p in case["probabilities"].items()
        }
        total = sum(weighted.values())
        if total <= 0.0:
            continue
        posterior = {b: v / total for b, v in weighted.items()}
        gain = (
            _floored_log(posterior, case["truth_bucket"])
            - _floored_log(case["probabilities"], case["truth_bucket"])
        )
        gains_by_day.setdefault(day, []).append(gain)
        # Charter 240: the guardrails on the same pair, posterior minus root
        # (positive is worse).
        if all(b in case["centers"] for b in posterior):
            post_brier, post_crps = _floored_guard_scores(
                posterior, case["centers"], case["truth_bucket"]
            )
            root_brier, root_crps = _floored_guard_scores(
                {b: case["probabilities"][b] for b in posterior},
                case["centers"], case["truth_bucket"],
            )
            guard_by_day.setdefault(day, []).append(
                (post_brier - root_brier, post_crps - root_crps)
            )
        scored += 1
        hits["root"] += int(
            max(case["probabilities"], key=case["probabilities"].get)
            == case["truth_bucket"]
        )
        hits["taf"] += int(
            max(posterior, key=posterior.get) == case["truth_bucket"]
        )
    means = [statistics.mean(values) for values in gains_by_day.values()]
    if len(means) < 5:
        return {"state": TL.GATE_IDENTITY, "reason": "too_few_days",
                "days": len(means), "station_days": scored,
                "training_cut": TRAINING_CUT}
    rng = random.Random(20260919)
    boot = sorted(
        statistics.mean(rng.choice(means) for _ in means)
        for _ in range(BOOTSTRAP_DRAWS)
    )
    low = boot[int(0.025 * BOOTSTRAP_DRAWS)]
    high = boot[int(0.975 * BOOTSTRAP_DRAWS)]
    # Charter 240: the log score is primary (its lower bound above zero);
    # the bucket Brier and the ladder CRPS are guardrails -- each day's mean
    # change, posterior minus root, must not be significantly above zero.
    guard_days = [day for day in gains_by_day if guard_by_day.get(day)]
    brier_means = [statistics.mean(v[0] for v in guard_by_day[d]) for d in guard_days]
    crps_means = [statistics.mean(v[1] for v in guard_by_day[d]) for d in guard_days]
    brier_ci = _day_bootstrap(brier_means, 20261001) if len(brier_means) >= 5 else None
    crps_ci = _day_bootstrap(crps_means, 20261002) if len(crps_means) >= 5 else None
    brier_ok = brier_ci is not None and brier_ci[0] <= 0.0
    crps_ok = crps_ci is not None and crps_ci[0] <= 0.0
    primary = low > 0.0
    return {
        "state": (
            TL.GATE_SERVE if primary and brier_ok and crps_ok else TL.GATE_IDENTITY
        ),
        "gate_rule": "logscore_primary_crps_brier_guardrail_day_bootstrap_v1",
        "primary_pass": primary,
        "brier_guard": {"ci": list(brier_ci) if brier_ci else None, "pass": brier_ok,
                        "mean": statistics.mean(brier_means) if brier_means else None},
        "crps_guard": {"ci": list(crps_ci) if crps_ci else None, "pass": crps_ok,
                       "mean": statistics.mean(crps_means) if crps_means else None},
        "by_day_mean_gain_nat": statistics.mean(means),
        "ci_low": low,
        "ci_high": high,
        "days": len(means),
        "station_days": scored,
        "mode_hit_root": hits["root"] / max(scored, 1),
        "mode_hit_taf": hits["taf"] / max(scored, 1),
        "training_cut": TRAINING_CUT,
        "fits": sum(1 for value in fits.values() if value is not None),
        "decided_after_own_truth_unscored": after_truth,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--target-db", default=str(
        ROOT / "data" / "bayes_basket_target_ssot.db"
    ))
    parser.add_argument("--since", default=DEFAULT_SINCE)
    parser.add_argument("--through", required=True)
    parser.add_argument("--out", default=str(TL.ARTIFACT_PATH))
    parser.add_argument("--fitted-at-utc", default=None)
    parser.add_argument(
        "--case-cache", default=str(CASE_CACHE_PATH),
        help="frozen days' envelope rows; '' reads every day off the SSOT",
    )
    args = parser.parse_args()

    from datetime import date, timedelta  # noqa: PLC0415

    now_utc = _iso(datetime.now(timezone.utc))
    frozen_through = (
        date.fromisoformat(now_utc[:10]) - timedelta(days=FREEZE_LAG_DAYS)
    ).isoformat()
    cache = open_case_cache(args.case_cache) if args.case_cache else None
    cache_receipt: dict[str, Any] | None = None
    try:
        conn = sqlite3.connect(f"file:{args.target_db}?mode=ro", uri=True, timeout=120)
        try:
            if cache is not None:
                cache_receipt = refresh_case_cache(
                    conn, cache, since=args.since,
                    frozen_through=min(frozen_through, args.through), now_utc=now_utc,
                )
            source = CachedSource(conn, cache) if cache is not None else SsotSource(conn)
            cases = load_cases(conn, since=args.since, through=args.through, source=source)
            if len(cases) < 50:
                print(json.dumps({"state": "REFUSED_THIN", "cases": len(cases)}))
                return 3
            tempering_cases = load_tempering_cases(
                conn, since=args.since, through=args.through, source=source,
            )
        finally:
            conn.close()
    finally:
        if cache is not None:
            cache.close()
    gate = walk_forward(cases)
    line = fit(cases)
    served_line = {"schema": TL.SCHEMA, "gate": {"state": TL.GATE_SERVE}, **line}
    lambdas, group_weight = fit_tempering(served_line, tempering_cases)
    tempering_gate = walk_forward_tempering(cases, tempering_cases)
    body = {
        "schema": TL.SCHEMA,
        "rule": TL.RULE,
        **line,
        "gate": gate,
        TL.TEMPERING_KEY: {
            "rule": TL.TEMPERING_RULE,
            "state": tempering_gate["state"],
            "lambda_by_group": lambdas,
            "group_weight_station_days": group_weight,
            "tafs": len(tempering_cases),
            "grid": list(LAMBDA_GRID),
            "min_group_weight": MIN_GROUP_WEIGHT,
            "walk_forward": tempering_gate,
        },
        "trained_from": min(c["event_date"] for c in cases),
        "trained_through": max(c["event_date"] for c in cases),
        "fitted_at_utc": args.fitted_at_utc or _iso(datetime.now(timezone.utc)),
    }
    body["content_sha256"] = content_sha256(body)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = out.with_suffix(out.suffix + ".new")
    staging.write_text(json.dumps(body, indent=2, sort_keys=True))
    os.replace(staging, out)
    print(json.dumps({
        "schema": TL.SCHEMA,
        "gate": gate["state"],
        "cases": len(cases),
        "pool": body["pool"],
        **{k: (round(v, 4) if isinstance(v, float) else v)
           for k, v in gate.items() if k != "state"},
        "tempering": {
            "state": tempering_gate["state"],
            "lambda_by_group": lambdas,
            "gain_vs_lambda_one": tempering_gate.get(
                "by_day_mean_gain_vs_lambda_one_nat"
            ),
            "ci": [tempering_gate.get("ci_low"), tempering_gate.get("ci_high")],
            "tafs": len(tempering_cases),
        },
        "out": str(out),
        "case_cache": cache_receipt,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
