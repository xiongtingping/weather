#!/usr/bin/env python3
"""Read-only d1 historical strict-OOS candidate probe.

This probe consumes facts already present in the target SSOT.  It never
downloads model data, writes SQLite, appends an artifact/activation, or opens
the order gateway.  Open-Meteo ``previous-runs`` rows are kept explicitly as
historical republisher evidence; they are not native arrival or live
authority.

The probe is intentionally deterministic and bounded to the 45-station
target registry.  It reports whether each station has enough exact
``d1``/``PREVIOUS_DAY1`` facts for a 20-train/8-holdout candidate and emits
the immutable lineage pointers needed by a later reviewed contract binding.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256, model_set_hash  # noqa: E402


SCHEMA = "d1_historical_strict_candidate_probe_v1"
LEAD_BUCKET = "d1"
RUN_CYCLE = "PREVIOUS_DAY1"
SETTLEMENT_DEFINITION = "official_daily_tmax_c"
TRAIN_DAYS = 20
HOLDOUT_DAYS = 8
EXPECTED_MODELS = ("CMA", "ECMWF", "GEM", "GFS", "ICON")
EXPECTED_MODEL_SET = frozenset(EXPECTED_MODELS)
NO_LIVE_SCOPE = "NO_LIVE_REPLAY_CANDIDATE"
TRUTH_AUTHORITY_SCOPES = frozenset({
    "FORMAL_NATIVE_ELIGIBLE",
    "NO_LIVE_REPLAY_CANDIDATE",
})
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc(raw: Any, *, field: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _normal_utc(raw: Any) -> str | None:
    parsed = _utc(raw, field="timestamp")
    return None if parsed is None else parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def _date(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw or "").strip())
    except ValueError:
        return None


def _sha_ok(raw: Any) -> bool:
    return bool(HEX64.fullmatch(str(raw or "").strip().lower()))


def _connect(path: Path) -> sqlite3.Connection:
    requested = path.expanduser()
    if requested.is_symlink():
        raise ValueError("d1_probe_db_symlink_forbidden")
    resolved = requested.resolve(strict=True)
    conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def _market_timezone(conn: sqlite3.Connection, station: str) -> tuple[str | None, str | None]:
    rows = conn.execute(
        "SELECT settlement_timezone FROM basket_target_market_registry "
        "WHERE station_id=? AND TRIM(settlement_timezone)<>''",
        (station,),
    ).fetchall()
    values = sorted({str(row[0]).strip() for row in rows})
    if len(values) != 1:
        return None, "MARKET_TIMEZONE_NOT_UNIQUE"
    try:
        ZoneInfo(values[0])
    except ZoneInfoNotFoundError:
        return None, "MARKET_TIMEZONE_INVALID"
    binding = conn.execute(
        "SELECT settlement_timezone,market_registry_timezone_count "
        "FROM calibration_station_market_timezone_binding "
        "WHERE station_id=?",
        (station,),
    ).fetchone()
    if binding is not None and (
        str(binding[0]).strip() != values[0]
        or int(binding[1] or 0) != 1
    ):
        return None, "MARKET_TIMEZONE_BINDING_MISMATCH"
    return values[0], None


def _truth_by_day(
    conn: sqlite3.Connection,
    *,
    station: str,
    as_of: datetime,
    timezone_name: str,
) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT truth_id,local_date,official_tmax_c,revision_no,"
        "canonical_committed_at_utc,source_lane,qc_status,authority_scope,"
        "source_role,content_sha256 FROM calibration_station_daily_high_truth "
        "WHERE station_id=? AND source_lane='AWC' AND qc_status='ACCEPTED' "
        "AND authority_scope IN ('FORMAL_NATIVE_ELIGIBLE',"
        "'NO_LIVE_REPLAY_CANDIDATE')",
        (station,),
    ).fetchall()
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return {}
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        day = _date(row[1])
        committed = _utc(row[4], field="canonical_committed_at_utc")
        if day is None or committed is None or committed > as_of:
            continue
        grouped[day.isoformat()].append(row)
    output: dict[str, dict[str, Any]] = {}
    for day_text, candidates in grouped.items():
        # The loader's as-of rule is latest revision visible at the cutoff;
        # committed time breaks a malformed duplicate revision tie.
        selected = max(
            candidates,
            key=lambda row: (
                int(row[3] or 0),
                _utc(row[4], field="canonical_committed_at_utc") or datetime.min.replace(tzinfo=timezone.utc),
            ),
        )
        day = _date(day_text)
        if day is None:
            continue
        day_start = datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc)
        day_end = day_start + timedelta(days=1)
        committed = _utc(selected[4], field="canonical_committed_at_utc")
        if committed is None:
            continue
        output[day_text] = {
            "truth_id": str(selected[0]),
            "official_tmax_c": float(selected[2]),
            "revision_no": int(selected[3] or 0),
            "canonical_committed_at_utc": _normal_utc(selected[4]),
            "source_lane": str(selected[5]),
            "qc_status": str(selected[6]),
            "authority_scope": str(selected[7]),
            "source_role": str(selected[8]),
            "content_sha256": str(selected[9]),
            "station_day_end_utc": day_end.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "closed_at_or_after_day_end": committed >= day_end,
            "committed_at_or_before_as_of": committed <= as_of,
        }
    return output


def _station_registry(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        "SELECT station_id,timezone_name,source_lane,settlement_definition "
        "FROM calibration_station_registry ORDER BY station_id"
    ).fetchall()
    return {str(row[0]).strip().upper(): row for row in rows}


def _target_stations(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT station_id FROM basket_target_market_registry "
        "WHERE TRIM(station_id)<>'' ORDER BY station_id"
    ).fetchall()
    return sorted({str(row[0]).strip().upper() for row in rows})


def _load_model_rows(
    conn: sqlite3.Connection,
    *,
    station: str,
    as_of: datetime,
) -> tuple[dict[str, dict[str, sqlite3.Row]], dict[str, list[str]]]:
    rows = conn.execute(
        "SELECT model_run_id,model_id,target_local_date,run_init_utc,"
        "lead_bucket,run_cycle,raw_tmax_c,provider_id,source_class,"
        "aggregator_or_republisher,open_meteo,asof_status,"
        "historical_replay_eligible,prospective_strict_asof_eligible,"
        "authority_scope,source_object_sha256,content_sha256 "
        "FROM calibration_historical_asof_model_run "
        "WHERE station_id=? AND lead_bucket='d1' "
        "ORDER BY target_local_date,model_id,model_run_id",
        (station,),
    ).fetchall()
    by_day: dict[str, dict[str, sqlite3.Row]] = defaultdict(dict)
    anomalies: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        day = str(row[2])
        model = str(row[1]).strip().upper()
        if model in by_day[day]:
            anomalies[day].append("DUPLICATE_MODEL_RUN")
            continue
        by_day[day][model] = row
        reasons: list[str] = []
        if str(row[5]) != RUN_CYCLE:
            reasons.append("RUN_CYCLE_NOT_PREVIOUS_DAY1")
        run_init = _utc(row[3], field="run_init_utc")
        parsed_day = _date(day)
        expected_init = (
            datetime.combine(parsed_day - timedelta(days=1), time.min, tzinfo=timezone.utc)
            if parsed_day is not None else None
        )
        if run_init is None or expected_init is None or run_init != expected_init:
            reasons.append("RUN_INIT_NOT_PREVIOUS_DAY_00Z")
        if run_init is None or run_init > as_of:
            reasons.append("MODEL_RUN_AFTER_AS_OF")
        if str(row[4]) != LEAD_BUCKET:
            reasons.append("LEAD_BUCKET_MISMATCH")
        if str(row[8]) != "aggregator_republisher":
            reasons.append("SOURCE_CLASS_NOT_REPUBLISHER")
        if int(row[9] or 0) != 1 or int(row[10] or 0) != 1:
            reasons.append("REPUBLISHER_FLAGS_MISSING")
        if str(row[11]) != "HISTORICAL_REPLAY_ONLY":
            reasons.append("ASOF_STATUS_NOT_HISTORICAL_REPLAY")
        if int(row[12] or 0) != 1 or int(row[13] or 0) != 0:
            reasons.append("HISTORICAL_ELIGIBILITY_FLAGS_INVALID")
        if str(row[14]) != NO_LIVE_SCOPE:
            reasons.append("AUTHORITY_SCOPE_NOT_NO_LIVE")
        if not _sha_ok(row[16]) or not _sha_ok(row[15]):
            reasons.append("MODEL_CONTENT_HASH_INVALID")
        if reasons:
            anomalies[day].extend(reasons)
    return dict(by_day), {day: sorted(set(values)) for day, values in anomalies.items()}


def _load_residuals(
    conn: sqlite3.Connection,
    *,
    station: str,
) -> dict[str, list[sqlite3.Row]]:
    rows = conn.execute(
        "SELECT r.residual_id,r.model_run_id,r.model_id,r.station_id,"
        "m.target_local_date,r.lead_bucket,r.run_cycle,r.paired_asof_utc,"
        "r.qc_status,r.historical_replay_eligible,"
        "r.prospective_strict_asof_eligible,r.authority_scope,"
        "r.pair_sha256,r.content_sha256,m.run_init_utc,r.truth_id "
        "FROM calibration_model_station_residual r "
        "JOIN calibration_historical_asof_model_run m "
        "ON m.model_run_id=r.model_run_id "
        "WHERE r.station_id=? AND r.lead_bucket='d1'",
        (station,),
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[str(row[1])].append(row)
    return dict(grouped)


def _lineage_digest(rows: list[Mapping[str, Any]]) -> str:
    return content_sha256([
        {
            "event_date": str(row["event_date"]),
            "model_id": str(row["model_id"]),
            "model_run_id": str(row["model_run_id"]),
            "model_content_sha256": str(row["model_content_sha256"]),
            "residual_id": str(row["residual_id"]),
            "residual_content_sha256": str(row["residual_content_sha256"]),
            "truth_id": str(row["truth_id"]),
            "truth_revision_no": int(row["truth_revision_no"]),
            "truth_content_sha256": str(row["truth_content_sha256"]),
        }
        for row in rows
    ])


def _d1_activation_binding(
    conn: sqlite3.Connection,
    *,
    station: str,
    season_bin: str,
) -> dict[str, Any]:
    """Check the append-only no-live calibration root for one d1 scope."""

    model_count = int(conn.execute(
        "SELECT COUNT(*) FROM calibration_model_artifact "
        "WHERE station_id=? AND season_bin=? AND lead_bucket='d1' "
        "AND run_cycle='PREVIOUS_DAY1' AND settlement_definition=?",
        (station, season_bin, SETTLEMENT_DEFINITION),
    ).fetchone()[0])
    selection_count = int(conn.execute(
        "SELECT COUNT(*) FROM calibration_ensemble_selection_evidence "
        "WHERE station_id=? AND season_bin=? AND lead_bucket='d1' "
        "AND run_cycle='PREVIOUS_DAY1' AND settlement_definition=?",
        (station, season_bin, SETTLEMENT_DEFINITION),
    ).fetchone()[0])
    ensemble_count = int(conn.execute(
        "SELECT COUNT(*) FROM calibration_ensemble_artifact "
        "WHERE station_id=? AND season_bin=? AND lead_bucket='d1' "
        "AND run_cycle='PREVIOUS_DAY1' AND settlement_definition=?",
        (station, season_bin, SETTLEMENT_DEFINITION),
    ).fetchone()[0])
    # Activation is append-only.  Count only the latest generation for this
    # business scope; counting all historical generations would make a valid
    # superseding root look invalid after a truth-revision rebind.
    activation_scope = (
        station,
        season_bin,
        SETTLEMENT_DEFINITION,
    )
    model_rows = conn.execute(
        "SELECT model_id_or_star,generation,artifact_id FROM calibration_activation "
        "WHERE station_id=? AND artifact_kind='MODEL_CALIBRATION' "
        "AND season_bin=? AND lead_bucket='d1' AND run_cycle='PREVIOUS_DAY1' "
        "AND settlement_definition=? AND authority_scope='PRODUCTION_NO_LIVE_ONLY' "
        "ORDER BY generation DESC,model_id_or_star,activation_id",
        activation_scope,
    ).fetchall()
    ensemble_rows = conn.execute(
        "SELECT available_model_set_hash_or_star,generation,artifact_id FROM calibration_activation "
        "WHERE station_id=? AND artifact_kind='MODEL_ENSEMBLE' "
        "AND model_id_or_star='*' AND season_bin=? AND lead_bucket='d1' "
        "AND run_cycle='PREVIOUS_DAY1' AND settlement_definition=? "
        "AND authority_scope='PRODUCTION_NO_LIVE_ONLY' "
        "ORDER BY generation DESC,available_model_set_hash_or_star,activation_id",
        activation_scope,
    ).fetchall()
    model_generation = max((int(row[1]) for row in model_rows), default=0)
    ensemble_generation = max((int(row[1]) for row in ensemble_rows), default=0)
    latest_model_rows = [row for row in model_rows if int(row[1]) == model_generation]
    latest_ensemble_rows = [row for row in ensemble_rows if int(row[1]) == ensemble_generation]
    model_ids = {str(row[0]) for row in latest_model_rows}
    ensemble_masks = {str(row[0]) for row in latest_ensemble_rows}
    # A latest root must point at artifacts that actually exist in the same
    # d1 scope.  This catches stale pointers without mutating old history.
    model_artifact_ids = {
        str(row[0])
        for row in conn.execute(
            "SELECT calibration_artifact_id FROM calibration_model_artifact "
            "WHERE station_id=? AND season_bin=? AND lead_bucket='d1' "
            "AND run_cycle='PREVIOUS_DAY1' AND settlement_definition=?",
            activation_scope,
        ).fetchall()
    }
    ensemble_artifact_ids = {
        str(row[0])
        for row in conn.execute(
            "SELECT ensemble_artifact_id FROM calibration_ensemble_artifact "
            "WHERE station_id=? AND season_bin=? AND lead_bucket='d1' "
            "AND run_cycle='PREVIOUS_DAY1' AND settlement_definition=?",
            activation_scope,
        ).fetchall()
    }
    expected_masks = {
        model_set_hash(subset)
        for size in range(2, len(EXPECTED_MODEL_SET) + 1)
        for subset in __import__("itertools").combinations(sorted(EXPECTED_MODEL_SET), size)
    }
    latest_model_artifact_ids = {str(row[2]) for row in latest_model_rows}
    latest_ensemble_artifact_ids = {str(row[2]) for row in latest_ensemble_rows}
    latest_ensemble_rows_by_id = []
    if latest_ensemble_artifact_ids:
        placeholders = ",".join("?" for _ in latest_ensemble_artifact_ids)
        latest_ensemble_rows_by_id = conn.execute(
            "SELECT ensemble_artifact_id,selection_evidence_sha256 "
            "FROM calibration_ensemble_artifact "
            "WHERE ensemble_artifact_id IN (" + placeholders + ")",
            tuple(sorted(latest_ensemble_artifact_ids)),
        ).fetchall()
    selection_hashes = {
        str(row[0])
        for row in conn.execute(
            "SELECT content_sha256 FROM calibration_ensemble_selection_evidence "
            "WHERE station_id=? AND season_bin=? AND lead_bucket='d1' "
            "AND run_cycle='PREVIOUS_DAY1' AND settlement_definition=?",
            activation_scope,
        ).fetchall()
    }
    latest_selection_pointer_ok = (
        len(latest_ensemble_artifact_ids) == 1
        and len(latest_ensemble_rows_by_id) == 1
        and str(latest_ensemble_rows_by_id[0][1]) in selection_hashes
    )
    latest_model_pointer_ok = (
        len(latest_model_rows) == len(EXPECTED_MODEL_SET)
        and model_ids == EXPECTED_MODEL_SET
        and latest_model_artifact_ids.issubset(model_artifact_ids)
    )
    latest_ensemble_pointer_ok = (
        len(latest_ensemble_rows) == len(expected_masks)
        and ensemble_masks == expected_masks
        and latest_ensemble_artifact_ids.issubset(ensemble_artifact_ids)
        and latest_selection_pointer_ok
    )
    model_activation_count = len(latest_model_rows)
    ensemble_activation_count = len(latest_ensemble_rows)
    ready = (
        model_count >= len(EXPECTED_MODEL_SET)
        and selection_count >= 1
        and ensemble_count >= 1
        and model_generation == ensemble_generation > 0
        and latest_model_pointer_ok
        and latest_ensemble_pointer_ok
    )
    return {
        "state": "BOUND_NO_LIVE_CANDIDATE" if ready else "NOT_BOUND",
        "ready": ready,
        "model_artifact_count": model_count,
        "selection_evidence_count": selection_count,
        "ensemble_artifact_count": ensemble_count,
        "model_activation_count": model_activation_count,
        "ensemble_activation_count": ensemble_activation_count,
        "activation_generation": model_generation if model_generation == ensemble_generation else None,
        "historical_activation_generations_seen": sorted({
            int(row[1]) for row in (*model_rows, *ensemble_rows)
        }),
        "latest_model_pointer_ok": latest_model_pointer_ok,
        "latest_ensemble_pointer_ok": latest_ensemble_pointer_ok,
        "latest_selection_pointer_ok": latest_selection_pointer_ok,
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "native_arrival_index_eligible": False,
        "p1_eligible": False,
        "formal_live_authority": False,
    }


def _station_probe(
    conn: sqlite3.Connection,
    *,
    station: str,
    registry: Mapping[str, sqlite3.Row],
    as_of: datetime,
) -> dict[str, Any]:
    row = registry.get(station)
    registry_timezone = str(row[1]).strip() if row is not None else ""
    market_timezone, timezone_error = _market_timezone(conn, station)
    blockers: set[str] = set()
    warnings: set[str] = set()
    if row is None:
        blockers.add("CALIBRATION_STATION_REGISTRY_MISSING")
    if timezone_error:
        blockers.add(timezone_error)
    if market_timezone is None:
        # Keep the record bounded even when no timezone can be resolved.
        return {
            "station_id": station,
            "registry_timezone": registry_timezone,
            "market_timezone": None,
            "effective_timezone": None,
            "timezone_source": None,
            "historical_data_ready": False,
            "binding_readiness": "BLOCKED",
            "append_bindable": False,
            "hard_blockers": sorted(blockers),
            "contract_blockers": [
                "HISTORICAL_REPUBLISHER_CONTRACT_MISSING",
                "RUN_CYCLE_ADAPTER_REQUIRED",
            ],
            "model_day_count": 0,
            "complete_day_count": 0,
            "train_n": 0,
            "holdout_n": 0,
        }
    effective_timezone = market_timezone
    if registry_timezone and registry_timezone != effective_timezone:
        warnings.add("REGISTRY_TIMEZONE_MISMATCH_RESOLVED_BY_MARKET_BINDING")
    truths = _truth_by_day(
        conn,
        station=station,
        as_of=as_of,
        timezone_name=effective_timezone,
    )
    if any(
        str(truth.get("source_role") or "") == "CANONICAL_AMSC_AWC_INGRESS"
        for truth in truths.values()
    ):
        # The data lane is AWC, but older rows retain the historical combined
        # role label.  Preserve the lineage warning instead of silently
        # treating it as an AMSC truth source.
        warnings.add("TRUTH_SOURCE_ROLE_LEGACY_AMSC_AWC_LABEL")
    model_by_day, model_anomalies = _load_model_rows(
        conn,
        station=station,
        as_of=as_of,
    )
    residuals = _load_residuals(conn, station=station)
    all_days = sorted(set(model_by_day) | set(truths))
    complete_days: list[str] = []
    day_lineage: dict[str, list[dict[str, Any]]] = {}
    day_failures: dict[str, set[str]] = defaultdict(set)
    for day_text in all_days:
        day_blockers = set(model_anomalies.get(day_text, []))
        models = model_by_day.get(day_text, {})
        if set(models) != EXPECTED_MODEL_SET:
            day_blockers.add("EXACT_FIVE_MODEL_SET_MISSING")
        truth = truths.get(day_text)
        if truth is None:
            day_blockers.add("AWC_TRUTH_MISSING_OR_NOT_VISIBLE_AS_OF")
        elif not truth["closed_at_or_after_day_end"]:
            day_blockers.add("AWC_TRUTH_NOT_CLOSED_AT_DAY_END")
        elif not truth["committed_at_or_before_as_of"]:
            day_blockers.add("AWC_TRUTH_AFTER_AS_OF")
        entries: list[dict[str, Any]] = []
        if not day_blockers and truth is not None:
            for model in EXPECTED_MODELS:
                model_row = models[model]
                model_run_id = str(model_row[0])
                residual_candidates = residuals.get(model_run_id, [])
                exact_residuals = [
                    item for item in residual_candidates
                    if str(item[15]) == str(truth["truth_id"])
                ]
                if len(exact_residuals) != 1:
                    day_blockers.add(
                        "RESIDUAL_LINEAGE_MISSING"
                        if not exact_residuals
                        else "RESIDUAL_LINEAGE_AMBIGUOUS"
                    )
                    continue
                residual = exact_residuals[0]
                if (
                    str(residual[2]).strip().upper() != model
                    or str(residual[3]).strip().upper() != station
                    or str(residual[4]) != day_text
                    or str(residual[5]) != LEAD_BUCKET
                    or str(residual[6]) != RUN_CYCLE
                    or str(residual[8]) != "ACCEPTED"
                    or int(residual[9] or 0) != 1
                    or int(residual[10] or 0) != 0
                    or str(residual[11]) != NO_LIVE_SCOPE
                    or not _sha_ok(residual[12])
                    or not _sha_ok(residual[13])
                    or str(residual[7]) != str(model_row[3])
                ):
                    day_blockers.add("RESIDUAL_LINEAGE_SCOPE_INVALID")
                    continue
                entries.append({
                    "event_date": day_text,
                    "model_id": model,
                    "model_run_id": model_run_id,
                    "model_content_sha256": str(model_row[16]),
                    "residual_id": str(residual[0]),
                    "residual_content_sha256": str(residual[13]),
                    "truth_id": str(truth["truth_id"]),
                    "truth_revision_no": int(truth["revision_no"]),
                    "truth_content_sha256": str(truth["content_sha256"]),
                })
        if not day_blockers:
            complete_days.append(day_text)
            day_lineage[day_text] = entries
        else:
            day_failures[day_text].update(day_blockers)
    holdout_days = complete_days[-HOLDOUT_DAYS:]
    train_days = complete_days[:-HOLDOUT_DAYS]
    if len(holdout_days) < HOLDOUT_DAYS:
        blockers.add("HOLDOUT_DAYS_BELOW_8")
    if len(train_days) < TRAIN_DAYS:
        blockers.add("TRAIN_DAYS_BELOW_20")
    if not complete_days:
        blockers.add("NO_COMPLETE_HISTORICAL_STATION_DAYS")
    # Per-day anomalies that did not enter the complete set remain evidence
    # blockers, but are summarized rather than expanded without bound.
    failed_days = {
        day: sorted(reasons)
        for day in all_days
        if day not in complete_days
        and (reasons := day_failures.get(day))
    }
    holdout_lineage = [entry for day in holdout_days for entry in day_lineage.get(day, [])]
    train_lineage = [entry for day in train_days for entry in day_lineage.get(day, [])]
    hard_ready = not blockers
    binding = _d1_activation_binding(
        conn,
        station=station,
        season_bin="summer",
    ) if hard_ready else {
        "state": "NOT_BOUND",
        "ready": False,
        "model_artifact_count": 0,
        "selection_evidence_count": 0,
        "ensemble_artifact_count": 0,
        "model_activation_count": 0,
        "ensemble_activation_count": 0,
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "native_arrival_index_eligible": False,
        "p1_eligible": False,
        "formal_live_authority": False,
    }
    return {
        "station_id": station,
        "registry_timezone": registry_timezone,
        "market_timezone": market_timezone,
        "effective_timezone": effective_timezone,
        "timezone_source": "MARKET_REGISTRY_WITH_OPTIONAL_BINDING",
        "registry_timezone_mismatch": registry_timezone not in {"", effective_timezone},
        "warnings": sorted(warnings),
        "model_day_count": len(model_by_day),
        "truth_day_count": len(truths),
        "complete_day_count": len(complete_days),
        "complete_days": complete_days,
        "failed_day_reasons": failed_days,
        "model_ids": list(EXPECTED_MODELS),
        "run_cycle_by_model": {model: RUN_CYCLE for model in EXPECTED_MODELS},
        "source_role": "aggregator_republisher",
        "authority_scope": NO_LIVE_SCOPE,
        "activation_binding": binding,
        "train_n": len(train_days),
        "holdout_n": len(holdout_days),
        "train_start_date": train_days[0] if train_days else None,
        "train_end_date": train_days[-1] if train_days else None,
        "holdout_start_date": holdout_days[0] if holdout_days else None,
        "holdout_end_date": holdout_days[-1] if holdout_days else None,
        "holdout_days": holdout_days,
        "train_lineage_sha256": _lineage_digest(train_lineage) if train_lineage else None,
        "holdout_lineage_sha256": _lineage_digest(holdout_lineage) if holdout_lineage else None,
        "holdout_truth_refs": [
            {
                "event_date": day,
                "truth_id": str(truths[day]["truth_id"]),
                "revision_no": int(truths[day]["revision_no"]),
                "content_sha256": str(truths[day]["content_sha256"]),
                "source_lane": truths[day]["source_lane"],
                "source_role": truths[day]["source_role"],
                "canonical_committed_at_utc": truths[day]["canonical_committed_at_utc"],
                "station_day_end_utc": truths[day]["station_day_end_utc"],
            }
            for day in holdout_days
        ],
        "hard_blockers": sorted(blockers),
        "contract_blockers": [] if binding["ready"] else [
            "HISTORICAL_REPUBLISHER_CONTRACT_MISSING",
            "RUN_CYCLE_ADAPTER_REQUIRED",
        ],
        "historical_data_ready": hard_ready,
        "binding_readiness": (
            "BOUND_NO_LIVE_CANDIDATE"
            if binding["ready"]
            else "READY_FOR_HISTORICAL_CONTRACT" if hard_ready else "BLOCKED"
        ),
        "append_bindable": False,
        "activation_root_append_allowed": False,
    }


def build_probe(database: Path, *, as_of_utc: str) -> dict[str, Any]:
    as_of = _utc(as_of_utc, field="as_of_utc")
    if as_of is None:
        raise ValueError("d1_probe_as_of_utc_invalid")
    with _connect(database) as conn:
        registry = _station_registry(conn)
        stations = _target_stations(conn)
        records = [
            _station_probe(
                conn,
                station=station,
                registry=registry,
                as_of=as_of,
            )
            for station in stations
        ]
    ready = [row for row in records if row.get("historical_data_ready")]
    blocked = [row for row in records if not row.get("historical_data_ready")]
    holdout_sets = {
        tuple(row.get("holdout_days") or [])
        for row in ready
        if len(row.get("holdout_days") or []) == HOLDOUT_DAYS
    }
    exact_shared = len(holdout_sets) == 1 and len(ready) == len(records)
    common_holdout = list(next(iter(holdout_sets))) if exact_shared else []
    # A unified 45-station scorecard needs one exact historical holdout
    # cohort.  Keep the per-station evidence, but fail the station-level
    # binding gate when a closed-day anomaly forces a different 8-day slice.
    if holdout_sets:
        dominant_holdout = max(
            holdout_sets,
            key=lambda value: (sum(
                tuple(row.get("holdout_days") or []) == value
                for row in ready
            ), value),
        )
        if len(holdout_sets) > 1:
            common_holdout = list(dominant_holdout)
            for row in records:
                if (
                    row.get("historical_data_ready")
                    and tuple(row.get("holdout_days") or []) != dominant_holdout
                ):
                    hard = set(row.get("hard_blockers") or [])
                    hard.add("EXACT_SHARED_HOLDOUT_COHORT_MISMATCH")
                    row["hard_blockers"] = sorted(hard)
                    row["historical_data_ready"] = False
                    row["binding_readiness"] = "BLOCKED"
                    row["append_bindable"] = False
            ready = [row for row in records if row.get("historical_data_ready")]
            blocked = [row for row in records if not row.get("historical_data_ready")]
        exact_shared = len(holdout_sets) == 1 and len(ready) == len(records)
    identity = {
        "schema": SCHEMA,
        "as_of_utc": as_of.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "database": str(database.expanduser().resolve()),
        "lead_bucket": LEAD_BUCKET,
        "run_cycle": RUN_CYCLE,
        "train_days_required": TRAIN_DAYS,
        "holdout_days_required": HOLDOUT_DAYS,
        "expected_models": list(EXPECTED_MODELS),
        "station_count": len(records),
        "ready_station_count": len(ready),
        "blocked_station_count": len(blocked),
        "exact_shared_holdout": exact_shared,
        "common_holdout_days": common_holdout,
        "dominant_holdout_days": common_holdout,
        "station_ids": [row["station_id"] for row in records],
        "stations": records,
        "boundary": {
            "historical_closed_days_only": True,
            "future_event_count": 0,
            "open_meteo_is_aggregator_republisher": True,
            "native_arrival_authority": False,
            "production_authority": False,
            "activation_root_append_allowed": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "writes_database": False,
        },
        "recommendation": {
            "reuse_existing_model_raw": True,
            "preferred_candidate_ingress": "artifact_ingress::ModelEnsembleSelectionEvidence+ModelEnsembleArtifact",
            "native_cross_ensemble_ingress": "not_applicable_to_deterministic_d1_rows",
            "required_contract": "d1_historical_republisher_deterministic_oos_v1",
            "contract_key": "station_id×model_id×event_date×run_init_utc×AWC_truth_revision",
            "append_order": [
                "bind_existing_model_run/residual/content hashes to latest AWC closed-day revision",
                "append candidate-only selection/ensemble evidence",
                "re-audit exact 20/8 and all station holdout dates",
                "only then consider a separate no-live candidate pointer; do not append production/live authority here",
            ],
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = build_probe(args.db, as_of_utc=args.as_of_utc)
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output:
        target = args.output.expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(encoded + "\n", encoding="utf-8")
        print(json.dumps({
            "schema": SCHEMA,
            "output": str(target.resolve()),
            "station_count": report["station_count"],
            "ready_station_count": report["ready_station_count"],
            "blocked_station_count": report["blocked_station_count"],
            "exact_shared_holdout": report["exact_shared_holdout"],
            "content_sha256": report["content_sha256"],
        }, ensure_ascii=False, sort_keys=True))
    else:
        print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
