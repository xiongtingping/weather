#!/usr/bin/env python3
"""Build bounded 45-station five-model calibration candidates.

This orchestrator is deliberately outside the production hot path.  It
retrieves official AWC historical METAR labels, builds one station-local truth
dataset, then runs d1/d2 five-model Previous Runs OOS.  For AMSC-owned
stations, AWC labels require the explicit temporary calibration authority and
remain forbidden from realtime truth, P1 and hard-NO consumers.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import load_bounded_json  # noqa: E402
from bayes_basket_target.station_registry import (  # noqa: E402
    coordinate_registry_manifest,
)


SCHEMA = "bayes_basket_target_45station_five_model_calibration_run_v1"
DEFAULT_LANE_MAP = (
    ROOT / "config/trading/bayes_basket_target_station_lane_map_v1.json"
)
DEFAULT_MARKET_REGISTRY = (
    ROOT / "data/bayes_basket_target_market_registry_snapshot.json"
)
DEFAULT_OUTPUT_ROOT = (
    ROOT / "output/audits/bayes_basket_target_45station_five_model_calibration"
)
DEFAULT_RAW_ROOT = (
    ROOT / "data/research/bayes_basket_target/five_model_45station_truth_raw"
)
MAX_WORKERS = 4
MAX_DAYS = 32
MIN_OOS_SAMPLE_N = 16
MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    raw = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(path.parent).free - len(raw) < MIN_FREE_BYTES:
        raise ContractError("five_model_45station_minimum_free_disk_guard")
    handle = tempfile.NamedTemporaryFile(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_scope(
    lane_map_path: Path,
    registry_path: Path,
) -> dict[str, dict[str, str]]:
    lane_raw = load_bounded_json(lane_map_path.resolve())
    lanes = lane_raw.get("stations") if isinstance(lane_raw, Mapping) else None
    if not isinstance(lanes, Mapping) or len(lanes) != 45:
        raise ContractError("five_model_45station_lane_map_invalid")
    coordinate_ids = set(coordinate_registry_manifest()["station_ids"])
    if set(lanes) != coordinate_ids:
        raise ContractError("five_model_45station_coordinate_scope_mismatch")
    registry = load_bounded_json(registry_path.resolve())
    rows = registry.get("rows") if isinstance(registry, Mapping) else None
    if not isinstance(rows, list):
        raise ContractError("five_model_45station_market_registry_invalid")
    timezones: dict[str, set[str]] = {station: set() for station in lanes}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        station = str(row.get("station_id") or "").strip().upper()
        timezone_name = str(row.get("settlement_timezone") or "").strip()
        if station in timezones and timezone_name:
            timezones[station].add(timezone_name)
    result: dict[str, dict[str, str]] = {}
    for station, lane in sorted(lanes.items()):
        values = timezones[station]
        if len(values) != 1:
            raise ContractError(
                f"five_model_45station_timezone_ambiguous:{station}"
            )
        normalized_lane = str(lane).strip().lower()
        if normalized_lane not in {"amsc", "awc"}:
            raise ContractError("five_model_45station_lane_invalid")
        result[station] = {
            "source_lane": normalized_lane,
            "settlement_timezone": next(iter(values)),
        }
    return result


def _dates(first: str, last: str) -> list[str]:
    start = date.fromisoformat(first)
    end = date.fromisoformat(last)
    if start > end or (end - start).days + 1 > MAX_DAYS:
        raise ContractError("five_model_45station_date_range_invalid")
    result = []
    cursor = start
    while cursor <= end:
        result.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return result


def _run(command: list[str], *, accepted_codes: set[int] = {0}) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=1800,
        check=False,
    )
    if completed.returncode not in accepted_codes:
        raise RuntimeError(
            f"child_failed:{completed.returncode}:"
            f"{completed.stderr[-1000:] or completed.stdout[-1000:]}"
        )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    return json.loads(lines[-1]) if lines else {}


def _truth_slice(
    *,
    station: str,
    scope: Mapping[str, str],
    days: list[str],
    proxy_url: str,
    output_root: Path,
    raw_root: Path,
    resume: bool,
    truth_artifact_root: Path | None = None,
) -> dict[str, Any]:
    station_root = output_root / station
    receipt = station_root / "historical_truth_rebuild.json"
    observations = station_root / "historical_truth_observations.json"
    references = station_root / "historical_truth_references.json"
    dataset = station_root / "calibration_truth_dataset.json"
    lane = scope["source_lane"]
    if truth_artifact_root is not None:
        external = (
            truth_artifact_root.expanduser().resolve()
            / station
            / "calibration_truth_dataset.json"
        )
        payload = load_bounded_json(external)
        if (
            payload.get("station_id") != station
            or payload.get("settlement_timezone")
            != scope["settlement_timezone"]
            or str(payload.get("start_date") or "") > days[0]
            or str(payload.get("end_date") or "") < days[-1]
        ):
            raise ContractError(
                f"five_model_45station_external_truth_invalid:{station}"
            )
        return {
            "station_id": station,
            "state": (
                "TRUTH_READY"
                if len(payload.get("samples") or []) >= MIN_OOS_SAMPLE_N
                else "TRUTH_INSUFFICIENT"
            ),
            "sample_count": len(payload.get("samples") or []),
            "missing_date_count": len(payload.get("missing_dates") or []),
            "label_authority": payload.get("label_authority"),
            "dataset": str(external),
        }
    if not (
        resume
        and dataset.is_file()
        and load_bounded_json(dataset).get("station_id") == station
    ):
        command = [
            sys.executable,
            str(ROOT / "scripts/rebuild_bayes_basket_target_historical_truth.py"),
            "--station-id", station,
            "--skip-amsc-fetch",
            "--proxy-url", proxy_url,
            "--output", str(receipt),
            "--observations-output", str(observations),
            "--reference-output", str(references),
            "--raw-root", str(raw_root),
        ]
        for day in days:
            command.extend(["--event-date", day])
        if lane == "amsc":
            command.extend([
                "--reference-only",
                "--awc-reference-station", station,
            ])
        _run(command, accepted_codes={0, 2})
        dataset_command = [
            sys.executable,
            str(
                ROOT
                / "scripts/build_bayes_basket_target_station_calibration_dataset.py"
            ),
            "--observation-artifact",
            str(references if lane == "amsc" else observations),
            "--station-id", station,
            "--settlement-timezone", scope["settlement_timezone"],
            "--source-lane", "AWC",
            "--start-date", days[0],
            "--end-date", days[-1],
            "--output", str(dataset),
        ]
        if lane == "amsc":
            dataset_command.append("--temporary-awc-calibration-label")
        _run(dataset_command, accepted_codes={0, 2})
    payload = load_bounded_json(dataset)
    return {
        "station_id": station,
        "state": (
            "TRUTH_READY"
            if len(payload.get("samples") or []) >= MIN_OOS_SAMPLE_N
            else "TRUTH_INSUFFICIENT"
        ),
        "sample_count": len(payload.get("samples") or []),
        "missing_date_count": len(payload.get("missing_dates") or []),
        "label_authority": payload.get("label_authority"),
        "dataset": str(dataset.resolve()),
    }


def _model_slice(
    *,
    truth_result: Mapping[str, Any],
    scope: Mapping[str, str],
    days: list[str],
    proxy_url: str,
    output_root: Path,
    resume: bool,
    lead_days: tuple[int, ...] = (1, 2),
    native_ecmwf_evidence: tuple[Path, ...] = (),
    native_gfs_evidence: tuple[Path, ...] = (),
) -> dict[str, Any]:
    station = str(truth_result["station_id"])
    if truth_result["state"] != "TRUTH_READY":
        return {
            "station_id": station,
            "state": "BLOCKED_TRUTH_INSUFFICIENT",
            "leads": {},
        }
    leads: dict[str, Any] = {}
    for lead in lead_days:
        output = output_root / station / f"five_model_d{lead}_oos.json"
        if not (
            resume
            and output.is_file()
            and load_bounded_json(output).get("station_id") == station
        ):
            command = [
                sys.executable,
                str(
                    ROOT
                    / "scripts/build_rkpk_open_meteo_five_model_historical_oos.py"
                ),
                "--truth-artifact", str(truth_result["dataset"]),
                "--station-id", station,
                "--settlement-timezone", scope["settlement_timezone"],
                "--truth-source-lane", "AWC",
                "--start-date", days[0],
                "--end-date", days[-1],
                "--lead-days", str(lead),
                "--proxy-url", proxy_url,
                "--max-response-bytes", str(4 * 1024 * 1024),
                "--max-total-response-bytes", str(16 * 1024 * 1024),
                "--output", str(output),
            ]
            if lead == 2:
                for path in native_ecmwf_evidence:
                    command.extend([
                        "--native-ecmwf-evidence", str(path.resolve())
                    ])
                for path in native_gfs_evidence:
                    command.extend([
                        "--native-gfs-evidence", str(path.resolve())
                    ])
            _run(command)
        payload = load_bounded_json(output)
        oos = payload.get("oos") or {}
        winner = oos.get("research_winner") or {}
        leads[f"d{lead}"] = {
            "state": oos.get("state"),
            "common_sample_n": oos.get("common_sample_n"),
            "exact_join_row_count": payload.get(
                "exact_join", {}
            ).get("row_count"),
            "native_overlay_models": sorted({
                str(item.get("model_id") or "")
                for item in payload.get("native_overlays") or []
                if isinstance(item, Mapping)
            }),
            "formal_oos_selection_contract_valid": payload.get(
                "authority", {}
            ).get("formal_oos_selection_contract_valid"),
            "selected_model_count": len(
                winner.get("selected_models") or []
            ),
            "research_winner": winner or None,
            "output": str(output.resolve()),
        }
    ready = all(
        str(item.get("state") or "").startswith("OOS_RESEARCH_COMPLETE")
        for item in leads.values()
    )
    return {
        "station_id": station,
        "state": "FIVE_MODEL_OOS_READY" if ready else "FIVE_MODEL_OOS_BLOCKED",
        "leads": leads,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--proxy-url", default="http://127.0.0.1:7897")
    parser.add_argument("--lane-map", type=Path, default=DEFAULT_LANE_MAP)
    parser.add_argument(
        "--market-registry", type=Path, default=DEFAULT_MARKET_REGISTRY
    )
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--truth-only", action="store_true")
    parser.add_argument(
        "--truth-artifact-root",
        type=Path,
        help="Reuse already frozen per-station calibration truth datasets.",
    )
    parser.add_argument(
        "--lead-day", type=int, action="append", choices=(1, 2), default=[]
    )
    parser.add_argument(
        "--native-ecmwf-evidence", type=Path, action="append", default=[]
    )
    parser.add_argument(
        "--native-gfs-evidence", type=Path, action="append", default=[]
    )
    args = parser.parse_args()
    if not 1 <= args.workers <= MAX_WORKERS:
        raise ContractError("five_model_45station_worker_budget_invalid")
    output_root = args.output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(output_root).free < MIN_FREE_BYTES:
        raise ContractError("five_model_45station_minimum_free_disk_guard")
    scope = _load_scope(args.lane_map, args.market_registry)
    days = _dates(args.start_date, args.end_date)
    lead_days = tuple(sorted(set(args.lead_day or (1, 2))))
    if (
        (args.native_ecmwf_evidence or args.native_gfs_evidence)
        and lead_days != (2,)
    ):
        raise ContractError(
            "five_model_45station_native_overlay_requires_d2_only"
        )

    truth_results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=args.workers
    ) as pool:
        futures = {
            pool.submit(
                _truth_slice,
                station=station,
                scope=item,
                days=days,
                proxy_url=args.proxy_url,
                output_root=output_root,
                raw_root=args.raw_root.expanduser().resolve(),
                resume=args.resume,
                truth_artifact_root=args.truth_artifact_root,
            ): station
            for station, item in scope.items()
        }
        for future in concurrent.futures.as_completed(futures):
            station = futures[future]
            try:
                truth_results.append(future.result())
            except Exception as exc:
                truth_results.append({
                    "station_id": station,
                    "state": "TRUTH_FAILED",
                    "error": f"{type(exc).__name__}:{str(exc)[:500]}",
                })

    model_results: list[dict[str, Any]] = []
    if not args.truth_only:
        truth_by_station = {
            str(item["station_id"]): item for item in truth_results
        }
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=args.workers
        ) as pool:
            futures = {
                pool.submit(
                    _model_slice,
                    truth_result=truth_by_station[station],
                    scope=item,
                    days=days,
                    proxy_url=args.proxy_url,
                    output_root=output_root,
                    resume=args.resume,
                    lead_days=lead_days,
                    native_ecmwf_evidence=tuple(
                        path.expanduser().resolve()
                        for path in args.native_ecmwf_evidence
                    ),
                    native_gfs_evidence=tuple(
                        path.expanduser().resolve()
                        for path in args.native_gfs_evidence
                    ),
                ): station
                for station, item in scope.items()
            }
            for future in concurrent.futures.as_completed(futures):
                station = futures[future]
                try:
                    model_results.append(future.result())
                except Exception as exc:
                    model_results.append({
                        "station_id": station,
                        "state": "FIVE_MODEL_OOS_FAILED",
                        "error": f"{type(exc).__name__}:{str(exc)[:500]}",
                    })

    identity = {
        "schema": SCHEMA,
        "start_date": days[0],
        "end_date": days[-1],
        "station_count": len(scope),
        "model_ids": ["ECMWF", "GFS", "ICON", "GEM", "CMA"],
        "lead_days": list(lead_days),
        "truth_results": sorted(
            truth_results, key=lambda item: str(item["station_id"])
        ),
        "model_results": sorted(
            model_results, key=lambda item: str(item["station_id"])
        ),
        "checks": {
            "exact_45_station_scope": len(scope) == 45,
            "truth_ready_station_count": sum(
                item.get("state") == "TRUTH_READY"
                for item in truth_results
            ),
            "five_model_oos_ready_station_count": sum(
                item.get("state") == "FIVE_MODEL_OOS_READY"
                for item in model_results
            ),
            "minimum_formal_ensemble_models": 2,
            "formal_oos_selection_valid_station_count": sum(
                all(
                    lead.get("formal_oos_selection_contract_valid") is True
                    and int(lead.get("selected_model_count") or 0) >= 2
                    and int(lead.get("exact_join_row_count") or 0)
                    == int(lead.get("common_sample_n") or 0)
                    for lead in item.get("leads", {}).values()
                )
                for item in model_results
                if item.get("state") == "FIVE_MODEL_OOS_READY"
            ),
            "native_overlay_station_count": sum(
                all(
                    set(lead.get("native_overlay_models") or [])
                    == {"ECMWF", "GFS"}
                    for lead in item.get("leads", {}).values()
                )
                for item in model_results
                if item.get("state") == "FIVE_MODEL_OOS_READY"
            ),
        },
        "authority": {
            "research_candidates_materialized": True,
            "production_no_live_activation_written": False,
            "formal_promotion_written": False,
            "forward_oos_required_before_activation": False,
            "forward_oos_observational_not_blocking": True,
        },
        "boundary": {
            "writes_production_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
        },
    }
    manifest = {**identity, "content_sha256": content_sha256(identity)}
    manifest_path = output_root / "manifest.json"
    _atomic_write(manifest_path, manifest)
    print(json.dumps({
        "schema": SCHEMA,
        **manifest["checks"],
        "manifest": str(manifest_path.resolve()),
    }, ensure_ascii=False, sort_keys=True))
    return 0 if (
        manifest["checks"]["truth_ready_station_count"] == 45
        and (
            args.truth_only
            or (
                manifest["checks"]["five_model_oos_ready_station_count"] == 45
                and manifest["checks"][
                    "formal_oos_selection_valid_station_count"
                ] == 45
                and (
                    not (
                        args.native_ecmwf_evidence
                        or args.native_gfs_evidence
                    )
                    or manifest["checks"][
                        "native_overlay_station_count"
                    ] == 45
                )
            )
        )
    ) else 2


if __name__ == "__main__":
    raise SystemExit(main())
