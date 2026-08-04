"""Shared temporal guards for the historical TAF/P0-M OOS lane.

The live TAF resident consumes forward AWC receipts, but no forward receipt is
an OOS label.  This small, dependency-free module keeps the distinction
consistent across the exact join, nested transport evaluator and the native
P0-M evaluator.  A missing optional marker is tolerated for legacy *native*
rows when the settlement window is already closed; a row that identifies an
Open-Meteo/republisher transport must carry the explicit historical contract
pointer and eligibility bit.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Mapping

from .contracts import ContractError


HISTORICAL_OOS_TEMPORAL_SCOPE = "CLOSED_HISTORICAL_AWC_TRUTH"
OBSERVATIONAL_CANDIDATE_NOT_OOS = "observational_candidate_not_oos"
HISTORICAL_REPUBLISHER_RECEIPT = "HISTORICAL_ASOF_REPUBLISHER_RECEIPT"


def _utc(value: Any, *, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _future_count(value: Any, *, name: str) -> int:
    try:
        result = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if result < 0:
        raise ContractError(f"{name}_invalid")
    return result


def _republisher_transport(value: Any) -> bool:
    text = str(value or "").strip().upper()
    return "OPEN_METEO" in text or "REPUBLISH" in text or text.startswith("AGGREGATOR")


def assert_historical_taf_oos(
    row: Mapping[str, Any],
    *,
    event_date: str,
    truth_closed_at: datetime,
    as_of: datetime | None = None,
    transport_values: tuple[Any, ...] = (),
    name: str = "taf_oos",
) -> dict[str, Any]:
    """Validate temporal/lineage fields for one TAF OOS row.

    The helper never grants eligibility to a row.  It only rejects explicit
    forward/candidate claims and future closed-day evidence.  Native legacy
    rows may omit the new marker while retaining their existing strict closed
    window; republisher rows cannot use that compatibility path.
    """

    scope = str(row.get("oos_event_temporal_scope") or "").strip()
    evidence_role = str(
        row.get("evidence_role") or row.get("temporal_role") or ""
    ).strip().lower()
    if scope and scope != HISTORICAL_OOS_TEMPORAL_SCOPE:
        raise ContractError(f"{name}_forward_scope_forbidden")
    if evidence_role == OBSERVATIONAL_CANDIDATE_NOT_OOS:
        raise ContractError(f"{name}_observational_candidate_not_oos")
    future_count = _future_count(
        row.get("future_event_count", 0),
        name=f"{name}_future_event_count",
    )
    if future_count:
        raise ContractError(f"{name}_future_event_count_nonzero")
    try:
        parsed_date = date.fromisoformat(str(event_date))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_event_date_invalid") from exc
    cutoff = as_of.astimezone(timezone.utc) if as_of is not None else datetime.now(timezone.utc)
    closed = truth_closed_at.astimezone(timezone.utc)
    if closed > cutoff or parsed_date > cutoff.date():
        raise ContractError(f"{name}_future_event_not_oos")

    explicit_eligibility = "historical_oos_eligible" in row
    if explicit_eligibility:
        try:
            eligible_bit = int(row.get("historical_oos_eligible") or 0)
        except (TypeError, ValueError) as exc:
            raise ContractError(
                f"{name}_historical_oos_eligibility_required"
            ) from exc
        if eligible_bit != 1:
            raise ContractError(f"{name}_historical_oos_eligibility_required")

    republisher = any(_republisher_transport(value) for value in transport_values)
    contract_id = str(row.get("historical_oos_contract_id") or "").strip()
    if republisher:
        if not explicit_eligibility or not contract_id:
            raise ContractError(f"{name}_republisher_historical_contract_required")
        if scope != HISTORICAL_OOS_TEMPORAL_SCOPE:
            raise ContractError(f"{name}_republisher_historical_scope_required")
        receipt_class = str(
            row.get("receipt_class") or row.get("historical_oos_receipt_class") or ""
        ).strip()
        if receipt_class and receipt_class != HISTORICAL_REPUBLISHER_RECEIPT:
            raise ContractError(f"{name}_republisher_receipt_class_invalid")

    return {
        "oos_event_temporal_scope": HISTORICAL_OOS_TEMPORAL_SCOPE,
        "future_event_count": 0,
        "historical_oos_eligible": (
            bool(eligible_bit) if explicit_eligibility else True
        ),
        "historical_oos_contract_id": contract_id or None,
        "as_of_utc": cutoff.isoformat().replace("+00:00", "Z"),
    }


__all__ = [
    "HISTORICAL_OOS_TEMPORAL_SCOPE",
    "HISTORICAL_REPUBLISHER_RECEIPT",
    "OBSERVATIONAL_CANDIDATE_NOT_OOS",
    "assert_historical_taf_oos",
]
