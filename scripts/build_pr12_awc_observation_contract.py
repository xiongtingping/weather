#!/usr/bin/env python3
"""Build PR12 AWC evidence, canonical facts and terminal gaps without publishing."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.awc_observation_builder import (  # noqa: E402
    build_awc_observation_contract,
    export_awc_ingress_evidence_v4,
)
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    write_bounded_json_if_changed,
)
from bayes_basket_target.pr12_authority_scope_projection import (  # noqa: E402
    validate_pr12_authority_scope_projection,
)


def _station_registry(target_db: Path) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    with sqlite3.connect(f"file:{target_db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT station_id,settlement_definition,content_sha256 "
            "FROM calibration_station_registry ORDER BY station_id"
        ).fetchall()
    for raw in rows:
        record = dict(raw)
        station = str(record["station_id"]).strip().upper()
        prior = result.get(station)
        if prior is not None:
            raise ContractError(f"pr12_station_registry_duplicate:{station}")
        record["station_id"] = station
        result[station] = record
    if len(result) != 45:
        raise ContractError(f"pr12_station_registry_not_45:{len(result)}")
    return result


def _exclude_materialized_day_scopes(
    target_db: Path, evidence: dict[str, object]
) -> dict[str, object]:
    """Do not rebuild an append-only station-day already in canonical SSOT."""

    existing: dict[tuple[str, str], str] = {}
    with sqlite3.connect(f"file:{target_db}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT fact_json FROM canonical_ssot_fact "
            "WHERE fact_type='truth_observation_day_bounds'"
        ).fetchall()
    for (raw_json,) in rows:
        raw = json.loads(str(raw_json))
        key = (str(raw["station_id"]).upper(), str(raw["event_date"]))
        zone = str(raw["settlement_timezone"])
        prior = existing.get(key)
        if prior is not None and prior != zone:
            raise ContractError(f"pr12_existing_day_timezone_ambiguous:{key[0]}:{key[1]}")
        existing[key] = zone

    value = dict(evidence)
    excluded = 0
    kept_rows = []
    for raw in value.get("rows", []):
        key = (str(raw.get("station_id") or "").upper(), str(raw.get("event_date") or ""))
        prior_zone = existing.get(key)
        if prior_zone is None:
            kept_rows.append(raw)
            continue
        if prior_zone != str(raw.get("settlement_timezone") or ""):
            raise ContractError(f"pr12_existing_day_timezone_conflict:{key[0]}:{key[1]}")
        excluded += 1
    value["rows"] = kept_rows
    for field in ("station_day_halts", "station_day_reviews"):
        value[field] = [
            raw for raw in value.get(field, [])
            if (
                str(raw.get("station_id") or "").upper(),
                str(raw.get("event_date") or ""),
            ) not in existing
        ]
    value["materialized_day_scope_exclusion"] = {
        "existing_day_scope_count": len(existing),
        "excluded_evidence_row_count": excluded,
        "uses_canonical_natural_key_only": True,
        "backfills_or_rekeys_existing_fact": False,
    }
    identity = {key: item for key, item in value.items() if key != "receipt_sha256"}
    return {**identity, "receipt_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-database", type=Path, required=True)
    parser.add_argument("--target-database", type=Path, required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--evidence-output", type=Path, required=True)
    parser.add_argument("--contract-output", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    parser.add_argument("--authority-projection", type=Path, required=True)
    args = parser.parse_args()
    target = args.target_database.expanduser().resolve(strict=True)
    registry = _station_registry(target)
    projection = validate_pr12_authority_scope_projection(
        json.loads(args.authority_projection.expanduser().resolve(strict=True).read_text())
    )
    evidence = export_awc_ingress_evidence_v4(
        args.archive_database,
        station_registry=registry,
        authority_scope_projection=projection,
        as_of_utc=args.as_of_utc,
    )
    evidence = _exclude_materialized_day_scopes(target, evidence)
    contract, receipt = build_awc_observation_contract(evidence)
    for destination, payload in (
        (args.evidence_output, evidence),
        (args.contract_output, contract),
        (args.receipt_output, receipt),
    ):
        write_bounded_json_if_changed(
            destination.expanduser().resolve(),
            payload,
            limit=64 * 1024 * 1024,
            max_nodes=1_000_000,
        )
    summary = {
        "state": receipt["state"],
        "evidence_rows": len(evidence["rows"]),
        "extraction_gaps": len(evidence["extraction_gaps"]),
        "revision_facts": len(contract["revision_facts"]),
        "day_bounds_facts": len(contract["day_bounds_facts"]),
        "terminal_gaps": len(receipt["terminal_gaps"]),
        "evidence_receipt_sha256": evidence["receipt_sha256"],
        "contract_content_sha256": contract["content_sha256"],
        "builder_receipt_sha256": receipt["receipt_sha256"],
        "writes_database": False,
        "publishes_pr04_pointer": False,
        "starts_services": False,
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
