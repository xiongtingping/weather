"""Strict-time OOS fitting for the bounded TAF revision artifact.

The fitter is deliberately independent of network and persistence.  It turns
closed settlement-day samples into one content-addressed artifact plus an
inspectable OOS scorecard.  TAF is evaluated as a probabilistic forecast of
daily Tmax; it is never promoted to truth and never receives order authority.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median
from typing import Any, Iterable, Mapping, Sequence

from .contracts import ContractError, content_sha256, timestamp_lt
from .taf_revision import (
    TAF_ARTIFACT_SCHEMA,
    TAF_AUTHORITY_SCOPE,
    TAF_CORRELATION_METHOD,
    TAF_METHOD_VERSION,
    TafRevisionArtifact,
)


TAF_CALIBRATION_SCORECARD_SCHEMA = "bayes_basket_taf_calibration_scorecard_v1"
# AWC's official date-addressed TAF replay is capped at roughly 30 days.
# Require nearly the full official window without making a compliant artifact
# impossible to fit from the authoritative source.
MIN_TAF_CALIBRATION_SAMPLE_N = 24
MIN_TAF_HOLDOUT_N = 6
_FEATURES = (
    "cloud_cover_fraction",
    "precipitation_probability",
    "wind_speed_normalized",
)


def _finite(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{name}_invalid")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if not math.isfinite(parsed):
        raise ContractError(f"{name}_invalid")
    return parsed


def _utc(name: str, value: Any) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class TafCalibrationSample:
    sample_id: str
    station_id: str
    event_date: str
    settlement_timezone: str
    season_bin: str
    lead_bucket: str
    run_cycle: str
    settlement_definition: str
    nwp_combination_artifact_sha256: str
    nwp_combination_decision_at_utc: str
    nwp_combination_prediction_oos: bool
    taf_issued_at_utc: str
    taf_available_at_utc: str
    truth_closed_at_utc: str
    tx_c: float
    truth_tmax_c: float
    cloud_cover_fraction: float
    precipitation_probability: float
    wind_speed_mps: float
    nwp_combined_tmax_c: float

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "TafCalibrationSample":
        allowed = {
            "sample_id", "station_id", "event_date", "settlement_timezone",
            "season_bin", "lead_bucket", "run_cycle",
            "settlement_definition", "nwp_combination_artifact_sha256",
            "nwp_combination_decision_at_utc",
            "nwp_combination_prediction_oos",
            "taf_issued_at_utc", "taf_available_at_utc", "truth_closed_at_utc",
            "tx_c", "truth_tmax_c", "cloud_cover_fraction",
            "precipitation_probability", "wind_speed_mps",
            "nwp_combined_tmax_c",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ContractError(
                "taf_calibration_sample_unknown_fields:"
                + ",".join(sorted(str(item) for item in unknown))
            )
        text = {
            key: str(raw.get(key) or "").strip()
            for key in (
                "sample_id", "station_id", "event_date", "settlement_timezone",
                "season_bin", "lead_bucket", "run_cycle",
                "settlement_definition", "nwp_combination_artifact_sha256",
                "nwp_combination_decision_at_utc",
                "taf_issued_at_utc", "taf_available_at_utc", "truth_closed_at_utc",
            )
        }
        if any(not value for value in text.values()):
            raise ContractError("taf_calibration_sample_identity_incomplete")
        nwp_sha = text["nwp_combination_artifact_sha256"].lower()
        if (
            len(nwp_sha) != 64
            or any(char not in "0123456789abcdef" for char in nwp_sha)
        ):
            raise ContractError(
                "taf_calibration_nwp_combination_artifact_sha256_invalid"
            )
        issued = _utc("taf_issued_at_utc", text["taf_issued_at_utc"])
        available = _utc("taf_available_at_utc", text["taf_available_at_utc"])
        closed = _utc("truth_closed_at_utc", text["truth_closed_at_utc"])
        nwp_decision = _utc(
            "nwp_combination_decision_at_utc",
            text["nwp_combination_decision_at_utc"],
        )
        if issued > available:
            raise ContractError("taf_calibration_available_before_issue")
        if available >= closed:
            raise ContractError("taf_calibration_truth_available_before_close")
        if nwp_decision > available:
            raise ContractError(
                "taf_calibration_nwp_combination_after_taf_arrival"
            )
        if raw.get("nwp_combination_prediction_oos") is not True:
            raise ContractError(
                "taf_calibration_nwp_combination_oos_required"
            )
        cloud = _finite("cloud_cover_fraction", raw.get("cloud_cover_fraction"))
        precip = _finite("precipitation_probability", raw.get("precipitation_probability"))
        wind = _finite("wind_speed_mps", raw.get("wind_speed_mps"))
        if not 0.0 <= cloud <= 1.0:
            raise ContractError("cloud_cover_fraction_out_of_range")
        if not 0.0 <= precip <= 1.0:
            raise ContractError("precipitation_probability_out_of_range")
        if wind < 0.0:
            raise ContractError("wind_speed_mps_out_of_range")
        return cls(
            sample_id=text["sample_id"],
            station_id=text["station_id"].upper(),
            event_date=text["event_date"],
            settlement_timezone=text["settlement_timezone"],
            season_bin=text["season_bin"],
            lead_bucket=text["lead_bucket"],
            run_cycle=text["run_cycle"],
            settlement_definition=text["settlement_definition"],
            nwp_combination_artifact_sha256=nwp_sha,
            nwp_combination_decision_at_utc=_iso(nwp_decision),
            nwp_combination_prediction_oos=True,
            taf_issued_at_utc=_iso(issued),
            taf_available_at_utc=_iso(available),
            truth_closed_at_utc=_iso(closed),
            tx_c=_finite("tx_c", raw.get("tx_c")),
            truth_tmax_c=_finite("truth_tmax_c", raw.get("truth_tmax_c")),
            cloud_cover_fraction=cloud,
            precipitation_probability=precip,
            wind_speed_mps=wind,
            nwp_combined_tmax_c=_finite(
                "nwp_combined_tmax_c",
                raw.get("nwp_combined_tmax_c"),
            ),
        )


def _solve_ridge(rows: Sequence[Sequence[float]], targets: Sequence[float], ridge: float) -> list[float]:
    width = len(rows[0])
    matrix = [[0.0 for _ in range(width + 1)] for _ in range(width)]
    for values, target in zip(rows, targets):
        for left in range(width):
            for right in range(width):
                matrix[left][right] += values[left] * values[right]
            matrix[left][-1] += values[left] * target
    for index in range(1, width):
        matrix[index][index] += ridge
    for pivot in range(width):
        swap = max(range(pivot, width), key=lambda row: abs(matrix[row][pivot]))
        if abs(matrix[swap][pivot]) < 1e-12:
            raise ContractError("taf_calibration_singular_design")
        matrix[pivot], matrix[swap] = matrix[swap], matrix[pivot]
        scale = matrix[pivot][pivot]
        matrix[pivot] = [value / scale for value in matrix[pivot]]
        for row in range(width):
            if row == pivot:
                continue
            factor = matrix[row][pivot]
            matrix[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(matrix[row], matrix[pivot])
            ]
    return [matrix[index][-1] for index in range(width)]


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _bucket_probability(actual: float, mean: float, scale: float) -> float:
    center = round(actual)
    return max(
        1e-12,
        _normal_cdf((center + 0.5 - mean) / scale)
        - _normal_cdf((center - 0.5 - mean) / scale),
    )


def _crps(actual: float, mean: float, scale: float) -> float:
    z = (actual - mean) / scale
    return scale * (
        z * (2.0 * _normal_cdf(z) - 1.0)
        + 2.0 * math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)
        - 1.0 / math.sqrt(math.pi)
    )


def _metrics(actuals: Sequence[float], means: Sequence[float], scale: float) -> dict[str, float]:
    lower = math.floor(min(min(actuals), min(means))) - 4
    upper = math.ceil(max(max(actuals), max(means))) + 4
    brier = 0.0
    nll = 0.0
    crps = 0.0
    for actual, mean in zip(actuals, means):
        truth_bucket = round(actual)
        probs = {
            bucket: max(
                0.0,
                _normal_cdf((bucket + 0.5 - mean) / scale)
                - _normal_cdf((bucket - 0.5 - mean) / scale),
            )
            for bucket in range(lower, upper + 1)
        }
        total = sum(probs.values())
        probs = {bucket: value / total for bucket, value in probs.items()}
        brier += sum(
            (probability - (1.0 if bucket == truth_bucket else 0.0)) ** 2
            for bucket, probability in probs.items()
        )
        nll -= math.log(max(1e-12, probs.get(truth_bucket, 0.0)))
        crps += _crps(actual, mean, scale)
    count = float(len(actuals))
    return {
        "brier_score": brier / count,
        "nll": nll / count,
        "crps": crps / count,
    }


def _fit_coefficients(samples: Sequence[TafCalibrationSample], wind_cap: float) -> list[float]:
    rows = [
        [
            1.0,
            item.tx_c - item.nwp_combined_tmax_c,
            item.cloud_cover_fraction,
            item.precipitation_probability,
            min(1.0, item.wind_speed_mps / wind_cap),
        ]
        for item in samples
    ]
    residuals = [
        item.truth_tmax_c - item.nwp_combined_tmax_c
        for item in samples
    ]
    return _solve_ridge(rows, residuals, ridge=2.0)


def _means(samples: Sequence[TafCalibrationSample], coefficients: Sequence[float], wind_cap: float) -> list[float]:
    return [
        item.nwp_combined_tmax_c
        + coefficients[0]
        + coefficients[1] * (
            item.tx_c - item.nwp_combined_tmax_c
        )
        + coefficients[2] * item.cloud_cover_fraction
        + coefficients[3] * item.precipitation_probability
        + coefficients[4] * min(1.0, item.wind_speed_mps / wind_cap)
        for item in samples
    ]


def _pearson(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        raise ContractError("taf_nwp_residual_pairing_invalid")
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum(
        (a - left_mean) * (b - right_mean)
        for a, b in zip(left, right)
    )
    left_scale = math.sqrt(sum((value - left_mean) ** 2 for value in left))
    right_scale = math.sqrt(sum((value - right_mean) ** 2 for value in right))
    if left_scale <= 1e-12 or right_scale <= 1e-12:
        return 1.0
    return max(-1.0, min(1.0, numerator / (left_scale * right_scale)))


def fit_taf_revision_artifact(
    samples: Iterable[TafCalibrationSample | Mapping[str, Any]],
    *,
    artifact_id: str,
    activation_id: str,
    season_bin: str,
    lead_bucket: str,
    run_cycle: str,
    settlement_definition: str,
    valid_from_utc: str,
    valid_until_utc: str,
) -> tuple[TafRevisionArtifact, dict[str, Any]]:
    """Fit and OOS-gate one formal no-live TAF artifact."""

    rows = [
        item if isinstance(item, TafCalibrationSample) else TafCalibrationSample.from_mapping(item)
        for item in samples
    ]
    rows.sort(key=lambda item: (item.event_date, item.taf_available_at_utc, item.sample_id))
    if len(rows) < MIN_TAF_CALIBRATION_SAMPLE_N:
        raise ContractError("taf_calibration_sample_n_below_min")
    if len({item.sample_id for item in rows}) != len(rows):
        raise ContractError("taf_calibration_sample_id_duplicate")
    scope = {
        (
            item.station_id,
            item.settlement_timezone,
            item.season_bin,
            item.lead_bucket,
            item.run_cycle,
            item.settlement_definition,
            item.nwp_combination_artifact_sha256,
        )
        for item in rows
    }
    if len(scope) != 1:
        raise ContractError("taf_calibration_scope_mixed")
    (
        station_id,
        settlement_timezone,
        sample_season,
        sample_lead,
        sample_run_cycle,
        sample_settlement_definition,
        nwp_combination_artifact_sha256,
    ) = next(iter(scope))
    if (
        sample_season != str(season_bin)
        or sample_lead != str(lead_bucket)
        or sample_run_cycle != str(run_cycle)
        or sample_settlement_definition != str(settlement_definition)
    ):
        raise ContractError("taf_calibration_declared_scope_mismatch")
    if any(rows[index - 1].event_date >= rows[index].event_date for index in range(1, len(rows))):
        raise ContractError("taf_calibration_event_date_not_unique_increasing")

    holdout_n = max(MIN_TAF_HOLDOUT_N, int(math.ceil(len(rows) * 0.20)))
    train = rows[:-holdout_n]
    holdout = rows[-holdout_n:]
    wind_cap = max(10.0, min(30.0, max(item.wind_speed_mps for item in train)))
    coefficients = _fit_coefficients(train, wind_cap)
    train_means = _means(train, coefficients, wind_cap)
    residuals = [
        item.truth_tmax_c - mean for item, mean in zip(train, train_means)
    ]
    scale = max(0.5, math.sqrt(sum(value * value for value in residuals) / len(residuals)))
    actuals = [item.truth_tmax_c for item in holdout]
    calibrated_means = _means(holdout, coefficients, wind_cap)
    baseline_means = [item.nwp_combined_tmax_c for item in holdout]
    calibrated_metrics = _metrics(actuals, calibrated_means, scale)
    baseline_scale = max(
        0.5,
        math.sqrt(
            sum(
                (
                    item.truth_tmax_c
                    - item.nwp_combined_tmax_c
                )
                ** 2
                for item in train
            )
            / len(train)
        ),
    )
    baseline_metrics = _metrics(
        actuals,
        baseline_means,
        baseline_scale,
    )
    if (
        calibrated_metrics["nll"] > baseline_metrics["nll"] + 1e-12
        or calibrated_metrics["crps"] > baseline_metrics["crps"] + 1e-12
    ):
        raise ContractError("taf_calibration_oos_gate_failed")
    taf_oos_residuals = [
        actual - mean
        for actual, mean in zip(actuals, calibrated_means)
    ]
    nwp_oos_residuals = [
        item.truth_tmax_c - item.nwp_combined_tmax_c
        for item in holdout
    ]
    taf_nwp_residual_correlation = _pearson(
        taf_oos_residuals,
        nwp_oos_residuals,
    )

    all_coefficients = _fit_coefficients(rows, wind_cap)
    all_means = _means(rows, all_coefficients, wind_cap)
    all_residuals = [
        item.truth_tmax_c - mean for item, mean in zip(rows, all_means)
    ]
    final_scale = max(
        0.5,
        math.sqrt(sum(value * value for value in all_residuals) / len(all_residuals)),
    )
    if len(all_residuals) > 1:
        mean_residual = sum(all_residuals) / len(all_residuals)
        numerator = sum(
            (left - mean_residual) * (right - mean_residual)
            for left, right in zip(all_residuals, all_residuals[1:])
        )
        denominator = sum((value - mean_residual) ** 2 for value in all_residuals)
        rho = max(-0.8, min(0.8, numerator / denominator if denominator else 0.0))
    else:
        rho = 0.0
    effective_n = max(1.0, min(float(len(rows)), len(rows) * (1.0 - rho) / (1.0 + rho)))
    improvement = max(
        0.0,
        min(
            1.0,
            0.5 * (
                1.0 - calibrated_metrics["nll"] / baseline_metrics["nll"]
                + 1.0 - calibrated_metrics["crps"] / baseline_metrics["crps"]
            ),
        ),
    )
    reliability = min(1.0, effective_n / 64.0) * (0.5 + 0.5 * improvement)
    correction_cap = min(0.30, max(0.08, 0.08 + 0.22 * reliability))
    confirmation_cap = min(0.35, correction_cap + 0.05)
    mass_cap = min(0.15, max(0.05, correction_cap * 0.5))
    # TAF confirms only the information incremental to the already-calibrated
    # NWP combination.  Strong positive or negative holdout dependence means
    # less independent evidence and therefore a smaller revision strength.
    correlation_discount = max(
        0.20,
        min(1.0, 1.0 - abs(taf_nwp_residual_correlation)),
    )
    trained_through = max(item.truth_closed_at_utc for item in rows)
    if not timestamp_lt(trained_through, valid_from_utc):
        raise ContractError("taf_calibration_valid_from_not_after_training")

    identity = {
        "schema": TAF_ARTIFACT_SCHEMA,
        "artifact_id": str(artifact_id),
        "activation_id": str(activation_id),
        "station_id": station_id,
        "season_bin": str(season_bin),
        "lead_bucket": str(lead_bucket),
        "run_cycle": str(run_cycle),
        "settlement_definition": str(settlement_definition),
        "settlement_timezone": settlement_timezone,
        "method_version": TAF_METHOD_VERSION,
        "trained_through_utc": trained_through,
        "sample_n": len(rows),
        "effective_sample_n": effective_n,
        "oos_metrics": calibrated_metrics,
        "tx_bias_c": all_coefficients[0],
        "taf_nwp_delta_coefficient": all_coefficients[1],
        "tx_residual_scale_c": final_scale,
        "physical_feature_effect_c": {
            name: all_coefficients[index + 2]
            for index, name in enumerate(_FEATURES)
        },
        "wind_speed_cap_mps": wind_cap,
        "confirmation_tolerance_c": max(
            0.5, min(2.0, median(abs(value) for value in all_residuals))
        ),
        "max_confirmation_strength": confirmation_cap,
        "max_correction_strength": correction_cap,
        "max_probability_mass_transfer": mass_cap,
        "max_center_displacement_buckets": 1,
        "likelihood_ratio_floor": 0.25,
        "likelihood_ratio_ceiling": 4.0,
        "nwp_combination_artifact_sha256": (
            nwp_combination_artifact_sha256
        ),
        "oos_taf_nwp_residual_correlation": (
            taf_nwp_residual_correlation
        ),
        "correlation_discount_method": TAF_CORRELATION_METHOD,
        "correlation_discount": correlation_discount,
        "joint_physical_oos_validated": True,
        "valid_from_utc": str(valid_from_utc),
        "valid_until_utc": str(valid_until_utc),
        "authority_scope": TAF_AUTHORITY_SCOPE,
        "activated": True,
    }
    artifact = TafRevisionArtifact.from_mapping(
        {**identity, "content_sha256": content_sha256(identity)}
    )
    scorecard_identity = {
        "schema": TAF_CALIBRATION_SCORECARD_SCHEMA,
        "artifact_id": artifact.artifact_id,
        "artifact_content_sha256": artifact.content_sha256,
        "station_id": station_id,
        "settlement_timezone": settlement_timezone,
        "season_bin": sample_season,
        "lead_bucket": sample_lead,
        "run_cycle": sample_run_cycle,
        "settlement_definition": sample_settlement_definition,
        "nwp_combination_artifact_sha256": (
            nwp_combination_artifact_sha256
        ),
        "sample_n": len(rows),
        "train_n": len(train),
        "holdout_n": len(holdout),
        "train_start_date": train[0].event_date,
        "train_end_date": train[-1].event_date,
        "holdout_start_date": holdout[0].event_date,
        "holdout_end_date": holdout[-1].event_date,
        "trained_through_utc": trained_through,
        "nwp_baseline_oos_metrics": baseline_metrics,
        "calibrated_oos_metrics": calibrated_metrics,
        "oos_gate": {
            "nll_not_worse": calibrated_metrics["nll"] <= baseline_metrics["nll"],
            "crps_not_worse": calibrated_metrics["crps"] <= baseline_metrics["crps"],
            "passed": True,
        },
        "serial_residual_rho_1": rho,
        "taf_nwp_holdout_residual_correlation": (
            taf_nwp_residual_correlation
        ),
        "correlation_discount_method": TAF_CORRELATION_METHOD,
        "correlation_discount": correlation_discount,
        "joint_physical_feature_oos_gate": {
            "taf_minus_nwp_cloud_precipitation_wind_jointly_fitted": True,
            "nll_not_worse_than_nwp_combination": (
                calibrated_metrics["nll"] <= baseline_metrics["nll"]
            ),
            "crps_not_worse_than_nwp_combination": (
                calibrated_metrics["crps"] <= baseline_metrics["crps"]
            ),
            "passed": True,
        },
        "effective_sample_n": effective_n,
        "sample_ids_sha256": content_sha256([item.sample_id for item in rows]),
        "boundary": {
            "strict_time_split": True,
            "future_truth_leakage_forbidden": True,
            "official_truth_lane": "AWC",
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    return artifact, {
        **scorecard_identity,
        "content_sha256": content_sha256(scorecard_identity),
    }


__all__ = [
    "MIN_TAF_CALIBRATION_SAMPLE_N",
    "MIN_TAF_HOLDOUT_N",
    "TAF_CALIBRATION_SCORECARD_SCHEMA",
    "TafCalibrationSample",
    "fit_taf_revision_artifact",
]
