#!/usr/bin/env python3
"""Persist forward TAF facts and build native-P0/TAF/truth exact joins."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.native_ensemble_training_facts import (  # noqa: E402
    build_taf_native_exact_join,
    ingest_forward_taf_inbox,
)
from bayes_basket_target.probability_packet import (  # noqa: E402
    build_probability_envelope,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json_if_changed,
)
from bayes_basket_target.ssot import apply_production_ssot_schema  # noqa: E402
from bayes_basket_target.taf_native_ensemble_oos import (  # noqa: E402
    resolve_native_live_contract,
)


DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
DEFAULT_TAF_INBOX = (
    ROOT / "data" / "bayes_basket_target_taf_revision_inbox.json"
)
DEFAULT_P0_INBOX = ROOT / "data" / "bayes_basket_target_p0_inbox.json"
MAX_LIVE_SCOPES = 45


def _discover_live_scopes(
    live_inbox: Mapping[str, Any],
) -> list[tuple[str, str, str, str]]:
    if live_inbox.get("schema") != "bayes_basket_target_p0_inbox_v1":
        raise RuntimeError("native_training_live_p0_inbox_schema_invalid")
    packets = live_inbox.get("packets")
    if not isinstance(packets, list) or len(packets) > 256:
        raise RuntimeError("native_training_live_p0_packets_invalid")
    scopes: set[tuple[str, str, str, str]] = set()
    for wrapper in packets:
        if (
            not isinstance(wrapper, Mapping)
            or not isinstance(wrapper.get("packet"), Mapping)
        ):
            raise RuntimeError("native_training_live_p0_packet_invalid")
        packet = wrapper["packet"]
        if packet.get("probability_pathway") != (
            "CALIBRATED_NATIVE_MEMBERS_CROSS_ENSEMBLE"
        ):
            continue
        envelope = build_probability_envelope(packet)
        lineage = envelope.lineage
        scope = (
            envelope.station_id,
            str(lineage.get("season_bin") or ""),
            str(lineage.get("lead_bucket") or ""),
            str(lineage.get("run_cycle") or ""),
        )
        if any(not value for value in scope):
            raise RuntimeError("native_training_live_scope_incomplete")
        scopes.add(scope)
    if len(scopes) > MAX_LIVE_SCOPES:
        raise RuntimeError("native_training_live_scope_limit_exceeded")
    return sorted(scopes)


def _safe_scope_filename(scope: tuple[str, str, str, str]) -> str:
    return "_".join(
        re.sub(r"[^A-Za-z0-9.-]+", "-", value).strip("-")
        for value in scope
    ) + "_taf_native_exact_join.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--taf-inbox", type=Path, default=DEFAULT_TAF_INBOX)
    parser.add_argument("--live-p0-inbox", type=Path, default=DEFAULT_P0_INBOX)
    parser.add_argument("--station-id")
    parser.add_argument("--season-bin")
    parser.add_argument("--lead-bucket")
    parser.add_argument("--run-cycle")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--all-live-scopes", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--ingest-only", action="store_true")
    args = parser.parse_args()
    db = args.db.expanduser().resolve(strict=True)
    taf_inbox = load_bounded_json(
        args.taf_inbox.expanduser().resolve(strict=True)
    )
    if not isinstance(taf_inbox, dict):
        raise RuntimeError("native_training_taf_inbox_invalid")
    with sqlite3.connect(db) as conn:
        apply_production_ssot_schema(conn)
        taf_receipt = ingest_forward_taf_inbox(conn, taf_inbox)
        conn.commit()
        if args.ingest_only:
            print(json.dumps({
                "state": "COMMITTED",
                "taf_ingress": taf_receipt,
                "exact_join": None,
                "production_no_live": True,
                "calls_order_gateway": False,
                "orders_submitted": 0,
            }, ensure_ascii=False, sort_keys=True))
            return 0
        live_inbox = load_bounded_json(
            args.live_p0_inbox.expanduser().resolve(strict=True)
        )
        if not isinstance(live_inbox, dict):
            raise RuntimeError("native_training_live_p0_inbox_invalid")
        if args.all_live_scopes:
            if args.output_dir is None or any((
                args.station_id,
                args.season_bin,
                args.lead_bucket,
                args.run_cycle,
                args.output,
            )):
                raise RuntimeError(
                    "native_training_all_live_scope_arguments_invalid"
                )
            scopes = _discover_live_scopes(live_inbox)
        else:
            required = (
                args.station_id,
                args.season_bin,
                args.lead_bucket,
                args.run_cycle,
                args.output,
            )
            if any(value is None for value in required) or (
                args.output_dir is not None
            ):
                raise RuntimeError(
                    "native_training_exact_join_scope_required"
                )
            scopes = [(
                str(args.station_id).strip().upper(),
                str(args.season_bin),
                str(args.lead_bucket),
                str(args.run_cycle),
            )]
        results = []
        for station_id, season_bin, lead_bucket, run_cycle in scopes:
            envelope, fingerprint, intervals = resolve_native_live_contract(
                live_inbox,
                station_id=station_id,
                season_bin=season_bin,
                lead_bucket=lead_bucket,
                run_cycle=run_cycle,
            )
            exact_join = build_taf_native_exact_join(
                conn,
                station_id=envelope.station_id,
                season_bin=season_bin,
                lead_bucket=lead_bucket,
                run_cycle=run_cycle,
                settlement_definition=str(
                    envelope.lineage["settlement_definition"]
                ),
                settlement_timezone=envelope.settlement_timezone,
                ensemble_system_ids=[
                    str(value)
                    for value in fingerprint["ensemble_system_ids"]
                ],
                independence_family_ids=[
                    str(value)
                    for value in fingerprint[
                        "independence_family_ids"
                    ]
                ],
                family_weight_caps={
                    str(key): float(value)
                    for key, value in fingerprint[
                        "family_weight_caps"
                    ].items()
                },
                bucket_intervals=intervals,
            )
            output = (
                args.output_dir.expanduser().resolve()
                / _safe_scope_filename((
                    station_id,
                    season_bin,
                    lead_bucket,
                    run_cycle,
                ))
                if args.all_live_scopes
                else args.output.expanduser().resolve()
            )
            wrote = write_bounded_json_if_changed(output, exact_join)
            results.append({
                "station_id": station_id,
                "season_bin": season_bin,
                "lead_bucket": lead_bucket,
                "run_cycle": run_cycle,
                "exact_join_sample_count": exact_join["sample_count"],
                "crossfit_prediction_artifact_sha256": exact_join[
                    "nwp_crossfit_prediction_artifact_sha256"
                ],
                "output": str(output),
                "wrote": wrote,
            })
    print(json.dumps({
        "state": "COMMITTED",
        "taf_ingress": taf_receipt,
        "scope_count": len(results),
        "sample_count": sum(
            int(result["exact_join_sample_count"])
            for result in results
        ),
        "results": results,
        "production_no_live": True,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
