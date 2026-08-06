"""Atomic V10-to-V11 migration for canonical market bucket bridges."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .canonical_ssot_migration_common import migrate_append_only_fact_schema
from .canonical_ssot_schema import (
    FACT_TYPES_V11,
    V10_TRIGGERS,
    V11_TRIGGERS,
    inspect_canonical_ssot_schema,
)


SCHEMA = "bayes_basket_target_canonical_ssot_fact_v11_migration_receipt_v1"
CONFIRMATION = "migrate-canonical-ssot-fact-v11-production-no-live"
TABLE = "canonical_ssot_fact"
NEW_TABLE = "canonical_ssot_fact_v11_new"
BACKUP_TABLE = "canonical_ssot_fact_v10_replaced"
SOURCE_TRIGGERS = V10_TRIGGERS
TARGET_TRIGGERS = V11_TRIGGERS
RETIRED_TRIGGERS = (
    "canonical_ssot_fact_v10_replaced_no_update",
    "canonical_ssot_fact_v10_replaced_no_delete",
)

TARGET_TABLE_DDL = """
CREATE TABLE canonical_ssot_fact (
  fact_id TEXT PRIMARY KEY,
  fact_type TEXT NOT NULL CHECK(fact_type IN ('model','truth','residual','activation','activation_supersession','ensemble_member','native_arrival','ensemble_member_arrival_binding','ensemble_member_receipt_manifest','republisher_oos_artifact','republisher_cross_selection','republisher_no_live_activation','forward_applicability','probability_distribution_artifact','p0_snapshot','market_bucket_bridge')),
  scope_key TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  fact_json TEXT NOT NULL,
  lineage_json TEXT NOT NULL,
  ingested_at_utc TEXT NOT NULL,
  UNIQUE(fact_type, content_sha256),
  UNIQUE(fact_type, scope_key)
)
""".strip()
TARGET_NEW_TABLE_DDL = TARGET_TABLE_DDL.replace(
    "CREATE TABLE canonical_ssot_fact", f"CREATE TABLE {NEW_TABLE}", 1
)
TARGET_TRIGGER_DDL = (
    "CREATE TRIGGER canonical_ssot_fact_no_update BEFORE UPDATE ON canonical_ssot_fact "
    "BEGIN SELECT RAISE(ABORT,'append_only'); END",
    "CREATE TRIGGER canonical_ssot_fact_no_delete BEFORE DELETE ON canonical_ssot_fact "
    "BEGIN SELECT RAISE(ABORT,'append_only'); END",
    "CREATE TRIGGER canonical_ssot_fact_ingress_only BEFORE INSERT ON canonical_ssot_fact "
    "WHEN canonical_ingress_authorized()!=1 "
    "BEGIN SELECT RAISE(ABORT,'canonical_ingress_only'); END",
)


class CanonicalSSOTV11MigrationError(RuntimeError):
    """Fail-closed V11 migration error."""


def migrate_canonical_ssot_fact_v11(
    db_path: Path,
    *,
    apply: bool = False,
    confirmation: str | None = None,
    writer_quiescence_receipt: Mapping[str, Any] | None = None,
    inject_fault_at: str | None = None,
) -> dict[str, Any]:
    return migrate_append_only_fact_schema(
        db_path,
        apply=apply,
        confirmation=confirmation,
        expected_confirmation=CONFIRMATION,
        writer_quiescence_receipt=writer_quiescence_receipt,
        inject_fault_at=inject_fault_at,
        source_state="V10",
        target_state="V11",
        table=TABLE,
        new_table=NEW_TABLE,
        backup_table=BACKUP_TABLE,
        target_new_table_ddl=TARGET_NEW_TABLE_DDL,
        target_trigger_ddl=TARGET_TRIGGER_DDL,
        source_triggers=SOURCE_TRIGGERS,
        target_triggers=TARGET_TRIGGERS,
        retired_triggers=RETIRED_TRIGGERS,
        inspect_schema=inspect_canonical_ssot_schema,
        receipt_schema=SCHEMA,
        error_type=CanonicalSSOTV11MigrationError,
        error_prefix="canonical_ssot_v11",
    )


__all__ = [
    "BACKUP_TABLE",
    "CONFIRMATION",
    "FACT_TYPES_V11",
    "NEW_TABLE",
    "RETIRED_TRIGGERS",
    "SCHEMA",
    "TARGET_TABLE_DDL",
    "TARGET_TRIGGER_DDL",
    "CanonicalSSOTV11MigrationError",
    "migrate_canonical_ssot_fact_v11",
]
