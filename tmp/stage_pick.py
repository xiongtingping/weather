"""Stage-aware selection of a station-day's belief, shared by the re-runs.

The rule matches the production fix: observation stage first, clock second.
Once a P1 row exists at or before the cutoff it is the belief, because P1 is
the only chain that ingests the day's METAR and applies the hard lower bound.
A P0 row written later is a forecast produced after the answer was measured.

Within one decision instant P1 writes the prefix chain step by step, so the
LAST row at that instant is the complete prefix -- taking an earlier one
reads a belief that has swallowed only part of the day's observations.
"""
import json, sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DB = "file:data/bayes_basket_target_ssot.db?mode=ro"
RANK = {"P1": 2, "P0-M": 1, "P0": 0}


def load_chains(conn, keys=None):
    """All committed rows per station-day, ascending, with stage."""
    rows = {}
    for r in conn.execute(
        "SELECT station_id,event_date,stage,decision_at_utc,envelope_json"
        " FROM basket_probability_bridge_history WHERE state='COMMITTED'"
        " ORDER BY decision_at_utc"
    ):
        k = (str(r[0]), str(r[1]))
        if keys is not None and k not in keys:
            continue
        rows.setdefault(k, []).append((str(r[2]), str(r[3]), r[4]))
    return rows


def pick(chain, cutoff=None, predicate=None):
    """The believed distribution: best stage first, then latest.

    ``predicate`` filters candidate rows (e.g. "held bucket still alive").
    It is applied WITHIN a stage, never across: skipping a P1 row because it
    zeroed the held window and falling back to a P0 row would select exactly
    the observation-blind belief this helper exists to avoid.
    """
    best = None
    for stage, at, blob in chain:
        if cutoff is not None and at > cutoff:
            continue
        env = json.loads(blob)
        if predicate is not None and not predicate(env):
            continue
        rank = RANK.get(stage, 0)
        if best is None or (rank, at) >= (best[0], best[1]):
            best = (rank, at, stage, env)
    return best


def local_close(day, tz):
    return datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(tz)) + timedelta(days=1)


def parse(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
