#!/usr/bin/env python3
"""Build one read-only, 45-station model/data/OOS gap ledger.

The ledger does not infer readiness and never changes SSOT.  It joins the
already retained audit artifacts by station, model family, run cycle, lead and
event date, so missing data, lineage, truth, revision, sample and utility
gaps remain distinguishable.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.republisher_fallback_binding import (  # noqa: E402
    validate_republisher_fallback_binding,
)
from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: E402
from bayes_basket_target.station_policy_forward_outcome_binding import (  # noqa: E402
    build_station_policy_utility_contract_pointer,
)

MAX_STATIONS = 45
MAX_INPUT_BYTES = 64 * 1024 * 1024
SCHEMA = "bayes_basket_target_oos_gap_ledger_45station_v1"
SNAPSHOT_MANIFEST_SCHEMA = "bayes_basket_target_oos_atomic_snapshot_manifest_v1"
P1_AUDIT_SCHEMA = "bayes_basket_target_p1_trajectory_45station_audit_v1"
P1_REPAIR_SCHEMA = "bayes_basket_target_p1_trajectory_repair_plan_v1"
BLOCKER_TYPES = (
    "数据缺失",
    "arrival lineage 缺失",
    "truth 缺失",
    "revision 未复核",
    "样本不足",
    "utility 合同缺失",
    "utility 证据绑定缺失",
)

# Only settled historical days can be OOS.  Future-day evidence is retained
# for arrival/interface observability and later temporal validation, but is
# explicitly *not* an OOS role, OOS sample, or OOS gate input.
EVIDENCE_ROLES = (
    "historical_strict_oos",
    "observational_candidate_not_oos",
)

_OPTIONAL_INPUT_KEYS = frozenset({
    # These append-only Open-Meteo receipts are deliberately optional so a
    # clean checkout can still build the ledger before the first historical
    # fallback receipt exists.  When present, they are consumed verbatim;
    # absence is reported as a model/data gap, never synthesized.
    "aifs_republisher",
    "icon_republisher",
    "republisher_fallback_binding",
    "p1_replay_binding",
})

# A blocker must be operational rather than merely descriptive.  These are
# evidence-collection actions only: none authorizes a fallback source,
# reconstructed arrival timestamp, synthetic OOS day, or live trade action.
BLOCKER_RESOLUTION_ACTIONS: dict[str, dict[str, Any]] = {
    "数据缺失": {
        "action": "CAPTURE_NEXT_OFFICIAL_EVIDENCE",
        "required_evidence": "new official source object/receipt not already retained; never refetch retained raw",
        "automatic_write_authorized": False,
    },
    "arrival lineage 缺失": {
        "action": "VERIFY_NATIVE_PUBLICATION_AND_ARRIVAL",
        "required_evidence": "provider-native publication/arrival receipt with content hash",
        "automatic_write_authorized": False,
    },
    "truth 缺失": {
        "action": "WAIT_FOR_CLOSED_AWC_TRUTH",
        "required_evidence": "accepted closed AWC station-day truth with current revision lineage",
        "automatic_write_authorized": False,
    },
    "revision 未复核": {
        "action": "QUARANTINE_STATION_DAY_AND_MANUAL_REVIEW",
        "required_evidence": "append-only AWC COR/AMD review receipt for the exact station day",
        "automatic_write_authorized": False,
    },
    "样本不足": {
        "action": "ACCUMULATE_NATURAL_STRICT_OOS_SAMPLES",
        "required_evidence": "new independent strict-as-of closed-day samples; threshold unchanged",
        "automatic_write_authorized": False,
    },
    "utility 合同缺失": {
        "action": "MATERIALIZE_VERSIONED_UTILITY_CONTRACT",
        "required_evidence": "versioned candidate/baseline, official WS, fill, settlement, reserve and cadence contract declaration",
        "automatic_write_authorized": False,
    },
    "utility 证据绑定缺失": {
        "action": "BIND_REAL_EXECUTION_SETTLEMENT_UTILITY",
        "required_evidence": "same-decision official WS cost, confirmed fill/zero-fill receipt, settlement and reserve/cadence opportunity cost",
        "automatic_write_authorized": False,
    },
}

# The underlying audits use stable English machine reasons, while this
# operator-facing ledger deliberately exposes the bounded Chinese blocker
# classes (with utility-evidence binding kept separate from contract
# definition). Keep the raw reasons in ``diagnostic_blockers`` so no evidence is
# lost when several P1 time-bucket scopes collapse into one station row.
_P1_BLOCKER_MAP = {
    "data_missing": "数据缺失",
    "formal_metar_trajectory_artifact_missing": "数据缺失",
    "trajectory_artifact_missing": "数据缺失",
    "p1_replay_required": "arrival lineage 缺失",
    "no_formal_metar_or_speci_asof_no_eligible_p1": "数据缺失",
    "arrival_lineage_missing": "arrival lineage 缺失",
    "truth_missing": "truth 缺失",
    "sample_insufficient": "样本不足",
    "trajectory_oos_not_ready": "样本不足",
    "revision_unreviewed": "revision 未复核",
    "correction_requires_manual_review": "revision 未复核",
    "active_halt": "revision 未复核",
}

DEFAULTS = {
    # Retained append-only receipts; selecting the newest pointer never
    # downloads or rebuilds model data.
    "native": ROOT / "output/audits/native_ensemble_forward_oos_readonly_20260802_native_activation.json",
    # Rebuilt from the retained native manifest and the read-only SSOT at
    # 2026-08-02T00:00Z.  It makes already-consumable IFS/GEFS/GEPS pointers
    # explicit while leaving any failed latest OOS or stale generations
    # fail-closed; no historical raw is fetched or rewritten.
    "native_linkage": ROOT / "output/audits/native_ensemble_activation_oos_linkage_cutoff_20260803.json",
    "native_repair": ROOT / "output/audits/native_ensemble_activation_repair_plan_cutoff_20260803.json",
    # These are rebuilt only from persisted receipts and current materialized
    # P0 contracts.  Follow the bounded latest pointer rather than preserving
    # a stale pre-rebuild audit in the global SSOT ledger.
    "taf": ROOT / "output/audits/taf_p0m_exact_join_45station_20260802_current_p0_inbox.json",
    # The P0-M pointer remains the shared latest artifact above, while this
    # independent revision pointer consumes the fuller 163 station-day
    # forward-chain audit.  It adds ambiguous sequence rows to the ledger
    # without treating the 19-scope P0-M snapshot as a global promotion.
    "taf_revision": ROOT / "output/audits/taf_exact_join_quarantine_rebuild_20260802_forward_chain_audit/taf_revision_quarantine_receipt.json",
    "aifs": ROOT / "output/audits/aifs_deterministic_strict_oos_20260802_latest.json",
    # These are retained Open-Meteo deterministic historical contracts.  They
    # are deliberately separate from the official-native AIFS/ICON pointers:
    # the same model family can have both a native arrival lane and an
    # aggregator-republisher no-live fallback, but the latter must never be
    # promoted to native arrival, P1, formal/live authority or a native P0
    # gate.  Selecting these pointers only reads the already retained JSON;
    # it never refetches the Open-Meteo archive.
    "aifs_republisher": ROOT / "output/audits/open_meteo_aifs_deterministic_historical_oos_20260802.json",
    "icon_republisher": ROOT / "output/audits/open_meteo_icon_global_deterministic_historical_oos_20260802.json",
    # This is a content-addressed, read-only lineage pointer joining the
    # retained Open-Meteo deterministic reports to their no-live fallback
    # bindings.  It is supplementary audit evidence only; it never writes
    # native-arrival/P1/live authority or production state.
    "republisher_fallback_binding": ROOT / "config/bayes_basket_target/republisher_no_live_fallback_binding_v1.json",
    "aifs_readiness": ROOT / "output/audits/aifs_official_strict_oos_readiness_45station_20260802_lineage.json",
    "regional": ROOT / "output/audits/regional_model_candidate_closure_20260802_latest.json",
    "icon": ROOT / "output/audits/icon_global_eps_strict_oos_20260802.json",
    # Latest offline f015 lineage recheck.  It remains a fail-closed repair
    # receipt (14 verified objects + 1 missing original receipt), but keeping
    # the pointer current prevents the global ledger from silently consuming
    # an older inspection timestamp or treating the unrelated deterministic
    # ICON receipt as an EPS arrival substitute.
    "icon_arrival": ROOT / "output/audits/icon_global_eps_native_arrival_index_f015_recheck_20260801T225254Z.json",
    # This receipt includes the explicit candidate-intent/cycle and simulated
    # execution-receipt inventory.  It remains blocked/unbound and never
    # supplies a utility score.
    # Latest immutable station-policy no-live recheck.  This is an audit
    # pointer only; it never authorizes score emission or promotion.
    "policy": ROOT / "output/audits/station_policy_exact_stage_scorecard_45station_utility_recheck_20260803T054035Z.json",
    # These two retained receipts are an atomic 00:00Z pair: the repair plan
    # names the audit content hash it was derived from.  Do not substitute a
    # filename called "latest" unless it proves the same evidence boundary.
    "p1": ROOT / "output/audits/p1_trajectory_45station_audit_20260802.json",
    "p1_repair": ROOT / "output/audits/p1_trajectory_repair_plan_20260802.json",
    # Resident stage routing is an independent lineage gate. A stale P1
    # rooted at an older P0 cannot silently fall back to the refreshed P0;
    # keep that replay requirement in the same station×stage ledger.
    "p1_replay_binding": ROOT / "output/audits/resident_p1_replay_binding_20260802_latest.json",
    "vertical_chain": ROOT / "output/audits/historical_probability_vertical_chain_no_live_20260802.json",
    "delta": ROOT / "output/audits/delta_v2_cash_phase_oos_evidence_target_owned_projection_20260801T1325Z_canonical.json",
    "delta_observations": ROOT / "output/audits/delta_v2_target_owned_forward_observation_candidate_20260801T1325Z.json",
    "worker": ROOT / "output/audits/post_migration_worker_status_global_45station_20260802.json",
    "snapshot_manifest": ROOT / "output/audits/oos_atomic_snapshot_manifest_45station_20260802.json",
}


def _load(path: Path) -> Mapping[str, Any]:
    path = path.expanduser().resolve(strict=True)
    if not path.is_file() or path.stat().st_size > MAX_INPUT_BYTES:
        raise RuntimeError(f"bounded_input_invalid:{path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise RuntimeError(f"input_mapping_required:{path}")
    return value


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _republisher_fallback_binding_summary(
    binding: Mapping[str, Any],
    *,
    binding_path: Path,
) -> dict[str, Any]:
    """Return a bounded, lineage-only summary of the fallback pointer.

    The full pointer remains an input artifact.  The gap ledger carries only
    its content/file digests, model scope and explicit authority boundaries so
    downstream audits can prove which fallback lineage was observed without
    copying binding rows into the station/OOS records.  Validation is done by
    the pointer contract before this summary is emitted; no readiness or
    production gate is inferred here.
    """

    if not binding:
        return {
            "state": "MISSING_OPTIONAL_AUDIT",
            "schema": "",
            "path": str(binding_path),
            "file_sha256": None,
            "content_sha256": "",
            "binding_count": 0,
            "bound_model_ids": [],
            "candidate_only_model_ids": [],
            "source_role": "aggregator_republisher",
            "transport_provider_id": "OPEN_METEO",
            "authority_mode": "PRODUCTION_NO_LIVE_REPUBLISHER_FALLBACK",
            "formal_p0_eligible": False,
            "native_arrival_index_written": False,
            "p1_authority_written": False,
            "formal_live_authority_written": False,
            "production_database_written": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        }

    return {
        "state": str(binding.get("state") or "UNKNOWN"),
        "schema": str(binding.get("schema") or ""),
        "path": str(binding_path),
        "file_sha256": _sha(binding_path),
        "content_sha256": str(binding.get("content_sha256") or ""),
        "binding_count": int(binding.get("binding_count") or 0),
        "bound_model_ids": sorted(
            str(value) for value in binding.get("bound_model_ids") or []
        ),
        "candidate_only_model_ids": sorted(
            str(value) for value in binding.get("candidate_only_model_ids") or []
        ),
        "source_role": str(
            (binding.get("authority") or {}).get("source_role")
            or "aggregator_republisher"
        ),
        "transport_provider_id": str(
            (binding.get("authority") or {}).get("transport_provider_id")
            or "OPEN_METEO"
        ),
        "authority_mode": str(
            (binding.get("authority") or {}).get("authority_mode") or ""
        ),
        # These values are intentionally copied from the pointer's explicit
        # boundary and never computed as readiness.  They must remain false
        # even when the pointer contains complete historical 20+8 bindings.
        "formal_p0_eligible": bool(binding.get("formal_p0_eligible") is True),
        "native_arrival_index_written": bool(
            binding.get("native_arrival_index_written") is True
        ),
        "p1_authority_written": bool(binding.get("p1_authority_written") is True),
        "formal_live_authority_written": bool(
            binding.get("formal_live_authority_written") is True
        ),
        "production_database_written": bool(
            binding.get("production_database_written") is True
        ),
        "calls_order_gateway": bool(binding.get("calls_order_gateway") is True),
        "orders_submitted": int(binding.get("orders_submitted") or 0),
    }


def _station_event_date(*, as_of_utc: str, settlement_timezone: str) -> str:
    """Resolve the audit cut-off to the station's actual calendar date.

    P1 correction reviews are scoped to a local station day.  A fixed UTC
    literal silently assigns Asia/Pacific stations to the wrong review scope,
    so ledger rows must carry the same local date as the runtime reader.
    """

    try:
        parsed = datetime.fromisoformat(str(as_of_utc).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("p1_audit_as_of_utc_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError("p1_audit_as_of_utc_timezone_required")
    try:
        zone = ZoneInfo(str(settlement_timezone))
    except Exception as exc:
        raise RuntimeError("p1_audit_settlement_timezone_invalid") from exc
    return parsed.astimezone(timezone.utc).astimezone(zone).date().isoformat()


def _event_date_is_future(event_date: str, as_of_utc: str) -> bool:
    """Return whether an event-day row is still a forward observation.

    This intentionally uses the evidence receipt's UTC date as a conservative
    lower bound.  A row on or before that date is not automatically historical
    historical: its own closed-AWC/truth checks still decide that.  The helper
    only prevents an unmistakably future event from entering OOS counts.
    """

    try:
        event = datetime.fromisoformat(str(event_date).strip()[:10]).date()
        as_of = datetime.fromisoformat(
            str(as_of_utc).replace("Z", "+00:00")
        ).date()
    except (TypeError, ValueError):
        return False
    return event > as_of


def _evidence_role_for_event_date(
    *,
    event_date: str,
    as_of_utc: str,
    closed_day: bool | None = None,
) -> str:
    """Classify a future event as an observation, never as OOS.

    The ledger has several per-event evidence sources.  Centralising this
    conversion prevents a newly added future audit (such as a TAF revision
    chain) from accidentally inheriting the historical OOS role merely
    because ``_record`` defaults to it.
    """

    return (
        "observational_candidate_not_oos"
        if _event_date_is_future(event_date, as_of_utc) or closed_day is False
        else "historical_strict_oos"
    )


def _validate_p1_repair_evidence_pair(
    p1_audit: Mapping[str, Any], p1_repair: Mapping[str, Any]
) -> None:
    """Reject a ledger that joins P1 eligibility to another evidence instant.

    The P1 repair plan reuses the audit's correction/training/OOS facts.  Its
    activation lookup must be at exactly that audit instant, otherwise a
    candidate pointer can be reported alongside a proof that never evaluated
    it.  This is an evidence-boundary check, not a promotion gate.
    """

    if (
        str(p1_audit.get("schema") or "") != P1_AUDIT_SCHEMA
        or str(p1_repair.get("schema") or "") != P1_REPAIR_SCHEMA
    ):
        raise RuntimeError("p1_evidence_pair_schema_invalid")
    audit_as_of = str(p1_audit.get("as_of_utc") or "")
    repair_as_of = str(p1_repair.get("as_of_utc") or "")
    audit_sha = str(p1_audit.get("content_sha256") or "")
    if not audit_as_of or not repair_as_of or not audit_sha:
        raise RuntimeError("p1_evidence_pair_identity_missing")
    if repair_as_of != audit_as_of:
        raise RuntimeError("p1_evidence_pair_as_of_mismatch")
    if str(p1_repair.get("audit_content_sha256") or "") != audit_sha:
        raise RuntimeError("p1_evidence_pair_audit_hash_mismatch")


def _validate_snapshot_manifest(
    snapshot: Mapping[str, Any],
    *,
    paths: Mapping[str, Path],
    taf: Mapping[str, Any],
    stations: tuple[str, ...],
) -> None:
    """Require ledger inputs to be the same byte-addressed evidence window."""

    if snapshot.get("schema") != SNAPSHOT_MANIFEST_SCHEMA:
        raise RuntimeError("oos_snapshot_manifest_schema_invalid")
    supplied = str(snapshot.get("content_sha256") or "").lower()
    identity = {
        key: value for key, value in snapshot.items() if key != "content_sha256"
    }
    if len(supplied) != 64 or content_sha256(identity) != supplied:
        raise RuntimeError("oos_snapshot_manifest_content_sha_mismatch")
    if int(snapshot.get("canonical_station_count") or 0) != len(stations):
        raise RuntimeError("oos_snapshot_manifest_station_count_mismatch")
    if tuple(str(item).upper() for item in snapshot.get("canonical_station_ids") or []) != stations:
        raise RuntimeError("oos_snapshot_manifest_station_scope_mismatch")
    inputs = snapshot.get("input_artifacts")
    if not isinstance(inputs, Mapping):
        raise RuntimeError("oos_snapshot_manifest_inputs_missing")
    for snapshot_key, ledger_key in (
        ("taf_audit", "taf"),
        ("native_linkage", "native_linkage"),
        ("native_repair", "native_repair"),
    ):
        item = inputs.get(snapshot_key)
        if not isinstance(item, Mapping):
            raise RuntimeError(f"oos_snapshot_manifest_input_missing:{snapshot_key}")
        if str(item.get("path") or "") != str(paths[ledger_key].resolve()):
            raise RuntimeError(f"oos_snapshot_manifest_path_mismatch:{snapshot_key}")
        if str(item.get("file_sha256") or "").lower() != _sha(paths[ledger_key]).lower():
            raise RuntimeError(f"oos_snapshot_manifest_file_hash_mismatch:{snapshot_key}")
    p0 = inputs.get("p0_inbox")
    if not isinstance(p0, Mapping) or len(str(p0.get("declared_content_sha256") or "")) != 64:
        raise RuntimeError("oos_snapshot_manifest_p0_pointer_missing")
    taf_input = taf.get("input_artifacts")
    taf_p0 = taf_input.get("p0_inbox") if isinstance(taf_input, Mapping) else None
    if not isinstance(taf_p0, Mapping) or str(taf_p0.get("content_sha256") or "").lower() != str(p0.get("declared_content_sha256") or "").lower():
        raise RuntimeError("oos_snapshot_manifest_taf_p0_pointer_mismatch")
    native_binding = snapshot.get("native_p0_binding")
    if not isinstance(native_binding, Mapping):
        raise RuntimeError("oos_snapshot_manifest_native_p0_binding_missing")
    binding_identity = {
        key: value for key, value in native_binding.items()
        if key != "content_sha256"
    }
    if (
        str(native_binding.get("schema") or "")
        != "native_p0_consumable_binding_v1"
        or content_sha256(binding_identity)
        != str(native_binding.get("content_sha256") or "").lower()
        or str(native_binding.get("source_linkage_content_sha256") or "").lower()
        != str(inputs["native_linkage"].get("declared_content_sha256") or "").lower()
        or str(native_binding.get("source_repair_content_sha256") or "").lower()
        != str(inputs["native_repair"].get("declared_content_sha256") or "").lower()
    ):
        raise RuntimeError("oos_snapshot_manifest_native_p0_binding_mismatch")


def _record(
    *,
    station_id: str,
    model_family: str,
    model_id: str,
    run_cycle: str = "*",
    lead: str = "*",
    event_date: str = "*",
    source_class: str,
    native_arrival_verified: bool,
    raw_receipt_hash_complete: bool,
    awc_truth_closed: bool,
    taf_revision_complete: bool,
    train_count: int = 0,
    holdout_count: int = 0,
    state: str,
    blockers: list[str],
    evidence: list[str],
    diagnostic_blockers: list[str] | None = None,
    evidence_role: str = "historical_strict_oos",
    strict_oos_gate_applies: bool | None = None,
) -> dict[str, Any]:
    if evidence_role not in EVIDENCE_ROLES:
        raise RuntimeError(f"unknown_evidence_role:{evidence_role}")
    bad = sorted(set(blockers))
    unknown = [item for item in bad if item not in BLOCKER_TYPES]
    if unknown:
        raise RuntimeError(f"unknown_gap_type:{','.join(unknown)}")
    applies_to_hard_gate = (
        evidence_role == "historical_strict_oos"
        if strict_oos_gate_applies is None
        else bool(strict_oos_gate_applies)
    )
    return {
        "station_id": station_id,
        "model_family": model_family,
        "model_id": model_id,
        "run_cycle": run_cycle,
        "lead": lead,
        "event_date": event_date,
        "source_class": source_class,
        "native_arrival_verified": native_arrival_verified,
        "raw_receipt_hash_complete": raw_receipt_hash_complete,
        "awc_truth_closed": awc_truth_closed,
        "taf_revision_complete": taf_revision_complete,
        "train_count": int(train_count),
        "holdout_count": int(holdout_count),
        "state": state,
        "blockers": bad,
        "hard_blockers": bad if applies_to_hard_gate else [],
        "observational_candidate_blockers": (
            bad if evidence_role == "observational_candidate_not_oos" else []
        ),
        "resolution_actions": [
            {"blocker": blocker, **BLOCKER_RESOLUTION_ACTIONS[blocker]}
            for blocker in bad
        ],
        "diagnostic_blockers": sorted(set(diagnostic_blockers or [])),
        "evidence": evidence,
        "evidence_role": evidence_role,
        "strict_oos_gate_applies": applies_to_hard_gate,
        "future_event_not_oos": (
            evidence_role == "observational_candidate_not_oos"
        ),
    }


def _is_sha256(value: Any) -> bool:
    """Return whether *value* is a complete lowercase/uppercase SHA-256."""

    raw = str(value or "").strip()
    if len(raw) != 64:
        return False
    try:
        int(raw, 16)
    except ValueError:
        return False
    return True


def _deterministic_republisher_records(
    artifact: Mapping[str, Any],
    *,
    artifact_path: Path,
    stations: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Expand one retained deterministic Open-Meteo receipt into ledger rows.

    The receipt is already a historical calibration/OOS candidate.  This
    helper only changes its shape to the ledger grain
    ``station × model × run × lead × event_date``; it never fetches data or
    upgrades the source's authority.  Every valid historical prediction is
    marked ``aggregator_republisher`` and ``historical_strict_oos`` while
    native-arrival, native-P0, P1 and formal/live flags remain false.  A
    future or unbound prediction is retained as an observational candidate
    with zero OOS sample counts.
    """

    source_model_id = str(artifact.get("model_id") or "UNKNOWN").strip().upper()
    # Keep the model family human-readable while making the republisher
    # lineage explicit in the primary key.  The official-native AIFS/ICON
    # rows use different IDs, so both lanes can coexist without overwriting
    # one another.
    republisher_model_id = f"{source_model_id}_OPEN_METEO_REPUBLISHER"
    as_of_utc = str(artifact.get("as_of_utc") or "")
    source_role_claim = str(
        artifact.get("source_role") or "aggregator_republisher"
    )
    transport_provider_claim = str(
        artifact.get("transport_provider_id")
        or artifact.get("provider_id")
        or "OPEN_METEO"
    )
    # The ledger's lineage fields are normative, not a pass-through of an
    # untrusted artifact claim.  A malformed receipt is still surfaced as an
    # Open-Meteo republisher row (with a diagnostic mismatch), never upgraded
    # to native because of a provider/role string.
    source_role = "aggregator_republisher"
    transport_provider = "OPEN_METEO"
    # The deterministic artifact contract requires this role/provider.  Keep
    # the emitted source class fixed even if a malformed input claims another
    # role, and expose the mismatch only as a diagnostic field below.
    source_class = "aggregator_republisher"
    top_level_receipts = artifact.get("capture_receipt_sha256s")
    top_level_receipts_complete = (
        isinstance(top_level_receipts, (list, tuple))
        and bool(top_level_receipts)
        and all(_is_sha256(value) for value in top_level_receipts)
    )
    candidate_rows = {
        str(row.get("station_id") or "").strip().upper(): row
        for row in artifact.get("candidates") or []
        if isinstance(row, Mapping) and str(row.get("station_id") or "").strip()
    }
    records: list[dict[str, Any]] = []

    def _finalize(
        row: dict[str, Any],
        *,
        candidate: Mapping[str, Any] | None,
        prediction: Mapping[str, Any] | None,
        raw_receipt_complete: bool,
        temporal_scope: str,
        source_role_mismatch: bool,
    ) -> dict[str, Any]:
        candidate = candidate or {}
        prediction = prediction or {}
        row.update({
            # Open-Meteo is never a native arrival source, irrespective of
            # whether an official native object exists for the same model.
            "native_arrival_index_eligible": False,
            "native_p0_gate_eligible": False,
            "formal_p0_eligible": False,
            "formal_live_eligible": False,
            "p1_eligible": False,
            "historical_oos_calibration_fallback": bool(
                row["evidence_role"] == "historical_strict_oos"
                and row["awc_truth_closed"]
                and raw_receipt_complete
            ),
            "historical_republisher_contract_verified": bool(
                row["evidence_role"] == "historical_strict_oos"
                and row["awc_truth_closed"]
                and row["raw_receipt_hash_complete"]
            ),
            "republisher_no_live_fallback_only": True,
            "source_role": source_role,
            "transport_provider_id": transport_provider,
            "deterministic_republisher_schema": str(artifact.get("schema") or ""),
            "oos_event_temporal_scope": temporal_scope,
            "historical_oos_contract_id": str(
                prediction.get("historical_oos_contract_id") or ""
            ),
            "member_run_content_sha256s": [
                str(value)
                for value in prediction.get("member_run_content_shas") or []
                if str(value)
            ],
            "source_role_mismatch": source_role_mismatch,
        })
        if candidate:
            row["candidate_state"] = str(candidate.get("state") or "")
            row["candidate_content_sha256"] = str(candidate.get("content_sha256") or "")
        row["raw_receipt_hash_complete"] = bool(raw_receipt_complete)
        return row

    for station in stations:
        candidate = candidate_rows.get(station)
        predictions = [
            item for item in (candidate or {}).get("predictions") or []
            if isinstance(item, Mapping)
        ]
        # The ledger primary key contains event_date.  If a malformed receipt
        # repeats a date, retain the first immutable row and mark the duplicate
        # only in diagnostics rather than emitting a duplicate key.
        predictions_by_date: dict[str, Mapping[str, Any]] = {}
        duplicate_dates: set[str] = set()
        for prediction in predictions:
            event_date = str(prediction.get("event_date") or "").strip()
            if not event_date:
                continue
            if event_date in predictions_by_date:
                duplicate_dates.add(event_date)
                continue
            predictions_by_date[event_date] = prediction

        if candidate and predictions_by_date:
            candidate_future_count = int(candidate.get("future_event_count") or 0)
            candidate_scope = str(
                candidate.get("oos_event_temporal_scope")
                or (artifact.get("oos_policy") or {}).get("temporal_scope")
                or ""
            )
            for event_date in sorted(predictions_by_date):
                prediction = predictions_by_date[event_date]
                temporal_scope = str(
                    prediction.get("temporal_scope") or candidate_scope
                )
                contract_id = str(
                    prediction.get("historical_oos_contract_id") or ""
                ).strip()
                receipt_class = str(prediction.get("receipt_class") or "")
                actual_present = prediction.get("actual_tmax_c") is not None
                future_signal = (
                    _event_date_is_future(event_date, as_of_utc)
                    or candidate_future_count > 0
                )
                closed_historical = bool(
                    not future_signal
                    and temporal_scope == "CLOSED_HISTORICAL_AWC_TRUTH"
                    and receipt_class == "HISTORICAL_ASOF_REPUBLISHER_RECEIPT"
                    and contract_id
                    and actual_present
                )
                evidence_role = (
                    "historical_strict_oos"
                    if closed_historical
                    else "observational_candidate_not_oos"
                )
                train = int(candidate.get("train_n") or 0) if closed_historical else 0
                holdout = int(candidate.get("holdout_n") or 0) if closed_historical else 0
                blockers: list[str] = []
                if not actual_present or not contract_id or temporal_scope != "CLOSED_HISTORICAL_AWC_TRUTH":
                    blockers.append("truth 缺失")
                if not future_signal and (
                    train < int(candidate.get("min_train_days") or 20)
                    or holdout < int(candidate.get("min_holdout_days") or 8)
                ):
                    blockers.append("样本不足")
                # A future packet is intentionally visible but never a hard
                # gate.  Keep its missing truth/sample diagnostics without
                # allowing any train/holdout count to leak into OOS totals.
                if future_signal:
                    blockers.extend(["truth 缺失", "样本不足"])
                member_hashes = prediction.get("member_run_content_shas") or []
                raw_complete = bool(
                    top_level_receipts_complete
                    and _is_sha256(candidate.get("content_sha256"))
                    and isinstance(member_hashes, (list, tuple))
                    and bool(member_hashes)
                    and all(_is_sha256(value) for value in member_hashes)
                )
                if not raw_complete:
                    blockers.append("数据缺失")
                diagnostic = []
                if duplicate_dates and event_date in duplicate_dates:
                    diagnostic.append("duplicate_event_date")
                if source_role_claim != "aggregator_republisher":
                    diagnostic.append("source_role_not_aggregator_republisher")
                if transport_provider_claim != "OPEN_METEO":
                    diagnostic.append("transport_provider_not_open_meteo")
                if str(artifact.get("schema") or "") != "deterministic_republisher_strict_oos_v1":
                    diagnostic.append("deterministic_republisher_schema_mismatch")
                if str(candidate.get("state") or "") == "OOS_REJECTED":
                    diagnostic.append("candidate_oos_rejected")
                row = _record(
                    station_id=station,
                    model_family=source_model_id,
                    model_id=republisher_model_id,
                    run_cycle=str(candidate.get("run_cycle") or "AGGREGATOR_UNKNOWN_NATIVE_CYCLE"),
                    lead=str(candidate.get("lead_bucket") or "d0"),
                    event_date=event_date,
                    source_class=source_class,
                    native_arrival_verified=False,
                    raw_receipt_hash_complete=raw_complete,
                    awc_truth_closed=closed_historical,
                    taf_revision_complete=False,
                    train_count=train,
                    holdout_count=holdout,
                    state=(
                        str(candidate.get("state") or artifact.get("state") or "READY_CANDIDATE")
                        if evidence_role == "historical_strict_oos"
                        else "OBSERVATIONAL_CANDIDATE_NOT_OOS"
                    ),
                    blockers=sorted(set(blockers)),
                    diagnostic_blockers=diagnostic,
                    evidence=[str(artifact_path)],
                    evidence_role=evidence_role,
                    strict_oos_gate_applies=(evidence_role == "historical_strict_oos"),
                )
                records.append(_finalize(
                    row,
                    candidate=candidate,
                    prediction=prediction,
                    raw_receipt_complete=raw_complete,
                    temporal_scope=temporal_scope,
                    source_role_mismatch=(
                        source_role_claim != "aggregator_republisher"
                        or transport_provider_claim != "OPEN_METEO"
                    ),
                ))
            continue

        # No candidate/prediction was retained for this canonical station.
        # Use explicit missing_truth_scopes when available; otherwise retain a
        # wildcard row.  Neither branch fabricates an event date or samples.
        missing_scopes = [
            item for item in artifact.get("missing_truth_scopes") or []
            if isinstance(item, Mapping)
            and str(item.get("station_id") or "").strip().upper() == station
        ]
        missing_dates = sorted({
            str(item.get("event_date") or "").strip()
            for item in missing_scopes
            if str(item.get("event_date") or "").strip()
        }) or ["*"]
        for event_date in missing_dates:
            missing_is_future = _event_date_is_future(event_date, as_of_utc)
            missing_role = (
                "observational_candidate_not_oos"
                if missing_is_future or event_date == "*"
                else "historical_strict_oos"
            )
            row = _record(
                station_id=station,
                model_family=source_model_id,
                model_id=republisher_model_id,
                run_cycle="AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
                lead="d0",
                event_date=event_date,
                source_class=source_class,
                native_arrival_verified=False,
                raw_receipt_hash_complete=False,
                awc_truth_closed=False,
                taf_revision_complete=False,
                train_count=0,
                holdout_count=0,
                state="HISTORICAL_REPUBLISHER_DATA_MISSING",
                blockers=["数据缺失", "truth 缺失", "样本不足"],
                diagnostic_blockers=["candidate_missing", "historical_truth_scope_missing"],
                evidence=[str(artifact_path)],
                evidence_role=missing_role,
                # The event date may be historical, but this row has no
                # closed truth/strict contract.  Keep it visible in the
                # historical cohort without allowing its blockers to become
                # a strict-OOS hard gate; only complete historical facts can
                # set ``strict_oos_gate_applies`` true.
                strict_oos_gate_applies=False,
            )
            records.append(_finalize(
                row,
                candidate=None,
                prediction=None,
                raw_receipt_complete=False,
                temporal_scope="CLOSED_HISTORICAL_AWC_TRUTH",
                source_role_mismatch=(
                    source_role_claim != "aggregator_republisher"
                    or transport_provider_claim != "OPEN_METEO"
                ),
            ))

    return records


def build(args: argparse.Namespace) -> dict[str, Any]:
    # ``build`` is also imported by read-only tests with a hand-built
    # Namespace.  Fill omitted optional/default pointers from ``DEFAULTS`` so
    # adding the deterministic republisher lanes does not make those callers
    # accidentally KeyError before the existing snapshot validation runs.
    paths = {
        key: Path(getattr(args, key, default))
        for key, default in DEFAULTS.items()
    }
    # The replay audit is a supplementary read-only receipt.  Keep the
    # ledger runnable from a clean checkout before that receipt is generated;
    # an absent optional audit contributes no rows and is exposed as MISSING
    # in the global gate rather than blocking unrelated model evidence.
    artifacts = {
        key: (
            {}
            if key in _OPTIONAL_INPUT_KEYS and not path.exists()
            else _load(path)
        )
        for key, path in paths.items()
    }
    # The fallback pointer is an append-only lineage artifact.  Validate it
    # before any ledger rows are built so a tampered/over-broad pointer cannot
    # be mistaken for a harmless Open-Meteo receipt.  A missing optional
    # pointer remains visible as MISSING_OPTIONAL_AUDIT and does not block the
    # existing native/OOS ledger inputs.
    if artifacts["republisher_fallback_binding"]:
        validate_republisher_fallback_binding(
            artifacts["republisher_fallback_binding"]
        )
    _validate_p1_repair_evidence_pair(artifacts["p1"], artifacts["p1_repair"])
    registry = coordinate_registry_manifest()
    stations = tuple(str(item).upper() for item in registry["station_ids"])
    if len(stations) != MAX_STATIONS or len(set(stations)) != MAX_STATIONS:
        raise RuntimeError("canonical_station_count_not_45")
    _validate_snapshot_manifest(
        artifacts["snapshot_manifest"],
        paths=paths,
        taf=artifacts["taf"],
        stations=stations,
    )

    records: list[dict[str, Any]] = []
    native_reports = {
        str(row.get("station_id") or "").upper(): row
        for row in artifacts["native"].get("reports", [])
        if isinstance(row, Mapping)
    }
    native_systems = (
        ("ECMWF", "ECMWF_IFS_ENS_OPEN_PF50", "18Z"),
        ("NOAA_GEFS", "NOAA_GEFS_05", "18Z"),
        ("ECCC", "ECCC_GEM_GLOBAL_ENS", "00Z"),
    )
    for station in stations:
        report = native_reports.get(station, {})
        readiness = report.get("readiness") if isinstance(report, Mapping) else []
        readiness = readiness if isinstance(readiness, list) else []
        for family, model_id, cycle in native_systems:
            rows = [
                row for row in readiness
                if isinstance(row, Mapping)
                and str(row.get("ensemble_system_id") or "") == model_id
                and str(row.get("run_cycle") or "") in {cycle, "*"}
            ]
            row = max(rows, key=lambda item: int(item.get("case_count") or 0), default={})
            train = int(row.get("training_case_count") or row.get("train_n") or 0)
            holdout = int(row.get("strict_holdout_case_count") or row.get("holdout_n") or 0)
            state = str(row.get("state") or "DATA_MISSING")
            blockers: list[str] = []
            if not row or int(row.get("case_count") or 0) == 0:
                blockers.append("数据缺失")
            if train < 20 or holdout < 8:
                blockers.append("样本不足")
            # A strict native OOS row already carries closed historical target
            # facts (the holdout actuals are part of that immutable receipt).
            # Do not label those facts as ``truth_missing`` merely because a
            # later P1/TAF stage is still blocked.
            native_truth_closed = bool(
                row
                and holdout >= 8
                and state in {"OOS_READY", "PRODUCTION_NO_LIVE_OOS_PASS"}
            )
            # A native OOS receipt carries the exact holdout event dates in
            # ``predictions``.  Emit one ledger row per event date so the
            # primary key remains station × model × run × lead × event_date;
            # keep a wildcard only for a missing/blocked receipt that has no
            # event-level evidence to enumerate.
            system_evaluation: Mapping[str, Any] = {}
            if isinstance(report.get("system_evaluations"), Mapping):
                candidate_evaluation = report["system_evaluations"].get(model_id)
                if isinstance(candidate_evaluation, Mapping):
                    system_evaluation = candidate_evaluation
            if not system_evaluation:
                for candidate in report.get("cross_candidates") or []:
                    if not isinstance(candidate, Mapping):
                        continue
                    candidate_evaluations = candidate.get("system_evaluations")
                    if not isinstance(candidate_evaluations, Mapping):
                        continue
                    candidate_evaluation = candidate_evaluations.get(model_id)
                    if isinstance(candidate_evaluation, Mapping):
                        system_evaluation = candidate_evaluation
                        break
            prediction_rows = row.get("predictions") or (
                system_evaluation.get("predictions")
                if isinstance(system_evaluation, Mapping)
                else []
            )
            event_dates = sorted({
                str(prediction.get("event_date") or "").strip()
                for prediction in (prediction_rows or [])
                if isinstance(prediction, Mapping)
                and str(prediction.get("event_date") or "").strip()
            })
            for event_date in event_dates or ["*"]:
                evidence_role = _evidence_role_for_event_date(
                    event_date=event_date,
                    as_of_utc=str(artifacts["native"].get("as_of_utc") or ""),
                )
                records.append(_record(
                    station_id=station, model_family=family, model_id=model_id,
                    run_cycle=str(row.get("run_cycle") or cycle), lead=str(row.get("lead_bucket") or "d2"),
                    event_date=event_date,
                    source_class="official_native", native_arrival_verified=bool(row and row.get("transport_provider_id") in {"ECMWF_OPEN_DATA_S3", "NOAA_GEFS_PDS_S3", "ECCC_DD_WEATHER"}),
                    raw_receipt_hash_complete=bool(row and int(row.get("case_count") or 0) > 0),
                    awc_truth_closed=native_truth_closed, taf_revision_complete=False,
                    train_count=train, holdout_count=holdout, state=state,
                    blockers=blockers, evidence=[str(paths["native"])],
                    evidence_role=evidence_role,
                ))

    taf_entries = {
        str(row.get("station_id") or "").upper(): row
        for row in artifacts["taf"].get("entries", [])
        if isinstance(row, Mapping)
    }
    for station in stations:
        row = taf_entries.get(station, {})
        exact = int(row.get("exact_join_sample_count") or row.get("sample_count") or 0)
        exact_join = row.get("exact_join") if isinstance(row.get("exact_join"), Mapping) else {}
        if not exact:
            exact = int(exact_join.get("sample_count") or 0)
        raw_blockers = {str(value) for value in row.get("blockers") or []}
        raw_blockers.update(str(value) for value in exact_join.get("errors") or [])
        blockers: list[str] = []
        if (
            not exact
            or "exact_join_artifact_missing" in raw_blockers
            or "current_p0_scope_missing" in raw_blockers
        ):
            blockers.append("数据缺失")
        if exact < 24 or any("sample_n_insufficient" in value for value in raw_blockers):
            blockers.append("样本不足")
        if any("amd_cor" in value for value in raw_blockers):
            blockers.append("revision 未复核")
        lineage_count = int(exact_join.get("lineage_pointer_count") or 0)
        truth_closed = lineage_count >= exact and exact > 0
        if not truth_closed:
            blockers.append("truth 缺失")
        revision_complete = exact > 0 and not any("amd_cor" in value for value in raw_blockers)
        scope = row.get("scope") or exact_join.get("scope") or {}
        if isinstance(scope, Mapping):
            run_cycle = str(scope.get("run_cycle") or "*")
            lead = str(scope.get("lead_bucket") or "*")
        else:
            scope = list(scope) if isinstance(scope, (list, tuple)) else ["*", "*", "*"]
            run_cycle = str(scope[2] if len(scope) > 2 else "*")
            lead = str(scope[1] if len(scope) > 1 else "*")
        records.append(_record(
            station_id=station, model_family="AWC_TAF", model_id="AWC_TAF_P0M",
            run_cycle=run_cycle, lead=lead, event_date="2026-08-01",
            source_class="official_awc", native_arrival_verified=True,
            raw_receipt_hash_complete=exact > 0 and bool(exact_join.get("content_sha256") or row.get("artifact_sha256")),
            awc_truth_closed=truth_closed, taf_revision_complete=revision_complete,
            train_count=exact, holdout_count=0,
            state=str(row.get("state") or "EXACT_JOIN_ACCUMULATING"), blockers=blockers,
            evidence=[str(paths["taf"]), str(paths["taf_revision"])],
            evidence_role=_evidence_role_for_event_date(
                event_date="2026-08-01",
                as_of_utc=str(artifacts["taf"].get("as_of_utc") or ""),
                closed_day=truth_closed,
            ),
        ))

    # Forward receipts can preserve multiple official capture attempts for a
    # station-day.  They are only a usable TAF revision chain when the
    # receipt's explicit sequence is linear.  Emit the ambiguous days at the
    # required station × model × run × lead × event_date grain rather than
    # hiding them in a global counter or choosing an arbitrary predecessor.
    forward_chain_audit = artifacts["taf_revision"].get(
        "forward_revision_chain_audit", {}
    )
    if isinstance(forward_chain_audit, Mapping):
        truth_by_day = {
            (str(row.get("station_id") or "").upper(), str(row.get("event_date") or "")):
            bool(row.get("awc_truth_closed"))
            for row in artifacts["taf_revision"].get("revision_days", [])
            if isinstance(row, Mapping)
        }
        for row in forward_chain_audit.get("station_days", []):
            if not isinstance(row, Mapping):
                continue
            station = str(row.get("station_id") or "").upper()
            event_date = str(row.get("event_date") or "")
            if station not in stations or not event_date:
                continue
            if str(row.get("state") or "") == "LINEAR_CHAIN_VERIFIED":
                continue
            diagnostic = [str(value) for value in row.get("blockers") or []]
            records.append(_record(
                station_id=station,
                model_family="AWC_TAF",
                model_id="AWC_TAF_FORWARD_REVISION_CHAIN",
                run_cycle="*",
                lead="*",
                event_date=event_date,
                source_class="official_forward_native_receipt",
                native_arrival_verified=True,
                raw_receipt_hash_complete=bool(row.get("revisions")) and all(
                    len(str(item.get("content_sha256") or "")) == 64
                    and len(str(item.get("source_object_sha256") or "")) == 64
                    for item in row.get("revisions", [])
                    if isinstance(item, Mapping)
                ),
                awc_truth_closed=truth_by_day.get((station, event_date), False),
                taf_revision_complete=False,
                train_count=0,
                holdout_count=0,
                state=str(row.get("state") or "AMBIGUOUS_FAIL_CLOSED"),
                blockers=["revision 未复核"],
                diagnostic_blockers=diagnostic,
                evidence=[str(paths["taf_revision"])],
                evidence_role=_evidence_role_for_event_date(
                    event_date=event_date,
                    as_of_utc=str(
                        artifacts["taf_revision"].get("as_of_utc") or ""
                    ),
                    closed_day=truth_by_day.get((station, event_date), False),
                ),
            ))

    aifs_inventory = {
        str(row.get("station_id") or "").upper(): row
        for row in artifacts["aifs"].get("station_inventory", [])
        if isinstance(row, Mapping)
    }
    # The deterministic AIFS runner is a newer, bounded receipt that exposes
    # one strict-OOS shortfall per retained station rather than the older
    # inventory envelope.  Reuse its immutable facts; never infer missing
    # cases as successful samples.
    if not aifs_inventory and artifacts["aifs"].get("schema") == "aifs_deterministic_strict_oos_runner_receipt_v1":
        aifs_inventory = {
            str(row.get("station_id") or "").upper(): {
                "event_date": row.get("event_date_end") or "2026-08-01",
                "closed_day_outcome": artifacts["aifs"].get("state") or "PENDING_STRICT_OOS_SAMPLES",
                "accepted_awc_truth_available": int(row.get("available_case_count") or 0) > 0,
                "curve_state": "COMPLETE" if int(row.get("missing_case_count") or 0) == 0 else "INCOMPLETE",
                "available_case_count": int(row.get("available_case_count") or 0),
                "required_total_case_count": int(row.get("required_total_case_count") or 28),
                "run_cycle": row.get("run_cycle") or "18Z",
                "lead_bucket": row.get("lead_bucket") or "*",
            }
            for row in artifacts["aifs"].get("scope_shortfalls", [])
            if isinstance(row, Mapping) and str(row.get("station_id") or "").strip()
        }
    for station in stations:
        row = aifs_inventory.get(station, {})
        outcome = str(row.get("closed_day_outcome") or "")
        blockers = []
        if not row:
            blockers.append("数据缺失")
        elif outcome == "STATION_LOCAL_DAY_OPEN":
            blockers.append("数据缺失")
        elif not row.get("accepted_awc_truth_available"):
            blockers.append("truth 缺失")
        facts = int(row.get("available_case_count") or (1 if row.get("accepted_awc_truth_available") else 0))
        required_cases = int(row.get("required_total_case_count") or 28)
        if facts < required_cases:
            blockers.append("样本不足")
        event_date = str(row.get("event_date") or "2026-08-01")
        aifs_evidence_role = _evidence_role_for_event_date(
            event_date=event_date,
            as_of_utc=str(artifacts["aifs"].get("as_of_utc") or ""),
            closed_day=bool(row.get("accepted_awc_truth_available")),
        )
        records.append(_record(
            station_id=station, model_family="AIFS", model_id="ECMWF_AIFS_SINGLE_0P25",
            run_cycle="18Z", lead="d0", event_date=event_date,
            source_class="official_native_deterministic", native_arrival_verified=True,
            raw_receipt_hash_complete=bool(row.get("curve_state") == "COMPLETE"),
            awc_truth_closed=bool(row.get("accepted_awc_truth_available")), taf_revision_complete=False,
            train_count=facts, holdout_count=0, state=outcome or str(row.get("curve_state") or "UNKNOWN"),
            blockers=blockers, evidence=[str(paths["aifs"])], evidence_role=aifs_evidence_role,
        ))

    # Expand the retained deterministic Open-Meteo historical receipts at the
    # same station×event-date grain as native OOS evidence.  These records are
    # calibration/no-live fallback evidence only; they remain separate from
    # the official-native AIFS/ICON rows and cannot alter formal/live state.
    deterministic_republisher_meta: dict[str, dict[str, Any]] = {}
    for input_key in ("aifs_republisher", "icon_republisher"):
        artifact = artifacts.get(input_key) or {}
        artifact_path = paths[input_key]
        if not artifact:
            deterministic_republisher_meta[input_key] = {
                "state": "MISSING_OPTIONAL_AUDIT",
                "schema": "",
                "record_count": 0,
                "historical_strict_oos_record_count": 0,
                "observational_candidate_not_oos_record_count": 0,
                "station_count": 0,
                "candidate_count": 0,
                "curve_count": 0,
                "truth_count": 0,
                "future_event_count": 0,
                "formal_p0_eligible": False,
                "formal_live_eligible": False,
                "p1_eligible": False,
                "native_arrival_index_eligible": False,
                "source_class": "aggregator_republisher",
                "source_role": "aggregator_republisher",
                "transport_provider_id": "OPEN_METEO",
                "path": str(artifact_path),
            }
            continue
        republisher_records = _deterministic_republisher_records(
            artifact,
            artifact_path=artifact_path,
            stations=stations,
        )
        records.extend(republisher_records)
        deterministic_republisher_meta[input_key] = {
            "state": str(artifact.get("state") or "UNKNOWN"),
            "schema": str(artifact.get("schema") or ""),
            "record_count": len(republisher_records),
            "historical_strict_oos_record_count": sum(
                row.get("evidence_role") == "historical_strict_oos"
                for row in republisher_records
            ),
            "historical_strict_oos_train_count": sum(
                int(row.get("train_count") or 0)
                for row in republisher_records
                if row.get("evidence_role") == "historical_strict_oos"
            ),
            "historical_strict_oos_holdout_count": sum(
                int(row.get("holdout_count") or 0)
                for row in republisher_records
                if row.get("evidence_role") == "historical_strict_oos"
            ),
            "strict_oos_gate_applies_record_count": sum(
                bool(row.get("strict_oos_gate_applies"))
                for row in republisher_records
            ),
            "observational_candidate_not_oos_record_count": sum(
                row.get("evidence_role") == "observational_candidate_not_oos"
                for row in republisher_records
            ),
            "station_count": int(artifact.get("station_count") or 0),
            "candidate_count": int(artifact.get("candidate_count") or 0),
            "curve_count": int(artifact.get("curve_count") or 0),
            "truth_count": int(artifact.get("truth_count") or 0),
            "future_event_count": int(
                (artifact.get("oos_policy") or {}).get("future_event_count") or 0
            ),
            "formal_p0_eligible": bool(artifact.get("formal_p0_eligible") is True),
            "formal_live_eligible": False,
            "p1_eligible": False,
            "native_arrival_index_eligible": False,
            "source_class": "aggregator_republisher",
            "source_role": "aggregator_republisher",
            "source_role_claim": str(
                artifact.get("source_role") or "aggregator_republisher"
            ),
            "transport_provider_id": "OPEN_METEO",
            "transport_provider_claim": str(
                artifact.get("transport_provider_id") or "OPEN_METEO"
            ),
            "as_of_utc": str(artifact.get("as_of_utc") or ""),
            "content_sha256": str(artifact.get("content_sha256") or ""),
            "path": str(artifact_path),
        }

    # P1 is a data/lineage gate too: keep correction review and missing formal
    # trajectory artifacts visible in the same ledger rather than hiding them
    # inside a worker receipt.
    worker_production = artifacts["worker"].get("production", {})
    # The worker receipt contains only attempted scopes.  The dedicated
    # dry-run is the complete 45-station P1 scope; otherwise absent rows would
    # be incorrectly reported as READY_NO_LIVE.
    p1 = artifacts.get("p1", {})
    if not isinstance(p1, Mapping) or not p1.get("scopes"):
        p1 = worker_production.get("resident_p1_ingress", {}) if isinstance(worker_production, Mapping) else {}
    p1_replay = artifacts.get("p1_replay_binding", {})
    p1_replay_by_station: dict[str, list[Mapping[str, Any]]] = {}
    if isinstance(p1_replay, Mapping):
        for raw in p1_replay.get("reports") or []:
            if not isinstance(raw, Mapping):
                continue
            station = str(raw.get("station_id") or "").strip().upper()
            if station:
                p1_replay_by_station.setdefault(station, []).append(raw)
    # The current P1 audit is intentionally four local-time scopes per station
    # (180 rows).  Collapse only for the station-level gap row, preserving the
    # union of every machine blocker and the full scope count in the global
    # gate.  The older dry-run has one row per station and remains supported.
    p1_by_station: dict[str, list[Mapping[str, Any]]] = {}
    for raw in (p1.get("scopes") or p1.get("blockers") or []):
        if not isinstance(raw, Mapping):
            continue
        station = str(raw.get("station_id") or "").upper()
        if station:
            p1_by_station.setdefault(station, []).append(raw)
    for station in stations:
        rows = p1_by_station.get(station, [])
        timezone_name = next(
            (
                str(row.get("settlement_timezone") or "").strip()
                for row in rows
                if str(row.get("settlement_timezone") or "").strip()
            ),
            "",
        )
        event_date = _station_event_date(
            as_of_utc=str(p1.get("as_of_utc") or ""),
            settlement_timezone=timezone_name,
        ) if timezone_name else "*"
        diagnostic: set[str] = set()
        # Root replay is a separate lineage gate from the trajectory OOS
        # artifact. A stale P1 rooted at an older P0 remains authoritative
        # until it is replayed; it must never silently fall back to P0.
        for replay_row in p1_replay_by_station.get(station, []):
            replay = replay_row.get("replay")
            replay_state = (
                str(replay.get("state") or "")
                if isinstance(replay, Mapping)
                else ""
            )
            if replay_state in {
                "RESIDENT_P1_REPLAY_REQUIRED",
                "P1_SCOPE_AMBIGUOUS",
                "P0_ROOT_MISSING",
            }:
                diagnostic.add("p1_replay_required")
            binding = replay_row.get("formal_trajectory_binding")
            binding_state = (
                str(binding.get("state") or "")
                if isinstance(binding, Mapping)
                else ""
            )
            if binding_state in {
                "FORMAL_TRAJECTORY_BINDING_MISSING",
                "FORMAL_TRAJECTORY_BINDING_AMBIGUOUS",
                "NOT_EVALUABLE_P0_ROOT_MISSING",
            }:
                diagnostic.add("trajectory_artifact_missing")
        for row in rows:
            reason = str(row.get("reason") or "")
            if reason:
                diagnostic.add(reason)
            raw_reasons = row.get("blockers")
            if isinstance(raw_reasons, (list, tuple, set)):
                diagnostic.update(str(value) for value in raw_reasons if str(value))
            review = row.get("correction_review")
            if isinstance(review, Mapping):
                # The P1 audit binds every correction to a station day and
                # proves whether that date survived into a historical fold.
                # A historical AMD/COR that was quarantined before training
                # is not an eternal station-wide revision blocker; a date
                # that reached a fold, or whose date cannot be resolved,
                # remains hard-blocked.  The realtime P1 reader separately
                # enforces active station-day halts at decision time.
                reachable = review.get("unreviewed_reachable_event_date_count")
                if reachable is None:
                    revision_blocked = (
                        int(review.get("unreviewed_correction_count") or 0) > 0
                        or int(review.get("active_halt_count") or 0) > 0
                    )
                else:
                    historical_revision_blocked = (
                        not bool(review.get("event_date_resolution_complete", False))
                        or int(reachable or 0) > 0
                    )
                if reachable is None:
                    historical_revision_blocked = revision_blocked
                active_today = any(
                    str(item.get("event_date") or "") == event_date
                    for item in review.get("active_halts", ())
                    if isinstance(item, Mapping)
                )
                unreviewed_today = event_date in {
                    str(value)
                    for value in review.get("unreviewed_event_dates", ())
                    if str(value)
                }
                # The P1 runtime checks this exact local station day.  Keep
                # a current correction hard-blocked even when every older
                # correction was safely excluded from historical OOS folds.
                revision_blocked = (
                    historical_revision_blocked
                    or active_today
                    or unreviewed_today
                )
                if revision_blocked:
                    diagnostic.add("revision_unreviewed")
            # A missing formally promoted artifact is a data/strict-OOS gap,
            # never a signal that P1 may fall back to a hard-bound-only curve.
            artifact = row.get("artifact")
            if isinstance(artifact, Mapping):
                authority = artifact.get("authority")
                if isinstance(authority, Mapping) and all(
                    str(authority.get(key) or "").upper() == "MISSING"
                    for key in ("FORMAL_PROMOTED", "PRODUCTION_NO_LIVE_CANDIDATE")
                ):
                    diagnostic.add("trajectory_artifact_missing")
            training = row.get("training")
            if isinstance(training, Mapping) and str(training.get("state") or "").upper() == "BLOCKED_EVIDENCE":
                error = str(training.get("error") or "")
                diagnostic.add(
                    "sample_insufficient"
                    if any(token in error.lower() for token in ("fold", "sample", "oos"))
                    else (error or "sample_insufficient")
                )
        if not rows:
            diagnostic.add("data_missing")
        blockers = sorted({
            _P1_BLOCKER_MAP.get(reason, "数据缺失")
            for reason in diagnostic
        })
        observation_count = max(
            (
                int(row.get("observation_count") or 0)
                or int((row.get("source_counts") or {}).get("observation_row_count") or 0)
                for row in rows
            ),
            default=0,
        )
        truth_count = max(
            (
                int((row.get("source_counts") or {}).get("truth_row_count") or 0)
                for row in rows
            ),
            default=0,
        )
        state = "BLOCKED_FAIL_CLOSED" if blockers else "READY_NO_LIVE"
        records.append(_record(
            station_id=station, model_family="AWC", model_id="AWC_METAR_SPECI_P1",
            run_cycle="*", lead="intraday", event_date=event_date,
            source_class="official_awc", native_arrival_verified=True,
            # Observation/truth closure is independent from the later formal
            # trajectory OOS/artifact gate.  A station can have a closed AWC
            # prefix while its promoted P1 artifact is still missing.
            raw_receipt_hash_complete=observation_count > 0,
            awc_truth_closed=truth_count > 0,
            taf_revision_complete=False, train_count=0, holdout_count=0,
            state=state, blockers=blockers,
            diagnostic_blockers=sorted(diagnostic),
            evidence=[str(paths["p1"])],
            evidence_role=_evidence_role_for_event_date(
                event_date=event_date,
                as_of_utc=str(p1.get("as_of_utc") or ""),
                closed_day=truth_count > 0,
            ),
        ))

    for model in artifacts["regional"].get("models", []):
        if not isinstance(model, Mapping):
            continue
        model_id = str(model.get("model_key") or "UNKNOWN")
        source_class = str(
            model.get("source_class") or "aggregator_republisher"
        )
        reasons = set(
            str(value) for value in model.get("fail_closed_reasons") or []
        )
        base_blockers = ["arrival lineage 缺失", "样本不足"]
        if "official_native_arrival_absent" not in reasons:
            base_blockers.remove("arrival lineage 缺失")
        if model.get("observed_station_count", 0) == 0:
            base_blockers.append("数据缺失")
        scope = (
            "regional"
            if str(model.get("geographic_scope")) == "regional"
            else "global"
        )
        station_rows = {
            str(row.get("station_id") or "").upper(): row
            for row in model.get("stations") or []
            if isinstance(row, Mapping)
        }
        for station in stations:
            station_evidence = station_rows.get(station, {})
            temporal = station_evidence.get("republisher_temporal_evidence")
            if not isinstance(temporal, Mapping):
                temporal = {}
            historical_member_rows = int(
                temporal.get("historical_republisher_member_run_count") or 0
            )
            historical_contracts = int(
                temporal.get("historical_strict_oos_contract_count") or 0
            )
            historical_strict_days = int(
                temporal.get("historical_strict_oos_station_day_count") or 0
            )
            forward_member_rows = int(
                temporal.get("forward_observational_member_run_count") or 0
            )
            strict_oos_pass = int(
                station_evidence.get("strict_oos_pass") or 0
            )
            activation_count = int(
                station_evidence.get("cross_ensemble_activations") or 0
            )

            # Historical Open-Meteo rebuilds are potentially useful only when
            # their own append-only contract proves closed AWC truth and
            # strict-as-of timing.  Report an unbound historical raw as a
            # *historical gap*, rather than silently recasting it as a future
            # observation or counting it as OOS.
            if source_class == "aggregator_republisher" and historical_member_rows:
                historical_blockers = list(base_blockers)
                if historical_contracts:
                    historical_blockers = [
                        item
                        for item in historical_blockers
                        if item != "arrival lineage 缺失"
                    ]
                if strict_oos_pass and activation_count:
                    historical_blockers = [
                        item
                        for item in historical_blockers
                        if item != "样本不足"
                    ]
                historical_record = _record(
                    station_id=station,
                    model_family=scope.upper(),
                    model_id=model_id,
                    run_cycle="AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
                    lead="d0",
                    event_date=str(
                        temporal.get("historical_event_date_end") or "*"
                    ),
                    source_class=source_class,
                    native_arrival_verified=False,
                    # A retained historical republisher row is not a
                    # verifiable receipt/hash chain until its append-only
                    # historical OOS contract is present.  Do not let raw
                    # presence look stronger than its lineage.
                    raw_receipt_hash_complete=historical_contracts > 0,
                    awc_truth_closed=historical_contracts > 0,
                    taf_revision_complete=False,
                    train_count=historical_strict_days,
                    holdout_count=0,
                    state=(
                        "OOS_READY_NO_LIVE_REPUBLISHER_FALLBACK"
                        if not historical_blockers
                        else (
                            "HISTORICAL_REPUBLISHER_CONTRACT_PENDING"
                            if not historical_contracts
                            else "PENDING_STRICT_OOS_SAMPLES"
                        )
                    ),
                    blockers=historical_blockers,
                    evidence=[str(paths["regional"])],
                    evidence_role="historical_strict_oos",
                )
                historical_record.update({
                    "historical_oos_calibration_fallback": True,
                    "historical_republisher_contract_verified": (
                        historical_contracts > 0
                    ),
                    "historical_strict_oos_member_run_count": int(
                        temporal.get(
                            "historical_strict_oos_member_run_count"
                        )
                        or 0
                    ),
                    "historical_strict_oos_activation_count": activation_count,
                    "native_arrival_index_eligible": False,
                    "native_p0_gate_eligible": False,
                    "formal_p0_eligible": False,
                    "republisher_no_live_fallback_only": True,
                })
                records.append(historical_record)

            # A forward Open-Meteo packet remains an observational candidate
            # even when the target date is now in the past.  It has no
            # immutable historical OOS contract, so it cannot contribute a
            # train/holdout/TAF sample or substitute for formal promotion.
            if source_class == "aggregator_republisher" and forward_member_rows:
                forward_record = _record(
                    station_id=station,
                    model_family=scope.upper(),
                    model_id=model_id,
                    run_cycle="AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
                    lead="d0",
                    event_date=str(
                        temporal.get("forward_event_date_end") or "*"
                    ),
                    source_class=source_class,
                    native_arrival_verified=False,
                    raw_receipt_hash_complete=True,
                    awc_truth_closed=False,
                    taf_revision_complete=False,
                    train_count=0,
                    holdout_count=0,
                    state="OBSERVATIONAL_CANDIDATE_NOT_OOS",
                    blockers=base_blockers,
                    evidence=[str(paths["regional"])],
                    evidence_role="observational_candidate_not_oos",
                )
                forward_record.update({
                    "historical_oos_calibration_fallback": False,
                    "forward_packet_evidence_role": (
                        "observational_candidate_not_oos"
                    ),
                    "native_arrival_index_eligible": False,
                    "native_p0_gate_eligible": False,
                    "formal_p0_eligible": False,
                    "republisher_no_live_fallback_activation_required": True,
                })
                records.append(forward_record)

            # No retained model row exists for this station/model.  Keep the
            # planned candidate visible without fabricating an event date or
            # turning the absence itself into an OOS sample.
            if not historical_member_rows and not forward_member_rows:
                regional_record = _record(
                    station_id=station,
                    model_family=scope.upper(),
                    model_id=model_id,
                    run_cycle="*",
                    lead="*",
                    event_date="*",
                    source_class=source_class,
                    native_arrival_verified=False,
                    raw_receipt_hash_complete=False,
                    awc_truth_closed=False,
                    taf_revision_complete=False,
                    train_count=0,
                    holdout_count=0,
                    state=str(model.get("readiness") or "candidate_only"),
                    blockers=base_blockers,
                    evidence=[str(paths["regional"])],
                    evidence_role="observational_candidate_not_oos",
                )
                if source_class == "aggregator_republisher":
                    regional_record.update({
                        "historical_oos_calibration_fallback": True,
                        "native_arrival_index_eligible": False,
                        "native_p0_gate_eligible": False,
                        "formal_p0_eligible": False,
                    })
                records.append(regional_record)

    icon_readiness_rows = {
        str(row.get("station_id") or "").upper(): row
        for row in (artifacts["icon"].get("readiness") or [])
        if isinstance(row, Mapping)
    }
    icon_arrival = artifacts.get("icon_arrival", {})
    icon_arrival_index_eligible = bool(icon_arrival.get("native_arrival_index_eligible") is True)
    icon_arrival_verified_count = int(icon_arrival.get("verified_object_count") or 0)
    icon_arrival_unresolved_count = int(icon_arrival.get("unresolved_receipt_count") or 0)
    for station in stations:
        icon_row = icon_readiness_rows.get(station, {})
        icon_event_date = str(icon_row.get("event_date") or "2026-08-01")
        icon_raw_blockers = [
            str(value)
            for value in icon_row.get("blockers") or []
            if str(value)
        ]
        icon_blockers = [
            value for value in icon_raw_blockers if value in BLOCKER_TYPES
        ]
        if not icon_blockers:
            icon_blockers = ["arrival lineage 缺失", "truth 缺失", "样本不足"]
        if icon_arrival and not icon_arrival_index_eligible and "arrival lineage 缺失" not in icon_blockers:
            icon_blockers.append("arrival lineage 缺失")
        records.append(_record(
            station_id=station, model_family="ICON", model_id="DWD_ICON_GLOBAL_EPS_OFFICIAL",
            run_cycle="00Z", lead="d0-d2", event_date=icon_event_date,
            # The retained DWD cohort is official raw evidence, but its
            # prospective publication/receive lineage is not verified and it
            # is explicitly not arrival-index eligible yet.
            source_class="official_native", native_arrival_verified=icon_arrival_index_eligible,
            raw_receipt_hash_complete=bool(icon_arrival_verified_count or icon_row), awc_truth_closed=False, taf_revision_complete=False,
            train_count=int(icon_row.get("training_case_count") or 0),
            holdout_count=int(icon_row.get("strict_holdout_case_count") or 0),
            state=str(icon_row.get("state") or artifacts["icon"].get("state") or "OFFICIAL_NATIVE_OBSERVED_STRICT_OOS_PENDING"),
            blockers=icon_blockers, diagnostic_blockers=icon_raw_blockers,
            evidence=[str(paths["icon"])],
            evidence_role=_evidence_role_for_event_date(
                event_date=icon_event_date,
                as_of_utc=str(artifacts["icon"].get("as_of_utc") or ""),
                closed_day=False,
            ),
        ))

    policy = artifacts["policy"].get("input_counts", {})
    policy_checks = artifacts["policy"].get("checks", {})
    policy_historical_binding = artifacts["policy"].get(
        "historical_utility_binding_inventory", {}
    )
    policy_historical_stages = policy_historical_binding.get("stages", {})
    policy_historical_train_count = sum(
        int(row.get("closed_historical_fact_count") or 0)
        for row in policy_historical_stages.values()
        if isinstance(row, Mapping)
    )
    policy_historical_holdout_count = sum(
        int(row.get("closed_official_awc_label_count") or 0)
        for row in policy_historical_stages.values()
        if isinstance(row, Mapping)
    )
    policy_contract_pointer = artifacts["policy"].get("utility_contract_pointer")
    policy_contract_pointer_source = "scorecard_artifact"
    policy_contract_present = isinstance(policy_contract_pointer, Mapping)
    if not policy_contract_present:
        # Older append-only scorecards predate the explicit pointer.  Keep the
        # ledger read-only and make the contract lineage visible by pointing
        # to the current immutable schema; this does not upgrade old facts or
        # imply that a utility binding exists.
        policy_contract_pointer = build_station_policy_utility_contract_pointer()
        policy_contract_pointer_source = "current_code_schema_only"
    for station in stations:
        policy_record = _record(
            station_id=station, model_family="STATION_POLICY", model_id="TARGET_RESERVE_CADENCE",
            event_date="*", source_class="target_owned_utility",
            native_arrival_verified=False, raw_receipt_hash_complete=True,
            awc_truth_closed=False, taf_revision_complete=False,
            # Count only explicitly historical strict-OOS facts here.  Closed
            # `PRODUCTION_NO_LIVE_FORWARD` rows remain observational and are
            # exposed below, never smuggled into train/holdout totals.
            train_count=policy_historical_train_count,
            holdout_count=policy_historical_holdout_count,
            state="BLOCKED_EVIDENCE",
            blockers=[
                "utility 证据绑定缺失"
                if policy_contract_present
                else "utility 合同缺失"
            ],
            evidence=[str(paths["policy"])],
            # Direct fill/settlement utility is a formal/live-promotion
            # proof.  Retain the gap, but do not turn it into a hard
            # production_no_live gate for already materialized historical
            # P0/P0-M/P1 evidence.
            strict_oos_gate_applies=False,
        )
        policy_record["utility_contract_pointer"] = policy_contract_pointer
        policy_record["station_policy_utility_contract_pointer_source"] = policy_contract_pointer_source
        policy_record["utility_metric_binding_state"] = str(
            policy_checks.get("policy_utility_contract_binding_state") or "UNBOUND"
        )
        policy_record["utility_score_rows_emitted"] = bool(
            policy_checks.get("policy_utility_score_rows_emitted") is True
        )
        policy_record["historical_utility_binding_state"] = str(
            policy_historical_binding.get("state") or "UNAVAILABLE"
        )
        policy_observational_count = sum(
            int(row.get("observational_candidate_not_oos_count") or 0)
            for row in policy_historical_stages.values()
            if isinstance(row, Mapping)
        )
        policy_record["observational_candidate_not_oos_count"] = (
            policy_observational_count
            if policy_historical_stages
            else int(policy.get("observational_candidate_not_oos_count") or 0)
        )
        records.append(policy_record)

    counts = {kind: sum(kind in row["blockers"] for row in records) for kind in BLOCKER_TYPES}
    hard_gate_counts = {
        kind: sum(
            kind in row["blockers"]
            and bool(row.get("strict_oos_gate_applies", True))
            for row in records
        )
        for kind in BLOCKER_TYPES
    }
    observational_record_count = sum(
        row.get("evidence_role") == "observational_candidate_not_oos"
        for row in records
    )
    state_counts: dict[str, int] = {}
    for row in records:
        state_counts[row["state"]] = state_counts.get(row["state"], 0) + 1
    # Artifact collection is intentionally asynchronous.  Report both the
    # most recent P1 decision cut-off used for local station-day records and
    # the individual source clocks; do not mislabel an old worker completion
    # as the entire 45-station ledger's current evidence time.
    p1_as_of_utc = str(p1.get("as_of_utc") or "").strip()
    worker_completed_at = str(artifacts["worker"].get("completed_at") or "").strip()
    aifs_as_of_utc = str(artifacts["aifs"].get("as_of_utc") or "").strip()
    aifs_republisher_as_of_utc = str(
        artifacts.get("aifs_republisher", {}).get("as_of_utc") or ""
    ).strip()
    icon_republisher_as_of_utc = str(
        artifacts.get("icon_republisher", {}).get("as_of_utc") or ""
    ).strip()
    republisher_fallback_binding_as_of_utc = str(
        artifacts["republisher_fallback_binding"].get("as_of_utc") or ""
    ).strip()
    native_linkage_as_of_utc = str(
        artifacts["native_linkage"].get("as_of_utc") or ""
    ).strip()
    native_repair_as_of_utc = str(
        artifacts["native_repair"].get("as_of_utc") or ""
    ).strip()
    republisher_fallback_binding_summary = _republisher_fallback_binding_summary(
        artifacts["republisher_fallback_binding"],
        binding_path=paths["republisher_fallback_binding"],
    )
    identity = {
        "schema": SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z"),
        "as_of_utc": p1_as_of_utc or worker_completed_at or aifs_as_of_utc,
        "source_as_of_utc": {
            "p1": p1_as_of_utc,
            "worker": worker_completed_at,
            "aifs": aifs_as_of_utc,
            "aifs_republisher": aifs_republisher_as_of_utc,
            "icon_republisher": icon_republisher_as_of_utc,
            "republisher_fallback_binding": republisher_fallback_binding_as_of_utc,
            "native_activation_linkage": native_linkage_as_of_utc,
            "native_activation_repair": native_repair_as_of_utc,
        },
        "station_count": len(stations),
        "station_ids": list(stations),
        "blocker_types": list(BLOCKER_TYPES),
        "oos_policy": {
            "future_event_data_evidence_role": "observational_candidate_not_oos",
            "future_event_data_is_oos": False,
            "future_event_data_counts_as_strict_oos": False,
            "future_event_data_blocks_production_no_live": False,
            # A prospective row is not itself a hard gate.  Formal promotion
            # is evaluated from the historical strict-as-of cohort; a future
            # row simply cannot substitute for that cohort.
            "future_event_data_blocks_formal_promotion": False,
            "future_event_data_can_substitute_for_formal_promotion": False,
            "utility_execution_evidence_blocks_production_no_live": False,
            "historical_closed_awc_truth_required_for_strict_oos": True,
            "hard_gate_blocker_counts_exclude_observational_candidates": True,
        },
        # Expose the immutable utility-contract lineage at the ledger root as
        # well as in ``global_gates`` and the station-policy records.  Older
        # scorecards may lack the field; the fallback above points only to the
        # current UNBOUND schema and never upgrades historical evidence.
        "station_policy_utility_contract_pointer": policy_contract_pointer,
        "station_policy_utility_contract_pointer_source": policy_contract_pointer_source,
        "evidence_snapshot_manifest": {
            "schema": str(artifacts["snapshot_manifest"].get("schema") or ""),
            "path": str(paths["snapshot_manifest"].resolve()),
            "file_sha256": _sha(paths["snapshot_manifest"]),
            "content_sha256": str(artifacts["snapshot_manifest"].get("content_sha256") or ""),
            "scope_fingerprint": str(artifacts["snapshot_manifest"].get("scope_fingerprint") or ""),
        },
        # These per-input counters make it auditable that the retained
        # Open-Meteo deterministic curves were included, while keeping their
        # republisher/no-live authority visible at the ledger root.
        "deterministic_republisher_inputs": deterministic_republisher_meta,
        # Keep the separately generated pointer in the ledger as lineage
        # only.  Its binding rows remain in the immutable input artifact and
        # are not duplicated as station/OOS facts here.
        "republisher_fallback_binding": republisher_fallback_binding_summary,
        "records": records,
        "summary": {
            "record_count": len(records),
            "blocker_counts": counts,
            "hard_gate_blocker_counts": hard_gate_counts,
            "observational_candidate_not_oos_record_count": observational_record_count,
            "state_counts": dict(sorted(state_counts.items())),
            "formal_p0_eligible": False,
            "production_no_live": True,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
        "global_gates": {
            "evidence_snapshot_manifest": {
                "schema": str(artifacts["snapshot_manifest"].get("schema") or ""),
                "content_sha256": str(artifacts["snapshot_manifest"].get("content_sha256") or ""),
                "scope_fingerprint": str(artifacts["snapshot_manifest"].get("scope_fingerprint") or ""),
                "state": "FROZEN_READ_ONLY",
            },
            "oos_policy": {
                "future_event_data_evidence_role": "observational_candidate_not_oos",
                "future_event_data_is_oos": False,
                "future_event_data_counts_as_strict_oos": False,
                "future_event_data_blocks_production_no_live": False,
                "future_event_data_blocks_formal_promotion": False,
                "future_event_data_can_substitute_for_formal_promotion": False,
                "utility_execution_evidence_blocks_production_no_live": False,
                "historical_closed_awc_truth_required_for_strict_oos": True,
            },
            "taf_qualified_scope_count_at_24": int(
                (artifacts["taf"].get("totals") or {}).get("qualified_scope_count_at_24")
                or sum(
                    int(row.get("sample_count") or 0) >= 24
                    and not list(row.get("blockers") or [])
                    for row in artifacts["taf"].get("entries", [])
                    if isinstance(row, Mapping)
                )
            ),
            "taf_pointer_audit_state": artifacts["taf"].get("state"),
            "taf_revision_quarantine": {
                "state": artifacts["taf_revision"].get("state"),
                "station_day_count": int(artifacts["taf_revision"].get("station_day_count") or 0),
                "correction_count": int(artifacts["taf_revision"].get("correction_count") or 0),
                "awc_truth_closed_station_day_count": int(artifacts["taf_revision"].get("awc_truth_closed_station_day_count") or 0),
                "awc_truth_missing_station_day_count": int(artifacts["taf_revision"].get("awc_truth_missing_station_day_count") or 0),
                "content_sha256": str(artifacts["taf_revision"].get("content_sha256") or ""),
                "forward_revision_chain": {
                    "state": forward_chain_audit.get("state"),
                    "station_day_count": int(
                        forward_chain_audit.get("station_day_count") or 0
                    ),
                    "linear_chain_station_day_count": int(
                        forward_chain_audit.get(
                            "linear_chain_station_day_count"
                        ) or 0
                    ),
                    "ambiguous_station_day_count": int(
                        forward_chain_audit.get(
                            "ambiguous_station_day_count"
                        ) or 0
                    ),
                    "content_sha256": str(
                        forward_chain_audit.get("content_sha256") or ""
                    ),
                },
            },
            "taf_max_exact_join_sample_count": max(
                [
                    int(row.get("exact_join_sample_count") or row.get("sample_count") or 0)
                    for row in artifacts["taf"].get("entries", [])
                    if isinstance(row, Mapping)
                ]
                or [0]
            ),
            "aifs_fact_count": int(artifacts["aifs"].get("fact_count") or 0),
            "aifs_readiness": artifacts["aifs"].get("readiness", {}),
            "aifs_provider_authority": {
                "state": artifacts["aifs_readiness"].get("state"),
                "blockers": artifacts["aifs_readiness"].get("blockers") or [],
                "provider_binding": artifacts["aifs_readiness"].get("provider_binding") or {},
                "content_sha256": str(artifacts["aifs_readiness"].get("content_sha256") or ""),
            },
            "icon_readiness_state": artifacts["icon"].get("state"),
            "icon_strict_oos": {
                "state": artifacts["icon"].get("state"),
                "strict_oos_state": artifacts["icon"].get("strict_oos_state"),
                "fact_count": int(artifacts["icon"].get("fact_count") or 0),
                "candidate_count": int(artifacts["icon"].get("candidate_count") or 0),
                "formal_p0_eligible": bool(artifacts["icon"].get("formal_p0_eligible") is True),
                "source_authority": artifacts["icon"].get("source_authority") or {},
                "native_arrival_index": {
                    "state": icon_arrival.get("state"),
                    "native_arrival_index_eligible": icon_arrival_index_eligible,
                    "verified_object_count": icon_arrival_verified_count,
                    "unresolved_receipt_count": icon_arrival_unresolved_count,
                    "content_sha256": str(icon_arrival.get("content_sha256") or ""),
                },
            },
            "open_meteo_deterministic_republisher": deterministic_republisher_meta,
            # Supplementary lineage pointer; never a native/P1/formal/live
            # authority gate and never a production write dependency.
            "republisher_fallback_binding": republisher_fallback_binding_summary,
            "native_activation_linkage": {
                "as_of_utc": native_linkage_as_of_utc,
                "content_sha256": str(
                    artifacts["native_linkage"].get("content_sha256") or ""
                ),
                "state": artifacts["native_linkage"].get("audit_state"),
                "activation_count": int(artifacts["native_linkage"].get("activation_count") or 0),
                "consumable_exact_oos_pass_activation_count": int(
                    artifacts["native_linkage"].get("consumable_exact_oos_pass_activation_count") or 0
                ),
                "unconsumable_activation_count": int(
                    artifacts["native_linkage"].get("unconsumable_activation_count") or 0
                ),
            },
            "native_activation_repair_plan": {
                "as_of_utc": native_repair_as_of_utc,
                "content_sha256": str(artifacts["native_repair"].get("content_sha256") or ""),
                "candidate_action_summary": artifacts["native_repair"].get("candidate_action_summary") or {},
                "activation_action_summary": artifacts["native_repair"].get("activation_action_summary") or {},
                "artifact_pointer_count": int(artifacts["native_repair"].get("artifact_pointer_count") or 0),
                "repair_ready_count": int(artifacts["native_repair"].get("repair_ready_count") or 0),
                "safe_write_state": str(artifacts["native_repair"].get("safe_write_state") or "UNKNOWN"),
                "automatic_write_authorized": bool(artifacts["native_repair"].get("automatic_write_authorized") is True),
                "boundary": artifacts["native_repair"].get("boundary") or {},
            },
            "p1_scope_count": int(artifacts["p1"].get("scope_count") or 0),
            "p1_eligible_scope_count": int(
                artifacts["p1"].get("formal_eligible_scope_count")
                or artifacts["p1"].get("eligible_scope_count")
                or 0
            ),
            "p1_replay_binding": {
                "state": "READ_ONLY_AUDIT"
                if isinstance(p1_replay, Mapping)
                and p1_replay.get("schema") == "resident_p1_replay_binding_audit_v1"
                else "MISSING",
                "schema": p1_replay.get("schema")
                if isinstance(p1_replay, Mapping)
                else None,
                "content_sha256": str(
                    p1_replay.get("content_sha256") or ""
                    if isinstance(p1_replay, Mapping)
                    else ""
                ),
                "resident_p1_replay_required_count": int(
                    (p1_replay.get("summary") or {}).get(
                        "resident_p1_replay_required_count"
                    )
                    or 0
                    if isinstance(p1_replay, Mapping)
                    else 0
                ),
                "formal_replay_binding_present_count": int(
                    (p1_replay.get("summary") or {}).get(
                        "formal_replay_binding_present_count"
                    )
                    or 0
                    if isinstance(p1_replay, Mapping)
                    else 0
                ),
                "boundary": p1_replay.get("boundary") or {}
                if isinstance(p1_replay, Mapping)
                else {},
            },
            "p1_repair_plan": {
                "content_sha256": str(artifacts["p1_repair"].get("content_sha256") or ""),
                "scope_count": int(artifacts["p1_repair"].get("scope_count") or 0),
                "formal_eligible_scope_count": int(artifacts["p1_repair"].get("formal_eligible_scope_count") or 0),
                "candidate_pointer_count": int(artifacts["p1_repair"].get("candidate_pointer_count") or 0),
                "usable_candidate_pointer_count": int(artifacts["p1_repair"].get("usable_candidate_pointer_count") or 0),
                "action_summary": artifacts["p1_repair"].get("action_summary") or {},
                "blocker_summary": artifacts["p1_repair"].get("blocker_summary") or {},
                "safe_write_state": str(artifacts["p1_repair"].get("safe_write_state") or "UNKNOWN"),
                "automatic_write_authorized": bool(artifacts["p1_repair"].get("automatic_write_authorized") is True),
                "boundary": artifacts["p1_repair"].get("boundary") or {},
            },
            "vertical_chain": {
                "status": artifacts["vertical_chain"].get("status"),
                "formal_no_live_chain_scopes": int((artifacts["vertical_chain"].get("counts") or {}).get("formal_no_live_chain_scopes") or 0),
                "native_p0_station_count": int((artifacts["vertical_chain"].get("45station_expansion") or {}).get("native_p0_station_count") or 0),
                "awc_p1_station_count": int((artifacts["vertical_chain"].get("45station_expansion") or {}).get("awc_p1_station_count") or 0),
                "formal_three_stage_station_count": int((artifacts["vertical_chain"].get("45station_expansion") or {}).get("formal_three_stage_station_count") or 0),
                "content_sha256": str(artifacts["vertical_chain"].get("content_sha256") or ""),
            },
            "station_policy_score_rows": (artifacts["policy"].get("input_counts") or {}).get("score_rows", {}),
            "station_policy_utility_contract_pointer": policy_contract_pointer,
            "station_policy_utility_contract_pointer_source": policy_contract_pointer_source,
            # Schema presence is distinct from a bound metric.  Keep the
            # latter blocked until real post-label utility evidence exists.
            "station_policy_utility_contracts_defined": True,
            "station_policy_utility_contract_state": "DEFINED_UNBOUND",
            "station_policy_utility_contract_score_emission": "FORBIDDEN",
            # Keep policy evidence surfaces visible in the cross-domain
            # ledger without treating a quote, intent, or simulated receipt
            # as realised utility.  The scorecard remains the authority for
            # the explicit UNBOUND/direct-fill blockers.
            "station_policy_utility_evidence_inventory": artifacts["policy"].get(
                "utility_evidence_inventory", {}
            ),
            # The immutable contract schema above is defined; this separate
            # metric flag describes whether any post-label utility rows are
            # actually bound.  Do not overwrite the schema-state field with
            # this false value, or the ledger erases the useful distinction
            # between a defined contract and unavailable real evidence.
            "station_policy_utility_metric_contracts_defined": bool(
                (artifacts["policy"].get("checks") or {}).get(
                    "policy_utility_metric_contracts_defined"
                )
            ),
            "station_policy_utility_metric_binding_complete": bool(
                (artifacts["policy"].get("checks") or {}).get(
                    "policy_utility_metric_binding_complete"
                )
            ),
            "station_policy_utility_score_rows_emitted": bool(
                (artifacts["policy"].get("checks") or {}).get(
                    "policy_utility_score_rows_emitted"
                )
            ),
            "station_policy_promotion_permitted": bool(
                (artifacts["policy"].get("checks") or {}).get(
                    "promotion_permitted"
                )
            ),
            "delta_input_gaps": artifacts["delta"].get("input_gaps") or [],
            "delta_promotion_status": str(
                (artifacts["delta"].get("evidence") or {}).get("status")
                or artifacts["delta"].get("status")
                or "UNKNOWN"
            ),
            "delta_missing_requirements": (artifacts["delta"].get("rkpk") or {}).get("missing_requirements", []),
            # ``delta`` is the older cash-phase/design candidate receipt.  Its
            # ``evidence.global_blockers`` (for example
            # ``registry_row_invalid``) describes the candidate policy
            # contract, not whether the retained forward observations are
            # usable.  Treating that field as a source blocker falsely makes
            # an already-closed source lane look broken.  The forward
            # observation artifact is the authoritative source-ingress gate;
            # preserve the legacy design blockers under a separate key for
            # audit context.
            "delta_source_blockers": list(artifacts["delta_observations"].get("source_blockers") or []),
            "delta_design_global_blockers": list((artifacts["delta"].get("evidence") or {}).get("global_blockers") or []),
            "delta_forward_observation_counts": artifacts["delta_observations"].get("counts", {}),
            "delta_forward_observation_source_blockers": artifacts["delta_observations"].get("source_blockers", []),
            "delta_forward_observation_state": artifacts["delta_observations"].get("state"),
            "delta_natural_event_deficits": [
                str(item)
                for item in (artifacts["delta"].get("rkpk") or {}).get("missing_requirements", [])
                if str(item).startswith("phase_") and "unique_event_deficit" in str(item)
            ],
        },
        "input_artifacts": {
            key: (
                {
                    "path": str(paths[key]),
                    "sha256": _sha(paths[key]),
                }
                if paths[key].exists()
                else {
                    "path": str(paths[key]),
                    "sha256": None,
                    "state": "MISSING_OPTIONAL_AUDIT"
                    if key in {"p1_replay_binding", "republisher_fallback_binding"}
                    else "MISSING",
                }
            )
            for key in paths
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for key, path in DEFAULTS.items():
        parser.add_argument(f"--{key}", type=Path, default=path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build(args)
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"schema": SCHEMA, "state": "ACCUMULATING", "record_count": len(result["records"]), "output": str(output), "content_sha256": result["content_sha256"]}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
