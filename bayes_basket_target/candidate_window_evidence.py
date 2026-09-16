"""Production's own record of every window it enumerated, and the truth label.

Charter 140 (user ruling, 2026-09-16: "自动激活。把数据来源搬到生产").  The
lambda and beta lanes fit on every window the enumeration offered, refused
ones included -- fitting on seats conditions the sample on the estimate and
ratchets the gate (charter 87).  Production's decision record kept only the
shortlist (EPWA 2026-09-15 P0: fifteen enumerated, one recorded), so the only
table that held the whole enumeration was the paper ledger's
``paper_trade_candidate_window``: production's function on production's
envelopes and books, but sampled at the cycle the ledger's walk stopped on,
rewritten whenever a rule change re-walked a day, and enumerated against paper
holdings.  This module moves the population onto production.

Three objects, two of them append-only:

``basket_candidate_window_evidence``
    one row per window per committed production decision fact, written in the
    same transaction as the fact (``record_fact_windows``).  The numbers are
    the enumeration's own row, projected by ``candidate_windows_v1`` inside
    the compile; ``decision_at_utc`` is the envelope's decision instant, the
    clock every claim lane's anchors and joins are on.  ``sampled`` marks the
    first decision of each (station, day, stage, lead band): production
    re-decides about fifteen times a scope, and a fit that took every
    re-decision would weight a day by how often its book moved.
``basket_station_day_settlement_label``
    the settled bucket of a station-day per truth revision, off the truth lane
    and the day's own committed ladder, with the moment the revision was
    committed as the moment the label became knowable.
``basket_candidate_window_scored``
    the view the fits read: sampled rows only, scored against the standing
    label, under the column names the paper table carried so the fitting code
    is unchanged apart from where it reads.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

from .basket_kernel.delta_v2 import (
    SEAT_SHAPE_RULE_VERSION,
    beta_lead_band,
    claim_transform_fingerprint,
)
from .contracts import content_sha256

EVIDENCE_TABLE = "basket_candidate_window_evidence"
LABEL_TABLE = "basket_station_day_settlement_label"
SCORED_VIEW = "basket_candidate_window_scored"
LEDGER_TABLE = "paper_trade_candidate_window"

#: Where the lambda and beta lanes read their population.  ``production`` is
#: the rule; ``ledger`` reads the paper table as it did before charter 140 and
#: exists for comparing the two, never for serving.
EVIDENCE_SOURCE = str(
    os.environ.get("PM_CLAIM_LANE_EVIDENCE", "production") or "production"
).strip().lower()
if EVIDENCE_SOURCE not in {"production", "ledger"}:
    raise ValueError(f"unknown claim lane evidence source: {EVIDENCE_SOURCE}")
CANDIDATE_SOURCE = SCORED_VIEW if EVIDENCE_SOURCE == "production" else LEDGER_TABLE

#: The earliest event day the labeller resolves on its first pass.  Labels are
#: a property of the truth, so older days cost nothing but rows; this floor is
#: the paper book's own, which every lane that reads labels starts after.
LABEL_FLOOR_EVENT_DATE = "2026-08-15"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {EVIDENCE_TABLE} (
  evidence_id TEXT PRIMARY KEY,
  fact_id TEXT NOT NULL,
  decision_id TEXT NOT NULL,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  stage TEXT NOT NULL CHECK(stage IN ('P0','P0-M','P1')),
  decision_at_utc TEXT NOT NULL,
  production_decision_at_utc TEXT NOT NULL,
  lead_band TEXT NOT NULL DEFAULT '',
  sampled INTEGER NOT NULL CHECK(sampled IN (0,1)),
  buckets_json TEXT NOT NULL,
  window_size INTEGER NOT NULL,
  win_probability REAL NOT NULL,
  raw_win_probability REAL,
  all_in_price REAL NOT NULL,
  gate_agreement REAL,
  admissible INTEGER NOT NULL CHECK(admissible IN (0,1)),
  seated INTEGER NOT NULL CHECK(seated IN (0,1)),
  band_axis TEXT,
  band_table_sha256 TEXT,
  claim_transform_sha256 TEXT,
  leg_claim_cost_json TEXT,
  seat_shape_rule TEXT,
  market_mid REAL,
  hours_to_cutoff REAL,
  created_at_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS {EVIDENCE_TABLE}_stage_day_idx
  ON {EVIDENCE_TABLE}(stage, event_date, sampled);
CREATE INDEX IF NOT EXISTS {EVIDENCE_TABLE}_scope_idx
  ON {EVIDENCE_TABLE}(station_id, event_date, stage, lead_band, sampled);
CREATE INDEX IF NOT EXISTS {EVIDENCE_TABLE}_fact_idx
  ON {EVIDENCE_TABLE}(fact_id);
CREATE TRIGGER IF NOT EXISTS {EVIDENCE_TABLE}_no_update
  BEFORE UPDATE ON {EVIDENCE_TABLE}
  BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS {EVIDENCE_TABLE}_no_delete
  BEFORE DELETE ON {EVIDENCE_TABLE}
  BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TABLE IF NOT EXISTS {LABEL_TABLE} (
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  truth_revision_no INTEGER NOT NULL,
  settled_bucket TEXT NOT NULL,
  settled_tmax_c REAL NOT NULL,
  truth_content_sha256 TEXT NOT NULL,
  known_at_utc TEXT NOT NULL,
  ladder_decision_at_utc TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  PRIMARY KEY(station_id, event_date, truth_revision_no)
);
CREATE TRIGGER IF NOT EXISTS {LABEL_TABLE}_no_update
  BEFORE UPDATE ON {LABEL_TABLE}
  BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS {LABEL_TABLE}_no_delete
  BEFORE DELETE ON {LABEL_TABLE}
  BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE VIEW IF NOT EXISTS {SCORED_VIEW} AS
SELECT
  e.evidence_id AS candidate_id,
  e.station_id, e.event_date, e.stage, e.decision_at_utc,
  e.buckets_json, e.window_size, e.win_probability, e.raw_win_probability,
  e.all_in_price, e.gate_agreement, e.admissible, e.seated,
  '' AS selection_rule_sha256,
  e.band_axis, e.band_table_sha256, e.claim_transform_sha256,
  e.leg_claim_cost_json, e.seat_shape_rule, e.market_mid, e.hours_to_cutoff,
  l.settled_bucket,
  CASE
    WHEN l.settled_bucket IS NULL THEN NULL
    WHEN EXISTS (
      SELECT 1 FROM json_each(e.buckets_json) j
      WHERE j.value = l.settled_bucket
    ) THEN 1 ELSE 0
  END AS hit,
  l.known_at_utc AS settled_at_utc,
  l.truth_revision_no,
  l.truth_content_sha256
FROM {EVIDENCE_TABLE} e
LEFT JOIN {LABEL_TABLE} l
  ON l.station_id = e.station_id AND l.event_date = e.event_date
 AND l.truth_revision_no = (
   SELECT MAX(x.truth_revision_no) FROM {LABEL_TABLE} x
   WHERE x.station_id = e.station_id AND x.event_date = e.event_date)
WHERE e.sampled = 1;
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    # Statement by statement, never ``executescript``: that commits whatever
    # transaction is open, and the writer runs inside the atomic ledger's.
    for statement in _statements(_SCHEMA):
        conn.execute(statement)


def _statements(script: str) -> list[str]:
    """The DDL as single statements, for a caller already in a transaction.

    ``executescript`` commits first, which inside the atomic ledger's
    transaction would commit the decision half-written.  Triggers carry a
    semicolon inside ``BEGIN ... END`` and the view one inside its subquery,
    so the split is on statement heads rather than on every semicolon.
    """

    out: list[str] = []
    current: list[str] = []
    for line in script.strip().splitlines():
        if line.startswith("CREATE ") and current:
            out.append("\n".join(current).rstrip().rstrip(";"))
            current = []
        current.append(line)
    if current:
        out.append("\n".join(current).rstrip().rstrip(";"))
    return [statement for statement in out if statement.strip()]


def candidate_windows_v1(
    windows: Sequence[Mapping[str, Any]],
    *,
    seated_buckets: Sequence[str] | None,
) -> list[dict[str, Any]]:
    """The enumeration's rows, projected onto what the claim lanes read.

    Called inside the compile, where the rows exist, so the numbers are the
    enumeration's own and nothing downstream re-derives a claim or a price.
    A row without a price cannot be scored and is left out, as the paper
    table left it out.
    """

    seat = tuple(str(bucket) for bucket in (seated_buckets or ()))
    out: list[dict[str, Any]] = []
    for row in windows:
        buckets = [str(bucket) for bucket in row.get("buckets") or ()]
        if not buckets or row.get("all_in_price") is None:
            continue
        out.append({
            "buckets": buckets,
            "win_probability": float(row.get("win_probability") or 0.0),
            "raw_win_probability": (
                None if row.get("raw_win_probability") is None
                else float(row["raw_win_probability"])
            ),
            "all_in_price": float(row["all_in_price"]),
            "gate_agreement": (
                None if row.get("effective_edge_agreement") is None
                else float(row["effective_edge_agreement"])
            ),
            "admissible": bool(row.get("admissible", True)),
            "seated": bool(seat) and tuple(buckets) == seat,
            "holds_centre": float(row.get("center_gap_buckets") or 0.0) <= 0.0,
            "leg_claim_cost": row.get("leg_claim_cost") or None,
            "market_mid": (
                None if row.get("beta_market_mid") is None
                else float(row["beta_market_mid"])
            ),
            "hours_to_cutoff": (
                None if row.get("beta_hours_to_cutoff") is None
                else float(row["beta_hours_to_cutoff"])
            ),
        })
    return out


def _envelope_receipt(
    conn: sqlite3.Connection, envelope_id: str
) -> tuple[str | None, str | None, str | None]:
    """(decision instant, band axis, band table sha) of the fact's envelope."""

    try:
        row = conn.execute(
            "SELECT decision_at_utc,"
            " json_extract(lineage_json, '$.band_reliability_json')"
            " FROM basket_probability_envelope WHERE envelope_id=?",
            (str(envelope_id),),
        ).fetchone()
    except sqlite3.Error:
        return None, None, None
    if not row:
        return None, None, None
    band = row[1]
    try:
        band = json.loads(band) if isinstance(band, str) else band
    except (TypeError, ValueError):
        band = None
    band = band if isinstance(band, dict) else {}
    return (
        str(row[0]) if row[0] else None,
        str(band["axis"]) if band.get("axis") else None,
        str(band["band_table_sha256"]) if band.get("band_table_sha256") else None,
    )


def _off_centre_sha(conn: sqlite3.Connection, stage: str) -> str | None:
    try:
        from .off_centre_claim_store import load_off_centre_bands  # noqa: PLC0415

        loaded = load_off_centre_bands(conn, stage=str(stage))
    except Exception:  # noqa: BLE001 -- no store is the bootstrap
        return None
    return str(loaded[1]) if loaded else None


def record_fact_windows(
    conn: sqlite3.Connection, fact: Mapping[str, Any]
) -> int:
    """Append the enumeration a newly committed decision fact carries.

    Runs inside the atomic ledger's transaction, right after the fact itself,
    so a decision and its evidence commit or roll back together.  A fact
    whose plan carries no enumeration -- a compile from before charter 140, a
    stage-zero blocker plan -- writes nothing.  Returns the rows written.
    """

    try:
        feature = json.loads(str(fact.get("feature_json") or "{}")) or {}
    except (TypeError, ValueError):
        return 0
    plan = feature.get("plan") if isinstance(feature, Mapping) else None
    audit = plan.get("delta_v2_audit") if isinstance(plan, Mapping) else None
    windows = audit.get("candidate_windows") if isinstance(audit, Mapping) else None
    if not isinstance(windows, list) or not windows:
        return 0
    station = str(fact["station_id"])
    day = str(fact["event_date"])
    stage = str(fact["stage"])
    fact_id = str(fact["fact_id"])
    decided, band_axis, band_sha = _envelope_receipt(
        conn, str(fact.get("probability_envelope_id") or "")
    )
    production_at = str(fact.get("decision_at_utc") or "")
    decided = decided or production_at
    ensure_schema(conn)
    off_centre = _off_centre_sha(conn, stage)
    centre_sha = claim_transform_fingerprint(holds_centre=True)
    off_sha = claim_transform_fingerprint(
        holds_centre=False, off_centre_table_sha=off_centre
    )
    now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    lead_sampled: dict[str, int] = {}
    written = 0
    for row in windows:
        if not isinstance(row, Mapping):
            continue
        buckets = [str(bucket) for bucket in row.get("buckets") or ()]
        if not buckets:
            continue
        lead = beta_lead_band(row.get("hours_to_cutoff")) or ""
        if lead not in lead_sampled:
            # The first decision of this scope and lead band is the sample.
            # Facts are written in decision order by one writer, so "no row
            # yet" is "this is the first".
            lead_sampled[lead] = 0 if conn.execute(
                f"SELECT 1 FROM {EVIDENCE_TABLE}"
                " WHERE station_id=? AND event_date=? AND stage=?"
                "   AND lead_band=? AND sampled=1 AND fact_id<>? LIMIT 1",
                (station, day, stage, lead, fact_id),
            ).fetchone() else 1
        evidence_id = "cwe_" + content_sha256({
            "fact_id": fact_id, "buckets": buckets,
        })[:40]
        cursor = conn.execute(
            f"INSERT OR IGNORE INTO {EVIDENCE_TABLE} ("
            " evidence_id, fact_id, decision_id, station_id, event_date, stage,"
            " decision_at_utc, production_decision_at_utc, lead_band, sampled,"
            " buckets_json, window_size, win_probability, raw_win_probability,"
            " all_in_price, gate_agreement, admissible, seated, band_axis,"
            " band_table_sha256, claim_transform_sha256, leg_claim_cost_json,"
            " seat_shape_rule, market_mid, hours_to_cutoff, created_at_utc"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                evidence_id, fact_id, str(fact.get("decision_id") or ""),
                station, day, stage, decided, production_at, lead,
                lead_sampled[lead],
                json.dumps(buckets, ensure_ascii=False), len(buckets),
                float(row.get("win_probability") or 0.0),
                row.get("raw_win_probability"),
                float(row.get("all_in_price") or 0.0),
                row.get("gate_agreement"),
                1 if row.get("admissible", True) else 0,
                1 if row.get("seated") else 0,
                band_axis, band_sha,
                centre_sha if row.get("holds_centre", True) else off_sha,
                (
                    json.dumps(row["leg_claim_cost"], sort_keys=True,
                               ensure_ascii=False)
                    if row.get("leg_claim_cost") else None
                ),
                SEAT_SHAPE_RULE_VERSION,
                row.get("market_mid"),
                row.get("hours_to_cutoff"),
                now,
            ),
        )
        written += int(cursor.rowcount or 0)
    return written


def _settled_bucket(
    ladder: Sequence[tuple[Any, ...]], tmax_c: float
) -> str | None:
    """The rung a settled value falls in, lower edge inclusive.

    The settler's rule (``paper_trading_ledger._settled_bucket``), stated
    once more here because the library cannot import a script; the test
    suite pins the two to the same answer.
    """

    for bucket, _ordinal, lower, upper in ladder:
        low = -1e9 if lower is None else float(lower)
        high = 1e9 if upper is None else float(upper)
        if low <= tmax_c < high:
            return str(bucket)
    return None


def standing_labels(
    conn: sqlite3.Connection,
    *,
    floor_event_date: str = LABEL_FLOOR_EVENT_DATE,
    only_unlabelled: bool = True,
) -> tuple[list[tuple[Any, ...]], dict[str, int]]:
    """The label rows the standing truth revisions resolve to.

    The revision that stands is the highest ACCEPTED one (charter 44); its
    commit time is when a fit may first read it.  The ladder is the
    station-day's newest committed decision's -- a station-day carries one
    ladder -- so a value is placed on the rungs the day was priced against.
    A value outside every rung is not labelled: that is not a settlement
    anything bought could be scored on.
    """

    counts = {"labelled": 0, "outside_ladder": 0, "no_ladder": 0}
    has_labels = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (LABEL_TABLE,),
    ).fetchone() is not None
    unlabelled_clause = (
        "   AND NOT EXISTS ("
        f"     SELECT 1 FROM {LABEL_TABLE} l"
        "     WHERE l.station_id=t.station_id AND l.event_date=t.local_date"
        "       AND l.truth_revision_no=t.revision_no)"
        if only_unlabelled and has_labels else ""
    )
    rows = conn.execute(
        "SELECT t.station_id, t.local_date, t.revision_no, t.official_tmax_c,"
        " t.content_sha256, t.canonical_committed_at_utc"
        " FROM calibration_station_daily_high_truth t"
        " WHERE t.qc_status='ACCEPTED' AND t.local_date>=?"
        "   AND t.revision_no = ("
        "     SELECT MAX(x.revision_no) FROM calibration_station_daily_high_truth x"
        "     WHERE x.station_id=t.station_id AND x.local_date=t.local_date"
        "       AND x.qc_status='ACCEPTED')" + unlabelled_clause,
        (str(floor_event_date),),
    ).fetchall()
    now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    out: list[tuple[Any, ...]] = []
    for station, day, revision, tmax, sha, committed in rows:
        head = conn.execute(
            "SELECT history_id, decision_at_utc"
            " FROM basket_probability_bridge_history"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
            " ORDER BY decision_at_utc DESC LIMIT 1",
            (station, day),
        ).fetchone()
        if not head:
            counts["no_ladder"] += 1
            continue
        ladder = conn.execute(
            "SELECT bucket, ordinal, lower_c, upper_c"
            " FROM basket_probability_bridge_bucket_probability"
            " WHERE history_id=? ORDER BY ordinal",
            (head[0],),
        ).fetchall()
        bucket = _settled_bucket(ladder, float(tmax)) if ladder else None
        if bucket is None:
            counts["no_ladder" if not ladder else "outside_ladder"] += 1
            continue
        out.append((
            str(station), str(day), int(revision), bucket, float(tmax),
            str(sha or ""), str(committed or now), str(head[1]), now,
        ))
        counts["labelled"] += 1
    return out, counts


def refresh_settlement_labels(
    conn: sqlite3.Connection,
    *,
    floor_event_date: str = LABEL_FLOOR_EVENT_DATE,
) -> dict[str, int]:
    """Append a label for every standing truth revision not yet labelled."""

    ensure_schema(conn)
    rows, counts = standing_labels(conn, floor_event_date=floor_event_date)
    conn.executemany(
        f"INSERT OR IGNORE INTO {LABEL_TABLE} ("
        " station_id, event_date, truth_revision_no, settled_bucket,"
        " settled_tmax_c, truth_content_sha256, known_at_utc,"
        " ladder_decision_at_utc, created_at_utc"
        ") VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return counts


def latest_labelled_event_date(conn: sqlite3.Connection) -> str | None:
    """The newest event day with a label: where a rolling fit ends."""

    try:
        row = conn.execute(f"SELECT MAX(event_date) FROM {LABEL_TABLE}").fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row and row[0] else None


__all__ = [
    "CANDIDATE_SOURCE",
    "EVIDENCE_SOURCE",
    "EVIDENCE_TABLE",
    "LABEL_TABLE",
    "SCORED_VIEW",
    "candidate_windows_v1",
    "ensure_schema",
    "latest_labelled_event_date",
    "record_fact_windows",
    "refresh_settlement_labels",
    "standing_labels",
]
