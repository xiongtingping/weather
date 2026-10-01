"""The serving-lane sample the calibration lanes share, and the band receipts.

This module was the band reliability store: DB-resident, content-addressed
tables that rescaled each P0 bucket by its distance from the peak, refitted
nightly and pinned by every certified artifact.  Charter 232 retired that
layer (user ruling 2026-09-26, 「退役 band 层」): measured on the market ladder
over 43 test days (docs/research/band_layer_recheck_20260925) it had no
position of its own above the width layer, and because it was fitted on each
station-day's first decision and applied to every decision after it, it hurt
the short leads -- h00_12 Brier and LogScore both significantly worse.  The
fit, the veto, the tables' loaders and their shadow lane went with it; the
rows stay in the store as history.

What stays is what other lanes read through here:

* ``serving_lane_by_station_day`` -- each station-day's first served decision
  per stage, with every bucket's claim and whether truth landed in it.  The
  off-centre claim lane samples it.
* ``_invert_band_receipt`` -- decisions served before the retirement carry a
  band receipt, and the centre/width lane measures the claim as it stood
  before that table touched it.  The receipts record the applied factors and
  the pre-normalisation mass, so the inversion is exact; a row without one
  (every row since the retirement) is returned as served.
* ``_truth``, ``utc_instant``, ``_begin_immediate`` and
  ``_first_decision_by_station_day`` -- the settled high at its newest
  revision, instants that order, the write lock taken with a bounded retry,
  and when each station-day's first decision was taken.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .contracts import ContractError

BAND_STAGES = ("P0", "P0-M", "P1")
# The axes the retired tables were keyed on.  The off-centre claim lane's
# rows are partitioned by them and keep reading.
BAND_AXIS_CLAIM = "claim"
BAND_AXIS_CENTRE_DISTANCE = "centre_distance"
BAND_AXES = (BAND_AXIS_CLAIM, BAND_AXIS_CENTRE_DISTANCE)


# The store has one write lock and the resident basket worker owns it for
# about half of every production cycle, in bursts: probed against the live
# store on 2026-08-21, ``BEGIN IMMEDIATE`` waited 3.6-9.1s (six samples)
# to get in, while 19 of 20 attempts capped at 3s never got in at all.
# The cron already waits a minute per attempt (``PRAGMA busy_timeout``),
# which is 6-17x the measured wait and normally ample -- it has lost the
# race once, on 2026-08-20.  Retrying is the same lever as a longer
# busy_timeout, with two differences worth having: the budget is bounded
# and countable (the attempt count lands in the report, so a run that
# nearly missed reads differently from one that walked in), and each
# retry re-enters SQLite's race from a fresh handler rather than sitting
# out the whole wait in one poll loop that has no fairness guarantee.
WRITE_LOCK_ATTEMPTS = 4
WRITE_LOCK_BACKOFF_SECONDS = 5.0


def _check_stage(stage: str) -> str:
    value = str(stage)
    if value not in BAND_STAGES:
        raise ContractError("band_reliability_stage_invalid")
    return value


def _begin_immediate(conn: sqlite3.Connection) -> int:
    """Take the write lock from the resident, returning attempts spent.

    Each attempt already waits out the connection's own ``busy_timeout``;
    this only decides how many such waits we are willing to spend before
    giving up on a writer that is not letting go.  Contention is the only
    thing retried -- any other operational error is a real fault and is
    raised on the first try rather than being sat on for minutes.
    """

    attempt = 0
    while True:
        attempt += 1
        try:
            conn.execute("BEGIN IMMEDIATE")
            return attempt
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            contended = "locked" in message or "busy" in message
            if not contended or attempt >= WRITE_LOCK_ATTEMPTS:
                raise
        time.sleep(WRITE_LOCK_BACKOFF_SECONDS)


def utc_instant(value: str) -> "datetime":
    """An ISO-8601 UTC string as an aware instant.

    The stores write timestamps with and without fractional seconds, so the
    strings do not order as instants: ``...T15:27:09.831Z`` sorts before
    ``...T15:27:09Z`` because ``.`` precedes ``Z``.  Every as-of comparison
    goes through here.
    """

    parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _truth(conn: sqlite3.Connection) -> dict[tuple[str, str], float]:
    """The settled high per station-day, at its latest revision.

    The revision filter is not cosmetic: 285 station-days carry two rows,
    revision 0 stating the METAR body group's whole degree and revision 1 the
    tenths from the remark group (KATL 2026-07-14 reads 29.0 then 28.9), and
    which of the two a band fit sees decides which bucket it scores against
    on a day whose truth sits near an edge.  Unfiltered, this took whichever
    row the scan returned last -- today that happens to be the newest, and
    nothing guarantees it stays so.  The ledger's settlement query has always
    taken MAX(revision_no); this now asks the same question.
    """

    return {
        (str(station), str(day)): float(value)
        for station, day, value in conn.execute(
            "SELECT t.station_id, t.local_date, t.official_tmax_c"
            " FROM calibration_station_daily_high_truth AS t"
            " WHERE t.revision_no=("
            "  SELECT MAX(x.revision_no)"
            "  FROM calibration_station_daily_high_truth AS x"
            "  WHERE x.station_id=t.station_id AND x.local_date=t.local_date"
            # Charter 225: the newest revision across lanes -- revision
            # numbers are one sequence per station-day, so a settlement-
            # source revision supersedes the AWC one instead of standing
            # beside it as a second "newest".
            "  AND x.settlement_definition=t.settlement_definition)"
        )
    }


def _invert_band_receipt(
    records: list[tuple[float, str]],
    receipt: Mapping[str, Any],
) -> list[tuple[float, str]]:
    """Reconstruct pre-band claims from a served row's receipt.

    Serving computed ``served_i = raw_i * f_i / total`` and recorded both
    ``f_i`` (applied_factors, identity omitted) and ``total``
    (pre_normalisation_mass), so the inversion is exact, not estimated.
    """

    total = float(receipt.get("pre_normalisation_mass") or 0.0)
    applied = {
        str(key): float(value)
        for key, value in (receipt.get("applied_factors") or {}).items()
    }
    if total <= 0.0:
        raise ContractError("band_reliability_receipt_mass_invalid")
    return [
        (probability * total / applied.get(key, 1.0), key)
        for probability, key in records
    ]


def serving_lane_by_station_day(
    conn: sqlite3.Connection,
    truth: Mapping[tuple[str, str], float],
    *,
    stage: str = "P0",
    pre_band: bool = True,
    min_decision_at_utc: str | None = None,
) -> dict[tuple[str, str], list[list[tuple[float, int, float]]]]:
    """First decision per station-day for one stage, as pre-band claims.

    ``min_decision_at_utc`` bounds the evidence below, the same bound and for
    the same reason the centre/width lane has carried since 2026-08-27: this
    table measures how wrong the *served* claim is, so a decision taken before
    the serving arithmetic changed describes a distribution nobody serves any
    more.  Unbounded, P0 was fitting 41 days reaching back to 2026-07-28 --
    across the width layer reaching the fleet on 2026-09-04 -- and the two
    cells that only clear their interval on that pooled sample cost 0.85 of a
    peak-hit point on the days after the change while buying 0.0020 nat.
    Measured 2026-09-10 at three walk-forward cuts; a fourteen-day bounded
    window scored at least as well as the unbounded one at every cut.

    The bound is applied to the inner MIN() as well, so the "first decision"
    is the first one this arithmetic made rather than a retired one that
    happens to sort earlier.

    First, not last (user ruling, 2026-08-24).  The last recompute of a
    station-day is written at a median of 23:00 local while the daily high
    has landed by 13.9h -- a claim made after the answer is a description,
    not a forecast, and a table fitted on descriptions certifies them as
    calibrated.  Measured against market-adjudicated truth on 289 closed
    days, the claims the money actually trades on -- the stage's first
    compile, where the seat is priced -- still over-claim 1.26x in
    [0.20,0.35) and 2x above it AFTER this table was applied, while the
    last-snapshot fit was serving 1.07 upward on the same ground.  The
    same clock error is on record twice already: the "model never learns"
    verdict that came from reading end-of-day rows, and the P1 ruling that
    a prophet is judged before the peak, not after.

    Within the first wall-clock decision burst the LAST row is taken: P1
    writes one row per observation step of a burst, and only the final row
    of the burst is the complete prefix (the same contract every other
    reader of the bridge history follows).

    Keyed by (station, event) with each claim carrying its bucket's
    lower bound, ordered coldest first, so a per-station reader can
    reconstruct windows.
    """

    # Charter 238 (「现在做」): frozen days' payloads come from the cache.
    from .serving_payload_cache import cached_history_envelope_payload, record_pass

    by_station_day: dict[
        tuple[str, str], list[list[tuple[float, int, float]]]
    ] = defaultdict(list)
    bound = str(min_decision_at_utc) if min_decision_at_utc else None
    inner_bound = " AND decision_at_utc>=?" if bound else ""
    outer_bound = " AND h.decision_at_utc>=?" if bound else ""
    parameters: list[Any] = [_check_stage(stage)]
    if bound:
        parameters.append(bound)
    parameters.append(_check_stage(stage))
    if bound:
        parameters.append(bound)
    rows = conn.execute(
        "SELECT h.station_id, h.event_date, h.history_id,"
        " h.envelope_json, h.checkpoint_id, h.envelope_content_sha256,"
        " MAX(h.rowid)"
        " FROM basket_probability_bridge_history h"
        " JOIN (SELECT station_id, event_date,"
        "         MIN(decision_at_utc) AS first_decision_at"
        "       FROM basket_probability_bridge_history"
        f"       WHERE stage=?{inner_bound} GROUP BY station_id, event_date) f"
        "   ON f.station_id = h.station_id AND f.event_date = h.event_date"
        "  AND h.decision_at_utc = f.first_decision_at"
        f" WHERE h.stage=?{outer_bound}"
        " GROUP BY h.station_id, h.event_date",
        tuple(parameters),
    ).fetchall()
    for station, event, history_id, stored, checkpoint, sha, _last_row in rows:
        actual = truth.get((str(station), str(event)))
        if actual is None:
            continue
        envelope = cached_history_envelope_payload(
            conn,
            checkpoint_id=str(checkpoint),
            envelope_content_sha256=str(sha),
            stored=str(stored) if stored else None,
        )
        receipt = None
        lineage = envelope.get("lineage") or {}
        raw = lineage.get("band_reliability_json")
        parsed = json.loads(raw) if raw else None
        if parsed and parsed.get("state") == "APPLIED":
            receipt = parsed
        buckets = conn.execute(
            "SELECT bucket, lower_c, upper_c, probability"
            " FROM basket_probability_bridge_bucket_probability"
            " WHERE history_id=?",
            (history_id,),
        ).fetchall()
        if not buckets:
            continue
        keyed = [
            (float(probability), str(bucket))
            for bucket, _lower, _upper, probability in buckets
        ]
        # ``pre_band=False`` keeps the claims exactly as served, factors
        # included -- the residual audit's view; the fit keeps inverting.
        if receipt is not None and pre_band:
            keyed = _invert_band_receipt(keyed, receipt)
        claim_by_bucket = {key: probability for probability, key in keyed}
        entries = []
        for bucket, lower, upper, _probability in buckets:
            lower_c = -1e9 if lower is None else float(lower)
            upper_c = 1e9 if upper is None else float(upper)
            entries.append((
                claim_by_bucket[str(bucket)],
                1 if lower_c <= actual < upper_c else 0,
                lower_c,
            ))
        entries.sort(key=lambda entry: entry[2])
        by_station_day[(str(station), str(event))].append(entries)
    record_pass(
        "band_reliability.serving_lane_by_station_day", stage=_check_stage(stage),
        pre_band=pre_band, min_decision_at_utc=min_decision_at_utc, conn=conn,
    )
    return dict(by_station_day)


def _first_decision_by_station_day(
    conn: sqlite3.Connection,
    *,
    stage: str,
    min_decision_at_utc: str | None = None,
) -> dict[tuple[str, str], str]:
    """When each station-day's first decision was taken.

    The lane's rows are that first decision, and the veto has to know which
    of them the incumbent was actually serving.  Event date will not answer
    it: a decision for an event two days out is taken two days early, so a
    day AFTER an activation routinely carries a decision from before it.
    """

    query = (
        "SELECT station_id, event_date, MIN(decision_at_utc)"
        " FROM basket_probability_bridge_history"
        " WHERE stage=?"
    )
    parameters: list[Any] = [_check_stage(stage)]
    if min_decision_at_utc:
        query += " AND decision_at_utc>=?"
        parameters.append(str(min_decision_at_utc))
    query += " GROUP BY station_id, event_date"
    return {
        (str(station), str(event)): str(decided)
        for station, event, decided in conn.execute(query, parameters)
    }
