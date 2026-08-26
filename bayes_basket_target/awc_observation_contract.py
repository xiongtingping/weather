"""Strict PR12 canonical AWC observation revision and day-bounds contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .contracts import ContractError, content_sha256
from .pr12_authority_scope_projection import validate_pr12_authority_scope_projection
from .source_policy import source_policy_reason


CONTRACT_SCHEMA = "bayes_basket_target_canonical_awc_observation_contract_v2"
COVERAGE_RECEIPT_SCHEMA = "bayes_basket_target_pr12_awc_hourly_coverage_receipt_v1"
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
# Retained only so a caller can still name the Eastern zone; it is no longer
# the fleet's settlement zone.  Each station settles on the zone its own
# market listing declares (doctrine, 2026-08-26 canonical-day 口径换代).
SETTLEMENT_TIMEZONE = "America/New_York"
RAW_OBJECT_KIND = "AWC_RAW_METAR_TEXT_UTF8"
AWC_ORIGIN_SOURCE_IDS = frozenset({"global_noaa_awc_relay", "us_noaa_awc"})
AWC_ORIGIN_AUTHORITIES = frozenset(
    {
        "NOAA/NWS Aviation Weather Center worldwide relay",
        "NOAA/NWS Aviation Weather Center",
    }
)
AWC_ORIGIN_URL = "https://aviationweather.gov/data/api/"


def _text(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"awc_observation_{label}_missing")
    return result


def _sha(value: Any, label: str) -> str:
    result = _text(value, label).lower()
    if len(result) != 64 or any(c not in "0123456789abcdef" for c in result):
        raise ContractError(f"awc_observation_{label}_invalid")
    return result


def _utc(value: Any, label: str) -> str:
    text = _text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"awc_observation_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"awc_observation_{label}_timezone_required")
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _date(value: Any, label: str) -> str:
    text = _text(value, label)
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ContractError(f"awc_observation_{label}_invalid") from exc


def _strict(raw: Mapping[str, Any], fields: set[str], label: str) -> None:
    if set(raw) != fields:
        raise ContractError(f"awc_observation_{label}_fields_mismatch")


@dataclass(frozen=True)
class CanonicalAWCObservationRevisionFact:
    raw: dict[str, Any]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalAWCObservationRevisionFact":
        fields = {
            "station_id", "observation_id", "revision_no", "event_date",
            "settlement_timezone", "source_lane", "authority_scope", "report_type",
            "authority_scope_projection_content_sha256",
            "sequence", "observed_at_utc", "received_at_utc",
            "first_seen_at_utc", "parsed_at_utc", "fetch_started_at_utc",
            "fetch_completed_at_utc", "source_observation_committed_at_utc",
            "canonical_committed_at_utc",
            "reported_temperature_c",
            "raw_object_kind", "raw_object_sha256",
            "awc_source_receipt_sha256",
            "origin_source_id", "origin_authority", "origin_url",
            "origin_transport", "origin_registry_sha256",
            "origin_record_sha256", "origin_committed_at_utc",
            "correction_kind", "correction_state",
            "supersedes_observation_fact_sha256", "qc_state", "review_receipt_sha256",
            "halt_state", "content_sha256",
        }
        _strict(raw, fields, "revision")
        value = dict(raw)
        value["authority_scope_projection_content_sha256"] = _sha(
            value["authority_scope_projection_content_sha256"],
            "authority_scope_projection_content_sha256",
        )
        if value["source_lane"] != "AWC" or value["authority_scope"] != AUTHORITY_SCOPE:
            raise ContractError("awc_observation_revision_authority_invalid")
        if value["report_type"] not in {"METAR", "SPECI"}:
            raise ContractError("awc_observation_revision_report_type_invalid")
        value["station_id"] = _text(value["station_id"], "station_id").upper()
        value["event_date"] = _date(value["event_date"], "event_date")
        if int(value["revision_no"]) < 1 or int(value["sequence"]) < 1:
            raise ContractError("awc_observation_revision_sequence_invalid")
        value["revision_no"] = int(value["revision_no"])
        value["sequence"] = int(value["sequence"])
        if isinstance(value["reported_temperature_c"], bool) or not isinstance(
            value["reported_temperature_c"], (int, float)
        ):
            raise ContractError("awc_observation_revision_temperature_invalid")
        value["reported_temperature_c"] = float(value["reported_temperature_c"])
        if not -100.0 <= value["reported_temperature_c"] <= 70.0:
            raise ContractError("awc_observation_revision_temperature_invalid")
        if value["raw_object_kind"] != RAW_OBJECT_KIND:
            raise ContractError("awc_observation_revision_raw_object_kind_invalid")
        for field in (
            "raw_object_sha256",
            "awc_source_receipt_sha256",
            "origin_registry_sha256",
            "origin_record_sha256",
        ):
            value[field] = _sha(value[field], field)
        for field in (
            "observed_at_utc",
            "received_at_utc",
            "first_seen_at_utc",
            "parsed_at_utc",
            "fetch_started_at_utc",
            "fetch_completed_at_utc",
            "source_observation_committed_at_utc",
            "origin_committed_at_utc",
            "canonical_committed_at_utc",
        ):
            value[field] = _utc(value[field], field)
        if not (
            value["observed_at_utc"]
            <= value["received_at_utc"]
            == value["first_seen_at_utc"]
            <= value["parsed_at_utc"]
            <= value["source_observation_committed_at_utc"]
            <= value["canonical_committed_at_utc"]
            and value["fetch_started_at_utc"]
            <= value["first_seen_at_utc"]
            <= value["fetch_completed_at_utc"]
            <= value["canonical_committed_at_utc"]
            and value["origin_committed_at_utc"] <= value["canonical_committed_at_utc"]
        ):
            raise ContractError("awc_observation_revision_timestamp_order_invalid")
        if (
            value["origin_source_id"] not in AWC_ORIGIN_SOURCE_IDS
            or value["origin_url"] != AWC_ORIGIN_URL
            or not _text(value["origin_transport"], "origin_transport").startswith(
                "official_awc_"
            )
            or _text(value["origin_authority"], "origin_authority")
            not in AWC_ORIGIN_AUTHORITIES
        ):
            raise ContractError("awc_observation_revision_origin_lineage_invalid")
        # The station's own settlement zone, from the sealed projection this
        # fact is bound to.  Pinning one zone fleet-wide is what filed 42
        # stations' reports under an Eastern day they do not settle on.
        try:
            zone = ZoneInfo(str(value["settlement_timezone"]))
        except Exception as exc:
            raise ContractError("awc_observation_revision_timezone_invalid") from exc
        observed = datetime.fromisoformat(value["observed_at_utc"].replace("Z", "+00:00"))
        if observed.astimezone(zone).date().isoformat() != value["event_date"]:
            raise ContractError("awc_observation_revision_event_date_mismatch")
        # Not a minute rule of its own: the builder and this validator have to
        # agree on what is formal, and both now ask the lane-wide policy the
        # operator settled on 2026-08-10 -- a routine METAR is formal at its
        # provider's own official minute, which for 18 of the 45 stations is
        # never :00.
        if source_policy_reason(
            source_lane="awc",
            report_type=value["report_type"],
            valid_at=observed,
            expected_lane="awc",
        ):
            raise ContractError("awc_observation_revision_source_policy_invalid")
        if value["correction_kind"] not in {"NONE", "COR", "AMD"}:
            raise ContractError("awc_observation_revision_correction_kind_invalid")
        if value["correction_state"] not in {"NO_CORRECTION", "REVIEW_REQUIRED", "RESOLVED"}:
            raise ContractError("awc_observation_revision_correction_state_invalid")
        if value["qc_state"] not in {"ACCEPTED", "REVIEW_REQUIRED"} or value["halt_state"] not in {"CLEAR", "HALTED"}:
            raise ContractError("awc_observation_revision_state_invalid")
        for field in ("supersedes_observation_fact_sha256", "review_receipt_sha256"):
            if value[field] is not None:
                value[field] = _sha(value[field], field)
        if value["correction_kind"] == "NONE":
            if (
                value["correction_state"] != "NO_CORRECTION"
                or value["qc_state"] != "ACCEPTED"
                or value["halt_state"] != "CLEAR"
                or any(
                    value[field] is not None
                    for field in (
                        "supersedes_observation_fact_sha256",
                        "review_receipt_sha256",
                    )
                )
            ):
                raise ContractError("awc_observation_revision_unexpected_review_lineage")
        elif value["correction_state"] == "REVIEW_REQUIRED":
            if (
                value["qc_state"] != "REVIEW_REQUIRED"
                or value["halt_state"] != "HALTED"
                or value["review_receipt_sha256"] is not None
                or value["supersedes_observation_fact_sha256"] is None
            ):
                raise ContractError("awc_observation_revision_review_state_invalid")
        elif value["correction_state"] == "RESOLVED":
            if (
                value["review_receipt_sha256"] is None
                or value["supersedes_observation_fact_sha256"] is None
                or value["qc_state"] != "ACCEPTED"
                or value["halt_state"] != "CLEAR"
            ):
                raise ContractError("awc_observation_revision_review_receipt_required")
        else:
            raise ContractError("awc_observation_revision_correction_state_invalid")
        supplied = _sha(value["content_sha256"], "revision_content_sha256")
        identity = {k: v for k, v in value.items() if k != "content_sha256"}
        if supplied != content_sha256(identity):
            raise ContractError("awc_observation_revision_content_sha256_mismatch")
        value["content_sha256"] = supplied
        return cls(value)

    @property
    def content_sha256(self) -> str:
        return str(self.raw["content_sha256"])

    @property
    def scope_key(self) -> str:
        return "|".join(str(self.raw[k]) for k in (
            "station_id", "observation_id", "revision_no", "observed_at_utc",
            "canonical_committed_at_utc",
            "authority_scope_projection_content_sha256",
        ))


@dataclass(frozen=True)
class CanonicalAWCObservationDayBoundsFact:
    raw: dict[str, Any]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalAWCObservationDayBoundsFact":
        fields = {
            "station_id", "event_date", "settlement_timezone", "bounds_version",
            "source_lane",
            "authority_scope_projection_content_sha256",
            "day_start_utc", "day_end_utc", "running_max_c",
            "max_observation_fact_sha256", "observation_revision_vector_sha256",
            "max_tie_vector_sha256", "max_tie_break_rule",
            "coverage_receipt", "coverage_receipt_sha256",
            "correction_state", "halt_state", "latest_review_receipt_sha256",
            "closed_day", "canonical_committed_at_utc", "authority_scope", "content_sha256",
        }
        _strict(raw, fields, "day_bounds")
        value = dict(raw)
        value["authority_scope_projection_content_sha256"] = _sha(
            value["authority_scope_projection_content_sha256"],
            "authority_scope_projection_content_sha256",
        )
        if (
            value["source_lane"] != "AWC"
            or value["authority_scope"] != AUTHORITY_SCOPE
            or int(value["bounds_version"]) < 1
        ):
            raise ContractError("awc_observation_day_bounds_authority_invalid")
        value["station_id"] = _text(value["station_id"], "station_id").upper()
        value["event_date"] = _date(value["event_date"], "event_date")
        value["bounds_version"] = int(value["bounds_version"])
        if isinstance(value["running_max_c"], bool) or not isinstance(
            value["running_max_c"], (int, float)
        ):
            raise ContractError("awc_observation_day_bounds_temperature_invalid")
        value["running_max_c"] = float(value["running_max_c"])
        for field in ("day_start_utc", "day_end_utc", "canonical_committed_at_utc"):
            value[field] = _utc(value[field], field)
        if not value["day_start_utc"] < value["day_end_utc"] <= value["canonical_committed_at_utc"]:
            raise ContractError("awc_observation_day_bounds_timestamp_order_invalid")
        try:
            zone = ZoneInfo(str(value["settlement_timezone"]))
        except Exception as exc:
            raise ContractError("awc_observation_day_bounds_timezone_invalid") from exc
        day_start = datetime.fromisoformat(value["day_start_utc"].replace("Z", "+00:00"))
        day_end = datetime.fromisoformat(value["day_end_utc"].replace("Z", "+00:00"))
        if (
            day_start.astimezone(zone).date().isoformat() != value["event_date"]
            or day_end.astimezone(zone).date().isoformat()
            != (date.fromisoformat(value["event_date"]) + timedelta(days=1)).isoformat()
            or day_start.astimezone(zone).time().isoformat() != "00:00:00"
            or day_end.astimezone(zone).time().isoformat() != "00:00:00"
        ):
            raise ContractError("awc_observation_day_bounds_event_date_mismatch")
        for field in (
            "max_observation_fact_sha256",
            "observation_revision_vector_sha256",
            "max_tie_vector_sha256",
            "coverage_receipt_sha256",
        ):
            value[field] = _sha(value[field], field)
        if value["max_tie_break_rule"] != "EARLIEST_SEQUENCE_THEN_FACT_SHA":
            raise ContractError("awc_observation_day_bounds_max_tie_rule_invalid")
        coverage_fields = {
            "schema", "station_id", "event_date", "settlement_timezone",
            "as_of_utc", "expected_hourly_slots", "observed_hourly_slots",
            "expected_hourly_metar_count", "observed_hourly_metar_count",
            "coverage_complete", "source_evidence_receipt_sha256", "content_sha256",
        }
        coverage = value["coverage_receipt"]
        if not isinstance(coverage, Mapping):
            raise ContractError("awc_observation_day_bounds_coverage_receipt_invalid")
        _strict(coverage, coverage_fields, "coverage_receipt")
        coverage_value = dict(coverage)
        if coverage_value["schema"] != COVERAGE_RECEIPT_SCHEMA:
            raise ContractError("awc_observation_day_bounds_coverage_schema_invalid")
        if any(
            coverage_value[field] != value[field]
            for field in ("station_id", "event_date", "settlement_timezone")
        ):
            raise ContractError("awc_observation_day_bounds_coverage_scope_mismatch")
        expected_slots = [_utc(x, "coverage_expected_slot") for x in coverage_value["expected_hourly_slots"]]
        observed_slots = [_utc(x, "coverage_observed_slot") for x in coverage_value["observed_hourly_slots"]]
        if (
            expected_slots != sorted(set(expected_slots))
            or observed_slots != sorted(set(observed_slots))
            or expected_slots != observed_slots
            or int(coverage_value["expected_hourly_metar_count"]) != len(expected_slots)
            or int(coverage_value["observed_hourly_metar_count"]) != len(observed_slots)
            or coverage_value["coverage_complete"] is not True
        ):
            raise ContractError("awc_observation_day_bounds_coverage_incomplete")
        coverage_value["expected_hourly_slots"] = expected_slots
        coverage_value["observed_hourly_slots"] = observed_slots
        coverage_value["expected_hourly_metar_count"] = len(expected_slots)
        coverage_value["observed_hourly_metar_count"] = len(observed_slots)
        coverage_value["as_of_utc"] = _utc(coverage_value["as_of_utc"], "coverage_as_of_utc")
        coverage_value["source_evidence_receipt_sha256"] = _sha(
            coverage_value["source_evidence_receipt_sha256"],
            "coverage_source_evidence_receipt_sha256",
        )
        coverage_sha = _sha(coverage_value["content_sha256"], "coverage_content_sha256")
        if coverage_sha != content_sha256(
            {k: v for k, v in coverage_value.items() if k != "content_sha256"}
        ) or coverage_sha != value["coverage_receipt_sha256"]:
            raise ContractError("awc_observation_day_bounds_coverage_sha256_mismatch")
        value["coverage_receipt"] = {**coverage_value, "content_sha256": coverage_sha}
        if value["latest_review_receipt_sha256"] is not None:
            value["latest_review_receipt_sha256"] = _sha(value["latest_review_receipt_sha256"], "latest_review_receipt_sha256")
        if value["correction_state"] not in {"NO_CORRECTION", "REVIEW_REQUIRED", "RESOLVED"} or value["halt_state"] not in {"CLEAR", "HALTED"}:
            raise ContractError("awc_observation_day_bounds_state_invalid")
        if (
            value["closed_day"] is not True
            or value["correction_state"] != "NO_CORRECTION"
            or value["halt_state"] != "CLEAR"
            or value["latest_review_receipt_sha256"] is not None
        ):
            raise ContractError("awc_observation_day_bounds_closed_day_required")
        supplied = _sha(value["content_sha256"], "day_bounds_content_sha256")
        identity = {k: v for k, v in value.items() if k != "content_sha256"}
        if supplied != content_sha256(identity):
            raise ContractError("awc_observation_day_bounds_content_sha256_mismatch")
        value["content_sha256"] = supplied
        return cls(value)

    @property
    def content_sha256(self) -> str:
        return str(self.raw["content_sha256"])

    @property
    def scope_key(self) -> str:
        return "|".join(str(self.raw[k]) for k in (
            "station_id", "event_date", "settlement_timezone", "bounds_version"
        ))


@dataclass(frozen=True)
class CanonicalAWCObservationContract:
    as_of_utc: str
    builder_id: str
    authority_scope_projection: dict[str, Any]
    revision_facts: tuple[CanonicalAWCObservationRevisionFact, ...]
    day_bounds_facts: tuple[CanonicalAWCObservationDayBoundsFact, ...]
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalAWCObservationContract":
        _strict(raw, {"schema", "as_of_utc", "builder_id", "authority_scope_projection", "revision_facts", "day_bounds_facts", "content_sha256"}, "contract")
        if raw["schema"] != CONTRACT_SCHEMA:
            raise ContractError("awc_observation_contract_schema_invalid")
        projection_raw = raw["authority_scope_projection"]
        if not isinstance(projection_raw, Mapping):
            raise ContractError("awc_observation_authority_projection_missing")
        projection = validate_pr12_authority_scope_projection(projection_raw)
        revisions = tuple(CanonicalAWCObservationRevisionFact.from_mapping(x) for x in raw["revision_facts"])
        bounds = tuple(CanonicalAWCObservationDayBoundsFact.from_mapping(x) for x in raw["day_bounds_facts"])
        if any(
            fact.raw["authority_scope_projection_content_sha256"]
            != projection["content_sha256"]
            for fact in (*revisions, *bounds)
        ):
            raise ContractError("awc_observation_authority_projection_parent_mismatch")
        # A fact's zone must be the one the projection binds for its station,
        # not merely a zone that its own event_date happens to agree with.
        # While one zone was pinned fleet-wide a plain equality check covered
        # this; now that each station carries its own, a fact could otherwise
        # declare any zone and stay internally consistent by shifting the
        # event_date to match it.
        listed_zone = {
            str(row["station_id"]).upper(): str(row["settlement_timezone"])
            for row in projection["rows"]
        }
        if any(
            listed_zone.get(str(fact.raw["station_id"]).upper())
            != str(fact.raw["settlement_timezone"])
            for fact in (*revisions, *bounds)
        ):
            raise ContractError("awc_observation_settlement_timezone_mismatch")
        if len({x.scope_key for x in revisions}) != len(revisions) or len({x.scope_key for x in bounds}) != len(bounds):
            raise ContractError("awc_observation_contract_scope_duplicate")
        sequence_keys = [
            (
                x.raw["station_id"],
                x.raw["event_date"],
                x.raw["settlement_timezone"],
                x.raw["sequence"],
            )
            for x in revisions
        ]
        if len(set(sequence_keys)) != len(sequence_keys):
            raise ContractError("awc_observation_contract_sequence_duplicate")
        revision_by_hash = {x.content_sha256: x for x in revisions}
        revisions_by_day: dict[tuple[str, str, str], list[CanonicalAWCObservationRevisionFact]] = {}
        for revision in revisions:
            key = (
                revision.raw["station_id"],
                revision.raw["event_date"],
                revision.raw["settlement_timezone"],
            )
            revisions_by_day.setdefault(key, []).append(revision)
        for scoped in revisions_by_day.values():
            ordered = sorted(
                scoped,
                key=lambda item: (
                    item.raw["observed_at_utc"],
                    item.raw["report_type"],
                    item.raw["observation_id"],
                    item.raw["revision_no"],
                    item.content_sha256,
                ),
            )
            if [item.raw["sequence"] for item in ordered] != list(
                range(1, len(ordered) + 1)
            ):
                raise ContractError("awc_observation_contract_sequence_gap")
            by_observation: dict[str, list[CanonicalAWCObservationRevisionFact]] = {}
            for item in ordered:
                by_observation.setdefault(item.raw["observation_id"], []).append(item)
                # v1 has no independently content-addressed review fact.  It
                # therefore cannot prove a correction resolution and must not
                # admit any correction into canonical truth by implication.
                if item.raw["correction_kind"] != "NONE":
                    raise ContractError(
                        "awc_observation_contract_correction_review_unbound"
                    )
            for observation_revisions in by_observation.values():
                if sorted(item.raw["revision_no"] for item in observation_revisions) != list(
                    range(1, len(observation_revisions) + 1)
                ):
                    raise ContractError("awc_observation_contract_revision_number_gap")
        for bound in bounds:
            day_key = (
                bound.raw["station_id"],
                bound.raw["event_date"],
                bound.raw["settlement_timezone"],
            )
            all_day_revisions = revisions_by_day.get(day_key, [])
            if not all_day_revisions or any(
                item.raw["qc_state"] != "ACCEPTED"
                or item.raw["halt_state"] != "CLEAR"
                or item.raw["correction_kind"] != "NONE"
                for item in all_day_revisions
            ):
                raise ContractError("awc_observation_day_bounds_unresolved_revision")
            scoped = sorted(
                (
                    x for x in all_day_revisions
                ),
                key=lambda x: x.raw["sequence"],
            )
            if not scoped:
                raise ContractError("awc_observation_day_bounds_revision_vector_empty")
            if [x.raw["sequence"] for x in scoped] != list(range(1, len(scoped) + 1)):
                raise ContractError("awc_observation_day_bounds_sequence_gap")
            if bound.raw["observation_revision_vector_sha256"] != content_sha256(
                [x.content_sha256 for x in scoped]
            ):
                raise ContractError("awc_observation_day_bounds_revision_vector_mismatch")
            maximum_value = max(x.raw["reported_temperature_c"] for x in scoped)
            tied = sorted(
                (x for x in scoped if x.raw["reported_temperature_c"] == maximum_value),
                key=lambda x: (x.raw["sequence"], x.content_sha256),
            )
            if bound.raw["max_tie_vector_sha256"] != content_sha256(
                sorted(x.content_sha256 for x in tied)
            ):
                raise ContractError("awc_observation_day_bounds_max_tie_vector_mismatch")
            maximum = revision_by_hash.get(bound.raw["max_observation_fact_sha256"])
            if maximum is None:
                raise ContractError("awc_observation_day_bounds_max_reference_missing")
            if (
                maximum.raw["station_id"] != bound.raw["station_id"]
                or maximum.raw["event_date"] != bound.raw["event_date"]
                or maximum.raw["settlement_timezone"]
                != bound.raw["settlement_timezone"]
                or maximum.raw["reported_temperature_c"] != bound.raw["running_max_c"]
                or bound.raw["running_max_c"] != maximum_value
                or maximum.content_sha256 != tied[0].content_sha256
            ):
                raise ContractError("awc_observation_day_bounds_max_reference_mismatch")
        as_of = _utc(raw["as_of_utc"], "contract_as_of_utc")
        if any(x.raw["canonical_committed_at_utc"] > as_of for x in (*revisions, *bounds)):
            raise ContractError("awc_observation_contract_future_fact")
        supplied = _sha(raw["content_sha256"], "contract_content_sha256")
        identity = {k: v for k, v in raw.items() if k != "content_sha256"}
        if supplied != content_sha256(identity):
            raise ContractError("awc_observation_contract_content_sha256_mismatch")
        return cls(
            as_of, _text(raw["builder_id"], "builder_id"), projection,
            revisions, bounds, supplied,
        )


__all__ = [
    "AUTHORITY_SCOPE", "AWC_ORIGIN_AUTHORITIES", "AWC_ORIGIN_SOURCE_IDS", "AWC_ORIGIN_URL", "CONTRACT_SCHEMA", "SETTLEMENT_TIMEZONE",
    "COVERAGE_RECEIPT_SCHEMA",
    "RAW_OBJECT_KIND", "CanonicalAWCObservationContract",
    "CanonicalAWCObservationDayBoundsFact", "CanonicalAWCObservationRevisionFact",
]
