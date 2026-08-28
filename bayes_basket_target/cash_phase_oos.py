"""Target-owned cash-phase historical strict-as-of OOS scorecard.

The module is deliberately pure and bounded: it consumes caller-provided
evidence only, never opens a database, touches a service, or reads a source
wallet.  A row is an as-of decision snapshot; settlement labels are selected
by the newest valid revision visible at the scorecard cut-off.  The result is
an audit artifact, not an execution authorization.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from itertools import islice
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .cash_phase import (
    BasketCashPhaseInput,
    BasketCashPhasePolicyArtifact,
    PHASES,
    evaluate_cash_phase,
)
from .contracts import ContractError, content_sha256
from .fleet import expected_station_count


# Keep the governed promotion schema stable; the richer attribution is an
# additive payload under the same formal scorecard contract.
SCHEMA = "bayes_basket_target_cash_phase_forward_scorecard_v1"
EVIDENCE_BUNDLE_SCHEMA = "bayes_basket_target_cash_phase_oos_evidence_v1"
PROMOTION_DECISION_SCHEMA = "bayes_basket_target_cash_phase_promotion_decision_candidate_v1"
WINDOWS_DAYS = (3, 7, 15, 30)
MAX_ROWS = 4096
MAX_STATIONS = expected_station_count()
MAX_REGISTRY_ROWS = 4096
MAX_FAULT_CASES = 256
DEFAULT_MIN_UNIQUE_EVENTS_PER_PHASE = 30
MAX_JSON_NODES = 100_000
EPS = 1e-9
REQUIRED_FAULT_CASES = frozenset({
    "half_open_start_included",
    "half_open_end_excluded",
    "future_label_excluded",
    "timezone_mismatch_rejected",
    "phase_rewind_rejected",
    "duplicate_conflict_rejected",
})


class CashPhaseOOSBlocked(ContractError):
    """Raised only by the strict convenience API when a result is unusable."""


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _number(name: str, value: Any, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{name}_invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if not math.isfinite(result) or (nonnegative and result < 0):
        raise ContractError(f"{name}_invalid")
    return result


def _utc(name: str, value: Any) -> tuple[str, datetime]:
    raw = _text(name, value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat().replace("+00:00", "Z"), parsed


def _phase(value: Any) -> str:
    phase = str(value or "").strip().upper()
    if phase not in PHASES:
        raise ContractError("cash_phase_invalid")
    return phase


def _mapping(name: str, value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{name}_mapping_required")
    return value


def _bool_alias(row: Mapping[str, Any], *names: str) -> bool | None:
    for name in names:
        if name in row and row[name] is not None:
            value = row[name]
            if not isinstance(value, bool):
                raise ContractError(f"{name}_must_be_boolean")
            return value
    return None


def _label_rows(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = row.get("labels", row.get("label_revisions"))
    if raw is None:
        candidate = row.get("label")
        raw = [] if candidate is None else [candidate]
    if isinstance(raw, Mapping):
        raw = [raw]
    if isinstance(raw, (str, bytes, bytearray)) or not isinstance(raw, Sequence):
        raise ContractError("label_revisions_must_be_sequence")
    if len(raw) > 64:
        raise ContractError("label_revision_count_out_of_bounds")
    result: list[Mapping[str, Any]] = []
    for item in raw:
        result.append(_mapping("label_revision", item))
    return result


def _revision(row: Mapping[str, Any], *, as_of: datetime) -> tuple[Mapping[str, Any] | None, list[str]]:
    """Return newest valid visible revision and all integrity blockers."""

    blockers: list[str] = []
    valid: list[tuple[int, datetime, str, Mapping[str, Any]]] = []
    for index, label in enumerate(_label_rows(row)):
        try:
            revision = int(label.get("truth_revision_no", label.get("revision")))
            if revision < 0:
                raise ValueError
            labeled_raw = label.get("labeled_at_utc", label.get("labelled_at_utc"))
            labeled, labeled_dt = _utc("label_labeled_at_utc", labeled_raw)
            closed_raw = label.get("settlement_closed_at_utc", labeled)
            closed, closed_dt = _utc("label_settlement_closed_at_utc", closed_raw)
            if labeled_dt > as_of or closed_dt > as_of:
                blockers.append("future_label_excluded")
                continue
            truth = label.get("winning_bucket", label.get("truth_bucket"))
            if truth is None or not str(truth).strip():
                raise ValueError
            truth_sha = str(label.get("truth_sha256") or "")
            if truth_sha and (len(truth_sha) != 64 or any(c not in "0123456789abcdefABCDEF" for c in truth_sha)):
                raise ValueError
            identity = {
                "revision": revision,
                "labeled_at_utc": labeled,
                "settlement_closed_at_utc": closed,
                "winning_bucket": str(truth),
                "truth_sha256": truth_sha or None,
            }
            label_id = str(label.get("label_id") or label.get("id") or f"label-{index}")
            valid.append((revision, labeled_dt, label_id, {**dict(label), **identity}))
        except (ContractError, TypeError, ValueError):
            blockers.append("invalid_label_revision")
    if not valid:
        blockers.append("settlement_label_missing")
        return None, sorted(set(blockers))
    valid.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return valid[0][3], sorted(set(blockers))


def _probability(row: Mapping[str, Any], winner: str) -> float:
    raw = row.get("probabilities", row.get("probability_by_bucket"))
    if raw is None:
        # A precomputed coverage is accepted only as an explicit scalar; it
        # is still bound into the artifact and never treated as a label.
        raw = row.get("probability_coverage")
        return _number("probability_coverage", raw, nonnegative=True) if raw is not None else 0.0
    probs = _mapping("probabilities", raw)
    values = {str(key): _number("probability", value, nonnegative=True) for key, value in probs.items()}
    total = sum(values.values())
    if total <= EPS or total > 1.0 + 1e-6:
        raise ContractError("probabilities_not_normalized")
    return min(1.0, values.get(str(winner), 0.0) / total)


def _price(row: Mapping[str, Any]) -> float | None:
    raw = row.get("all_in_yes_ask", row.get("price", row.get("candidate_price")))
    if raw is None:
        return None
    result = _number("candidate_price", raw, nonnegative=True)
    if result >= 1.0:
        raise ContractError("candidate_price_out_of_range")
    return result


def _status(row: Mapping[str, Any]) -> str:
    raw = str(row.get("fill_status", row.get("status", "NONFILLED")) or "").strip().upper()
    aliases = {"UNFILLED": "NONFILLED", "NOT_FILLED": "NONFILLED", "NO_EXECUTION": "NO_EXECUTION", "FILLED": "FILLED"}
    status = aliases.get(raw, raw)
    if status not in {"FILLED", "NONFILLED", "NO_EXECUTION"}:
        raise ContractError("fill_status_invalid")
    return status


def _ci(successes: float, count: float) -> tuple[float, float]:
    if count <= 0:
        return 0.0, 0.0
    p = max(0.0, min(1.0, successes / count))
    z = 1.959963984540054
    denominator = 1.0 + z * z / count
    centre = (p + z * z / (2.0 * count)) / denominator
    radius = z * math.sqrt(p * (1.0 - p) / count + z * z / (4.0 * count * count)) / denominator
    return max(0.0, centre - radius), min(1.0, centre + radius)


def _mean_report(values: Sequence[float], *, unique_count: int | None = None) -> dict[str, Any]:
    count = len(values)
    if not count:
        return {"count": 0, "n_eff": 0.0, "mean": None, "sum": 0.0, "interval_95": None}
    mean = sum(values) / count
    variance = sum((value - mean) ** 2 for value in values) / count
    n_eff = float(unique_count if unique_count is not None else count)
    radius = 1.959963984540054 * math.sqrt(variance / max(n_eff, 1.0))
    return {
        "count": count,
        "n_eff": n_eff,
        "mean": mean,
        "sum": sum(values),
        "interval_95": [mean - radius, mean + radius],
    }


def _rate_report(successes: int, count: int, *, n_eff: int) -> dict[str, Any]:
    lower, upper = _ci(float(successes), float(count))
    return {
        "count": count,
        "successes": successes,
        "rate": successes / count if count else None,
        "n_eff": float(n_eff),
        "interval_95": [lower, upper] if count else None,
    }


def _phase_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ids = {str(row["event_id"]) for row in rows}
    probability = [float(row["probability_coverage"]) for row in rows]
    price_valid = sum(1 for row in rows if row["price"] is not None)
    authorized = sum(1 for row in rows if row["candidate_authorized"])
    filled = sum(1 for row in rows if row["fill_status"] == "FILLED")
    nonfilled = len(rows) - filled
    edge = [float(row["edge"]) for row in rows if row["edge"] is not None]
    execution = [float(row["execution_cost_usdc"]) for row in rows]
    realized = [float(row["realized_pnl_usdc"]) for row in rows if row["realized_pnl_usdc"] is not None]
    settlement = [float(row["settlement_pnl_usdc"]) for row in rows if row["settlement_pnl_usdc"] is not None]
    failure = [float(row["failure_cost_usdc"]) for row in rows]
    return {
        "n_eff": float(len(ids)),
        "observation_count": len(rows),
        "probability_coverage": _mean_report(probability, unique_count=len(ids)),
        "price_coverage": _rate_report(price_valid, len(rows), n_eff=len(ids)),
        "candidate_authorization": _rate_report(authorized, len(rows), n_eff=len(ids)),
        "fill_nonfill": {
            "filled": filled,
            "nonfilled": nonfilled,
            "fill_rate": filled / len(rows) if rows else None,
            "n_eff": float(len(ids)),
        },
        "edge": _mean_report(edge, unique_count=len(ids)),
        "execution_cost": _mean_report(execution, unique_count=len(ids)),
        "realized_pnl": _mean_report(realized, unique_count=len(ids)),
        "settlement_pnl": _mean_report(settlement, unique_count=len(ids)),
        "failure_cost": _mean_report(failure, unique_count=len(ids)),
        "no_execution_evidence": sum(1 for row in rows if row["fill_status"] == "NO_EXECUTION"),
    }


def _normalize_row(
    raw: Mapping[str, Any],
    *,
    as_of: datetime,
    default_policy_version: str,
    expected_station_timezone: str | None = None,
    strict_target_owned: bool = False,
) -> tuple[dict[str, Any] | None, list[str]]:
    row = _mapping("cash_phase_observation", raw)
    blockers: list[str] = []
    event_id = _text("event_id", row.get("event_id", row.get("decision_id")))
    station_id = _text("station_id", row.get("station_id"))
    phase = _phase(row.get("phase", row.get("cash_phase")))
    decision, decision_dt = _utc("decision_at_utc", row.get("decision_at_utc", row.get("observed_at_utc")))
    if decision_dt > as_of:
        return None, ["future_decision_excluded"]
    for name in ("published_at_utc", "received_at_utc", "captured_at_utc"):
        if row.get(name) is not None:
            _, value_dt = _utc(name, row[name])
            if value_dt > as_of or value_dt > decision_dt:
                blockers.append(f"{name}_violates_asof")
    label, label_blockers = _revision(row, as_of=as_of)
    blockers.extend(label_blockers)
    if label is None:
        return None, sorted(set(blockers))
    _, labeled_dt = _utc("label_labeled_at_utc", label["labeled_at_utc"])
    _, closed_dt = _utc(
        "label_settlement_closed_at_utc",
        label["settlement_closed_at_utc"],
    )
    if labeled_dt <= decision_dt or closed_dt <= decision_dt:
        blockers.append("label_not_strictly_after_decision")
    station_timezone = str(row.get("station_timezone") or "").strip()
    if expected_station_timezone is not None:
        if station_timezone != expected_station_timezone:
            blockers.append("station_timezone_mismatch")
    if station_timezone:
        try:
            ZoneInfo(station_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            blockers.append("station_timezone_invalid")
    if strict_target_owned:
        if str(row.get("evidence_owner") or "") != "bayes_basket_target":
            blockers.append("evidence_owner_not_target")
        if str(row.get("production_mode") or "").lower() != "production_no_live":
            blockers.append("production_mode_not_no_live")
        split = str(row.get("evaluation_split") or "").upper()
        if split not in {"HISTORICAL", "OOS", "STRICT_HISTORICAL_OOS"}:
            blockers.append("evaluation_split_not_historical_oos")
        role = str(
            row.get("evidence_role") or "historical_strict_oos"
        ).strip().lower()
        if role != "historical_strict_oos":
            blockers.append("evidence_role_not_historical_strict_oos")
    semantics = str(row.get("label_semantics", row.get("semantics", "")) or "").strip().lower()
    live_fill = _bool_alias(row, "live_fill", "real_live_fill")
    if live_fill is True or semantics in {"actual_fills", "confirmed_fills", "live_fills", "filled_orders"}:
        blockers.append("real_live_fill_mixed_in")
    status = _status(row)
    no_execution = row.get("no_execution_policy") is not None or semantics in {"no_execution", "hypothetical_requested_intents_not_fills"}
    if no_execution and status == "FILLED":
        blockers.append("no_execution_claimed_filled")
    winner = str(label["winning_bucket"])
    probability = _probability(row, winner)
    price = _price(row)
    authorized = _bool_alias(row, "candidate_authorized", "authorized")
    if authorized is None:
        authorized = False
    cost = _number("execution_cost_usdc", row.get("execution_cost_usdc", row.get("execution_cost", 0.0)), nonnegative=True)
    realized_raw = row.get("realized_pnl_usdc", row.get("realized_pnl"))
    settlement_raw = row.get("settlement_pnl_usdc", row.get("settlement_pnl"))
    realized = None if realized_raw is None else _number("realized_pnl_usdc", realized_raw)
    settlement = None if settlement_raw is None else _number("settlement_pnl_usdc", settlement_raw)
    failure = _number("failure_cost_usdc", row.get("failure_cost_usdc", row.get("failure_cost", 0.0)), nonnegative=True)
    edge = None if price is None else probability - price
    policy_version = str(row.get("policy_version") or default_policy_version)
    if not policy_version:
        blockers.append("cash_phase_policy_version_missing")
    elif policy_version != default_policy_version:
        blockers.append("cash_phase_policy_version_mismatch")
    normalized = {
        "event_id": event_id,
        "observation_id": str(row.get("observation_id") or row.get("decision_id") or content_sha256(dict(row))),
        "station_id": station_id,
        "station_timezone": station_timezone,
        "phase": phase,
        "decision_at_utc": decision,
        "decision_dt": decision_dt,
        "winning_bucket": winner,
        "label_revision": int(label["revision"]),
        "policy_version": policy_version,
        "probability_coverage": probability,
        "price": price,
        "edge": edge,
        "candidate_authorized": bool(authorized),
        "fill_status": "NO_EXECUTION" if no_execution else status,
        "execution_cost_usdc": cost,
        "realized_pnl_usdc": realized,
        "settlement_pnl_usdc": settlement,
        "failure_cost_usdc": failure,
        "source_content_sha256": str(row.get("content_sha256") or content_sha256(dict(row))),
    }
    return normalized, sorted(set(blockers))


def build_cash_phase_oos_scorecard(
    observations: Iterable[Mapping[str, Any]],
    *,
    as_of_utc: str,
    policy_version: str,
    policy_promoted: bool = False,
    policy_evidence_status: str = "DESIGN_CANDIDATE",
    expected_station_id: str | None = None,
    expected_station_timezone: str | None = None,
    evaluation_start_utc: str | None = None,
    evaluation_end_utc: str | None = None,
    policy_training_cutoff_utc: str | None = None,
    minimum_unique_events_per_phase: int = DEFAULT_MIN_UNIQUE_EVENTS_PER_PHASE,
    fault_cases: Sequence[Mapping[str, Any]] = (),
    strict_target_owned: bool = False,
    max_rows: int = MAX_ROWS,
) -> dict[str, Any]:
    """Build a deterministic phase/window OOS attribution artifact.

    Duplicate byte-identical observations are ignored.  Same event IDs with
    different content are a hard conflict.  Invalid/future evidence is never
    silently used; it is represented as a blocker and keeps the artifact
    fail-closed.
    """

    as_of, as_of_dt = _utc("as_of_utc", as_of_utc)
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or not 1 <= max_rows <= MAX_ROWS:
        raise ContractError("cash_phase_oos_max_rows_invalid")
    version = _text("policy_version", policy_version)
    if (
        isinstance(minimum_unique_events_per_phase, bool)
        or not isinstance(minimum_unique_events_per_phase, int)
        or minimum_unique_events_per_phase < 1
    ):
        raise ContractError("cash_phase_oos_minimum_sample_invalid")
    evaluation_start, evaluation_start_dt = _utc(
        "evaluation_start_utc",
        evaluation_start_utc
        or (as_of_dt - timedelta(days=30)).isoformat().replace("+00:00", "Z"),
    )
    evaluation_end, evaluation_end_dt = _utc(
        "evaluation_end_utc",
        evaluation_end_utc or as_of,
    )
    if not evaluation_start_dt < evaluation_end_dt <= as_of_dt:
        raise ContractError("cash_phase_oos_evaluation_window_invalid")
    training_cutoff = None
    training_cutoff_dt = None
    if policy_training_cutoff_utc is not None:
        training_cutoff, training_cutoff_dt = _utc(
            "policy_training_cutoff_utc",
            policy_training_cutoff_utc,
        )
        if training_cutoff_dt > evaluation_start_dt:
            raise ContractError("cash_phase_oos_training_overlaps_evaluation")
    if len(fault_cases) > MAX_FAULT_CASES:
        raise ContractError("cash_phase_oos_fault_cases_exceeded")
    normalized_faults: list[dict[str, Any]] = []
    fault_names: set[str] = set()
    for raw_case in fault_cases:
        case = _mapping("cash_phase_fault_case", raw_case)
        name = _text("cash_phase_fault_case_name", case.get("name"))
        if name in fault_names:
            raise ContractError("cash_phase_fault_case_duplicate")
        passed = case.get("passed")
        if not isinstance(passed, bool):
            raise ContractError("cash_phase_fault_case_passed_must_be_boolean")
        if strict_target_owned and str(case.get("evidence_owner") or "") != "bayes_basket_target":
            raise ContractError("cash_phase_fault_case_owner_not_target")
        evidence_sha256 = str(case.get("evidence_sha256") or "")
        if (
            len(evidence_sha256) != 64
            or any(character not in "0123456789abcdef" for character in evidence_sha256)
        ):
            raise ContractError("cash_phase_fault_case_evidence_sha256_invalid")
        fault_names.add(name)
        normalized_faults.append({
            "name": name,
            "passed": passed,
            "evidence_owner": str(case.get("evidence_owner") or ""),
            "evidence_sha256": evidence_sha256,
        })
    source = list(islice(iter(observations), max_rows + 1))
    if len(source) > max_rows:
        source = source[:max_rows]
        cap_blocker = "observation_row_cap_reached"
    else:
        cap_blocker = None
    blockers: set[str] = set(filter(None, [cap_blocker]))
    normalized: dict[str, dict[str, Any]] = {}
    semantic_rows: dict[tuple[str, str, str, str], str] = {}
    for raw in source:
        try:
            item, item_blockers = _normalize_row(
                raw,
                as_of=as_of_dt,
                default_policy_version=version,
                expected_station_timezone=expected_station_timezone,
                strict_target_owned=strict_target_owned,
            )
        except (ContractError, TypeError, ValueError) as exc:
            blockers.add("observation_invalid:" + str(exc))
            continue
        blockers.update(item_blockers)
        if item is None:
            continue
        if expected_station_id is not None and item["station_id"] != expected_station_id:
            blockers.add("station_scope_mismatch")
            continue
        if not evaluation_start_dt <= item["decision_dt"] < evaluation_end_dt:
            continue
        observation_id = item["observation_id"]
        semantic_id = (
            item["station_id"],
            item["event_id"],
            item["phase"],
            item["decision_at_utc"],
        )
        prior_semantic_sha = semantic_rows.get(semantic_id)
        semantic_sha = item["source_content_sha256"]
        if prior_semantic_sha is not None:
            if prior_semantic_sha != semantic_sha:
                blockers.add("duplicate_event_content_conflict")
            continue
        semantic_rows[semantic_id] = semantic_sha
        previous = normalized.get(observation_id)
        if previous is not None:
            if content_sha256(previous) != content_sha256(item):
                blockers.add("duplicate_observation_content_conflict")
            continue
        normalized[observation_id] = item
    rows = sorted(normalized.values(), key=lambda item: (item["decision_dt"], item["event_id"], item["phase"]))
    # Phase is an execution clock.  It may move forward, but never backward
    # for one event.  The input may be deliberately shuffled.
    phase_by_event: dict[str, int] = {}
    for item in rows:
        rank = PHASES.index(item["phase"])
        prior = phase_by_event.get(item["event_id"])
        if prior is not None and rank < prior:
            blockers.add("phase_nonmonotonic")
        phase_by_event[item["event_id"]] = max(rank, prior if prior is not None else rank)
    grouped = {phase: [row for row in rows if row["phase"] == phase] for phase in PHASES}
    phases = {phase: _phase_metrics(grouped[phase]) for phase in PHASES}
    windows: dict[str, dict[str, Any]] = {}
    for days in WINDOWS_DAYS:
        start = max(
            evaluation_start_dt,
            evaluation_end_dt - timedelta(days=days),
        )
        win_rows = [
            row
            for row in rows
            if start <= row["decision_dt"] < evaluation_end_dt
        ]
        windows[str(days)] = {
            "days": days,
            "window_start_utc": start.isoformat().replace("+00:00", "Z"),
            "window_end_utc": evaluation_end,
            "n_eff": float(len({row["event_id"] for row in win_rows})),
            "by_phase": {phase: _phase_metrics([row for row in win_rows if row["phase"] == phase]) for phase in PHASES},
        }
    if not rows:
        blockers.add("no_asof_observations")
    for phase in PHASES:
        if not grouped[phase]:
            blockers.add(f"phase_{phase.lower()}_unobserved")
    live_mix = "real_live_fill_mixed_in" in blockers
    if policy_promoted:
        blockers.add("artifact_already_promoted_not_candidate")
    if str(policy_evidence_status or "").upper() != "DESIGN_CANDIDATE":
        blockers.add("artifact_not_design_candidate")
    if policy_training_cutoff_utc is None:
        blockers.add("policy_training_cutoff_missing")
    # A complete promoted artifact needs settled attribution in every phase;
    # no-live evidence may prove candidate/nonfill but not pretend to be fills.
    for phase in PHASES:
        if grouped[phase] and not all(row["settlement_pnl_usdc"] is not None for row in grouped[phase]):
            blockers.add(f"phase_{phase.lower()}_settlement_attribution_incomplete")
        if phases[phase]["n_eff"] < float(minimum_unique_events_per_phase):
            blockers.add(f"phase_{phase.lower()}_sample_size_insufficient")
    missing_fault_cases = sorted(REQUIRED_FAULT_CASES - fault_names)
    if missing_fault_cases:
        blockers.add("required_fault_cases_missing")
    if any(not item["passed"] for item in normalized_faults):
        blockers.add("fault_case_failed")
    checks = {
        "target_only": bool(strict_target_owned and not live_mix and not any(
            item in blockers
            for item in {
                "evidence_owner_not_target",
                "production_mode_not_no_live",
                "evaluation_split_not_historical_oos",
                "evidence_role_not_historical_strict_oos",
            }
        )),
        "strict_asof": not any(item.startswith("future_") or "_violates_asof" in item for item in blockers),
        "price_coverage_complete": all(
            phases[p]["observation_count"] > 0
            and phases[p]["price_coverage"]["successes"]
            == phases[p]["observation_count"]
            for p in PHASES
        ),
        "candidate_fill_scorecard_complete": all(phases[p]["observation_count"] > 0 for p in PHASES),
        "pnl_attribution_complete": all(not any(item.startswith(f"phase_{p.lower()}_settlement") for item in blockers) for p in PHASES),
        "fault_scorecard_complete": bool(
            REQUIRED_FAULT_CASES <= fault_names
            and normalized_faults
            and all(item["passed"] for item in normalized_faults)
        ),
        "phase_monotonic": "phase_nonmonotonic" not in blockers,
        "cash_phase_does_not_change_weather_plan": True,
    }
    windows_complete = all(
        windows[str(days)]["n_eff"] > 0
        and all(
            windows[str(days)]["by_phase"][phase]["n_eff"] > 0
            for phase in PHASES
        )
        for days in WINDOWS_DAYS
    )
    checks["windows_3_7_15_30_complete"] = windows_complete
    checks["sample_size_complete"] = not any(
        item.endswith("_sample_size_insufficient") for item in blockers
    )
    checks["strict_time_extrapolation"] = bool(
        training_cutoff_dt is not None
        and training_cutoff_dt <= evaluation_start_dt
        and all(row["decision_dt"] >= evaluation_start_dt for row in rows)
    )
    checks["promotion_eligible"] = bool(
        not policy_promoted
        and str(policy_evidence_status or "").upper() == "DESIGN_CANDIDATE"
        and not blockers
        and all(checks.values())
    )
    artifact: dict[str, Any] = {
        "schema": SCHEMA,
        "oos_data_policy": "HISTORICAL_CLOSED_DAY_ONLY",
        "future_data_evidence_role": "observational_candidate_not_oos",
        "future_data_counts_as_oos": False,
        "as_of_utc": as_of,
        "policy_version": version,
        "policy_promoted": bool(policy_promoted),
        "policy_evidence_status": str(policy_evidence_status or "").upper(),
        "production_no_live": True,
        "live_order_authorized": False,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "scope": {
            "station_id": expected_station_id,
            "station_count": len({row["station_id"] for row in rows}),
            "event_count": len(rows),
            "decision_count": len(rows),
            "station_timezone": expected_station_timezone,
            "window_start_utc": evaluation_start,
            "window_end_utc": evaluation_end,
            "policy_training_cutoff_utc": training_cutoff,
        },
        "phase_observations": {phase: {"observed": bool(grouped[phase]), "count": len(grouped[phase]), "n_eff": phases[phase]["n_eff"]} for phase in PHASES},
        "attribution": {"by_phase": phases, "windows": windows},
        "coverage": {
            "price": {"complete": checks["price_coverage_complete"], "sample_count": len(rows)},
            "candidate_fill": {"complete": checks["candidate_fill_scorecard_complete"], "sample_count": len(rows)},
            "pnl_attribution": {"complete": checks["pnl_attribution_complete"], "sample_count": len(rows)},
        },
        "pnl_attribution": {
            "settled_event_count": sum(1 for row in rows if row["settlement_pnl_usdc"] is not None),
            "attributed_event_count": sum(1 for row in rows if row["settlement_pnl_usdc"] is not None),
            "by_phase": {phase: {"realized": phases[phase]["realized_pnl"], "settlement": phases[phase]["settlement_pnl"], "execution_cost": phases[phase]["execution_cost"], "failure_cost": phases[phase]["failure_cost"]} for phase in PHASES},
        },
        "fault_scorecard": {
            "case_count": len(normalized_faults),
            "all_boundary_cases_pass": bool(
                normalized_faults
                and REQUIRED_FAULT_CASES <= fault_names
                and all(item["passed"] for item in normalized_faults)
            ),
            "required_cases": sorted(REQUIRED_FAULT_CASES),
            "cases": normalized_faults,
        },
        "checks": checks,
        "blockers": sorted(blockers),
        "resource_bounds": {
            "max_rows": max_rows,
            "rows_materialized": len(rows),
            "maximum_stations": MAX_STATIONS,
            "maximum_fault_cases": MAX_FAULT_CASES,
            "network": False,
            "database": False,
            "gateway": False,
        },
    }
    # Preserve the old promotion validator's required top-level fields while
    # carrying the richer v2 attribution under the same content address.
    artifact["content_sha256"] = content_sha256(artifact)
    return artifact


def _registry_station_timezones(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, str], list[str]]:
    if len(rows) > MAX_REGISTRY_ROWS:
        raise ContractError("cash_phase_oos_registry_rows_exceeded")
    values: dict[str, set[str]] = defaultdict(set)
    blockers: list[str] = []
    for raw in rows:
        row = _mapping("cash_phase_registry_row", raw)
        try:
            station = _text("cash_phase_registry_station_id", row.get("station_id"))
            timezone_name = _text(
                "cash_phase_registry_settlement_timezone",
                row.get("settlement_timezone"),
            )
            ZoneInfo(timezone_name)
        except (ContractError, ZoneInfoNotFoundError, ValueError):
            blockers.append("registry_row_invalid")
            continue
        values[station].add(timezone_name)
    result: dict[str, str] = {}
    for station, timezones in sorted(values.items()):
        if len(timezones) != 1:
            blockers.append(f"registry_timezone_not_unique:{station}")
            continue
        result[station] = next(iter(timezones))
    return result, sorted(set(blockers))


def _policy_phase_blockers(
    policy: BasketCashPhasePolicyArtifact,
    row: Mapping[str, Any],
) -> list[str]:
    blockers: list[str] = []
    if str(row.get("policy_version") or "") != policy.policy_version:
        blockers.append("cash_phase_policy_version_mismatch")
    supplied_sha = str(row.get("policy_sha256") or "")
    if supplied_sha and supplied_sha != policy.content_sha256:
        blockers.append("cash_phase_policy_sha256_mismatch")
    try:
        phase_input = BasketCashPhaseInput.from_mapping({
            "policy_version": policy.policy_version,
            "station_id": policy.station_id,
            "current_phase": row.get("current_phase"),
            "event_date_local": row.get("event_date_local"),
            "observed_at_utc": row.get(
                "decision_at_utc",
                row.get("observed_at_utc"),
            ),
            "station_timezone": policy.station_timezone,
            "checkpoint_id": row.get(
                "checkpoint_id",
                row.get("observation_id"),
            ),
        })
        decision = evaluate_cash_phase(policy, phase_input)
        if decision.next_phase.value != _phase(
            row.get("phase", row.get("cash_phase"))
        ):
            blockers.append("cash_phase_clock_decision_mismatch")
    except ContractError as exc:
        blockers.append("cash_phase_decision_invalid:" + str(exc))
    return sorted(set(blockers))


def build_cash_phase_oos_evidence(
    observations: Iterable[Mapping[str, Any]],
    *,
    policies: Sequence[BasketCashPhasePolicyArtifact | Mapping[str, Any]],
    registry_rows: Sequence[Mapping[str, Any]],
    policy_training_cutoffs: Mapping[str, str],
    fault_cases: Sequence[Mapping[str, Any]],
    as_of_utc: str,
    evaluation_start_utc: str,
    evaluation_end_utc: str,
    expected_station_ids: Sequence[str],
    minimum_unique_events_per_phase: int = DEFAULT_MIN_UNIQUE_EVENTS_PER_PHASE,
    max_rows: int = MAX_ROWS,
) -> dict[str, Any]:
    """Build a 45-station, target-owned OOS promotion evidence candidate.

    ``PROMOTION_ELIGIBLE`` is evidence for a later governed decision only.
    The function never mutates a DESIGN_CANDIDATE, writes an authority, opens
    a database, or authorizes live/gateway execution.
    """

    as_of, as_of_dt = _utc("as_of_utc", as_of_utc)
    evaluation_start, evaluation_start_dt = _utc(
        "evaluation_start_utc",
        evaluation_start_utc,
    )
    evaluation_end, evaluation_end_dt = _utc(
        "evaluation_end_utc",
        evaluation_end_utc,
    )
    if not evaluation_start_dt < evaluation_end_dt <= as_of_dt:
        raise ContractError("cash_phase_oos_evaluation_window_invalid")
    if (
        isinstance(max_rows, bool)
        or not isinstance(max_rows, int)
        or not 1 <= max_rows <= MAX_ROWS
    ):
        raise ContractError("cash_phase_oos_max_rows_invalid")
    source = list(islice(iter(observations), max_rows + 1))
    if len(source) > max_rows:
        raise ContractError("cash_phase_oos_observation_rows_exceeded")
    if len(policies) > MAX_STATIONS:
        raise ContractError("cash_phase_oos_policy_rows_exceeded")
    expected = sorted({_text("expected_station_id", item) for item in expected_station_ids})
    registry, global_blockers = _registry_station_timezones(registry_rows)
    if len(expected) != MAX_STATIONS:
        global_blockers.append(
            f"expected_station_count_not_{MAX_STATIONS}:{len(expected)}"
        )
    if set(registry) != set(expected):
        global_blockers.append("registry_station_scope_mismatch")
    if not isinstance(policy_training_cutoffs, Mapping):
        raise ContractError("cash_phase_oos_training_cutoffs_mapping_required")

    parsed_policies: list[BasketCashPhasePolicyArtifact] = []
    for raw in policies:
        try:
            parsed_policies.append(
                raw
                if isinstance(raw, BasketCashPhasePolicyArtifact)
                else BasketCashPhasePolicyArtifact.from_mapping(raw)
            )
        except ContractError as exc:
            global_blockers.append("policy_invalid:" + str(exc))
    policies_by_station: dict[str, list[BasketCashPhasePolicyArtifact]] = defaultdict(list)
    for policy in parsed_policies:
        policies_by_station[policy.station_id].append(policy)

    station_scorecards: dict[str, dict[str, Any]] = {}
    station_gaps: dict[str, list[str]] = {}
    decision_candidates: list[dict[str, Any]] = []
    for station in expected:
        gaps: set[str] = set()
        timezone_name = registry.get(station)
        station_policies = policies_by_station.get(station, [])
        if timezone_name is None:
            gaps.add("registry_timezone_missing")
        if len(station_policies) != 1:
            gaps.add(
                "design_candidate_policy_not_unique:"
                + str(len(station_policies))
            )
        policy = station_policies[0] if len(station_policies) == 1 else None
        if policy is not None:
            if policy.promoted or policy.evidence_status != "DESIGN_CANDIDATE":
                gaps.add("policy_not_design_candidate")
            if timezone_name is not None and policy.station_timezone != timezone_name:
                gaps.add("policy_registry_timezone_mismatch")
            policy_from = _utc("policy_valid_from_utc", policy.valid_from_utc)[1]
            policy_until = _utc("policy_valid_until_utc", policy.valid_until_utc)[1]
            if not (
                policy_from <= evaluation_start_dt
                and evaluation_end_dt <= policy_until
            ):
                gaps.add("policy_does_not_cover_evaluation_window")
        training_cutoff = policy_training_cutoffs.get(station)
        if training_cutoff is None:
            gaps.add("policy_training_cutoff_missing")
        else:
            cutoff_dt = _utc(
                "policy_training_cutoff_utc",
                training_cutoff,
            )[1]
            if cutoff_dt > evaluation_start_dt:
                gaps.add("policy_training_overlaps_evaluation")

        station_rows = [
            row
            for row in source
            if isinstance(row, Mapping)
            and str(row.get("station_id") or "") == station
        ]
        if not station_rows:
            gaps.add("forward_realized_observations_missing")
        if policy is not None:
            for row in station_rows:
                gaps.update(_policy_phase_blockers(policy, row))

        if policy is None or timezone_name is None:
            station_gaps[station] = sorted(gaps)
            continue
        scorecard = build_cash_phase_oos_scorecard(
            station_rows,
            as_of_utc=as_of,
            policy_version=policy.policy_version,
            policy_promoted=policy.promoted,
            policy_evidence_status=policy.evidence_status,
            expected_station_id=station,
            expected_station_timezone=timezone_name,
            evaluation_start_utc=evaluation_start,
            evaluation_end_utc=evaluation_end,
            policy_training_cutoff_utc=training_cutoff,
            minimum_unique_events_per_phase=minimum_unique_events_per_phase,
            fault_cases=fault_cases,
            strict_target_owned=True,
            max_rows=max_rows,
        )
        gaps.update(scorecard["blockers"])
        station_scorecards[station] = scorecard
        if not gaps and scorecard["checks"]["promotion_eligible"] is True:
            decision_candidates.append({
                "schema": PROMOTION_DECISION_SCHEMA,
                "station_id": station,
                "station_timezone": timezone_name,
                "policy_version": policy.policy_version,
                "policy_sha256": policy.content_sha256,
                "scorecard_sha256": scorecard["content_sha256"],
                "decision": "PROMOTION_ELIGIBLE",
                "mutates_policy": False,
                "writes_authority": False,
            })
        station_gaps[station] = sorted(gaps)

    foreign_observation_stations = sorted({
        str(row.get("station_id") or "")
        for row in source
        if isinstance(row, Mapping)
        and str(row.get("station_id") or "")
        and str(row.get("station_id") or "") not in set(expected)
    })
    if foreign_observation_stations:
        global_blockers.append("observation_station_scope_mismatch")
    eligible = bool(
        len(expected) == MAX_STATIONS
        and not global_blockers
        and len(decision_candidates) == MAX_STATIONS
        and all(not gaps for gaps in station_gaps.values())
    )
    status = "PROMOTION_ELIGIBLE" if eligible else "NA"
    core: dict[str, Any] = {
        "schema": EVIDENCE_BUNDLE_SCHEMA,
        "status": status,
        "as_of_utc": as_of,
        "evaluation_window": {
            "start_utc_inclusive": evaluation_start,
            "end_utc_exclusive": evaluation_end,
            "strict_half_open": True,
        },
        "sample_policy": {
            "minimum_unique_events_per_phase": minimum_unique_events_per_phase,
            "required_phases": list(PHASES),
            "required_station_count": MAX_STATIONS,
        },
        "station_ids": expected,
        "station_timezones": {
            station: registry[station]
            for station in expected
            if station in registry
        },
        "station_scorecards": station_scorecards,
        "station_gaps": station_gaps,
        "global_blockers": sorted(set(global_blockers)),
        "promotion_decision_candidates": (
            decision_candidates if eligible else []
        ),
        "promotion_eligible_station_count": len(decision_candidates),
        "design_candidate_mutated": False,
        "writes_authority": False,
        "writes_production_db": False,
        "production_no_live": True,
        "live_order_authorized": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "resource_bounds": {
            "max_rows": max_rows,
            "rows_received": len(source),
            "max_stations": MAX_STATIONS,
            "max_registry_rows": MAX_REGISTRY_ROWS,
            "max_fault_cases": MAX_FAULT_CASES,
        },
    }
    return {**core, "content_sha256": content_sha256(core)}


def assert_cash_phase_oos_promotable(artifact: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(artifact, Mapping) or artifact.get("schema") != SCHEMA:
        raise CashPhaseOOSBlocked("cash_phase_oos_artifact_schema_invalid")
    if artifact.get("checks", {}).get("promotion_eligible") is not True:
        raise CashPhaseOOSBlocked("cash_phase_oos_not_promotable")
    expected = content_sha256({key: value for key, value in artifact.items() if key != "content_sha256"})
    if artifact.get("content_sha256") != expected:
        raise CashPhaseOOSBlocked("cash_phase_oos_content_sha_mismatch")
    return artifact


# Readable aliases used by scorecard/promotion callers.
build_cash_phase_forward_oos = build_cash_phase_oos_scorecard
validate_cash_phase_oos_promotion = assert_cash_phase_oos_promotable


__all__ = [
    "CashPhaseOOSBlocked",
    "DEFAULT_MIN_UNIQUE_EVENTS_PER_PHASE",
    "EVIDENCE_BUNDLE_SCHEMA",
    "MAX_FAULT_CASES",
    "MAX_ROWS",
    "MAX_STATIONS",
    "PROMOTION_DECISION_SCHEMA",
    "REQUIRED_FAULT_CASES",
    "SCHEMA",
    "WINDOWS_DAYS",
    "assert_cash_phase_oos_promotable",
    "build_cash_phase_forward_oos",
    "build_cash_phase_oos_evidence",
    "build_cash_phase_oos_scorecard",
    "validate_cash_phase_oos_promotion",
]
