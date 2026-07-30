"""Append-only authority for exact Maker price x timeout OOS grids.

The grid artifact contract is deliberately kept separate from the legacy
``maker_oos_*`` authority.  This module never reads those tables and never
reduces a price/queue point to a public activation scope.  The activation
scope is only station/season/lead/issuance/liquidity; every price, tick,
timeout, price-distance bin, and queue bin remains inside the immutable grid
body and is resolved exactly from that body.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
from typing import Any, Iterator, Mapping, Sequence

from .basket_kernel.contracts import ContractError, content_sha256
from .maker_grid_oos_artifact import (
    MAKER_GRID_OOS_ARTIFACT_SCHEMA,
    MakerGridOOSArtifact,
    SCOPE_FIELDS,
    grid_point_key_text,
    validate_maker_grid_oos_artifact,
)


MAKER_GRID_OOS_AUTHORITY_SCHEMA = "maker_grid_oos_authority_v1"
MAKER_GRID_OOS_MANIFEST_SCHEMA = "maker_grid_oos_activation_manifest_v1"
MAKER_GRID_OOS_POINTER_SCHEMA = "maker_grid_oos_activation_pointer_v1"
MAX_BODY_BYTES = 1_000_000
MAX_EVIDENCE_BYTES = 65_536
MAX_POINTER_BYTES = 65_536
MAX_RESOLUTION_ACTIVATIONS = 64
MAX_RESOLUTION_BUCKETS = 16
PUBLIC_SCOPE_FIELDS = SCOPE_FIELDS[:5]
_BODY_COLUMNS = (
    "artifact_id", "content_sha256", "schema", "artifact_json", "scope_json",
    "station_id", "season_bin", "lead_bucket", "issuance", "liquidity_bin",
    "as_of_utc", "grid_size", "curve_sha256", "stored_at_utc",
)
_MANIFEST_COLUMNS = (
    "activation_id", "artifact_id", "artifact_content_sha256",
    "promotion_decision_id", "manifest_sha256", "manifest_json",
    "station_id", "season_bin", "lead_bucket", "issuance", "liquidity_bin",
    "generation", "valid_from_utc", "valid_until_utc", "activated_at_utc",
    "pointer_sha256",
)


def _error(code: str) -> ContractError:
    return ContractError(f"maker_grid_oos_authority_{code}")


def _text(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise _error(f"{name}_invalid")
    result = value.strip()
    if not result:
        raise _error(f"{name}_missing")
    if len(result) > 512:
        raise _error(f"{name}_too_large")
    return result


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise _error(f"{name}_invalid")
    return result


def _timestamp(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _error(f"{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _error(f"{name}_timezone_required")
    return result


def _utc(name: str, value: Any) -> datetime:
    parsed = datetime.fromisoformat(_timestamp(name, value).replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


def _json(value: Any, *, name: str, max_bytes: int) -> str:
    try:
        result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise _error(f"{name}_invalid") from exc
    if len(result.encode("utf-8")) > max_bytes:
        raise _error(f"{name}_too_large")
    return result


def _named_row(row: Any, columns: Sequence[str]) -> Mapping[str, Any]:
    """Normalize sqlite default tuple rows and Row-factory rows alike."""

    if hasattr(row, "keys"):
        return row
    return dict(zip(columns, row))


def _public_scope(raw: Mapping[str, Any], *, name: str = "scope") -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise _error(f"{name}_mapping_required")
    if set(raw) - set(PUBLIC_SCOPE_FIELDS):
        raise _error(f"{name}_unknown_fields")
    missing = set(PUBLIC_SCOPE_FIELDS) - set(raw)
    if missing:
        raise _error(f"{name}_missing_fields")
    return {field: _text(f"{name}_{field}", raw[field]) for field in PUBLIC_SCOPE_FIELDS}


def _scope_from_artifact(artifact: Mapping[str, Any]) -> dict[str, str]:
    points = artifact.get("points")
    if not isinstance(points, Sequence) or isinstance(points, (str, bytes)) or not points:
        raise _error("artifact_points_invalid")
    first = points[0]
    if not isinstance(first, Mapping):
        raise _error("artifact_point_invalid")
    result = _public_scope({field: first.get(field) for field in PUBLIC_SCOPE_FIELDS})
    for point in points[1:]:
        if not isinstance(point, Mapping):
            raise _error("artifact_point_invalid")
        candidate = _public_scope({field: point.get(field) for field in PUBLIC_SCOPE_FIELDS})
        if candidate != result:
            raise _error("artifact_public_scope_inconsistent")
    return result


def _artifact(raw: Mapping[str, Any] | MakerGridOOSArtifact) -> tuple[MakerGridOOSArtifact, dict[str, Any], dict[str, str]]:
    try:
        value = validate_maker_grid_oos_artifact(raw)
    except ContractError as exc:
        raise _error(f"artifact_invalid:{exc}") from exc
    body = value.to_dict()
    scope = _scope_from_artifact(body)
    return value, body, scope


def _load_body(conn: sqlite3.Connection, artifact_id: str) -> tuple[MakerGridOOSArtifact, dict[str, Any], dict[str, str], sqlite3.Row]:
    row_raw = conn.execute(
        "SELECT " + ",".join(_BODY_COLUMNS) + " FROM maker_grid_oos_artifact_body WHERE artifact_id=?",
        (_text("artifact_id", artifact_id),),
    ).fetchone()
    if row_raw is None:
        raise _error("artifact_missing")
    row = _named_row(row_raw, _BODY_COLUMNS)
    try:
        raw = json.loads(str(row["artifact_json"]))
    except json.JSONDecodeError as exc:
        raise _error("artifact_body_invalid") from exc
    value, body, scope = _artifact(raw)
    if value.content_sha256 != str(row["content_sha256"]):
        raise _error("artifact_hash_mismatch")
    if str(row["artifact_id"]) != str(body["artifact_id"]):
        raise _error("artifact_hash_mismatch")
    if str(row["schema"]) != MAKER_GRID_OOS_ARTIFACT_SCHEMA:
        raise _error("artifact_schema_mismatch")
    if str(row["scope_json"]) != _json(scope, name="scope", max_bytes=4096):
        raise _error("artifact_scope_hash_mismatch")
    if str(row["as_of_utc"]) != str(body["as_of_utc"]):
        raise _error("artifact_as_of_mismatch")
    if int(row["grid_size"]) != int(body["grid_size"]):
        raise _error("artifact_grid_size_mismatch")
    if str(row["curve_sha256"]) != str(body["curve_sha256"]):
        raise _error("artifact_curve_hash_mismatch")
    return value, body, scope, row


@contextmanager
def _atomic(conn: sqlite3.Connection, label: str) -> Iterator[None]:
    """Make each multi-row append all-or-nothing without committing a caller transaction."""

    savepoint = f"{label}_{id(conn):x}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    else:
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")


def ensure_maker_grid_oos_authority_schema(
    conn: sqlite3.Connection, *, commit: bool = True
) -> dict[str, Any]:
    """Install only the independent grid authority schema."""

    from .ssot import apply_maker_grid_oos_authority_schema

    return apply_maker_grid_oos_authority_schema(conn, commit=commit)


def append_maker_grid_oos_artifact_body(
    conn: sqlite3.Connection,
    artifact: Mapping[str, Any] | MakerGridOOSArtifact,
    *,
    stored_at_utc: str,
) -> dict[str, Any]:
    """Append one complete immutable grid body; never fill a missing point."""

    value, body, scope = _artifact(artifact)
    body_json = _json(body, name="artifact_body", max_bytes=MAX_BODY_BYTES)
    stored_at = _timestamp("stored_at_utc", stored_at_utc)
    if _utc("stored_at_utc", stored_at) < _utc(
        "artifact_as_of_utc",
        body["as_of_utc"],
    ):
        raise _error("artifact_stored_before_as_of")
    with _atomic(conn, "maker_grid_oos_body"):
        conn.execute(
            """
            INSERT OR IGNORE INTO maker_grid_oos_artifact_body(
              artifact_id, content_sha256, schema, artifact_json, scope_json,
              station_id, season_bin, lead_bucket, issuance, liquidity_bin,
              as_of_utc, grid_size, curve_sha256, stored_at_utc
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(body["artifact_id"]), value.content_sha256,
                MAKER_GRID_OOS_ARTIFACT_SCHEMA, body_json,
                _json(scope, name="scope", max_bytes=4096),
                *(scope[field] for field in PUBLIC_SCOPE_FIELDS),
                str(body["as_of_utc"]), int(body["grid_size"]),
                str(body["curve_sha256"]), stored_at,
            ),
        )
        row = conn.execute(
            "SELECT artifact_json FROM maker_grid_oos_artifact_body WHERE artifact_id=?",
            (str(body["artifact_id"]),),
        ).fetchone()
        if row is None:
            duplicate = conn.execute(
                "SELECT artifact_id FROM maker_grid_oos_artifact_body WHERE content_sha256=?",
                (value.content_sha256,),
            ).fetchone()
            if duplicate is not None:
                raise _error("artifact_conflict")
            raise _error("artifact_not_persisted")
        if str(row[0]) != body_json:
            raise _error("artifact_conflict")
    return body


def append_maker_grid_oos_promotion_evidence(
    conn: sqlite3.Connection,
    *,
    artifact_id: str,
    evidence: Mapping[str, Any],
    stored_at_utc: str,
) -> dict[str, Any]:
    """Append governance evidence bound to the exact grid content hash."""

    _, body, _scope, _ = _load_body(conn, artifact_id)
    if not isinstance(evidence, Mapping) or len(evidence) > 64:
        raise _error("promotion_evidence_invalid")
    required = {
        "decision_id", "decision", "gate_version", "decided_at_utc",
        "receipt_sha256", "oos_metrics_sha256",
    }
    if not required <= set(evidence):
        raise _error("promotion_incomplete")
    normalized = dict(evidence)
    normalized["decision_id"] = _text("promotion_decision_id", normalized["decision_id"])
    normalized["decision"] = _text("promotion_decision", normalized["decision"]).upper()
    if normalized["decision"] not in {"PROMOTED", "REJECTED"}:
        raise _error("promotion_decision_invalid")
    normalized["gate_version"] = _text("promotion_gate_version", normalized["gate_version"])
    normalized["decided_at_utc"] = _timestamp("promotion_decided_at_utc", normalized["decided_at_utc"])
    normalized["receipt_sha256"] = _sha("promotion_receipt_sha256", normalized["receipt_sha256"])
    normalized["oos_metrics_sha256"] = _sha("promotion_oos_metrics_sha256", normalized["oos_metrics_sha256"])
    supplied_content = normalized.get("artifact_content_sha256", body["content_sha256"])
    if _sha("promotion_artifact_content_sha256", supplied_content) != str(body["content_sha256"]):
        raise _error("promotion_artifact_mismatch")
    normalized["artifact_content_sha256"] = str(body["content_sha256"])
    if "artifact_id" in normalized and _text("promotion_artifact_id", normalized["artifact_id"]) != str(body["artifact_id"]):
        raise _error("promotion_artifact_mismatch")
    if "as_of_utc" in normalized and _timestamp("promotion_as_of_utc", normalized["as_of_utc"]) != str(body["as_of_utc"]):
        raise _error("promotion_as_of_mismatch")
    evidence_json = _json(normalized, name="promotion_evidence", max_bytes=MAX_EVIDENCE_BYTES)
    evidence_sha = content_sha256(normalized)
    stored_at = _timestamp("stored_at_utc", stored_at_utc)
    if not _utc("artifact_as_of_utc", body["as_of_utc"]) <= _utc(
        "promotion_decided_at_utc",
        normalized["decided_at_utc"],
    ) <= _utc("stored_at_utc", stored_at):
        raise _error("promotion_as_of_order_invalid")
    with _atomic(conn, "maker_grid_oos_promotion"):
        conn.execute(
            """
            INSERT OR IGNORE INTO maker_grid_oos_promotion_evidence(
              promotion_id, artifact_id, artifact_content_sha256, decision,
              gate_version, decided_at_utc, receipt_sha256, oos_metrics_sha256,
              evidence_sha256, evidence_json, stored_at_utc
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                normalized["decision_id"], str(body["artifact_id"]),
                str(body["content_sha256"]), normalized["decision"],
                normalized["gate_version"], normalized["decided_at_utc"],
                normalized["receipt_sha256"], normalized["oos_metrics_sha256"],
                evidence_sha, evidence_json, stored_at,
            ),
        )
        row = conn.execute(
            "SELECT evidence_json FROM maker_grid_oos_promotion_evidence WHERE promotion_id=?",
            (normalized["decision_id"],),
        ).fetchone()
        if row is None:
            raise _error("promotion_not_persisted")
        if str(row[0]) != evidence_json:
            raise _error("promotion_conflict")
    return normalized


def _manifest_identity(
    *,
    activation_id: str,
    artifact_id: str,
    artifact_content_sha256: str,
    promotion_decision_id: str,
    scope: Mapping[str, str],
    generation: int,
    valid_from_utc: str,
    valid_until_utc: str,
    activated_at_utc: str,
    manifest_extra: Mapping[str, Any] | None,
) -> dict[str, Any]:
    extra = dict(manifest_extra or {})
    if len(extra) > 8 or set(extra) - {"reason", "authority_scope"}:
        raise _error("manifest_unknown_fields")
    return {
        "schema": MAKER_GRID_OOS_MANIFEST_SCHEMA,
        "activation_id": activation_id,
        "artifact_id": artifact_id,
        "artifact_content_sha256": artifact_content_sha256,
        "promotion_decision_id": promotion_decision_id,
        "scope": dict(scope),
        "generation": generation,
        "valid_from_utc": valid_from_utc,
        "valid_until_utc": valid_until_utc,
        "activated_at_utc": activated_at_utc,
        "pointer_sha256": None,
        **extra,
    }


def append_maker_grid_oos_activation_manifest(
    conn: sqlite3.Connection,
    *,
    activation_id: str,
    artifact_id: str,
    promotion_decision_id: str,
    scope: Mapping[str, Any],
    generation: int,
    valid_from_utc: str,
    valid_until_utc: str,
    activated_at_utc: str,
    pointer_sha256: str | None = None,
    manifest_extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a promoted activation and its content-addressed pointer."""

    activation = _text("activation_id", activation_id)
    artifact_id = _text("artifact_id", artifact_id)
    promotion_id = _text("promotion_decision_id", promotion_decision_id)
    public_scope = _public_scope(scope)
    value, body, artifact_scope, _ = _load_body(conn, artifact_id)
    if artifact_scope != public_scope:
        raise _error("activation_scope_mismatch")
    promotion = conn.execute(
        "SELECT artifact_id,artifact_content_sha256,decision,decided_at_utc FROM maker_grid_oos_promotion_evidence WHERE promotion_id=?",
        (promotion_id,),
    ).fetchall()
    if len(promotion) != 1:
        raise _error("promotion_missing")
    if str(promotion[0][0]) != artifact_id or str(promotion[0][1]) != value.content_sha256:
        raise _error("promotion_artifact_mismatch")
    if str(promotion[0][2]) != "PROMOTED":
        raise _error("promotion_not_promoted")
    valid_from = _timestamp("valid_from_utc", valid_from_utc)
    valid_until = _timestamp("valid_until_utc", valid_until_utc)
    if not _utc("valid_from_utc", valid_from) < _utc("valid_until_utc", valid_until):
        raise _error("validity_window_invalid")
    activated_at = _timestamp("activated_at_utc", activated_at_utc)
    artifact_as_of = _utc("artifact_as_of_utc", body["as_of_utc"])
    promotion_at = _utc("promotion_decided_at_utc", promotion[0][3])
    activation_at = _utc("activated_at_utc", activated_at)
    validity_start = _utc("valid_from_utc", valid_from)
    if not artifact_as_of <= promotion_at <= activation_at <= validity_start:
        raise _error("activation_as_of_order_invalid")
    try:
        generation_int = int(generation)
    except (TypeError, ValueError) as exc:
        raise _error("generation_invalid") from exc
    if isinstance(generation, bool) or generation_int < 1:
        raise _error("generation_invalid")
    manifest = _manifest_identity(
        activation_id=activation,
        artifact_id=artifact_id,
        artifact_content_sha256=value.content_sha256,
        promotion_decision_id=promotion_id,
        scope=public_scope,
        generation=generation_int,
        valid_from_utc=valid_from,
        valid_until_utc=valid_until,
        activated_at_utc=activated_at,
        manifest_extra=manifest_extra,
    )
    manifest["manifest_sha256"] = content_sha256(manifest)
    pointer_identity = {
        "schema": MAKER_GRID_OOS_POINTER_SCHEMA,
        "activation_id": activation,
        "artifact_id": artifact_id,
        "artifact_content_sha256": value.content_sha256,
        "manifest_sha256": manifest["manifest_sha256"],
        "scope": public_scope,
        "generation": generation_int,
    }
    expected_pointer = content_sha256(pointer_identity)
    pointer = expected_pointer if pointer_sha256 is None else _sha("pointer_sha256", pointer_sha256)
    if pointer != expected_pointer:
        raise _error("pointer_hash_mismatch")
    manifest["pointer_sha256"] = pointer
    manifest_json = _json(manifest, name="activation_manifest", max_bytes=MAX_EVIDENCE_BYTES)
    pointer_json = _json(pointer_identity, name="activation_pointer", max_bytes=MAX_POINTER_BYTES)
    scope_values = tuple(public_scope[field] for field in PUBLIC_SCOPE_FIELDS)

    with _atomic(conn, "maker_grid_oos_activation"):
        existing = conn.execute(
            "SELECT manifest_json FROM maker_grid_oos_activation_manifest WHERE activation_id=?",
            (activation,),
        ).fetchone()
        if existing is not None:
            if str(existing[0]) != manifest_json:
                raise _error("activation_conflict")
        else:
            max_row = conn.execute(
                """
                SELECT MAX(generation) FROM maker_grid_oos_activation_manifest
                WHERE station_id=? AND season_bin=? AND lead_bucket=? AND issuance=? AND liquidity_bin=?
                """,
                scope_values,
            ).fetchone()
            max_generation = int(max_row[0] or 0)
            if generation_int <= max_generation:
                raise _error("generation_not_unique_highest")
            conn.execute(
                """
                INSERT INTO maker_grid_oos_activation_manifest(
                  activation_id, artifact_id, artifact_content_sha256,
                  promotion_decision_id, manifest_sha256, manifest_json,
                  station_id, season_bin, lead_bucket, issuance, liquidity_bin,
                  generation, valid_from_utc, valid_until_utc, activated_at_utc,
                  pointer_sha256
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    activation, artifact_id, value.content_sha256, promotion_id,
                    manifest["manifest_sha256"], manifest_json, *scope_values,
                    generation_int, valid_from, valid_until, activated_at, pointer,
                ),
            )
        conn.execute(
            """
            INSERT OR IGNORE INTO maker_grid_oos_activation_pointer(
              pointer_sha256, activation_id, artifact_id, manifest_sha256,
              pointer_json, created_at_utc
            ) VALUES (?,?,?,?,?,?)
            """,
            (pointer, activation, artifact_id, manifest["manifest_sha256"], pointer_json, activated_at),
        )
        stored = conn.execute(
            "SELECT pointer_json FROM maker_grid_oos_activation_pointer WHERE pointer_sha256=?",
            (pointer,),
        ).fetchone()
        if stored is None or str(stored[0]) != pointer_json:
            raise _error("pointer_conflict")
    return manifest


def _verify_manifest_and_pointer(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    expected_scope: Mapping[str, str],
) -> dict[str, Any]:
    try:
        manifest = json.loads(str(row["manifest_json"]))
    except json.JSONDecodeError as exc:
        raise _error("manifest_invalid") from exc
    if not isinstance(manifest, Mapping):
        raise _error("manifest_invalid")
    identity = dict(manifest)
    supplied_manifest_sha = identity.pop("manifest_sha256", None)
    identity["pointer_sha256"] = None
    if supplied_manifest_sha != str(row["manifest_sha256"]) or content_sha256(identity) != str(row["manifest_sha256"]):
        raise _error("manifest_hash_mismatch")
    if dict(manifest.get("scope") or {}) != dict(expected_scope):
        raise _error("manifest_scope_mismatch")
    pointer_rows = conn.execute(
        "SELECT pointer_json,activation_id,artifact_id,manifest_sha256 FROM maker_grid_oos_activation_pointer WHERE activation_id=?",
        (str(row["activation_id"]),),
    ).fetchall()
    if len(pointer_rows) != 1:
        raise _error("pointer_ambiguous")
    pointer_row = pointer_rows[0]
    if str(pointer_row[1]) != str(row["activation_id"]) or str(pointer_row[2]) != str(row["artifact_id"]):
        raise _error("pointer_mismatch")
    if str(pointer_row[3]) != str(row["manifest_sha256"]):
        raise _error("pointer_manifest_mismatch")
    try:
        pointer_object = json.loads(str(pointer_row[0]))
    except json.JSONDecodeError as exc:
        raise _error("pointer_invalid") from exc
    if not isinstance(pointer_object, Mapping) or content_sha256(pointer_object) != str(row["pointer_sha256"]):
        raise _error("pointer_hash_mismatch")
    expected_pointer = dict(pointer_object)
    if expected_pointer.get("activation_id") != str(row["activation_id"]):
        raise _error("pointer_mismatch")
    if expected_pointer.get("artifact_id") != str(row["artifact_id"]):
        raise _error("pointer_mismatch")
    if expected_pointer.get("artifact_content_sha256") != str(row["artifact_content_sha256"]):
        raise _error("pointer_artifact_hash_mismatch")
    if expected_pointer.get("manifest_sha256") != str(row["manifest_sha256"]):
        raise _error("pointer_manifest_mismatch")
    if dict(expected_pointer.get("scope") or {}) != dict(expected_scope):
        raise _error("pointer_scope_mismatch")
    if int(expected_pointer.get("generation", 0)) != int(row["generation"]):
        raise _error("pointer_generation_mismatch")
    return dict(manifest)


def _load_usable_candidate(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    expected_scope: Mapping[str, str],
    as_of_utc: str,
) -> dict[str, Any]:
    manifest = _verify_manifest_and_pointer(conn, row, expected_scope=expected_scope)
    value, body, artifact_scope, _body_row = _load_body(conn, str(row["artifact_id"]))
    if artifact_scope != dict(expected_scope):
        raise _error("artifact_scope_mismatch")
    if str(row["artifact_content_sha256"]) != value.content_sha256:
        raise _error("activation_artifact_hash_mismatch")
    if str(row["artifact_id"]) != str(body["artifact_id"]):
        raise _error("activation_artifact_id_mismatch")
    promotion = conn.execute(
        "SELECT artifact_id,artifact_content_sha256,decision,evidence_sha256,evidence_json FROM maker_grid_oos_promotion_evidence WHERE promotion_id=?",
        (str(row["promotion_decision_id"]),),
    ).fetchall()
    if len(promotion) != 1:
        raise _error("promotion_missing")
    if str(promotion[0][0]) != str(row["artifact_id"]) or str(promotion[0][1]) != value.content_sha256:
        raise _error("promotion_artifact_mismatch")
    if str(promotion[0][2]) != "PROMOTED":
        raise _error("promotion_not_promoted")
    try:
        evidence = json.loads(str(promotion[0][4]))
    except json.JSONDecodeError as exc:
        raise _error("promotion_evidence_invalid") from exc
    if not isinstance(evidence, Mapping) or content_sha256(evidence) != str(promotion[0][3]):
        raise _error("promotion_evidence_hash_mismatch")
    if evidence.get("artifact_content_sha256") != value.content_sha256:
        raise _error("promotion_evidence_hash_mismatch")
    # Re-check the query time with UTC datetimes; SQL text ordering is not safe
    # when callers use offsets instead of a literal Z suffix.
    at = _utc("as_of_utc", as_of_utc)
    if not _utc("valid_from_utc", row["valid_from_utc"]) <= at < _utc("valid_until_utc", row["valid_until_utc"]):
        raise _error("no_active_artifact")
    return {
        "artifact": body,
        "oos_artifact": body,
        "activation_manifest": manifest,
        "maker_grid_oos_context": {
            **dict(expected_scope),
            "as_of_utc": _timestamp("as_of_utc", as_of_utc),
            "artifact_id": value["artifact_id"],
            "artifact_content_sha256": value.content_sha256,
            "activation_id": str(row["activation_id"]),
            "activation_generation": int(row["generation"]),
            "promotion_decision_id": str(row["promotion_decision_id"]),
            "authority_schema": MAKER_GRID_OOS_AUTHORITY_SCHEMA,
            "grid_size": int(body["grid_size"]),
            "artifact_as_of_utc": str(body["as_of_utc"]),
        },
    }


def resolve_maker_grid_oos_artifact(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    season_bin: str,
    lead_bucket: str,
    issuance: str,
    liquidity_bin: str,
    as_of_utc: str,
) -> dict[str, Any]:
    """Resolve one active promoted grid using only the five public scope fields."""

    expected = _public_scope({
        "station_id": station_id,
        "season_bin": season_bin,
        "lead_bucket": lead_bucket,
        "issuance": issuance,
        "liquidity_bin": liquidity_bin,
    })
    at = _timestamp("as_of_utc", as_of_utc)
    cursor = conn.execute(
        """
        SELECT activation_id,artifact_id,artifact_content_sha256,
               promotion_decision_id,manifest_sha256,manifest_json,
               station_id,season_bin,lead_bucket,issuance,liquidity_bin,
               generation,valid_from_utc,valid_until_utc,activated_at_utc,
               pointer_sha256
        FROM maker_grid_oos_activation_manifest
        WHERE station_id=? AND season_bin=? AND lead_bucket=? AND issuance=? AND liquidity_bin=?
        ORDER BY generation DESC
        LIMIT ?
        """,
        (*(expected[field] for field in PUBLIC_SCOPE_FIELDS), MAX_RESOLUTION_ACTIVATIONS + 1),
    )
    rows = [_named_row(raw, _MANIFEST_COLUMNS) for raw in cursor.fetchall()]
    if len(rows) > MAX_RESOLUTION_ACTIVATIONS:
        raise _error("activation_row_limit")
    active = [
        row for row in rows
        if _utc("valid_from_utc", row["valid_from_utc"]) <= _utc("as_of_utc", at)
        < _utc("valid_until_utc", row["valid_until_utc"])
    ]
    if not active:
        raise _error("no_active_artifact")
    if len(active) != 1:
        raise _error("multiple_active_artifacts")
    return _load_usable_candidate(conn, active[0], expected_scope=expected, as_of_utc=at)


def resolve_maker_grid_oos_point(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    season_bin: str,
    lead_bucket: str,
    issuance: str,
    liquidity_bin: str,
    candidate_price: float,
    price_tick: float,
    timeout_seconds: float,
    price_distance_bin: str,
    queue_bin: str,
    as_of_utc: str,
) -> dict[str, Any]:
    """Select one exact point from the resolved grid; never interpolate."""

    result = resolve_maker_grid_oos_artifact(
        conn,
        station_id=station_id,
        season_bin=season_bin,
        lead_bucket=lead_bucket,
        issuance=issuance,
        liquidity_bin=liquidity_bin,
        as_of_utc=as_of_utc,
    )
    requested = {
        "candidate_price": candidate_price,
        "price_tick": price_tick,
        "timeout_seconds": timeout_seconds,
        "station_id": station_id,
        "season_bin": season_bin,
        "lead_bucket": lead_bucket,
        "issuance": issuance,
        "liquidity_bin": liquidity_bin,
        "price_distance_bin": price_distance_bin,
        "queue_bin": queue_bin,
    }
    # ``grid_point_key_text`` performs the same canonical numeric conversion
    # as the artifact validator (notably 10 and 10.0 must hash identically).
    key = grid_point_key_text(requested)
    matches = [point for point in result["artifact"]["points"] if point["grid_point_key"] == key]
    if len(matches) != 1:
        raise _error("exact_grid_point_missing")
    result["grid_point"] = matches[0]
    result["maker_grid_oos_context"] = {
        **result["maker_grid_oos_context"],
        "grid_point_key": matches[0]["grid_point_key"],
        "price_distance_bin": matches[0]["price_distance_bin"],
        "queue_bin": matches[0]["queue_bin"],
    }
    return result


def resolve_maker_grid_oos_for_buckets(
    conn: sqlite3.Connection,
    *,
    bucket_scopes: Mapping[str, Mapping[str, Any]],
    as_of_utc: str,
) -> dict[str, Any]:
    """Resolve one formal grid and bind each bucket's exact queue context.

    Price-distance is intentionally absent here because it changes with each
    candidate price.  The full artifact owns that mapping point-by-point.
    """

    if not isinstance(bucket_scopes, Mapping) or not bucket_scopes:
        raise _error("bucket_scopes_required")
    if len(bucket_scopes) > MAX_RESOLUTION_BUCKETS:
        raise _error("bucket_scope_limit")
    contexts: dict[str, dict[str, str]] = {}
    common_scope: dict[str, str] | None = None
    for raw_bucket, raw_scope in bucket_scopes.items():
        bucket = _text("bucket", raw_bucket)
        if not isinstance(raw_scope, Mapping):
            raise _error("bucket_scope_invalid")
        allowed = set(PUBLIC_SCOPE_FIELDS) | {"queue_bin"}
        if set(raw_scope) != allowed:
            raise _error("bucket_scope_fields_invalid")
        public = _public_scope({
            field: raw_scope[field]
            for field in PUBLIC_SCOPE_FIELDS
        })
        if common_scope is None:
            common_scope = public
        elif public != common_scope:
            raise _error("bucket_public_scope_mismatch")
        contexts[bucket] = {
            **public,
            "queue_bin": _text("bucket_queue_bin", raw_scope["queue_bin"]),
        }
    assert common_scope is not None
    resolved = resolve_maker_grid_oos_artifact(
        conn,
        **common_scope,
        as_of_utc=as_of_utc,
    )
    artifact = resolved["artifact"]
    available_queue_bins = {
        str(point["queue_bin"])
        for point in artifact["points"]
    }
    for context in contexts.values():
        if context["queue_bin"] not in available_queue_bins:
            raise _error("bucket_queue_scope_missing")
    models = {
        bucket: {"grid_oos_artifact": artifact}
        for bucket in contexts
    }
    return {
        "artifact": artifact,
        "artifacts_by_bucket": {
            bucket: artifact
            for bucket in contexts
        },
        "maker_grid_oos_artifacts_by_bucket": {
            bucket: artifact
            for bucket in contexts
        },
        "maker_grid_oos_models": models,
        "maker_grid_oos_context": {
            "as_of_utc": _timestamp("as_of_utc", as_of_utc),
            "scope_by_bucket": contexts,
            "authority_schema": MAKER_GRID_OOS_AUTHORITY_SCHEMA,
            "resolution_count": len(contexts),
            **common_scope,
        },
    }


__all__ = [
    "MAKER_GRID_OOS_AUTHORITY_SCHEMA",
    "MAKER_GRID_OOS_MANIFEST_SCHEMA",
    "MAKER_GRID_OOS_POINTER_SCHEMA",
    "MAX_BODY_BYTES",
    "MAX_EVIDENCE_BYTES",
    "MAX_RESOLUTION_ACTIVATIONS",
    "MAX_RESOLUTION_BUCKETS",
    "PUBLIC_SCOPE_FIELDS",
    "append_maker_grid_oos_activation_manifest",
    "append_maker_grid_oos_artifact_body",
    "append_maker_grid_oos_promotion_evidence",
    "ensure_maker_grid_oos_authority_schema",
    "resolve_maker_grid_oos_artifact",
    "resolve_maker_grid_oos_for_buckets",
    "resolve_maker_grid_oos_point",
]
