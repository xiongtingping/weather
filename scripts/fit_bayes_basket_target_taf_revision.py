#!/usr/bin/env python3
"""Fit one strict-time OOS TAF revision artifact from a bounded AWC dataset."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json  # noqa: E402
from bayes_basket_target.taf_calibration import fit_taf_revision_artifact  # noqa: E402
from bayes_basket_target.taf_exact_join import TAF_EXACT_JOIN_SCHEMA  # noqa: E402


def _samples(dataset: dict) -> list[dict]:
    if dataset.get("schema") != TAF_EXACT_JOIN_SCHEMA:
        raise ValueError("TAF calibration dataset schema invalid")
    if dataset.get("source_class") != "strict_oos_crossfit_exact_join":
        raise ValueError("TAF calibration dataset source class invalid")
    if dataset.get("blockers"):
        raise ValueError("TAF calibration dataset has blockers")
    rows: list[dict] = []
    for item in dataset.get("samples", []):
        taf = item["taf"]
        truth = item["truth"]
        window = item["settlement_window"]
        scope = item.get("calibration_scope")
        nwp = item.get("nwp_combination")
        if not isinstance(scope, dict):
            raise ValueError(
                "TAF sample missing calibration_scope; raw TAF+truth capture "
                "must first be exact-joined to the P0-M NWP scope"
            )
        if not isinstance(nwp, dict):
            raise ValueError(
                "TAF sample missing nwp_combination; conditional TAF increment "
                "cannot be fitted without a strict OOS P0-M baseline"
            )
        if truth.get("source_lane") != "AWC":
            raise ValueError("TAF calibration truth lane must be AWC")
        rows.append({
            "sample_id": item["sample_id"],
            "station_id": item["station_id"],
            "event_date": item["event_date"],
            "settlement_timezone": item["settlement_timezone"],
            "season_bin": scope["season_bin"],
            "lead_bucket": scope["lead_bucket"],
            "run_cycle": scope["run_cycle"],
            "settlement_definition": scope["settlement_definition"],
            "nwp_combination_artifact_sha256": (
                nwp["artifact_sha256"]
            ),
            "nwp_combination_decision_at_utc": nwp["decision_at_utc"],
            "nwp_combination_prediction_oos": nwp["prediction_oos"],
            "taf_issued_at_utc": taf["issue_time_utc"],
            "taf_available_at_utc": taf["available_time_utc"],
            "truth_closed_at_utc": window["end_utc"],
            "tx_c": taf["tx_c"],
            "truth_tmax_c": truth["tmax_c"],
            "cloud_cover_fraction": taf["cloud_cover_fraction"],
            "precipitation_probability": taf["precipitation_probability"],
            "wind_speed_mps": taf["wind_speed_mps"],
            "nwp_combined_tmax_c": nwp["combined_tmax_c"],
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--artifact-output", type=Path, required=True)
    parser.add_argument("--scorecard-output", type=Path, required=True)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--activation-id", required=True)
    parser.add_argument("--season-bin", default="summer")
    parser.add_argument("--lead-bucket", default="d0")
    parser.add_argument("--run-cycle", default="18Z")
    parser.add_argument("--settlement-definition", default="official_daily_tmax_c")
    parser.add_argument("--valid-from-utc", required=True)
    parser.add_argument("--valid-until-utc", required=True)
    args = parser.parse_args()

    dataset = load_bounded_json(args.dataset.expanduser().resolve())
    if not isinstance(dataset, dict):
        raise SystemExit("dataset must be a JSON object")
    artifact, scorecard = fit_taf_revision_artifact(
        _samples(dataset),
        artifact_id=args.artifact_id,
        activation_id=args.activation_id,
        season_bin=args.season_bin,
        lead_bucket=args.lead_bucket,
        run_cycle=args.run_cycle,
        settlement_definition=args.settlement_definition,
        valid_from_utc=args.valid_from_utc,
        valid_until_utc=args.valid_until_utc,
    )
    write_bounded_json(args.artifact_output.expanduser().resolve(), artifact.to_dict())
    write_bounded_json(args.scorecard_output.expanduser().resolve(), scorecard)
    print(json.dumps({
        "artifact_id": artifact.artifact_id,
        "artifact_content_sha256": artifact.content_sha256,
        "sample_n": artifact.sample_n,
        "effective_sample_n": artifact.effective_sample_n,
        "oos_metrics": dict(artifact.oos_metrics),
        "artifact_output": str(args.artifact_output.expanduser().resolve()),
        "scorecard_output": str(args.scorecard_output.expanduser().resolve()),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
