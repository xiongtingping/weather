#!/usr/bin/env python3
"""Freeze a bounded, read-only SSOT/ledger/worktree baseline.

This is the PR01 stale-evidence fence.  It deliberately does not repair a
ledger, project legacy rows, fetch providers, mutate SQLite, start a service,
or call an order gateway.  It records enough content and database identity to
tell the difference between a stale output pointer and missing source data.

The manifest is allowed to be ``BLOCKED``: that is the expected result when a
consumer pointer does not describe the exact canonical ledger on disk.  A
caller which needs a consumable baseline can pass ``--require-consistent`` and
receive a non-zero exit code instead of silently accepting the split SSOT.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.canonical_activation_release import (  # noqa: E402
    LEDGER_SCHEMA as GAP_LEDGER_SCHEMA_V2,
    ROOT_POINTER_SCHEMA,
    verify_current_root_pointer_v2,
    verify_ledger_v2,
)
from bayes_basket_target.gap_ledger import (  # noqa: E402
    BuilderScopeStatus,
    GapLedger,
    GapRecord,
    SCHEMA as GAP_LEDGER_SCHEMA,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    write_bounded_json_if_changed,
)


SCHEMA = "bayes_basket_target_ssot_baseline_manifest_v2"
INNER_OOS_SNAPSHOT_SCHEMA = "bayes_basket_target_oos_atomic_snapshot_manifest_v1"
P1_AUDIT_SCHEMA = "bayes_basket_target_p1_trajectory_45station_audit_v1"
P1_REPAIR_SCHEMA = "bayes_basket_target_p1_trajectory_repair_plan_v1"
MAX_READ_BYTES = min(int(MAX_JSON_INPUT_BYTES), 64 * 1024 * 1024)
MAX_WORKTREE_PATHS = 8_192
# The shared implementation worktree currently contains more than 65k
# independent hunks.  Keep the scan bounded, but leave enough headroom to
# capture the actual pre-existing ownership map instead of silently dropping
# it at the old limit.
MAX_WORKTREE_HUNKS = 131_072
MAX_RETRIES = 3
GIT_DIFF_TIMEOUT_SECONDS = 30
HUNK_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)


class BaselineError(RuntimeError):
    """The requested baseline cannot be trusted."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _utc_z(value: object, *, field: str) -> tuple[str, datetime]:
    """Parse a supplied evidence clock without guessing a timezone."""

    text = str(value or "").strip()
    if not text:
        raise BaselineError(f"{field}_missing")
    if not text.endswith("Z"):
        raise BaselineError(f"{field}_must_be_utc_z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise BaselineError(f"{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BaselineError(f"{field}_timezone_required")
    normalized = parsed.astimezone(timezone.utc)
    return (
        normalized.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        normalized,
    )


def _sha256_text(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if len(text) != 64:
        return None
    try:
        int(text, 16)
    except ValueError:
        return None
    return text


def _resolved_file(path: Path | str) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise BaselineError("baseline_symlink_path_forbidden")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise BaselineError("baseline_input_missing") from exc
    if not resolved.is_file():
        raise BaselineError("baseline_input_not_regular_file")
    return resolved


def _file_identity(path: Path) -> dict[str, int]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise BaselineError("baseline_input_stat_failed") from exc
    return {
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _read_stable_json(path: Path | str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read a bounded non-symlink JSON object and bind its bytes/stat identity."""

    source = _resolved_file(path)
    last_error: Exception | None = None
    for _ in range(MAX_RETRIES):
        try:
            before = _file_identity(source)
            if before["bytes"] > MAX_READ_BYTES:
                raise BaselineError("baseline_json_input_bytes_exceeded")
            raw = source.read_bytes()
            after = _file_identity(source)
            if before != after or len(raw) != before["bytes"]:
                raise BaselineError("baseline_json_input_changed_during_read")
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, Mapping):
                raise BaselineError("baseline_json_mapping_required")
            mapping = dict(value)
            supplied = str(mapping.get("content_sha256") or "").lower()
            identity = {key: value for key, value in mapping.items() if key != "content_sha256"}
            return mapping, {
                "path": str(source),
                "file_sha256": hashlib.sha256(raw).hexdigest(),
                "file": after,
                "declared_content_sha256": supplied or None,
                "declared_content_hash_valid": bool(supplied)
                and supplied == content_sha256(identity),
            }
        except (BaselineError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            last_error = exc
    raise BaselineError(str(last_error or "baseline_json_read_failed"))


def _exact_station_ids(value: object, *, field: str) -> tuple[str, ...] | None:
    if not isinstance(value, list) or len(value) != 45:
        return None
    stations = tuple(str(item).strip().upper() for item in value)
    if any(not station for station in stations) or len(set(stations)) != 45:
        return None
    if tuple(sorted(stations)) != stations:
        return None
    return stations


def _nested_content_hash_valid(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    supplied = _sha256_text(value.get("content_sha256"))
    if supplied is None:
        return False
    identity = {key: child for key, child in value.items() if key != "content_sha256"}
    return supplied == content_sha256(identity)


def _inner_oos_snapshot(
    value: Mapping[str, Any],
    meta: Mapping[str, Any],
    *,
    outer_as_of: datetime,
) -> tuple[dict[str, Any], list[str], datetime | None]:
    """Validate the already-built atomic P0/TAF evidence as one inner proof.

    The atomic-snapshot builder owns recomputing raw P0/TAF bindings.  PR01
    intentionally does not rebuild or reinterpret those facts; it accepts
    only an immutable, content-addressed result whose direct scopes and clocks
    are complete enough to be consumed by a baseline pointer.
    """

    blockers: list[str] = []
    if value.get("schema") != INNER_OOS_SNAPSHOT_SCHEMA:
        blockers.append("INNER_OOS_SNAPSHOT_SCHEMA_INVALID")
    if not bool(meta.get("declared_content_hash_valid")):
        blockers.append("INNER_OOS_SNAPSHOT_CONTENT_SHA256_INVALID")
    boundary_expectations = {
        "read_only": True,
        "production_no_live": True,
        "network_fetches": 0,
        "writes_target_database": False,
        "writes_production_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    for key, expected in boundary_expectations.items():
        if value.get(key) != expected:
            blockers.append(f"INNER_OOS_SNAPSHOT_{key.upper()}_INVALID")

    inner_as_of: datetime | None = None
    inner_as_of_text: str | None = None
    try:
        inner_as_of_text, inner_as_of = _utc_z(
            value.get("as_of_utc"), field="inner_oos_snapshot_as_of_utc"
        )
        if inner_as_of > outer_as_of:
            blockers.append("INNER_OOS_SNAPSHOT_AFTER_OUTER_AS_OF")
    except BaselineError as exc:
        blockers.append(str(exc).upper())

    canonical = _exact_station_ids(value.get("canonical_station_ids"), field="canonical_station_ids")
    if value.get("canonical_station_count") != 45 or canonical is None:
        blockers.append("INNER_OOS_SNAPSHOT_CANONICAL_SCOPE_INVALID")
    scopes = value.get("scope_station_ids")
    if not isinstance(scopes, Mapping) or canonical is None:
        blockers.append("INNER_OOS_SNAPSHOT_SCOPE_MAPPING_INVALID")
    else:
        for scope in ("p0_inbox", "taf_audit", "native_linkage", "native_repair"):
            if _exact_station_ids(scopes.get(scope), field=f"inner_{scope}_stations") != canonical:
                blockers.append(f"INNER_OOS_SNAPSHOT_SCOPE_MISMATCH:{scope}")

    inputs = value.get("input_artifacts")
    p0 = inputs.get("p0_inbox") if isinstance(inputs, Mapping) else None
    taf = inputs.get("taf_audit") if isinstance(inputs, Mapping) else None
    linkage = inputs.get("native_linkage") if isinstance(inputs, Mapping) else None
    repair = inputs.get("native_repair") if isinstance(inputs, Mapping) else None
    p0_sha = _sha256_text(p0.get("declared_content_sha256")) if isinstance(p0, Mapping) else None
    p0_revalidated = _sha256_text(p0.get("revalidated_content_sha256")) if isinstance(p0, Mapping) else None
    taf_p0_sha = _sha256_text(taf.get("p0_inbox_content_sha256")) if isinstance(taf, Mapping) else None
    if p0_sha is None or p0_revalidated is None or p0_sha != p0_revalidated:
        blockers.append("INNER_OOS_SNAPSHOT_P0_CONTENT_ADDRESS_INVALID")
    if taf_p0_sha is None or taf_p0_sha != p0_revalidated:
        blockers.append("INNER_OOS_SNAPSHOT_TAF_P0_BINDING_INVALID")

    p0_generated: datetime | None = None
    taf_p0_generated: datetime | None = None
    for payload, key, label in (
        (p0, "generated_at_utc", "inner_p0_generated_at_utc"),
        (taf, "p0_inbox_generated_at_utc", "inner_taf_p0_generated_at_utc"),
        (taf, "evidence_clock_utc", "inner_taf_evidence_clock_utc"),
    ):
        try:
            _, parsed = _utc_z(
                payload.get(key) if isinstance(payload, Mapping) else None,
                field=label,
            )
            if inner_as_of is not None and parsed > inner_as_of:
                blockers.append(f"{label.upper()}_AFTER_INNER_AS_OF")
            if label == "inner_p0_generated_at_utc":
                p0_generated = parsed
            elif label == "inner_taf_p0_generated_at_utc":
                taf_p0_generated = parsed
        except BaselineError as exc:
            blockers.append(str(exc).upper())
    if p0_generated is not None and taf_p0_generated is not None and p0_generated != taf_p0_generated:
        blockers.append("INNER_OOS_SNAPSHOT_TAF_P0_FROZEN_AT_MISMATCH")

    for payload, key, label in (
        (linkage, "as_of_utc", "inner_native_linkage_as_of_utc"),
        (repair, "as_of_utc", "inner_native_repair_as_of_utc"),
    ):
        try:
            _, parsed = _utc_z(
                payload.get(key) if isinstance(payload, Mapping) else None,
                field=label,
            )
            if inner_as_of is not None and parsed > inner_as_of:
                blockers.append(f"{label.upper()}_AFTER_INNER_AS_OF")
        except BaselineError as exc:
            blockers.append(str(exc).upper())

    binding = value.get("native_p0_binding")
    if not isinstance(binding, Mapping) or not _nested_content_hash_valid(binding):
        blockers.append("INNER_OOS_SNAPSHOT_NATIVE_BINDING_INVALID")
    else:
        binding_stations = _exact_station_ids(binding.get("station_ids"), field="inner_native_binding_stations")
        linkage_sha = _sha256_text(linkage.get("declared_content_sha256")) if isinstance(linkage, Mapping) else None
        repair_sha = _sha256_text(repair.get("declared_content_sha256")) if isinstance(repair, Mapping) else None
        if (
            binding.get("authority_scope") != "PRODUCTION_NO_LIVE_ONLY"
            or binding.get("binding_count") != 45
            or binding_stations != canonical
            or binding.get("source_linkage_content_sha256") != linkage_sha
            or binding.get("source_repair_content_sha256") != repair_sha
        ):
            blockers.append("INNER_OOS_SNAPSHOT_NATIVE_BINDING_MISMATCH")

    return (
        {
            **meta,
            "schema": value.get("schema"),
            "as_of_utc": inner_as_of_text,
            "canonical_station_count": value.get("canonical_station_count"),
            "p0_content_sha256": p0_revalidated,
            "taf_p0_content_sha256": taf_p0_sha,
        },
        sorted(set(blockers)),
        inner_as_of,
    )


def _canonical_gap_ledger(
    value: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str], datetime | None]:
    """Parse the one canonical gap ledger; do not trust a summary by itself."""

    blockers: list[str] = []
    parsed_as_of: datetime | None = None
    normalized_as_of: str | None = None
    if value.get("schema") == GAP_LEDGER_SCHEMA_V2:
        try:
            verified = verify_ledger_v2(value)
            normalized_as_of, parsed_as_of = _utc_z(
                verified.get("as_of_utc"), field="ledger_as_of_utc"
            )
            summary = verified.get("summary")
            statuses = verified.get("builder_scope_statuses")
            if isinstance(statuses, list) and any(
                isinstance(status, Mapping) and status.get("state") == "BLOCKED"
                for status in statuses
            ):
                blockers.append("LEDGER_BUILDER_SCOPE_BLOCKED")
            if not isinstance(summary, Mapping):
                blockers.append("LEDGER_SUMMARY_INVALID")
            return (
                {
                    "schema": verified.get("schema"),
                    "as_of_utc": normalized_as_of,
                    "record_count": len(verified.get("records") or []),
                    "builder_scope_status_count": len(statuses or []),
                    "builder_scope_blocked_count": (
                        summary.get("builder_blocked_count")
                        if isinstance(summary, Mapping) else None
                    ),
                    "canonical_projection_content_sha256": verified.get(
                        "canonical_projection_content_sha256"
                    ),
                },
                sorted(set(blockers)),
                parsed_as_of,
            )
        except (ContractError, BaselineError) as exc:
            blockers.append(f"LEDGER_V2_INVALID:{type(exc).__name__}:{exc}")
            return (
                {
                    "schema": value.get("schema"),
                    "as_of_utc": value.get("as_of_utc"),
                    "record_count": _ledger_record_count(value),
                },
                sorted(set(blockers)),
                None,
            )
    if value.get("schema") != GAP_LEDGER_SCHEMA:
        blockers.append("LEDGER_SCHEMA_INVALID")
    try:
        normalized_as_of, parsed_as_of = _utc_z(
            value.get("as_of_utc"), field="ledger_as_of_utc"
        )
        raw_records = value.get("records")
        raw_statuses = value.get("builder_scope_statuses")
        raw_pointers = value.get("source_pointers")
        if not isinstance(raw_records, list) or not isinstance(raw_statuses, list) or not isinstance(raw_pointers, Mapping):
            raise ContractError("ledger_shape_invalid")
        records = [GapRecord.from_mapping(item) for item in raw_records]
        statuses = [BuilderScopeStatus.from_mapping(item) for item in raw_statuses]
        rebuilt = GapLedger(
            records,
            as_of_utc=normalized_as_of,
            source_pointers=raw_pointers,
            builder_scope_statuses=statuses,
        ).to_mapping()
        if set(value) != set(rebuilt) or value != rebuilt:
            blockers.append("LEDGER_CANONICAL_REPLAY_MISMATCH")
        ready_scope_keys = {
            (record.station_id, record.lead_bucket, record.model_family)
            for record in records
            if record.state == "ACTIVATION_READY"
        }
        if any(record.state == "ACTIVATION_READY" and record.blockers for record in records):
            blockers.append("LEDGER_READY_RECORD_HAS_BLOCKER")
        if any(status.state not in {"BUILT", "BLOCKED"} for status in statuses):
            blockers.append("LEDGER_BUILDER_SCOPE_STATE_INVALID")
        if any(status.state == "BUILT" and status.blockers for status in statuses):
            blockers.append("LEDGER_BUILDER_BUILT_HAS_BLOCKER")
        if any(status.state == "BLOCKED" and not status.blockers for status in statuses):
            blockers.append("LEDGER_BUILDER_BLOCKED_WITHOUT_BLOCKER")
        if any(status.state == "BLOCKED" for status in statuses):
            blockers.append("LEDGER_BUILDER_SCOPE_BLOCKED")
        if any(
            status.state == "BLOCKED"
            and (status.station_id, status.lead_bucket, status.model_family) in ready_scope_keys
            for status in statuses
        ):
            blockers.append("LEDGER_BUILDER_BLOCKER_CONTRADICTS_RECORD_READY")
        summary = rebuilt["summary"]
        return (
            {
                "schema": rebuilt["schema"],
                "as_of_utc": normalized_as_of,
                "record_count": len(records),
                "builder_scope_status_count": len(statuses),
                "builder_scope_blocked_count": summary["builder_scope_blocked_rows"],
            },
            sorted(set(blockers)),
            parsed_as_of,
        )
    except (ContractError, BaselineError, TypeError, ValueError) as exc:
        blockers.append(f"LEDGER_CANONICAL_PARSE_FAILED:{type(exc).__name__}")
    return (
        {"schema": value.get("schema"), "as_of_utc": normalized_as_of},
        sorted(set(blockers)),
        parsed_as_of,
    )


def _p1_evidence_pair(
    audit: Mapping[str, Any],
    audit_meta: Mapping[str, Any],
    repair: Mapping[str, Any],
    repair_meta: Mapping[str, Any],
    *,
    outer_as_of: datetime,
) -> tuple[dict[str, Any], list[str]]:
    """Freeze the explicit P1 audit/repair pair without selecting a latest file."""

    blockers: list[str] = []
    if audit.get("schema") != P1_AUDIT_SCHEMA:
        blockers.append("P1_AUDIT_SCHEMA_INVALID")
    if repair.get("schema") != P1_REPAIR_SCHEMA:
        blockers.append("P1_REPAIR_SCHEMA_INVALID")
    if not bool(audit_meta.get("declared_content_hash_valid")):
        blockers.append("P1_AUDIT_CONTENT_SHA256_INVALID")
    if not bool(repair_meta.get("declared_content_hash_valid")):
        blockers.append("P1_REPAIR_CONTENT_SHA256_INVALID")

    audit_as_of_text: str | None = None
    repair_as_of_text: str | None = None
    audit_as_of: datetime | None = None
    repair_as_of: datetime | None = None
    try:
        audit_as_of_text, audit_as_of = _utc_z(
            audit.get("as_of_utc"), field="p1_audit_as_of_utc"
        )
    except BaselineError as exc:
        blockers.append(str(exc).upper())
    try:
        repair_as_of_text, repair_as_of = _utc_z(
            repair.get("as_of_utc"), field="p1_repair_as_of_utc"
        )
    except BaselineError as exc:
        blockers.append(str(exc).upper())
    if audit_as_of is not None and repair_as_of is not None and audit_as_of != repair_as_of:
        blockers.append("P1_EVIDENCE_PAIR_AS_OF_MISMATCH")
    for label, parsed in (("P1_AUDIT", audit_as_of), ("P1_REPAIR", repair_as_of)):
        if parsed is not None and parsed > outer_as_of:
            blockers.append(f"{label}_AFTER_OUTER_AS_OF")

    audit_sha = _sha256_text(audit.get("content_sha256"))
    repair_audit_sha = _sha256_text(repair.get("audit_content_sha256"))
    if audit_sha is None or repair_audit_sha != audit_sha:
        blockers.append("P1_EVIDENCE_PAIR_AUDIT_HASH_MISMATCH")
    audit_scope_count = audit.get("scope_count")
    repair_scope_count = repair.get("scope_count")
    if (
        isinstance(audit_scope_count, bool)
        or not isinstance(audit_scope_count, int)
        or audit_scope_count < 1
        or repair_scope_count != audit_scope_count
    ):
        blockers.append("P1_EVIDENCE_PAIR_SCOPE_COUNT_INVALID")

    for label, payload in (("P1_AUDIT", audit), ("P1_REPAIR", repair)):
        boundary = payload.get("boundary")
        expected = {
            "execution_mode": "production_no_live",
            "live_submit_authorized": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "orders_submitted": 0,
            "writes_canonical_database": False,
            "writes_target_database": False,
        }
        if not isinstance(boundary, Mapping) or any(
            boundary.get(key) != value for key, value in expected.items()
        ):
            blockers.append(f"{label}_NO_LIVE_BOUNDARY_INVALID")

    return (
        {
            "audit": {
                **audit_meta,
                "schema": audit.get("schema"),
                "as_of_utc": audit_as_of_text,
                "scope_count": audit_scope_count,
            },
            "repair": {
                **repair_meta,
                "schema": repair.get("schema"),
                "as_of_utc": repair_as_of_text,
                "scope_count": repair_scope_count,
                "audit_content_sha256": repair.get("audit_content_sha256"),
            },
        },
        sorted(set(blockers)),
    )


def _sqlite_uri(path: Path) -> str:
    # SQLite accepts an absolute POSIX path in this URI form.  The database is
    # explicitly mode=ro and query_only is asserted again after connecting.
    return f"file:{path}?mode=ro"


def _activation_generation_summary(
    conn: sqlite3.Connection, *, cutoff_as_of_utc: str,
) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact WHERE fact_type='activation' "
        "AND julianday(ingested_at_utc) <= julianday(?) ORDER BY scope_key",
        (cutoff_as_of_utc,),
    ).fetchall()
    stations_by_lane: dict[tuple[str, str, str], dict[str, set[int]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for (raw,) in rows:
        try:
            value = json.loads(str(raw))
        except (TypeError, json.JSONDecodeError) as exc:
            raise BaselineError("baseline_activation_fact_json_invalid") from exc
        if not isinstance(value, Mapping):
            raise BaselineError("baseline_activation_fact_mapping_required")
        station = str(value.get("station_id") or "").strip().upper()
        family = str(value.get("model_family") or "").strip()
        lead = str(value.get("lead_bucket") or "").strip()
        cycle = str(value.get("run_cycle") or "").strip()
        generation = value.get("activation_generation")
        if not station or not family or not lead or not cycle or isinstance(generation, bool):
            raise BaselineError("baseline_activation_fact_scope_invalid")
        try:
            generation_int = int(generation)
        except (TypeError, ValueError) as exc:
            raise BaselineError("baseline_activation_fact_generation_invalid") from exc
        if generation_int < 1:
            raise BaselineError("baseline_activation_fact_generation_invalid")
        stations_by_lane[(family, lead, cycle)][station].add(generation_int)

    lanes: list[dict[str, Any]] = []
    for (family, lead, cycle), station_generations in sorted(stations_by_lane.items()):
        generation_sets: dict[str, int] = defaultdict(int)
        latest_coverage: dict[int, int] = defaultdict(int)
        for generations in station_generations.values():
            generation_sets[",".join(str(item) for item in sorted(generations))] += 1
            latest_coverage[max(generations)] += 1
        lanes.append(
            {
                "model_family": family,
                "lead_bucket": lead,
                "run_cycle": cycle,
                "station_count": len(station_generations),
                "generation_set_station_counts": dict(sorted(generation_sets.items())),
                "latest_generation_station_counts": {
                    str(key): latest_coverage[key] for key in sorted(latest_coverage)
                },
                "mixed_generation": len(generation_sets) > 1,
            }
        )
    return {"activation_row_count": len(rows), "lanes": lanes}


def _read_db_snapshot(
    path: Path | str, *, cutoff_as_of_utc: str,
) -> dict[str, Any]:
    source = _resolved_file(path)
    last_error: Exception | None = None
    for _ in range(MAX_RETRIES):
        conn: sqlite3.Connection | None = None
        try:
            before = _file_identity(source)
            wal = source.with_name(source.name + "-wal")
            shm = source.with_name(source.name + "-shm")
            wal_before = _file_identity(wal) if wal.is_file() else None
            shm_before = _file_identity(shm) if shm.is_file() else None
            conn = sqlite3.connect(_sqlite_uri(source), uri=True, timeout=1.0)
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='canonical_ssot_fact'"
            ).fetchone()
            if table is None:
                raise BaselineError("baseline_canonical_ssot_fact_missing")
            data_version_before = int(conn.execute("PRAGMA data_version").fetchone()[0])
            schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
            journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
            page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
            page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
            freelist_count = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
            invalid_ingested_at_count = int(conn.execute(
                "SELECT COUNT(*) FROM canonical_ssot_fact "
                "WHERE julianday(ingested_at_utc) IS NULL"
            ).fetchone()[0])
            if invalid_ingested_at_count:
                raise BaselineError("baseline_fact_ingested_at_invalid")
            fact_counts = {
                str(fact_type): int(count)
                for fact_type, count in conn.execute(
                    "SELECT fact_type,COUNT(*) FROM canonical_ssot_fact "
                    "WHERE julianday(ingested_at_utc) <= julianday(?) "
                    "GROUP BY fact_type ORDER BY fact_type",
                    (cutoff_as_of_utc,),
                ).fetchall()
            }
            max_ingested_at_row = conn.execute(
                "SELECT ingested_at_utc FROM canonical_ssot_fact "
                "WHERE julianday(ingested_at_utc) <= julianday(?) "
                "ORDER BY julianday(ingested_at_utc) DESC LIMIT 1",
                (cutoff_as_of_utc,),
            ).fetchone()
            max_ingested_at_utc = max_ingested_at_row[0] if max_ingested_at_row else None
            total_fact_count = int(conn.execute(
                "SELECT COUNT(*) FROM canonical_ssot_fact"
            ).fetchone()[0])
            fact_rows_after_cutoff = int(conn.execute(
                "SELECT COUNT(*) FROM canonical_ssot_fact "
                "WHERE julianday(ingested_at_utc) > julianday(?)",
                (cutoff_as_of_utc,),
            ).fetchone()[0])
            projection_digest = hashlib.sha256()
            projection_row_count = 0
            projection_rows = conn.execute(
                "SELECT fact_id,fact_type,scope_key,content_sha256,fact_json," 
                "lineage_json,ingested_at_utc FROM canonical_ssot_fact "
                "WHERE julianday(ingested_at_utc) <= julianday(?) "
                "ORDER BY fact_type,scope_key,fact_id",
                (cutoff_as_of_utc,),
            )
            for row in projection_rows:
                encoded = json.dumps(
                    list(row),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                projection_digest.update(len(encoded).to_bytes(8, "big"))
                projection_digest.update(encoded)
                projection_row_count += 1
            if projection_row_count != sum(fact_counts.values()):
                raise BaselineError("baseline_fact_projection_count_mismatch")
            activation_summary = _activation_generation_summary(
                conn, cutoff_as_of_utc=cutoff_as_of_utc,
            )
            data_version_after = int(conn.execute("PRAGMA data_version").fetchone()[0])
            conn.execute("ROLLBACK")
            conn.close()
            conn = None
            after = _file_identity(source)
            wal_after = _file_identity(wal) if wal.is_file() else None
            shm_after = _file_identity(shm) if shm.is_file() else None
            # A read transaction in WAL mode is a consistent SQLite snapshot
            # even when a resident writer appends to ``-wal`` after this
            # connection began.  Rejecting every external WAL append would
            # make PR01 impossible to run on the very append-only SSOT it is
            # meant to freeze.  Record activity for audit, but keep the
            # transaction result as the snapshot and never merge values read
            # before/after it.
            external_activity_observed = (
                before != after
                or wal_before != wal_after
                or shm_before != shm_after
                or data_version_before != data_version_after
            )
            return {
                "path": str(source),
                "file_at_snapshot_open": before,
                "wal_file_at_snapshot_open": wal_before,
                "shm_file_at_snapshot_open": shm_before,
                "file_after_snapshot_read": after,
                "wal_file_after_snapshot_read": wal_after,
                "shm_file_after_snapshot_read": shm_after,
                "external_activity_observed": external_activity_observed,
                "data_version": data_version_after,
                "schema_version": schema_version,
                "journal_mode": journal_mode,
                "page_count": page_count,
                "page_size": page_size,
                "freelist_count": freelist_count,
                "fact_type_counts": fact_counts,
                "max_ingested_at_utc": str(max_ingested_at_utc or ""),
                "cutoff_as_of_utc": cutoff_as_of_utc,
                "total_fact_count": total_fact_count,
                "fact_rows_after_cutoff": fact_rows_after_cutoff,
                "canonical_projection_row_count": projection_row_count,
                "canonical_projection_sha256": projection_digest.hexdigest(),
                "activation_generation_summary": activation_summary,
            }
        except (sqlite3.Error, BaselineError, OSError) as exc:
            last_error = exc
            if conn is not None:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                conn.close()
    raise BaselineError(str(last_error or "baseline_database_read_failed"))


def _decode_diff_path(raw: str) -> str | None:
    value = raw.rstrip("\n")
    if value == "/dev/null":
        return None
    if value.startswith('"'):
        try:
            value = str(json.loads(value))
        except (TypeError, json.JSONDecodeError) as exc:
            raise BaselineError("baseline_worktree_diff_path_invalid") from exc
    if value.startswith(("a/", "b/")):
        value = value[2:]
    return value


def _git_hunk_map(directory: Path, *, cached: bool) -> dict[str, list[str]]:
    command = [
        "git", "-c", "core.quotePath=false", "diff", "--no-ext-diff",
        "--no-textconv", "--no-color", "--unified=0",
    ]
    if cached:
        command.append("--cached")
    process = subprocess.Popen(
        command,
        cwd=directory,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    timed_out = {"value": False}

    def _kill() -> None:
        timed_out["value"] = True
        process.kill()

    timer = threading.Timer(GIT_DIFF_TIMEOUT_SECONDS, _kill)
    timer.start()
    result: dict[str, list[str]] = defaultdict(list)
    old_path: str | None = None
    current_path: str | None = None
    hunk_count = 0
    try:
        assert process.stdout is not None
        for line in process.stdout:
            if line.startswith("--- "):
                old_path = _decode_diff_path(line[4:])
                continue
            if line.startswith("+++ "):
                current_path = _decode_diff_path(line[4:]) or old_path
                continue
            match = HUNK_RE.match(line)
            if match is None or current_path is None:
                continue
            hunk_count += 1
            if hunk_count > MAX_WORKTREE_HUNKS:
                process.kill()
                raise BaselineError("baseline_worktree_hunk_limit_exceeded")
            # A compact, lossless representation keeps a large shared
            # worktree below the global JSON node/byte guards while retaining
            # the exact lane and old/new hunk ranges.
            result[current_path].append(
                f"{'index' if cached else 'worktree'}:"
                f"{int(match.group(1))},{int(match.group(2) or '1')}:"
                f"{int(match.group(3))},{int(match.group(4) or '1')}"
            )
        returncode = process.wait()
    finally:
        timer.cancel()
        if process.stdout is not None:
            process.stdout.close()
    if timed_out["value"]:
        raise BaselineError("baseline_worktree_hunk_scan_timeout")
    if returncode != 0:
        raise BaselineError("baseline_worktree_hunk_scan_failed")
    return {path: hunks for path, hunks in sorted(result.items())}


def _worktree_status(root: Path | str) -> dict[str, Any]:
    """Freeze dirty paths and hunk ranges without copying diff contents."""

    directory = Path(root).expanduser().resolve()
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=directory,
            check=False,
            capture_output=True,
            timeout=10,
        )
        if completed.returncode != 0:
            return {
                "state": "UNAVAILABLE",
                "reason": "git_status_failed",
                "returncode": int(completed.returncode),
            }
        raw_entries = completed.stdout.decode("utf-8", "replace").split("\0")
        staged_hunks = _git_hunk_map(directory, cached=True)
        worktree_hunks = _git_hunk_map(directory, cached=False)
    except (OSError, subprocess.SubprocessError, BaselineError) as exc:
        return {"state": "UNAVAILABLE", "reason": str(exc) or type(exc).__name__}

    paths: list[dict[str, Any]] = []
    index = 0
    while index < len(raw_entries):
        entry = raw_entries[index]
        index += 1
        if not entry:
            continue
        if len(entry) < 4 or entry[2] != " ":
            raise BaselineError("baseline_worktree_status_invalid")
        status = entry[:2]
        path = entry[3:]
        record: dict[str, Any] = {
            "status": status,
            "path": path,
            "owner": "PREEXISTING_USER_WORKTREE",
            "hunks": sorted(
                [*staged_hunks.get(path, []), *worktree_hunks.get(path, [])]
            ),
        }
        if "R" in status or "C" in status:
            if index >= len(raw_entries) or not raw_entries[index]:
                raise BaselineError("baseline_worktree_rename_source_missing")
            record["original_path"] = raw_entries[index]
            index += 1
        paths.append(record)
    if len(paths) > MAX_WORKTREE_PATHS:
        raise BaselineError("baseline_worktree_path_limit_exceeded")
    paths.sort(key=lambda item: (str(item["path"]), str(item["status"])))
    return {
        "state": "CAPTURED",
        "dirty_path_count": len(paths),
        "hunk_count": sum(len(item["hunks"]) for item in paths),
        "owner": "PREEXISTING_USER_WORKTREE",
        "paths_sha256": content_sha256(paths),
        "paths": paths,
    }


def _ledger_record_count(value: Mapping[str, Any]) -> int | None:
    records = value.get("records")
    if isinstance(records, list):
        return len(records)
    summary = value.get("summary")
    if isinstance(summary, Mapping) and isinstance(summary.get("rows"), int):
        return int(summary["rows"])
    return None


def _consistency_blockers(
    *,
    ledger: Mapping[str, Any],
    ledger_meta: Mapping[str, Any],
    pointer: Mapping[str, Any],
    pointer_meta: Mapping[str, Any],
    inner_snapshot_meta: Mapping[str, Any],
    ledger_as_of: datetime | None,
    inner_as_of: datetime | None,
) -> list[str]:
    blockers: list[str] = []
    if not bool(ledger_meta.get("declared_content_hash_valid")):
        blockers.append("LEDGER_DECLARED_CONTENT_HASH_INVALID")
    if not bool(pointer_meta.get("declared_content_hash_valid")):
        blockers.append("POINTER_DECLARED_CONTENT_HASH_INVALID")
    declared_ledger = str(ledger.get("content_sha256") or "").lower()
    if str(pointer.get("ledger_content_sha256") or "").lower() != declared_ledger:
        blockers.append("LEDGER_POINTER_CONTENT_SHA256_MISMATCH")
    if str(pointer.get("ledger_file_sha256") or "").lower() != str(ledger_meta["file_sha256"]):
        blockers.append("LEDGER_POINTER_FILE_SHA256_MISMATCH")
    pointer_path = str(pointer.get("ledger_path") or "").strip()
    if pointer_path:
        try:
            if Path(pointer_path).expanduser().resolve(strict=True) != Path(str(ledger_meta["path"])).resolve():
                blockers.append("LEDGER_POINTER_PATH_MISMATCH")
        except OSError:
            blockers.append("LEDGER_POINTER_PATH_MISMATCH")
    else:
        blockers.append("LEDGER_POINTER_PATH_MISSING")
    count = _ledger_record_count(ledger)
    if count is None:
        blockers.append("LEDGER_RECORD_COUNT_MISSING")
    elif pointer.get("record_count") != count:
        blockers.append("LEDGER_POINTER_RECORD_COUNT_MISMATCH")
    as_of = str(ledger.get("as_of_utc") or "").strip()
    if as_of and str(pointer.get("effective_as_of_utc") or "").strip() != as_of:
        blockers.append("LEDGER_POINTER_AS_OF_MISMATCH")
    pointer_inner_sha = _sha256_text(pointer.get("inner_oos_snapshot_content_sha256"))
    if pointer_inner_sha is None:
        blockers.append("POINTER_INNER_OOS_SNAPSHOT_CONTENT_SHA256_MISSING")
    elif pointer_inner_sha != _sha256_text(inner_snapshot_meta.get("declared_content_sha256")):
        blockers.append("POINTER_INNER_OOS_SNAPSHOT_CONTENT_SHA256_MISMATCH")
    try:
        pointer_as_of_text, pointer_as_of = _utc_z(
            pointer.get("effective_as_of_utc"), field="pointer_effective_as_of_utc"
        )
        if ledger_as_of is not None and pointer_as_of != ledger_as_of:
            blockers.append("LEDGER_POINTER_AS_OF_MISMATCH")
        if inner_as_of is not None and pointer_as_of != inner_as_of:
            blockers.append("POINTER_INNER_OOS_SNAPSHOT_AS_OF_MISMATCH")
        # Retain the parse solely as validation; the normalized value belongs
        # to the receipt fields built by the caller.
        del pointer_as_of_text
    except BaselineError:
        blockers.append("POINTER_EFFECTIVE_AS_OF_INVALID")
    no_live_expectations = {
        "production_no_live": True,
        "formal_p0_eligible": False,
        "formal_live_for_republisher": False,
        "future_data_counts_as_oos": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "writes_production_db": False,
    }
    for key, expected in no_live_expectations.items():
        if pointer.get(key) != expected:
            blockers.append(f"POINTER_{key.upper()}_INVALID")
    return blockers


def _stable_file_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in (
            "path",
            "file_sha256",
            "declared_content_sha256",
            "declared_content_hash_valid",
            "schema",
            "as_of_utc",
        )
    }


def build(
    *,
    db_path: Path,
    ledger_path: Path,
    pointer_path: Path,
    inner_oos_snapshot_path: Path,
    p1_audit_path: Path,
    p1_repair_path: Path,
    as_of_utc: str,
    worktree_root: Path = ROOT,
) -> dict[str, Any]:
    outer_as_of_text, outer_as_of = _utc_z(
        as_of_utc, field="baseline_outer_as_of_utc"
    )
    ledger, ledger_meta = _read_stable_json(ledger_path)
    pointer, pointer_meta = _read_stable_json(pointer_path)
    root_pointer_blockers: list[str] = []
    if pointer.get("schema") == ROOT_POINTER_SCHEMA:
        try:
            verify_current_root_pointer_v2(pointer, open_release=True)
        except ContractError as exc:
            root_pointer_blockers.append(
                f"CURRENT_ROOT_POINTER_V2_INVALID:{type(exc).__name__}:{exc}"
            )
    inner_snapshot, inner_snapshot_meta = _read_stable_json(inner_oos_snapshot_path)
    p1_audit, p1_audit_meta = _read_stable_json(p1_audit_path)
    p1_repair, p1_repair_meta = _read_stable_json(p1_repair_path)
    inner_summary, inner_blockers, inner_as_of = _inner_oos_snapshot(
        inner_snapshot, inner_snapshot_meta, outer_as_of=outer_as_of,
    )
    p1_summary, p1_blockers = _p1_evidence_pair(
        p1_audit,
        p1_audit_meta,
        p1_repair,
        p1_repair_meta,
        outer_as_of=outer_as_of,
    )
    ledger_summary, ledger_blockers, ledger_as_of = _canonical_gap_ledger(ledger)
    database_cutoff = (
        inner_summary.get("as_of_utc")
        if isinstance(inner_summary.get("as_of_utc"), str)
        else outer_as_of_text
    )
    database = _read_db_snapshot(
        db_path, cutoff_as_of_utc=str(database_cutoff),
    )
    blockers = _consistency_blockers(
        ledger=ledger,
        ledger_meta=ledger_meta,
        pointer=pointer,
        pointer_meta=pointer_meta,
        inner_snapshot_meta=inner_snapshot_meta,
        ledger_as_of=ledger_as_of,
        inner_as_of=inner_as_of,
    )
    blockers.extend(inner_blockers)
    blockers.extend(p1_blockers)
    blockers.extend(ledger_blockers)
    blockers.extend(root_pointer_blockers)
    if ledger_as_of is not None and ledger_as_of > outer_as_of:
        blockers.append("LEDGER_AFTER_OUTER_AS_OF")
    if inner_as_of is not None and ledger_as_of is not None and ledger_as_of != inner_as_of:
        blockers.append("LEDGER_INNER_OOS_SNAPSHOT_AS_OF_MISMATCH")
    worktree = _worktree_status(worktree_root)
    if worktree.get("state") != "CAPTURED":
        blockers.append("WORKTREE_SNAPSHOT_UNAVAILABLE")
    blockers = sorted(set(blockers))
    state = "READY" if not blockers else "BLOCKED"
    verification_identity = {
        "schema": SCHEMA,
        "as_of_utc": outer_as_of_text,
        "state": state,
        "blockers": blockers,
        "database": {
            key: database.get(key)
            for key in (
                "path",
                "schema_version",
                "journal_mode",
                "fact_type_counts",
                "max_ingested_at_utc",
                "cutoff_as_of_utc",
                "total_fact_count",
                "fact_rows_after_cutoff",
                "canonical_projection_row_count",
                "canonical_projection_sha256",
                "activation_generation_summary",
            )
        },
        "ledger": _stable_file_evidence({**ledger_meta, **ledger_summary}),
        "inner_oos_snapshot": _stable_file_evidence(inner_summary),
        "pointer": _stable_file_evidence(
            {
                **pointer_meta,
                "schema": pointer.get("schema"),
                "as_of_utc": pointer.get("effective_as_of_utc"),
            }
        ),
        "p1_evidence_pair": {
            key: _stable_file_evidence(value)
            for key, value in p1_summary.items()
        },
        "worktree": {
            "state": worktree.get("state"),
            "dirty_path_count": worktree.get("dirty_path_count"),
            "hunk_count": worktree.get("hunk_count"),
            "owner": worktree.get("owner"),
            "paths_sha256": worktree.get("paths_sha256"),
        },
        "boundary": {
            "writes_target_database": False,
            "writes_production_database": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    }
    identity = {
        "schema": SCHEMA,
        "as_of_utc": outer_as_of_text,
        "generated_at_utc": _utc_now(),
        "state": state,
        "blockers": blockers,
        "verification_identity_sha256": content_sha256(verification_identity),
        "database": database,
        "ledger": {
            **ledger_meta,
            **ledger_summary,
        },
        "inner_oos_snapshot": {
            **inner_summary,
        },
        "pointer": {
            **pointer_meta,
            "schema": pointer.get("schema"),
            "effective_as_of_utc": pointer.get("effective_as_of_utc"),
            "record_count": pointer.get("record_count"),
        },
        "p1_evidence_pair": p1_summary,
        "worktree": worktree,
        "writes_target_database": False,
        "writes_production_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "bayes_basket_target_ssot.db")
    parser.add_argument(
        "--ledger",
        type=Path,
        help=(
            "Explicit immutable ledger.  When omitted, it is resolved only "
            "from a strictly verified v2 current-root pointer."
        ),
    )
    parser.add_argument(
        "--pointer",
        type=Path,
        default=ROOT / "output" / "audits" / "oos_gap_ledger_45station_current.json",
    )
    parser.add_argument(
        "--inner-oos-snapshot",
        type=Path,
        required=True,
        help="Content-addressed atomic P0/TAF/OOS snapshot; no implicit latest fallback.",
    )
    parser.add_argument(
        "--p1-audit",
        type=Path,
        required=True,
        help="Explicit content-addressed P1 trajectory audit; no implicit latest fallback.",
    )
    parser.add_argument(
        "--p1-repair",
        type=Path,
        required=True,
        help="Explicit P1 repair plan bound to --p1-audit.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "output" / "audits" / "ssot_baseline_manifest_current.json",
    )
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--worktree-root", type=Path, default=ROOT)
    parser.add_argument("--require-consistent", action="store_true")
    args = parser.parse_args()
    ledger_path = args.ledger
    if ledger_path is None:
        pointer_value, _pointer_meta = _read_stable_json(args.pointer)
        if pointer_value.get("schema") != ROOT_POINTER_SCHEMA:
            raise BaselineError("baseline_legacy_pointer_requires_explicit_ledger")
        verify_current_root_pointer_v2(pointer_value, open_release=True)
        pointer_ledger_path = str(pointer_value.get("ledger_path") or "").strip()
        if not pointer_ledger_path:
            raise BaselineError("baseline_current_root_ledger_path_missing")
        ledger_path = Path(pointer_ledger_path)
    output = args.output.expanduser().resolve()
    for source in (
        args.db,
        ledger_path,
        args.pointer,
        args.inner_oos_snapshot,
        args.p1_audit,
        args.p1_repair,
    ):
        try:
            if output == Path(source).expanduser().resolve(strict=True):
                raise BaselineError("baseline_output_input_collision")
        except OSError:
            pass
    result = build(
        db_path=args.db,
        ledger_path=ledger_path,
        pointer_path=args.pointer,
        inner_oos_snapshot_path=args.inner_oos_snapshot,
        p1_audit_path=args.p1_audit,
        p1_repair_path=args.p1_repair,
        as_of_utc=args.as_of_utc,
        worktree_root=args.worktree_root,
    )
    changed = write_bounded_json_if_changed(output, result)
    print(
        json.dumps(
            {
                "schema": result["schema"],
                "state": result["state"],
                "blockers": result["blockers"],
                "content_sha256": result["content_sha256"],
                "verification_identity_sha256": result["verification_identity_sha256"],
                "changed": changed,
                "output": str(output),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 2 if args.require_consistent and result["blockers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
