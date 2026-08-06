"""Canonical surface observations and one priority winner per decision time.

Raw providers keep their own lineage and report kind.  The winner contract
contains the complete terminal vector, so an unavailable higher-priority lane
cannot disappear from the decision evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import csv
import gzip
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .contracts import ContractError, content_sha256


OBSERVATION_CONTRACT_SCHEMA = (
    "bayes_basket_target_canonical_surface_observation_contract_v1"
)
WINNER_CONTRACT_SCHEMA = (
    "bayes_basket_target_canonical_surface_observation_winner_contract_v1"
)
OBSERVATION_FACT_TYPE = "surface_observation"
WINNER_FACT_TYPE = "surface_observation_winner"
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
STATE = "MATERIALIZED_UNPUBLISHED"
PRIORITY_CONTRACT_VERSION = "SURFACE_OBSERVATION_PRIORITY_220_210_100_V1"
Q = Decimal("0.000000000001")

SOURCE_POLICY = {
    "IEM": {
        "source_class": "aggregator_republisher",
        "priority": 220,
        "report_types": frozenset({"ASOS_1MIN"}),
    },
    "MADIS": {
        "source_class": "official_republisher",
        "priority": 210,
        "report_types": frozenset({"ASOS_HF"}),
    },
    "AWC": {
        "source_class": "official_awc",
        "priority": 100,
        "report_types": frozenset({"METAR", "SPECI"}),
        "provider_id": "NOAA_AWC",
        "measurement_semantics": "AWC_METAR_SPECI_REMARK_TENTHS_CELSIUS",
    },
}

OBSERVATION_FIELDS = {
    "station_id", "event_date", "settlement_timezone", "source_lane",
    "source_class", "source_priority", "provider_id", "report_type",
    "measurement_semantics", "observation_id", "valid_at_utc",
    "source_committed_at_utc", "canonical_committed_at_utc",
    "reported_temperature_c", "temperature_resolution_c", "hard_lower_bound_c",
    "hard_lower_bound_method", "raw_record_sha256",
    "source_evidence_receipt_sha256", "origin_url", "qc_state",
    "correction_suspected", "authority_scope", "state", "content_sha256",
}
TERMINAL_FIELDS = {
    "source_lane", "source_class", "source_priority", "eligibility_state",
    "terminal_reason", "observation_count", "coverage_complete",
    "source_evidence_receipt_sha256", "observation_set_sha256",
    "observed_max_c", "max_valid_at_utc",
    "max_observation_content_sha256", "terminal_content_sha256",
}
WINNER_FIELDS = {
    "station_id", "event_date", "settlement_timezone", "winner_as_of_utc",
    "priority_contract_version", "candidate_terminals",
    "candidate_terminal_set_sha256", "selected_source_lane",
    "selected_source_class", "selected_source_priority",
    "selected_observation_content_sha256", "selected_report_type",
    "selected_valid_at_utc", "selected_committed_at_utc", "reported_high_c",
    "hard_lower_bound_c", "temperature_resolution_c", "hard_lower_bound_method",
    "selection_reason", "authority_scope", "state", "content_sha256",
}


def _text(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"surface_observation_{label}_missing")
    return result


def _sha(value: Any, label: str) -> str:
    result = _text(value, label).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"surface_observation_{label}_invalid")
    return result


def _utc(value: Any, label: str) -> str:
    text = _text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"surface_observation_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"surface_observation_{label}_timezone_required")
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _date(value: Any, label: str) -> str:
    try:
        return date.fromisoformat(_text(value, label)).isoformat()
    except ValueError as exc:
        raise ContractError(f"surface_observation_{label}_invalid") from exc


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ContractError(f"surface_observation_{label}_invalid")
    try:
        result = Decimal(_text(value, label))
    except InvalidOperation as exc:
        raise ContractError(f"surface_observation_{label}_invalid") from exc
    if not result.is_finite() or result < Decimal("-100") or result > Decimal("70"):
        raise ContractError(f"surface_observation_{label}_invalid")
    return result


def _num(value: Decimal) -> str:
    return format(value.quantize(Q, rounding=ROUND_HALF_EVEN), ".12f")


def _event_date_for(valid_at_utc: str, timezone_name: str) -> str:
    try:
        zone = ZoneInfo(timezone_name)
    except Exception as exc:
        raise ContractError("surface_observation_settlement_timezone_invalid") from exc
    parsed = datetime.fromisoformat(valid_at_utc.replace("Z", "+00:00"))
    return parsed.astimezone(zone).date().isoformat()


@dataclass(frozen=True)
class CanonicalSurfaceObservationFact:
    raw: dict[str, Any]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalSurfaceObservationFact":
        if set(raw) != OBSERVATION_FIELDS:
            raise ContractError("surface_observation_fact_fields_mismatch")
        value = dict(raw)
        value["station_id"] = _text(value["station_id"], "station_id").upper()
        value["event_date"] = _date(value["event_date"], "event_date")
        value["settlement_timezone"] = _text(
            value["settlement_timezone"], "settlement_timezone"
        )
        lane = _text(value["source_lane"], "source_lane").upper()
        policy = SOURCE_POLICY.get(lane)
        if policy is None:
            raise ContractError("surface_observation_source_lane_invalid")
        value["source_lane"] = lane
        value["source_class"] = _text(value["source_class"], "source_class")
        value["source_priority"] = int(value["source_priority"])
        value["report_type"] = _text(value["report_type"], "report_type").upper()
        if (
            value["source_class"] != policy["source_class"]
            or value["source_priority"] != policy["priority"]
            or value["report_type"] not in policy["report_types"]
        ):
            raise ContractError("surface_observation_source_lineage_invalid")
        value["provider_id"] = _text(value["provider_id"], "provider_id")
        value["measurement_semantics"] = _text(
            value["measurement_semantics"], "measurement_semantics"
        )
        value["observation_id"] = _text(value["observation_id"], "observation_id")
        if lane == "AWC" and (
            value["provider_id"] != policy["provider_id"]
            or value["measurement_semantics"] != policy["measurement_semantics"]
            or value["observation_id"]
            != f"AWC:{str(value['raw_record_sha256']).lower()}"
        ):
            raise ContractError("surface_observation_awc_adapter_mapping_invalid")
        for field in (
            "valid_at_utc", "source_committed_at_utc", "canonical_committed_at_utc"
        ):
            value[field] = _utc(value[field], field)
        if not (
            value["valid_at_utc"]
            <= value["source_committed_at_utc"]
            <= value["canonical_committed_at_utc"]
        ):
            raise ContractError("surface_observation_clock_order_invalid")
        if _event_date_for(
            value["valid_at_utc"], value["settlement_timezone"]
        ) != value["event_date"]:
            raise ContractError("surface_observation_event_date_mismatch")
        value["reported_temperature_c"] = _num(
            _decimal(value["reported_temperature_c"], "reported_temperature_c")
        )
        resolution = _decimal(value["temperature_resolution_c"], "temperature_resolution_c")
        if resolution <= 0:
            raise ContractError("surface_observation_temperature_resolution_invalid")
        lower = _decimal(value["hard_lower_bound_c"], "hard_lower_bound_c")
        value["temperature_resolution_c"] = _num(resolution)
        value["hard_lower_bound_c"] = _num(lower)
        if value["hard_lower_bound_method"] != "ROUND_NEAREST_RESOLUTION_LOWER_EDGE":
            raise ContractError("surface_observation_lower_bound_method_invalid")
        if lower != _decimal(value["reported_temperature_c"], "reported_temperature_c") - resolution / 2:
            raise ContractError("surface_observation_hard_lower_bound_mismatch")
        for field in ("raw_record_sha256", "source_evidence_receipt_sha256"):
            value[field] = _sha(value[field], field)
        value["origin_url"] = _text(value["origin_url"], "origin_url")
        if value["qc_state"] != "ACCEPTED":
            raise ContractError("surface_observation_qc_state_invalid")
        if not isinstance(value["correction_suspected"], bool):
            raise ContractError("surface_observation_correction_flag_invalid")
        if value["correction_suspected"]:
            raise ContractError("surface_observation_correction_review_required")
        if value["authority_scope"] != AUTHORITY_SCOPE or value["state"] != STATE:
            raise ContractError("surface_observation_authority_state_invalid")
        supplied = _sha(value["content_sha256"], "content_sha256")
        if supplied != content_sha256({k: v for k, v in value.items() if k != "content_sha256"}):
            raise ContractError("surface_observation_content_sha256_mismatch")
        value["content_sha256"] = supplied
        return cls(value)

    @property
    def content_sha256(self) -> str:
        return str(self.raw["content_sha256"])

    @property
    def scope_key(self) -> str:
        return "|".join(str(self.raw[key]) for key in (
            "station_id", "event_date", "settlement_timezone", "source_lane",
            "valid_at_utc", "report_type", "raw_record_sha256",
        ))


@dataclass(frozen=True)
class CanonicalSurfaceObservationContract:
    as_of_utc: str
    builder_id: str
    observation_facts: tuple[CanonicalSurfaceObservationFact, ...]
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalSurfaceObservationContract":
        fields = {"schema", "as_of_utc", "builder_id", "observation_facts", "content_sha256"}
        if set(raw) != fields or raw.get("schema") != OBSERVATION_CONTRACT_SCHEMA:
            raise ContractError("surface_observation_contract_invalid")
        rows = raw["observation_facts"]
        if not isinstance(rows, list) or not rows:
            raise ContractError("surface_observation_fact_required")
        facts = tuple(CanonicalSurfaceObservationFact.from_mapping(item) for item in rows)
        if len({item.scope_key for item in facts}) != len(facts):
            raise ContractError("surface_observation_scope_duplicate")
        as_of = _utc(raw["as_of_utc"], "contract_as_of_utc")
        if any(item.raw["canonical_committed_at_utc"] > as_of for item in facts):
            raise ContractError("surface_observation_fact_after_asof")
        supplied = _sha(raw["content_sha256"], "contract_content_sha256")
        if supplied != content_sha256({k: v for k, v in raw.items() if k != "content_sha256"}):
            raise ContractError("surface_observation_contract_hash_mismatch")
        return cls(as_of, _text(raw["builder_id"], "builder_id"), facts, supplied)


def _terminal(raw: Mapping[str, Any]) -> dict[str, Any]:
    if set(raw) != TERMINAL_FIELDS:
        raise ContractError("surface_observation_winner_terminal_fields_mismatch")
    value = dict(raw)
    lane = _text(value["source_lane"], "terminal_source_lane").upper()
    policy = SOURCE_POLICY.get(lane)
    if policy is None:
        raise ContractError("surface_observation_winner_terminal_lane_invalid")
    value["source_lane"] = lane
    value["source_class"] = _text(value["source_class"], "terminal_source_class")
    value["source_priority"] = int(value["source_priority"])
    if (
        value["source_class"] != policy["source_class"]
        or value["source_priority"] != policy["priority"]
    ):
        raise ContractError("surface_observation_winner_terminal_lineage_invalid")
    if value["eligibility_state"] not in {"ELIGIBLE", "INELIGIBLE", "DATA_MISSING"}:
        raise ContractError("surface_observation_winner_terminal_state_invalid")
    value["terminal_reason"] = _text(value["terminal_reason"], "terminal_reason")
    if isinstance(value["observation_count"], bool) or int(value["observation_count"]) < 0:
        raise ContractError("surface_observation_winner_terminal_count_invalid")
    value["observation_count"] = int(value["observation_count"])
    if not isinstance(value["coverage_complete"], bool):
        raise ContractError("surface_observation_winner_terminal_coverage_invalid")
    value["source_evidence_receipt_sha256"] = _sha(
        value["source_evidence_receipt_sha256"], "terminal_source_receipt"
    )
    value["observation_set_sha256"] = _sha(
        value["observation_set_sha256"], "terminal_observation_set"
    )
    optional = ("observed_max_c", "max_valid_at_utc", "max_observation_content_sha256")
    if value["eligibility_state"] == "ELIGIBLE":
        if not value["coverage_complete"] or value["observation_count"] < 1:
            raise ContractError("surface_observation_winner_terminal_eligible_invalid")
        value["observed_max_c"] = _num(_decimal(value["observed_max_c"], "terminal_max_c"))
        value["max_valid_at_utc"] = _utc(value["max_valid_at_utc"], "terminal_max_valid_at")
        value["max_observation_content_sha256"] = _sha(
            value["max_observation_content_sha256"], "terminal_max_observation"
        )
    else:
        for field in optional:
            if value[field] is not None:
                if field == "observed_max_c":
                    value[field] = _num(_decimal(value[field], "terminal_max_c"))
                elif field == "max_valid_at_utc":
                    value[field] = _utc(value[field], "terminal_max_valid_at")
                else:
                    value[field] = _sha(value[field], "terminal_max_observation")
    supplied = _sha(value["terminal_content_sha256"], "terminal_content_sha256")
    if supplied != content_sha256({k: v for k, v in value.items() if k != "terminal_content_sha256"}):
        raise ContractError("surface_observation_winner_terminal_hash_mismatch")
    value["terminal_content_sha256"] = supplied
    return value


@dataclass(frozen=True)
class CanonicalSurfaceObservationWinnerFact:
    raw: dict[str, Any]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalSurfaceObservationWinnerFact":
        if set(raw) != WINNER_FIELDS:
            raise ContractError("surface_observation_winner_fact_fields_mismatch")
        value = dict(raw)
        value["station_id"] = _text(value["station_id"], "winner_station_id").upper()
        value["event_date"] = _date(value["event_date"], "winner_event_date")
        value["settlement_timezone"] = _text(
            value["settlement_timezone"], "winner_settlement_timezone"
        )
        value["winner_as_of_utc"] = _utc(value["winner_as_of_utc"], "winner_as_of_utc")
        if value["priority_contract_version"] != PRIORITY_CONTRACT_VERSION:
            raise ContractError("surface_observation_winner_priority_contract_invalid")
        rows = value["candidate_terminals"]
        if not isinstance(rows, list) or len(rows) != len(SOURCE_POLICY):
            raise ContractError("surface_observation_winner_terminal_vector_invalid")
        terminals = [_terminal(item) for item in rows]
        terminals.sort(key=lambda item: (-item["source_priority"], item["source_lane"]))
        if [item["source_lane"] for item in terminals] != ["IEM", "MADIS", "AWC"]:
            raise ContractError("surface_observation_winner_terminal_scope_invalid")
        if value["candidate_terminal_set_sha256"] != content_sha256(terminals):
            raise ContractError("surface_observation_winner_terminal_set_hash_mismatch")
        eligible = [item for item in terminals if item["eligibility_state"] == "ELIGIBLE"]
        if not eligible:
            raise ContractError("surface_observation_winner_no_eligible_source")
        selected = max(eligible, key=lambda item: item["source_priority"])
        selected_fields = {
            "selected_source_lane": selected["source_lane"],
            "selected_source_class": selected["source_class"],
            "selected_source_priority": selected["source_priority"],
            "selected_observation_content_sha256": selected["max_observation_content_sha256"],
            "selected_valid_at_utc": selected["max_valid_at_utc"],
            "reported_high_c": selected["observed_max_c"],
        }
        for field, expected in selected_fields.items():
            if value[field] != expected:
                raise ContractError("surface_observation_winner_selected_terminal_mismatch")
        value["candidate_terminals"] = terminals
        value["selected_source_lane"] = selected["source_lane"]
        value["selected_source_class"] = selected["source_class"]
        value["selected_source_priority"] = selected["source_priority"]
        value["selected_observation_content_sha256"] = _sha(
            value["selected_observation_content_sha256"], "winner_observation"
        )
        value["selected_report_type"] = _text(
            value["selected_report_type"], "winner_report_type"
        ).upper()
        if value["selected_report_type"] not in SOURCE_POLICY[selected["source_lane"]]["report_types"]:
            raise ContractError("surface_observation_winner_report_type_invalid")
        value["selected_valid_at_utc"] = _utc(value["selected_valid_at_utc"], "winner_valid_at")
        value["selected_committed_at_utc"] = _utc(
            value["selected_committed_at_utc"], "winner_committed_at"
        )
        if not value["selected_valid_at_utc"] <= value["selected_committed_at_utc"] <= value["winner_as_of_utc"]:
            raise ContractError("surface_observation_winner_clock_order_invalid")
        value["reported_high_c"] = _num(_decimal(value["reported_high_c"], "winner_high_c"))
        value["hard_lower_bound_c"] = _num(
            _decimal(value["hard_lower_bound_c"], "winner_hard_lower_bound_c")
        )
        value["temperature_resolution_c"] = _num(
            _decimal(value["temperature_resolution_c"], "winner_temperature_resolution_c")
        )
        if value["hard_lower_bound_method"] != "ROUND_NEAREST_RESOLUTION_LOWER_EDGE":
            raise ContractError("surface_observation_winner_lower_bound_method_invalid")
        value["selection_reason"] = _text(value["selection_reason"], "selection_reason")
        if value["authority_scope"] != AUTHORITY_SCOPE or value["state"] != STATE:
            raise ContractError("surface_observation_winner_authority_state_invalid")
        supplied = _sha(value["content_sha256"], "winner_content_sha256")
        if supplied != content_sha256({k: v for k, v in value.items() if k != "content_sha256"}):
            raise ContractError("surface_observation_winner_content_sha256_mismatch")
        value["content_sha256"] = supplied
        return cls(value)

    @property
    def content_sha256(self) -> str:
        return str(self.raw["content_sha256"])

    @property
    def scope_key(self) -> str:
        return "|".join(str(self.raw[key]) for key in (
            "station_id", "event_date", "settlement_timezone", "winner_as_of_utc"
        ))


@dataclass(frozen=True)
class CanonicalSurfaceObservationWinnerContract:
    as_of_utc: str
    builder_id: str
    winner_facts: tuple[CanonicalSurfaceObservationWinnerFact, ...]
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalSurfaceObservationWinnerContract":
        fields = {"schema", "as_of_utc", "builder_id", "winner_facts", "content_sha256"}
        if set(raw) != fields or raw.get("schema") != WINNER_CONTRACT_SCHEMA:
            raise ContractError("surface_observation_winner_contract_invalid")
        rows = raw["winner_facts"]
        if not isinstance(rows, list) or not rows:
            raise ContractError("surface_observation_winner_fact_required")
        facts = tuple(CanonicalSurfaceObservationWinnerFact.from_mapping(item) for item in rows)
        if len({item.scope_key for item in facts}) != len(facts):
            raise ContractError("surface_observation_winner_scope_duplicate")
        as_of = _utc(raw["as_of_utc"], "winner_contract_as_of")
        if any(item.raw["winner_as_of_utc"] > as_of for item in facts):
            raise ContractError("surface_observation_winner_fact_after_asof")
        supplied = _sha(raw["content_sha256"], "winner_contract_content_sha256")
        if supplied != content_sha256({k: v for k, v in raw.items() if k != "content_sha256"}):
            raise ContractError("surface_observation_winner_contract_hash_mismatch")
        return cls(as_of, _text(raw["builder_id"], "winner_builder_id"), facts, supplied)


def build_surface_observation_contract(
    *, observation_inputs: Sequence[Mapping[str, Any]], as_of_utc: str
) -> dict[str, Any]:
    facts = []
    for raw in observation_inputs:
        identity = {k: v for k, v in raw.items() if k != "content_sha256"}
        identity["station_id"] = _text(identity["station_id"], "station_id").upper()
        identity["event_date"] = _date(identity["event_date"], "event_date")
        identity["settlement_timezone"] = _text(
            identity["settlement_timezone"], "settlement_timezone"
        )
        identity["source_lane"] = _text(identity["source_lane"], "source_lane").upper()
        identity["source_class"] = _text(identity["source_class"], "source_class")
        identity["source_priority"] = int(identity["source_priority"])
        identity["provider_id"] = _text(identity["provider_id"], "provider_id")
        identity["report_type"] = _text(identity["report_type"], "report_type").upper()
        identity["measurement_semantics"] = _text(
            identity["measurement_semantics"], "measurement_semantics"
        )
        identity["observation_id"] = _text(identity["observation_id"], "observation_id")
        for field in (
            "valid_at_utc", "source_committed_at_utc", "canonical_committed_at_utc"
        ):
            identity[field] = _utc(identity[field], field)
        for field in (
            "reported_temperature_c", "temperature_resolution_c", "hard_lower_bound_c"
        ):
            identity[field] = _num(_decimal(identity[field], field))
        for field in ("raw_record_sha256", "source_evidence_receipt_sha256"):
            identity[field] = _sha(identity[field], field)
        fact = {**identity, "content_sha256": content_sha256(identity)}
        facts.append(CanonicalSurfaceObservationFact.from_mapping(fact).raw)
    facts.sort(key=lambda item: (
        item["station_id"], item["event_date"], item["source_lane"],
        item["valid_at_utc"], item["report_type"], item["raw_record_sha256"],
    ))
    identity = {
        "schema": OBSERVATION_CONTRACT_SCHEMA,
        "as_of_utc": _utc(as_of_utc, "builder_as_of_utc"),
        "builder_id": "canonical.surface_observation.v1",
        "observation_facts": facts,
    }
    packet = {**identity, "content_sha256": content_sha256(identity)}
    CanonicalSurfaceObservationContract.from_mapping(packet)
    return packet


def _load_observation(
    conn: sqlite3.Connection, content_sha: str
) -> CanonicalSurfaceObservationFact:
    row = conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact WHERE fact_type=? AND content_sha256=?",
        (OBSERVATION_FACT_TYPE, _sha(content_sha, "winner_parent_observation")),
    ).fetchall()
    if len(row) != 1:
        raise ContractError("surface_observation_winner_parent_not_unique")
    return CanonicalSurfaceObservationFact.from_mapping(json.loads(str(row[0][0])))


def _source_observations(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    source_lane: str,
    source_receipt_sha256: str,
) -> list[CanonicalSurfaceObservationFact]:
    result = []
    for (raw_json,) in conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact WHERE fact_type=?",
        (OBSERVATION_FACT_TYPE,),
    ).fetchall():
        fact = CanonicalSurfaceObservationFact.from_mapping(json.loads(str(raw_json)))
        if (
            fact.raw["station_id"] == station_id
            and fact.raw["event_date"] == event_date
            and fact.raw["settlement_timezone"] == settlement_timezone
            and fact.raw["source_lane"] == source_lane
            and fact.raw["source_evidence_receipt_sha256"] == source_receipt_sha256
        ):
            result.append(fact)
    result.sort(key=lambda item: (
        item.raw["valid_at_utc"], item.raw["report_type"], item.content_sha256
    ))
    return result


def build_surface_observation_winner_contract(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    candidate_terminals: Sequence[Mapping[str, Any]],
    as_of_utc: str,
) -> dict[str, Any]:
    terminals = [_terminal(item) for item in candidate_terminals]
    terminals.sort(key=lambda item: (-item["source_priority"], item["source_lane"]))
    eligible = [item for item in terminals if item["eligibility_state"] == "ELIGIBLE"]
    if not eligible:
        raise ContractError("surface_observation_winner_no_eligible_source")
    selected = max(eligible, key=lambda item: item["source_priority"])
    expected_scope = (
        _text(station_id, "winner_station_id").upper(),
        _date(event_date, "winner_event_date"),
        _text(settlement_timezone, "winner_settlement_timezone"),
        selected["source_lane"],
    )
    observations = _source_observations(
        conn,
        station_id=expected_scope[0],
        event_date=expected_scope[1],
        settlement_timezone=expected_scope[2],
        source_lane=expected_scope[3],
        source_receipt_sha256=selected["source_evidence_receipt_sha256"],
    )
    if (
        len(observations) != selected["observation_count"]
        or content_sha256([item.content_sha256 for item in observations])
        != selected["observation_set_sha256"]
    ):
        raise ContractError("surface_observation_winner_source_observation_set_mismatch")
    maximum = sorted(
        observations,
        key=lambda item: (
            -Decimal(item.raw["reported_temperature_c"]),
            item.raw["valid_at_utc"],
            item.content_sha256,
        ),
    )[0]
    observation = _load_observation(conn, str(selected["max_observation_content_sha256"]))
    if observation.content_sha256 != maximum.content_sha256:
        raise ContractError("surface_observation_winner_source_maximum_mismatch")
    actual_scope = tuple(observation.raw[key] for key in (
        "station_id", "event_date", "settlement_timezone", "source_lane"
    ))
    if actual_scope != expected_scope:
        raise ContractError("surface_observation_winner_parent_scope_mismatch")
    if (
        observation.raw["reported_temperature_c"] != selected["observed_max_c"]
        or observation.raw["valid_at_utc"] != selected["max_valid_at_utc"]
    ):
        raise ContractError("surface_observation_winner_parent_value_mismatch")
    as_of = _utc(as_of_utc, "winner_builder_as_of")
    identity = {
        "station_id": expected_scope[0],
        "event_date": expected_scope[1],
        "settlement_timezone": expected_scope[2],
        "winner_as_of_utc": as_of,
        "priority_contract_version": PRIORITY_CONTRACT_VERSION,
        "candidate_terminals": terminals,
        "candidate_terminal_set_sha256": content_sha256(terminals),
        "selected_source_lane": selected["source_lane"],
        "selected_source_class": selected["source_class"],
        "selected_source_priority": selected["source_priority"],
        "selected_observation_content_sha256": observation.content_sha256,
        "selected_report_type": observation.raw["report_type"],
        "selected_valid_at_utc": observation.raw["valid_at_utc"],
        "selected_committed_at_utc": observation.raw["canonical_committed_at_utc"],
        "reported_high_c": observation.raw["reported_temperature_c"],
        "hard_lower_bound_c": observation.raw["hard_lower_bound_c"],
        "temperature_resolution_c": observation.raw["temperature_resolution_c"],
        "hard_lower_bound_method": observation.raw["hard_lower_bound_method"],
        "selection_reason": f"{selected['source_lane']}_HIGHEST_ELIGIBLE_PRIORITY",
        "authority_scope": AUTHORITY_SCOPE,
        "state": STATE,
    }
    fact = {**identity, "content_sha256": content_sha256(identity)}
    packet_identity = {
        "schema": WINNER_CONTRACT_SCHEMA,
        "as_of_utc": as_of,
        "builder_id": "canonical.surface_observation_winner.v1",
        "winner_facts": [fact],
    }
    packet = {**packet_identity, "content_sha256": content_sha256(packet_identity)}
    CanonicalSurfaceObservationWinnerContract.from_mapping(packet)
    return packet


def seal_candidate_terminal(raw: Mapping[str, Any]) -> dict[str, Any]:
    identity = {k: v for k, v in raw.items() if k != "terminal_content_sha256"}
    identity["source_lane"] = _text(identity["source_lane"], "terminal_source_lane").upper()
    identity["source_class"] = _text(identity["source_class"], "terminal_source_class")
    identity["source_priority"] = int(identity["source_priority"])
    identity["terminal_reason"] = _text(identity["terminal_reason"], "terminal_reason")
    identity["observation_count"] = int(identity["observation_count"])
    for field in ("source_evidence_receipt_sha256", "observation_set_sha256"):
        identity[field] = _sha(identity[field], field)
    if identity["observed_max_c"] is not None:
        identity["observed_max_c"] = _num(
            _decimal(identity["observed_max_c"], "terminal_max_c")
        )
    if identity["max_valid_at_utc"] is not None:
        identity["max_valid_at_utc"] = _utc(
            identity["max_valid_at_utc"], "terminal_max_valid_at"
        )
    if identity["max_observation_content_sha256"] is not None:
        identity["max_observation_content_sha256"] = _sha(
            identity["max_observation_content_sha256"], "terminal_max_observation"
        )
    return _terminal({**identity, "terminal_content_sha256": content_sha256(identity)})


def assert_surface_observation_external_evidence(
    evidence_root: Path | str,
    contract: CanonicalSurfaceObservationContract,
) -> None:
    """Re-open the retained AWC receipt and every content-addressed raw row."""

    root = Path(evidence_root).expanduser().resolve(strict=True)
    receipt_path = root / "fetch_receipt.json"
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise ContractError("surface_observation_external_receipt_missing")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError("surface_observation_external_receipt_invalid") from exc
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("content_sha256")
        != content_sha256({k: v for k, v in receipt.items() if k != "content_sha256"})
    ):
        raise ContractError("surface_observation_external_receipt_hash_mismatch")
    for fact in contract.observation_facts:
        if fact.raw["source_lane"] != "AWC":
            raise ContractError("surface_observation_external_lane_not_implemented")
        if fact.raw["source_evidence_receipt_sha256"] != receipt["content_sha256"]:
            raise ContractError("surface_observation_external_receipt_binding_mismatch")
        raw_sha = fact.raw["raw_record_sha256"]
        raw_path = root / "raw" / "objects" / raw_sha[:2] / f"{raw_sha}.json.gz"
        if raw_path.is_symlink() or not raw_path.is_file():
            raise ContractError("surface_observation_external_raw_missing")
        try:
            with gzip.open(raw_path, "rt", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ContractError("surface_observation_external_raw_invalid") from exc
        raw_text = str(raw.get("raw_text") or "")
        tenths = re.search(r"(?:^|\s)T([01])(\d{3})[01]\d{3}(?:\s|$)", raw_text)
        if tenths is None:
            raise ContractError("surface_observation_external_awc_tenths_missing")
        reported_tenths = Decimal(tenths.group(2)) / Decimal("10")
        if tenths.group(1) == "1":
            reported_tenths = -reported_tenths
        if (
            hashlib.sha256(raw_text.encode("utf-8")).hexdigest() != raw_sha
            or raw.get("station_id") != fact.raw["station_id"]
            or _utc(raw.get("valid_at_utc"), "external_valid_at_utc")
            != fact.raw["valid_at_utc"]
            or raw.get("report_type") != fact.raw["report_type"]
            or _num(_decimal(raw.get("reported_temperature_c"), "external_temperature"))
            != fact.raw["reported_temperature_c"]
            or raw.get("source_url") != fact.raw["origin_url"]
            or fact.raw["provider_id"] != SOURCE_POLICY["AWC"]["provider_id"]
            or fact.raw["measurement_semantics"]
            != SOURCE_POLICY["AWC"]["measurement_semantics"]
            or fact.raw["observation_id"] != f"AWC:{raw_sha}"
            or fact.raw["temperature_resolution_c"] != "0.100000000000"
            or _num(reported_tenths) != fact.raw["reported_temperature_c"]
        ):
            raise ContractError("surface_observation_external_raw_binding_mismatch")


def assert_surface_observation_winner_external_evidence(
    evidence_root: Path | str,
    contract: CanonicalSurfaceObservationWinnerContract,
) -> None:
    """Re-open the complete three-lane terminal evidence for each winner."""

    root = Path(evidence_root).expanduser().resolve(strict=True)
    workspace = Path(__file__).resolve().parents[1]
    for winner in contract.winner_facts:
        terminal_by_lane = {
            str(item["source_lane"]): item
            for item in winner.raw["candidate_terminals"]
        }

        awc = terminal_by_lane["AWC"]
        awc_receipt_path = root / "awc" / "fetch_receipt.json"
        if awc_receipt_path.is_symlink() or not awc_receipt_path.is_file():
            raise ContractError("surface_observation_winner_awc_receipt_missing")
        try:
            awc_receipt = json.loads(awc_receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ContractError("surface_observation_winner_awc_receipt_invalid") from exc
        awc_sha = content_sha256({
            key: value for key, value in awc_receipt.items()
            if key != "content_sha256"
        })
        terminal_states = awc_receipt.get("terminal_state_counts") or {}
        if (
            awc_receipt.get("content_sha256") != awc_sha
            or awc["source_evidence_receipt_sha256"] != awc_sha
            or awc["observation_count"] != awc_receipt.get("observation_count")
            or awc["eligibility_state"] != "ELIGIBLE"
            or awc["coverage_complete"] is not True
            or terminal_states.get("FETCHED_OFFICIAL_RAW")
            != awc_receipt.get("terminal_count")
        ):
            raise ContractError("surface_observation_winner_awc_receipt_binding_mismatch")

        iem = terminal_by_lane["IEM"]
        manifest_path = root / "asos" / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ContractError("surface_observation_winner_iem_manifest_missing")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ContractError("surface_observation_winner_iem_manifest_invalid") from exc
        downloads = [
            row for row in manifest.get("downloads") or []
            if winner.raw["station_id"] in (row.get("station_ids") or [])
            and row.get("event_date") == winner.raw["event_date"]
        ]
        if len(downloads) != 1:
            raise ContractError("surface_observation_winner_iem_download_not_unique")
        download = downloads[0]
        raw_path = (workspace / str(download.get("raw_path") or "")).resolve(strict=True)
        if raw_path.is_symlink() or not raw_path.is_file() or workspace not in raw_path.parents:
            raise ContractError("surface_observation_winner_iem_raw_invalid")
        payload = raw_path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != download.get("raw_sha256"):
            raise ContractError("surface_observation_winner_iem_raw_hash_mismatch")
        try:
            rows = list(csv.DictReader(payload.decode("utf-8", "strict").splitlines()))
        except (UnicodeError, csv.Error) as exc:
            raise ContractError("surface_observation_winner_iem_raw_decode_invalid") from exc
        valid_rows: list[tuple[str, Decimal]] = []
        for row in rows:
            try:
                stamp = datetime.strptime(
                    str(row.get("valid(UTC)") or ""), "%Y-%m-%d %H:%M"
                ).replace(tzinfo=__import__("datetime").timezone.utc)
                temp_f = Decimal(str(row.get("tmpf") or ""))
            except (ValueError, InvalidOperation):
                continue
            valid_rows.append(
                (
                    stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    (temp_f - Decimal("32")) * Decimal("5") / Decimal("9"),
                )
            )
        iem_set_sha = content_sha256({
            "raw_sha256": download["raw_sha256"],
            "report_count": len(valid_rows),
            "valid_at_utc": [item[0] for item in valid_rows],
        })
        if (
            not valid_rows
            or iem["source_evidence_receipt_sha256"]
            != download.get("source_receipt_sha256")
            or iem["observation_count"] != len(valid_rows)
            or iem["observation_set_sha256"] != iem_set_sha
            or iem["eligibility_state"] != "INELIGIBLE"
            or iem["coverage_complete"] is not False
        ):
            raise ContractError("surface_observation_winner_iem_terminal_binding_mismatch")

        madis = terminal_by_lane["MADIS"]
        absence_path = root / "madis_absence_receipt.json"
        if absence_path.is_symlink() or not absence_path.is_file():
            raise ContractError("surface_observation_winner_madis_receipt_missing")
        try:
            absence = json.loads(absence_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ContractError("surface_observation_winner_madis_receipt_invalid") from exc
        absence_sha = content_sha256({
            key: value for key, value in absence.items()
            if key != "content_sha256"
        })
        if (
            absence.get("content_sha256") != absence_sha
            or absence.get("schema")
            != "canonical_surface_madis_absence_receipt_v1"
            or absence.get("station_id") != winner.raw["station_id"]
            or absence.get("event_date") != winner.raw["event_date"]
            or absence.get("settlement_timezone")
            != winner.raw["settlement_timezone"]
            or absence.get("retained_object_count") != 0
            or absence.get("retained_observation_count") != 0
            or madis["source_evidence_receipt_sha256"] != absence_sha
            or madis["eligibility_state"] != "DATA_MISSING"
            or madis["observation_count"] != 0
            or madis["coverage_complete"] is not False
            or madis["observation_set_sha256"] != content_sha256([])
        ):
            raise ContractError("surface_observation_winner_madis_receipt_binding_mismatch")


__all__ = [
    "AUTHORITY_SCOPE", "OBSERVATION_CONTRACT_SCHEMA", "OBSERVATION_FACT_TYPE",
    "PRIORITY_CONTRACT_VERSION", "SOURCE_POLICY", "STATE",
    "WINNER_CONTRACT_SCHEMA", "WINNER_FACT_TYPE",
    "CanonicalSurfaceObservationContract", "CanonicalSurfaceObservationFact",
    "CanonicalSurfaceObservationWinnerContract",
    "CanonicalSurfaceObservationWinnerFact", "build_surface_observation_contract",
    "build_surface_observation_winner_contract", "seal_candidate_terminal",
    "assert_surface_observation_external_evidence",
    "assert_surface_observation_winner_external_evidence",
]
