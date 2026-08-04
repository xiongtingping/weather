#!/usr/bin/env python3
"""Build bounded 45-station five-model calibration candidates.

This orchestrator is deliberately outside the production hot path.  It
retrieves official AWC historical METAR labels, builds one station-local truth
dataset, then runs d1/d2 five-model Previous Runs OOS.  Only formal AWC
canonical-observation evidence is eligible for the truth dataset.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, time as wall_time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.calibration_ingress import (  # noqa: E402
    HISTORICAL_AWC_REPLAY,
    NO_LIVE_REPLAY,
)
from bayes_basket_target.contracts import (  # noqa: E402
    ContractError,
    assert_timestamp_not_future,
    content_sha256,
)
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
        if normalized_lane != "awc":
            raise ContractError("five_model_45station_lane_not_awc")
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


def _sha256(value: Any, *, label: str) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"{label}_invalid")
    return result


def _utc(value: Any, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{label}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _settlement_window(event_date: str, timezone_name: str) -> tuple[str, str]:
    try:
        local_date = date.fromisoformat(event_date)
        zone = ZoneInfo(timezone_name)
    except (ValueError, KeyError) as exc:
        raise ContractError("five_model_45station_truth_timezone_invalid") from exc
    start = datetime.combine(local_date, wall_time.min, tzinfo=zone)
    end = datetime.combine(local_date + timedelta(days=1), wall_time.min, tzinfo=zone)
    return _utc_text(start), _utc_text(end)


def _validate_formal_awc_truth(
    payload: Mapping[str, Any],
    *,
    station: str,
    scope: Mapping[str, str],
    days: list[str],
) -> None:
    checks = payload.get("checks")
    boundary = payload.get("boundary")
    try:
        as_of = _utc(
            assert_timestamp_not_future(
                str(payload.get("as_of_utc") or ""),
                name="five_model_45station_truth_as_of_utc",
            ),
            label="five_model_45station_truth_as_of_utc",
        )
    except ContractError as exc:
        raise ContractError(
            f"five_model_45station_formal_awc_truth_invalid:{station}"
        ) from exc
    content_sha = _sha256(
        payload.get("content_sha256"),
        label="five_model_45station_truth_content_sha256",
    )
    if content_sha != content_sha256({
        key: value for key, value in payload.items() if key != "content_sha256"
    }):
        raise ContractError(
            f"five_model_45station_formal_awc_truth_invalid:{station}"
        )
    if (
        payload.get("schema")
        != "bayes_basket_station_calibration_dataset_v1"
        or payload.get("station_id") != station
        or payload.get("settlement_timezone") != scope["settlement_timezone"]
        or payload.get("source_lane") != "AWC"
        or payload.get("source_role") != HISTORICAL_AWC_REPLAY
        or payload.get("authority_scope") != NO_LIVE_REPLAY
        or payload.get("label_authority") != "FORMAL_STATION_LANE_TRUTH"
        or payload.get("start_date") != days[0]
        or payload.get("end_date") != days[-1]
        or not isinstance(checks, Mapping)
        or not isinstance(boundary, Mapping)
        or checks.get("closed_days_only") is not True
        or checks.get("formal_awc_truth_only") is not True
        or checks.get("awc_hourly_metar_and_speci_only") is not True
        or checks.get("historical_awc_replay_only") is not True
        or checks.get("input_evidence_content_hashes_verified") is not True
        or checks.get("settlement_timezone_exact_match") is not True
        or checks.get("reference_only_excluded") is not True
        or checks.get("complete_date_coverage") is not True
        or payload.get("missing_dates") != []
        or any(
            boundary.get(key) is not False
            for key in (
                "writes_production_database",
                "calls_order_gateway",
                "places_orders",
                "live_submit_authorized",
                "enters_realtime_truth",
                "enters_p1",
                "enters_hard_no",
            )
        )
    ):
        raise ContractError(
            f"five_model_45station_formal_awc_truth_invalid:{station}"
        )
    evidence_shas = payload.get("input_evidence_sha256s")
    samples = payload.get("samples")
    if (
        not isinstance(evidence_shas, list)
        or not evidence_shas
        or len(set(evidence_shas)) != len(evidence_shas)
        or any(
            _sha256(value, label="five_model_45station_input_evidence_sha")
            != str(value).lower()
            for value in evidence_shas
        )
        or not isinstance(samples, list)
        or len(samples) != len(days)
    ):
        raise ContractError(
            f"five_model_45station_formal_awc_truth_invalid:{station}"
        )
    seen_dates: set[str] = set()
    seen_observations: set[tuple[str, str, str]] = set()
    for sample in samples:
        if not isinstance(sample, Mapping):
            raise ContractError(
                f"five_model_45station_formal_awc_truth_invalid:{station}"
            )
        event_date = str(sample.get("event_date") or "")
        if (
            event_date not in days
            or event_date in seen_dates
            or sample.get("station_id") != station
            or sample.get("settlement_timezone")
            != scope["settlement_timezone"]
            or sample.get("sample_id") != "truth-" + content_sha256({
                key: value for key, value in sample.items()
                if key != "sample_id"
            })[:32]
        ):
            raise ContractError(
                f"five_model_45station_formal_awc_truth_invalid:{station}"
            )
        seen_dates.add(event_date)
        expected_start, expected_end = _settlement_window(
            event_date, scope["settlement_timezone"]
        )
        window = sample.get("settlement_window")
        truth = sample.get("truth")
        if (
            not isinstance(window, Mapping)
            or window.get("start_utc") != expected_start
            or window.get("end_utc") != expected_end
            or as_of < _utc(
                expected_end,
                label="five_model_45station_window_end",
            )
            or not isinstance(truth, Mapping)
            or truth.get("source_lane") != "AWC"
            or truth.get("source_role") != HISTORICAL_AWC_REPLAY
            or truth.get("authority_scope") != NO_LIVE_REPLAY
            or not isinstance(truth.get("observations"), list)
            or not truth["observations"]
        ):
            raise ContractError(
                f"five_model_45station_formal_awc_truth_invalid:{station}"
            )
        temperatures: list[float] = []
        for observation in truth["observations"]:
            if not isinstance(observation, Mapping):
                raise ContractError(
                    f"five_model_45station_formal_awc_truth_invalid:{station}"
                )
            report_type = str(observation.get("report_type") or "").upper()
            valid_at = _utc(
                observation.get("valid_at_utc"),
                label="five_model_45station_observation_valid_at",
            )
            raw_sha = _sha256(
                observation.get("raw_sha256"),
                label="five_model_45station_observation_raw_sha",
            )
            if (
                observation.get("station_id") != station
                or observation.get("source_lane") != "AWC"
                or observation.get("source_role") != HISTORICAL_AWC_REPLAY
                or observation.get("authority_scope") != NO_LIVE_REPLAY
                or observation.get("settlement_timezone")
                != scope["settlement_timezone"]
                or observation.get("evidence_role") != "FORMAL"
                or observation.get("reference_only") is not False
                or observation.get("qc_status") != "ACCEPTED"
                or report_type not in {"METAR", "SPECI"}
                or (
                    report_type == "METAR"
                    and (valid_at.minute != 0 or valid_at.second != 0)
                )
                or not (
                    _utc(window["start_utc"], label="five_model_45station_window_start")
                    <= valid_at
                    < _utc(window["end_utc"], label="five_model_45station_window_end")
                )
            ):
                raise ContractError(
                    f"five_model_45station_formal_awc_truth_invalid:{station}"
                )
            observation_key = (report_type, _utc_text(valid_at), raw_sha)
            if observation_key in seen_observations:
                raise ContractError(
                    f"five_model_45station_formal_awc_truth_invalid:{station}"
                )
            seen_observations.add(observation_key)
            try:
                temperature = float(observation.get("reported_temperature_c"))
            except (TypeError, ValueError) as exc:
                raise ContractError(
                    f"five_model_45station_formal_awc_truth_invalid:{station}"
                ) from exc
            if not math.isfinite(temperature):
                raise ContractError(
                    f"five_model_45station_formal_awc_truth_invalid:{station}"
                )
            temperatures.append(temperature)
        try:
            tmax = float(truth.get("tmax_c"))
        except (TypeError, ValueError) as exc:
            raise ContractError(
                f"five_model_45station_formal_awc_truth_invalid:{station}"
            ) from exc
        if not math.isfinite(tmax) or tmax != max(temperatures):
            raise ContractError(
                f"five_model_45station_formal_awc_truth_invalid:{station}"
            )
    if seen_dates != set(days):
        raise ContractError(
            f"five_model_45station_formal_awc_truth_invalid:{station}"
        )


def _truth_slice(
    *,
    station: str,
    scope: Mapping[str, str],
    days: list[str],
    proxy_url: str,
    truth_transport: str,
    output_root: Path,
    raw_root: Path,
    resume: bool,
    truth_artifact_root: Path | None = None,
) -> dict[str, Any]:
    station_root = output_root / station
    receipt = station_root / "historical_truth_rebuild.json"
    observations = station_root / "historical_truth_observations.json"
    dataset = station_root / "calibration_truth_dataset.json"
    if truth_artifact_root is not None:
        external = (
            truth_artifact_root.expanduser().resolve()
            / station
            / "calibration_truth_dataset.json"
        )
        payload = load_bounded_json(external)
        _validate_formal_awc_truth(
            payload, station=station, scope=scope, days=days
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
            "--transport", truth_transport,
            "--output", str(receipt),
            "--observations-output", str(observations),
            "--raw-root", str(raw_root),
        ]
        for day in days:
            command.extend(["--event-date", day])
        _run(command, accepted_codes={0, 2})
        dataset_command = [
            sys.executable,
            str(
                ROOT
                / "scripts/build_bayes_basket_target_station_calibration_dataset.py"
            ),
            "--observation-artifact",
            str(observations),
            "--station-id", station,
            "--settlement-timezone", scope["settlement_timezone"],
            "--source-lane", "AWC",
            "--start-date", days[0],
            "--end-date", days[-1],
            "--output", str(dataset),
        ]
        _run(dataset_command, accepted_codes={0, 2})
    payload = load_bounded_json(dataset)
    _validate_formal_awc_truth(
        payload, station=station, scope=scope, days=days
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
    parser.add_argument("--proxy-url", default="http://127.0.0.1:1082")
    parser.add_argument(
        "--truth-transport",
        choices=("configured_proxy", "direct"),
        default="configured_proxy",
        help="Transport used only for official AWC historical truth repair.",
    )
    parser.add_argument("--lane-map", type=Path, default=DEFAULT_LANE_MAP)
    parser.add_argument(
        "--market-registry", type=Path, default=DEFAULT_MARKET_REGISTRY
    )
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument(
        "--station-id",
        action="append",
        default=[],
        help=(
            "Restrict a bounded repair run to registered stations; repeatable. "
            "The canonical 45-station registry is still validated first."
        ),
    )
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
    canonical_scope = _load_scope(args.lane_map, args.market_registry)
    requested_stations = sorted({
        str(item).strip().upper()
        for item in args.station_id
        if str(item).strip()
    })
    unknown_stations = sorted(set(requested_stations) - set(canonical_scope))
    if unknown_stations:
        raise ContractError(
            "five_model_45station_repair_scope_invalid:"
            + ",".join(unknown_stations)
        )
    scope = (
        {
            station: canonical_scope[station]
            for station in requested_stations
        }
        if requested_stations
        else canonical_scope
    )
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
                truth_transport=args.truth_transport,
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
            "exact_45_station_scope": len(canonical_scope) == 45,
            "selected_station_count": len(scope),
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
        manifest["checks"]["truth_ready_station_count"] == len(scope)
        and (
            args.truth_only
            or (
                manifest["checks"]["five_model_oos_ready_station_count"] == len(scope)
                and manifest["checks"][
                    "formal_oos_selection_valid_station_count"
                ] == len(scope)
                and (
                    not (
                        args.native_ecmwf_evidence
                        or args.native_gfs_evidence
                    )
                    or manifest["checks"][
                        "native_overlay_station_count"
                    ] == len(scope)
                )
            )
        )
    ) else 2


if __name__ == "__main__":
    raise SystemExit(main())
