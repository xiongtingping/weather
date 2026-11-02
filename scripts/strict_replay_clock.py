"""Exact, fail-closed clocks for offline historical evidence (no I/O)."""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


class EvidenceClockError(ValueError):
    pass


def instant(value: str | datetime) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone missing")
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError) as exc:
        raise EvidenceClockError("invalid_evidence_timestamp") from exc


def day_end(event_date: str, timezone_name: str) -> datetime:
    try:
        return datetime.combine(date.fromisoformat(event_date) + timedelta(days=1), time(), ZoneInfo(timezone_name)).astimezone(timezone.utc)
    except (ValueError, TypeError, KeyError) as exc:
        raise EvidenceClockError("invalid_station_day_clock") from exc


def case_truth_known(case: Mapping[str, Any], as_of: datetime) -> bool:
    required = ("truth_id", "truth_revision_no", "truth_canonical_committed_at_utc", "event_date", "timezone_name")
    if any(k not in case or case[k] is None for k in required) or not case["truth_id"]:
        raise EvidenceClockError("case_truth_receipt_missing")
    if not isinstance(case["truth_revision_no"], int) or case["truth_revision_no"] < 0:
        raise EvidenceClockError("invalid_truth_revision")
    now = instant(as_of)
    return max(instant(case["truth_canonical_committed_at_utc"]), day_end(case["event_date"], case["timezone_name"])) <= now


def select_truth_asof(rows: Sequence[Mapping[str, Any]], *, station: str, event_date: str,
                     timezone_name: str, settlement_definition: str, as_of: datetime) -> dict[str, Any] | None:
    """Choose an actual formal revision; never copy latest values onto old clocks."""
    now = instant(as_of)
    if day_end(event_date, timezone_name) > now:
        return None
    eligible = []
    for row in rows:
        if row["station_id"] != station or row["local_date"] != event_date:
            raise EvidenceClockError("truth_scope_mismatch")
        if row["settlement_timezone"] != timezone_name or row["settlement_definition"] != settlement_definition:
            raise EvidenceClockError("truth_contract_mismatch")
        if row["source_lane"] not in {"AWC", "SETTLEMENT_SOURCE"} or row["qc_status"] != "ACCEPTED":
            continue
        if row["authority_scope"] not in {"FORMAL_NATIVE_ELIGIBLE", "NO_LIVE_REPLAY_CANDIDATE"}:
            continue
        if row["timestamps_complete"] != 1:
            raise EvidenceClockError("truth_timestamps_incomplete")
        if not row["truth_id"] or not math.isfinite(float(row["official_tmax_c"])):
            raise EvidenceClockError("invalid_truth_value")
        if not isinstance(row["revision_no"], int) or row["revision_no"] < 0:
            raise EvidenceClockError("invalid_truth_revision")
        if instant(row["canonical_committed_at_utc"]) <= now:
            eligible.append(row)
    if not eligible:
        return None
    revision = max(r["revision_no"] for r in eligible)
    winners = [r for r in eligible if r["revision_no"] == revision]
    if len(winners) != 1:
        raise EvidenceClockError("ambiguous_truth_revision")
    return dict(winners[0])


def exact_member_arrival(case: Mapping[str, Any], receipts: Mapping[str, Mapping[str, Any]],
                         *, station: str, system: str, run_cycle: str) -> datetime:
    """All exact member contents must be received; run init is not arrival."""
    hashes = case.get("member_run_content_shas")
    if not hashes or len(hashes) != len(set(hashes)) or len(hashes) != len(case["values"]):
        raise EvidenceClockError("member_identity_missing_or_duplicate")
    stamps = []
    members = set()
    for sha in hashes:
        row = receipts.get(sha)
        if row is None or row["content_sha256"] != sha:
            raise EvidenceClockError("exact_member_receipt_missing")
        if (row["station_id"], row["ensemble_system_id"], row["target_local_date"], row["archive_lead_bucket"], row["run_cycle"]) != (
                station, system, case["event_date"], case["archive_lead_bucket"], run_cycle):
            raise EvidenceClockError("member_scope_mismatch")
        if row["qc_status"] != "ACCEPTED" or row["transport_provider_id"] == "OPEN_METEO":
            raise EvidenceClockError("member_not_native_accepted")
        if row["member_id"] in members:
            raise EvidenceClockError("duplicate_member")
        members.add(row["member_id"])
        arrived = instant(row["archive_query_at_utc"])
        if arrived < instant(row["native_run_init_utc"]):
            raise EvidenceClockError("arrival_before_run_init")
        stamps.append(arrived)
    return max(stamps)


def training_fingerprint(cases: Sequence[Mapping[str, Any]]) -> tuple:
    return tuple((c["event_date"], c["member_set_sha256"], c["truth_id"], c["truth_revision_no"],
                  c["truth_canonical_committed_at_utc"]) for c in cases)
