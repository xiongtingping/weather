#!/usr/bin/env python3
"""Normalize one retained PR10 raw extension into canonical event-only facts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.canonical_ingress import ingest_canonical_contract  # noqa: E402
from bayes_basket_target.canonical_ssot_v15_migration import (  # noqa: E402
    TARGET_TABLE_DDL,
    TARGET_TRIGGER_DDL,
)
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.republisher_historical_event_builder import (  # noqa: E402
    build_republisher_historical_event_bundle,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    load_bounded_json,
    write_bounded_json,
)


def _database(*truth_contracts: dict[str, object]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(TARGET_TABLE_DDL)
    for ddl in TARGET_TRIGGER_DDL:
        conn.execute(ddl)
    for contract in truth_contracts:
        ingest_canonical_contract(
            conn, contract, commit=True, ingested_at_utc=str(contract["as_of_utc"])
        )
    return conn


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-bundle", type=Path, required=True)
    parser.add_argument("--base-truth-contract", type=Path, required=True)
    parser.add_argument("--truth-extension-contract", type=Path, required=True)
    parser.add_argument("--coverage", type=Path, required=True)
    parser.add_argument("--authority-projection", type=Path, required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw_bundle = load_bounded_json(args.raw_bundle.resolve(strict=True), limit=MAX_JSON_INPUT_BYTES)
    base_truth = load_bounded_json(args.base_truth_contract.resolve(strict=True), limit=MAX_JSON_INPUT_BYTES)
    truth_extension = load_bounded_json(args.truth_extension_contract.resolve(strict=True), limit=MAX_JSON_INPUT_BYTES)
    coverage = load_bounded_json(args.coverage.resolve(strict=True), limit=MAX_JSON_INPUT_BYTES)
    authority = load_bounded_json(args.authority_projection.resolve(strict=True), limit=MAX_JSON_INPUT_BYTES)
    grouped: dict[tuple[str, int], list[dict[str, object]]] = {}
    for receipt in raw_bundle["receipts"]:
        lead = int(str(receipt["lead_bucket"])[1:])
        raw = json.loads(Path(receipt["raw_object_path"]).read_text(encoding="utf-8"))
        variable = f"temperature_2m_previous_day{lead}"
        times = raw["hourly"]["time"]
        values = raw["hourly"][variable]
        rows = [
            {
                "valid_time_utc": f"{stamp}:00Z" if len(stamp) == 16 else f"{stamp}Z",
                "temperature_2m_c": value,
            }
            for stamp, value in zip(times, values, strict=True)
            if value is not None
        ]
        grouped.setdefault((receipt["station_id"], lead), []).append({
            "model_id": receipt["model_id"],
            "provider_id": receipt["provider_id"],
            "source_class": "aggregator_republisher",
            "previous_run_semantics": f"value_predicted_{24 * lead}_hours_before_valid_time",
            "formal_lineage_eligible": False,
            "response_sha256": receipt["raw_object_sha256"],
            "source_url": receipt["source_url"],
            "hourly_temperature_rows": rows,
        })
    evidences = []
    paths = []
    for (station, lead), captures in sorted(grouped.items()):
        identity = {
            "schema": "rkpk_open_meteo_five_model_historical_oos_v1",
            "station_id": station,
            "lead_days": lead,
            "settlement_timezone": "America/New_York",
            "fetched_at_utc": max(
                item["fetched_at_utc"]
                for item in raw_bundle["receipts"]
                if item["station_id"] == station and item["lead_bucket"] == f"d{lead}"
            ),
            "source_contract": {
                "endpoint": "https://previous-runs-api.open-meteo.com/v1/forecast",
                "fixed_lead_semantics": f"predicted_{24 * lead}_hours_before_each_valid_time",
                "native_publication_or_arrival_claimed": False,
            },
            "captures": sorted(captures, key=lambda item: item["model_id"]),
        }
        evidence = {**identity, "content_sha256": content_sha256(identity)}
        path = args.evidence_root.resolve() / station / f"d{lead}.json"
        write_bounded_json(path, evidence)
        evidences.append(evidence)
        paths.append(path)
    conn = _database(base_truth, truth_extension)
    try:
        bundle = build_republisher_historical_event_bundle(
            conn,
            evidences=evidences,
            evidence_paths=paths,
            coverage_rows=coverage["rows"],
            authority_scope_projection=authority,
            as_of_utc=args.as_of_utc,
        )
    finally:
        conn.close()
    write_bounded_json(args.output.resolve(), bundle, limit=64 * 1024 * 1024, max_nodes=2_000_000)
    print({
        "event_count": bundle["event_count"],
        "terminal_count": bundle["terminal_count"],
        "blocker_counts": bundle["blocker_counts"],
        "content_sha256": bundle["content_sha256"],
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
