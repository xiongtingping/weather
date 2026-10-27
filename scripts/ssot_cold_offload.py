#!/usr/bin/env python3
"""Move the aged rows of the SSOT's append-only history tables to the cold tier.

User ruling 2026-09-27 (「优化ssot，移动部分不影响生产的数据至外置硬盘」).

The SSOT only ever grows.  Charters 218, 220 and 221 took the duplicated bytes
out of what is written, and the store still gains about five GB a day against
a ceiling raised from 23 to 68 GiB in eleven steps since 2026-08-22 -- because
nothing ever leaves.  Most of what it keeps is read once, by the cycle that wrote it,
and never again: a P0 preselection plan is looked up by the id of the decision
being committed and, for the edge evidence, as the newest row of its station;
a cross-system OOS evaluation is read as the latest two of its reader key and,
by the stable-pool gate, for 21 days.  Past those windows a row is history.
This moves that history -- rows, not files -- into one SQLite file per table
on the charter 196 cold tier, and deletes it from the SSOT only after the copy
is committed on the volume and read back identical.

What decides a table, and what is kept (``POLICIES``, one entry per table):

* a time column and a hot window: rows whose time is before
  ``today - hot_days`` are candidates, compared at date precision so every
  ISO spelling the writers use orders the same way; a policy may narrow the
  candidates further (``candidate_where``);
* per-reader protections, derived from the readers rather than guessed:
  the newest *k* rows of a reader's own key (a latest-per-key reader stays
  exact at every instant after the cutoff), every row of a station-day that
  carried real money, and floors computed at run time from what a reader is
  still waiting to judge;
* always the table's own newest rowid, so no rowid a moved row had is ever
  handed out again, and any operator hold (``--restore`` writes one).

What makes a table eligible at all: production reads it only through the
windows above; it is a rowid table; no other table declares a FOREIGN KEY to
it; and its only DELETE triggers are the append-only ``RAISE(ABORT)`` guards.
Anything else is refused table by table, and the reason is in the receipt.

The move, per batch:

1. read the rows (``SELECT rowid, *``) off the SSOT, outside any write lock;
2. ``INSERT OR IGNORE`` them, rowid and all, into the table's cold file
   (the SSOT's columns and declared types, append-only, its CREATE TABLE text
   recorded beside it), together with one row in the cold file's own batch
   ledger, and commit with ``synchronous=FULL``;
3. read them back through a fresh connection and compare every value *and
   its SQLite storage class* with the source (the batch's digest over the
   same goes into both ledgers);
4. ``BEGIN IMMEDIATE`` on the SSOT, re-read the same rows and require them
   unchanged (a row that changed after it was copied is not deleted),
   delete them on a connection whose triggers are switched off
   (``SQLITE_DBCONFIG_ENABLE_TRIGGER`` -- the schema is never touched, so
   every other connection stays guarded throughout), commit;
5. append the batch to the manifest on the internal disk.

A crash between 2 and 4 leaves the rows in both places; the next run finds
them, re-verifies and deletes.  Nothing is ever deleted that is not already
on the volume, verified.

The file does not shrink -- SQLite puts the freed pages on its freelist and
every later write reuses them before the file grows again (the same arithmetic
as the 2026-09-19 retirement and the market registry retention).  Run daily,
the store stops growing; giving the file back needs a compaction, which is a
separate decision.

The cold tier is usable or the lane does nothing: unplugged, TCC-denied under
launchd, read-only, on the internal disk or short of space, every row stays
hot and the receipt says why.  Production never reads the cold files; only
offline readers do, through :func:`open_cold_table`, which asserts the volume
first.  ``--restore`` puts rows back verbatim and writes a hold so the next run
does not move them again.

Dry-run is the default; ``--apply`` moves.
"""

from __future__ import annotations

import argparse
from array import array
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import fcntl
import hashlib
import heapq
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.native_ensemble_oos import (  # noqa: E402
    DEFAULT_MIN_HOLDOUT_DAYS,
    ROLLING_TRAIN_WINDOW_DAYS,
    STABLE_POOL_GATE_HISTORY_KEY,
    STABLE_POOL_GATE_LOOKBACK_DAYS,
    STABLE_POOL_GATE_MEMBERSHIP_SQL,
    STABLE_POOL_GATE_SCORES_SQL,
    _EVALUATION_READER_KEY as OOS_EVALUATION_READER_KEY,
    stable_pool_gate_row_days,
)
from bayes_basket_target.offloaded_paths import (  # noqa: E402
    DEFAULT_COLD_TIER_MIN_FREE_BYTES,
    OffloadedVolumeMissing,
    assert_present,
    cold_tier_root,
    cold_tier_status,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_TARGET_DB_BYTES,
    READER_YIELD_WAL_BYTES,
    install_wal_yield,
    wait_for_wal_under,
)
from bayes_basket_target.valuation_store import WINDOW_DAYS as VALUATION_WINDOW_DAYS  # noqa: E402
from bayes_basket_target.board_readings import SEAT_LOOKBACK_DAYS as BOARD_SEAT_LOOKBACK_DAYS  # noqa: E402

SCHEMA = "ssot_cold_offload_v1"
BATCH_SCHEMA = "ssot_cold_offload_batch_v1"
DEFAULT_SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
#: Under the charter 196 cold root, beside ``evicted/``.
COLD_SUBDIR = "ssot_rows"
#: The cold file's own record of what it holds and where it came from.
COLD_BATCH_TABLE = "__ssot_cold_batches"
#: The SSOT's CREATE TABLE text for every archived table, as first seen.
COLD_SCHEMA_TABLE = "__ssot_cold_schema"
RUNTIME = ROOT / "data" / "runtime"
MANIFEST = RUNTIME / "ssot_cold_offload.jsonl"
HOLDS = RUNTIME / "ssot_cold_offload_holds.json"
#: The days each superseded-rule row gives (MoveSuperseded.row_days), kept by
#: rowid beside the manifest.  Those rows are append-only and never updated,
#: so a row's days never change; reading them again was a JSON parse of every
#: evaluation in the window each run (146 s of a 185 s plan, 2026-10-15).
ROW_DAYS_CACHE = RUNTIME / "ssot_cold_offload_row_days.sqlite"
RECEIPT = RUNTIME / "ssot_cold_offload_latest.json"
LOCK = RUNTIME / "ssot_cold_offload.lock"

MIB = 1024 * 1024
GIB = 1024 * MIB
#: A batch is the unit that holds the one SSOT write lock, so it is sized by
#: the lock: the 2026-09-19 retirement held it about a second per 8 MiB.
BATCH_ROWS = 1000
BATCH_BYTES = 8 * MIB
#: Rows read per SELECT while planning and copying.  Well under SQLite's
#: variable limit, and small enough that no statement stays open for long on
#: a store whose WAL the resident has to be able to checkpoint.
READ_CHUNK = 500
#: After each delete the lane sleeps at least this long, and at least
#: ``pause_factor`` times what the delete held the lock for: measured on
#: 2026-09-19, factor 1 saturated the disk and stretched one production cycle
#: from 5-9 minutes to 11; factor 3 left the resident its turns.
PAUSE_SEC = 0.5
DEFAULT_PAUSE_FACTOR = 3.0
#: A batch's lock is what production waits behind, so the batch follows the
#: lock: one held past LOCK_TARGET_SEC halves the next batch (to
#: MIN_BATCH_ROWS), one well under it lets the next grow back toward the
#: configured size.  2026-10-16 02:23-03:08Z, on a machine swapping 3.5 GiB:
#: 1,000-row batches held the writer 33 s (market snapshots) and 90 s
#: (inventory snapshots) each, while the same batches take about a second on
#: a quiet one.
LOCK_TARGET_SEC = 5.0
MIN_BATCH_ROWS = 50
#: The resident truncates the WAL past 256 MiB when it is idle; this lane
#: stops adding to it until a checkpoint has caught up.
MAX_WAL_BYTES = 256 * MIB
WAL_WAIT_LIMIT_SEC = 1800.0
#: Deletes put pages on the freelist and frames in the WAL.  Below this much
#: free internal disk the lane stops rather than be the thing that fills it.
MIN_INTERNAL_FREE_BYTES = 2 * GIB
#: Re-check the volume at least this often while moving.
COLD_RECHECK_BYTES = 1 * GIB
DEFAULT_ESTIMATE_SAMPLE = 2000
#: ``scripts/acknowledge_serving_arithmetic.py`` (``SHAPE_SHIFT_WINDOW_HOURS``)
#: judges a fingerprint on the plans this many hours either side of each
#: moment it names; the test pins the two together.
SERVING_AUDIT_WINDOW_HOURS = 12
#: The paper ledger's daily pass walks settled event dates today-4 .. today-2
#: (``--window-days`` 3 ending ``DAILY_PASS_SETTLE_LAG_DAYS`` 2 back), and each
#: replayed decision re-reads its regime artifact.  Pinned by the test.
PAPER_LEDGER_EVENT_DAYS = 4


class OffloadRefused(RuntimeError):
    """A table this lane will not touch, and why (fail closed, table by table)."""


# --------------------------------------------------------------------------
# Policies.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class KeepLatest:
    """Per ``key``, the newest ``count`` rows before the cutoff stay hot.

    ``order`` is the reader's own ordering, newest first (each column is read
    DESC; ``rowid`` is allowed).  A reader that takes the latest *k* rows of a
    key at any instant after the cutoff then gets exactly the rows it got
    before: the rows after the cutoff all stay, and the ones it reaches below
    the cutoff are at most these *k*.
    """

    key: tuple[str, ...]
    order: tuple[str, ...]
    count: int
    why: str


@dataclass(frozen=True)
class KeepRecent:
    """Rows whose ``column`` is one of ``values`` keep a longer window.

    For a table whose readers want some kinds of row for longer than others:
    a row of one of those kinds stays while its time is on or after the run's
    as-of date less ``days``, though the table's own window has passed.
    """

    name: str
    column: str
    values: tuple[str, ...]
    days: int
    why: str


@dataclass(frozen=True)
class KeepVectorRefs:
    """Every row an execution version vector names by content hash stays hot.

    ``basket_target_execution_version_vectors.<vector_column>`` holds the
    ``content_sha256`` of the row an order's execution was bound to, and
    ``execution_transition_runtime`` re-reads the row by that hash.  Rows are
    found per station-day (the vector's cycle names it) off the table's own
    station-day index, so no large row body is read except the matches.
    ``instant_column`` narrows a station-day to the rows whose instant equals
    the vector's ``quote_observed_at`` (parsed: the two are spelled with
    different precisions) before their hashes are read.  A hash not found at
    its instant is looked for across its whole station-day; a hash the
    station-day does not hold at all names a book this table never had (or
    no longer has under that hash), and keeping the day would protect no row
    a reader can find -- so it is counted (``absent_vector_refs``), not kept.
    """

    vector_column: str
    why: str
    instant_column: str | None = None


@dataclass(frozen=True)
class KeepDecisionPlans:
    """The plan each live decision acted on stays hot, by name.

    A decision with a live-authorised intent names the compiled plan it acted
    on in ``details_json.production_plan_sha256``, and the preselection row
    carrying that plan is committed in the same transaction, so its
    ``created_at`` is the decision's to the millisecond.  The row is found off
    ``instant_index`` at that instant and confirmed by the plan's own hash
    (``plan_json.compiled_plan.production_plan_sha256``, the key the
    dashboard's live book reads), so only those rows' JSON is read.  A
    decision whose plan cannot be found that way keeps its whole station-day.
    Decisions that name no plan (the lanes that do not compile one) keep
    nothing.
    """

    instant_index: str
    why: str


@dataclass(frozen=True)
class KeepDecisionEnvelopes:
    """The envelope each live decision stood on stays hot, by name.

    A decision with a live-authorised intent records, in
    ``details_json.forward_evidence_binding``, the stage, the decision instant
    and the ``content_sha256`` of the probability envelope it acted on -- the
    key ``live_book`` re-reads it by.  The row is found the same way: the
    committed envelopes of that station-day and stage within that second, off
    ``scope_index``, then the hash picks the one.  A decision whose envelope
    cannot be found that way keeps its whole station-day; one that names no
    envelope keeps nothing.
    """

    scope_index: str
    why: str


@dataclass(frozen=True)
class KeepNamed:
    """Every row whose ``column`` a reader's own query still names stays hot.

    ``sql`` returns one column of values; it runs once per plan, and must be
    cheap (an index scan), since it runs against the live store.
    """

    name: str
    column: str
    sql: str
    why: str
    #: When set, ``sql`` takes ``:floor`` -- the run's as-of date less this
    #: many days -- so a reader's window ages out with the run, not the wall
    #: clock.
    window_days: int | None = None


@dataclass(frozen=True)
class MoveSuperseded:
    """Rows inside the window whose every answer a newer row of the key gives.

    For a table whose window reader takes, per ``key`` and per day, the newest
    row that gives that day (``row_days``), ordered by ``order`` (newest
    first).  Walking a key newest first, a row whose days the newer rows
    already all give can never be that reader's answer for any instant from
    now on -- every newer row stays in the window at least as long -- so it
    leaves though it is inside the window.  ``keep_latest`` of the policy
    still keeps its newest rows per its own key, inside the window too.

    Read one ``partition`` value at a time, each in its own short read, so
    the walk never holds a snapshot open across the whole table.
    """

    key: tuple[str, ...]
    order: tuple[str, ...]
    partition: str
    #: SQL expressions read per row and handed to ``row_days`` in order.
    day_sql: tuple[str, ...]
    row_days: Callable[..., Mapping[str, Any]]
    why: str
    #: Names ``key``/``order`` may use that are not the table's own columns,
    #: for a reader whose newest-first order lives in another table (the OOS
    #: predictions' generation is the activation's): name -> (the row's own
    #: columns, a loader run once per plan that returns {those values: value}).
    #: Looked up in memory, never a per-row subquery: a correlated read over
    #: the 2.8M predictions held its snapshot past the WAL yield and was
    #: interrupted after 58 minutes (2026-10-19).
    computed: Mapping[str, tuple[tuple[str, ...], Callable[[sqlite3.Connection], Mapping[tuple, Any]]]] = (
        field(default_factory=dict)
    )


@dataclass(frozen=True)
class DynamicFloor:
    """Rows dated on or after a day computed at run time stay hot.

    ``resolve`` reads the SSOT and returns a ``YYYY-MM-DD`` day or None (no
    floor today); it raises :class:`OffloadRefused` when it cannot tell, and
    the table is then left alone for this run.
    """

    name: str
    why: str
    resolve: Callable[[sqlite3.Connection], str | None]


@dataclass(frozen=True)
class TablePolicy:
    table: str
    time_column: str
    hot_days: int
    readers: str
    #: An SQL predicate a row must also satisfy to leave at all.
    candidate_where: str | None = None
    keep_latest: tuple[KeepLatest, ...] = ()
    keep_recent: tuple[KeepRecent, ...] = ()
    keep_vector_refs: KeepVectorRefs | None = None
    keep_decision_plans: KeepDecisionPlans | None = None
    keep_decision_envelopes: KeepDecisionEnvelopes | None = None
    #: Every row another table names by a declared FOREIGN KEY stays hot.
    #: Without it a table with foreign-key children is refused outright.
    keep_foreign_key_refs: bool = False
    keep_named: tuple[KeepNamed, ...] = ()
    floors: tuple[DynamicFloor, ...] = ()
    move_superseded: MoveSuperseded | None = None
    #: Content-addressed side tables the moved rows name, COPIED (never
    #: deleted) into the same cold file so an offline reader can expand a
    #: cold row without the SSOT.
    mirror_tables: tuple[str, ...] = ()
    #: At most this many batches leave per run (None = until the budget).
    #: For a table whose rows are tiny (hundreds of bytes) every batch costs
    #: a write lock and a round trip to the volume for almost no space back,
    #: and left uncapped it eats the run's whole budget on a quiet morning
    #: while the tables that free gigabytes never get their turn (2026-10-12).
    max_batches_per_run: int | None = None

    def cutoff_date(self, as_of: datetime) -> str:
        return (as_of.astimezone(timezone.utc).date() - timedelta(days=int(self.hot_days))).isoformat()

    def describe(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "time_column": self.time_column,
            "hot_days": self.hot_days,
            "candidate_where": self.candidate_where,
            "readers": self.readers,
            "keep_latest": [
                {"key": list(r.key), "order": list(r.order), "count": r.count, "why": r.why}
                for r in self.keep_latest
            ],
            "keep_recent": [
                {"name": r.name, "column": r.column, "values": list(r.values), "days": r.days, "why": r.why}
                for r in self.keep_recent
            ],
            "keep_vector_refs": None if self.keep_vector_refs is None else {
                "vector_column": self.keep_vector_refs.vector_column,
                "instant_column": self.keep_vector_refs.instant_column,
                "why": self.keep_vector_refs.why,
            },
            "keep_decision_plans": None if self.keep_decision_plans is None else {
                "instant_index": self.keep_decision_plans.instant_index,
                "why": self.keep_decision_plans.why,
            },
            "keep_decision_envelopes": None if self.keep_decision_envelopes is None else {
                "scope_index": self.keep_decision_envelopes.scope_index,
                "why": self.keep_decision_envelopes.why,
            },
            "keep_foreign_key_refs": self.keep_foreign_key_refs,
            "keep_named": [{"name": k.name, "column": k.column, "why": k.why} for k in self.keep_named],
            "floors": [{"name": f.name, "why": f.why} for f in self.floors],
            "move_superseded": None if self.move_superseded is None else {
                "key": list(self.move_superseded.key),
                "order": list(self.move_superseded.order),
                "computed": {k: list(v[0]) for k, v in self.move_superseded.computed.items()},
                "why": self.move_superseded.why,
            },
            "mirror_tables": list(self.mirror_tables),
            "max_batches_per_run": self.max_batches_per_run,
        }


def serving_arithmetic_floor(conn: sqlite3.Connection) -> str | None:
    """The day before the oldest serving fingerprint still waiting for a verdict.

    The daily calibration sweep runs ``acknowledge_serving_arithmetic --audit``,
    which judges an unacknowledged fingerprint on the P0 plans twelve hours
    either side of the moment it started serving and of each lane activation
    after it.  While any stage has one, the plans from that day on stay, so
    the audit can still say "inert" or "a bump is owed" instead of "no sample".
    The same ``watchdog`` the audit calls decides it.
    """

    from bayes_basket_target import serving_arithmetic  # noqa: PLC0415

    floors: list[str] = []
    for stage in ("P0", "P1"):
        try:
            reading = serving_arithmetic.watchdog(conn, stage=stage)
        except sqlite3.Error as exc:
            raise OffloadRefused(f"serving_arithmetic_watchdog_unreadable:{stage}") from exc
        if reading.get("acknowledged") or not reading.get("discrepancy"):
            continue
        since = str(reading.get("fingerprint_since_utc") or "")
        try:
            moment = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError as exc:
            raise OffloadRefused(f"serving_arithmetic_since_unreadable:{stage}:{since}") from exc
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        moment -= timedelta(hours=SERVING_AUDIT_WINDOW_HOURS)
        floors.append(moment.astimezone(timezone.utc).date().isoformat())
    return min(floors) if floors else None


#: The P0 envelopes the feature gate (charter 201/227, served) re-scores:
#: event days of its last WINDOW_DAYS; P1's likelihood refit reads 44.
#: Pinned by the test to ``scripts/fit_deterministic_feature_gate.py`` and
#: ``scripts/p1_likelihood_refit_sweep.sh``.
FEATURE_GATE_WINDOW_DAYS = 60
#: How far back a hand-run truth reconciliation may revise a settled day
#: (``scripts/settlement_truth_reconciliation.py`` MAX_DAYS; the daily repair
#: looks back 10).  A revision gives every fact of that day a new label, so a
#: fact stays while its day can still be revised.  Pinned by the test.
RECONCILIATION_MAX_DAYS = 45
#: The native members the weights/OOS fit reads: ROLLING_TRAIN_WINDOW_DAYS
#: before a key's own last DEFAULT_MIN_HOLDOUT_DAYS holdout days.  A key's
#: holdout lags the newest closed day when its lane misses days (the d2 lane
#: was dead 09-03..09-21, eighteen days), so the window carries a margin.
MEMBER_RUN_LAG_MARGIN_DAYS = 22
MEMBER_RUN_HOT_DAYS = ROLLING_TRAIN_WINDOW_DAYS + DEFAULT_MIN_HOLDOUT_DAYS + MEMBER_RUN_LAG_MARGIN_DAYS
#: Member rows are ~600 bytes each: 100 batches (100k rows) is about one
#: day's intake per run and roughly 0.06 GiB back, which keeps up over two
#: runs a day without spending the hour the big-payload tables need.
MEMBER_RUN_MAX_BATCHES_PER_RUN = 100
#: Writers that re-project any day they find missing from a fixed start
#: (metar_physical_forward from 2026-08-01) would write a moved row back;
#: their rows stay.  The TAF TX rows are few and stay with them.
MEMBER_RUN_STAYING_SYSTEMS = ("METAR_PHYSICAL_PATH_TMAX", "OFFICIAL_AIRPORT_TAF_TX")
P1_LIKELIHOOD_WINDOW_DAYS = 44
#: A decision for event date D is taken up to two days before D, so an
#: anchor on the decision clock keeps event dates from two days before it.
EVIDENCE_EVENT_DATE_MARGIN_DAYS = 2


#: The dashboard's truth-landing (books and envelopes from 08-15) and H5
#: (envelopes from 08-27) sections keep their closed days in a cache and,
#: when a code change voids it, recompute through ``_history_conn`` -- the
#: one dashboard path allowed onto the cold tier (user ruling 2026-10-01
#: 「在外置硬盘」).  They are no floor.


def metar_trajectory_training_floor(conn: sqlite3.Connection) -> str | None:
    """P1's trajectory training reads every root of a station, from its first day.

    ``production_worker._run_metar_trajectory_training`` runs
    ``build_bayes_basket_target_metar_trajectory_training_packet.py`` for one
    due station a cycle; the packet reads every P0/P0-M the station has, and
    since 2026-10-01 reads a station-day frozen in its root cache from there.
    The floor is the first station-day the SSOT holds that the cache has not
    frozen: until the packet has run once for every station, that is the
    first day of all.
    """

    from scripts import build_bayes_basket_target_metar_trajectory_training_packet as packet  # noqa: PLC0415

    held = conn.execute(
        "SELECT station_id, event_date FROM basket_probability_envelope"
        " INDEXED BY basket_probability_envelope_asof_idx"
        " WHERE stage IN ('P0','P0-M') AND state='COMMITTED' GROUP BY station_id, event_date"
    ).fetchall()
    if not held:
        return None
    path = Path(packet.ROOT_CACHE_PATH)
    frozen: set[tuple[str, str]] = set()
    if path.is_file():
        try:
            # Not mode=ro: a WAL file whose writer has closed has no -shm,
            # and a read-only open cannot make one.
            cache = sqlite3.connect(str(path))
            cache.execute("PRAGMA query_only=1")
            try:
                frozen = {
                    (str(station), str(day)) for station, day in cache.execute(
                        "SELECT station_id, event_date FROM frozen_day WHERE schema=?",
                        (packet.ROOT_CACHE_SCHEMA,),
                    )
                }
            finally:
                cache.close()
        except sqlite3.Error as exc:
            raise OffloadRefused(f"metar_trajectory_root_cache_unreadable:{path}") from exc
    unfrozen = [str(day) for station, day in held if (str(station), str(day)) not in frozen]
    return min(unfrozen) if unfrozen else None


def evidence_anchor_floor(conn: sqlite3.Connection) -> str | None:
    """The width and off-centre lanes read every decision since their anchor.

    Both refit nightly from ``evidence_anchor`` (charter 202's epoch, or a
    served version's own start), rebuilding each sampled row's envelope --
    since 2026-10-01 through ``serving_payload_cache``, which keeps a frozen
    day's payloads and records each complete sampler pass.  Per stage, the
    floor is the day after the last frozen day every lane's recent pass
    covered from no later than the anchor; a lane with no such pass keeps
    everything from the anchor (less the decision-to-event margin).  No
    anchor means an unbounded read, so nothing moves.
    """

    from bayes_basket_target import serving_payload_cache as payloads  # noqa: PLC0415
    from bayes_basket_target.center_width_store import (  # noqa: PLC0415
        CENTER_WIDTH_SERVED_STAGES,
        evidence_anchor,
    )
    from bayes_basket_target.off_centre_claim_store import SERVED_STAGES as OFF_CENTRE_STAGES  # noqa: PLC0415

    lanes = [("center_width.serving_cells_by_day", s) for s in CENTER_WIDTH_SERVED_STAGES] + [
        ("band_reliability.serving_lane_by_station_day", s) for s in OFF_CENTRE_STAGES
    ]
    days: list[str] = []
    for sampler, stage in lanes:
        try:
            anchor = evidence_anchor(conn, stage=stage)
        except sqlite3.Error as exc:
            raise OffloadRefused(f"evidence_anchor_unreadable:{stage}") from exc
        moment = _instant(anchor) if anchor else None
        if moment is None:
            raise OffloadRefused(f"evidence_anchor_missing:{stage}")
        covered = payloads.covered_through(sampler, stage=stage, anchor_utc=str(anchor))
        if covered:
            days.append((date.fromisoformat(covered) + timedelta(days=1)).isoformat())
        else:
            days.append((moment.date() - timedelta(days=EVIDENCE_EVENT_DATE_MARGIN_DAYS)).isoformat())
    return min(days) if days else None


def _cross_activation_generations(conn: sqlite3.Connection) -> dict[tuple, int]:
    """(artifact, station, season, lead, cycle) -> its highest activation
    generation: the ``a.generation`` the training-fact reader orders by."""

    return {
        (artifact, station, season, lead, cycle): int(generation)
        for artifact, station, season, lead, cycle, generation in conn.execute(
            "SELECT artifact_id, station_id, season_bin, lead_bucket, run_cycle, MAX(generation)"
            " FROM calibration_native_ensemble_activation WHERE artifact_kind='CROSS_NATIVE_ENSEMBLE'"
            " GROUP BY artifact_id, station_id, season_bin, lead_bucket, run_cycle"
        )
    }


POLICIES: tuple[TablePolicy, ...] = (
    TablePolicy(
        table="basket_p0_preselection_plan",
        time_column="created_at",
        hot_days=7,
        readers=(
            "production_worker._station_edge_evidence: MAX(rowid) per station; "
            "atomic_ledger (preliminary decision) and the p0_preselection writer: by "
            "the preselection_id being committed; calibration_refit's "
            "acknowledge_serving_arithmetic --audit: 12h either side of an "
            "unacknowledged fingerprint; dashboard: the plan each live decision names "
            "by production_plan_sha256.  The paper ledger reads the recorded plan from "
            "the training fact, not from here."
        ),
        keep_latest=(
            KeepLatest(
                key=("station_id",), order=("rowid",), count=1,
                why="_station_edge_evidence reads the newest plan of each station, whatever its age",
            ),
        ),
        # Not the live station-day whole: once every lane traded, nearly every
        # station-day carried a live intent, and on 2026-10-08 that kept
        # 73,815 of the 73,835 plans past the window -- the lane moved nothing,
        # the file reached the ceiling and every writer stopped.  The money's
        # audit trail is the plan each live decision acted on, kept by name.
        keep_decision_plans=KeepDecisionPlans(
            instant_index="basket_p0_preselection_created_idx",
            why="the dashboard re-reads the plan each live decision names; real money's plan stays",
        ),
        floors=(
            DynamicFloor(
                name="serving_arithmetic_audit",
                why="the fingerprint audit judges on plans around unacknowledged fingerprints",
                resolve=serving_arithmetic_floor,
            ),
        ),
        mirror_tables=("basket_p0_preselection_blob",),
    ),
    # 2026-10-19 (user 「按宪章 237 冷层机制，把 OOS 预测里被更新代次取代、且不是当前
    # 在役组合的行迁到外置盘冷层；迁完并验证后上限回 70 GiB」): 2.32M of the table's
    # 2.79M rows (~83%) predict a (key, event day) a newer activation generation
    # predicts again.  The training-fact reader takes, per key and day, only the
    # newest generation and skips the rest, so those rows are never read again;
    # the window covers the whole table, so only superseded rows leave.  The
    # artifact each activation scope serves now stays whole, whatever its rows.
    TablePolicy(
        table="calibration_cross_native_ensemble_oos_prediction",
        time_column="event_date",
        hot_days=36500,
        move_superseded=MoveSuperseded(
            key=("station_id", "season_bin", "lead_bucket", "run_cycle", "settlement_definition",
                 "settlement_timezone", "ensemble_system_ids_json"),
            order=("generation", "decision_at_utc", "prediction_id"),
            partition="station_id",
            day_sql=("event_date",),
            row_days=lambda day: {str(day)},
            computed={"generation": (
                ("cross_ensemble_artifact_id", "station_id", "season_bin", "lead_bucket", "run_cycle"),
                _cross_activation_generations,
            )},
            why=(
                "native_ensemble_training_facts reads, per key and event day, the newest "
                "activation generation's prediction and skips older generations; a row whose "
                "day a newer generation of the key gives is never read again"
            ),
        ),
        keep_named=(
            KeepNamed(
                name="serving_artifact",
                column="cross_ensemble_artifact_id",
                sql=(
                    "SELECT artifact_id FROM (SELECT artifact_id, generation, MAX(generation) OVER ("
                    "PARTITION BY station_id, ensemble_system_id_or_star, season_bin, lead_bucket, run_cycle,"
                    " available_system_set_hash_or_star) AS newest"
                    " FROM calibration_native_ensemble_activation"
                    " WHERE artifact_kind='CROSS_NATIVE_ENSEMBLE'"
                    " AND valid_from_utc<=strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                    " AND valid_until_utc>strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
                    " WHERE generation=newest"
                ),
                why=(
                    "P0's certified family map, its forward-prediction check and the "
                    "post-migration audit read by the artifact each scope serves now"
                ),
            ),
        ),
        readers=(
            "native_ensemble_training_facts: per key and event day the newest activation "
            "generation, older generations skipped; native_ensemble_p0 (certified family map, "
            "forward-prediction check) and run_bayes_basket_target_post_migration_no_live: by "
            "the serving artifact; calibration_dashboard scorecard: every row of the last 45 "
            "days (after this move it scores each day's surviving generations only); "
            "build_persisted_native_taf_exact_join (offline repair): by scope."
        ),
    ),
    TablePolicy(
        table="calibration_cross_native_ensemble_oos_evaluation",
        time_column="evaluated_at_utc",
        # The stable-pool gate reads [as_of - LOOKBACK, as_of]; two days of
        # margin keep the date-precision cutoff clear of its window.
        hot_days=STABLE_POOL_GATE_LOOKBACK_DAYS + 2,
        # 2026-10-13 (user 「桥接+精细保留」): the window held ~1M rows (~7 GiB)
        # while ~72% of them score only days a newer row of the same gate key
        # re-scored -- rows the gate never reads again.  They leave now; P0's
        # latest two per reader key and the 221 writer's latest one stay.
        move_superseded=MoveSuperseded(
            key=tuple(STABLE_POOL_GATE_HISTORY_KEY),
            order=("evaluated_at_utc", "evaluation_id"),
            partition="station_id",
            day_sql=(STABLE_POOL_GATE_SCORES_SQL, STABLE_POOL_GATE_MEMBERSHIP_SQL),
            row_days=stable_pool_gate_row_days,
            why=(
                "native_ensemble_oos._stable_pool_gate_history takes each day from the "
                "newest row of its key that gives it; a row whose days newer rows of the "
                "key all give is never read again"
            ),
        ),
        readers=(
            "native_ensemble_p0 (two places), republisher_fallback_activation, "
            "plan_native_ensemble_activation_repair and the charter 221 writer: the "
            "latest two rows of the reader key at or before their clock; "
            "native_ensemble_oos._stable_pool_gate_history: the key's rows in the last "
            "STABLE_POOL_GATE_LOOKBACK_DAYS; dashboard: the last 7 days and the latest "
            "per scope; health audit: MAX per scope."
        ),
        keep_latest=(
            KeepLatest(
                key=tuple(OOS_EVALUATION_READER_KEY),
                order=("evaluated_at_utc", "evaluation_id"),
                count=2,
                why=(
                    "P0 and the republisher fallback take the latest two per reader key "
                    "(and refuse when they share a clock); since charter 221 a stable "
                    "scope may not be rewritten for weeks"
                ),
            ),
        ),
    ),
    TablePolicy(
        table="basket_candidate_window_evidence",
        time_column="event_date",
        hot_days=3,
        candidate_where="sampled = 0",
        readers=(
            "every reader takes sampled=1 only: the scored view (beta/lambda/L4 "
            "fits, posterior_store, dashboard) filters e.sampled = 1, and the "
            "writer's first-decision check reads sampled=1.  The re-decisions "
            "(sampled=0, about fourteen in fifteen rows) are read by nobody."
        ),
    ),
    TablePolicy(
        table="basket_shadow_objective_score",
        time_column="decision_at_utc",
        hot_days=2,
        readers="write-only: shadow_objective writes one row per committed P0 plan and nothing reads it.",
    ),
    TablePolicy(
        table="basket_probability_update_proof",
        time_column="decision_at_utc",
        hot_days=3,
        readers=(
            "write-only for production: insert_probability_update_proof reads by the "
            "proof_id it is writing (idempotency).  Envelope lineage names proof ids "
            "but the validator recomputes the proof instead of reading it."
        ),
    ),
    TablePolicy(
        table="basket_portfolio_inventory_snapshot",
        time_column="as_of_utc",
        hot_days=2,
        readers="no reader anywhere: persist_portfolio_inventory writes it and nothing reads it back.",
    ),
    TablePolicy(
        table="basket_regime_artifact",
        # Keyed on the event date: forward days open about 72 hours before
        # their local start, so a row's creation time says little about when
        # it stops being read.
        time_column="event_date",
        hot_days=PAPER_LEDGER_EVENT_DAYS + 3,
        readers=(
            "regime_materialization.load_regime_artifact: by the current envelope "
            "(production P0/P0-M/P1 compiles, and the paper ledger's replays of event "
            "dates today-4 .. today-2); production_worker._materialize_regime_ssot and "
            "atomic_ledger: current scopes only."
        ),
    ),
    # Charter 238 (user ruling 2026-10-01 「扩」): the three per-cycle market
    # snapshots.  Measured that day, 237's seven tables covered 45% of what
    # the store gains a day; the WS book alone was 26% (about 410 MB/day).
    # All three are keyed on the event date for the same reason as the regime
    # artifact: the paper ledger replays settled days today-4 .. today-2 and
    # re-reads their books, and a forward day's rows are written ~72 hours
    # before it starts.  The WS book is also read, through the training facts
    # that name it, by the dashboard's charter-EV page over the valuation
    # population's WINDOW_DAYS (28) and by its posterior page over 14 days,
    # so its window was that population's plus two.  Offline readers of the
    # whole history read the cold rows through :func:`attach_cold_history`.
    #
    # 2026-10-06 (SSOT fail-closed at the ceiling): a 30-day window kept
    # every book of every cycle -- 134k rows / 1.3 GB a day by then, ~25 GB
    # live -- for two readers that name a few thousand of them.  The window
    # is now the paper ledger's, like the inventory and order-state books,
    # and those two readers' books stay hot by name for the 30 days they
    # read: the book each training fact priced against (charter-EV and
    # posterior pages), and the newest book at or before each committed P0
    # envelope (the board's centre/shape reading, board_readings).  Both
    # read exactly the rows they read before.
    TablePolicy(
        table="basket_official_ws_snapshot",
        time_column="event_date",
        hot_days=PAPER_LEDGER_EVENT_DAYS + 3,
        readers=(
            "market_ingress: MAX(sequence)+1 and by snapshot_id for the day being written; "
            "production_io / production_worker / atomic_ledger / tiebreak, p1_tail, "
            "resting_ladder, mis_bought_leg_exit, certain_win_recycle and proper lanes: "
            "the newest committed book of a current station-day, or the snapshot_id a "
            "current decision names; execution_transition_runtime: by content_sha256 of "
            "an intent's execution vector; paper_ledger_aligned: event dates today-4 .. "
            "today-2; dashboard: recent event dates, and through the training facts the "
            "charter-EV page (valuation_store.population, WINDOW_DAYS) and the posterior "
            "page (posterior_store.realisation, 14 days).  Whole-history readers "
            "(posterior_store/valuation_store refits, forward_scorecard, edge_attribution "
            "and the market-ladder research probes) read through attach_cold_history."
        ),
        keep_latest=(
            KeepLatest(
                key=("station_id", "event_date"), order=("sequence",), count=1,
                why="market_ingress numbers the next book MAX(sequence)+1 per station-day",
            ),
        ),
        keep_vector_refs=KeepVectorRefs(
            vector_column="official_ws_quote_sha256", instant_column="quote_observed_at",
            why="execution_transition_runtime re-reads the book an order executed against by its hash",
        ),
        keep_named=(
            KeepNamed(
                name="training_fact_book",
                column="snapshot_id",
                sql=(
                    "SELECT f.ws_snapshot_id FROM basket_policy_training_fact f"
                    " WHERE f.event_date >= :floor"
                ),
                why="valuation_store.population / posterior_store join each fact's book by snapshot_id",
                window_days=VALUATION_WINDOW_DAYS + 2,
            ),
            KeepNamed(
                name="p0_envelope_book",
                column="snapshot_id",
                sql=(
                    "SELECT (SELECT s.snapshot_id FROM basket_official_ws_snapshot s"
                    " INDEXED BY basket_official_ws_snapshot_asof_idx"
                    " WHERE s.station_id = e.station_id AND s.event_date = e.event_date"
                    " AND s.state = 'COMMITTED' AND s.observed_at <= e.decision_at_utc"
                    " ORDER BY s.observed_at DESC, s.sequence DESC LIMIT 1)"
                    " FROM basket_probability_envelope e"
                    " INDEXED BY basket_probability_envelope_publisher_scope_idx"
                    " WHERE e.state = 'COMMITTED'"
                    " AND e.event_date >= :floor AND e.stage = 'P0'"
                ),
                why="board_readings' centre/shape reading takes the book at or before each P0 envelope",
                window_days=VALUATION_WINDOW_DAYS + 2,
            ),
        ),
    ),
    TablePolicy(
        table="basket_confirmed_inventory_snapshot",
        time_column="event_date",
        hot_days=PAPER_LEDGER_EVENT_DAYS + 3,
        readers=(
            "certain_win_recycle: the newest complete row per station-day for event dates "
            "within MAX_EVENT_AGE_DAYS; production_io / production_worker / atomic_ledger "
            "/ p1_tail: the current station-day's newest row or the id/sha a current "
            "decision names; execution_transition_runtime: by content_sha256 of an "
            "intent's execution vector; paper ledger: event dates today-4 .. today-2."
        ),
        keep_latest=(
            KeepLatest(
                key=("station_id", "event_date"), order=("rowid",), count=1,
                why="the newest inventory of a station-day is its standing position, whatever its age",
            ),
        ),
        keep_vector_refs=KeepVectorRefs(
            vector_column="inventory_sha256",
            why="execution_transition_runtime re-reads the inventory an order executed against by its hash",
        ),
    ),
    TablePolicy(
        table="basket_order_state_snapshot",
        time_column="event_date",
        hot_days=PAPER_LEDGER_EVENT_DAYS + 3,
        readers=(
            "production_io / production_worker / atomic_ledger: the current station-day's "
            "newest row or the id/sha a current decision names; "
            "execution_transition_runtime: by content_sha256 of an intent's execution "
            "vector; paper ledger: event dates today-4 .. today-2."
        ),
        keep_latest=(
            KeepLatest(
                key=("station_id", "event_date"), order=("rowid",), count=1,
                why="the newest order state of a station-day is its standing state, whatever its age",
            ),
        ),
        keep_vector_refs=KeepVectorRefs(
            vector_column="order_state_sha256",
            why="execution_transition_runtime re-reads the order state an order executed against by its hash",
        ),
    ),
)

POLICIES += (
    # 2026-10-01 (user: 「查发布链」): the probability envelope.  Its readers
    # with a window: the served feature gate (60 event days of P0), P1's
    # likelihood refit (44), the dashboard (valuation 28, posterior 14), the
    # width/off-centre lanes (from their evidence anchor, a floor), the TAF
    # fit (from 2026-08-16, through its case cache: a floor until a day is
    # frozen).  The release chain that inventories every envelope
    # (canonical_typed_scope_builder / canonical_expected_scope_manifest) has
    # no caller, and no envelope since 09-28 carries the release_state the
    # production check reads.  Offline whole-history readers attach the cold
    # rows.
    # Charter 237 amendment (2026-10-09, second): the native ensemble member
    # history.  The weights/OOS fit (native_ensemble_oos) reads a station's
    # rows and keeps the ROLLING_TRAIN_WINDOW_DAYS before its holdout; every
    # other production reader reads the day being captured or decided.  The
    # 2025-12-29 onward history backfill beyond that window has no
    # production reader.
    TablePolicy(
        table="calibration_native_ensemble_historical_member_run",
        time_column="target_local_date",
        hot_days=MEMBER_RUN_HOT_DAYS,
        max_batches_per_run=MEMBER_RUN_MAX_BATCHES_PER_RUN,
        candidate_where=(
            "ensemble_system_id NOT IN ("
            + ",".join(f"'{system}'" for system in MEMBER_RUN_STAYING_SYSTEMS)
            + ")"
        ),
        readers=(
            "native_ensemble_oos (weights and OOS, nightly and in the resident): a "
            "station's rows, fitted on ROLLING_TRAIN_WINDOW_DAYS before the key's last "
            "DEFAULT_MIN_HOLDOUT_DAYS holdout days; the six archive writers, "
            "native_ensemble_training_facts, retained_native_ensemble_canonical_adapter and "
            "rolling_native_taf_oos: the station-day being captured or predicted; "
            "metar_physical_forward: its own system's rows from 2026-08-01 (kept); the "
            "dashboard: the last few target days.  run_native_history_backfill and "
            "backfill_aifs_ens_history_from_research refuse days older than this window."
        ),
        keep_latest=(
            KeepLatest(
                key=("ensemble_system_id", "station_id"), order=("target_local_date",), count=1,
                why="a reader of a system's newest member at a station finds it whatever its age",
            ),
        ),
    ),
    # Charter 237 amendment (2026-10-09): the decisions' training facts.
    # Production reads the current station-day, a fact by id / scope key /
    # cycle, or windows of 28 event days (valuation, realisation, posterior);
    # the label lane relabels a day whenever its truth is revised, which a
    # hand-run reconciliation may do 45 days back.  Rows the prepared-
    # supersession receipts name by foreign key, the facts of live decisions
    # (live_selection re-reads them by decision id) and settled d2facts the
    # label lane has not labelled stay whatever their age.
    TablePolicy(
        table="basket_policy_training_fact",
        time_column="event_date",
        hot_days=max(RECONCILIATION_MAX_DAYS, VALUATION_WINDOW_DAYS) + 2,
        readers=(
            "production (worker, ledger, production, lanes, mis_bought_leg_exit, "
            "delta_v2_training): the current station-day or a fact by id, scope key or "
            "cycle; valuation_store / realisation_store / posterior_store: 28 event days; "
            "tiebreak/tail-short/p1-consensus lanes: event days from a recent floor; "
            "settlement_truth / forward_observation_store: rowid cursors over new rows; "
            "delta_v2_settlement_label: every settled d2fact not labelled against its day's "
            "newest truth (a revision up to RECONCILIATION_MAX_DAYS back relabels); "
            "live_station_scope: the stations with any fact (each decides daily); "
            "live_selection (dashboard): the fact of every live decision; "
            "the paper ledger: its few-day window."
        ),
        keep_foreign_key_refs=True,
        keep_named=(
            KeepNamed(
                name="live_decision",
                column="decision_id",
                sql=(
                    "SELECT d.decision_id FROM basket_target_order_intents i"
                    " JOIN auto_trade_decisions d ON d.decision_id = i.decision_id"
                    " WHERE i.live_submit_authorized <> 0"
                ),
                why="live_selection re-reads the fact of every live decision; real money's facts stay",
            ),
            KeepNamed(
                name="unlabelled_d2fact",
                column="fact_id",
                sql=(
                    "SELECT f.fact_id FROM basket_policy_training_fact f"
                    " WHERE f.event_date < :floor AND f.fact_id LIKE 'd2fact_%'"
                    " AND NOT EXISTS (SELECT 1 FROM basket_policy_training_label l"
                    " WHERE l.fact_id = f.fact_id)"
                ),
                window_days=max(RECONCILIATION_MAX_DAYS, VALUATION_WINDOW_DAYS) + 2,
                why="the label lane still owes a settled d2fact its label",
            ),
        ),
    ),
    #
    # Two windows (2026-10-09): only P0 and P0-M rows are read for long -- the
    # feature gate re-scores P0 over 60 event days and P1's likelihood refit
    # reads its P0/P0-M roots over 44 -- so a P1 row keeps the table's window,
    # set by the readers of every stage: the board's seat windows (45 days,
    # three stages, a launchd job) and the 28-day valuation/realisation/
    # posterior reads.  The 08-16..09-08 P1 rows alone were 3.8 GiB.
    TablePolicy(
        table="basket_probability_envelope",
        time_column="event_date",
        hot_days=max(BOARD_SEAT_LOOKBACK_DAYS, VALUATION_WINDOW_DAYS) + 2,
        readers=(
            "production (worker, io, ledger, lanes, bridge, materialization): the current "
            "station-day, an envelope by id, or the newest per station; "
            "fit_deterministic_feature_gate: P0 of the last 60 event days; "
            "build_metar_phase_hazard_samples / fit_p1_observation_likelihood: P0/P0-M "
            "roots of 44 days; board_readings.seat_windows: three stages, 45 days; "
            "refit_center_width / "
            "refit_off_centre_claim and serving_arithmetic: from the evidence anchor; "
            "dashboard: valuation 28 days, posterior 14, live_book the envelope each live "
            "decision names, "
            "truth landing and H5 from their cache, recomputing through the cold tier; "
            "delta_v2_settlement_label: the envelope an unlabelled fact names; "
            "the METAR trajectory packet (production, one due station a cycle): every "
            "root of the station, through its root cache (a floor until frozen)."
        ),
        keep_latest=(
            KeepLatest(
                key=("station_id", "stage"), order=("decision_at_utc",), count=1,
                why="native_ensemble_oos and rolling_native_taf_oos read each station's newest P0",
            ),
        ),
        keep_recent=(
            KeepRecent(
                name="root_stages",
                column="stage",
                values=("P0", "P0-M"),
                days=max(FEATURE_GATE_WINDOW_DAYS, P1_LIKELIHOOD_WINDOW_DAYS, BOARD_SEAT_LOOKBACK_DAYS) + 2,
                why="the feature gate reads P0 for 60 event days, P1's likelihood refit P0/P0-M roots for 44",
            ),
        ),
        # Not the live station-day whole (the plan table's lesson of 10-08):
        # live_book re-reads the one envelope each live decision names.
        keep_decision_envelopes=KeepDecisionEnvelopes(
            scope_index="basket_probability_envelope_publisher_scope_idx",
            why="live_book re-reads the envelope each live decision names; real money's envelope stays",
        ),
        keep_named=(
            KeepNamed(
                name="unlabelled_fact",
                column="envelope_id",
                sql=(
                    "SELECT f.probability_envelope_id FROM basket_policy_training_fact f"
                    " INDEXED BY sqlite_autoindex_basket_policy_training_fact_1"
                    " WHERE NOT EXISTS (SELECT 1 FROM basket_policy_training_label l"
                    " WHERE l.fact_id = f.fact_id)"
                ),
                why="the settlement label lane re-reads the envelope of a fact it has not labelled",
            ),
        ),
        floors=(
            DynamicFloor(
                name="calibration_evidence_anchor",
                why="the width and off-centre lanes rebuild sampled envelopes from their anchor on",
                resolve=evidence_anchor_floor,
            ),
            DynamicFloor(
                name="metar_trajectory_training",
                why="P1's trajectory training reads a station-day off the SSOT until its root cache has frozen it",
                resolve=metar_trajectory_training_floor,
            ),
        ),
    ),
)

POLICY_BY_TABLE = {policy.table: policy for policy in POLICIES}


# --------------------------------------------------------------------------
# Small helpers.
# --------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _q(name: str) -> str:
    """A quoted identifier.  Registry names are constants; quoting is hygiene."""

    return '"' + str(name).replace('"', '""') + '"'


def _chunks(values: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def rows_digest(rows: Iterable[Sequence[Any]]) -> str:
    """sha256 over ``(rowid, *values)`` rows in the order given.

    Every value goes in with its SQLite storage class and, for text and
    blobs, its length: ``1``, ``1.0`` and ``'1'`` all compare equal somewhere,
    and a copy that changed a value's class, or moved a byte from one column
    into the next, must not hash the same.  Length-prefixed rather than JSON
    so a 100 KB plan is hashed, not re-escaped, three times a batch.
    """

    digest = hashlib.sha256()
    update = digest.update
    for row in rows:
        for value in row:
            if value is None:
                update(b"n")
            elif isinstance(value, bool):  # never produced by sqlite3
                update(b"i%d;" % int(value))
            elif isinstance(value, int):
                update(b"i%d;" % value)
            elif isinstance(value, float):
                update(b"f" + value.hex().encode("ascii") + b";")
            elif isinstance(value, bytes):
                update(b"b%d:" % len(value))
                update(value)
            else:
                data = str(value).encode("utf-8")
                update(b"t%d:" % len(data))
                update(data)
        update(b"\n")
    return digest.hexdigest()


def same_rows(left: Sequence[Sequence[Any]], right: Sequence[Sequence[Any]]) -> bool:
    """Row for row, value for value, and storage class for storage class.

    What ``rows_digest`` attests, compared directly: a verification does not
    need a hash, and this is the part of a batch that runs twice.
    """

    if len(left) != len(right):
        return False
    for a, b in zip(left, right):
        if len(a) != len(b):
            return False
        for x, y in zip(a, b):
            if type(x) is not type(y) or x != y:
                return False
    return True


def _row_bytes(row: Sequence[Any]) -> int:
    total = 0
    for value in row:
        if isinstance(value, bytes):
            total += len(value)
        elif isinstance(value, str):
            total += len(value.encode("utf-8"))
        else:
            total += 8
    return total


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({_q(table)})")]


def _normalized_sql(sql: str) -> str:
    return " ".join(str(sql).upper().split())


def _is_append_only_guard(sql: str) -> bool:
    """``BEFORE UPDATE|DELETE ON t BEGIN SELECT RAISE(ABORT, '...'); END`` and
    nothing else: a trigger that only ever refuses, unconditionally."""

    text = _normalized_sql(sql)
    if not text.startswith("CREATE TRIGGER"):
        return False
    parts = text.split(" BEGIN ", 1)
    if len(parts) != 2:
        return False
    head, body = parts
    if " BEFORE DELETE ON " not in head and " BEFORE UPDATE ON " not in head:
        return False
    if " WHEN " in head:
        return False
    body = body.strip()
    return (
        body.startswith("SELECT RAISE(ABORT,")
        and body.endswith("; END")
        and body.count(";") == 1
    )


def _triggers(conn: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    return [
        (str(name), str(sql)) for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name=? ORDER BY name",
            (table,),
        )
    ]


def _foreign_key_children(conn: sqlite3.Connection, table: str) -> list[str]:
    """Tables that declare a FOREIGN KEY to ``table``."""

    children = []
    for (child,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall():
        for row in conn.execute(f"PRAGMA foreign_key_list({_q(str(child))})"):
            if str(row[2]) == table:
                children.append(str(child))
                break
    return sorted(set(children))


def _foreign_key_refs(conn: sqlite3.Connection, table: str) -> list[tuple[str, str, str]]:
    """Each ``(child table, child column, referenced column)`` that names ``table``.

    Single-column keys only; a composite key is refused by the caller.
    """

    refs: list[tuple[str, str, str]] = []
    for child in _foreign_key_children(conn, table):
        keys: dict[int, list[tuple[str, str]]] = {}
        for row in conn.execute(f"PRAGMA foreign_key_list({_q(child)})"):
            if str(row[2]) == table:
                keys.setdefault(int(row[0]), []).append((str(row[3]), str(row[4] or "")))
        for columns in keys.values():
            if len(columns) != 1 or not columns[0][1]:
                raise OffloadRefused(f"foreign_key_not_single_named_column:{child}")
            refs.append((child, columns[0][0], columns[0][1]))
    return refs


def eligibility(conn: sqlite3.Connection, policy: TablePolicy) -> list[str]:
    """Why ``policy.table`` may not be offloaded here (empty when it may)."""

    table = policy.table
    if not _table_exists(conn, table):
        return ["table_missing"]
    reasons: list[str] = []
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()[0]
    if "WITHOUT ROWID" in _normalized_sql(sql):
        reasons.append("without_rowid_table")
    columns = set(_columns(conn, table))
    needed = {policy.time_column}
    for rule in policy.keep_latest:
        needed.update(c for c in (*rule.key, *rule.order) if c != "rowid")
    for rule in policy.keep_recent:
        needed.add(rule.column)
    for rule in policy.keep_named:
        needed.add(rule.column)
    if policy.keep_vector_refs is not None:
        needed.update({"station_id", "event_date", "content_sha256"})
        if policy.keep_vector_refs.instant_column:
            needed.add(policy.keep_vector_refs.instant_column)
    if policy.keep_decision_plans is not None:
        needed.update({"station_id", "event_date", "created_at", "plan_json"})
        indexed = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND tbl_name=? AND name=?",
            (table, policy.keep_decision_plans.instant_index),
        ).fetchone()
        if indexed is None:
            reasons.append("index_missing:" + policy.keep_decision_plans.instant_index)
    if policy.keep_decision_envelopes is not None:
        needed.update({"station_id", "event_date", "stage", "state", "decision_at_utc", "content_sha256"})
        indexed = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND tbl_name=? AND name=?",
            (table, policy.keep_decision_envelopes.scope_index),
        ).fetchone()
        if indexed is None:
            reasons.append("index_missing:" + policy.keep_decision_envelopes.scope_index)
    children = _foreign_key_children(conn, table)
    if children and not policy.keep_foreign_key_refs:
        reasons.append("foreign_key_children:" + ",".join(children))
    elif children:
        # Kept by name instead (each referenced value stays hot); the delete
        # connection enforces foreign keys besides, so a miss fails the batch.
        try:
            needed.update(parent for _child, _column, parent in _foreign_key_refs(conn, table))
        except OffloadRefused as exc:
            reasons.append(str(exc))
    missing = sorted(needed - columns)
    if missing:
        reasons.append("columns_missing:" + ",".join(missing))
    for name, trigger_sql in _triggers(conn, table):
        text = _normalized_sql(trigger_sql)
        if " DELETE ON " in text and not _is_append_only_guard(trigger_sql):
            reasons.append(f"delete_trigger_not_a_guard:{name}")
    return reasons


# --------------------------------------------------------------------------
# Holds (what --restore leaves behind so the next run does not undo it).
# --------------------------------------------------------------------------


def load_holds(path: Path = HOLDS) -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        # A hold that cannot be read could be protecting anything.
        raise OffloadRefused(f"holds_unreadable:{path}:{type(exc).__name__}") from exc
    if not isinstance(raw, dict) or not all(isinstance(v, dict) for v in raw.values()):
        raise OffloadRefused(f"holds_malformed:{path}")
    return {str(k): dict(v) for k, v in raw.items()}


def write_hold(table: str, since: str, *, reason: str, path: Path = HOLDS) -> dict[str, Any]:
    """Keep ``table``'s rows dated ``since`` or later hot until the hold is removed."""

    holds = load_holds(path)
    current = holds.get(table)
    if current is None or str(since) < str(current.get("since") or "9999"):
        holds[table] = {"since": str(since), "reason": reason, "written_at_utc": _iso(_utc_now())}
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(path.name + ".tmp")
    staging.write_text(json.dumps(holds, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(staging, path)
    return holds[table]


# --------------------------------------------------------------------------
# Planning: which rowids leave.
# --------------------------------------------------------------------------


@dataclass
class TablePlan:
    policy: TablePolicy
    cutoff: str
    state: str = "planned"
    refused: list[str] = field(default_factory=list)
    older_than_cutoff: int = 0
    kept: dict[str, int] = field(default_factory=dict)
    floors: dict[str, str | None] = field(default_factory=dict)
    candidates: Sequence[int] = field(default_factory=list)
    estimated_bytes: int | None = None
    oldest: str | None = None
    newest: str | None = None
    table_rows: int | None = None
    table_estimated_bytes: int | None = None
    #: Hashes an execution vector names that its station-day does not hold.
    absent_vector_refs: int = 0
    #: Rows inside the window that leave under ``move_superseded``.
    superseded_rows: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "table": self.policy.table,
            "state": self.state,
            "refused": list(self.refused),
            "time_column": self.policy.time_column,
            "hot_days": self.policy.hot_days,
            "candidate_where": self.policy.candidate_where,
            "cutoff_before": self.cutoff,
            "older_than_cutoff": self.older_than_cutoff,
            "floors": dict(self.floors),
            "kept": dict(self.kept),
            "candidate_rows": len(self.candidates),
            "estimated_bytes": self.estimated_bytes,
            "oldest_candidate": self.oldest,
            "newest_candidate": self.newest,
            "table_rows": self.table_rows,
            "table_estimated_bytes": self.table_estimated_bytes,
            "absent_vector_refs": self.absent_vector_refs,
            "superseded_rows": self.superseded_rows,
        }


VECTOR_TABLE = "basket_target_execution_version_vectors"
CYCLE_TABLE = "basket_target_execution_cycles"


def _instant(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _vector_ref_rowids(
    conn: sqlite3.Connection, table: str, rule: KeepVectorRefs,
    before_day: str | None = None,
) -> tuple[dict[int, str], int]:
    """The rowids of ``table`` an execution vector names, and how many named
    hashes their station-day does not hold (see :class:`KeepVectorRefs`)."""

    if not (_table_exists(conn, VECTOR_TABLE) and _table_exists(conn, CYCLE_TABLE)):
        return {}, 0
    wanted: dict[tuple[str, str], dict[str, set[Any]]] = {}
    for station, day, sha, instant in conn.execute(
        f"SELECT c.station_id, c.event_date, v.{_q(rule.vector_column)}, v.quote_observed_at"
        f" FROM {VECTOR_TABLE} v JOIN {CYCLE_TABLE} c ON c.cycle_id = v.cycle_id"
        f" WHERE v.{_q(rule.vector_column)} IS NOT NULL"
        # Only station-days with rows old enough to leave: a protection of a
        # row that is not leaving changes nothing, and reading the recent
        # days' books for their hashes was most of this pass (2026-10-15).
        + (" AND c.event_date < ?" if before_day else ""),
        (before_day,) if before_day else (),
    ):
        wanted.setdefault((str(station), str(day)), {}).setdefault(str(sha), set()).add(_instant(instant))
    kept: dict[int, str] = {}
    absent = 0
    for (station, day), shas in wanted.items():
        if rule.instant_column:
            instants = set().union(*shas.values())
            candidates = [
                int(rowid) for rowid, at in conn.execute(
                    f"SELECT rowid, {_q(rule.instant_column)} FROM {_q(table)}"
                    " WHERE station_id=? AND event_date=?", (station, day),
                ) if _instant(at) in instants
            ]
        else:
            candidates = [
                int(rowid) for (rowid,) in conn.execute(
                    f"SELECT rowid FROM {_q(table)} WHERE station_id=? AND event_date=?", (station, day),
                )
            ]
        found: set[str] = set()
        for chunk in _chunks(candidates, READ_CHUNK):
            marks = ",".join("?" * len(chunk))
            for rowid, sha in conn.execute(
                f"SELECT rowid, content_sha256 FROM {_q(table)} WHERE rowid IN ({marks})", tuple(chunk),
            ):
                if str(sha) in shas:
                    kept[int(rowid)] = "execution_vector"
                    found.add(str(sha))
        if set(shas) - found:
            # Not at its instant: look across the whole station-day.  What
            # the day does not hold under that hash, no reader can find there
            # either (the reader looks the hash up), so it keeps nothing: on
            # 2026-10-08 keeping such days whole held 173,448 books of 398
            # live station-days whose named books were never written here.
            for rowid, sha in conn.execute(
                f"SELECT rowid, content_sha256 FROM {_q(table)} WHERE station_id=? AND event_date=?",
                (station, day),
            ):
                if str(sha) in shas:
                    kept[int(rowid)] = "execution_vector"
                    found.add(str(sha))
            absent += len(set(shas) - found)
    return kept, absent


def _decision_plan_rowids(conn: sqlite3.Connection, table: str, rule: KeepDecisionPlans) -> dict[int, str]:
    """The rowids of ``table`` a live decision names (see :class:`KeepDecisionPlans`)."""

    if not _table_exists(conn, "auto_trade_decisions"):
        return {}
    named: set[tuple[str, str, str, str]] = set()
    for intents in ("basket_target_order_intents", "basket_target_order_intents_pre_go_live"):
        if not _table_exists(conn, intents):
            continue
        for station, day, at, sha in conn.execute(
            "SELECT DISTINCT d.station_id, d.event_date, d.created_at,"
            " json_extract(d.details_json, '$.production_plan_sha256')"
            f" FROM {_q(intents)} i JOIN auto_trade_decisions d ON d.decision_id=i.decision_id"
            " WHERE i.live_submit_authorized<>0"
        ):
            if sha:
                named.add((str(station), str(day), str(at), str(sha)))
    kept: dict[int, str] = {}
    for station, day, at, sha in sorted(named):
        found = [
            int(rowid) for rowid, plan_sha in conn.execute(
                "SELECT rowid, json_extract(plan_json, '$.compiled_plan.production_plan_sha256')"
                f" FROM {_q(table)} INDEXED BY {_q(rule.instant_index)}"
                " WHERE created_at=? AND station_id=? AND event_date=?",
                (at, station, day),
            ) if str(plan_sha) == sha
        ]
        for rowid in found:
            kept[rowid] = "live_decision_plan"
        if not found:
            # Fail closed: a named plan not found keeps its whole station-day.
            for (rowid,) in conn.execute(
                f"SELECT rowid FROM {_q(table)} WHERE station_id=? AND event_date=?", (station, day),
            ):
                kept.setdefault(int(rowid), "live_decision_plan_unresolved_station_day")
    return kept


def _decision_envelope_rowids(
    conn: sqlite3.Connection, table: str, rule: KeepDecisionEnvelopes,
) -> dict[int, str]:
    """The rowids of ``table`` a live decision names (see :class:`KeepDecisionEnvelopes`)."""

    if not _table_exists(conn, "auto_trade_decisions"):
        return {}
    binding = "$.forward_evidence_binding"
    named: set[tuple[str, str, str, str, str]] = set()
    for intents in ("basket_target_order_intents", "basket_target_order_intents_pre_go_live"):
        if not _table_exists(conn, intents):
            continue
        for station, day, at, sha, stage in conn.execute(
            "SELECT DISTINCT d.station_id, d.event_date,"
            f" json_extract(d.details_json, '{binding}.probability_decision_at_utc'),"
            f" json_extract(d.details_json, '{binding}.probability_artifact_sha256'),"
            f" coalesce(json_extract(d.details_json, '{binding}.probability_stage'),"
            "          json_extract(d.details_json, '$.decision_signature.stage'))"
            f" FROM {_q(intents)} i JOIN auto_trade_decisions d ON d.decision_id=i.decision_id"
            " WHERE i.live_submit_authorized<>0"
        ):
            if at and sha and stage and len(str(at)) >= 19:
                named.add((str(station), str(day), str(at)[:19], str(sha), str(stage)))
    kept: dict[int, str] = {}
    for station, day, second, sha, stage in sorted(named):
        found = [
            int(rowid) for rowid, content in conn.execute(
                f"SELECT rowid, content_sha256 FROM {_q(table)} INDEXED BY {_q(rule.scope_index)}"
                " WHERE state='COMMITTED' AND event_date=? AND station_id=?"
                " AND decision_at_utc>=? AND decision_at_utc<=? AND stage=?",
                (day, station, second, second + "z", stage),
            ) if str(content) == sha
        ]
        for rowid in found:
            kept[rowid] = "live_decision_envelope"
        if not found:
            # Fail closed: a named envelope not found keeps its whole station-day.
            for (rowid,) in conn.execute(
                f"SELECT rowid FROM {_q(table)} WHERE station_id=? AND event_date=?", (station, day),
            ):
                kept.setdefault(int(rowid), "live_decision_envelope_unresolved_station_day")
    return kept


def _sql_order_key(value: Any) -> tuple:
    """SQLite's ORDER BY across storage classes: NULL, numbers, text, blobs."""

    if value is None:
        return (0,)
    if isinstance(value, (int, float)):
        return (1, value)
    if isinstance(value, str):
        return (2, value)
    return (3, bytes(value))


def plan_table(
    conn: sqlite3.Connection,
    policy: TablePolicy,
    *,
    as_of: datetime,
    holds: Mapping[str, Mapping[str, Any]],
    estimate_sample: int = DEFAULT_ESTIMATE_SAMPLE,
    row_days_cache: Path | None = None,
) -> TablePlan:
    """Every rowid of ``policy.table`` that may leave, and what was kept and why.

    Read so that no row body is touched.  The old rows are enumerated as
    ``(rowid, time)`` off an index that covers the time column (every
    registered table has one -- the test pins the query plans), because the
    time column often sits behind a large JSON column and reading it through
    the table walks every overflow chain: the dashboard's full-table filter on
    ``evaluated_at_utc`` read 1.38 GB for 235k rows and took 1,017 s.  The
    columns the protections need beyond that (keys, station, event date) are
    all near the front of their rows and are fetched by rowid, a chunk at a
    time, while the newest rows per key are kept in small heaps -- so memory
    is the old rowids plus a few rows per key, never the rows themselves.
    """

    plan = TablePlan(policy=policy, cutoff=policy.cutoff_date(as_of))
    refused = eligibility(conn, policy)
    if refused:
        plan.state = "refused"
        plan.refused = refused
        return plan
    plan.table_rows, plan.table_estimated_bytes = _table_size(conn, policy.table, estimate_sample)
    floor_days: list[tuple[str, str]] = []
    for floor in policy.floors:
        try:
            day = floor.resolve(conn)
        except OffloadRefused as exc:
            plan.state = "refused"
            plan.refused = [f"floor_unresolved:{floor.name}:{exc}"]
            return plan
        plan.floors[floor.name] = day
        if day:
            floor_days.append((str(day), floor.name))
    hold = holds.get(policy.table) or {}
    if hold.get("since"):
        floor_days.append((str(hold["since"]), "hold"))
    table, time_column = policy.table, policy.time_column
    extra = f" AND ({policy.candidate_where})" if policy.candidate_where else ""
    # The old rows are counted off their own listing below (plus what a floor
    # holds), not by a COUNT of their own: on the 7.5M member rows that was a
    # second 21 s pass over the same range (2026-10-15).
    kept: dict[str, int] = {}
    held = 0
    if policy.move_superseded is None and conn.execute(
        f"SELECT 1 FROM {_q(table)} WHERE {_q(time_column)} < ?{extra} LIMIT 1", (plan.cutoff,)
    ).fetchone() is None:
        plan.state = "nothing_old"
        return plan
    # A floor or a hold keeps everything dated on or after it: below the
    # lowest of them and the cutoff is all that can leave.
    effective = plan.cutoff
    if floor_days:
        lowest, name = min(floor_days)
        if lowest < effective:
            held = int(conn.execute(
                f"SELECT COUNT(*) FROM {_q(table)} WHERE {_q(time_column)} >= ?"
                f" AND {_q(time_column)} < ?{extra}",
                (lowest, plan.cutoff),
            ).fetchone()[0])
            if held:
                kept[name] = held
            effective = lowest
    recent = [
        (rule, (as_of.astimezone(timezone.utc).date() - timedelta(days=int(rule.days))).isoformat())
        for rule in policy.keep_recent
    ]
    wanted: list[str] = []
    for rule in policy.keep_latest:
        wanted += [c for c in (*rule.key, *rule.order) if c not in {"rowid", time_column}]
    wanted += [rule.column for rule, _floor in recent if rule.column != time_column]
    named: list[tuple[KeepNamed, set[str]]] = [
        (rule, {
            str(r[0])
            for r in conn.execute(
                rule.sql,
                {} if rule.window_days is None else {
                    "floor": (as_of.astimezone(timezone.utc).date()
                              - timedelta(days=int(rule.window_days))).isoformat(),
                },
            )
            if r[0] is not None
        })
        for rule in policy.keep_named
    ]
    if policy.keep_foreign_key_refs:
        for child, column, parent in _foreign_key_refs(conn, table):
            named.append((
                KeepNamed(name=f"fk:{child}.{column}", column=parent, sql="",
                          why="a declared foreign key names the row"),
                {str(r[0]) for r in conn.execute(
                    f"SELECT DISTINCT {_q(column)} FROM {_q(child)} WHERE {_q(column)} IS NOT NULL"
                )},
            ))
    wanted += [rule.column for rule, _ids in named if rule.column != time_column]
    wanted = list(dict.fromkeys(wanted))
    at = {column: index for index, column in enumerate(wanted)}
    heaps: list[dict[tuple, list[tuple[tuple, int]]]] = [{} for _ in policy.keep_latest]
    protected: dict[int, str] = {}
    older = array("q")
    cursor = conn.execute(
        f"SELECT rowid,{_q(time_column)} FROM {_q(table)} WHERE {_q(time_column)} < ?{extra}",
        (effective,),
    )
    while True:
        chunk = cursor.fetchmany(READ_CHUNK)
        if not chunk:
            break
        stamps = {int(rowid): stamp for rowid, stamp in chunk}
        older.extend(stamps)
        if not wanted and not named:
            continue
        rows: Iterable[Sequence[Any]] = [(rowid,) for rowid in stamps]
        if wanted:
            listed = ",".join(_q(c) for c in wanted)
            marks = ",".join("?" * len(stamps))
            rows = conn.execute(
                f"SELECT rowid,{listed} FROM {_q(table)} WHERE rowid IN ({marks})", tuple(stamps)
            ).fetchall()
        for row in rows:
            rowid = int(row[0])

            def value(column: str, row: Sequence[Any] = row, rowid: int = rowid) -> Any:
                if column == "rowid":
                    return rowid
                if column == time_column:
                    return stamps[rowid]
                return row[1 + at[column]]

            for rule, heap in zip(policy.keep_latest, heaps):
                key = tuple(value(c) for c in rule.key)
                order = tuple(_sql_order_key(value(c)) for c in rule.order)
                entries = heap.setdefault(key, [])
                heapq.heappush(entries, (order, rowid))
                if len(entries) > rule.count:
                    heapq.heappop(entries)
            for rule, floor_day in recent:
                if str(value(rule.column)) in rule.values and str(value(time_column)) >= floor_day:
                    protected.setdefault(rowid, f"recent:{rule.name}")
            for rule, ids in named:
                if str(value(rule.column)) in ids:
                    protected.setdefault(rowid, f"named:{rule.name}")
    plan.older_than_cutoff = len(older) + held
    if not plan.older_than_cutoff and policy.move_superseded is None:
        plan.state = "nothing_old"
        return plan
    if policy.keep_vector_refs is not None and len(older):
        old_set = set(older)
        refs, plan.absent_vector_refs = _vector_ref_rowids(
            conn, table, policy.keep_vector_refs,
            before_day=effective if time_column == "event_date" else None,
        )
        for rowid, label in refs.items():
            if rowid in old_set:
                protected.setdefault(rowid, label)
        del old_set
    if policy.keep_decision_plans is not None and len(older):
        old_set = set(older)
        for rowid, label in _decision_plan_rowids(conn, table, policy.keep_decision_plans).items():
            if rowid in old_set:
                protected.setdefault(rowid, label)
        del old_set
    if policy.keep_decision_envelopes is not None and len(older):
        old_set = set(older)
        for rowid, label in _decision_envelope_rowids(conn, table, policy.keep_decision_envelopes).items():
            if rowid in old_set:
                protected.setdefault(rowid, label)
        del old_set
    for index, (rule, heap) in enumerate(zip(policy.keep_latest, heaps)):
        label = f"latest_{index}:{'+'.join(rule.key)}"
        for entries in heap.values():
            for _order, rowid in entries:
                protected.setdefault(rowid, label)
    # Every protection so far names a row of ``older``; the newest rowid may
    # or may not be one.
    for reason in protected.values():
        kept[reason] = kept.get(reason, 0) + 1
    newest = conn.execute(f"SELECT MAX(rowid) FROM {_q(table)}").fetchone()[0]
    if newest is not None and int(newest) not in protected:
        protected[int(newest)] = "newest_rowid"
        if int(newest) in older:
            kept["newest_rowid"] = 1
    plan.kept = dict(sorted(kept.items()))
    leaving = {rowid for rowid in older if rowid not in protected}
    del older
    if policy.move_superseded is not None:
        superseded = _superseded_rowids(
            conn, policy, since=effective, until=min(floor_days)[0] if floor_days else None,
            named=named, protected_latest=protected, row_days_cache=row_days_cache,
        )
        superseded.difference_update(leaving)
        plan.superseded_rows = len(superseded)
        leaving |= superseded
    plan.candidates = array("q", sorted(leaving))
    del leaving
    if plan.candidates:
        plan.oldest = _time_of(conn, table, time_column, plan.candidates[0])
        plan.newest = _time_of(conn, table, time_column, plan.candidates[-1])
        plan.estimated_bytes = _estimate_bytes(conn, table, plan.candidates, estimate_sample)
    plan.state = "planned" if plan.candidates else "nothing_to_move"
    return plan


def _superseded_rowids(
    conn: sqlite3.Connection,
    policy: TablePolicy,
    *,
    since: str,
    until: str | None,
    named: Sequence[tuple[KeepNamed, set[str]]],
    protected_latest: Mapping[int, str],
    row_days_cache: Path | None = None,
) -> set[int]:
    """The rows dated on or after ``since`` (and before ``until``) that
    ``policy.move_superseded`` lets leave.

    ``policy.keep_latest`` is applied here over the whole key, not only the
    rows below the cutoff, and a row a ``named`` rule names never leaves.
    ``protected_latest`` (the plan's protections below the cutoff) is only
    consulted so a row the plan already kept is not counted twice.
    """

    rule = policy.move_superseded
    assert rule is not None
    table, time_column = policy.table, policy.time_column
    latest_columns = [c for r in policy.keep_latest for c in (*r.key, *r.order)]
    named_columns = [r.column for r, _ids in named]
    columns = list(dict.fromkeys([*rule.key, *rule.order, *latest_columns, *named_columns]))
    sources = [c for name in rule.computed for c in rule.computed[name][0]]
    columns = [c for c in dict.fromkeys([*columns, *sources]) if c != "rowid" and c not in rule.computed]
    lookups = {name: (srcs, loader(conn)) for name, (srcs, loader) in rule.computed.items()}
    # The row's identity column rides along so a cached rowid is checked
    # against the row it was read from.
    identity = rule.order[-1]
    if identity not in columns:
        columns.append(identity)
    at = {column: 1 + index for index, column in enumerate(columns)}
    listed = ",".join(_q(c) for c in columns)
    reads = ",".join(rule.day_sql)
    cache = _RowDaysCache(row_days_cache, table) if row_days_cache is not None else None
    window = f"{_q(time_column)} >= ?" + (f" AND {_q(time_column)} < ?" if until else "")
    extra = f" AND ({policy.candidate_where})" if policy.candidate_where else ""
    parts = [
        r[0] for r in conn.execute(f"SELECT DISTINCT {_q(rule.partition)} FROM {_q(table)}")
    ]
    leaving: set[int] = set()
    for part in parts:
        args: tuple = (part, since) + ((until,) if until else ())
        if cache is None:
            rows = conn.execute(
                f"SELECT rowid,{listed},{reads} FROM {_q(table)}"
                f" WHERE {_q(rule.partition)}=? AND {window}{extra}",
                args,
            ).fetchall()
            days_of = {int(row[0]): set(rule.row_days(*row[1 + len(columns):])) for row in rows}
        else:
            rows = conn.execute(
                f"SELECT rowid,{listed} FROM {_q(table)}"
                f" WHERE {_q(rule.partition)}=? AND {window}{extra}",
                args,
            ).fetchall()
            days_of = cache.days(
                conn, {int(row[0]): str(row[at[identity]]) for row in rows}, reads, rule.row_days,
            )

        def value(row: Sequence[Any], column: str) -> Any:
            if column in lookups:
                srcs, mapping = lookups[column]
                return mapping.get(tuple(row[at[c]] for c in srcs))
            return int(row[0]) if column == "rowid" else row[at[column]]

        keep: set[int] = set()
        for latest in policy.keep_latest:
            groups: dict[tuple, list[tuple[tuple, int]]] = {}
            for row in rows:
                entries = groups.setdefault(tuple(value(row, c) for c in latest.key), [])
                heapq.heappush(entries, (tuple(_sql_order_key(value(row, c)) for c in latest.order), int(row[0])))
                if len(entries) > latest.count:
                    heapq.heappop(entries)
            keep.update(rowid for entries in groups.values() for _order, rowid in entries)
        for named_rule, ids in named:
            keep.update(int(row[0]) for row in rows if str(value(row, named_rule.column)) in ids)
        by_key: dict[tuple, list[Sequence[Any]]] = {}
        for row in rows:
            by_key.setdefault(tuple(value(row, c) for c in rule.key), []).append(row)
        for members in by_key.values():
            members.sort(
                key=lambda row: tuple(_sql_order_key(value(row, c)) for c in rule.order),
                reverse=True,
            )
            given: set[str] = set()
            for row in members:
                rowid = int(row[0])
                days = days_of[rowid]
                if not days <= given and days:
                    given |= days
                    continue
                if rowid not in keep and rowid not in protected_latest:
                    leaving.add(rowid)
        del rows
    if cache is not None:
        cache.close()
    return leaving


class _RowDaysCache:
    """``MoveSuperseded.row_days`` per rowid, persisted (see ROW_DAYS_CACHE).

    Keyed on (table, rowid) and checked against the row's identity value, so
    a rowid that ever named another row is read again.  Not the SSOT: a lost
    or stale file costs one full read, never a wrong plan.
    """

    def __init__(self, path: Path, table: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.table = table
        self.db = sqlite3.connect(str(path), timeout=30.0)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS row_days(tbl TEXT NOT NULL, row_id INTEGER NOT NULL,"
            " identity TEXT NOT NULL, days_json TEXT NOT NULL, PRIMARY KEY(tbl, row_id))"
        )

    def days(
        self, conn: sqlite3.Connection, identities: Mapping[int, str], reads: str,
        row_days: Callable[..., Any],
    ) -> dict[int, set[str]]:
        out: dict[int, set[str]] = {}
        missing: list[int] = []
        for chunk in _chunks(sorted(identities), READ_CHUNK):
            marks = ",".join("?" * len(chunk))
            for row_id, identity, days_json in self.db.execute(
                f"SELECT row_id, identity, days_json FROM row_days WHERE tbl=? AND row_id IN ({marks})",
                (self.table, *chunk),
            ):
                if identities.get(int(row_id)) == identity:
                    out[int(row_id)] = set(json.loads(days_json))
        missing = [rowid for rowid in identities if rowid not in out]
        fresh: list[tuple[str, int, str, str]] = []
        for chunk in _chunks(missing, READ_CHUNK):
            marks = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT rowid,{reads} FROM {_q(self.table)} WHERE rowid IN ({marks})", tuple(chunk),
            ):
                days = set(row_days(*row[1:]))
                out[int(row[0])] = days
                fresh.append((self.table, int(row[0]), identities[int(row[0])], json.dumps(sorted(days))))
        if fresh:
            self.db.executemany("INSERT OR REPLACE INTO row_days VALUES(?,?,?,?)", fresh)
            self.db.commit()
        return out

    def close(self) -> None:
        self.db.close()


def _time_of(conn: sqlite3.Connection, table: str, column: str, rowid: int) -> str | None:
    row = conn.execute(f"SELECT {_q(column)} FROM {_q(table)} WHERE rowid=?", (rowid,)).fetchone()
    return None if row is None or row[0] is None else str(row[0])


def _payload_bytes(conn: sqlite3.Connection, table: str, rowids: Sequence[int]) -> int:
    columns = _columns(conn, table)
    width = "+".join(f"COALESCE(length(CAST({_q(c)} AS BLOB)),0)" for c in columns) or "0"
    total = 0
    for chunk in _chunks(list(rowids), READ_CHUNK):
        marks = ",".join("?" * len(chunk))
        total += int(conn.execute(
            f"SELECT COALESCE(SUM({width}),0) FROM {_q(table)} WHERE rowid IN ({marks})", tuple(chunk)
        ).fetchone()[0])
    return total


def _estimate_bytes(conn: sqlite3.Connection, table: str, rowids: Sequence[int], sample: int) -> int:
    """Payload bytes of ``rowids``, from an even sample of them."""

    if not rowids:
        return 0
    step = max(1, len(rowids) // max(1, int(sample)))
    picked = list(rowids[::step])
    return int(_payload_bytes(conn, table, picked) * len(rowids) / len(picked))


def _table_size(conn: sqlite3.Connection, table: str, sample: int) -> tuple[int, int]:
    """The table's rows, and its payload bytes from rows spread over its rowids.

    One seek per sample point, the same measure the 2026-09-25 inventory
    used (``resource_bounds``: rowid-sampled table sizes, within 0.5% of the
    file) -- a dbstat pass over a 60 GiB store is the read this lane exists
    to avoid.  Payload only: indexes and page slack come on top.
    """

    rows = int(conn.execute(f"SELECT COUNT(*) FROM {_q(table)}").fetchone()[0])
    if not rows:
        return 0, 0
    # Two statements: SQLite answers MIN(rowid) or MAX(rowid) from one end
    # of the b-tree, but both in one SELECT walks the whole table (35 s on
    # the 7.5M member rows, 2026-10-15).
    low = conn.execute(f"SELECT MIN(rowid) FROM {_q(table)}").fetchone()[0]
    high = conn.execute(f"SELECT MAX(rowid) FROM {_q(table)}").fetchone()[0]
    points = max(1, min(int(sample), rows))
    step = max(1, (int(high) - int(low) + 1) // points)
    picked: list[int] = []
    for point in range(int(low), int(high) + 1, step):
        hit = conn.execute(
            f"SELECT rowid FROM {_q(table)} WHERE rowid>=? ORDER BY rowid LIMIT 1", (point,)
        ).fetchone()
        if hit is not None:
            picked.append(int(hit[0]))
    picked = sorted(set(picked))
    return rows, int(_payload_bytes(conn, table, picked) * rows / max(1, len(picked)))


# --------------------------------------------------------------------------
# The cold side.
# --------------------------------------------------------------------------


def cold_path(root: Path | str, table: str) -> Path:
    return Path(root) / COLD_SUBDIR / f"{table}.sqlite"


COLD_BATCH_DDL = f"""
CREATE TABLE IF NOT EXISTS {COLD_BATCH_TABLE} (
  batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  first_rowid INTEGER NOT NULL,
  last_rowid INTEGER NOT NULL,
  row_count INTEGER NOT NULL,
  rows_sha256 TEXT NOT NULL,
  payload_bytes INTEGER NOT NULL,
  source_path TEXT NOT NULL,
  cutoff_before TEXT NOT NULL,
  policy_json TEXT NOT NULL,
  copied_at_utc TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS {COLD_BATCH_TABLE}_no_update
BEFORE UPDATE ON {COLD_BATCH_TABLE} BEGIN SELECT RAISE(ABORT,'cold_append_only'); END;
CREATE TRIGGER IF NOT EXISTS {COLD_BATCH_TABLE}_no_delete
BEFORE DELETE ON {COLD_BATCH_TABLE} BEGIN SELECT RAISE(ABORT,'cold_append_only'); END;
CREATE TABLE IF NOT EXISTS {COLD_SCHEMA_TABLE} (
  table_name TEXT NOT NULL,
  create_sql TEXT NOT NULL,
  create_sql_sha256 TEXT NOT NULL,
  first_seen_utc TEXT NOT NULL,
  PRIMARY KEY(table_name, create_sql_sha256)
);
CREATE TRIGGER IF NOT EXISTS {COLD_SCHEMA_TABLE}_no_update
BEFORE UPDATE ON {COLD_SCHEMA_TABLE} BEGIN SELECT RAISE(ABORT,'cold_append_only'); END;
CREATE TRIGGER IF NOT EXISTS {COLD_SCHEMA_TABLE}_no_delete
BEFORE DELETE ON {COLD_SCHEMA_TABLE} BEGIN SELECT RAISE(ABORT,'cold_append_only'); END;
"""


def _cold_columns(conn: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    """Name and declared type of every column: what storage depends on."""

    return [(str(row[1]), str(row[2] or "")) for row in conn.execute(f"PRAGMA table_info({_q(table)})")]


def _cold_statements(table: str, columns: Sequence[tuple[str, str]], time_column: str | None) -> list[str]:
    """The archive table: the SSOT's columns and declared types, nothing else.

    No keys, UNIQUE constraints or CHECKs: the SSOT enforced them when each
    row was written, and a key the archive maintained would cost a random
    page per row on a spinning USB disk (~83 random I/Os a second) -- the
    OOS evaluation ids and content hashes are random strings, and in a year
    each of those indexes alone would be gigabytes.  What is kept is append
    order: the rowid is the SSOT's own, and the one index is on the time
    column, which grows at its right edge.  The same declared types give the
    same affinity, so every value lands in the storage class it had (the
    digest checks the class too).  The archive's own guards refuse any
    change.
    """

    body = ", ".join(f"{_q(name)} {kind}".rstrip() for name, kind in columns)
    statements = [f"CREATE TABLE IF NOT EXISTS {_q(table)} ({body})"]
    if time_column:
        statements.append(
            f"CREATE INDEX IF NOT EXISTS {_q(table + '__cold_time_idx')} ON {_q(table)}({_q(time_column)})"
        )
    statements += [
        f"CREATE TRIGGER IF NOT EXISTS {_q(table + '__cold_no_update')} BEFORE UPDATE ON {_q(table)}"
        " BEGIN SELECT RAISE(ABORT,'cold_append_only'); END",
        f"CREATE TRIGGER IF NOT EXISTS {_q(table + '__cold_no_delete')} BEFORE DELETE ON {_q(table)}"
        " BEGIN SELECT RAISE(ABORT,'cold_append_only'); END",
    ]
    return statements


def _open_cold(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=60.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    # Durable at every commit: the SSOT copy is deleted on the strength of it.
    conn.execute("PRAGMA synchronous=FULL")
    # Appends need little: the right edge of one table and one index.
    conn.execute("PRAGMA cache_size=-32768")
    return conn


def ensure_cold_table(
    ssot: sqlite3.Connection, cold: sqlite3.Connection, table: str, *, time_column: str | None = None,
) -> None:
    """The cold table exists with the SSOT's columns, and still matches them.

    The SSOT's own CREATE TABLE text is recorded in the cold file (one row
    per distinct text, first seen when), so the archive names the canonical
    schema its rows were written under.  A cold table whose columns differ
    from the SSOT's -- a column added since -- fails closed: this lane does
    not guess how to map one onto the other.
    """

    create = ssot.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if create is None:
        raise OffloadRefused(f"table_missing:{table}")
    columns = _cold_columns(ssot, table)
    create_sql = str(create[0])
    cold.executescript(COLD_BATCH_DDL)
    cold.execute("BEGIN IMMEDIATE")
    try:
        if _table_exists(cold, table):
            if _cold_columns(cold, table) != columns:
                cold.execute("ROLLBACK")
                raise OffloadRefused(f"cold_schema_mismatch:{table}")
        else:
            for statement in _cold_statements(table, columns, time_column):
                cold.execute(statement)
        cold.execute(
            f"INSERT OR IGNORE INTO {COLD_SCHEMA_TABLE} VALUES(?,?,?,?)",
            (table, create_sql, hashlib.sha256(create_sql.encode("utf-8")).hexdigest(), _iso(_utc_now())),
        )
        cold.execute("COMMIT")
    except BaseException:
        if cold.in_transaction:
            cold.execute("ROLLBACK")
        raise


def mirror_table(ssot: sqlite3.Connection, cold: sqlite3.Connection, table: str) -> int:
    """Copy (never move) a content-addressed side table into the cold file."""

    if not _table_exists(ssot, table):
        return 0
    ensure_cold_table(ssot, cold, table)
    columns = _columns(ssot, table)
    listed = ",".join(_q(c) for c in columns)
    marks = ",".join("?" * (len(columns) + 1))
    have = {int(r[0]) for r in cold.execute(f"SELECT rowid FROM {_q(table)}")}
    missing = [int(r[0]) for r in ssot.execute(f"SELECT rowid FROM {_q(table)}") if int(r[0]) not in have]
    copied = 0
    for chunk in _chunks(missing, READ_CHUNK):
        rows = _read_rows(ssot, table, columns, chunk)
        cold.execute("BEGIN IMMEDIATE")
        try:
            cold.executemany(f"INSERT OR IGNORE INTO {_q(table)}(rowid,{listed}) VALUES({marks})", rows)
            cold.execute("COMMIT")
        except BaseException:
            cold.execute("ROLLBACK")
            raise
        copied += len(rows)
    return copied


def open_cold_table(table: str, *, root: Path | str | None = None) -> sqlite3.Connection:
    """A read-only connection to ``table``'s cold file, for offline readers.

    Asserts the volume first (charters 167/196): an unplugged or TCC-denied
    volume raises :class:`OffloadedVolumeMissing` instead of reading as "no
    rows".  Production must never call this -- it reads the SSOT only.
    """

    base = Path(root) if root is not None else cold_tier_root()
    if base is None:
        raise OffloadedVolumeMissing("cold tier is switched off (PM_COLD_TIER_ROOT='')")
    assert_present(base, what="cold tier")
    if not base.is_dir():
        raise OffloadedVolumeMissing(f"cold tier {base} is not mounted")
    path = cold_path(base, table)
    if not path.is_file():
        raise FileNotFoundError(f"no cold rows for {table} (looked at {path})")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _moved_tables(manifest: Path = MANIFEST) -> set[str]:
    """Tables this lane has ever moved rows of, from the internal manifest."""

    moved: set[str] = set()
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return moved
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("table"):
            moved.add(str(row["table"]))
    return moved


def assert_within_hot_window(table: str, oldest_day: str, *, today: date | None = None) -> None:
    """Refuse a backfill of days this lane moves out of the SSOT.

    A row written older than its table's window leaves again on the next
    nightly run, and while the cold copy holds the first one an offline
    reader of the whole history sees the day twice.  So a historical writer
    stays inside the window; restore the day first if it must be rewritten.
    """

    policy = POLICY_BY_TABLE.get(table)
    if policy is None:
        return
    now = today or datetime.now(timezone.utc).date()
    cutoff = (now - timedelta(days=int(policy.hot_days))).isoformat()
    if str(oldest_day)[:10] < cutoff:
        raise OffloadRefused(
            f"backfill_older_than_hot_window:{table}:{str(oldest_day)[:10]}<{cutoff}"
        )


def attach_cold_history(
    conn: sqlite3.Connection,
    tables: Iterable[str],
    *,
    root: Path | str | None = None,
    manifest: Path = MANIFEST,
) -> dict[str, str]:
    """Make ``tables`` read as their whole history -- hot rows and cold -- on ``conn``.

    For offline readers of a table's whole history (charter 238): each table
    whose rows this lane has moved gets a TEMP VIEW of the same name, the SSOT
    rows UNION ALL the cold rows the SSOT no longer holds.  SQLite resolves an
    unqualified name in ``temp`` before ``main``, so the reader's own queries
    are unchanged.  Call it right after connecting and before ``PRAGMA
    query_only`` (a TEMP VIEW is a schema change).

    Fails closed: a table the manifest says was moved whose cold file cannot
    be opened raises (:class:`OffloadedVolumeMissing` when the volume is
    absent) instead of reading as a shorter history.  A table never moved is
    left alone.  Production must never call this.
    """

    moved = _moved_tables(manifest)
    attached: dict[str, str] = {}
    for table in dict.fromkeys(str(t) for t in tables):
        base = Path(root) if root is not None else cold_tier_root()
        if base is None:
            if table in moved:
                raise OffloadedVolumeMissing(
                    f"{table} has cold rows but the cold tier is switched off (PM_COLD_TIER_ROOT='')"
                )
            continue
        if table in moved:
            assert_present(base, what="cold tier")
        path = cold_path(base, table)
        # The manifest line is written after the SSOT delete commits, so a
        # cold file counts too: rows may have left with the line unwritten.
        if not path.is_file():
            if table in moved:
                raise FileNotFoundError(f"{table} has cold rows but no cold file at {path}")
            continue
        alias = f"cold_{len(attached)}"
        conn.execute(f"ATTACH DATABASE ? AS {alias}", (f"file:{path}?mode=ro",))
        hot = _cold_columns(conn, table)
        if [(n, k) for n, k in conn.execute(
            f"SELECT name, type FROM pragma_table_info(?, ?)", (table, alias)
        )] != hot:
            raise OffloadRefused(f"cold_schema_mismatch:{table}")
        listed = ",".join(_q(name) for name, _kind in hot)
        cold_listed = ",".join(f"c.{_q(name)}" for name, _kind in hot)
        # A crash between the copy and the delete leaves a row in both
        # places; the hot copy wins, so nothing is read twice.
        conn.execute(
            f"CREATE TEMP VIEW {_q(table)} AS"
            f" SELECT {listed} FROM main.{_q(table)}"
            f" UNION ALL SELECT {cold_listed} FROM {alias}.{_q(table)} c"
            f" WHERE NOT EXISTS (SELECT 1 FROM main.{_q(table)} h WHERE h.rowid = c.rowid)"
        )
        attached[table] = str(path)
    return attached


# --------------------------------------------------------------------------
# Moving.
# --------------------------------------------------------------------------


def _wal_bytes(db: Path) -> int:
    wal = db.with_name(db.name + "-wal")
    try:
        return wal.stat().st_size
    except FileNotFoundError:
        return 0


def _wait_for_wal(
    db: Path, conn: sqlite3.Connection, *, limit_sec: float = WAL_WAIT_LIMIT_SEC,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Hold off while the WAL is past the line, helping the checkpoint along."""

    waited = 0.0
    while _wal_bytes(db) > MAX_WAL_BYTES:
        if waited >= limit_sec:
            return False
        try:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.Error:
            pass
        sleep(10.0)
        waited += 10.0
    return True


def _read_rows(conn: sqlite3.Connection, table: str, columns: Sequence[str], rowids: Sequence[int]) -> list[tuple]:
    listed = ",".join(_q(c) for c in columns)
    rows: list[tuple] = []
    for chunk in _chunks(list(rowids), READ_CHUNK):
        marks = ",".join("?" * len(chunk))
        rows.extend(
            tuple(r) for r in conn.execute(
                f"SELECT rowid,{listed} FROM {_q(table)} WHERE rowid IN ({marks}) ORDER BY rowid",
                tuple(chunk),
            )
        )
    rows.sort(key=lambda r: r[0])
    return rows


def _begin_immediate(
    conn: sqlite3.Connection, *, sleep: Callable[[float], None] = time.sleep,
    attempts: int = 6, wait_sec: float = 30.0,
) -> None:
    """Take the SSOT write lock, outwaiting a resident that holds it long.

    Each attempt already waits ``busy_timeout`` (120 s); a resident restart
    has held the lock past that (2026-09-19 16:57Z, the retirement's
    restore).  Nothing is written before the lock is ours, so trying again
    is the same batch.
    """

    for attempt in range(attempts):
        try:
            conn.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            text = str(exc).lower()
            if ("locked" not in text and "busy" not in text) or attempt == attempts - 1:
                raise
            sleep(wait_sec)


def _delete_connection(db: Path) -> sqlite3.Connection:
    """The one connection that writes the SSOT here, with its triggers off.

    Only this connection: the schema is never changed, so the append-only
    guards stay in force for every other connection throughout, and a crash
    can never leave a table unguarded.  Foreign keys are enforced on it, so a
    row some other table still names by a declared key cannot be deleted.
    """

    if not hasattr(sqlite3, "SQLITE_DBCONFIG_ENABLE_TRIGGER"):
        raise OffloadRefused("python_3_12_required_for_connection_trigger_switch")
    conn = sqlite3.connect(str(db), timeout=120.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=120000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_TRIGGER, False)
    if conn.getconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_TRIGGER):
        raise OffloadRefused("connection_trigger_switch_ineffective")
    return conn


def _append_manifest(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


#: Longest a run waits for the resident to truncate the WAL before a read.
WAL_SETTLE_MAX_SECONDS = 180.0


#: The share of an applied run's budget its planning may take before the
#: moves start on what is planned.
PLAN_BUDGET_SHARE = 0.5


@dataclass
class MoveBudget:
    deadline: float | None
    pause_factor: float = DEFAULT_PAUSE_FACTOR
    batch_rows: int = BATCH_ROWS
    batch_bytes: int = BATCH_BYTES

    def expired(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline

    def seconds_left(self, cap: float) -> float:
        if self.deadline is None:
            return cap
        return max(0.0, min(cap, self.deadline - time.monotonic()))


def move_table(
    *,
    ssot_path: Path,
    read: sqlite3.Connection,
    delete: sqlite3.Connection,
    plan: TablePlan,
    cold_root: Path,
    budget: MoveBudget,
    manifest: Path,
    cold_ok: Callable[[], dict[str, Any]],
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Move ``plan.candidates`` batch by batch; stop cleanly on any limit."""

    policy = plan.policy
    table = policy.table
    result: dict[str, Any] = {
        "table": table, "state": "started", "moved_rows": 0, "moved_bytes": 0,
        "batches": 0, "max_lock_sec": 0.0, "max_lock_wait_sec": 0.0, "mirrored_rows": 0,
        "cold_path": str(cold_path(cold_root, table)),
    }
    columns = _columns(read, table)
    cold_file = cold_path(cold_root, table)
    cold = _open_cold(cold_file)
    verify: sqlite3.Connection | None = None
    listed = ",".join(_q(c) for c in columns)
    marks = ",".join("?" * (len(columns) + 1))
    policy_json = json.dumps(policy.describe(), ensure_ascii=False, sort_keys=True)
    since_check = 0

    limit = {"rows": int(budget.batch_rows), "bytes": int(budget.batch_bytes)}

    def move_batch(rows: list[tuple], size: int) -> str | None:
        """One batch across; None when it moved, else why the table stops."""

        nonlocal since_check
        ids = [int(r[0]) for r in rows]
        digest = rows_digest(rows)
        # 2. Onto the volume, with the batch's own ledger row.
        cold.execute("BEGIN IMMEDIATE")
        try:
            cold.executemany(f"INSERT OR IGNORE INTO {_q(table)}(rowid,{listed}) VALUES({marks})", rows)
            cold.execute(
                f"INSERT INTO {COLD_BATCH_TABLE}(table_name,first_rowid,last_rowid,row_count,"
                "rows_sha256,payload_bytes,source_path,cutoff_before,policy_json,copied_at_utc)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (table, ids[0], ids[-1], len(ids), digest, size, str(ssot_path),
                 plan.cutoff, policy_json, _iso(_utc_now())),
            )
            cold.execute("COMMIT")
        except BaseException:
            if cold.in_transaction:
                cold.execute("ROLLBACK")
            raise
        # 3. Read back through another connection, value and type.
        landed = _read_rows(verify, table, columns, ids)
        if not same_rows(landed, rows):
            return "cold_verify_mismatch"
        # 4. Delete exactly what was verified.
        if not _wait_for_wal(ssot_path, delete, sleep=sleep):
            return "wal_not_draining"
        if shutil.disk_usage(ssot_path.parent).free < MIN_INTERNAL_FREE_BYTES:
            return "internal_disk_low"
        queued = time.monotonic()
        _begin_immediate(delete, sleep=sleep)
        started = time.monotonic()
        try:
            again = _read_rows(delete, table, columns, ids)
            if not same_rows(again, rows):
                delete.execute("ROLLBACK")
                return "ssot_rows_changed_since_copy"
            removed = 0
            for chunk in _chunks(ids, READ_CHUNK):
                removed += delete.execute(
                    f"DELETE FROM {_q(table)} WHERE rowid IN ({','.join('?' * len(chunk))})",
                    tuple(chunk),
                ).rowcount
            if removed != len(ids):
                delete.execute("ROLLBACK")
                return "ssot_delete_count_mismatch"
            delete.execute("COMMIT")
        except BaseException:
            if delete.in_transaction:
                delete.execute("ROLLBACK")
            raise
        held = time.monotonic() - started
        result["max_lock_sec"] = round(max(result["max_lock_sec"], held), 3)
        if held > LOCK_TARGET_SEC:
            limit["rows"] = max(MIN_BATCH_ROWS, limit["rows"] // 2)
            limit["bytes"] = max(1, limit["bytes"] // 2)
        elif held < LOCK_TARGET_SEC / 4:
            limit["rows"] = min(budget.batch_rows, limit["rows"] * 2)
            limit["bytes"] = min(budget.batch_bytes, limit["bytes"] * 2)
        result["min_batch_rows"] = min(
            int(result.get("min_batch_rows") or limit["rows"]), limit["rows"]
        )
        result["max_lock_wait_sec"] = round(max(result["max_lock_wait_sec"], started - queued), 3)
        # 5. The internal record, once the delete is durable.
        _append_manifest(manifest, {
            "schema": BATCH_SCHEMA, "table": table, "first_rowid": ids[0], "last_rowid": ids[-1],
            "rows": len(ids), "payload_bytes": size, "rows_sha256": digest,
            "cold_path": str(cold_file), "cutoff_before": plan.cutoff,
            "lock_sec": round(held, 3), "committed_at_utc": _iso(_utc_now()),
            "production_dependency": False,
        })
        result["moved_rows"] += len(ids)
        result["moved_bytes"] += size
        result["batches"] += 1
        since_check += size
        cap = policy.max_batches_per_run
        if cap is not None and result["batches"] >= int(cap):
            return "batch_cap"
        if since_check >= COLD_RECHECK_BYTES:
            since_check = 0
            status = cold_ok()
            if status.get("state") != "available":
                return f"cold_tier_{status.get('state')}"
        sleep(max(PAUSE_SEC, held * budget.pause_factor))
        return None

    try:
        ensure_cold_table(read, cold, table, time_column=policy.time_column)
        for side in policy.mirror_tables:
            result["mirrored_rows"] += mirror_table(read, cold, side)
        verify = sqlite3.connect(f"file:{cold_file}?mode=ro", uri=True, timeout=60.0)
        pending: list[tuple] = []
        pending_bytes = 0
        stopped: str | None = None
        for chunk in _chunks(plan.candidates, READ_CHUNK):
            if budget.expired():
                stopped = "budget"
                break
            for row in _read_rows(read, table, columns, chunk):
                pending.append(row)
                pending_bytes += _row_bytes(row)
                if len(pending) >= limit["rows"] or pending_bytes >= limit["bytes"]:
                    stopped = move_batch(pending, pending_bytes)
                    pending, pending_bytes = [], 0
                    if stopped:
                        break
            if stopped:
                break
        if pending and (stopped is None or stopped == "budget"):
            reason = move_batch(pending, pending_bytes)
            stopped = reason or stopped
        if stopped == "budget":
            result["state"] = "partial_budget"
        elif stopped == "batch_cap":
            result["state"] = "partial_batch_cap"
        elif stopped:
            result["state"] = "stopped"
            result["reason"] = stopped
        else:
            result["state"] = "moved" if result["moved_rows"] else "nothing_moved"
        return result
    finally:
        if verify is not None:
            verify.close()
        try:
            cold.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        cold.close()


# --------------------------------------------------------------------------
# Restoring.
# --------------------------------------------------------------------------


def restore_table(
    *,
    ssot_path: Path,
    cold_root: Path,
    table: str,
    since: str,
    until: str | None = None,
    holds_path: Path = HOLDS,
    reason: str = "restore",
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Put cold rows with ``time >= since`` (and ``< until``) back, verbatim.

    The hold is written first: from then on no run moves those rows again,
    whatever the policy says, until somebody removes it.  Rows go back with
    their own rowids (``INSERT OR IGNORE``, so a row still present is left
    alone) on the trigger-less connection, so an insert guard, if a table
    ever grows one, does not refuse history it once accepted.
    """

    policy = POLICY_BY_TABLE.get(table)
    if policy is None:
        raise OffloadRefused(f"table_not_registered:{table}")
    hold = write_hold(table, since, reason=reason, path=holds_path)
    source = open_cold_table(table, root=cold_root)
    target = _delete_connection(ssot_path)
    restored = 0
    rowids: list[int] = []
    try:
        if _cold_columns(source, table) != _cold_columns(target, table):
            raise OffloadRefused(f"cold_schema_mismatch:{table}")
        columns = _columns(source, table)
        listed = ",".join(_q(c) for c in columns)
        marks = ",".join("?" * (len(columns) + 1))
        where = f"{_q(policy.time_column)} >= ?"
        args: tuple = (since,)
        if until:
            where += f" AND {_q(policy.time_column)} < ?"
            args += (until,)
        rowids = [int(r[0]) for r in source.execute(
            f"SELECT rowid FROM {_q(table)} WHERE {where} ORDER BY rowid", args
        )]
        for chunk in _chunks(rowids, READ_CHUNK):
            rows = _read_rows(source, table, columns, chunk)
            _begin_immediate(target, sleep=sleep)
            try:
                before = target.total_changes
                target.executemany(
                    f"INSERT OR IGNORE INTO {_q(table)}(rowid,{listed}) VALUES({marks})", rows
                )
                restored += target.total_changes - before
                target.execute("COMMIT")
            except BaseException:
                if target.in_transaction:
                    target.execute("ROLLBACK")
                raise
            sleep(PAUSE_SEC)
    finally:
        source.close()
        target.close()
    return {
        "schema": SCHEMA, "table": table, "since": since, "until": until,
        "cold_rows_selected": len(rowids), "restored_rows": restored,
        "hold": hold, "holds_path": str(holds_path),
    }


# --------------------------------------------------------------------------
# The run.
# --------------------------------------------------------------------------


def _store_pages(conn: sqlite3.Connection) -> dict[str, int]:
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    return {
        "page_size": page_size, "page_count": page_count, "freelist_count": freelist,
        "file_bytes_by_pages": page_size * page_count, "free_page_bytes": page_size * freelist,
        "live_bytes": page_size * max(0, page_count - freelist),
    }


def _ceiling(ssot_path: Path, pages: Mapping[str, int]) -> dict[str, Any]:
    """What the writers' capacity guard makes of the store right now.

    ``resource_bounds.assert_target_db_capacity`` compares the store's *live*
    pages (file less freelist) with ``MAX_TARGET_DB_BYTES`` since 2026-10-06,
    so the pages this lane frees reopen a store that sits on the ceiling.
    Writers reuse the free pages first and then grow the file up to the
    ceiling (``max_page_count``), so the two together are what they can
    still write.
    """

    file_bytes = int(ssot_path.stat().st_size)
    free_page_bytes = int(pages["free_page_bytes"])
    live_bytes = max(0, file_bytes - free_page_bytes)
    writers_open = live_bytes <= MAX_TARGET_DB_BYTES
    growth = max(0, MAX_TARGET_DB_BYTES - int(pages["file_bytes_by_pages"]))
    return {
        "max_database_bytes": MAX_TARGET_DB_BYTES,
        "file_bytes": file_bytes,
        "live_bytes": live_bytes,
        "writers_open": writers_open,
        "writable_bytes": growth + free_page_bytes if writers_open else 0,
    }


def run(
    *,
    ssot_path: Path,
    cold_root: Path | None,
    tables: Sequence[str] | None = None,
    apply: bool = False,
    as_of: datetime | None = None,
    budget_seconds: float = 0.0,
    pause_factor: float = DEFAULT_PAUSE_FACTOR,
    batch_rows: int = BATCH_ROWS,
    batch_bytes: int = BATCH_BYTES,
    min_cold_free_bytes: int = DEFAULT_COLD_TIER_MIN_FREE_BYTES,
    local_reference: Path | None = ROOT,
    manifest: Path = MANIFEST,
    holds_path: Path = HOLDS,
    estimate_sample: int = DEFAULT_ESTIMATE_SAMPLE,
    policies: Sequence[TablePolicy] = POLICIES,
    sleep: Callable[[float], None] = time.sleep,
    row_days_cache: Path | None = None,
    plan_first: Sequence[str] = (),
) -> dict[str, Any]:
    """Plan every selected table; with ``apply``, move what the plan names.

    ``plan_first``: tables the previous run left unplanned when its planning
    share ran out, planned ahead of the registry order this time.
    """

    started = time.monotonic()
    moment = as_of or _utc_now()
    status = (
        cold_tier_status(cold_root, local_reference=local_reference, min_free_bytes=min_cold_free_bytes)
        if cold_root is not None else {"state": "disabled", "root": "", "free_bytes": None}
    )
    receipt: dict[str, Any] = {
        "schema": SCHEMA, "generated_at_utc": _iso(_utc_now()), "as_of_utc": _iso(moment),
        "mode": "apply" if apply else "dry_run", "ssot": str(ssot_path),
        "cold_tier": status, "tables": [],
        "calls_order_gateway": False, "orders_submitted": 0, "production_dependency": False,
    }
    by_table = {policy.table: policy for policy in policies}
    selected = [by_table[name] for name in tables] if tables else list(policies)
    first = set(plan_first)
    selected = [p for p in selected if p.table in first] + [p for p in selected if p.table not in first]
    if apply and status.get("state") != "available":
        # These rows are not re-obtainable: without the tier they stay hot,
        # and there is no reason to spend the store's reads planning a move
        # that cannot happen (the daily job under launchd without TCC access).
        receipt["state"] = f"skipped_cold_tier_{status.get('state')}"
        receipt["run_seconds"] = round(time.monotonic() - started, 1)
        return receipt
    read = sqlite3.connect(f"file:{ssot_path}?mode=ro", uri=True, timeout=60.0)
    read.execute("PRAGMA busy_timeout=60000")
    # Planning reads every registered table and a move re-reads its rows: a
    # long reader beside production, so it lets go past the reader WAL line
    # like the board's (2026-10-16).  An interrupted plan is refused, an
    # interrupted move stops; the next run carries on.
    receipt["wal_yield"] = install_wal_yield(read, ssot_path)
    delete: sqlite3.Connection | None = None
    try:
        receipt["store_before"] = _store_pages(read)
        receipt["ceiling_before"] = _ceiling(ssot_path, receipt["store_before"])
        holds = load_holds(holds_path)
        receipt["holds"] = holds
        budget = MoveBudget(
            deadline=(started + float(budget_seconds)) if budget_seconds and budget_seconds > 0 else None,
            pause_factor=float(pause_factor), batch_rows=int(batch_rows), batch_bytes=int(batch_bytes),
        )
        root = Path(str(status["root"])) if status.get("root") else None

        def cold_ok() -> dict[str, Any]:
            return cold_tier_status(root, local_reference=local_reference, min_free_bytes=min_cold_free_bytes)

        if apply:
            delete = _delete_connection(ssot_path)
        moves: list[dict[str, Any]] = []
        stopped = False
        # Every table is planned first (the receipt lists them in registry
        # order), then the moves go biggest estimated payload first.  In
        # registry order a quiet morning spent its whole hour on a table of
        # 600-byte rows while the tables that free gigabytes, and the two at
        # the end of the registry, were "not started" run after run
        # (2026-10-12).  A plan only names rows older than the cutoff, and a
        # batch re-reads and re-verifies its rows before deleting, so a plan
        # made an hour before its move is as safe as one made just before.
        plans: list[TablePlan] = []

        def wal_settled() -> None:
            # A read started into a WAL still past the line is interrupted at
            # once: 10-25/26 one yield made every later table of the run
            # "plan_error:interrupted" within the same second, and the SSOT
            # went into its last GiB with nothing planned.
            wait_for_wal_under(ssot_path, READER_YIELD_WAL_BYTES, budget.seconds_left(WAL_SETTLE_MAX_SECONDS))

        # Planning may take half the budget; the rest is the moves'
        # (2026-10-27, user 「继续」 on the full store): on a swapping host the
        # plans alone took the whole 2,400 s at 03:42Z -- 1.39 GiB planned,
        # 0.01 GiB moved -- and the store reached its ceiling with every
        # writer stopped for three hours.  Tables not planned in time are
        # planned first next run (``plan_first``, from this run's receipt).
        plan_deadline = (
            started + float(budget_seconds) * PLAN_BUDGET_SHARE
            if apply and budget_seconds and budget_seconds > 0 else None
        )
        for policy in selected:
            if plan_deadline is not None and plans and time.monotonic() >= plan_deadline:
                receipt["tables"].append(TablePlan(
                    policy=policy, cutoff=policy.cutoff_date(moment), state="not_planned_budget",
                ).summary())
                continue
            plan = None
            for attempt in range(2):
                wal_settled()
                try:
                    plan = plan_table(read, policy, as_of=moment, holds=holds,
                                      estimate_sample=estimate_sample,
                                      row_days_cache=row_days_cache)
                    break
                except sqlite3.OperationalError as exc:
                    if attempt == 0 and "interrupted" in str(exc):
                        continue
                    plan = TablePlan(policy=policy, cutoff=policy.cutoff_date(moment), state="refused",
                                     refused=[f"plan_error:{type(exc).__name__}:{str(exc)[:200]}"])
                except sqlite3.Error as exc:
                    # A table that cannot be planned is left alone; the others go on.
                    plan = TablePlan(policy=policy, cutoff=policy.cutoff_date(moment), state="refused",
                                     refused=[f"plan_error:{type(exc).__name__}:{str(exc)[:200]}"])
                    break
            receipt["tables"].append(plan.summary())
            if apply and plan.state == "planned":
                plans.append(plan)
        order = {policy.table: index for index, policy in enumerate(selected)}
        plans.sort(key=lambda p: (-int(p.estimated_bytes or 0), order[p.policy.table]))
        receipt["move_order"] = [p.policy.table for p in plans]
        for plan in plans:
            policy = plan.policy
            if stopped or budget.expired():
                moves.append({"table": policy.table, "state": "not_started_budget" if not stopped
                              else "not_started_after_stop"})
                continue
            wal_settled()
            try:
                outcome = move_table(
                    ssot_path=ssot_path, read=read, delete=delete, plan=plan, cold_root=root,
                    budget=budget, manifest=manifest, cold_ok=cold_ok, sleep=sleep,
                )
            except (sqlite3.Error, OSError, OffloadRefused) as exc:
                # Whatever broke, the batch in flight either committed both
                # halves or rolled back its delete: the SSOT never loses a row
                # the volume does not hold.  The receipt says what broke.
                outcome = {
                    "table": policy.table, "state": "stopped",
                    "reason": f"error:{type(exc).__name__}:{str(exc)[:200]}",
                }
            moves.append(outcome)
            stopped = stopped or outcome.get("state") == "stopped"
        receipt["candidate_rows"] = sum(int(t["candidate_rows"]) for t in receipt["tables"])
        receipt["estimated_bytes"] = sum(int(t["estimated_bytes"] or 0) for t in receipt["tables"])
        receipt["table_estimated_bytes"] = sum(int(t.get("table_estimated_bytes") or 0) for t in receipt["tables"])
        if not apply:
            receipt["state"] = "dry_run"
            return receipt
        receipt["moves"] = moves
        receipt["moved_rows"] = sum(int(m.get("moved_rows", 0)) for m in moves)
        receipt["moved_bytes"] = sum(int(m.get("moved_bytes", 0)) for m in moves)
        receipt["store_after"] = _store_pages(read)
        receipt["ceiling_after"] = _ceiling(ssot_path, receipt["store_after"])
        if any(m.get("state") == "stopped" for m in moves):
            receipt["state"] = "stopped"
        elif any(str(m.get("state", "")).startswith(("partial", "not_started")) for m in moves):
            receipt["state"] = "partial"
        else:
            receipt["state"] = "applied"
        return receipt
    finally:
        receipt["run_seconds"] = round(time.monotonic() - started, 1)
        if delete is not None:
            delete.close()
        read.close()


def _single_instance(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


_STATE_WORDS = {
    "planned": "可搬", "nothing_to_move": "旧行全受保护", "nothing_old": "没有旧行",
    "refused": "拒绝", "not_planned_budget": "计划时间到、下次再排", "moved": "已搬", "nothing_moved": "未搬", "partial_budget": "预算到点、下次续搬",
    "partial_batch_cap": "本轮封顶、下次续搬",
    "stopped": "中途停下", "not_started_budget": "预算用完未开始", "not_started_after_stop": "前表停下未开始",
}
_KEPT_WORDS = {"newest_rowid": "最大 rowid", "hold": "hold",
               "recent:root_stages": "P0/P0-M 长窗", "live_decision_envelope": "实盘决策点名的信封",
               "live_decision_envelope_unresolved_station_day": "点名信封找不到·整站日留",
               "serving_arithmetic_audit": "指纹审计", "live_decision_plan": "实盘决策点名的计划",
               "named:live_decision": "实盘决策的事实", "named:unlabelled_d2fact": "待贴标签",
               "latest_0:ensemble_system_id+station_id": "每系统×站最新",
               "live_decision_plan_unresolved_station_day": "点名计划找不到·整站日留"}


def _print_summary(receipt: Mapping[str, Any]) -> None:
    def gib(value: Any) -> str:
        return f"{int(value or 0) / GIB:.2f} GiB"

    def ceiling_words(ceiling: Mapping[str, Any]) -> str:
        head = (f"  写者上限：在用 {gib(ceiling.get('live_bytes'))}（文件 {gib(ceiling.get('file_bytes'))}）/ 上限 "
                f"{gib(ceiling.get('max_database_bytes'))}")
        if ceiling.get("writers_open"):
            return head + f" → 写者可开，还能写约 {gib(ceiling.get('writable_bytes'))}（先复用空闲页，再长到上限）"
        return head + " → 写者拒开：在用页超上限，须再搬或减写入"

    tier = receipt.get("cold_tier") or {}
    mode = "预演（只读，一行不动）" if receipt.get("mode") == "dry_run" else "执行"
    print(f"SSOT 老行冷层（宪章 237）  {mode}  状态={receipt.get('state')}")
    print(f"  冷层：{tier.get('state')}  {tier.get('root') or ''}"
          + (f"  剩余 {gib(tier.get('free_bytes'))}" if tier.get("free_bytes") else ""))
    before = receipt.get("store_before") or {}
    if before:
        print(f"  SSOT：文件 {gib(before.get('file_bytes_by_pages'))}，在用 {gib(before.get('live_bytes'))}，"
              f"空闲页 {gib(before.get('free_page_bytes'))}")
    if receipt.get("ceiling_before"):
        print(ceiling_words(receipt["ceiling_before"]))
    for row in receipt.get("tables") or []:
        kept = "、".join(
            f"{_KEPT_WORDS.get(k, '每键最新' if k.startswith('latest_') else k)} {v:,}"
            for k, v in (row.get("kept") or {}).items()
        ) or "无"
        print(f"  · {row['table']}  [{_STATE_WORDS.get(row['state'], row['state'])}]")
        if row.get("refused"):
            print(f"      原因：{'; '.join(row['refused'])}")
            continue
        print(f"      全表 {int(row.get('table_rows') or 0):,} 行 ≈ {gib(row.get('table_estimated_bytes'))}"
              f"；{row['time_column']} 早于 {row['cutoff_before']} 的旧行 {int(row['older_than_cutoff']):,}"
              + (f"（且 {row['candidate_where']}）" if row.get("candidate_where") else ""))
        print(f"      保留：{kept}")
        if row.get("absent_vector_refs"):
            print(f"      执行向量点名、但本站日没有这份哈希的盘口：{row['absent_vector_refs']:,} 个（不留整站日）")
        print(f"      可搬 {int(row['candidate_rows']):,} 行 ≈ {gib(row.get('estimated_bytes'))}"
              + (f"（{row['oldest_candidate']} … {row['newest_candidate']}）" if row.get("oldest_candidate") else ""))
    print(f"  合计：七张表约 {gib(receipt.get('table_estimated_bytes'))}，可搬约 {gib(receipt.get('estimated_bytes'))}"
          f"（{int(receipt.get('candidate_rows') or 0):,} 行；行内数据量，不含索引与页内空隙）")
    for row in receipt.get("moves") or []:
        print(
            f"  搬移 {row.get('table')}：{_STATE_WORDS.get(str(row.get('state')), row.get('state'))}"
            f"  {int(row.get('moved_rows', 0)):,} 行 ≈ {gib(row.get('moved_bytes'))}，{row.get('batches', 0)} 批，"
            f"单批持锁最长 {row.get('max_lock_sec', 0)} 秒"
            + (f"  原因：{row['reason']}" if row.get("reason") else "")
        )
    after = receipt.get("store_after") or {}
    if after:
        print(f"  SSOT 之后：在用 {gib(after.get('live_bytes'))}，空闲页 {gib(after.get('free_page_bytes'))}"
              "（空闲页由后续写入复用，文件本身不缩）")
    if receipt.get("ceiling_after"):
        print(ceiling_words(receipt["ceiling_after"]))
    print(f"  用时 {receipt.get('run_seconds')} 秒")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ssot", type=Path, default=DEFAULT_SSOT)
    parser.add_argument("--cold-tier-root", type=Path, default=None,
                        help="default: the charter 196 cold root (PM_COLD_TIER_ROOT)")
    parser.add_argument("--cold-tier-min-free-gib", type=float,
                        default=DEFAULT_COLD_TIER_MIN_FREE_BYTES / GIB)
    parser.add_argument("--tables", nargs="*", choices=sorted(POLICY_BY_TABLE))
    parser.add_argument("--apply", action="store_true", help="move; the default only plans")
    parser.add_argument("--budget-seconds", type=float, default=0.0, help="0 = no limit")
    parser.add_argument("--pause-factor", type=float, default=DEFAULT_PAUSE_FACTOR)
    parser.add_argument("--batch-rows", type=int, default=BATCH_ROWS)
    parser.add_argument("--batch-mib", type=float, default=BATCH_BYTES / MIB)
    parser.add_argument("--estimate-sample", type=int, default=DEFAULT_ESTIMATE_SAMPLE)
    parser.add_argument("--receipt", type=Path, default=None,
                        help="default: runtime/ssot_cold_offload_latest.json beside the SSOT")
    parser.add_argument("--list-policies", action="store_true", help="print the registry and stop")
    parser.add_argument("--restore", action="store_true",
                        help="put one table's cold rows back (needs --tables T --since YYYY-MM-DD)")
    parser.add_argument("--since", help="restore: rows whose time is on or after this")
    parser.add_argument("--until", help="restore: rows whose time is before this")
    args = parser.parse_args(argv)

    if args.list_policies:
        print(json.dumps([policy.describe() for policy in POLICIES], ensure_ascii=False, indent=2))
        return 0
    ssot_path = args.ssot.expanduser().resolve()
    # The ledger, the holds and the lock live beside the SSOT they describe,
    # so a copy of this script run from anywhere else (an unpacked branch)
    # keeps the daily job's books and takes the daily job's lock.
    runtime = args.ssot.expanduser().absolute().parent / RUNTIME.name
    manifest, holds = runtime / MANIFEST.name, runtime / HOLDS.name
    receipt_path = args.receipt if args.receipt is not None else runtime / RECEIPT.name
    root = args.cold_tier_root if args.cold_tier_root is not None else cold_tier_root()
    lock = _single_instance(runtime / LOCK.name)
    if lock is None:
        print("  another ssot_cold_offload run holds the lock; nothing done")
        return 0
    try:
        if args.restore:
            if not args.tables or len(args.tables) != 1 or not args.since:
                parser.error("--restore needs exactly one --tables and --since")
            if root is None:
                parser.error("--restore needs the cold tier")
            outcome = restore_table(ssot_path=ssot_path, cold_root=Path(root), table=args.tables[0],
                                    since=args.since, until=args.until, holds_path=holds)
            print(json.dumps(outcome, ensure_ascii=False, indent=2))
            return 0
        previous: dict[str, Any] = {}
        if receipt_path and receipt_path.is_file():
            try:
                previous = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                previous = {}
        receipt = run(
            plan_first=[
                str(row.get("table")) for row in (previous.get("tables") or [])
                if isinstance(row, dict) and row.get("state") == "not_planned_budget"
            ],
            ssot_path=ssot_path, cold_root=Path(root) if root is not None else None,
            tables=args.tables, apply=args.apply, budget_seconds=args.budget_seconds,
            pause_factor=args.pause_factor, batch_rows=args.batch_rows,
            batch_bytes=int(args.batch_mib * MIB),
            min_cold_free_bytes=int(args.cold_tier_min_free_gib * GIB),
            estimate_sample=args.estimate_sample, manifest=manifest, holds_path=holds,
            row_days_cache=manifest.parent / ROW_DAYS_CACHE.name,
        )
        if receipt_path:
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_text(
                json.dumps(receipt, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
            )
        _print_summary(receipt)
        return 1 if receipt.get("state") == "stopped" else 0
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
