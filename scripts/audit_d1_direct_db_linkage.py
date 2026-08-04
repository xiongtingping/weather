#!/usr/bin/env python3
"""Audit the retained 28-station d1 direct artifacts against SSOT facts.

This command is deliberately a read-only linkage receipt.  It does not fetch
the network, open a production writer, create calibration artifacts, append an
activation, materialize P0/P1, or call an order gateway.  The direct artifact
uses research ``FIXED_24H`` semantics while the SSOT row uses canonical
``PREVIOUS_DAY1`` semantics; both are retained and the mapping is explicit.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json  # noqa: E402


SCHEMA = "bayes_basket_target_d1_direct_db_linkage_v1"
DB_DEFAULT = ROOT / "data/bayes_basket_target_ssot.db"
DIRECT_DEFAULT = ROOT / "output/audits/bayes_basket_target_45station_five_model_29d_direct_20260730_v1"
CLOSEOUT_DEFAULT = ROOT / "output/audits/republisher_fallback_closeout_45station_20260803.json"
OUTPUT_DEFAULT = ROOT / "output/audits/d1_direct_db_linkage_28station_20260803.json"
MODELS = ("CMA", "ECMWF", "GEM", "GFS", "ICON")
MODEL_SET = frozenset(MODELS)
HEX64 = re.compile(r"^[0-9a-f]{64}$")
DATE_START = "2026-07-01"
DATE_END = "2026-07-29"
EXPECTED_DATES = tuple(
    f"2026-07-{day:02d}" for day in range(1, 30)
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _sha(value: Any) -> str:
    return _text(value).lower()


def _sha_ok(value: Any) -> bool:
    return bool(HEX64.fullmatch(_sha(value)))


def _float(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{field}_invalid") from exc
    if not math.isfinite(number):
        raise ContractError(f"{field}_invalid")
    return number


def _same_number(left: Any, right: Any) -> bool:
    try:
        return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=1e-9)
    except (TypeError, ValueError):
        return False


def _path_sha256(path: Path) -> str:
    # Input files are small JSON artifacts.  The SQLite file is intentionally
    # not hashed here: a full 2+ GiB database scan would violate this receipt's
    # bounded audit purpose.  The DB identity is recorded by path/size.
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> tuple[dict[str, Any], str]:
    requested = path.expanduser()
    if requested.is_symlink():
        raise ContractError("d1_linkage_input_symlink_forbidden")
    resolved = requested.resolve(strict=True)
    if not resolved.is_file():
        raise ContractError("d1_linkage_input_path_invalid")
    payload = load_bounded_json(resolved)
    if not isinstance(payload, Mapping):
        raise ContractError("d1_linkage_input_mapping_required")
    return dict(payload), _path_sha256(resolved)


def _connect_readonly(path: Path) -> sqlite3.Connection:
    requested = path.expanduser()
    if requested.is_symlink():
        raise ContractError("d1_linkage_database_symlink_forbidden")
    resolved = requested.resolve(strict=True)
    conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def _scope(closeout: Mapping[str, Any]) -> list[str]:
    if closeout.get("schema") != "bayes_basket_target_republisher_fallback_closeout_v1":
        raise ContractError("d1_linkage_closeout_schema_invalid")
    if closeout.get("d1_station_count") != 28 or closeout.get("d1_blocked_count") != 28:
        raise ContractError("d1_linkage_closeout_scope_invalid")
    if closeout.get("d1_compiled_count") not in (0, None):
        raise ContractError("d1_linkage_closeout_compiled_scope_unexpected")
    rows = closeout.get("rows")
    if not isinstance(rows, list):
        raise ContractError("d1_linkage_closeout_rows_missing")
    selected = sorted({
        _text(row.get("station_id")).upper()
        for row in rows
        if isinstance(row, Mapping)
        and _text(row.get("expected_lead_bucket")) == "d1"
        and _text(row.get("blocker")) == "d1_strict_oos_unavailable"
    })
    # ``blocked_station_ids`` is the aggregate d0+d1 list (33 stations) in
    # the closeout.  For this receipt the d1 rows are the sole authority; the
    # aggregate list is only a containment check.
    declared_all = {_text(item).upper() for item in (closeout.get("blocked_station_ids") or [])}
    if not set(selected).issubset(declared_all) or len(selected) != 28:
        raise ContractError("d1_linkage_closeout_station_scope_mismatch")
    return selected


def _direct_payload(root: Path, station: str) -> tuple[dict[str, Any], str, Path]:
    path = root.expanduser().resolve() / station / "five_model_d1_oos.json"
    payload, file_sha = _load_json(path)
    if _text(payload.get("station_id")).upper() != station:
        raise ContractError("d1_linkage_direct_station_mismatch:" + station)
    if _text(payload.get("schema")) != "rkpk_open_meteo_five_model_historical_oos_v1":
        raise ContractError("d1_linkage_direct_schema_invalid:" + station)
    supplied = _sha(payload.get("content_sha256"))
    if supplied and supplied != content_sha256({key: value for key, value in payload.items() if key != "content_sha256"}):
        raise ContractError("d1_linkage_direct_content_hash_invalid:" + station)
    return payload, file_sha, path


def _direct_rows(payload: Mapping[str, Any], station: str) -> tuple[dict[str, dict[str, Any]], dict[str, str], dict[str, Any]]:
    exact = payload.get("exact_join")
    if not isinstance(exact, Mapping):
        raise ContractError("d1_linkage_direct_exact_join_missing:" + station)
    rows = exact.get("rows")
    if not isinstance(rows, list) or len(rows) != len(EXPECTED_DATES):
        raise ContractError("d1_linkage_direct_exact_join_count_invalid:" + station)
    by_date: dict[str, dict[str, Any]] = {}
    truth_hashes: dict[str, str] = {}
    for item in rows:
        if not isinstance(item, Mapping):
            raise ContractError("d1_linkage_direct_exact_join_row_invalid:" + station)
        event_date = _text(item.get("event_date"))
        if event_date in by_date or event_date not in EXPECTED_DATES:
            raise ContractError("d1_linkage_direct_exact_join_date_invalid:" + station)
        models = item.get("models")
        if not isinstance(models, Mapping) or set(models) != MODEL_SET:
            raise ContractError("d1_linkage_direct_model_set_invalid:" + station)
        for model in MODELS:
            row = models.get(model)
            if not isinstance(row, Mapping):
                raise ContractError("d1_linkage_direct_model_row_invalid:" + station)
            if _text(row.get("source_class")) != "aggregator_republisher":
                raise ContractError("d1_linkage_direct_source_class_invalid:" + station)
        by_date[event_date] = dict(item)
        truth_hashes[event_date] = _sha(item.get("truth_rows_sha256"))
        if not _sha_ok(truth_hashes[event_date]):
            raise ContractError("d1_linkage_direct_truth_hash_invalid:" + station)
    if tuple(sorted(by_date)) != EXPECTED_DATES:
        raise ContractError("d1_linkage_direct_date_window_invalid:" + station)
    captures = payload.get("captures")
    if not isinstance(captures, list):
        raise ContractError("d1_linkage_direct_captures_missing:" + station)
    response_sha_by_model: dict[str, str] = {}
    truth_values_by_date: dict[str, set[float]] = {event_date: set() for event_date in EXPECTED_DATES}
    for capture in captures:
        if not isinstance(capture, Mapping):
            continue
        model = _text(capture.get("model_id")).upper()
        if model not in MODEL_SET:
            continue
        response_sha = _sha(capture.get("response_sha256"))
        if not _sha_ok(response_sha):
            raise ContractError("d1_linkage_direct_response_hash_invalid:" + station)
        response_sha_by_model[model] = response_sha
        daily_rows = capture.get("daily_rows")
        if isinstance(daily_rows, list):
            for daily in daily_rows:
                if not isinstance(daily, Mapping):
                    continue
                event_date = _text(daily.get("event_date"))
                if event_date in truth_values_by_date and daily.get("official_tmax_c") is not None:
                    truth_values_by_date[event_date].add(
                        _float(daily.get("official_tmax_c"), field="direct_capture_truth")
                    )
    if set(response_sha_by_model) != MODEL_SET:
        raise ContractError("d1_linkage_direct_capture_model_set_invalid:" + station)
    winner = (payload.get("oos") or {}).get("research_winner")
    if not isinstance(winner, Mapping):
        raise ContractError("d1_linkage_direct_research_winner_missing:" + station)
    if _text(winner.get("run_cycle")) != "FIXED_24H":
        raise ContractError("d1_linkage_direct_research_cycle_invalid:" + station)
    candidate_scores = (payload.get("oos") or {}).get("candidate_scores")
    expected_masks = {
        tuple(sorted(subset))
        for size in range(1, len(MODELS) + 1)
        for subset in itertools.combinations(MODELS, size)
    }
    actual_masks = {
        tuple(sorted(_text(model).upper() for model in item.get("models", [])))
        for item in (candidate_scores or [])
        if isinstance(item, Mapping)
    }
    if actual_masks != expected_masks:
        raise ContractError("d1_linkage_direct_candidate_partition_invalid:" + station)
    selected_models = tuple(sorted(_text(model).upper() for model in (winner.get("selected_models") or [])))
    if not selected_models or not set(selected_models).issubset(MODEL_SET):
        raise ContractError("d1_linkage_direct_research_winner_model_mask_invalid:" + station)
    return by_date, truth_hashes, {
        "response_sha256_by_model": response_sha_by_model,
        "research_winner": {
            "selected_models": list(winner.get("selected_models") or []),
            "selected_weights": dict(winner.get("selected_weights") or {}),
            "selected_model_set_hash": _sha(winner.get("selected_model_set_hash")),
            "run_cycle": _text(winner.get("run_cycle")),
            "candidate_count": int(winner.get("candidate_count") or 0),
            "candidate_partition_count": len(actual_masks),
            "candidate_partition_complete": True,
            "selection_evidence_sha256": _sha(winner.get("selection_evidence_sha256")),
        },
        "truth_values_by_date": {
            event_date: sorted(values)
            for event_date, values in truth_values_by_date.items()
        },
    }


def _db_rows(conn: sqlite3.Connection, station: str) -> dict[tuple[str, str], sqlite3.Row]:
    rows = conn.execute(
        "SELECT model_run_id,model_id,station_id,target_local_date,run_init_utc,"
        "lead_bucket,maturity_key,run_cycle,raw_tmax_c,provider_id,provider_role,"
        "source_class,aggregator_or_republisher,open_meteo,asof_status,"
        "historical_replay_eligible,prospective_strict_asof_eligible,authority_scope,"
        "source_object_sha256,content_sha256,capture_modes_json "
        "FROM calibration_historical_asof_model_run "
        "WHERE station_id=? AND lead_bucket='d1' ORDER BY target_local_date,model_id",
        (station,),
    ).fetchall()
    result: dict[tuple[str, str], sqlite3.Row] = {}
    for row in rows:
        key = (_text(row[3]), _text(row[1]).upper())
        if key in result:
            raise ContractError("d1_linkage_db_duplicate_model_day:" + station)
        result[key] = row
    return result


def _db_truth(conn: sqlite3.Connection, station: str, event_date: str) -> sqlite3.Row | None:
    rows = conn.execute(
        "SELECT truth_id,station_id,local_date,official_tmax_c,source_lane,"
        "settlement_definition,revision_no,canonical_committed_at_utc,qc_status,"
        "source_payload_sha256,authority_scope,content_sha256 "
        "FROM calibration_station_daily_high_truth WHERE station_id=? AND local_date=? "
        "AND source_lane='AWC' AND qc_status='ACCEPTED' "
        "ORDER BY revision_no DESC, canonical_committed_at_utc DESC, truth_id DESC",
        (station, event_date),
    ).fetchall()
    return rows[0] if rows else None


def _db_residuals(conn: sqlite3.Connection, station: str) -> dict[tuple[str, str], sqlite3.Row]:
    rows = conn.execute(
        "SELECT r.residual_id,r.truth_id,r.model_run_id,r.station_id,r.model_id,"
        "m.target_local_date,r.lead_bucket,r.run_cycle,r.e_y_minus_f_raw_c,"
        "r.qc_status,r.historical_replay_eligible,r.prospective_strict_asof_eligible,"
        "r.authority_scope,r.pair_sha256,r.content_sha256,m.run_init_utc "
        "FROM calibration_model_station_residual r "
        "JOIN calibration_historical_asof_model_run m ON m.model_run_id=r.model_run_id "
        "WHERE r.station_id=? AND r.lead_bucket='d1' "
        "ORDER BY m.target_local_date,r.model_id",
        (station,),
    ).fetchall()
    result: dict[tuple[str, str], sqlite3.Row] = {}
    for row in rows:
        key = (_text(row[5]), _text(row[4]).upper())
        if key in result:
            raise ContractError("d1_linkage_db_duplicate_residual_day:" + station)
        result[key] = row
    return result


def _station_audit(conn: sqlite3.Connection, direct_root: Path, station: str) -> dict[str, Any]:
    payload, direct_file_sha, direct_path = _direct_payload(direct_root, station)
    direct_by_date, direct_truth_hashes, direct_meta = _direct_rows(payload, station)
    db_by_key = _db_rows(conn, station)
    residual_by_key = _db_residuals(conn, station)
    model_rows = []
    model_failures: list[dict[str, Any]] = []
    residual_failures: list[dict[str, Any]] = []
    truth_rows = []
    stale_dates: list[str] = []
    missing_truth_dates: list[str] = []
    truth_value_mismatch_dates: list[str] = []
    expected_count = len(EXPECTED_DATES) * len(MODELS)
    for event_date in EXPECTED_DATES:
        direct_day = direct_by_date[event_date]
        truth = _db_truth(conn, station, event_date)
        if truth is None:
            missing_truth_dates.append(event_date)
        else:
            direct_values = direct_meta["truth_values_by_date"].get(event_date) or []
            if not direct_values:
                raise ContractError("d1_linkage_direct_capture_truth_missing:" + station)
            truth_value = _float(truth[3], field="db_truth")
            if any(not _same_number(value, truth_value) for value in direct_values):
                truth_value_mismatch_dates.append(event_date)
            direct_hash = direct_truth_hashes[event_date]
            db_hash = _sha(truth[9])
            if not _sha_ok(db_hash):
                raise ContractError("d1_linkage_db_truth_hash_invalid:" + station)
            if direct_hash != db_hash:
                stale_dates.append(event_date)
            truth_rows.append({
                "event_date": event_date,
                "truth_id": _text(truth[0]),
                "official_tmax_c": truth_value,
                "source_lane": _text(truth[4]),
                "authority_scope": _text(truth[10]),
                "revision_no": int(truth[6] or 0),
                "source_payload_sha256": db_hash,
                "direct_truth_rows_sha256": direct_hash,
                "truth_hash_status": "MATCH" if direct_hash == db_hash else "STALE_ARTIFACT_TRUTH_HASH",
            })
        for model in MODELS:
            key = (event_date, model)
            db = db_by_key.get(key)
            direct = direct_day["models"][model]
            failures: list[str] = []
            if db is None:
                failures.append("DB_MODEL_DAY_MISSING")
            else:
                if _text(db[5]) != "d1":
                    failures.append("LEAD_BUCKET_MISMATCH")
                if _text(db[7]) != "PREVIOUS_DAY1":
                    failures.append("RUN_CYCLE_NOT_PREVIOUS_DAY1")
                if _text(db[11]) != "aggregator_republisher":
                    failures.append("SOURCE_CLASS_NOT_REPUBLISHER")
                if int(db[12] or 0) != 1 or int(db[13] or 0) != 1:
                    failures.append("REPUBLISHER_FLAGS_INVALID")
                if _text(db[14]) != "HISTORICAL_REPLAY_ONLY":
                    failures.append("ASOF_STATUS_INVALID")
                if int(db[15] or 0) != 1 or int(db[16] or 0) != 0:
                    failures.append("ELIGIBILITY_FLAGS_INVALID")
                if _text(db[17]) != "NO_LIVE_REPLAY_CANDIDATE":
                    failures.append("AUTHORITY_SCOPE_INVALID")
                if not _same_number(db[8], direct.get("forecast_tmax_c")):
                    failures.append("FORECAST_VALUE_MISMATCH")
                expected_source_sha = direct_meta["response_sha256_by_model"][model]
                if _sha(db[18]) != expected_source_sha:
                    failures.append("MODEL_SOURCE_HASH_MISMATCH")
                if not _sha_ok(db[18]) or not _sha_ok(db[19]):
                    failures.append("MODEL_HASH_INVALID")
                residual = residual_by_key.get(key)
                if residual is None:
                    failures.append("DB_RESIDUAL_MISSING")
                else:
                    expected_truth = _db_truth(conn, station, event_date)
                    if (
                        _text(residual[2]) != _text(db[0])
                        or _text(residual[3]).upper() != station
                        or _text(residual[4]).upper() != model
                        or _text(residual[6]) != "d1"
                        or _text(residual[7]) != "PREVIOUS_DAY1"
                        or _text(residual[9]) != "ACCEPTED"
                        or int(residual[10] or 0) != 1
                        or int(residual[11] or 0) != 0
                        or _text(residual[12]) != "NO_LIVE_REPLAY_CANDIDATE"
                        or not _sha_ok(residual[13])
                        or not _sha_ok(residual[14])
                        or expected_truth is None
                        or _text(residual[1]) != _text(expected_truth[0])
                        or not _same_number(
                            residual[8],
                            _float(expected_truth[3], field="db_truth") - _float(db[8], field="db_forecast"),
                        )
                    ):
                        failures.append("RESIDUAL_LINEAGE_INVALID")
            model_rows.append({
                "event_date": event_date,
                "model_id": model,
                "direct_forecast_tmax_c": _float(direct.get("forecast_tmax_c"), field="direct_forecast"),
                "db_forecast_tmax_c": None if db is None else _float(db[8], field="db_forecast"),
                "direct_source_object_sha256": direct_meta["response_sha256_by_model"][model],
                "db_source_object_sha256": None if db is None else _sha(db[18]),
                "db_model_run_id": None if db is None else _text(db[0]),
                "db_run_init_utc": None if db is None else _text(db[4]),
                "db_run_cycle": None if db is None else _text(db[7]),
                "failures": sorted(set(failures)),
            })
            if failures:
                model_failures.append({"event_date": event_date, "model_id": model, "reasons": sorted(set(failures))})
    if len(db_by_key) != expected_count:
        model_failures.append({"event_date": None, "model_id": None, "reasons": [f"DB_D1_ROW_COUNT_{len(db_by_key)}_EXPECTED_{expected_count}"]})
    if len(residual_by_key) != expected_count:
        residual_failures.append({"event_date": None, "model_id": None, "reasons": [f"DB_D1_RESIDUAL_ROW_COUNT_{len(residual_by_key)}_EXPECTED_{expected_count}"]})
    residual_failures.extend(
        item for item in model_failures
        if any(reason in {"DB_RESIDUAL_MISSING", "RESIDUAL_LINEAGE_INVALID"} for reason in item.get("reasons", []))
    )
    direct_winner = direct_meta["research_winner"]
    exact_model_join = not model_failures
    truth_value_join = not missing_truth_dates and not truth_value_mismatch_dates
    candidate_linkage_ready = exact_model_join and truth_value_join
    return {
        "station_id": station,
        "direct_artifact": {
            "path": str(direct_path),
            "file_sha256": direct_file_sha,
            "payload_content_sha256": _sha(payload.get("content_sha256")),
            "research_semantics": "FIXED_24H",
            "research_winner": direct_winner,
        },
        "canonical_db_semantics": {
            "lead_bucket": "d1",
            "run_cycle": "PREVIOUS_DAY1",
            "maturity_key": "D-1_UTC_DATE_TO_TARGET_ET_DAY",
            "authority_scope": "NO_LIVE_REPLAY_CANDIDATE",
            "source_class": "aggregator_republisher",
            "historical_replay_only": True,
            "prospective_strict_asof_eligible": False,
        },
        "run_cycle_mapping": {
            "research_run_cycle": "FIXED_24H",
            "canonical_db_run_cycle": "PREVIOUS_DAY1",
            "mapping_rule": "retained direct previous-day-1 research capture is bound to the SSOT PREVIOUS_DAY1 d1 scope; research label is never overwritten",
        },
        "coverage": {
            "expected_dates": list(EXPECTED_DATES),
            "expected_model_ids": list(MODELS),
            "direct_exact_join_day_count": len(direct_by_date),
            "db_d1_row_count": len(db_by_key),
            "expected_db_d1_row_count": expected_count,
            "model_day_exact_join": exact_model_join,
            "source_object_hash_match": exact_model_join,
            "residual_lineage_exact": not residual_failures,
            "db_d1_residual_row_count": len(residual_by_key),
            "awc_truth_value_match": truth_value_join,
            "missing_truth_dates": missing_truth_dates,
            "truth_value_mismatch_dates": truth_value_mismatch_dates,
            "stale_artifact_truth_hash_dates": stale_dates,
            "stale_artifact_truth_hash_count": len(stale_dates),
        },
        "model_rows": model_rows,
        "truth_rows": truth_rows,
        "model_failures": model_failures,
        "residual_failures": residual_failures,
        "candidate_linkage_state": (
            "READY_WITH_STALE_TRUTH_BINDING" if candidate_linkage_ready and stale_dates
            else "READY_EXACT" if candidate_linkage_ready
            else "BLOCKED"
        ),
        "repair_actions": [
            "preserve_direct_research_fixed_24h_lineage",
            "bind_db_previous_day1_model_and_residual_rows_by_exact_station_model_date_run",
            "re_envelope_truth_binding_to_current_awc_source_payload_sha256" if stale_dates else "no_truth_re_envelope_required",
            "do_not_write_native_arrival_index_or_p1",
            "do_not_create_formal_promoted_activation",
        ],
    }


def build_receipt(*, db: Path, direct_root: Path, closeout: Path) -> dict[str, Any]:
    closeout_payload, closeout_sha = _load_json(closeout)
    stations = _scope(closeout_payload)
    requested_direct_root = direct_root.expanduser()
    if requested_direct_root.is_symlink():
        raise ContractError("d1_linkage_direct_root_symlink_forbidden")
    direct_root = requested_direct_root.resolve(strict=True)
    if not direct_root.is_dir():
        raise ContractError("d1_linkage_direct_root_invalid")
    with _connect_readonly(db) as conn:
        station_rows = [_station_audit(conn, direct_root, station) for station in stations]
    ready = [row for row in station_rows if row["candidate_linkage_state"] != "BLOCKED"]
    exact = [row for row in station_rows if row["candidate_linkage_state"] == "READY_EXACT"]
    stale = [row for row in station_rows if row["coverage"]["stale_artifact_truth_hash_count"]]
    identity = {
        "schema": SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "station_scope": stations,
        "station_count": len(station_rows),
        "ready_station_count": len(ready),
        "exact_ready_station_count": len(exact),
        "ready_with_stale_truth_binding_station_count": len(stale),
        "blocked_station_count": len(station_rows) - len(ready),
        "direct_artifact_root": str(direct_root),
        "closeout_artifact": {"path": str(closeout.expanduser().resolve()), "sha256": closeout_sha},
        "database": {
            "path": str(db.expanduser().resolve()),
            "read_only": True,
            "query_only": True,
            "size_bytes": int(db.expanduser().resolve().stat().st_size),
            "sha256_intentionally_not_computed": True,
        },
        "checks": {
            "all_28_station_scopes_from_closeout": len(station_rows) == 28,
            "all_model_date_joins_exact": len(ready) == 28,
            "all_awc_truth_values_match": all(row["coverage"]["awc_truth_value_match"] for row in station_rows),
            "model_source_hashes_match": all(row["coverage"]["source_object_hash_match"] for row in station_rows),
            "all_residual_lineage_exact": all(row["coverage"]["residual_lineage_exact"] for row in station_rows),
            "stale_truth_hashes_explicitly_recorded": True,
            "research_fixed_24h_never_overwrites_db_previous_day1": all(
                row["run_cycle_mapping"]["research_run_cycle"] == "FIXED_24H"
                and row["run_cycle_mapping"]["canonical_db_run_cycle"] == "PREVIOUS_DAY1"
                for row in station_rows
            ),
        },
        "boundary": {
            "network_fetches": 0,
            "database_mode": "read_only_query_only",
            "production_database_writes": False,
            "writes_production_p0": False,
            "writes_production_p0m": False,
            "writes_production_p1": False,
            "native_arrival_index_written": False,
            "p1_authority_written": False,
            "formal_promotion_written": False,
            "orders_submitted": 0,
            "calls_order_gateway": False,
        },
        "policy": {
            "d1_strict_oos_unavailable_remains_fail_closed_until_separate_oos_audit": True,
            "republisher_is_not_native_arrival": True,
            "republisher_is_not_p1": True,
            "republisher_is_not_formal_live_authority": True,
            "future_data_is_not_oos": True,
        },
        "stations": station_rows,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_DEFAULT)
    parser.add_argument("--direct-root", type=Path, default=DIRECT_DEFAULT)
    parser.add_argument("--closeout", type=Path, default=CLOSEOUT_DEFAULT)
    parser.add_argument("--output", type=Path, default=OUTPUT_DEFAULT)
    args = parser.parse_args(argv)
    receipt = build_receipt(db=args.db, direct_root=args.direct_root, closeout=args.closeout)
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    write_bounded_json(output, receipt)
    print(json.dumps({
        "schema": receipt["schema"],
        "output": str(output),
        "station_count": receipt["station_count"],
        "ready_station_count": receipt["ready_station_count"],
        "exact_ready_station_count": receipt["exact_ready_station_count"],
        "ready_with_stale_truth_binding_station_count": receipt["ready_with_stale_truth_binding_station_count"],
        "blocked_station_count": receipt["blocked_station_count"],
        "content_sha256": receipt["content_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
