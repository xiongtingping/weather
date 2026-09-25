"""Settlement labels for the unique DeltaBasketScore v2 forward facts."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime
from typing import Any, Mapping, Sequence

from .contracts import ContractError, canonical_json, content_sha256
from .resource_bounds import (
    assert_connection_target_capacity,
    assert_process_memory_budget,
)
from .market_registry import settlement_timezone_for_scope
from .canonical_contract import FORMAL_TRUTH_LANES_SQL, FORMAL_TRUTH_ROLES
from .settlement_time import settlement_day_window
from .settlement_evidence_packet import (
    SETTLEMENT_VERSION_BINDING_SCHEMA,
    TERMS_SCHEMA,
    build_attribution_terms,
    build_settlement_version_evidence_packet,
)


LABEL_SCHEMA = "delta_basket_score_training_label_v2"
LABEL_BATCH_SCHEMA = "delta_basket_score_training_label_batch_v2"
OUTCOME_SCHEMA = "delta_basket_score_no_live_outcome_v2"
FACT_AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_FORWARD"
MAX_LABEL_BATCH = 64
MAX_LABEL_OUTCOME_JSON_BYTES = 65_536
MAX_LABEL_SOURCE_JSON_BYTES = 262_144
# A label page is capped at 64 facts and each fact retains independent
# 256-KiB caps for policy and feature JSON.  The aggregate hard cap therefore
# remains bounded (at most 32 MiB under the current per-fact limits); 64 MiB
# gives a complete, valid page room to drain instead of deferring its tail at
# the former 2-MiB threshold.  RSS, target DB/WAL and free-space probes remain
# mandatory before the page is read or written.
MAX_LABEL_PAGE_SOURCE_JSON_BYTES = 64 * 1024 * 1024
MAX_LABEL_ENVELOPE_JSON_BYTES = 524_288
MAX_LABEL_INTENTS = 64
MAX_LABEL_INTENT_BUCKET_BYTES = 256
MAX_LABEL_INTENT_PAGE_BYTES = 16_384


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"delta_v2_{name}_missing")
    return result


def _timestamp(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"delta_v2_{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"delta_v2_{name}_timezone_required")
    return result


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(
        character not in "0123456789abcdef" for character in result
    ):
        raise ContractError(f"delta_v2_{name}_invalid")
    return result


def _mapping(name: str, value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError(f"delta_v2_{name}_json_invalid") from exc
        if isinstance(decoded, Mapping):
            return dict(decoded)
    raise ContractError(f"delta_v2_{name}_mapping_required")


def _one_row(
    conn: sqlite3.Connection,
    *,
    table: str,
    where: str,
    params: Sequence[Any],
    missing: str,
    ambiguous: str,
) -> dict[str, Any]:
    if table == "basket_probability_envelope":
        columns = {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(basket_probability_envelope)"
            ).fetchall()
        }
        json_columns = tuple(
            name
            for name in ("lineage_json", "probabilities_json")
            if name in columns
        )
        length_sql = ",".join(
            f"length(CAST({name} AS BLOB))" for name in json_columns
        )
        metadata = conn.execute(
            "SELECT rowid"
            + ("," + length_sql if length_sql else "")
            + f" FROM {table} WHERE {where} LIMIT 2",
            tuple(params),
        ).fetchall()
        if not metadata:
            raise ContractError(missing)
        if len(metadata) != 1:
            raise ContractError(ambiguous)
        projected = sum(int(value or 0) for value in metadata[0][1:])
        if any(
            int(value or 0) > MAX_LABEL_ENVELOPE_JSON_BYTES
            for value in metadata[0][1:]
        ):
            raise ContractError("delta_v2_label_envelope_json_too_large")
        assert_process_memory_budget(projected_bytes=projected * 3)
        cursor = conn.execute(
            f"SELECT * FROM {table} WHERE rowid=?",
            (int(metadata[0][0]),),
        )
    else:
        cursor = conn.execute(
            f"SELECT * FROM {table} WHERE {where} LIMIT 2",
            tuple(params),
        )
    rows = cursor.fetchall()
    if not rows:
        raise ContractError(missing)
    if len(rows) != 1:
        raise ContractError(ambiguous)
    columns = tuple(item[0] for item in (cursor.description or ()))
    row = rows[0]
    if isinstance(row, sqlite3.Row):
        return {key: row[key] for key in row.keys()}
    return {name: row[index] for index, name in enumerate(columns)}


def _registry_timezone(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
) -> str:
    # The registry's rows while it carries the scope, the station's bound
    # timezone once retention has retired them (``settlement_timezone_for_scope``).
    timezone_name = settlement_timezone_for_scope(
        conn, station_id=station_id, event_date=event_date
    )
    if not timezone_name:
        raise ContractError("delta_v2_market_registry_timezone_not_unique")
    settlement_day_window(event_date, timezone_name)
    return timezone_name


def _winning_bucket(
    *,
    official_tmax_c: float,
    bucket_intervals: Mapping[str, Any],
) -> str:
    matches: list[str] = []
    for bucket, raw in bucket_intervals.items():
        if (
            not isinstance(raw, Sequence)
            or isinstance(raw, (str, bytes, bytearray))
            or len(raw) != 2
        ):
            raise ContractError("delta_v2_bucket_interval_invalid")
        lower = None if raw[0] is None else float(raw[0])
        upper = None if raw[1] is None else float(raw[1])
        if (
            (lower is None or official_tmax_c >= lower)
            and (upper is None or official_tmax_c < upper)
        ):
            matches.append(str(bucket))
    if len(matches) != 1:
        raise ContractError("delta_v2_truth_bucket_mapping_invalid")
    return matches[0]


def _bucket_intervals_for_envelope(
    conn: sqlite3.Connection,
    *,
    envelope: Mapping[str, Any],
    lineage: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve exact bucket edges from this envelope or its bound P0 root."""

    raw_intervals = lineage.get("bucket_intervals_c_json")
    if raw_intervals is None:
        root_checkpoint_id = _text(
            "label_p0_root_checkpoint_id",
            lineage.get("p0_root_checkpoint_id"),
        )
        root = _one_row(
            conn,
            table="basket_probability_envelope",
            where=(
                "envelope_id=? AND station_id=? AND event_date=? "
                "AND settlement_timezone=? AND stage='P0' "
                "AND state='COMMITTED'"
            ),
            params=(
                root_checkpoint_id,
                envelope["station_id"],
                envelope["event_date"],
                envelope["settlement_timezone"],
            ),
            missing="delta_v2_label_p0_root_missing",
            ambiguous="delta_v2_label_p0_root_ambiguous",
        )
        root_lineage = _mapping(
            "label_p0_root_lineage",
            root["lineage_json"],
        )
        raw_intervals = root_lineage.get("bucket_intervals_c_json")
        current_probabilities = _mapping(
            "label_probability_envelope_probabilities",
            envelope["probabilities_json"],
        )
        root_probabilities = _mapping(
            "label_p0_root_probabilities",
            root["probabilities_json"],
        )
        if set(current_probabilities) != set(root_probabilities):
            raise ContractError(
                "delta_v2_label_p0_root_bucket_support_mismatch"
            )
    intervals = _mapping("bucket_intervals", raw_intervals)
    envelope_probabilities_json = envelope.get("probabilities_json")
    if envelope_probabilities_json is not None:
        envelope_probabilities = _mapping(
            "label_probability_envelope_probabilities",
            envelope_probabilities_json,
        )
        if set(intervals) != set(envelope_probabilities):
            raise ContractError(
                "delta_v2_label_bucket_interval_support_mismatch"
            )
    return intervals


def _label_candidate_page(
    conn: sqlite3.Connection,
    *,
    rowid_where: str,
    rowid_params: tuple[Any, ...],
    as_of: str,
    limit: int,
) -> list[tuple[Any, ...]]:
    """One page of settled ``d2fact_`` rows still owed a label, by fact rowid.

    ``rowid_where`` is a fixed fact-rowid predicate chosen by the caller
    (``f.rowid>?`` or ``f.rowid>? AND f.rowid<=?``), never external input.
    """

    # ``authority_scope`` is the fact row's thirty-fifth column, stored
    # after the two JSON documents, so testing it reads each row's
    # overflow pages.  Tested at the fact's own loop level -- which is
    # where a single-table term is evaluated -- that was every fact the
    # cursor range held: 37.6s for a page that found nothing on
    # 2026-09-14 (30,611 facts, every one already labelled), and the
    # retry lane wraps to the start of the table.  So the candidates are
    # found first on the row's leading columns, the truth join and the
    # label anti-join, and only those are read for their scope and sizes:
    # 4.9s for the same page, rows and order identical on the live table.
    return conn.execute(
        """
        WITH candidate AS MATERIALIZED (
          SELECT f.rowid AS fact_rowid, t.truth_id AS truth_id
          FROM basket_policy_training_fact f
          JOIN calibration_station_daily_high_truth t
            ON t.station_id=f.station_id AND t.local_date=f.event_date
          WHERE """
        + rowid_where
        + """
            AND f.fact_id LIKE 'd2fact_%'
            AND t.source_lane IN """ + FORMAL_TRUTH_LANES_SQL + """
            AND t.source_role IN (?,?)
            AND t.qc_status='ACCEPTED' AND t.timestamps_complete=1
            AND t.canonical_committed_at_utc<=?
            AND t.revision_no=(
              SELECT MAX(t2.revision_no)
              FROM calibration_station_daily_high_truth t2
              WHERE t2.station_id=t.station_id
                AND t2.local_date=t.local_date
                AND t2.source_lane IN """ + FORMAL_TRUTH_LANES_SQL + """
                AND t2.source_role IN (?,?)
                AND t2.canonical_committed_at_utc<=?
            )
            AND NOT EXISTS(
              SELECT 1 FROM basket_policy_training_label l
              WHERE l.fact_id=f.fact_id AND l.truth_id=t.truth_id
            )
        )
        SELECT f.rowid,f.fact_id,f.station_id,f.event_date,
               f.settlement_timezone,
               f.probability_envelope_id,
               length(CAST(f.policy_version_vector_json AS BLOB)),
               length(CAST(f.feature_json AS BLOB)),
               t.truth_id,t.revision_no,t.official_tmax_c,
               t.canonical_committed_at_utc,t.settlement_definition,
               t.authority_scope,t.content_sha256
        FROM candidate c
        JOIN basket_policy_training_fact f ON f.rowid=c.fact_rowid
        JOIN calibration_station_daily_high_truth t
          ON t.truth_id=c.truth_id
        WHERE f.authority_scope=?
        ORDER BY f.rowid
        LIMIT ?
        """,
        (
            *rowid_params,
            # Charter 225: the newest formal revision, in either lane.
            *FORMAL_TRUTH_ROLES,
            as_of,
            *FORMAL_TRUTH_ROLES,
            as_of,
            FACT_AUTHORITY_SCOPE,
            int(limit),
        ),
    ).fetchall()


def materialize_delta_v2_settled_training_labels(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str,
    after_fact_rowid: int = 0,
    cursor_state: Mapping[str, Any] | None = None,
    max_facts: int = MAX_LABEL_BATCH,
) -> dict[str, Any]:
    """Append bounded labels only for content-addressed ``d2fact_`` rows."""

    as_of = _timestamp("label_as_of_utc", as_of_utc)
    if isinstance(after_fact_rowid, bool) or int(after_fact_rowid) < 0:
        raise ContractError("delta_v2_label_cursor_invalid")
    cursor = int(after_fact_rowid)
    retry_after = 0
    next_lane = "retry"
    if cursor_state is not None:
        if (
            not isinstance(cursor_state, Mapping)
            or cursor_state.get("schema") != "delta_v2_label_cursor_v2"
        ):
            raise ContractError("delta_v2_label_cursor_state_invalid")
        try:
            state_cursor = int(cursor_state.get("scan_after_rowid"))
            retry_after = int(cursor_state.get("retry_after_rowid"))
        except (TypeError, ValueError) as exc:
            raise ContractError("delta_v2_label_cursor_state_invalid") from exc
        if (
            state_cursor < 0
            or retry_after < 0
            or retry_after > state_cursor
            or state_cursor != cursor
        ):
            raise ContractError("delta_v2_label_cursor_state_drift")
        next_lane = str(cursor_state.get("next_lane") or "")
        if next_lane not in {"retry", "new"}:
            raise ContractError("delta_v2_label_cursor_next_lane_invalid")
    if not 1 <= int(max_facts) <= MAX_LABEL_BATCH:
        raise ContractError("delta_v2_label_batch_limit_invalid")
    fact_limit = int(max_facts)
    # File-backed target callers must re-check the DB/WAL/free-space bound at
    # the exact label-ingress boundary.  ``:memory:`` focused tests have no
    # backing path and are intentionally a no-op in this shared guard.
    assert_connection_target_capacity(conn)
    assert_process_memory_budget()

    def _metadata_page(
        *,
        rowid_where: str,
        rowid_cursor: int,
        limit: int,
        extra_rowid_cursor: int | None = None,
    ) -> list[tuple[Any, ...]]:
        if limit <= 0:
            return []
        rowid_params: tuple[Any, ...] = (
            (rowid_cursor, extra_rowid_cursor)
            if extra_rowid_cursor is not None
            else (rowid_cursor,)
        )
        return _label_candidate_page(
            conn,
            rowid_where=rowid_where,
            rowid_params=rowid_params,
            as_of=as_of,
            limit=limit,
        )

    # A high-water cursor alone permanently loses an older open or malformed
    # fact.  The retry cursor rotates through unresolved low rows instead of
    # repeatedly selecting the same prefix.  With a one-row page, ``next_lane``
    # alternates retry/new so neither lane can starve.
    if cursor == 0:
        retry_capacity, new_capacity = 0, fact_limit
    elif fact_limit == 1:
        retry_capacity = 1 if next_lane == "retry" else 0
        new_capacity = 1 - retry_capacity
    else:
        retry_capacity = max(1, fact_limit // 2)
        new_capacity = fact_limit - retry_capacity

    def _retry_page(limit: int) -> tuple[list[tuple[Any, ...]], bool]:
        if limit <= 0 or cursor <= 0:
            return [], False
        rows = _metadata_page(
            rowid_where="f.rowid>? AND f.rowid<=?",
            rowid_cursor=retry_after,
            limit=limit + 1,
            extra_rowid_cursor=cursor,
        )
        wrapped_retry = False
        if not rows and retry_after > 0:
            rows = _metadata_page(
                rowid_where="f.rowid>? AND f.rowid<=?",
                rowid_cursor=0,
                limit=limit + 1,
                extra_rowid_cursor=cursor,
            )
            wrapped_retry = True
        return rows, wrapped_retry

    retry_candidates, retry_wrapped = _retry_page(retry_capacity)
    retry_has_more = len(retry_candidates) > retry_capacity
    retry_candidates = retry_candidates[:retry_capacity]
    if fact_limit > 1:
        new_capacity += retry_capacity - len(retry_candidates)
    new_candidates = _metadata_page(
        rowid_where="f.rowid>?",
        rowid_cursor=cursor,
        limit=new_capacity + 1,
    )
    new_has_more = len(new_candidates) > new_capacity
    new_candidates = new_candidates[:new_capacity]
    if fact_limit == 1 and not retry_candidates and not new_candidates:
        if next_lane == "retry":
            new_candidates = _metadata_page(
                rowid_where="f.rowid>?",
                rowid_cursor=cursor,
                limit=2,
            )
            new_has_more = len(new_candidates) > 1
            new_candidates = new_candidates[:1]
        else:
            retry_candidates, retry_wrapped = _retry_page(1)
            retry_has_more = len(retry_candidates) > 1
            retry_candidates = retry_candidates[:1]
    has_more = new_has_more or retry_has_more
    candidates = retry_candidates + new_candidates
    wrapped = False
    if not candidates and cursor > 0:
        wrapped = True

    source_cursor_after = max(
        [cursor]
        + [int(item[0]) for item in new_candidates],
    )
    retry_cursor_after = (
        max(int(item[0]) for item in retry_candidates)
        if retry_candidates
        else (0 if retry_wrapped else retry_after)
    )
    cursor_state_after = {
        "schema": "delta_v2_label_cursor_v2",
        "scan_after_rowid": source_cursor_after,
        "retry_after_rowid": min(
            retry_cursor_after,
            source_cursor_after,
        ),
        "next_lane": (
            "new" if fact_limit == 1 and next_lane == "retry"
            else "retry" if fact_limit == 1
            else next_lane
        ),
    }

    scanned_candidate_count = len(candidates)
    source_payload_bytes = 0
    resource_blockers: list[dict[str, str]] = []
    admitted_candidates: list[tuple[Any, ...]] = []
    for item in candidates:
        policy_bytes = int(item[6] or 0)
        feature_bytes = int(item[7] or 0)
        if (
            policy_bytes > MAX_LABEL_SOURCE_JSON_BYTES
            or feature_bytes > MAX_LABEL_SOURCE_JSON_BYTES
        ):
            resource_blockers.append({
                "fact_id": str(item[1] or "unknown_fact"),
                "reason": (
                    "ContractError:delta_v2_label_source_json_too_large"
                ),
            })
            continue
        row_bytes = policy_bytes + feature_bytes
        if (
            source_payload_bytes + row_bytes
            > MAX_LABEL_PAGE_SOURCE_JSON_BYTES
        ):
            resource_blockers.append({
                "fact_id": str(item[1] or "unknown_fact"),
                "reason": (
                    "ContractError:"
                    "delta_v2_label_page_source_json_budget_deferred"
                ),
            })
            continue
        source_payload_bytes += row_bytes
        admitted_candidates.append(item)
    candidates = admitted_candidates
    assert_process_memory_budget(
        projected_bytes=min(
            source_payload_bytes * 3,
            MAX_LABEL_PAGE_SOURCE_JSON_BYTES * 3,
        )
    )

    appended = 0
    skipped_open = 0
    blockers: list[dict[str, str]] = list(resource_blockers)
    for row in candidates:
        row_payload_bytes = int(row[6] or 0) + int(row[7] or 0)
        assert_process_memory_budget(
            projected_bytes=min(
                row_payload_bytes * 3,
                MAX_LABEL_SOURCE_JSON_BYTES * 6,
            )
        )
        try:
            (
                source_rowid,
                fact_id,
                station_id,
                event_date,
                settlement_timezone,
                envelope_id,
                _policy_bytes,
                _feature_bytes,
                truth_id,
                revision_no,
                official_tmax_c,
                committed_at,
                settlement_definition,
                authority_scope,
                truth_sha,
            ) = row
            blocked_fact_id = str(fact_id)
            payload = conn.execute(
                "SELECT policy_version_vector_json,feature_json,"
                "content_sha256,posterior_sha256,ws_snapshot_id,quote_sha256,"
                "inventory_snapshot_id,inventory_sha256,"
                "order_state_snapshot_id,order_state_sha256,"
                "cycle_id,decision_id,decision_sha256,plan_sha256 "
                "FROM basket_policy_training_fact WHERE rowid=?",
                (int(source_rowid),),
            ).fetchone()
            if payload is None:
                raise ContractError("delta_v2_label_source_fact_missing")
            (
                policy_json,
                feature_json,
                fact_sha,
                posterior_sha,
                ws_snapshot_id,
                quote_sha,
                inventory_snapshot_id,
                inventory_sha,
                order_state_snapshot_id,
                order_state_sha,
                cycle_id,
                decision_id,
                decision_sha,
                plan_sha,
            ) = payload
            policy_vector = _mapping("policy_vector", policy_json)
            if policy_vector.get("schema") != (
                "delta_basket_score_policy_version_vector_v2"
            ):
                raise ContractError("delta_v2_label_policy_vector_schema_invalid")
            if _mapping("feature_vector", feature_json).get("schema") != (
                "delta_basket_score_forward_feature_vector_v2"
            ):
                raise ContractError("delta_v2_label_feature_vector_schema_invalid")
            registry_timezone = _registry_timezone(
                conn,
                station_id=str(station_id),
                event_date=str(event_date),
            )
            if registry_timezone != str(settlement_timezone):
                raise ContractError("delta_v2_label_registry_timezone_mismatch")
            window = settlement_day_window(
                str(event_date), str(settlement_timezone)
            )
            closed_at = str(window["target_day_end_utc"])
            if as_of < closed_at:
                skipped_open += 1
                continue
            if str(committed_at or "") < closed_at:
                raise ContractError(
                    "delta_v2_truth_frozen_before_settlement_close"
                )
            envelope = _one_row(
                conn,
                table="basket_probability_envelope",
                where=(
                    "envelope_id=? AND station_id=? AND event_date=? "
                    "AND state='COMMITTED'"
                ),
                params=(envelope_id, station_id, event_date),
                missing="delta_v2_label_probability_envelope_missing",
                ambiguous="delta_v2_label_probability_envelope_ambiguous",
            )
            lineage = _mapping(
                "label_probability_lineage", envelope["lineage_json"]
            )
            if str(lineage.get("settlement_definition") or "") != str(
                settlement_definition or ""
            ):
                raise ContractError(
                    "delta_v2_label_settlement_definition_mismatch"
                )
            intervals = _bucket_intervals_for_envelope(
                conn,
                envelope=envelope,
                lineage=lineage,
            )
            value = float(official_tmax_c)
            if not math.isfinite(value):
                raise ContractError("delta_v2_truth_tmax_invalid")
            winner = _winning_bucket(
                official_tmax_c=value,
                bucket_intervals=intervals,
            )
            intent_metadata = conn.execute(
                "SELECT rowid,length(CAST(bucket AS BLOB)) "
                "FROM basket_target_order_intents WHERE cycle_id=("
                "SELECT cycle_id FROM basket_policy_training_fact WHERE fact_id=?"
                ") ORDER BY order_intent_id",
                (fact_id,),
            ).fetchmany(MAX_LABEL_INTENTS + 1)
            if len(intent_metadata) > MAX_LABEL_INTENTS:
                raise ContractError("delta_v2_label_intent_count_exceeded")
            intent_bytes = sum(int(item[1] or 0) for item in intent_metadata)
            if (
                any(
                    int(item[1] or 0) > MAX_LABEL_INTENT_BUCKET_BYTES
                    for item in intent_metadata
                )
                or intent_bytes > MAX_LABEL_INTENT_PAGE_BYTES
            ):
                raise ContractError("delta_v2_label_intent_bytes_exceeded")
            assert_process_memory_budget(projected_bytes=intent_bytes * 3)
            intents = [
                conn.execute(
                    "SELECT bucket,requested_size,requested_notional_usdc "
                    "FROM basket_target_order_intents WHERE rowid=?",
                    (int(item[0]),),
                ).fetchone()
                for item in intent_metadata
            ]
            requested_cost = sum(float(item[2]) for item in intents)
            winning_shares = sum(
                float(item[1]) for item in intents if str(item[0]) == winner
            )
            policy_vector_sha = content_sha256(policy_vector)
            settlement_binding_core = {
                "schema": SETTLEMENT_VERSION_BINDING_SCHEMA,
                "fact_id": str(fact_id),
                "fact_content_sha256": _sha(
                    "fact_content_sha256", fact_sha
                ),
                "station_id": str(station_id),
                "event_date": str(event_date),
                "settlement_timezone": str(settlement_timezone),
                "settlement_closed_at_utc": closed_at,
                "truth_id": _text("truth_id", truth_id),
                "truth_revision_no": int(revision_no),
                "truth_sha256": _sha("truth_sha256", truth_sha),
                "probability_envelope_id": str(envelope_id),
                "posterior_sha256": _sha(
                    "posterior_sha256", posterior_sha
                ),
                "ws_snapshot_id": _text("ws_snapshot_id", ws_snapshot_id),
                "quote_sha256": _sha("quote_sha256", quote_sha),
                "inventory_snapshot_id": _text(
                    "inventory_snapshot_id", inventory_snapshot_id
                ),
                "inventory_sha256": _sha(
                    "inventory_sha256", inventory_sha
                ),
                "order_state_snapshot_id": _text(
                    "order_state_snapshot_id", order_state_snapshot_id
                ),
                "order_state_sha256": _sha(
                    "order_state_sha256", order_state_sha
                ),
                "cycle_id": _text("cycle_id", cycle_id),
                "decision_id": _text("decision_id", decision_id),
                "decision_sha256": _sha(
                    "decision_sha256", decision_sha
                ),
                "plan_sha256": _sha("plan_sha256", plan_sha),
                "policy_version_vector_sha256": policy_vector_sha,
            }
            settlement_binding = {
                **settlement_binding_core,
                "content_sha256": content_sha256(
                    settlement_binding_core
                ),
            }
            attribution_terms = build_attribution_terms(
                fact_id=str(fact_id),
                fact_content_sha256=str(fact_sha),
                truth_sha256=str(truth_sha),
                winning_bucket=winner,
                policy_version_vector_sha256=policy_vector_sha,
                coverage_usdc=winning_shares,
                execution_cost_usdc=requested_cost,
            )
            hypothetical_pnl = float(
                attribution_terms["counterfactual_pnl_usdc"]
            )
            evidence_packet = build_settlement_version_evidence_packet(
                settlement_version_binding=settlement_binding,
                attribution_terms=attribution_terms,
            )
            outcome_json = canonical_json(
                {
                    "schema": OUTCOME_SCHEMA,
                    "label_semantics": (
                        "hypothetical_requested_intents_not_fills"
                    ),
                    "winning_bucket": winner,
                    "requested_cost_usdc": requested_cost,
                    "winning_requested_shares": winning_shares,
                    "hypothetical_requested_pnl_usdc": (
                        hypothetical_pnl
                    ),
                    "settlement_version_binding": settlement_binding,
                    "settled_attribution": attribution_terms,
                    "settlement_evidence_packet": evidence_packet,
                    "settlement_evidence_packet_sha256": evidence_packet[
                        "content_sha256"
                    ],
                    "orders_submitted": 0,
                    "calls_order_gateway": False,
                }
            )
            if (
                len(outcome_json.encode("utf-8"))
                > MAX_LABEL_OUTCOME_JSON_BYTES
            ):
                raise ContractError(
                    "delta_v2_label_outcome_json_too_large"
                )
            previous = conn.execute(
                "SELECT label_id FROM basket_policy_training_label "
                "WHERE fact_id=? ORDER BY truth_revision_no DESC,"
                "labeled_at_utc DESC,label_id DESC LIMIT 1",
                (fact_id,),
            ).fetchone()
            identity = {
                "fact_id": str(fact_id),
                "truth_id": _text("truth_id", truth_id),
                "truth_revision_no": int(revision_no),
                "truth_sha256": _sha("truth_sha256", truth_sha),
                "official_tmax_c": value,
                "winning_bucket": winner,
                "settlement_closed_at_utc": closed_at,
                "labeled_at_utc": as_of,
                "outcome_json": outcome_json,
                "authority_scope": _text(
                    "truth_authority_scope", authority_scope
                ),
                "supersedes_label_id": (
                    str(previous[0]) if previous is not None else None
                ),
            }
            label_sha = content_sha256({"schema": LABEL_SCHEMA, **identity})
            label_id = f"d2label_{label_sha[:40]}"
            conn.execute(
                "INSERT INTO basket_policy_training_label("
                "label_id,fact_id,truth_id,truth_revision_no,truth_sha256,"
                "official_tmax_c,winning_bucket,settlement_closed_at_utc,"
                "labeled_at_utc,outcome_json,authority_scope,"
                "supersedes_label_id,content_sha256"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    label_id,
                    identity["fact_id"],
                    identity["truth_id"],
                    identity["truth_revision_no"],
                    identity["truth_sha256"],
                    identity["official_tmax_c"],
                    identity["winning_bucket"],
                    identity["settlement_closed_at_utc"],
                    identity["labeled_at_utc"],
                    identity["outcome_json"],
                    identity["authority_scope"],
                    identity["supersedes_label_id"],
                    label_sha,
                ),
            )
            appended += 1
        except Exception as exc:
            blocked_fact_id = locals().get("fact_id")
            blockers.append(
                {
                    "fact_id": (
                        str(blocked_fact_id)
                        if blocked_fact_id is not None
                        else "unknown_fact"
                    ),
                    "reason": f"{type(exc).__name__}:{exc}",
                }
            )
            continue
    return {
        "schema": LABEL_BATCH_SCHEMA,
        "state": (
            "blocked_fail_closed" if blockers else
            ("committed" if appended else "no_new_final_labels")
        ),
        "candidate_count": scanned_candidate_count,
        "resource_blocker_count": len(resource_blockers),
        "retry_candidate_count": len(retry_candidates),
        "retry_has_more": retry_has_more,
        "new_candidate_count": len(new_candidates),
        "appended_count": appended,
        "blockers": blockers,
        "source_cursor_before": cursor,
        "source_cursor_after": source_cursor_after,
        "cursor_state_after": cursor_state_after,
        "cursor_wrapped": wrapped,
        "retry_cursor_wrapped": retry_wrapped,
        "has_more": has_more,
        "skipped_open_count": skipped_open,
        "source_payload_bytes": source_payload_bytes,
        "max_facts": int(max_facts),
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }


__all__ = [
    "FACT_AUTHORITY_SCOPE",
    "LABEL_BATCH_SCHEMA",
    "LABEL_SCHEMA",
    "OUTCOME_SCHEMA",
    "SETTLEMENT_VERSION_BINDING_SCHEMA",
    "TERMS_SCHEMA",
    "MAX_LABEL_PAGE_SOURCE_JSON_BYTES",
    "MAX_LABEL_SOURCE_JSON_BYTES",
    "materialize_delta_v2_settled_training_labels",
]
