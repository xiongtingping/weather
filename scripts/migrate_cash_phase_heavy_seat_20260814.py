#!/usr/bin/env python3
"""One-time stop-the-service migration: give every station a HEAVY cash seat.

Thirty-one stations' cash-phase policy artifacts carry no HEAVY entry --
their source-clip fit saw no pre-event trades -- so every pre-event
evaluation fails closed (``cash_phase_unobserved_phase_fail_closed``) and
the opening stage can never spend, which contradicts charter 46 (P0 opens
positions) and charter 48 (positions open at listing).  The policy tables
are append-only and candidate policy windows must be globally
non-overlapping per station, so the correction cannot be appended; per the
repository's established pattern the old tables are renamed to frozen
``_legacy_`` copies (rows byte-identical, still readable) and the rebuilt
tables receive corrected artifacts.

The HEAVY amount follows the existing design template's proportion
(HEAVY:STANDARD = 3:2 on the stations that already carry all three
phases): HEAVY = 1.5 x STANDARD.  Stations already carrying HEAVY are
copied unchanged.  Every replaced artifact gets a new policy_version and a
recomputed content hash via the library's own identity computation, and
every authority manifest that named a replaced artifact is re-created
against the new hash.

Run with the resident worker stopped.  No orders, no gateway; this edits
no-live planning policy only.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.cash_phase import BasketCashPhasePolicyArtifact  # noqa: E402
from bayes_basket_target.cash_phase_authority import (  # noqa: E402
    CashPhaseAuthorityManifest,
)

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
LEGACY_SUFFIX = "_legacy_no_heavy_phase_v1"
VERSION_SUFFIX = "-heavy-open-v20260814"
HEAVY_TO_STANDARD_RATIO = 1.5

ARTIFACT_TABLE = "basket_cash_phase_policy_artifact"
MANIFEST_TABLE = "basket_cash_phase_authority_manifest"


def main() -> int:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        existing = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if ARTIFACT_TABLE + LEGACY_SUFFIX in existing:
            print("already migrated; nothing to do")
            return 0
        create_sql = {
            row["name"]: row["sql"]
            for row in conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type IN ('table','index','trigger') "
                "AND (name LIKE '%cash_phase%')"
            )
        }
        conn.execute("BEGIN IMMEDIATE")
        # Named indexes follow a renamed table and keep their names, which
        # would collide with the rebuilt ones; the frozen legacy copies do
        # not need them.
        for index in (
            "basket_cash_phase_policy_scope_idx",
            "basket_cash_phase_authority_scope_idx",
        ):
            conn.execute(f"DROP INDEX {index}")
        for trigger in (
            "basket_cash_phase_policy_no_update",
            "basket_cash_phase_policy_no_delete",
            "basket_cash_phase_authority_no_update",
            "basket_cash_phase_authority_no_delete",
        ):
            conn.execute(f"DROP TRIGGER {trigger}")
        conn.execute(
            f"ALTER TABLE {ARTIFACT_TABLE} RENAME TO {ARTIFACT_TABLE}{LEGACY_SUFFIX}"
        )
        conn.execute(
            f"ALTER TABLE {MANIFEST_TABLE} RENAME TO {MANIFEST_TABLE}{LEGACY_SUFFIX}"
        )
        # Rebuild the working tables and their append-only triggers from the
        # captured originals, then freeze the legacy copies completely.
        for name in (ARTIFACT_TABLE, MANIFEST_TABLE):
            conn.execute(create_sql[name])
        for name, sql in create_sql.items():
            if name.endswith(("_no_update", "_no_delete")):
                conn.execute(sql)
            elif name.endswith(("_scope_idx",)):
                conn.execute(sql)
        for table, tag in (
            (ARTIFACT_TABLE + LEGACY_SUFFIX, "policy_legacy"),
            (MANIFEST_TABLE + LEGACY_SUFFIX, "authority_legacy"),
        ):
            for action in ("UPDATE", "DELETE", "INSERT"):
                conn.execute(
                    f"CREATE TRIGGER basket_cash_phase_{tag}_no_{action.lower()} "
                    f"BEFORE {action} ON {table} "
                    "BEGIN SELECT RAISE(ABORT,'legacy_frozen'); END"
                )

        replaced: dict[tuple[str, str], BasketCashPhasePolicyArtifact] = {}
        copied = 0
        for row in conn.execute(
            f"SELECT * FROM {ARTIFACT_TABLE}{LEGACY_SUFFIX} ORDER BY rowid"
        ).fetchall():
            cash = json.loads(row["cash_by_phase_json"])
            if "HEAVY" in cash:
                conn.execute(
                    f"INSERT INTO {ARTIFACT_TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    tuple(row),
                )
                copied += 1
                continue
            standard = cash.get("STANDARD")
            heavy = (
                round(float(standard) * HEAVY_TO_STANDARD_RATIO, 2)
                if standard is not None
                else 3.0
            )
            artifact = BasketCashPhasePolicyArtifact.create(
                policy_version=row["policy_version"] + VERSION_SUFFIX,
                station_id=row["station_id"],
                station_timezone=row["station_timezone"],
                heavy_to_standard_local_time=row["heavy_to_standard_local_time"],
                standard_to_tail_local_time=row["standard_to_tail_local_time"],
                cash_by_phase={"HEAVY": heavy, **cash},
                valid_from_utc=row["valid_from_utc"],
                valid_until_utc=row["valid_until_utc"],
                evidence_status=row["evidence_status"],
                promoted=bool(row["promoted"]),
            )
            conn.execute(
                f"INSERT INTO {ARTIFACT_TABLE} "
                "(policy_version,station_id,station_timezone,"
                "heavy_to_standard_local_time,standard_to_tail_local_time,"
                "cash_by_phase_json,valid_from_utc,valid_until_utc,"
                "evidence_status,promoted,content_sha256) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    artifact.policy_version,
                    artifact.station_id,
                    artifact.station_timezone,
                    artifact.heavy_to_standard_local_time,
                    artifact.standard_to_tail_local_time,
                    json.dumps(dict(artifact.cash_by_phase), sort_keys=True),
                    artifact.valid_from_utc,
                    artifact.valid_until_utc,
                    artifact.evidence_status,
                    int(artifact.promoted),
                    artifact.content_sha256,
                ),
            )
            replaced[(row["station_id"], row["policy_version"])] = artifact

        manifest_copied = 0
        manifest_rebound = 0
        for row in conn.execute(
            f"SELECT * FROM {MANIFEST_TABLE}{LEGACY_SUFFIX} ORDER BY rowid"
        ).fetchall():
            key = (row["station_id"], row["policy_version"])
            if key not in replaced:
                conn.execute(
                    f"INSERT INTO {MANIFEST_TABLE} VALUES (?,?,?,?,?,?,?,?)",
                    tuple(row),
                )
                manifest_copied += 1
                continue
            artifact = replaced[key]
            manifest = CashPhaseAuthorityManifest.create(
                station_id=artifact.station_id,
                policy_version=artifact.policy_version,
                artifact_sha256=artifact.content_sha256,
                station_timezone=artifact.station_timezone,
                valid_from_utc=artifact.valid_from_utc,
                valid_until_utc=artifact.valid_until_utc,
                authority_scope=row["authority_scope"],
            )
            conn.execute(
                f"INSERT INTO {MANIFEST_TABLE} "
                "(manifest_sha256,station_id,policy_version,artifact_sha256,"
                "station_timezone,valid_from_utc,valid_until_utc,authority_scope) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    manifest.manifest_sha256,
                    manifest.station_id,
                    manifest.policy_version,
                    manifest.artifact_sha256,
                    manifest.station_timezone,
                    manifest.valid_from_utc,
                    manifest.valid_until_utc,
                    manifest.authority_scope,
                ),
            )
            manifest_rebound += 1

        conn.commit()
        print(
            json.dumps(
                {
                    "artifacts_copied": copied,
                    "artifacts_replaced": len(replaced),
                    "manifests_copied": manifest_copied,
                    "manifests_rebound": manifest_rebound,
                },
                ensure_ascii=False,
            )
        )
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
