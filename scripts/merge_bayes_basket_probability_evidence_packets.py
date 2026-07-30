#!/usr/bin/env python3
"""Merge bounded probability evidence packets without writing any database."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.resource_bounds import (
    load_bounded_json,
    write_bounded_json,
)
from scripts.ingest_bayes_basket_probability_evidence import (
    MAX_PACKET_ROWS,
    SCHEMA,
)


MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 4 * 1024 * 1024


def _validated_packet(path: Path) -> Mapping[str, Any]:
    resolved = path.expanduser().resolve()
    if (
        not resolved.is_file()
        or resolved.is_symlink()
        or resolved.stat().st_size <= 0
        or resolved.stat().st_size > MAX_INPUT_BYTES
    ):
        raise ContractError(
            "probability_evidence_merge_input_resource_limit_exceeded"
        )
    packet = load_bounded_json(resolved, limit=MAX_INPUT_BYTES)
    if not isinstance(packet, Mapping) or packet.get("schema") != SCHEMA:
        raise ContractError(
            "probability_evidence_merge_packet_schema_invalid"
        )
    identity = dict(packet)
    supplied_sha = str(identity.pop("packet_sha256", "")).lower()
    if supplied_sha != content_sha256(identity):
        raise ContractError(
            "probability_evidence_merge_packet_sha256_mismatch"
        )
    return packet


def merge_packets(
    packets: list[Mapping[str, Any]],
    *,
    station_id: str,
) -> dict[str, Any]:
    station = str(station_id or "").strip().upper()
    if not station or not packets:
        raise ContractError(
            "probability_evidence_merge_scope_invalid"
        )
    rows = {
        "artifacts": [],
        "promotions": [],
        "activations": [],
    }
    id_fields = {
        "artifacts": "artifact_id",
        "promotions": "promotion_id",
        "activations": "activation_id",
    }
    seen = {key: set() for key in rows}
    for packet in packets:
        for key in rows:
            values = packet.get(key)
            if not isinstance(values, list):
                raise ContractError(
                    "probability_evidence_merge_rows_invalid"
                )
            for row in values:
                if not isinstance(row, Mapping):
                    raise ContractError(
                        "probability_evidence_merge_rows_invalid"
                    )
                row_id = str(row.get(id_fields[key]) or "").strip()
                if not row_id or row_id in seen[key]:
                    raise ContractError(
                        f"probability_evidence_merge_{key}_duplicate"
                    )
                if key != "promotions" and (
                    str(row.get("station_id") or "").upper() != station
                ):
                    raise ContractError(
                        "probability_evidence_merge_station_scope_mismatch"
                    )
                seen[key].add(row_id)
                rows[key].append(dict(row))
    if sum(len(value) for value in rows.values()) > MAX_PACKET_ROWS:
        raise ContractError(
            "probability_evidence_merge_row_limit_exceeded"
        )
    artifact_ids = {
        str(row["artifact_id"]) for row in rows["artifacts"]
    }
    if any(
        str(row.get("artifact_id") or "") not in artifact_ids
        for key in ("promotions", "activations")
        for row in rows[key]
    ):
        raise ContractError(
            "probability_evidence_merge_artifact_binding_missing"
        )
    for key in rows:
        rows[key].sort(
            key=lambda row: (
                str(row.get("artifact_kind") or ""),
                str(row.get(id_fields[key]) or ""),
            )
        )
    identity = {"schema": SCHEMA, **rows}
    return {
        **identity,
        "packet_sha256": content_sha256(identity),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", type=Path, action="append", required=True)
    parser.add_argument("--station-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    merged = merge_packets(
        [_validated_packet(path) for path in args.packet],
        station_id=args.station_id,
    )
    write_bounded_json(
        args.output.expanduser().resolve(),
        merged,
        limit=MAX_OUTPUT_BYTES,
    )
    print(merged["packet_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
