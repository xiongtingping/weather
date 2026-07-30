"""Bounded, deterministic OOS evaluation for station policy families.

The evaluator is intentionally a pure in-memory research compiler.  It
requires frozen settlement truth and exact station scope, performs all model
fitting on a station's own training rows, and emits separate reports and
artifacts for target/reserve/cadence.  It never opens a database, network
socket, worker, or gateway.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import ContractError, _finite, _sha, _text, _timestamp, content_sha256
from .station_policy_artifact import (
    POLICY_FAMILIES,
    WINDOW_DAYS,
    StationPolicyArtifact,
    StationPolicyScope,
    build_promotion_decision,
)


SCHEMA = "station_policy_oos_evidence_v1"
DEFAULT_LIMITS = {
    "max_rows": 100_000,
    "max_stations": 45,
    "min_station_sample_n": 4,
    "min_window_n_eff": 2.0,
    "min_cross_seasons": 4,
    "min_leave_one_station_n_eff": 2.0,
    "parameter_change_limit": 0.25,
    "non_degradation_tolerance": 1e-9,
}


def _mapping(name: str, value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{name}_mapping_required")
    return value


def _bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{name}_must_be_boolean")
    return value


def _utc(value: Any, name: str) -> datetime:
    text = _timestamp(name, value)
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def _timezone(name: str, value: Any) -> str:
    text = _text(name, value)
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    return text


def _number(name: str, value: Any) -> float:
    return _finite(name, value)


def _as_float_list(name: str, values: Iterable[Any]) -> list[float]:
    result = [_number(name, value) for value in values]
    if not result:
        raise ContractError(f"{name}_empty")
    return result


def _row_id(row: Mapping[str, Any]) -> str:
    supplied = row.get("row_id") or row.get("decision_id") or row.get("event_id")
    if supplied is not None:
        return _text("station_policy_row_id", supplied)
    return f"derived:{content_sha256(dict(row))}"


def _normalize_rows(rows: Iterable[Mapping[str, Any]], *, max_rows: int) -> list[dict[str, Any]]:
    if isinstance(rows, (str, bytes, bytearray)):
        raise ContractError("station_policy_rows_invalid")
    dedup: dict[str, tuple[str, dict[str, Any]]] = {}
    for index, value in enumerate(rows):
        if index >= max_rows:
            raise ContractError("station_policy_rows_limit_exceeded")
        if not isinstance(value, Mapping):
            raise ContractError("station_policy_row_mapping_required")
        raw = dict(value)
        rid = _row_id(raw)
        digest = content_sha256(raw)
        existing = dedup.get(rid)
        if existing is not None and existing[0] != digest:
            raise ContractError("station_policy_duplicate_row_conflict")
        dedup[rid] = (digest, raw)
    normalized: list[dict[str, Any]] = []
    for digest, raw in dedup.values():
        station = _text("station_policy_station_id", raw.get("station_id")).upper()
        raw["row_id"] = _row_id(raw)
        raw["station_id"] = station
        raw["settlement_timezone"] = _timezone("station_policy_settlement_timezone", raw.get("settlement_timezone"))
        raw["decision_at_utc"] = _timestamp("station_policy_decision_at_utc", raw.get("decision_at_utc"))
        raw["event_date"] = str(raw.get("event_date") or raw.get("target_local_date") or "").strip()
        if not raw["event_date"]:
            raise ContractError("station_policy_event_date_missing")
        try:
            datetime.strptime(raw["event_date"], "%Y-%m-%d")
        except ValueError as exc:
            raise ContractError("station_policy_event_date_invalid") from exc
        if raw.get("feature_as_of_utc") is not None:
            feature_asof = _utc(raw["feature_as_of_utc"], "station_policy_feature_as_of_utc")
            if feature_asof > _utc(raw["decision_at_utc"], "station_policy_decision_at_utc"):
                raise ContractError("station_policy_future_feature_leakage")
        frozen = raw.get("truth_frozen")
        if frozen is not True:
            raise ContractError("station_policy_truth_not_frozen")
        if raw.get("truth_frozen_at_utc") is not None:
            _timestamp("station_policy_truth_frozen_at_utc", raw["truth_frozen_at_utc"])
        raw["season_bin"] = _text("station_policy_season_bin", raw.get("season_bin"))
        raw["lead_bucket"] = _text("station_policy_lead_bucket", raw.get("lead_bucket"))
        raw["issuance_cycle"] = _text("station_policy_issuance_cycle", raw.get("issuance_cycle"))
        raw["stage"] = _text("station_policy_stage", raw.get("stage", "P0"))
        normalized.append(raw)
    return sorted(normalized, key=lambda row: (row["station_id"], row["event_date"], row["decision_at_utc"], row["row_id"]))


def _unique_cluster_count(rows: Sequence[Mapping[str, Any]]) -> float:
    clusters: dict[str, float] = {}
    for row in rows:
        cluster = str(row.get("event_cluster_id") or row["event_date"])
        weight = float(row.get("cluster_weight", 1.0))
        if not math.isfinite(weight) or weight <= 0 or weight > 1:
            raise ContractError("station_policy_cluster_weight_invalid")
        clusters[cluster] = max(clusters.get(cluster, 0.0), weight)
    return sum(clusters.values())


def _mean_interval(values: Sequence[float]) -> dict[str, Any]:
    vals = _as_float_list("station_policy_metric_values", values)
    mean = sum(vals) / len(vals)
    if len(vals) == 1:
        half = 0.0
    else:
        variance = sum((value - mean) ** 2 for value in vals) / (len(vals) - 1)
        half = 1.96 * math.sqrt(max(0.0, variance) / len(vals))
    return {"mean": mean, "lower": mean - half, "upper": mean + half, "n": len(vals)}


def _wilson_interval(successes: int, total: int) -> dict[str, Any]:
    if total <= 0:
        raise ContractError("station_policy_interval_sample_missing")
    p = successes / total
    z = 1.96
    denominator = 1.0 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return {"mean": p, "lower": max(0.0, center - half), "upper": min(1.0, center + half), "n": total}


def _probability_metrics(row: Mapping[str, Any], probabilities_key: str, outcome_key: str) -> tuple[float, float, float] | None:
    probs = row.get(probabilities_key)
    outcome = row.get(outcome_key)
    if not isinstance(probs, Mapping) or outcome is None:
        return None
    values = {str(key): _number("station_policy_probability", value) for key, value in probs.items()}
    if not values or any(value < 0 for value in values.values()) or abs(sum(values.values()) - 1.0) > 1e-6:
        raise ContractError("station_policy_probability_not_normalized")
    label = str(outcome)
    if label not in values:
        raise ContractError("station_policy_outcome_not_in_support")
    brier = sum((value - (1.0 if key == label else 0.0)) ** 2 for key, value in values.items())
    nll = -math.log(max(values[label], 1e-15))
    try:
        ordered = sorted(values, key=lambda key: float(key))
        outcome_index = ordered.index(label)
        cumulative = 0.0
        crps = 0.0
        for index, key in enumerate(ordered):
            cumulative += values[key]
            crps += (cumulative - (1.0 if index >= outcome_index else 0.0)) ** 2
    except (ValueError, TypeError):
        crps = brier
    return brier, nll, crps


def _score(row: Mapping[str, Any], family: str, *, candidate: bool) -> float:
    prefix = "candidate_" if candidate else "baseline_"
    direct_keys = {
        "target": (f"{prefix}score", f"{prefix}precision", f"{prefix}top1"),
        "reserve": (f"{prefix}score", f"{prefix}fill_score", f"{prefix}execution_score"),
        "cadence": (f"{prefix}score", f"{prefix}cadence_score", f"{prefix}execution_score"),
    }[family]
    for key in direct_keys:
        if row.get(key) is not None:
            return _number("station_policy_score", row[key])
    if family == "target" and candidate and row.get("predicted_bucket") is not None:
        return 1.0 if str(row["predicted_bucket"]) == str(row.get("observed_bucket")) else 0.0
    if family == "target" and not candidate and row.get("baseline_bucket") is not None:
        return 1.0 if str(row["baseline_bucket"]) == str(row.get("observed_bucket")) else 0.0
    if row.get("score") is not None and candidate:
        return _number("station_policy_score", row["score"])
    if row.get("baseline_score") is not None and not candidate:
        return _number("station_policy_score", row["baseline_score"])
    if family == "target":
        metrics = _probability_metrics(row, "probabilities" if candidate else "baseline_probabilities", "outcome")
        if metrics is not None:
            return -metrics[1]
    raise ContractError(f"station_policy_{family}_score_missing")


def _scope_from_rows(rows: Sequence[Mapping[str, Any]], station_id: str, *, train_end: datetime, test_start: datetime, test_end: datetime, strategy_version: str) -> StationPolicyScope:
    scoped = [row for row in rows if row["station_id"] == station_id]
    if not scoped:
        raise ContractError("station_policy_station_sample_missing")
    first = scoped[0]
    return StationPolicyScope(
        station_id=station_id,
        settlement_timezone=first["settlement_timezone"],
        season_bin=first["season_bin"],
        lead_bucket=first["lead_bucket"],
        issuance_cycle=first["issuance_cycle"],
        stage=first["stage"],
        maturity_bin=str(first.get("maturity_bin") or "d1"),
        liquidity_bin=str(first.get("liquidity_bin") or "liquid"),
        strategy_version=str(first.get("strategy_version") or strategy_version),
        train_start_utc=min(row["decision_at_utc"] for row in scoped if _utc(row["decision_at_utc"], "decision") < train_end),
        train_end_utc=train_end.isoformat().replace("+00:00", "Z"),
        test_start_utc=test_start.isoformat().replace("+00:00", "Z"),
        test_end_utc=test_end.isoformat().replace("+00:00", "Z"),
    )


def _family_report(
    *,
    rows: Sequence[dict[str, Any]],
    station_id: str,
    family: str,
    train_end: datetime,
    test_start: datetime,
    test_end: datetime,
    expected_station_ids: Sequence[str],
    limits: Mapping[str, float],
    scope_key: tuple[str, str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, float], dict[str, float], StationPolicyScope | None]:
    def same_scope(row: Mapping[str, Any]) -> bool:
        return scope_key is None or (
            str(row["season_bin"]),
            str(row["lead_bucket"]),
            str(row["issuance_cycle"]),
        ) == scope_key

    station_rows = [row for row in rows if row["station_id"] == station_id and same_scope(row)]
    station_validation_rows = [row for row in rows if row["station_id"] == station_id]
    train = [row for row in station_rows if _utc(row["decision_at_utc"], "decision") < train_end]
    test = [row for row in station_rows if test_start <= _utc(row["decision_at_utc"], "decision") < test_end]
    blockers: list[str] = []
    if not train:
        blockers.append("station_train_sample_missing")
    if not test:
        blockers.append("station_test_sample_missing")
    if train and test and max(_utc(row["decision_at_utc"], "decision") for row in train) >= min(_utc(row["decision_at_utc"], "decision") for row in test):
        blockers.append("train_test_future_leakage")
    expected_tz = {row["settlement_timezone"] for row in station_rows}
    if len(expected_tz) != 1:
        blockers.append("settlement_timezone_inconsistent")
    if any(row.get("truth_frozen") is not True for row in station_rows):
        blockers.append("truth_not_frozen")
    scope_fields = {
        "season_bin": {str(row.get("season_bin")) for row in station_rows},
        "lead_bucket": {str(row.get("lead_bucket")) for row in station_rows},
        "issuance_cycle": {str(row.get("issuance_cycle")) for row in station_rows},
        "stage": {str(row.get("stage")) for row in station_rows},
        "maturity_bin": {str(row.get("maturity_bin") or "d1") for row in station_rows},
        "liquidity_bin": {str(row.get("liquidity_bin") or "liquid") for row in station_rows},
        "strategy_version": {str(row.get("strategy_version") or limits.get("strategy_version", "station-policy-oos-v1")) for row in station_rows},
    }
    if any(len(values) != 1 for values in scope_fields.values()):
        blockers.append("station_policy_scope_inconsistent")
    cross_test = [row for row in station_validation_rows if test_start <= _utc(row["decision_at_utc"], "decision") < test_end]
    seasons = {row["season_bin"] for row in cross_test}
    if len(seasons) < int(limits["min_cross_seasons"]):
        blockers.append("cross_season_sample_insufficient")
    windows: dict[str, dict[str, Any]] = {}
    if cross_test:
        end_date = max(datetime.strptime(row["event_date"], "%Y-%m-%d").date() for row in cross_test)
        for days in WINDOW_DAYS:
            start_date = end_date - timedelta(days=days - 1)
            subset = [row for row in cross_test if start_date <= datetime.strptime(row["event_date"], "%Y-%m-%d").date() <= end_date]
            n_eff = _unique_cluster_count(subset) if subset else 0.0
            window_candidate = [_score(row, family, candidate=True) for row in subset] if subset else []
            window_baseline = [_score(row, family, candidate=False) for row in subset] if subset else []
            windows[str(days)] = {
                "sample_n": len(subset),
                "n_eff": n_eff,
                "candidate": _mean_interval(window_candidate) if window_candidate else None,
                "baseline": _mean_interval(window_baseline) if window_baseline else None,
                "complete": n_eff >= float(limits["min_window_n_eff"]),
            }
            if not windows[str(days)]["complete"]:
                blockers.append(f"window_{days}d_sample_insufficient")
    candidate_values = [_score(row, family, candidate=True) for row in test] if test else []
    baseline_values = [_score(row, family, candidate=False) for row in test] if test else []
    candidate_interval = _mean_interval(candidate_values) if candidate_values else {"mean": 0.0, "lower": 0.0, "upper": 0.0, "n": 0}
    baseline_interval = _mean_interval(baseline_values) if baseline_values else {"mean": 0.0, "lower": 0.0, "upper": 0.0, "n": 0}
    if candidate_values and baseline_values and candidate_interval["lower"] + float(limits["non_degradation_tolerance"]) < baseline_interval["lower"]:
        blockers.append("candidate_interval_deterioration")
    if test and _unique_cluster_count(test) < float(limits["min_station_sample_n"]):
        blockers.append("station_n_eff_insufficient")
    leave_rows = [row for row in rows if row["station_id"] != station_id and same_scope(row) and test_start <= _utc(row["decision_at_utc"], "decision") < test_end]
    leave_n_eff = _unique_cluster_count(leave_rows) if leave_rows else 0.0
    if len(set(row["station_id"] for row in leave_rows)) < 1 or leave_n_eff < float(limits["min_leave_one_station_n_eff"]):
        blockers.append("leave_one_station_evidence_missing")
    leave_candidate = [_score(row, family, candidate=True) for row in leave_rows] if leave_rows else []
    leave_baseline = [_score(row, family, candidate=False) for row in leave_rows] if leave_rows else []
    leave_metrics = {
        "candidate": _mean_interval(leave_candidate) if leave_candidate else None,
        "baseline": _mean_interval(leave_baseline) if leave_baseline else None,
        "delta": (sum(leave_candidate) / len(leave_candidate) - sum(leave_baseline) / len(leave_baseline)) if leave_candidate and leave_baseline else None,
        "n_eff": leave_n_eff,
    }
    cross_season: dict[str, Any] = {}
    for season in sorted(seasons):
        season_rows = [row for row in cross_test if row["season_bin"] == season]
        season_candidate = [_score(row, family, candidate=True) for row in season_rows]
        season_baseline = [_score(row, family, candidate=False) for row in season_rows]
        cross_season[season] = {
            "candidate": _mean_interval(season_candidate),
            "baseline": _mean_interval(season_baseline),
            "n_eff": _unique_cluster_count(season_rows),
        }
    theta_local: dict[str, float] = {}
    for row in train:
        values = row.get("theta") or row.get("features") or {}
        if isinstance(values, Mapping):
            for key, value in values.items():
                theta_local[str(key)] = theta_local.get(str(key), 0.0) + _number("station_policy_theta", value)
    if theta_local:
        theta_local = {key: value / len(train) for key, value in theta_local.items()}
    else:
        theta_local = {"intercept": sum(_score(row, family, candidate=True) for row in train) / len(train)} if train else {"intercept": 0.0}
    theta_post = dict(theta_local)
    n_eff = _unique_cluster_count(train) if train else 0.0
    rho = n_eff / (n_eff + float(limits.get("kappa", 8.0))) if n_eff > 0 else 0.0
    scope = _scope_from_rows(rows, station_id, train_end=train_end, test_start=test_start, test_end=test_end, strategy_version=str(limits.get("strategy_version", "station-policy-oos-v1"))) if train else None
    metrics: dict[str, Any] = {
        "primary_metric": "mean_score",
        "candidate": candidate_interval,
        "baseline": baseline_interval,
        "delta": candidate_interval["mean"] - baseline_interval["mean"],
        "n_eff": n_eff,
        "sample_n": len(train),
        "test_n_eff": _unique_cluster_count(test) if test else 0.0,
        "leave_one_station_out_n_eff": leave_n_eff,
        "leave_one_station_out": leave_metrics,
        "cross_season": cross_season,
        "windows": windows,
    }
    if family == "target":
        for key, prob_key in (("brier", "probabilities"), ("nll", "probabilities"), ("crps", "probabilities")):
            values = []
            for row in test:
                metric = _probability_metrics(row, prob_key, "outcome")
                if metric is not None:
                    values.append(metric[{"brier": 0, "nll": 1, "crps": 2}[key]])
            if values:
                metrics[key] = _mean_interval(values)
    checks = {
        "station_independent_fit": bool(train and all(row["station_id"] == station_id for row in train)),
        "strict_temporal_split": "train_test_future_leakage" not in blockers,
        "truth_frozen": "truth_not_frozen" not in blockers,
        "settlement_timezone_exact": "settlement_timezone_inconsistent" not in blockers,
        "scope_exact": "station_policy_scope_inconsistent" not in blockers,
        "cross_season": "cross_season_sample_insufficient" not in blockers,
        "windows_3_7_15_30": all(item.get("complete") for item in windows.values()) if windows else False,
        "leave_one_station_out": "leave_one_station_evidence_missing" not in blockers,
        "non_degraded_interval": "candidate_interval_deterioration" not in blockers,
        "promotion_eligible": not blockers,
    }
    report = {
        "schema": "station_policy_family_oos_report_v1",
        "family": family,
        "station_id": station_id,
        "scope": scope.to_dict() if scope else None,
        "metrics": metrics,
        "checks": checks,
        "blockers": sorted(set(blockers)),
        "train_sample_n": len(train),
        "test_sample_n": len(test),
        "test_seasons": sorted(seasons),
        "validation_scope_key": list(scope_key) if scope_key is not None else None,
        "station_count_in_input": len(set(row["station_id"] for row in rows)),
    }
    return report, theta_local, theta_post, scope


def evaluate_station_policy_oos(
    rows: Iterable[Mapping[str, Any]],
    *,
    station_ids: Sequence[str] | None = None,
    families: Sequence[str] = POLICY_FAMILIES,
    train_end_utc: str | None = None,
    test_start_utc: str | None = None,
    test_end_utc: str | None = None,
    expected_settlement_timezone_by_station: Mapping[str, str] | None = None,
    limits: Mapping[str, Any] | None = None,
    artifact_version: str = "station-policy-oos-v1",
    strategy_version: str = "station-policy-oos-v1",
    global_theta_by_family: Mapping[str, Mapping[str, Any]] | None = None,
    previous_theta_by_station_scope_family: Mapping[Any, Mapping[str, Any]] | None = None,
    previous_artifact_sha_by_station_scope_family: Mapping[Any, str] | None = None,
    promotion_requested: bool = False,
    decided_at_utc: str = "2026-07-27T00:00:00Z",
) -> dict[str, Any]:
    """Evaluate all requested stations independently and emit promotion evidence."""

    cfg = dict(DEFAULT_LIMITS)
    cfg.update(dict(limits or {}))
    cfg["strategy_version"] = strategy_version
    cfg["artifact_version"] = artifact_version
    normalized = _normalize_rows(rows, max_rows=int(cfg["max_rows"]))
    if not normalized:
        raise ContractError("station_policy_rows_empty")
    input_station_count = len({row["station_id"] for row in normalized})
    if input_station_count > int(cfg["max_stations"]):
        raise ContractError("station_policy_input_station_limit_exceeded")
    ids = tuple(sorted({str(value).strip().upper() for value in (station_ids or {row["station_id"] for row in normalized}) if str(value).strip()}))
    if not ids or len(ids) > int(cfg["max_stations"]):
        raise ContractError("station_policy_station_scope_invalid")
    if expected_settlement_timezone_by_station:
        for station in ids:
            expected = expected_settlement_timezone_by_station.get(station)
            if expected is None:
                raise ContractError("station_policy_settlement_timezone_missing")
            expected = _timezone("expected_settlement_timezone", expected)
            actual = {row["settlement_timezone"] for row in normalized if row["station_id"] == station}
            if actual and actual != {expected}:
                raise ContractError("station_policy_settlement_timezone_mismatch")
    for family in families:
        if family not in POLICY_FAMILIES:
            raise ContractError("station_policy_family_invalid")
    if global_theta_by_family is not None and not isinstance(global_theta_by_family, Mapping):
        raise ContractError("station_policy_global_theta_invalid")
    if any(not isinstance(value, Mapping) for value in (global_theta_by_family or {}).values()):
        raise ContractError("station_policy_global_theta_invalid")
    global_theta = {
        family: {
            str(key): _number("station_policy_global_theta", value)
            for key, value in (global_theta_by_family or {}).get(family, {}).items()
        }
        for family in families
    }
    decision_times = sorted(_utc(row["decision_at_utc"], "decision") for row in normalized)
    train_end = _utc(train_end_utc, "train_end_utc") if train_end_utc else decision_times[len(decision_times) * 3 // 5]
    test_start = _utc(test_start_utc, "test_start_utc") if test_start_utc else train_end
    test_end = _utc(test_end_utc, "test_end_utc") if test_end_utc else max(decision_times) + timedelta(microseconds=1)
    if not train_end <= test_start < test_end:
        raise ContractError("station_policy_split_invalid")
    reports: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    for station in ids:
        station_rows = [row for row in normalized if row["station_id"] == station]
        scope_keys = sorted({
            (str(row["season_bin"]), str(row["lead_bucket"]), str(row["issuance_cycle"]))
            for row in station_rows
        }) or [None]
        for family in families:
            for scope_key in scope_keys:
                report, theta_local, theta_post, scope = _family_report(
                    rows=normalized,
                    station_id=station,
                    family=family,
                    train_end=train_end,
                    test_start=test_start,
                    test_end=test_end,
                    expected_station_ids=ids,
                    limits=cfg,
                    scope_key=scope_key,
                )
                reports.append(report)
                if scope is None:
                    continue
                evidence_sha = content_sha256(report)
                n_eff = float(report["metrics"]["n_eff"])
                sample_n = int(report["metrics"]["sample_n"])
                prior = dict(global_theta.get(family, {}))
                theta_keys = set(theta_local) | set(prior)
                theta_local = {key: float(theta_local.get(key, prior.get(key, 0.0))) for key in sorted(theta_keys)}
                rho = n_eff / (n_eff + float(cfg.get("kappa", 8.0))) if n_eff else 0.0
                theta_post_final = {key: rho * theta_local[key] + (1.0 - rho) * prior.get(key, 0.0) for key in theta_local}
                parent_lookup_keys = (
                    (station, family, scope_key[0], scope_key[1], scope_key[2]),
                    (station, family),
                    f"{station}:{family}:{scope_key[0]}:{scope_key[1]}:{scope_key[2]}",
                    f"{station}:{family}",
                )
                parent_theta = None
                for lookup_key in parent_lookup_keys:
                    if previous_theta_by_station_scope_family and lookup_key in previous_theta_by_station_scope_family:
                        parent_theta = {
                            str(key): _number("station_policy_parent_theta", value)
                            for key, value in previous_theta_by_station_scope_family[lookup_key].items()
                        }
                        break
                comparison = parent_theta if parent_theta is not None else prior
                parameter_delta = max((abs(theta_post_final[key] - comparison.get(key, 0.0)) for key in theta_post_final), default=0.0)
                parent_sha = None
                for lookup_key in parent_lookup_keys:
                    if previous_artifact_sha_by_station_scope_family and lookup_key in previous_artifact_sha_by_station_scope_family:
                        parent_sha = _sha("station_policy_parent_artifact_sha256", previous_artifact_sha_by_station_scope_family[lookup_key])
                        break
                artifact = StationPolicyArtifact.create(
                    artifact_id=f"{family}-{station}-{scope_key[0]}-{scope_key[1]}-{scope_key[2]}-{artifact_version}-{evidence_sha[:16]}",
                    artifact_version=artifact_version,
                    family=family,
                    scope=scope,
                    theta_local=theta_local,
                    theta_post=theta_post_final,
                    rho=rho,
                    n_eff=n_eff,
                    sample_n=sample_n,
                    metrics=report["metrics"],
                    source_evidence_sha256=evidence_sha,
                    parent_artifact_sha256=parent_sha,
                    parameter_delta_max=parameter_delta,
                    promoted=False,
                )
                evidence = {
                    "report_sha256": evidence_sha,
                    "promotion_eligible": bool(report["checks"]["promotion_eligible"] and parameter_delta <= float(cfg["parameter_change_limit"])),
                    "checks": report["checks"],
                    "blockers": report["blockers"],
                    "n_eff": n_eff,
                    "sample_n": sample_n,
                }
                decision = build_promotion_decision(
                    artifact=artifact,
                    evidence=evidence,
                    promotion_requested=promotion_requested,
                    decided_at_utc=decided_at_utc,
                    parameter_change_limit=float(cfg["parameter_change_limit"]),
                )
                artifacts.append(artifact.to_dict())
                decisions.append(decision)
    # Every concrete station×season×lead×issuance scope must have one
    # artifact/decision.  A missing station emits blocked reports without an
    # artifact, so equality is the strict missing-scope gate; it also avoids
    # collapsing multiple seasonal scopes into a single aggregate count.
    expected_count = len(reports)
    promotion_eligible = (
        expected_count > 0
        and len(decisions) == expected_count
        and all(item["promotion_eligible"] for item in decisions)
    )
    # Eligibility is evidence.  It never mutates an artifact or activates it;
    # the receipt only reports promotion as permitted when the caller made an
    # explicit request.  The default therefore cannot auto-promote.
    promotion_permitted = bool(promotion_requested and promotion_eligible)
    identity = {
        "schema": SCHEMA,
        "station_ids": list(ids),
        "family_set": list(families),
        "train_end_utc": train_end.isoformat().replace("+00:00", "Z"),
        "test_start_utc": test_start.isoformat().replace("+00:00", "Z"),
        "test_end_utc": test_end.isoformat().replace("+00:00", "Z"),
        "reports": reports,
        "artifacts": artifacts,
        "promotion_decisions": decisions,
        "promotion_eligible": promotion_eligible,
        "promotion_permitted": promotion_permitted,
        "production_no_live": True,
        "live_order_authorized": False,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "resource_bounds": {key: cfg[key] for key in ("max_rows", "max_stations")},
        "input_station_count": input_station_count,
    }
    return {**identity, "evidence_sha256": content_sha256(identity)}


def assert_station_policy_oos_boundary(receipt: Mapping[str, Any]) -> None:
    if receipt.get("schema") != SCHEMA:
        raise ContractError("station_policy_oos_schema_invalid")
    if receipt.get("live_order_authorized") is not False or receipt.get("writes_production_db") is not False or receipt.get("calls_order_gateway") is not False:
        raise ContractError("station_policy_oos_authority_violation")
    if receipt.get("orders_submitted") != 0:
        raise ContractError("station_policy_oos_orders_forbidden")
    if not isinstance(receipt.get("reports"), list) or not isinstance(receipt.get("promotion_decisions"), list):
        raise ContractError("station_policy_oos_receipt_shape_invalid")
    expected = content_sha256({key: value for key, value in receipt.items() if key != "evidence_sha256"})
    if receipt.get("evidence_sha256") != expected:
        raise ContractError("station_policy_oos_evidence_sha256_mismatch")


__all__ = [
    "DEFAULT_LIMITS",
    "SCHEMA",
    "assert_station_policy_oos_boundary",
    "evaluate_station_policy_oos",
]
