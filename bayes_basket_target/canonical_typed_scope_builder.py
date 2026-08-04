"""Read-only source-to-canonical typed-scope envelope builders.

Each mapping is explicit and one-way.  A missing producer or source field
produces a blocked receipt; it is never renamed, guessed, or defaulted into a
canonical natural key.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping

from .canonical_contract import (
    CanonicalTypedScopeEnvelope,
    TYPED_SCOPE_ENVELOPE_SCHEMA,
    TYPED_SCOPE_TYPES,
)
from .contracts import ContractError, content_sha256


RECEIPT_SCHEMA = "bayes_basket_target_canonical_typed_scope_build_receipt_v1"
SUPPORTED_SOURCE_SCOPES = {
    "TRUTH_DAY",
    "TAF_P0M",
    "TRAJECTORY_P1",
    "MARKET_WS",
}
MISSING_PRODUCERS = {
    "STRATEGY_UTILITY": (
        "candidate_inventory_sha256",
        "baseline_inventory_sha256",
    ),
    "RUNTIME": ("runtime_release_id",),
}


class CanonicalTypedScopeBuildError(RuntimeError):
    """Fail-closed source mapping error."""


def _utc_z(value: object, label: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CanonicalTypedScopeBuildError(f"{label}_invalid") from exc
    if parsed.tzinfo is None:
        raise CanonicalTypedScopeBuildError(f"{label}_invalid")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _sha(value: object, label: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise CanonicalTypedScopeBuildError(f"{label}_invalid")
    return text


def _json_mapping(value: object, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as exc:
        raise CanonicalTypedScopeBuildError(f"{label}_invalid") from exc
    if not isinstance(parsed, dict):
        raise CanonicalTypedScopeBuildError(f"{label}_mapping_required")
    return parsed


def _json_list(value: object, label: str) -> list[Any]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as exc:
        raise CanonicalTypedScopeBuildError(f"{label}_invalid") from exc
    if not isinstance(parsed, list):
        raise CanonicalTypedScopeBuildError(f"{label}_list_required")
    return parsed


def _fact(
    *,
    scope_type: str,
    scope_kind: str,
    natural_key: Mapping[str, Any],
    available_at_utc: object,
    evidence_content_sha256: object,
) -> dict[str, Any]:
    identity = {
        "scope_type": scope_type,
        "scope_kind": scope_kind,
        "natural_key": dict(natural_key),
        "as_of_utc": _utc_z(available_at_utc, "typed_scope_available_at_utc"),
        "state": "CANDIDATE_ONLY",
        "blockers": ["ACTIVATION_ROOT_MISSING"],
        "evidence_content_sha256": _sha(
            evidence_content_sha256, "typed_scope_evidence_content_sha256"
        ),
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _truth_records(conn: sqlite3.Connection, cutoff: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    query = """
        SELECT fact_id,
               json_extract(fact_json,'$.station_id'),
               json_extract(fact_json,'$.event_date'),
               json_extract(fact_json,'$.truth_revision_no'),
               json_extract(fact_json,'$.canonical_committed_at_utc'),
               content_sha256
        FROM canonical_ssot_fact
        WHERE fact_type='truth'
          AND ingested_at_utc<=?
          AND json_extract(fact_json,'$.source_lane')='AWC'
          AND json_extract(fact_json,'$.source_role')='AWC_HISTORICAL_REPLAY_INGRESS'
          AND json_extract(fact_json,'$.authority_scope')='PRODUCTION_NO_LIVE_ONLY'
          AND json_extract(fact_json,'$.closed_day')=1
          AND json_extract(fact_json,'$.canonical_committed_at_utc')<=?
        ORDER BY json_extract(fact_json,'$.station_id'),
                 json_extract(fact_json,'$.event_date'),
                 json_extract(fact_json,'$.truth_revision_no'),fact_id
    """
    for row in conn.execute(query, (cutoff, cutoff)):
        evidence = _sha(row[5], "truth_day_source_content_sha256")
        records.append(
            _fact(
                scope_type="TRUTH_DAY",
                scope_kind="REVISION",
                natural_key={
                    "station_id": str(row[1]),
                    "event_date": str(row[2]),
                    "truth_revision_no": int(row[3]),
                },
                available_at_utc=row[4],
                evidence_content_sha256=evidence,
            )
        )
        sources.append({"source_id": str(row[0]), "content_sha256": evidence})
    return records, sources


def _probability_records(
    conn: sqlite3.Connection,
    cutoff: str,
    *,
    stage: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    lineage_filter = (
        " AND json_extract(lineage_json,'$.source_lane')='AWC'"
        if stage == "P1"
        else ""
    )
    rows = conn.execute(
        f"""
        WITH ranked AS (
          SELECT envelope_id,station_id,event_date,decision_at_utc,
                 posterior_sha256,lineage_json,content_sha256,
                 ROW_NUMBER() OVER (
                   PARTITION BY station_id,event_date
                   ORDER BY decision_at_utc DESC,content_sha256 DESC
                 ) AS rank_no
          FROM basket_probability_envelope
          WHERE stage=? AND state='COMMITTED'
            AND decision_at_utc<=? AND created_at<=?
            {lineage_filter}
        )
        SELECT envelope_id,station_id,event_date,decision_at_utc,
               posterior_sha256,lineage_json,content_sha256
        FROM ranked WHERE rank_no=1
        ORDER BY station_id,event_date,decision_at_utc,envelope_id
        """,
        (stage, cutoff, cutoff),
    )
    records: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for row in rows:
        lineage = _json_mapping(row[5], f"{stage.lower()}_lineage_json")
        evidence = _sha(row[6], f"{stage.lower()}_source_content_sha256")
        if stage == "P0-M":
            p0_root = _sha(
                lineage.get("p0_root_content_sha256"),
                "p0m_p0_root_sha256",
            )
            taf_revision = str(lineage.get("forecast_revision_id") or "").strip()
            if not taf_revision:
                raise CanonicalTypedScopeBuildError("p0m_taf_revision_id_missing")
            scope_type = "TAF_P0M"
            natural_key = {
                "station_id": str(row[1]),
                "event_date": str(row[2]),
                "decision_at_utc": _utc_z(row[3], "p0m_decision_at_utc"),
                "p0_root_sha256": p0_root,
                "taf_revision_id": taf_revision,
            }
        else:
            if str(lineage.get("source_lane") or "") != "AWC":
                raise CanonicalTypedScopeBuildError("p1_source_lane_not_awc")
            observation_id = str(
                lineage.get("canonical_observation_event_id") or ""
            ).strip()
            if not observation_id:
                raise CanonicalTypedScopeBuildError("p1_observation_id_missing")
            scope_type = "TRAJECTORY_P1"
            natural_key = {
                "station_id": str(row[1]),
                "event_date": str(row[2]),
                "decision_at_utc": _utc_z(row[3], "p1_decision_at_utc"),
                "probability_root_sha256": _sha(
                    row[4], "p1_probability_root_sha256"
                ),
                "observation_id": observation_id,
            }
        records.append(
            _fact(
                scope_type=scope_type,
                scope_kind="EVENT",
                natural_key=natural_key,
                available_at_utc=row[3],
                evidence_content_sha256=evidence,
            )
        )
        sources.append({"source_id": str(row[0]), "content_sha256": evidence})
    return records, sources


def _market_records(conn: sqlite3.Connection, cutoff: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = conn.execute(
        """
        WITH ranked AS (
          SELECT snapshot_id,market_id,station_id,event_date,sequence,
                 observed_at,buckets_json,content_sha256,
                 ROW_NUMBER() OVER (
                   PARTITION BY station_id,event_date,market_id
                   ORDER BY sequence DESC,observed_at DESC,content_sha256 DESC
                 ) AS rank_no
          FROM basket_official_ws_snapshot
          WHERE quote_source='OFFICIAL_CLOB_WS' AND state='COMMITTED'
            AND observed_at<=? AND created_at<=?
        )
        SELECT snapshot_id,market_id,station_id,event_date,sequence,
               observed_at,buckets_json,content_sha256
        FROM ranked WHERE rank_no=1
        ORDER BY station_id,event_date,market_id,sequence,snapshot_id
        """,
        (cutoff, cutoff),
    )
    records: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for row in rows:
        evidence = _sha(row[7], "market_ws_source_content_sha256")
        buckets = _json_list(row[6], "market_ws_buckets_json")
        token_ids: set[str] = set()
        for bucket in buckets:
            if not isinstance(bucket, Mapping):
                raise CanonicalTypedScopeBuildError(
                    "market_ws_bucket_mapping_required"
                )
            if str(bucket.get("quote_source") or "") != "OFFICIAL_CLOB_WS":
                raise CanonicalTypedScopeBuildError(
                    "market_ws_bucket_source_not_official_ws"
                )
            token_id = str(bucket.get("token_id") or "").strip()
            if not token_id or token_id in token_ids:
                raise CanonicalTypedScopeBuildError(
                    "market_ws_yes_token_id_invalid"
                )
            token_ids.add(token_id)
            records.append(
                _fact(
                    scope_type="MARKET_WS",
                    scope_kind="SNAPSHOT",
                    natural_key={
                        "station_id": str(row[2]),
                        "event_date": str(row[3]),
                        "market_id": str(row[1]),
                        "yes_token_id": token_id,
                        "ws_sequence": int(row[4]),
                        "snapshot_at_utc": _utc_z(
                            row[5], "market_ws_snapshot_at_utc"
                        ),
                    },
                    available_at_utc=row[5],
                    evidence_content_sha256=evidence,
                )
            )
        sources.append({"source_id": str(row[0]), "content_sha256": evidence})
    return records, sources


def _ordered_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        records,
        key=lambda item: (
            str(item["scope_kind"]),
            json.dumps(
                item["natural_key"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )


def build_canonical_typed_scope_envelope(
    db_path: Path | str,
    *,
    scope_type: str,
    as_of_utc: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Build one scope envelope or an explicit missing-producer receipt."""

    scope = str(scope_type).strip().upper()
    if scope not in TYPED_SCOPE_TYPES:
        raise CanonicalTypedScopeBuildError("typed_scope_builder_scope_invalid")
    cutoff = _utc_z(as_of_utc, "typed_scope_builder_as_of_utc")
    source = Path(db_path).expanduser().resolve(strict=True)
    if scope in MISSING_PRODUCERS:
        identity = {
            "schema": RECEIPT_SCHEMA,
            "scope_type": scope,
            "as_of_utc": cutoff,
            "state": "BLOCKED",
            "blockers": ["DATA_MISSING"],
            "missing_producer_fields": list(MISSING_PRODUCERS[scope]),
            "source_table": "",
            "source_row_count": 0,
            "record_count": 0,
            "source_selection_sha256": content_sha256([]),
            "envelope_content_sha256": "",
            "writes_database": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "production_no_live": True,
        }
        return None, {**identity, "content_sha256": content_sha256(identity)}

    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        if scope == "TRUTH_DAY":
            records, sources = _truth_records(conn, cutoff)
            table = "canonical_ssot_fact"
        elif scope == "TAF_P0M":
            records, sources = _probability_records(conn, cutoff, stage="P0-M")
            table = "basket_probability_envelope"
        elif scope == "TRAJECTORY_P1":
            records, sources = _probability_records(conn, cutoff, stage="P1")
            table = "basket_probability_envelope"
        elif scope == "MARKET_WS":
            records, sources = _market_records(conn, cutoff)
            table = "basket_official_ws_snapshot"
        else:  # pragma: no cover - guarded by the constant sets above
            raise CanonicalTypedScopeBuildError("typed_scope_builder_unreachable")
        data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
        schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        conn.rollback()
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.rollback()
        raise CanonicalTypedScopeBuildError(
            f"typed_scope_builder_source_query_failed:{scope}"
        ) from exc
    finally:
        conn.close()

    if not records:
        raise CanonicalTypedScopeBuildError(
            f"typed_scope_builder_source_rows_missing:{scope}"
        )
    ordered = _ordered_records(records)
    envelope_identity = {
        "schema": TYPED_SCOPE_ENVELOPE_SCHEMA,
        "as_of_utc": cutoff,
        "builder_id": f"canonical.{scope.lower()}.source_projection.v1",
        "scope_type": scope,
        "records": ordered,
        "production_no_live": True,
        "future_data_counts_as_oos": False,
    }
    envelope = {
        **envelope_identity,
        "content_sha256": content_sha256(envelope_identity),
    }
    try:
        validated = CanonicalTypedScopeEnvelope.from_mapping(envelope)
    except ContractError as exc:
        raise CanonicalTypedScopeBuildError(
            f"typed_scope_builder_contract_invalid:{scope}"
        ) from exc
    canonical_envelope = validated.to_mapping()
    source_selection = sorted(
        sources,
        key=lambda item: (item["source_id"], item["content_sha256"]),
    )
    receipt_identity = {
        "schema": RECEIPT_SCHEMA,
        "scope_type": scope,
        "as_of_utc": cutoff,
        "state": "CANDIDATE_ONLY",
        "blockers": ["ACTIVATION_ROOT_MISSING"],
        "missing_producer_fields": [],
        "source_table": table,
        "source_row_count": len(source_selection),
        "record_count": len(canonical_envelope["records"]),
        "source_selection_sha256": content_sha256(source_selection),
        "envelope_content_sha256": canonical_envelope["content_sha256"],
        "database": str(source),
        "database_data_version": data_version,
        "database_schema_version": schema_version,
        "writes_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "production_no_live": True,
    }
    return canonical_envelope, {
        **receipt_identity,
        "content_sha256": content_sha256(receipt_identity),
    }


__all__ = [
    "CanonicalTypedScopeBuildError",
    "RECEIPT_SCHEMA",
    "build_canonical_typed_scope_envelope",
]
