"""Atomic X-S-B probability bridge for the target probability lane.

The probability compiler owns the distribution over the canonical settlement
state ``X``.  The market registry owns the settlement semantics ``S`` and the
ordered market buckets ``B``.  This module is the deliberately narrow bridge
between them: it freezes the exact bucket map already bound into a
``ProbabilityEnvelope`` and materializes the resulting ``p(B)`` without
reading weather, models, market quotes, or execution state.

The tables are intentionally separate from the append-only envelope table.
They are a read-optimised projection and a latest pointer, never a second
posterior authority.  A caller must install the small DDL in its own schema
migration; this module never installs it implicitly on a production database.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from .atomic_ledger import ledger_span
from .contracts import ContractError, ProbabilityEnvelope, canonical_json, content_sha256, timestamp_lte
from .materialization import insert_probability_envelope
from .resource_bounds import ResourceBoundError, assert_connection_target_capacity, assert_process_memory_budget
from .ssot import PROBABILITY_BRIDGE_DDL
from .stages import STAGE_PRECEDENCE


SCHEMA = "bayes_basket_target_probability_xsb_bridge_v1"
MAX_BUCKETS = 512
MAX_BACKFILL_ENVELOPES = 512
PROBABILITY_TOLERANCE = 1e-9
_TABLES = (
    "basket_probability_bridge_history",
    "basket_probability_bridge_latest",
    "basket_probability_bridge_bucket_probability",
)


DDL = PROBABILITY_BRIDGE_DDL


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"{name}_invalid")
    return result


def _timestamp(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return result


def _finite_or_none(name: str, value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ContractError(f"{name}_invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if not math.isfinite(result):
        raise ContractError(f"{name}_invalid")
    return result


def install_probability_bridge_schema(
    conn: sqlite3.Connection,
    *,
    commit: bool = False,
) -> dict[str, Any]:
    """Install the isolated bridge DDL on a caller-controlled connection."""

    # sqlite3.executescript() owns transaction handling and implicitly closes
    # savepoints.  DDL installation is therefore deliberately a separate,
    # migration-only operation; the data materializer below is the atomic
    # checkpoint/history/latest/p(B) boundary.
    conn.executescript(DDL)
    missing = [
        table
        for table in _TABLES
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is None
    ]
    if missing:
        raise ContractError("probability_bridge_schema_incomplete:" + ",".join(missing))
    if commit:
        conn.commit()
    return {"schema": SCHEMA, "ok": True, "tables": list(_TABLES), "rows_written": 0}


def probability_bridge_schema_status(conn: sqlite3.Connection) -> dict[str, Any]:
    present = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    missing = [table for table in _TABLES if table not in present]
    return {"schema": SCHEMA, "ok": not missing, "missing_tables": missing}


def _bucket_map(
    envelope: ProbabilityEnvelope,
    *,
    bucket_intervals: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], ...]:
    if bucket_intervals is None:
        raw = envelope.lineage.get("bucket_intervals_c_json")
        if not raw:
            raise ContractError("xsb_bucket_contract_missing")
        try:
            intervals = json.loads(str(raw))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError("xsb_bucket_contract_json_invalid") from exc
    else:
        intervals = dict(bucket_intervals)
    if not isinstance(intervals, Mapping) or set(map(str, intervals)) != set(envelope.probabilities):
        raise ContractError("xsb_bucket_contract_support_mismatch")
    if len(intervals) > MAX_BUCKETS:
        raise ContractError("xsb_bucket_contract_exceeds_limit")
    rows: list[dict[str, Any]] = []
    for bucket, ordinal in envelope.bucket_ordinals.items():
        bounds = intervals.get(bucket)
        if not isinstance(bounds, Sequence) or isinstance(bounds, (str, bytes, bytearray)) or len(bounds) != 2:
            raise ContractError("xsb_bucket_contract_interval_invalid")
        lower = _finite_or_none("xsb_bucket_lower_c", bounds[0])
        upper = _finite_or_none("xsb_bucket_upper_c", bounds[1])
        if lower is not None and upper is not None and lower >= upper:
            raise ContractError("xsb_bucket_contract_interval_invalid")
        rows.append({
            "bucket": str(bucket),
            "ordinal": int(ordinal),
            "lower_c": lower,
            "upper_c": upper,
        })
    rows.sort(key=lambda row: (row["ordinal"], row["bucket"]))
    ordinals = [row["ordinal"] for row in rows]
    if ordinals != list(range(ordinals[0], ordinals[0] + len(rows))):
        raise ContractError("xsb_bucket_contract_ordinal_invalid")
    for index, row in enumerate(rows):
        if (row["lower_c"] is None) != (index == 0):
            raise ContractError("xsb_bucket_contract_lower_tail_invalid")
        if (row["upper_c"] is None) != (index == len(rows) - 1):
            raise ContractError("xsb_bucket_contract_upper_tail_invalid")
        if index:
            previous = rows[index - 1]
            if previous["upper_c"] != row["lower_c"]:
                raise ContractError("xsb_bucket_contract_gap_or_overlap")
    return tuple(rows)


def build_xsb_projection(
    envelope: ProbabilityEnvelope | Mapping[str, Any],
    *,
    bucket_intervals: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze X→S→B mapping and project the materialised stage to ``p(B)``.

    ``ProbabilityEnvelope`` is the already-calibrated stage distribution over
    the canonical settlement state.  The bridge does not recalculate that
    probability: it binds its content SHA to the contract-derived B map and
    exposes the exact bucket projection for consumers.
    """

    resolved = envelope if isinstance(envelope, ProbabilityEnvelope) else ProbabilityEnvelope.from_mapping(envelope)
    rows = _bucket_map(
        resolved,
        bucket_intervals=bucket_intervals,
    )
    probabilities: list[dict[str, Any]] = []
    total = 0.0
    for row in rows:
        probability = float(resolved.probabilities[row["bucket"]])
        if not math.isfinite(probability) or probability < 0.0:
            raise ContractError("xsb_probability_invalid")
        total += probability
        probabilities.append({**row, "probability": probability})
    if abs(total - 1.0) > PROBABILITY_TOLERANCE:
        raise ContractError("xsb_probability_not_normalized")
    bucket_map = {
        "schema": SCHEMA,
        "station_id": resolved.station_id,
        "event_date": resolved.event_date,
        "settlement_timezone": resolved.settlement_timezone,
        "settlement_definition": _text(
            "xsb_settlement_definition",
            resolved.lineage.get("settlement_definition"),
        ),
        "bridge_sha256": _sha("xsb_bridge_sha256", resolved.bridge_sha256),
        "buckets": [{key: row[key] for key in ("bucket", "ordinal", "lower_c", "upper_c")} for row in rows],
    }
    bucket_map_sha256 = content_sha256(bucket_map)
    frozen_intervals = {
        str(row["bucket"]): [row["lower_c"], row["upper_c"]]
        for row in rows
    }
    core = {
        "schema": SCHEMA,
        "stage": resolved.stage,
        "checkpoint_id": resolved.checkpoint_id,
        "station_id": resolved.station_id,
        "event_date": resolved.event_date,
        "settlement_timezone": resolved.settlement_timezone,
        "decision_at_utc": resolved.decision_at_utc,
        "fresh_until_utc": resolved.fresh_until_utc,
        "envelope_content_sha256": resolved.content_sha256,
        "bridge_sha256": resolved.bridge_sha256,
        "bucket_map_sha256": bucket_map_sha256,
        "bucket_intervals": frozen_intervals,
        "probability_by_bucket": probabilities,
    }
    return {**core, "projection_sha256": content_sha256(core)}


def _history_identity(projection: Mapping[str, Any], envelope: ProbabilityEnvelope, created_at_utc: str) -> dict[str, Any]:
    core = {
        "schema": SCHEMA,
        "history_role": "APPEND_ONLY_PROBABILITY_PROJECTION",
        "stage": envelope.stage,
        "checkpoint_id": envelope.checkpoint_id,
        "station_id": envelope.station_id,
        "event_date": envelope.event_date,
        "settlement_timezone": envelope.settlement_timezone,
        "decision_at_utc": envelope.decision_at_utc,
        "fresh_until_utc": envelope.fresh_until_utc,
        "envelope_content_sha256": envelope.content_sha256,
        "bridge_sha256": envelope.bridge_sha256,
        "bucket_map_sha256": projection["bucket_map_sha256"],
        "bucket_intervals": projection["bucket_intervals"],
        "projection_sha256": projection["projection_sha256"],
        "envelope": envelope.to_dict(),
        "created_at_utc": created_at_utc,
    }
    return core


def history_envelope_payload(
    conn: sqlite3.Connection,
    *,
    checkpoint_id: str,
    envelope_content_sha256: str,
    stored: str | None = None,
) -> dict[str, Any]:
    """The envelope body for one bridge-history row, as a mapping.

    History rows written before 2026-08-26 carry the body inline in
    ``envelope_json``; pass that column as ``stored`` and it is parsed
    as-is.  Newer rows leave the column empty -- the body was already
    written to ``basket_probability_envelope`` by the same savepoint, keyed
    by this checkpoint, and copying it doubled the store (5.9 GB of the 27
    that hit the 2026-08-26 ceiling).  A rebuilt payload is hashed and must
    equal the sha the history row has always carried, so a divergence fails
    loudly instead of serving a body the projection was not computed from.
    """

    if stored:
        try:
            parsed = json.loads(str(stored))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError("xsb_history_envelope_body_invalid") from exc
        if not isinstance(parsed, dict):
            raise ContractError("xsb_history_envelope_body_invalid")
        return parsed
    row = conn.execute(
        "SELECT stage,checkpoint_id,station_id,event_date,"
        "settlement_timezone,decision_at_utc,fresh_until_utc,"
        "bucket_ordinals_json,probabilities_json,posterior_version,"
        "posterior_sha256,bridge_sha256,lineage_json "
        "FROM basket_probability_envelope WHERE envelope_id=?",
        (str(checkpoint_id),),
    ).fetchone()
    if row is None:
        raise ContractError("xsb_history_envelope_body_missing")
    try:
        payload = {
            "schema": "bayes_basket_probability_envelope_v1",
            "stage": str(row[0]),
            "checkpoint_id": str(row[1]),
            "station_id": str(row[2]),
            "event_date": str(row[3]),
            "settlement_timezone": str(row[4]),
            "decision_at_utc": str(row[5]),
            "fresh_until_utc": str(row[6]),
            "bucket_ordinals": json.loads(str(row[7])),
            "probabilities": json.loads(str(row[8])),
            "posterior_version": str(row[9]),
            "posterior_sha256": str(row[10]),
            "bridge_sha256": str(row[11]),
            "lineage": json.loads(str(row[12])),
        }
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError("xsb_history_envelope_body_invalid") from exc
    if content_sha256(payload) != str(envelope_content_sha256):
        raise ContractError("xsb_history_envelope_body_sha_mismatch")
    return payload


def history_envelope_json(
    conn: sqlite3.Connection,
    *,
    checkpoint_id: str,
    envelope_content_sha256: str,
    stored: str | None = None,
) -> str:
    """The canonical envelope JSON string; see history_envelope_payload."""

    if stored:
        return str(stored)
    return canonical_json(history_envelope_payload(
        conn,
        checkpoint_id=checkpoint_id,
        envelope_content_sha256=envelope_content_sha256,
    ))


def _history_rows(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    as_of_utc: str | None,
    history_id: str | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    where_as_of = (
        " AND decision_at_utc<=?"
        if as_of_utc is not None
        else ""
    )
    where_history = " AND history_id=?" if history_id is not None else ""
    parameters: tuple[str, ...] = (
        (station_id, event_date, as_of_utc)
        if as_of_utc is not None
        else (station_id, event_date)
    )
    if history_id is not None:
        parameters = parameters + (history_id,)
    for row in conn.execute(
        "SELECT history_id,stage,checkpoint_id,decision_at_utc,fresh_until_utc,"
        "envelope_content_sha256,bridge_sha256,bucket_map_sha256,"
        "bucket_intervals_json,projection_sha256,envelope_json,"
        "content_sha256,created_at_utc "
        "FROM basket_probability_bridge_history "
        "WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        + where_as_of
        + where_history
        + " "
        "ORDER BY decision_at_utc,stage,checkpoint_id",
        parameters,
    ).fetchall():
        try:
            intervals_payload = json.loads(str(row[8]))
            envelope_payload = history_envelope_payload(
                conn,
                checkpoint_id=str(row[2]),
                envelope_content_sha256=str(row[5]),
                stored=str(row[10]) if row[10] else None,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError("xsb_history_envelope_json_invalid") from exc
        if not isinstance(intervals_payload, Mapping):
            raise ContractError("xsb_history_bucket_intervals_invalid")
        envelope = ProbabilityEnvelope.from_mapping(envelope_payload)
        history = {
            "history_id": str(row[0]), "stage": str(row[1]), "checkpoint_id": str(row[2]),
            "decision_at_utc": str(row[3]), "fresh_until_utc": str(row[4]),
            "envelope_content_sha256": _sha("xsb_history_envelope_sha256", row[5]),
            "bridge_sha256": _sha("xsb_history_bridge_sha256", row[6]),
            "bucket_map_sha256": _sha("xsb_history_bucket_map_sha256", row[7]),
            "bucket_intervals": dict(intervals_payload),
            "projection_sha256": _sha("xsb_history_projection_sha256", row[9]),
            "envelope": envelope,
            "content_sha256": _sha(
                "xsb_history_content_sha256",
                row[11],
            ),
            "created_at_utc": str(row[12]),
        }
        if (
            history["stage"] != envelope.stage
            or history["checkpoint_id"] != envelope.checkpoint_id
            or history["envelope_content_sha256"] != envelope.content_sha256
            or history["bridge_sha256"] != envelope.bridge_sha256
        ):
            raise ContractError("xsb_history_envelope_binding_mismatch")
        expected_identity = _history_identity(
            {
                "bucket_map_sha256": history["bucket_map_sha256"],
                "bucket_intervals": history["bucket_intervals"],
                "projection_sha256": history["projection_sha256"],
            },
            envelope,
            history["created_at_utc"],
        )
        if history["content_sha256"] != content_sha256(expected_identity):
            raise ContractError("xsb_history_content_sha256_mismatch")
        rows.append(history)
    return rows


def _authoritative_history(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    as_of_utc: str | None,
) -> Mapping[str, Any]:
    """The current pointer, without reading every envelope to find it.

    ``_select_authoritative_history`` ranks on two fields, stage and decision
    time, and the caller then uses one row.  Loading the whole station-day to
    supply them meant reading and JSON-parsing every committed envelope on
    every write -- and since each write appends one, the cost of writing a
    day grew with the day.  Measured 2026-08-22: 357 rows and 8.4 MB for the
    station-day being written, up to 1,394 rows and 27.9 MB for the worst
    one, 220 to 440ms a call, inside the writer's transaction.  Sixty-two P0
    envelopes in one transaction is where ``resident_p0_inbox`` spent 22 of
    its 36 seconds holding the write lock.

    So rank on three narrow columns, then load only the winner.  The rule is
    still ``_select_authoritative_history`` -- it is handed lighter rows, not
    a second implementation of itself -- and it runs again on the loaded row
    so the missing and ambiguous contracts are raised from the same place.
    """

    where_as_of = " AND decision_at_utc<=?" if as_of_utc is not None else ""
    parameters: tuple[str, ...] = (
        (station_id, event_date, as_of_utc)
        if as_of_utc is not None
        else (station_id, event_date)
    )
    with ledger_span("bridge_history_rank"):
        ranking = [
            {
                "history_id": str(row[0]),
                "stage": str(row[1]),
                "decision_at_utc": str(row[2]),
            }
            for row in conn.execute(
                "SELECT history_id,stage,decision_at_utc "
                "FROM basket_probability_bridge_history "
                "WHERE station_id=? AND event_date=? AND state='COMMITTED'"
                + where_as_of
                + " ORDER BY decision_at_utc,stage,checkpoint_id",
                parameters,
            ).fetchall()
        ]
    winner = _select_authoritative_history(ranking)
    with ledger_span("bridge_history_load_winner"):
        loaded = _history_rows(
            conn,
            station_id=station_id,
            event_date=event_date,
            as_of_utc=as_of_utc,
            history_id=str(winner["history_id"]),
        )
    return _select_authoritative_history(loaded)


def _select_authoritative_history(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    if not rows:
        raise ContractError("xsb_probability_history_missing")
    # Stage authority is deliberately independent of the most recent P0 clock:
    # a new raw P0 must replay P0-M/P1 rather than silently displace them.
    highest_stage = max(STAGE_PRECEDENCE[str(row["stage"])] for row in rows)
    candidates = [row for row in rows if STAGE_PRECEDENCE[str(row["stage"])] == highest_stage]
    latest_at = max(
        (str(row["decision_at_utc"]) for row in candidates),
        key=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")),
    )
    winners = [
        row
        for row in candidates
        if datetime.fromisoformat(str(row["decision_at_utc"]).replace("Z", "+00:00"))
        == datetime.fromisoformat(latest_at.replace("Z", "+00:00"))
    ]
    if len(winners) != 1:
        raise ContractError("xsb_probability_history_ambiguous")
    return winners[0]


def _load_projection_rows(conn: sqlite3.Connection, history: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = [
        {
            "bucket": str(row[0]), "ordinal": int(row[1]),
            "lower_c": None if row[2] is None else float(row[2]),
            "upper_c": None if row[3] is None else float(row[3]),
            "probability": float(row[4]), "content_sha256": _sha("xsb_bucket_row_sha256", row[5]),
        }
        for row in conn.execute(
            "SELECT bucket,ordinal,lower_c,upper_c,probability,content_sha256 "
            "FROM basket_probability_bridge_bucket_probability WHERE history_id=? "
            "ORDER BY ordinal,bucket",
            (history["history_id"],),
        ).fetchall()
    ]
    expected = build_xsb_projection(
        history["envelope"],
        bucket_intervals=history["bucket_intervals"],
    )
    if (
        expected["bucket_map_sha256"] != history["bucket_map_sha256"]
        or expected["projection_sha256"] != history["projection_sha256"]
        or len(rows) != len(expected["probability_by_bucket"])
    ):
        raise ContractError("xsb_projection_history_mismatch")
    for actual, wanted in zip(rows, expected["probability_by_bucket"]):
        identity = {
            "schema": SCHEMA, "history_id": history["history_id"],
            **{key: actual[key] for key in ("bucket", "ordinal", "lower_c", "upper_c", "probability")},
        }
        if (
            any(actual[key] != wanted[key] for key in ("bucket", "ordinal", "lower_c", "upper_c"))
            or abs(actual["probability"] - wanted["probability"]) > 1e-12
            or actual["content_sha256"] != content_sha256(identity)
        ):
            raise ContractError("xsb_projection_bucket_row_mismatch")
    if (
        abs(sum(row["probability"] for row in rows) - 1.0)
        > PROBABILITY_TOLERANCE
    ):
        raise ContractError("xsb_projection_probability_not_normalized")
    return rows


def _validate_latest_pointer(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    as_of_utc: str,
    authority: Mapping[str, Any],
) -> None:
    """Reject a current-pointer drift instead of recovering from history."""

    row = conn.execute(
        "SELECT settlement_timezone,history_id,stage,checkpoint_id,decision_at_utc,fresh_until_utc,"
        "envelope_content_sha256,projection_sha256,content_sha256,updated_at_utc "
        "FROM basket_probability_bridge_latest WHERE station_id=? AND event_date=?",
        (station_id, event_date),
    ).fetchone()
    if row is None:
        raise ContractError("xsb_latest_pointer_missing")
    decision_at = _timestamp("xsb_latest_pointer_decision_at_utc", row[4])
    identity = {
        "schema": SCHEMA, "station_id": station_id, "event_date": event_date,
        "settlement_timezone": str(row[0]), "history_id": str(row[1]),
        "stage": str(row[2]), "checkpoint_id": str(row[3]), "decision_at_utc": decision_at,
        "fresh_until_utc": str(row[5]), "envelope_content_sha256": str(row[6]),
        "projection_sha256": str(row[7]), "updated_at_utc": str(row[9]),
    }
    if _sha("xsb_latest_pointer_content_sha256", row[8]) != content_sha256(identity):
        raise ContractError("xsb_latest_pointer_content_sha256_mismatch")
    if not timestamp_lte(decision_at, as_of_utc):
        # A future latest pointer is expected in historical replay.  History
        # remains the as-of authority, but its self-hash must still validate.
        return
    expected = authority["envelope"]
    if (
        str(row[1]) != authority["history_id"]
        or str(row[2]) != expected.stage
        or str(row[3]) != expected.checkpoint_id
        or str(row[6]) != expected.content_sha256
        or str(row[7]) != authority["projection_sha256"]
    ):
        raise ContractError("xsb_latest_pointer_authority_mismatch")


def load_current_probability_bridge(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    as_of_utc: str,
) -> dict[str, Any]:
    """Resolve the single authority without downgrading malformed/stale P1."""

    if not probability_bridge_schema_status(conn)["ok"]:
        raise ContractError("probability_bridge_schema_missing")
    _timestamp("xsb_as_of_utc", as_of_utc)
    history = _authoritative_history(
        conn,
        station_id=_text("xsb_station_id", station_id),
        event_date=_text("xsb_event_date", event_date),
        as_of_utc=as_of_utc,
    )
    _validate_latest_pointer(
        conn,
        station_id=station_id,
        event_date=event_date,
        as_of_utc=as_of_utc,
        authority=history,
    )
    envelope = history["envelope"]
    # This check intentionally happens after stage authority is selected.  A
    # stale P1 blocks; it must never cause a fallback to a fresh P0/P0-M.
    if not envelope.is_servable(as_of_utc):
        raise ContractError("xsb_latest_stage_stale_fail_closed")
    rows = _load_projection_rows(conn, history)
    return {
        "schema": SCHEMA,
        "state": "COMMITTED",
        "stage": envelope.stage,
        "checkpoint_id": envelope.checkpoint_id,
        "history_id": history["history_id"],
        "envelope_content_sha256": envelope.content_sha256,
        "projection_sha256": history["projection_sha256"],
        "probability_by_bucket": rows,
        "places_orders": False,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }


def materialize_probability_bridge(
    conn: sqlite3.Connection,
    envelope: ProbabilityEnvelope | Mapping[str, Any],
    *,
    created_at_utc: str,
    bucket_intervals: Mapping[str, Any] | None = None,
    failure_injector: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Atomically commit checkpoint, append-only history, latest and p(B).

    ``failure_injector`` is intentionally a test seam.  It runs after each
    physical write point while the savepoint remains active, proving a caller
    sees all rows or none of the new rows when a disk/SQLite error occurs.
    """

    if not probability_bridge_schema_status(conn)["ok"]:
        raise ContractError("probability_bridge_schema_missing")
    try:
        assert_process_memory_budget()
        assert_connection_target_capacity(conn)
    except ResourceBoundError as exc:
        raise ContractError("probability_bridge_resource_gate_failed:" + str(exc)) from exc
    resolved = envelope if isinstance(envelope, ProbabilityEnvelope) else ProbabilityEnvelope.from_mapping(envelope)
    created = _timestamp("xsb_created_at_utc", created_at_utc)
    if not timestamp_lte(resolved.decision_at_utc, created):
        raise ContractError("xsb_created_before_checkpoint_decision")
    # Ninety-six seconds of a 383s cycle went into writing envelopes and only
    # twelve of them had a name: the envelope insert is timed inside
    # ``materialization``, and everything this function does around it -- the
    # projection, the three tables -- was not.  Each phase is marked so the
    # next "resident_p0_inbox held the writer for 36s" reads as an answer
    # rather than a starting point.
    with ledger_span("bridge_project"):
        projection = build_xsb_projection(
            resolved,
            bucket_intervals=bucket_intervals,
        )
    identity = _history_identity(projection, resolved, created)
    history_id = "xsb_" + content_sha256(identity)[:32]
    history_sha = content_sha256(identity)
    savepoint = f"probability_xsb_materialize_{id(conn):x}"

    def fault(point: str) -> None:
        if failure_injector is not None:
            failure_injector(point)

    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        with ledger_span("bridge_checkpoint"):
            checkpoint_receipt = insert_probability_envelope(
                conn, resolved, created_at_utc=created
            )
        fault("checkpoint")
        existing = conn.execute(
            "SELECT content_sha256 FROM basket_probability_bridge_history WHERE history_id=?",
            (history_id,),
        ).fetchone()
        if existing is not None:
            if str(existing[0]) != history_sha:
                raise ContractError("xsb_history_id_content_conflict")
            history_wrote = False
        else:
            with ledger_span("bridge_history_insert"):
                conn.execute(
                    "INSERT INTO basket_probability_bridge_history("
                    "history_id,station_id,event_date,settlement_timezone,stage,checkpoint_id,decision_at_utc,fresh_until_utc,"
                    "envelope_content_sha256,bridge_sha256,bucket_map_sha256,"
                    "bucket_intervals_json,projection_sha256,envelope_json,state,"
                    "created_at_utc,content_sha256) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        history_id, resolved.station_id, resolved.event_date, resolved.settlement_timezone,
                        resolved.stage, resolved.checkpoint_id, resolved.decision_at_utc, resolved.fresh_until_utc,
                        resolved.content_sha256, resolved.bridge_sha256,
                        projection["bucket_map_sha256"],
                        json.dumps(
                            projection["bucket_intervals"],
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        projection["projection_sha256"],
                        # The body already lives in basket_probability_envelope,
                        # written by this same savepoint and keyed by this
                        # checkpoint; readers rebuild it through
                        # history_envelope_json.  Copying it here doubled the
                        # store (5.9 GB of the 27 that hit the 08-26 ceiling).
                        "",
                        "COMMITTED", created, history_sha,
                    ),
                )
            history_wrote = True
        fault("history")
        authority = _authoritative_history(
            conn,
            station_id=resolved.station_id,
            event_date=resolved.event_date,
            # Incremental arrivals can be delayed or replayed out of order.
            # The current pointer must be selected from the complete committed
            # history, not from the triggering row's historical clock.
            as_of_utc=None,
        )
        latest_identity = {
            "schema": SCHEMA, "station_id": resolved.station_id, "event_date": resolved.event_date,
            "settlement_timezone": authority["envelope"].settlement_timezone,
            "history_id": authority["history_id"], "stage": authority["stage"],
            "checkpoint_id": authority["checkpoint_id"], "decision_at_utc": authority["decision_at_utc"],
            "fresh_until_utc": authority["fresh_until_utc"],
            "envelope_content_sha256": authority["envelope_content_sha256"],
            "projection_sha256": authority["projection_sha256"],
            # The pointer identity belongs to the selected authority, not to
            # whichever lower-stage replay happened to trigger this call.
            # Otherwise a later P0 replay could mutate the P1 pointer hash.
            "updated_at_utc": authority["created_at_utc"],
        }
        latest_sha = content_sha256(latest_identity)
        current_latest = conn.execute(
            "SELECT content_sha256 FROM basket_probability_bridge_latest WHERE station_id=? AND event_date=?",
            (resolved.station_id, resolved.event_date),
        ).fetchone()
        latest_wrote = current_latest is None or str(current_latest[0]) != latest_sha
        if latest_wrote:
            conn.execute(
                "INSERT INTO basket_probability_bridge_latest("
                "station_id,event_date,settlement_timezone,history_id,stage,checkpoint_id,decision_at_utc,fresh_until_utc,"
                "envelope_content_sha256,projection_sha256,content_sha256,updated_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(station_id,event_date) DO UPDATE SET "
                "settlement_timezone=excluded.settlement_timezone,history_id=excluded.history_id,stage=excluded.stage,"
                "checkpoint_id=excluded.checkpoint_id,decision_at_utc=excluded.decision_at_utc,fresh_until_utc=excluded.fresh_until_utc,"
                "envelope_content_sha256=excluded.envelope_content_sha256,projection_sha256=excluded.projection_sha256,"
                "content_sha256=excluded.content_sha256,updated_at_utc=excluded.updated_at_utc",
                (
                    latest_identity["station_id"], latest_identity["event_date"], latest_identity["settlement_timezone"],
                    latest_identity["history_id"], latest_identity["stage"], latest_identity["checkpoint_id"],
                    latest_identity["decision_at_utc"], latest_identity["fresh_until_utc"],
                    latest_identity["envelope_content_sha256"], latest_identity["projection_sha256"], latest_sha,
                    latest_identity["updated_at_utc"],
                ),
            )
        fault("latest")
        if history_wrote:
            # One statement for the whole projection, not one per bucket.
            # This runs inside the resident's write lock, and a P0 arrival
            # burst carries thousands of envelopes at roughly ten buckets
            # each: measured 2026-08-22, 4115 envelopes landed in ten
            # minutes and ``resident_p0_inbox`` held the writer for 20.9s of
            # a 33.8s cycle, which is longer than the five seconds the other
            # lanes wait before failing closed -- so market_ingress, P0, P1,
            # regime and the probability chain all reported "database is
            # locked" against a lock this loop was holding.
            #
            # The fault points are unchanged.  They fire after the rows are
            # staged rather than between individual inserts, which is the
            # same observable contract: every one of them is inside the
            # savepoint, and the savepoint rolls back whole.
            bucket_rows = [
                (
                    history_id, row["bucket"], row["ordinal"], row["lower_c"],
                    row["upper_c"], row["probability"],
                    content_sha256(
                        {"schema": SCHEMA, "history_id": history_id, **row}
                    ),
                )
                for row in projection["probability_by_bucket"]
            ]
            if bucket_rows:
                conn.executemany(
                    "INSERT INTO basket_probability_bridge_bucket_probability("
                    "history_id,bucket,ordinal,lower_c,upper_c,probability,content_sha256) VALUES(?,?,?,?,?,?,?)",
                    bucket_rows,
                )
            for index in range(len(bucket_rows)):
                fault(f"p_b:{index}")
        fault("p_b")
        # Validate before releasing the savepoint so a malformed projection can
        # never become a partially committed current pointer.
        _load_projection_rows(conn, {"history_id": history_id, "envelope": resolved,
                                    "bucket_map_sha256": projection["bucket_map_sha256"],
                                    "bucket_intervals": projection["bucket_intervals"],
                                    "projection_sha256": projection["projection_sha256"]})
        if authority["history_id"] != history_id:
            _load_projection_rows(conn, authority)
        _validate_latest_pointer(
            conn,
            station_id=resolved.station_id,
            event_date=resolved.event_date,
            as_of_utc=authority["decision_at_utc"],
            authority=authority,
        )
        # The post-write resource probe is part of the transaction boundary:
        # if it fails, checkpoint/history/latest/p(B) all roll back together.
        assert_connection_target_capacity(conn)
    except BaseException:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        raise
    else:
        conn.execute(f"RELEASE {savepoint}")
    return {
        "schema": SCHEMA, "state": "COMMITTED", "history_id": history_id,
        "checkpoint": checkpoint_receipt, "history_wrote": history_wrote, "latest_wrote": latest_wrote,
        "bucket_count": len(projection["probability_by_bucket"]),
        "projection_sha256": projection["projection_sha256"],
        "wrote": bool(
            checkpoint_receipt.get("wrote")
            or history_wrote
            or latest_wrote
        ),
        "places_orders": False, "calls_order_gateway": False,
        "live_submit_authorized": False, "orders_submitted": 0,
    }


def canonical_replay_probability_bridges(
    envelopes: Sequence[ProbabilityEnvelope | Mapping[str, Any]],
) -> dict[str, Any]:
    """Pure canonical replay receipt used to compare incremental/full streams."""

    if not isinstance(envelopes, Sequence) or isinstance(envelopes, (str, bytes, bytearray)):
        raise ContractError("xsb_replay_envelopes_list_required")
    resolved = [item if isinstance(item, ProbabilityEnvelope) else ProbabilityEnvelope.from_mapping(item) for item in envelopes]
    if not resolved:
        raise ContractError("xsb_replay_envelopes_empty")
    scope = {(item.station_id, item.event_date, item.settlement_timezone) for item in resolved}
    if len(scope) != 1:
        raise ContractError("xsb_replay_scope_mismatch")
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for envelope in sorted(resolved, key=lambda item: (item.decision_at_utc, STAGE_PRECEDENCE[item.stage], item.checkpoint_id)):
        if envelope.checkpoint_id in seen:
            raise ContractError("xsb_replay_checkpoint_duplicate")
        seen.add(envelope.checkpoint_id)
        projection = build_xsb_projection(envelope)
        rows.append({
            "checkpoint_id": envelope.checkpoint_id, "stage": envelope.stage,
            "decision_at_utc": envelope.decision_at_utc,
            "envelope_content_sha256": envelope.content_sha256,
            "bridge_sha256": envelope.bridge_sha256,
            "bucket_map_sha256": projection["bucket_map_sha256"],
            "projection_sha256": projection["projection_sha256"],
            "probability_by_bucket": projection["probability_by_bucket"],
        })
    authority = _select_authoritative_history([
        {"stage": item.stage, "decision_at_utc": item.decision_at_utc, "checkpoint_id": item.checkpoint_id}
        for item in resolved
    ])
    core = {"schema": SCHEMA, "rows": rows, "authoritative_checkpoint_id": authority["checkpoint_id"],
            "authoritative_stage": authority["stage"], "boundary": {"writes_production_db": False, "places_orders": False, "calls_order_gateway": False}}
    return {**core, "replay_sha256": content_sha256(core)}


def verify_probability_bridge_replay_equivalence(
    conn: sqlite3.Connection,
    envelopes: Sequence[ProbabilityEnvelope | Mapping[str, Any]],
    *,
    tolerance: float = PROBABILITY_TOLERANCE,
) -> dict[str, Any]:
    """Prove materialized incremental state equals a canonical full replay.

    This is a read-only evidence receipt.  It compares every checkpoint,
    bucket contract and bucket probability, then verifies that ``latest``
    points at the same authority selected by the full replay.
    """

    if (
        isinstance(tolerance, bool)
        or not isinstance(tolerance, (int, float))
        or not math.isfinite(float(tolerance))
        or float(tolerance) < 0.0
        or float(tolerance) > PROBABILITY_TOLERANCE
    ):
        raise ContractError("xsb_equivalence_tolerance_invalid")
    canonical = canonical_replay_probability_bridges(envelopes)
    max_absolute_difference = 0.0
    for expected in canonical["rows"]:
        history_row = conn.execute(
            "SELECT history_id,envelope_content_sha256,bridge_sha256,"
            "bucket_map_sha256,projection_sha256 "
            "FROM basket_probability_bridge_history "
            "WHERE checkpoint_id=? AND state='COMMITTED'",
            (expected["checkpoint_id"],),
        ).fetchall()
        if len(history_row) != 1:
            raise ContractError("xsb_equivalence_history_cardinality_mismatch")
        history_id, envelope_sha, bridge_sha, bucket_map_sha, projection_sha = history_row[0]
        if (
            str(envelope_sha) != expected["envelope_content_sha256"]
            or str(bridge_sha) != expected["bridge_sha256"]
            or str(bucket_map_sha) != expected["bucket_map_sha256"]
            or str(projection_sha) != expected["projection_sha256"]
        ):
            raise ContractError("xsb_equivalence_hash_binding_mismatch")
        actual_rows = conn.execute(
            "SELECT bucket,ordinal,lower_c,upper_c,probability,content_sha256 "
            "FROM basket_probability_bridge_bucket_probability "
            "WHERE history_id=? ORDER BY ordinal,bucket",
            (str(history_id),),
        ).fetchall()
        expected_rows = expected["probability_by_bucket"]
        if len(actual_rows) != len(expected_rows):
            raise ContractError("xsb_equivalence_bucket_cardinality_mismatch")
        for actual, wanted in zip(actual_rows, expected_rows):
            if (
                str(actual[0]) != wanted["bucket"]
                or int(actual[1]) != wanted["ordinal"]
                or actual[2] != wanted["lower_c"]
                or actual[3] != wanted["upper_c"]
            ):
                raise ContractError("xsb_equivalence_bucket_contract_mismatch")
            row_identity = {
                "schema": SCHEMA,
                "history_id": str(history_id),
                "bucket": str(actual[0]),
                "ordinal": int(actual[1]),
                "lower_c": actual[2],
                "upper_c": actual[3],
                "probability": float(actual[4]),
            }
            if _sha("xsb_equivalence_bucket_row_sha256", actual[5]) != content_sha256(
                row_identity
            ):
                raise ContractError("xsb_equivalence_bucket_hash_mismatch")
            difference = abs(float(actual[4]) - float(wanted["probability"]))
            max_absolute_difference = max(max_absolute_difference, difference)
            if difference > float(tolerance):
                raise ContractError("xsb_equivalence_probability_mismatch")
    latest = conn.execute(
        "SELECT checkpoint_id,stage FROM basket_probability_bridge_latest "
        "WHERE station_id=? AND event_date=?",
        (
            (envelopes[0].station_id if isinstance(envelopes[0], ProbabilityEnvelope)
             else ProbabilityEnvelope.from_mapping(envelopes[0]).station_id),
            (envelopes[0].event_date if isinstance(envelopes[0], ProbabilityEnvelope)
             else ProbabilityEnvelope.from_mapping(envelopes[0]).event_date),
        ),
    ).fetchall()
    if len(latest) != 1 or (
        str(latest[0][0]) != canonical["authoritative_checkpoint_id"]
        or str(latest[0][1]) != canonical["authoritative_stage"]
    ):
        raise ContractError("xsb_equivalence_latest_authority_mismatch")
    core = {
        "schema": SCHEMA,
        "state": "EQUIVALENT",
        "checkpoint_count": len(canonical["rows"]),
        "authoritative_checkpoint_id": canonical[
            "authoritative_checkpoint_id"
        ],
        "authoritative_stage": canonical["authoritative_stage"],
        "canonical_replay_sha256": canonical["replay_sha256"],
        "max_absolute_probability_difference": max_absolute_difference,
        "tolerance": float(tolerance),
        "boundary": {
            "writes_production_db": False,
            "places_orders": False,
            "calls_order_gateway": False,
        },
    }
    return {**core, "equivalence_sha256": content_sha256(core)}


def _backfill_probability_bridges(
    conn: sqlite3.Connection,
    *,
    max_envelopes: int = MAX_BACKFILL_ENVELOPES,
) -> dict[str, Any]:
    """Backfill exact historical p(B) projections during one maintenance txn.

    Derived P0-M/P1 envelopes may predate direct inheritance of the bucket
    interval JSON.  Their immutable P0 root remains the only permitted source
    for that contract.  An unbridgeable non-authoritative legacy row is
    reported and retained for audit; an unbridgeable authoritative row aborts
    the migration.
    """

    if (
        isinstance(max_envelopes, bool)
        or not isinstance(max_envelopes, int)
        or not 1 <= max_envelopes <= MAX_BACKFILL_ENVELOPES
    ):
        raise ContractError("xsb_backfill_limit_invalid")
    cursor = conn.execute(
        "SELECT stage,checkpoint_id,station_id,event_date,"
        "settlement_timezone,decision_at_utc,fresh_until_utc,"
        "bucket_ordinals_json,probabilities_json,posterior_version,"
        "posterior_sha256,bridge_sha256,lineage_json,created_at "
        "FROM basket_probability_envelope WHERE state='COMMITTED' "
        "ORDER BY decision_at_utc,stage,checkpoint_id LIMIT ?",
        (max_envelopes + 1,),
    )
    raw_rows = cursor.fetchall()
    if len(raw_rows) > max_envelopes:
        raise ContractError("xsb_backfill_envelope_limit_exceeded")
    envelopes: list[tuple[ProbabilityEnvelope, str]] = []
    by_checkpoint: dict[str, ProbabilityEnvelope] = {}
    for row in raw_rows:
        try:
            envelope = ProbabilityEnvelope.from_mapping({
                "stage": row[0],
                "checkpoint_id": row[1],
                "station_id": row[2],
                "event_date": row[3],
                "settlement_timezone": row[4],
                "decision_at_utc": row[5],
                "fresh_until_utc": row[6],
                "bucket_ordinals": json.loads(str(row[7])),
                "probabilities": json.loads(str(row[8])),
                "posterior_version": row[9],
                "posterior_sha256": row[10],
                "bridge_sha256": row[11],
                "lineage": json.loads(str(row[12])),
            })
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError("xsb_backfill_envelope_invalid") from exc
        envelopes.append((envelope, _timestamp(
            "xsb_backfill_created_at_utc",
            row[13],
        )))
        by_checkpoint[envelope.checkpoint_id] = envelope

    authoritative: dict[tuple[str, str], ProbabilityEnvelope] = {}
    for envelope, _created in envelopes:
        scope = (envelope.station_id, envelope.event_date)
        current = authoritative.get(scope)
        if current is None:
            authoritative[scope] = envelope
            continue
        candidate_key = (
            STAGE_PRECEDENCE[envelope.stage],
            datetime.fromisoformat(
                envelope.decision_at_utc.replace("Z", "+00:00")
            ),
        )
        current_key = (
            STAGE_PRECEDENCE[current.stage],
            datetime.fromisoformat(
                current.decision_at_utc.replace("Z", "+00:00")
            ),
        )
        if candidate_key > current_key:
            authoritative[scope] = envelope
        elif (
            candidate_key == current_key
            and envelope.checkpoint_id != current.checkpoint_id
        ):
            raise ContractError("xsb_backfill_authority_ambiguous")

    materialized = 0
    skipped: list[dict[str, str]] = []
    for envelope, created_at in envelopes:
        intervals_raw = envelope.lineage.get(
            "bucket_intervals_c_json"
        )
        root_checkpoint_id = str(
            envelope.lineage.get("p0_root_checkpoint_id")
            or envelope.lineage.get("prediction_root_checkpoint_id")
            or ""
        )
        if not intervals_raw and root_checkpoint_id:
            root = by_checkpoint.get(root_checkpoint_id)
            if root is not None:
                intervals_raw = root.lineage.get(
                    "bucket_intervals_c_json"
                )
        if not intervals_raw:
            skipped.append({
                "checkpoint_id": envelope.checkpoint_id,
                "reason": "exact_bucket_intervals_unavailable",
            })
            continue
        try:
            intervals = json.loads(str(intervals_raw))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError(
                "xsb_backfill_bucket_intervals_invalid"
            ) from exc
        if not isinstance(intervals, Mapping):
            raise ContractError(
                "xsb_backfill_bucket_intervals_invalid"
            )
        receipt = materialize_probability_bridge(
            conn,
            envelope,
            created_at_utc=created_at,
            bucket_intervals=intervals,
        )
        materialized += int(receipt["history_wrote"])

    skipped_ids = {item["checkpoint_id"] for item in skipped}
    invalid_authority = sorted(
        (
            envelope.station_id,
            envelope.event_date,
            envelope.checkpoint_id,
        )
        for envelope in authoritative.values()
        if envelope.checkpoint_id in skipped_ids
    )
    # Never leave a lower/older projection looking current when the true
    # highest-stage legacy authority lacks an exact bucket contract.
    for station_id, event_date, _checkpoint_id in invalid_authority:
        conn.execute(
            "DELETE FROM basket_probability_bridge_latest "
            "WHERE station_id=? AND event_date=?",
            (station_id, event_date),
        )
    return {
        "schema": SCHEMA,
        "state": (
            "BACKFILLED_WITH_LEGACY_GAPS"
            if invalid_authority
            else "BACKFILLED"
        ),
        "envelope_count": len(envelopes),
        "materialized_count": materialized,
        "skipped_legacy_non_authoritative_count": len(skipped),
        "skipped_legacy_non_authoritative": skipped,
        "authoritative_scope_count": len(authoritative),
        "unbridgeable_authoritative_scope_count": len(
            invalid_authority
        ),
        "unbridgeable_authoritative_scopes": [
            {
                "station_id": station_id,
                "event_date": event_date,
                "checkpoint_id": checkpoint_id,
                "state": "FAIL_CLOSED_NO_LATEST_POINTER",
            }
            for station_id, event_date, checkpoint_id
            in invalid_authority
        ],
        "places_orders": False,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }


def backfill_probability_bridges(
    conn: sqlite3.Connection,
    *,
    max_envelopes: int = MAX_BACKFILL_ENVELOPES,
) -> dict[str, Any]:
    """Atomically install every bridgeable legacy projection or write none."""

    savepoint = f"probability_xsb_backfill_{id(conn):x}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        result = _backfill_probability_bridges(
            conn,
            max_envelopes=max_envelopes,
        )
        assert_connection_target_capacity(conn)
    except BaseException:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        raise
    conn.execute(f"RELEASE {savepoint}")
    return result


__all__ = [
    "DDL", "MAX_BUCKETS", "SCHEMA", "backfill_probability_bridges",
    "build_xsb_projection", "canonical_replay_probability_bridges",
    "install_probability_bridge_schema", "load_current_probability_bridge", "materialize_probability_bridge",
    "probability_bridge_schema_status",
    "verify_probability_bridge_replay_equivalence",
]
