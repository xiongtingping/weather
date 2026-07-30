#!/usr/bin/env python3
"""Rebuild NWP P0 and materialize one retrospective, orderless P0-M."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ProbabilityEnvelope, content_sha256  # noqa: E402
from bayes_basket_target.event_contracts import ForecastRevision  # noqa: E402
from bayes_basket_target.materialization import materialize_p0m  # noqa: E402
from bayes_basket_target.database import resolve_target_database  # noqa: E402
from bayes_basket_target.p0_preselection import (  # noqa: E402
    compile_p0_preselection,
    load_market_registry_snapshot,
    load_official_ws_quotes,
)
from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json  # noqa: E402
from bayes_basket_target.runtime_config import (  # noqa: E402
    load_target_no_live_runtime_config,
)
from bayes_basket_target.taf_revision import (  # noqa: E402
    TafRevisionArtifact,
    compile_taf_revision_proof,
)
from scripts.materialize_bayes_basket_target_probability import build_envelope  # noqa: E402
from bayes_basket_target.materialization import insert_probability_envelope  # noqa: E402


SCHEMA = "bayes_basket_p0m_retrospective_replay_v1"


def _mapping(path: Path) -> Mapping[str, Any]:
    value = load_bounded_json(path.expanduser().resolve())
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _bucket_centers(packet: Mapping[str, Any], root: ProbabilityEnvelope) -> dict[str, float]:
    intervals = packet.get("bucket_intervals")
    if not isinstance(intervals, Mapping) or set(intervals) != set(root.probabilities):
        raise ValueError("P0 packet bucket intervals do not match root support")
    centers: dict[str, float] = {}
    for bucket, bounds in intervals.items():
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
            raise ValueError("bucket interval invalid")
        lower, upper = bounds
        if lower is None and upper is None:
            raise ValueError("unbounded bucket interval invalid")
        if lower is None:
            centers[str(bucket)] = float(upper) - 0.5
        elif upper is None:
            centers[str(bucket)] = float(lower) + 0.5
        else:
            centers[str(bucket)] = (float(lower) + float(upper)) / 2.0
    return centers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--p0-packet", type=Path, required=True)
    parser.add_argument("--taf-artifact", type=Path, required=True)
    parser.add_argument("--taf-capture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preselection-db", type=Path)
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--ws-manifest", type=Path)
    parser.add_argument("--evaluation-at-utc")
    parser.add_argument("--maturity-bin", default="d0")
    args = parser.parse_args()
    packet = _mapping(args.p0_packet)
    artifact = TafRevisionArtifact.from_mapping(_mapping(args.taf_artifact))
    capture = _mapping(args.taf_capture)
    if capture.get("schema") != "bayes_basket_awc_taf_revision_capture_v1":
        raise ValueError("TAF capture schema invalid")
    root = build_envelope(packet)
    revision = ForecastRevision.from_mapping(capture["forecast_revision"])
    centers = _bucket_centers(packet, root)
    root_decision = datetime.fromisoformat(
        root.decision_at_utc.replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    decision_at = (
        root_decision + timedelta(seconds=1)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    proof = compile_taf_revision_proof(
        root,
        p0_root=root,
        forecast_revision=revision,
        artifact=artifact,
        bucket_centers_c=centers,
        taf_fields=capture["taf_fields"],
        decision_at_utc=decision_at,
    )
    p0m = materialize_p0m(
        root,
        p0_root=root,
        taf_revision_artifact=artifact,
        taf_revision_proof=proof,
        decision_at_utc=decision_at,
        fresh_until_utc=root.fresh_until_utc,
        bridge_sha256=root.bridge_sha256,
        posterior_version="p0m_taf_physical_confirmation_v3",
    )
    preselection = None
    preselection_args = (
        args.preselection_db,
        args.registry,
        args.ws_manifest,
        args.evaluation_at_utc,
    )
    if any(preselection_args) and not all(preselection_args):
        raise ValueError(
            "preselection-db, registry, ws-manifest and evaluation-at-utc "
            "must be supplied together"
        )
    if all(preselection_args):
        source_path = resolve_target_database(
            args.preselection_db.expanduser().resolve()
        )
        registry = load_market_registry_snapshot(args.registry)
        ws_manifest = _mapping(args.ws_manifest)
        _ws_root, ws_quotes = load_official_ws_quotes(args.ws_manifest)
        source_uri = f"file:{source_path}?mode=ro"
        with sqlite3.connect(source_uri, uri=True, timeout=30.0) as source:
            with sqlite3.connect(":memory:", timeout=30.0) as scratch:
                source.backup(scratch)
                # The source may contain an older checkpoint built before the
                # TAF scope fields became mandatory lineage.  Replace it only
                # inside this disposable in-memory replay; the source remains
                # read-only and untouched.
                scratch.execute(
                    "DROP TRIGGER IF EXISTS basket_probability_envelope_no_delete"
                )
                scratch.execute(
                    "DELETE FROM basket_probability_envelope WHERE envelope_id IN (?,?)",
                    (root.checkpoint_id, p0m.checkpoint_id),
                )
                insert_probability_envelope(
                    scratch, root, created_at_utc=root.decision_at_utc
                )
                insert_probability_envelope(
                    scratch, p0m, created_at_utc=p0m.decision_at_utc
                )
                preselection = compile_p0_preselection(
                    scratch,
                    station_id=root.station_id,
                    event_date=root.event_date,
                    evaluation_at_utc=args.evaluation_at_utc,
                    registry_snapshot=registry,
                    ws_manifest=ws_manifest,
                    ws_quotes=ws_quotes,
                    season_bin=str(root.lineage["season_bin"]),
                    maturity_bin=args.maturity_bin,
                    liquidity_bin="liquid",
                    strategy_version="target-v1",
                    planning_budget_usdc=float(
                        load_target_no_live_runtime_config()[
                            "target_remaining_budget_usdc"
                        ]
                    ),
                )
    identity = {
        "schema": SCHEMA,
        "state": "retrospective_no_live_materialized",
        "p0_root": root.to_dict(),
        "taf_artifact": artifact.to_dict(),
        "taf_capture_sha256": capture["content_sha256"],
        "taf_revision_proof": proof.to_dict(),
        "p0m_envelope": p0m.to_dict(),
        "p0m_preselection": preselection,
        "boundary": {
            "retrospective_replay_only": True,
            "native_local_taf_receipt": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    result = {**identity, "content_sha256": content_sha256(identity)}
    write_bounded_json(args.output.expanduser().resolve(), result)
    print(json.dumps({
        "state": result["state"],
        "p0_checkpoint_id": root.checkpoint_id,
        "p0m_checkpoint_id": p0m.checkpoint_id,
        "mode": proof.mode,
        "center_before": proof.center_before,
        "center_after": proof.center_after,
        "probability_mass_transfer": proof.probability_mass_transfer,
        "output": str(args.output.expanduser().resolve()),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
