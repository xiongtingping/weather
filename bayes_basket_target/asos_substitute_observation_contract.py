"""Canonical identity facts for scheduled ASOS substitute observations.

A substitute observation fills a frozen METAR slot the AWC relay missed
(``asos_p1_slot_materializer``).  It is formal P1 evidence, but the PR12
truth lane cannot identify it: ``truth_observation_revision`` facts are the
AWC lane's own immutable identities, bound to AWC origins, AWC receipts and
the AWC correction machinery, and the truth ledger they key stays AWC-pure
by charter.  Without an identity fact of its own, a substitute-closed P1 can
never root an atomic plan -- the chain fails closed on
``target_atomic_revision_fact_not_staged`` -- so the stage that reacted to
the evidence cannot act on it.

This module is the substitute lanes' parallel identity contract: the same
role ``truth_observation_revision`` plays for AWC reports, under its own
fact type (``substitute_observation_revision``), its own origin vocabulary
(MADIS / IEM), and the two ex-ante gates every substitute already carries --
the station's OOS_READY exact-match adapter artifact and the frozen
pre-observation schedule slot.  It never touches the truth lane: substitute
facts produce no day bounds, no truth vectors, and can never settle a day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import sqlite3
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .contracts import ContractError, content_sha256


CONTRACT_SCHEMA = (
    "bayes_basket_target_canonical_substitute_observation_contract_v1"
)
FACT_TYPE = "substitute_observation_revision"
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
OBSERVATION_ID_PREFIX = "asos_p1_"
SUBSTITUTE_TRANSPORT = "asos_p1_scheduled_slot_substitute"
# Lane spellings follow the probability-envelope lineage (upper case), which
# is what the atomic plan carries and the ledger compares against.
SUBSTITUTE_LANE_REPORT_TYPES = {
    "MADIS_HFMETAR": "ASOS_HF",
    "ASOS_1MIN": "ASOS_1MIN",
}
SUBSTITUTE_RAW_OBJECT_KINDS = {
    "MADIS_HFMETAR": "MADIS_HFMETAR_DECODED_ROW_V1",
    "ASOS_1MIN": "IEM_ASOS1MIN_DECODED_ROW_V1",
}
# Single source of the substitute origin vocabulary: the slot materializer
# writes these into ``reported_trajectory_observation_origins`` and this
# contract refuses anything else, so the two ends cannot drift apart.
SUBSTITUTE_ORIGINS = {
    "MADIS_HFMETAR": {
        "origin_source_id": "noaa_madis_hfmetar",
        "origin_authority": "NOAA MADIS / ASOS HF-METAR",
        "origin_url": (
            "https://madis-data.ncep.noaa.gov/madisPublic1/data/LDAD/hfmetar/netCDF"
        ),
    },
    "ASOS_1MIN": {
        "origin_source_id": "iem_asos1min",
        "origin_authority": "NOAA/NCEI ASOS via IEM republisher",
        "origin_url": (
            "https://mesonet.agron.iastate.edu/cgi-bin/request/asos1min.py"
        ),
    },
}
MAX_FACTS_PER_CONTRACT = 2000


def _text(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"substitute_observation_{label}_missing")
    return result


def _sha(value: Any, label: str) -> str:
    result = _text(value, label).lower()
    if len(result) != 64 or any(c not in "0123456789abcdef" for c in result):
        raise ContractError(f"substitute_observation_{label}_invalid")
    return result


def _utc(value: Any, label: str) -> str:
    text = _text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"substitute_observation_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(
            f"substitute_observation_{label}_timezone_required"
        )
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parsed(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def is_substitute_revision_mapping(raw: Mapping[str, Any]) -> bool:
    """True when a revision mapping belongs to this contract, not PR12."""

    return (
        str(raw.get("source_lane") or "").strip().upper()
        in SUBSTITUTE_LANE_REPORT_TYPES
    )


@dataclass(frozen=True)
class CanonicalSubstituteObservationRevisionFact:
    raw: dict[str, Any]

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "CanonicalSubstituteObservationRevisionFact":
        fields = {
            "station_id", "observation_id", "revision_no", "event_date",
            "settlement_timezone", "source_lane", "report_type",
            "authority_scope", "observed_at_utc",
            "source_observation_committed_at_utc",
            "canonical_committed_at_utc", "reported_temperature_c",
            "raw_object_kind", "raw_object_sha256", "source_receipt_sha256",
            "asos_p1_oos_artifact_content_sha256", "schedule_content_sha256",
            "planned_metar_observation_at_utc", "origin_source_id",
            "origin_authority", "origin_url", "origin_transport",
            "qc_state", "content_sha256",
        }
        if set(raw) != fields:
            raise ContractError(
                "substitute_observation_revision_fields_mismatch"
            )
        value = dict(raw)
        lane = _text(value["source_lane"], "source_lane").upper()
        if lane not in SUBSTITUTE_LANE_REPORT_TYPES:
            raise ContractError(
                "substitute_observation_revision_source_lane_invalid"
            )
        value["source_lane"] = lane
        report_type = _text(value["report_type"], "report_type").upper()
        if report_type != SUBSTITUTE_LANE_REPORT_TYPES[lane]:
            raise ContractError(
                "substitute_observation_revision_report_type_invalid"
            )
        value["report_type"] = report_type
        if value["authority_scope"] != AUTHORITY_SCOPE:
            raise ContractError(
                "substitute_observation_revision_authority_invalid"
            )
        observation_id = _text(value["observation_id"], "observation_id")
        if not observation_id.startswith(OBSERVATION_ID_PREFIX):
            raise ContractError(
                "substitute_observation_revision_identity_invalid"
            )
        value["station_id"] = _text(value["station_id"], "station_id").upper()
        if int(value["revision_no"]) != 1:
            # A substitute is immutable by construction: it has no COR/AMD
            # channel and its content address is its identity.  A second
            # revision could only be fabrication.
            raise ContractError(
                "substitute_observation_revision_no_invalid"
            )
        value["revision_no"] = 1
        if isinstance(value["reported_temperature_c"], bool) or not isinstance(
            value["reported_temperature_c"], (int, float)
        ):
            raise ContractError(
                "substitute_observation_revision_temperature_invalid"
            )
        value["reported_temperature_c"] = float(
            value["reported_temperature_c"]
        )
        if not -100.0 <= value["reported_temperature_c"] <= 70.0:
            raise ContractError(
                "substitute_observation_revision_temperature_invalid"
            )
        if value["raw_object_kind"] != SUBSTITUTE_RAW_OBJECT_KINDS[lane]:
            raise ContractError(
                "substitute_observation_revision_raw_object_kind_invalid"
            )
        for field in (
            "raw_object_sha256",
            "source_receipt_sha256",
            "asos_p1_oos_artifact_content_sha256",
            "schedule_content_sha256",
        ):
            value[field] = _sha(value[field], field)
        for field in (
            "observed_at_utc",
            "source_observation_committed_at_utc",
            "canonical_committed_at_utc",
            "planned_metar_observation_at_utc",
        ):
            value[field] = _utc(value[field], field)
        if not (
            value["observed_at_utc"]
            <= value["source_observation_committed_at_utc"]
            <= value["canonical_committed_at_utc"]
        ):
            raise ContractError(
                "substitute_observation_revision_timestamp_order_invalid"
            )
        observed = _parsed(value["observed_at_utc"])
        planned = _parsed(value["planned_metar_observation_at_utc"])
        expected_minute = (
            planned.minute
            if lane == "ASOS_1MIN"
            else (planned.minute // 5) * 5
        )
        if observed.replace(second=0, microsecond=0) != planned.replace(
            minute=expected_minute, second=0, microsecond=0
        ):
            raise ContractError(
                "substitute_observation_revision_slot_binding_invalid"
            )
        try:
            zone = ZoneInfo(str(value["settlement_timezone"]))
        except Exception as exc:
            raise ContractError(
                "substitute_observation_revision_timezone_invalid"
            ) from exc
        if observed.astimezone(zone).date().isoformat() != str(
            value["event_date"]
        ):
            raise ContractError(
                "substitute_observation_revision_event_date_mismatch"
            )
        origin = SUBSTITUTE_ORIGINS[lane]
        if (
            value["origin_source_id"] != origin["origin_source_id"]
            or value["origin_authority"] != origin["origin_authority"]
            or value["origin_url"] != origin["origin_url"]
            or value["origin_transport"] != SUBSTITUTE_TRANSPORT
        ):
            raise ContractError(
                "substitute_observation_revision_origin_lineage_invalid"
            )
        if value["qc_state"] != "ACCEPTED":
            raise ContractError(
                "substitute_observation_revision_qc_state_invalid"
            )
        supplied = _sha(value["content_sha256"], "content_sha256")
        identity = {k: v for k, v in value.items() if k != "content_sha256"}
        if supplied != content_sha256(identity):
            raise ContractError(
                "substitute_observation_revision_content_sha256_mismatch"
            )
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
        ))


@dataclass(frozen=True)
class CanonicalSubstituteObservationContract:
    as_of_utc: str
    builder_id: str
    revision_facts: tuple[CanonicalSubstituteObservationRevisionFact, ...]
    content_sha256: str

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "CanonicalSubstituteObservationContract":
        if set(raw) != {
            "schema", "as_of_utc", "builder_id", "revision_facts",
            "content_sha256",
        } or raw.get("schema") != CONTRACT_SCHEMA:
            raise ContractError("substitute_observation_contract_invalid")
        facts_raw = raw["revision_facts"]
        if not isinstance(facts_raw, list) or not facts_raw:
            raise ContractError(
                "substitute_observation_contract_fact_required"
            )
        if len(facts_raw) > MAX_FACTS_PER_CONTRACT:
            raise ContractError(
                "substitute_observation_contract_resource_limit_exceeded"
            )
        facts = tuple(
            CanonicalSubstituteObservationRevisionFact.from_mapping(item)
            for item in facts_raw
        )
        if len({fact.scope_key for fact in facts}) != len(facts):
            raise ContractError(
                "substitute_observation_contract_scope_duplicate"
            )
        as_of = _utc(raw["as_of_utc"], "contract_as_of_utc")
        if any(
            fact.raw["canonical_committed_at_utc"] > as_of for fact in facts
        ):
            raise ContractError(
                "substitute_observation_contract_fact_after_asof"
            )
        supplied = _sha(raw["content_sha256"], "contract_content_sha256")
        if supplied != content_sha256({
            key: item for key, item in raw.items() if key != "content_sha256"
        }):
            raise ContractError(
                "substitute_observation_contract_hash_mismatch"
            )
        return cls(
            as_of, _text(raw["builder_id"], "builder_id"), facts, supplied
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CONTRACT_SCHEMA,
            "as_of_utc": self.as_of_utc,
            "builder_id": self.builder_id,
            "revision_facts": [dict(fact.raw) for fact in self.revision_facts],
            "content_sha256": self.content_sha256,
        }


def build_substitute_observation_contract(
    archive_conn: sqlite3.Connection,
    *,
    as_of_utc: str,
    window_days: int = 3,
    builder_id: str = "asos_substitute_observation_builder_v1",
) -> dict[str, Any] | None:
    """Build the contract for every substitute row in the recent window.

    Returns None when the window holds no substitute rows -- the common case
    on a healthy AWC day, and staging an empty contract would be noise.  The
    window is bounded like PR12's for the same reason: an unbounded read
    anchored at the oldest row drags weeks of history through every sweep.
    """

    as_of = _utc(as_of_utc, "builder_as_of_utc")
    if not 1 <= int(window_days) <= 14:
        raise ContractError("substitute_observation_builder_window_invalid")
    window_start = (
        _parsed(as_of) - timedelta(days=int(window_days))
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    rows = archive_conn.execute(
        """SELECT o.observation_id, o.source_lane, o.station_id,
                  o.report_type, o.valid_at, o.reported_temperature_c,
                  o.raw_sha256, o.canonical_committed_at, o.source_poll_id,
                  g.origin_json
             FROM reported_trajectory_observations o
             JOIN reported_trajectory_observation_origins g
               ON g.observation_id = o.observation_id
            WHERE o.source_lane IN ('asos_1min','madis_hfmetar')
              AND o.observation_id LIKE 'asos_p1_%'
              AND o.canonical_committed_at >= ?
              AND o.canonical_committed_at <= ?
            ORDER BY o.canonical_committed_at, o.observation_id
            LIMIT ?""",
        (window_start, as_of, MAX_FACTS_PER_CONTRACT + 1),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > MAX_FACTS_PER_CONTRACT:
        raise ContractError(
            "substitute_observation_builder_resource_limit_exceeded"
        )
    facts: list[dict[str, Any]] = []
    for (
        observation_id, lane, station_id, report_type, valid_at,
        temperature_c, raw_sha, committed_at, source_receipt, origin_json,
    ) in rows:
        adapter = json.loads(str(origin_json))
        lane_upper = str(lane).upper()
        identity = {
            "station_id": str(station_id).upper(),
            "observation_id": str(observation_id),
            "revision_no": 1,
            "event_date": str(adapter["event_date_local"]),
            "settlement_timezone": str(adapter["settlement_timezone"]),
            "source_lane": lane_upper,
            "report_type": str(report_type).upper(),
            "authority_scope": AUTHORITY_SCOPE,
            "observed_at_utc": _utc(valid_at, "row_observed_at"),
            "source_observation_committed_at_utc": _utc(
                committed_at, "row_committed_at"
            ),
            "canonical_committed_at_utc": _utc(
                committed_at, "row_committed_at"
            ),
            "reported_temperature_c": float(temperature_c),
            "raw_object_kind": SUBSTITUTE_RAW_OBJECT_KINDS[lane_upper],
            "raw_object_sha256": _sha(raw_sha, "row_raw_sha256"),
            "source_receipt_sha256": _sha(
                source_receipt, "row_source_receipt_sha256"
            ),
            "asos_p1_oos_artifact_content_sha256": _sha(
                adapter["asos_p1_oos_artifact_content_sha256"],
                "row_oos_artifact_sha256",
            ),
            "schedule_content_sha256": _sha(
                adapter["schedule_content_sha256"], "row_schedule_sha256"
            ),
            "planned_metar_observation_at_utc": _utc(
                adapter["planned_metar_observation_at_utc"],
                "row_planned_minute",
            ),
            **SUBSTITUTE_ORIGINS[lane_upper],
            "origin_transport": SUBSTITUTE_TRANSPORT,
            "qc_state": "ACCEPTED",
        }
        facts.append({**identity, "content_sha256": content_sha256(identity)})
    contract = {
        "schema": CONTRACT_SCHEMA,
        "as_of_utc": as_of,
        "builder_id": builder_id,
        "revision_facts": facts,
    }
    contract["content_sha256"] = content_sha256(contract)
    # Round-trip through the validator so the builder can never emit a
    # contract the ingress would refuse.
    CanonicalSubstituteObservationContract.from_mapping(contract)
    return contract


__all__ = [
    "AUTHORITY_SCOPE",
    "CONTRACT_SCHEMA",
    "FACT_TYPE",
    "MAX_FACTS_PER_CONTRACT",
    "OBSERVATION_ID_PREFIX",
    "SUBSTITUTE_LANE_REPORT_TYPES",
    "SUBSTITUTE_ORIGINS",
    "SUBSTITUTE_RAW_OBJECT_KINDS",
    "SUBSTITUTE_TRANSPORT",
    "CanonicalSubstituteObservationContract",
    "CanonicalSubstituteObservationRevisionFact",
    "build_substitute_observation_contract",
    "is_substitute_revision_mapping",
]
