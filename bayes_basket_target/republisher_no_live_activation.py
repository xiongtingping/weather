"""Build static no-live activation heads from persisted republisher selections.

The activation proves only that a closed-day historical strict-OOS selection
is eligible for later no-live consideration.  It contains no forward packet,
freshness claim, native-arrival claim, P0, P1, or formal/live authority.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from .canonical_contract import (
    CanonicalRepublisherCrossSelectionFact,
    CanonicalRepublisherNoLiveActivationContract,
    CanonicalRepublisherNoLiveActivationFact,
    REPUBLISHER_NO_LIVE_ACTIVATION_CONTRACT_SCHEMA,
)
from .contracts import ContractError, content_sha256
from .republisher_cross_selection import (
    assert_republisher_cross_selection_fact_references,
    load_persisted_republisher_cross_selection,
)


BUILDER_ID = "republisher_no_live_activation_builder_v1"


def _instant(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"republisher_no_live_activation_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"republisher_no_live_activation_{label}_invalid")
    return parsed


def _selection_root(selection: CanonicalRepublisherCrossSelectionFact) -> str:
    return "|".join(
        (
            selection.station_id,
            selection.settlement_timezone,
            selection.settlement_definition,
            selection.season_bin,
            selection.lead_bucket,
            "null",
            selection.run_cycle_scope,
        )
    )


def _load_station_selection(
    conn: sqlite3.Connection, station_id: str
) -> tuple[CanonicalRepublisherCrossSelectionFact, str]:
    candidates: list[tuple[CanonicalRepublisherCrossSelectionFact, str]] = []
    for content_sha256_value, fact_json, ingested_at_utc in conn.execute(
        "SELECT content_sha256,fact_json,ingested_at_utc FROM canonical_ssot_fact "
        "WHERE fact_type='republisher_cross_selection' ORDER BY fact_id"
    ):
        try:
            raw = json.loads(str(fact_json))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError(
                "republisher_no_live_activation_selection_json_invalid"
            ) from exc
        if not isinstance(raw, Mapping):
            raise ContractError(
                "republisher_no_live_activation_selection_mapping_required"
            )
        fact = CanonicalRepublisherCrossSelectionFact.from_mapping(raw)
        if str(content_sha256_value) != fact.content_sha256:
            raise ContractError(
                "republisher_no_live_activation_selection_hash_mismatch"
            )
        if fact.station_id == station_id:
            candidates.append((fact, str(ingested_at_utc)))
    if len(candidates) != 1:
        raise ContractError(
            "republisher_no_live_activation_exact_selection_required"
        )
    return candidates[0]


def _load_activation_chain(
    conn: sqlite3.Connection, root: str
) -> tuple[CanonicalRepublisherNoLiveActivationFact, ...]:
    facts: list[CanonicalRepublisherNoLiveActivationFact] = []
    for content_sha256_value, fact_json in conn.execute(
        "SELECT content_sha256,fact_json FROM canonical_ssot_fact "
        "WHERE fact_type='republisher_no_live_activation' "
        "AND scope_key LIKE ? ORDER BY scope_key",
        (root + "|%",),
    ):
        try:
            raw = json.loads(str(fact_json))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError(
                "republisher_no_live_activation_persisted_json_invalid"
            ) from exc
        if not isinstance(raw, Mapping):
            raise ContractError(
                "republisher_no_live_activation_persisted_mapping_required"
            )
        fact = CanonicalRepublisherNoLiveActivationFact.from_mapping(raw)
        if fact.activation_root != root or str(content_sha256_value) != fact.content_sha256:
            raise ContractError(
                "republisher_no_live_activation_persisted_lineage_invalid"
            )
        facts.append(fact)
    facts.sort(key=lambda item: item.activation_generation)
    for index, fact in enumerate(facts, start=1):
        if fact.activation_generation != index:
            raise ContractError("republisher_no_live_activation_chain_gap")
        expected = None if index == 1 else facts[index - 2].content_sha256
        if fact.predecessor_activation_content_sha256 != expected:
            raise ContractError("republisher_no_live_activation_chain_fork")
    return tuple(facts)


def _activation_mapping(
    selection: CanonicalRepublisherCrossSelectionFact,
    *,
    generation: int,
    predecessor: str | None,
    activated_at_utc: str,
) -> dict[str, Any]:
    root_identity = {
        "station_id": selection.station_id,
        "settlement_timezone": selection.settlement_timezone,
        "settlement_definition": selection.settlement_definition,
        "season_bin": selection.season_bin,
        "lead_bucket": selection.lead_bucket,
        "run_cycle": None,
        "run_cycle_scope": selection.run_cycle_scope,
    }
    activation_id = "republisher-no-live-activation-" + content_sha256(
        {
            "root": root_identity,
            "activation_generation": generation,
            "selection_content_sha256": selection.content_sha256,
            "predecessor_activation_content_sha256": predecessor,
        }
    )[:40]
    identity = {
        "activation_id": activation_id,
        **root_identity,
        "source_class": "aggregator_republisher",
        "transport_provider_id": "OPEN_METEO",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "model_family": "cross_global_deterministic",
        "selection_id": selection.selection_id,
        "selection_content_sha256": selection.content_sha256,
        "activation_generation": generation,
        "predecessor_activation_content_sha256": predecessor,
        "activated_at_utc": activated_at_utc,
        "state": "PRODUCTION_NO_LIVE_REPUBLISHER_ACTIVATION_READY",
        "future_event_count": 0,
        "native_arrival_indexed": False,
        "p1_eligible": False,
        "formal_live_authority": False,
        "production_no_live_only": True,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def build_republisher_no_live_activation_contract(
    conn: sqlite3.Connection,
    *,
    station_ids: Iterable[str],
    as_of_utc: str,
    builder_id: str = BUILDER_ID,
) -> dict[str, Any]:
    stations = sorted({str(value).strip().upper() for value in station_ids})
    if not stations or any(not value for value in stations):
        raise ContractError("republisher_no_live_activation_station_scope_empty")
    as_of = str(as_of_utc)
    as_of_instant = _instant(as_of, "as_of_utc")
    facts: list[dict[str, Any]] = []
    for station_id in stations:
        selection, selection_ingested_at = _load_station_selection(conn, station_id)
        if _instant(selection_ingested_at, "selection_ingested_at_utc") > as_of_instant:
            raise ContractError(
                "republisher_no_live_activation_selection_after_contract_asof"
            )
        assert_republisher_cross_selection_fact_references(
            conn, selection, as_of_utc=as_of
        )
        chain = _load_activation_chain(conn, _selection_root(selection))
        matches = [
            item for item in chain
            if item.selection_content_sha256 == selection.content_sha256
        ]
        if len(matches) > 1:
            raise ContractError(
                "republisher_no_live_activation_selection_reactivated"
            )
        if matches:
            facts.append(matches[0].to_mapping())
            continue
        generation = len(chain) + 1
        predecessor = chain[-1].content_sha256 if chain else None
        facts.append(
            _activation_mapping(
                selection,
                generation=generation,
                predecessor=predecessor,
                activated_at_utc=as_of,
            )
        )
    identity = {
        "schema": REPUBLISHER_NO_LIVE_ACTIVATION_CONTRACT_SCHEMA,
        "as_of_utc": as_of,
        "builder_id": str(builder_id).strip(),
        "activation_facts": facts,
    }
    packet = {**identity, "content_sha256": content_sha256(identity)}
    return CanonicalRepublisherNoLiveActivationContract.from_mapping(packet).to_mapping()


def assert_republisher_no_live_activation_references(
    conn: sqlite3.Connection,
    contract: CanonicalRepublisherNoLiveActivationContract,
) -> None:
    as_of_instant = _instant(contract.as_of_utc, "as_of_utc")
    for activation in contract.activation_facts:
        selection, selection_ingested_at = load_persisted_republisher_cross_selection(
            conn, activation.selection_content_sha256
        )
        if (
            selection.selection_id != activation.selection_id
            or _selection_root(selection) != activation.activation_root
        ):
            raise ContractError(
                "republisher_no_live_activation_selection_scope_mismatch"
            )
        if (
            _instant(selection_ingested_at, "selection_ingested_at_utc")
            > as_of_instant
            or _instant(selection_ingested_at, "selection_ingested_at_utc")
            > _instant(activation.activated_at_utc, "activated_at_utc")
        ):
            raise ContractError(
                "republisher_no_live_activation_selection_time_travel"
            )
        assert_republisher_cross_selection_fact_references(
            conn, selection, as_of_utc=contract.as_of_utc
        )
        chain = _load_activation_chain(conn, activation.activation_root)
        persisted_same = [
            item for item in chain if item.content_sha256 == activation.content_sha256
        ]
        if persisted_same:
            if len(persisted_same) != 1:
                raise ContractError("republisher_no_live_activation_replay_ambiguous")
            continue
        expected_generation = len(chain) + 1
        expected_predecessor = chain[-1].content_sha256 if chain else None
        if (
            activation.activation_generation != expected_generation
            or activation.predecessor_activation_content_sha256
            != expected_predecessor
        ):
            raise ContractError(
                "republisher_no_live_activation_generation_not_contiguous"
            )
        same_selection = any(
            item.selection_content_sha256 == activation.selection_content_sha256
            for item in chain
        )
        if activation.temperature_distribution_artifact_content_sha256 is None:
            if same_selection:
                raise ContractError(
                    "republisher_no_live_activation_selection_reactivated"
                )
            if activation.activation_generation > 1:
                raise ContractError(
                    "republisher_no_live_activation_distribution_required"
                )
        elif not same_selection:
            raise ContractError(
                "republisher_no_live_activation_distribution_selection_missing"
            )
        else:
            from .republisher_no_live_p0_contract import (
                CanonicalProbabilityDistributionArtifactFact,
            )

            rows = conn.execute(
                "SELECT content_sha256,fact_json FROM canonical_ssot_fact "
                "WHERE fact_type='probability_distribution_artifact' "
                "AND content_sha256=?",
                (activation.temperature_distribution_artifact_content_sha256,),
            ).fetchall()
            if len(rows) != 1:
                raise ContractError(
                    "republisher_no_live_activation_distribution_missing"
                )
            stored_sha, fact_json = rows[0]
            try:
                raw = json.loads(str(fact_json))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ContractError(
                    "republisher_no_live_activation_distribution_json_invalid"
                ) from exc
            if not isinstance(raw, Mapping):
                raise ContractError(
                    "republisher_no_live_activation_distribution_invalid"
                )
            distribution = CanonicalProbabilityDistributionArtifactFact.from_mapping(
                raw
            )
            if (
                str(stored_sha) != distribution.content_sha256
                or distribution.raw["selection_content_sha256"]
                != activation.selection_content_sha256
                or distribution.raw["station_id"] != activation.station_id
                or distribution.raw["settlement_timezone"]
                != activation.settlement_timezone
                or distribution.raw["settlement_definition"]
                != activation.settlement_definition
                or distribution.raw["season_bin"] != activation.season_bin
                or distribution.raw["lead_bucket"] != activation.lead_bucket
                or distribution.raw["run_cycle"] is not None
                or distribution.raw["run_cycle_scope"]
                != activation.run_cycle_scope
            ):
                raise ContractError(
                    "republisher_no_live_activation_distribution_mismatch"
                )


__all__ = [
    "BUILDER_ID",
    "assert_republisher_no_live_activation_references",
    "build_republisher_no_live_activation_contract",
]
