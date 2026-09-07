"""Fail-closed materialization of target probability checkpoints.

This module is the only boundary that turns a pure probability checkpoint
into a committed target-SSOT row.  It accepts no raw weather payload and has
no network, model-training, market, or gateway dependency.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from datetime import datetime
from typing import Any, Mapping, Sequence

from .atomic_ledger import ledger_span, note_ledger_span
from .contracts import (
    ContractError,
    ProbabilityEnvelope,
    content_sha256,
    timestamp_lt,
    timestamp_lte,
)
from .center_width_calibration import (
    apply_center_width_calibration,
    bucket_centers_from_intervals,
)
from .center_width_store import EDGE_WIDTH_C, taf_direction_for_gap
from .cross_consensus import apply_band_reliability
from .et_history import assert_event_day_started
from .metar_four_layer import (
    SERVE_ENABLED as FOUR_LAYER_SERVE_ENABLED,
    compile_four_layer_posterior,
)
from .regional_path_prior import (
    apply_regional_path_prior,
)
from .metar_ceiling import (
    apply_ceiling as apply_metar_ceiling,
    current_gap_c as ceiling_current_gap_c,
    latest_temperature_c as ceiling_latest_temperature_c,
)
from .settlement_time import settlement_day_window
from .ssot import production_ssot_schema_status
from .resource_bounds import assert_connection_target_capacity
from .four_layer_shadow import SHADOW_LINEAGE_KEYS
from .metar_trajectory import (
    METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION,
    METAR_TRAJECTORY_CONDITIONAL_PATH_METHOD_VERSION,
    METAR_TRAJECTORY_PHYSICAL_ABSOLUTE_TMAX_METHOD_VERSION,
    P1_SOFT_LR_SUSPENDED_LIKELIHOOD_MODE,
    SOFT_LIKELIHOOD_POLICY_SUSPENDED,
    SOFT_PATH_SOURCE_LANES,
    MetarTrajectoryLikelihoodArtifact,
    MetarTrajectoryObservation,
    MetarTrajectoryUpdateProof,
    validate_metar_trajectory_update_proof,
)
from .taf_revision import (
    TafRevisionArtifact,
    TafRevisionProof,
    validate_taf_revision_proof,
)


MATERIALIZATION_SCHEMA = "bayes_basket_probability_materialization_v1"
# The smallest claim a served bucket may carry unless an observation has
# structurally excluded it (2026-08-26 ruling; see the floor layer in
# ``_posterior`` for the LFPB case and the measured miss frequency).
PROBABILITY_MODEL_ZERO_FLOOR = 1e-3
_LINEAGE_FORBIDDEN_KEYS = frozenset(
    {
        "temperature",
        "metar",
        "speci",
        "taf",
        "nwp",
        "regime",
        "direction",
        "center_bucket",
    }
)

_NATIVE_FORWARD_CORE_KEYS = (
    "run_cycle_scope",
    "run_cycle_by_system_json",
    "run_init_utc_by_system_json",
    "archive_query_at_utc_by_system_json",
    "native_forward_availability_evidence_json",
    "native_forward_lineage_sha256",
)
_NATIVE_FORWARD_TAF_KEYS = (
    "taf_nwp_live_compatibility_json",
    "taf_nwp_live_compatibility_fingerprint_sha256",
)
_NATIVE_FORWARD_SCOPES = frozenset(
    {"SAME_NATIVE_CYCLE", "PER_SYSTEM_NATIVE_CYCLE"}
)
_NATIVE_RUN_CYCLES = frozenset({"00Z", "06Z", "12Z", "18Z"})
_MIXED_NATIVE_CYCLES = "MIXED_NATIVE_CYCLES"


def _anchor_bucket_for_temperature(
    value: float,
    *,
    root_lineage: Mapping[str, Any],
    bucket_ordinals: Mapping[str, int],
) -> str | None:
    """Map a stage's point estimate onto its bucket, or None if ungeometried.

    Returns None rather than guessing when the root carries no bucket geometry
    (non-native roots do not publish intervals).  Support selection then falls
    back to its own center resolution, which is the pre-existing behaviour.
    """

    raw = root_lineage.get("bucket_intervals_c_json")
    if not raw:
        return None
    try:
        intervals = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(intervals, Mapping) or set(intervals) != set(bucket_ordinals):
        return None
    try:
        target = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(target):
        return None
    matches = [
        str(bucket)
        for bucket, bounds in intervals.items()
        if (
            isinstance(bounds, Sequence)
            and not isinstance(bounds, (str, bytes, bytearray))
            and len(bounds) == 2
            and (bounds[0] is None or target >= float(bounds[0]))
            and (bounds[1] is None or target < float(bounds[1]))
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _bucket_centers_c(
    lineage: Mapping[str, Any],
    *,
    bucket_ordinals: Mapping[str, int],
) -> tuple[list[str], list[float]] | None:
    """The stage's bucket axis in degrees, or None when it has none.

    Same tolerance as ``_anchor_bucket_for_temperature``: a root that
    publishes no interval geometry is not an error, it is a root the
    temperature-axis corrections do not apply to.
    """

    raw = lineage.get("bucket_intervals_c_json")
    if not raw:
        return None
    try:
        intervals = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(intervals, Mapping) or set(intervals) != set(
        bucket_ordinals
    ):
        return None
    ordered = sorted(bucket_ordinals, key=lambda key: bucket_ordinals[key])
    try:
        centers = bucket_centers_from_intervals(
            intervals, ordered_keys=ordered, edge_width_c=EDGE_WIDTH_C
        )
    except (ValueError, TypeError):
        return None
    if any(b <= a for a, b in zip(centers, centers[1:])):
        return None
    return ordered, centers


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"{name}_invalid")
    return result


def _timestamp(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{name}_timezone_required")
    return result


def _lineage_json_mapping(name: str, value: Any) -> dict[str, Any]:
    """Decode a content-addressed lineage JSON object without coercion."""

    try:
        decoded = json.loads(_text(name, value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError(f"{name}_invalid_json") from exc
    if not isinstance(decoded, Mapping):
        raise ContractError(f"{name}_invalid_json")
    return {str(key): item for key, item in decoded.items()}


def _lineage_json_identity(name: str, value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a JSON object with a valid embedded content hash."""

    content = value.get("content_sha256")
    _sha(f"{name}_content_sha256", content)
    identity = {key: item for key, item in value.items() if key != "content_sha256"}
    if str(content).lower() != content_sha256(identity):
        raise ContractError(f"{name}_content_sha256_mismatch")
    return identity


def _named_cycle_riders(root_lineage: Mapping[str, Any]) -> set[str]:
    """Systems the root itself declared as riding another run cycle.

    Part of the root's content hash, so it cannot be added after the fact to
    excuse a drift the compiler never accepted.  An unreadable list yields
    nothing, which fails the caller closed rather than open.
    """

    try:
        declared = json.loads(
            str(root_lineage.get("activation_cycle_riders_json") or "[]")
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return set()
    if not isinstance(declared, Sequence) or isinstance(
        declared, (str, bytes, bytearray)
    ):
        return set()
    return {str(system_id) for system_id in declared}


def _embedded_capture_cycle(lineage: Mapping[str, Any]) -> str:
    """The capture cycle recorded inside the compatibility document.

    ``native_forward_run_cycle`` is a P0-only key; the document that carries
    the same value is inherited by every descendant and is bound by its own
    hash, so reading it back is not a second source of truth but the same one
    reached another way.  Silence on anything unreadable: the caller's
    fallback is the pre-existing behaviour, and a parse failure here must not
    turn into a refusal that was not there before.
    """

    raw = lineage.get("taf_nwp_live_compatibility_json")
    if not raw:
        return ""
    try:
        document = json.loads(str(raw))
        native = document.get("native_forward_lineage")
        if not isinstance(native, Mapping):
            return ""
        return str(native.get("run_cycle") or "").strip()
    except (TypeError, ValueError):
        return ""


def _native_forward_lineage(
    stage: str,
    *,
    root: ProbabilityEnvelope,
    predecessor: ProbabilityEnvelope,
) -> dict[str, str]:
    """Validate and return native-forward fields owned by the P0 root.

    A legacy deterministic/same-cycle envelope can omit all native-forward
    fields. Once any native-forward field is present, the materialization
    boundary requires the complete scope and evidence set. This keeps old
    same-cycle fixtures readable while making a mixed-cycle omission or
    mutation impossible to propagate into P0-M/P1.
    """

    root_lineage = root.lineage
    predecessor_lineage = predecessor.lineage

    def _has_core(lineage: Mapping[str, str]) -> bool:
        if any(key in lineage for key in _NATIVE_FORWARD_CORE_KEYS):
            return True
        compatibility = lineage.get("taf_nwp_live_compatibility_json")
        if compatibility is None:
            return False
        try:
            decoded = _lineage_json_mapping(
                "taf_nwp_live_compatibility_json", compatibility
            )
        except ContractError:
            return False
        return isinstance(decoded.get("native_forward_lineage"), Mapping)

    root_has_core = _has_core(root_lineage)
    predecessor_has_core = _has_core(predecessor_lineage)
    if not root_has_core:
        if predecessor_has_core:
            raise ContractError(f"{stage}_native_forward_root_lineage_missing")
        return {}
    # Open-Meteo/other republisher envelopes deliberately use an explicit
    # non-native scope. They remain on the legacy compatibility path and must
    # never be upgraded into official native evidence by this boundary.
    if root_lineage.get("run_cycle_scope") == "AGGREGATOR_NATIVE_CYCLE_UNKNOWN":
        return {}
    if not predecessor_has_core:
        raise ContractError(f"{stage}_native_forward_predecessor_lineage_missing")

    for key in _NATIVE_FORWARD_CORE_KEYS:
        if key not in root_lineage or not str(root_lineage.get(key) or "").strip():
            raise ContractError(f"{stage}_native_forward_lineage_missing:{key}")

    scope = _text("run_cycle_scope", root_lineage.get("run_cycle_scope"))
    # Two cycles live in a P0 root and they are not the same question.
    # ``run_cycle`` names the calibration scope the activation was fitted
    # under -- the resident TAF lane matches artifacts on it, so it stays.
    # ``native_forward_run_cycle`` names what the capture actually carried,
    # which is the only one this block may check the per-system map against.
    # They diverge whenever the activation match tolerates a cycle rider, and
    # reading the fitted one here rejected every such root.  Older envelopes
    # predate the split and carry only the one value, which was correct for
    # them precisely because nothing rode.
    # The capture cycle, from wherever this root still records it.  A P0 root
    # writes it at the top level.  A descendant does not inherit that key --
    # the inheritance block below says why -- but it does inherit the
    # compatibility document, and the P0 compiler wrote the same cycle inside
    # it, under the hash the document is bound by.  Measured: present on
    # 400 of 400 P0-M rows sampled from 2026-08-28, and equal to the top-level
    # value wherever both exist.
    stated_capture_cycle = str(
        root_lineage.get("native_forward_run_cycle")
        or _embedded_capture_cycle(root_lineage)
        or ""
    ).strip()
    run_cycle = _text(
        "run_cycle",
        stated_capture_cycle or root_lineage.get("run_cycle"),
    )
    if scope not in _NATIVE_FORWARD_SCOPES:
        raise ContractError(f"{stage}_native_forward_run_cycle_scope_invalid")
    # Why the recovery above matters.  Before it, a P1 rooted on a P0-M read
    # no capture cycle and fell back to ``run_cycle`` -- the calibration scope
    # an activation was fitted under -- and compared it against a question
    # about what the archive delivered.  That is the same category error the
    # inheritance block below already names, left standing in this test: it
    # refused 86 P0-M roots across 27 stations, KAUS on 2026-09-02 among them,
    # each a P1 blocked on a disagreement that was not one.  The compiler
    # split those two cycles apart on 2026-08-16 and the older P0-M rows
    # happen to carry the capture cycle in the ``run_cycle`` key, which is
    # why only the newer ones broke: 89 of 277 sampled PER_SYSTEM P0-M rows
    # read 00Z there where 182 read MIXED_NATIVE_CYCLES.
    if stated_capture_cycle:
        mixed = (
            scope == "PER_SYSTEM_NATIVE_CYCLE"
            or run_cycle == _MIXED_NATIVE_CYCLES
        )
        if mixed and (
            scope != "PER_SYSTEM_NATIVE_CYCLE"
            or run_cycle != _MIXED_NATIVE_CYCLES
        ):
            raise ContractError(f"{stage}_native_forward_mixed_scope_mismatch")
        if not mixed and (
            scope != "SAME_NATIVE_CYCLE"
            or run_cycle not in _NATIVE_RUN_CYCLES
        ):
            raise ContractError(f"{stage}_native_forward_same_scope_mismatch")
    else:
        mixed = scope == "PER_SYSTEM_NATIVE_CYCLE"
        if not mixed and run_cycle not in _NATIVE_RUN_CYCLES:
            raise ContractError(f"{stage}_native_forward_same_scope_mismatch")

    cycles = _lineage_json_mapping(
        "run_cycle_by_system_json", root_lineage["run_cycle_by_system_json"]
    )
    inits = _lineage_json_mapping(
        "run_init_utc_by_system_json", root_lineage["run_init_utc_by_system_json"]
    )
    queries = _lineage_json_mapping(
        "archive_query_at_utc_by_system_json",
        root_lineage["archive_query_at_utc_by_system_json"],
    )
    if set(cycles) != set(inits) or set(cycles) != set(queries):
        raise ContractError(f"{stage}_native_forward_system_lineage_incomplete")
    if mixed and not cycles:
        raise ContractError(f"{stage}_native_forward_system_lineage_empty")
    if not mixed:
        off_cycle = {
            str(system_id)
            for system_id, value in cycles.items()
            if str(value or "").strip() != run_cycle
        }
        # An envelope written before the capture cycle was recorded beside
        # the calibration cycle can only offer the fitted one, and under a
        # tolerated rider those disagree by construction.  The root is not
        # silent about which systems rode: the compiler wrote them into
        # ``activation_cycle_riders_json``, inside the same content hash, at
        # the moment the activation match accepted them.  Admit exactly that
        # named set and nothing else -- every one of the 13669 such envelopes
        # on the roster is fully covered by its own list, so a system that
        # drifted without being named is still a mismatch.  A root carrying
        # the capture cycle needs no allowance: its scope already tells the
        # truth, so ``off_cycle`` is empty and this branch never fires.
        if off_cycle and (
            root_lineage.get("native_forward_run_cycle")
            or off_cycle - _named_cycle_riders(root_lineage)
        ):
            raise ContractError(f"{stage}_native_forward_same_cycle_mismatch")

    for system_id in sorted(cycles):
        cycle = _text("run_cycle_by_system", cycles[system_id]).upper()
        if cycle not in _NATIVE_RUN_CYCLES:
            raise ContractError(f"{stage}_native_forward_cycle_invalid")
        if mixed or inits[system_id] or queries[system_id]:
            init = datetime.fromisoformat(
                _timestamp("run_init_utc_by_system", inits[system_id]).replace(
                    "Z", "+00:00"
                )
            )
            query = datetime.fromisoformat(
                _timestamp(
                    "archive_query_at_utc_by_system", queries[system_id]
                ).replace("Z", "+00:00")
            )
            if init.tzinfo is None or query.tzinfo is None or query < init:
                raise ContractError(f"{stage}_native_forward_timestamp_mismatch")
            if init.hour != int(cycle[:2]):
                raise ContractError(f"{stage}_native_forward_cycle_timestamp_mismatch")

    availability = _lineage_json_mapping(
        "native_forward_availability_evidence_json",
        root_lineage["native_forward_availability_evidence_json"],
    )
    _lineage_json_identity(
        "native_forward_availability_evidence", availability
    )
    availability_by_system = availability.get("by_system")
    if not isinstance(availability_by_system, Mapping):
        raise ContractError(f"{stage}_native_forward_availability_invalid")
    # Cover, not equal.  The availability evidence records every system the
    # forward packet observed, while ``cycles`` names the subset the cross
    # ensemble actually selected -- so a system that was fetched but left out
    # of the weights appears in one and not the other.  That is the ordinary
    # case, not a defect: ECCC publishes only 00Z, so an 18Z scope observes it
    # and cannot use it, and demanding the two sets match blocked P1 for every
    # such station with `availability_incomplete` while the evidence was in
    # fact more complete than required.
    #
    # What has to hold is that every system in use has a receipt.  Entries for
    # unselected systems are never read: the loop below indexes by ``cycles``,
    # so an extra receipt cannot reach any decision.
    if not set(cycles) <= set(str(key) for key in availability_by_system):
        raise ContractError(f"{stage}_native_forward_availability_incomplete")
    for system_id in sorted(cycles):
        receipt = availability_by_system[system_id]
        if not isinstance(receipt, Mapping):
            raise ContractError(f"{stage}_native_forward_availability_invalid")
        if (
            str(receipt.get("run_cycle") or "") != str(cycles[system_id])
            or str(receipt.get("run_init_utc") or "") != str(inits[system_id])
            or str(receipt.get("archive_query_at_utc") or "")
            != str(queries[system_id])
        ):
            raise ContractError(f"{stage}_native_forward_availability_mismatch")

    native_lineage_sha = _sha(
        "native_forward_lineage_sha256",
        root_lineage["native_forward_lineage_sha256"],
    )
    compatibility_json = root_lineage.get("taf_nwp_live_compatibility_json")
    compatibility_fingerprint = root_lineage.get(
        "taf_nwp_live_compatibility_fingerprint_sha256"
    )
    compatibility: dict[str, Any] | None = None
    if compatibility_json:
        compatibility = _lineage_json_mapping(
            "taf_nwp_live_compatibility_json", compatibility_json
        )
        embedded_fingerprint = _sha(
            "taf_nwp_live_compatibility_fingerprint_sha256",
            compatibility.get("fingerprint_sha256"),
        )
        compatibility_identity = {
            key: item
            for key, item in compatibility.items()
            if key != "fingerprint_sha256"
        }
        if embedded_fingerprint != content_sha256(compatibility_identity):
            raise ContractError(
                f"{stage}_taf_nwp_live_compatibility_fingerprint_mismatch"
            )
        if compatibility_fingerprint is not None and _sha(
            "taf_nwp_live_compatibility_fingerprint_sha256", compatibility_fingerprint
        ) != embedded_fingerprint:
            raise ContractError(
                f"{stage}_taf_nwp_live_compatibility_fingerprint_mismatch"
            )
        native_in_compatibility = compatibility.get("native_forward_lineage")
        if not isinstance(native_in_compatibility, Mapping):
            raise ContractError(
                f"{stage}_taf_nwp_live_compatibility_native_lineage_missing"
            )
        if content_sha256(native_in_compatibility) != native_lineage_sha:
            raise ContractError(f"{stage}_native_forward_lineage_hash_mismatch")
        expected_native = {
            "run_cycle_scope": scope,
            "run_cycle": run_cycle,
            "run_cycle_by_system": cycles,
            "run_init_utc_by_system": inits,
            "archive_query_at_utc_by_system": queries,
            "availability_evidence": availability,
        }
        for key, expected in expected_native.items():
            if native_in_compatibility.get(key) != expected:
                raise ContractError(
                    f"{stage}_native_forward_compatibility_mismatch"
                )
    elif compatibility_fingerprint is None:
        raise ContractError(
            f"{stage}_taf_nwp_live_compatibility_missing"
        )
    else:
        _sha(
            "taf_nwp_live_compatibility_fingerprint_sha256",
            compatibility_fingerprint,
        )

    # Inheritance is checked key against the same key.  ``run_cycle`` above
    # deliberately prefers ``native_forward_run_cycle`` because the scope and
    # mixed-cycle tests ask what the capture actually carried -- but using
    # that value as the expectation for the *``run_cycle`` key* compares two
    # different questions: the calibration scope an activation was fitted
    # under, against the cycle the archive delivered.  On 3827 of 18561 P0
    # roots those differ (native 18Z against a 00Z activation scope), and
    # every one of them raised ``native_forward_lineage_conflict:run_cycle``
    # -- including when the predecessor *was* the root, since the root's own
    # ``run_cycle`` key could never equal its ``native_forward_run_cycle``.
    # P0-M refused 14 scopes a cycle on this.
    # Only ``run_cycle`` is inherited: descendants do not carry
    # ``native_forward_run_cycle`` at all (checked -- absent on every P0-M
    # and P1 row), so demanding it would replace one false conflict with
    # another.
    native_values = {
        "run_cycle": _text("run_cycle", root_lineage.get("run_cycle")),
    }
    native_values.update({
        key: _text(key, root_lineage.get(key))
        for key in _NATIVE_FORWARD_CORE_KEYS
    })
    for key in _NATIVE_FORWARD_TAF_KEYS:
        if root_lineage.get(key) is not None:
            native_values[key] = _text(key, root_lineage[key])

    # Every descendant must carry exactly the root-owned native fields. A
    # changed JSON string is rejected even if its parsed object is equivalent;
    # this preserves the content-addressed root evidence byte-for-byte.
    for key, expected in native_values.items():
        supplied = predecessor_lineage.get(key)
        if supplied is None or str(supplied) != expected:
            raise ContractError(f"{stage}_native_forward_lineage_conflict:{key}")
    return native_values


def _likelihoods(
    probabilities: Mapping[str, float],
    raw: Mapping[str, Any],
    *,
    name: str,
) -> dict[str, float]:
    if not isinstance(raw, Mapping) or set(raw) != set(probabilities):
        raise ContractError(f"{name}_bucket_support_mismatch")
    result: dict[str, float] = {}
    for bucket in probabilities:
        value = raw[bucket]
        if isinstance(value, bool):
            raise ContractError(f"{name}_invalid")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ContractError(f"{name}_invalid") from exc
        if not math.isfinite(number) or number < 0.0:
            raise ContractError(f"{name}_invalid")
        result[str(bucket)] = number
    if not any(value > 0.0 for value in result.values()):
        raise ContractError(f"{name}_all_zero")
    return result


# Lineage a later stage may never restate differently.  These describe the
# model evidence P0 was built from -- which artifacts, which run, which bucket
# grid, which envelope bounds -- and a downstream stage that changed one of
# them would be reporting on a different forecast while claiming P0's identity.
_FROZEN_MODEL_LINEAGE_KEYS = (
    "selection_evidence_sha256",
    "ensemble_artifact_content_sha256",
    "ensemble_activation_content_sha256",
    "probability_pathway",
    "p0_probability_pathway",
    "season_bin",
    "lead_bucket",
    "run_cycle",
    "settlement_definition",
    "diagnostic_model_run_ids",
    "diagnostic_calibration_artifact_ids",
    "diagnostic_activation_ids",
    "diagnostic_model_set_hash",
    "model_envelope_lower_c",
    "model_envelope_upper_c",
    "model_envelope_span_c",
    "model_envelope_lower_bucket",
    "model_envelope_upper_bucket",
    "model_envelope_lower_ordinal",
    "model_envelope_upper_ordinal",
    "peripheral_model_reliability_by_bucket_json",
    "bucket_intervals_c_json",
    "bucket_centers_c_json",
    "formal_model_members_json",
    "formal_model_anchor_lower_bucket",
    "formal_model_anchor_upper_bucket",
    "formal_model_anchor_lower_ordinal",
    "formal_model_anchor_upper_ordinal",
    "p0_historical_tilt_proof_id",
    "p0_historical_tilt_proof_sha256",
    "p0_historical_tilt_artifact_id",
    "p0_historical_climatology_artifact_id",
    "p0_historical_applied_theta",
    "p0_historical_theta_zero_fallback",
    "conditional_future_temperature_curve_json",
    "conditional_future_temperature_curve_sha256",
    "conditional_future_temperature_curve_root_stage",
    "conditional_future_temperature_curve_root_checkpoint_id",
    "conditional_future_temperature_curve_root_probability_sha256",
)
# The centre is the one part of the model lineage each stage is *supposed* to
# move: P0-M re-anchors it on the physically confirmed centre, and P1 on the
# reported high plus the warming still expected.  Freezing it alongside the
# envelope made those stages structurally unable to publish their own anchor
# whenever the previous stage had set one -- P1 could never commit for any
# station whose P0-M had confirmed a centre.  The basket reads this under one
# key by design, so each stage restates it rather than inventing a new name;
# inheritance still applies when a stage has no anchor of its own.
_STAGE_ANCHOR_LINEAGE_KEYS = (
    "model_center_bucket",
    "model_center_c",
    "model_center_ordinal",
)


def _lineage(
    stage: str,
    raw: Mapping[str, Any],
    *,
    predecessor: ProbabilityEnvelope,
    required: Sequence[str],
) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise ContractError("probability_lineage_invalid")
    values: dict[str, str] = {}
    for key, value in raw.items():
        key_text = _text("lineage_key", key)
        if key_text.lower() in _LINEAGE_FORBIDDEN_KEYS:
            raise ContractError("probability_lineage_weather_field_forbidden")
        values[key_text] = _text("lineage_value", value)
    for key in required:
        if key not in values:
            raise ContractError(f"{stage}_lineage_missing:{key}")
    values["predecessor_checkpoint_id"] = predecessor.checkpoint_id
    values["predecessor_content_sha256"] = predecessor.content_sha256
    values["materialization_schema"] = MATERIALIZATION_SCHEMA
    values["settlement_timezone"] = predecessor.settlement_timezone
    for key in _FROZEN_MODEL_LINEAGE_KEYS:
        inherited = predecessor.lineage.get(key)
        if inherited is not None:
            supplied = values.get(key)
            if supplied is not None and supplied != inherited:
                raise ContractError("probability_model_envelope_lineage_conflict")
            values[key] = inherited
    for key in _STAGE_ANCHOR_LINEAGE_KEYS:
        inherited = predecessor.lineage.get(key)
        if inherited is not None and values.get(key) is None:
            values[key] = inherited
    return values


def _posterior(
    predecessor: ProbabilityEnvelope,
    *,
    stage: str,
    likelihoods: Mapping[str, float],
    hard_excluded_buckets: Sequence[str] = (),
    lineage: Mapping[str, str],
    checkpoint_id: str | None,
    posterior_version: str,
    decision_at_utc: str,
    fresh_until_utc: str,
    bridge_sha256: str,
    band_table: tuple[
        Sequence[tuple[tuple[float, float], float]], str
    ] | None = None,
    center_width: tuple[float, float, str] | None = None,
    center_width_floor_rungs: float = 0.0,
    center_width_tiers: Mapping[str, Any] | None = None,
    station_width: tuple[Mapping[str, float], str] | None = None,
    fleet_regime: Mapping[str, Any] | None = None,
    ceiling: Mapping[str, Any] | None = None,
    regional_prior: Mapping[str, Any] | None = None,
    four_layer_serving: Mapping[str, Any] | None = None,
) -> ProbabilityEnvelope:
    if predecessor.stage not in {"P0", "P0-M", "P1"}:
        raise ContractError("probability_predecessor_stage_invalid")
    if not timestamp_lt(predecessor.decision_at_utc, decision_at_utc):
        raise ContractError("probability_decision_time_rewind")
    if not timestamp_lt(decision_at_utc, fresh_until_utc):
        raise ContractError("probability_freshness_window_invalid")
    if stage == "P1":
        assert_event_day_started(
            predecessor.event_date,
            as_of_utc=decision_at_utc,
            target_timezone=predecessor.settlement_timezone,
        )
    excluded = {str(bucket) for bucket in hard_excluded_buckets}
    unknown = excluded - set(predecessor.probabilities)
    if unknown:
        raise ContractError("hard_exclusion_bucket_unknown")
    unnormalized = {
        bucket: predecessor.probabilities[bucket]
        * likelihoods[bucket]
        * (0.0 if bucket in excluded else 1.0)
        for bucket in predecessor.probabilities
    }
    total = sum(unnormalized.values())
    if not math.isfinite(total) or total <= 0.0:
        raise ContractError("probability_posterior_empty_after_hard_bound")
    probabilities = {bucket: value / total for bucket, value in unnormalized.items()}
    lineage = dict(lineage)
    # Geometry before claim.  This moves the distribution along the
    # temperature axis; the band table below rescales the claim that
    # distribution produces, and its factors were measured on served claims,
    # so it has to see the final one.  Applying them the other way round
    # would band the pre-shift claim and then move it out from under the
    # band that was chosen for it.
    # The station's own width scale composes with the stage scalars; either
    # may be present without the other, and identity for this station leaves
    # the posterior byte-identical.
    station_scale = 1.0
    station_width_sha: str | None = None
    if station_width is not None:
        station_scale = float(
            station_width[0].get(predecessor.station_id, 1.0)
        )
        station_width_sha = (
            str(station_width[1]) if station_scale != 1.0 else None
        )
    # The next-day fleet regime shift travels the same axis as the scalars and
    # is composed with them, but is loaded per stage-day by the caller rather
    # than read from an activated table: it is a reading of yesterday, not a
    # fitted artifact, and it has no version for a pointer to name.
    regime_delta = float((fleet_regime or {}).get("regime_delta_c") or 0.0)
    if (
        center_width is not None
        or station_scale != 1.0
        or regime_delta != 0.0
        or center_width_floor_rungs
        or center_width_tiers
    ):
        shift_c, width_lambda, center_width_sha = (
            center_width
            if center_width is not None
            else (0.0, 1.0, "")
        )
        centers = _bucket_centers_c(
            lineage, bucket_ordinals=predecessor.bucket_ordinals
        )
        if centers is None:
            # A root that publishes no bucket geometry cannot be corrected on
            # a temperature axis it does not define.  That is the pre-store
            # behaviour and stays available rather than failing the commit.
            lineage["center_width_json"] = json.dumps(
                {
                    "schema": "center_width_receipt_v1",
                    "state": "SKIPPED_NO_BUCKET_GEOMETRY",
                    "center_width_sha256": str(center_width_sha),
                },
                sort_keys=True,
            )
        else:
            ordered_keys, key_centers_c = centers
            probabilities, center_receipt = apply_center_width_calibration(
                probabilities,
                delta_c=shift_c,
                width_lambda=width_lambda,
                station_width_scale=station_scale,
                regime_delta_c=regime_delta,
                # The floor the pinned pair was fitted under, carried on the
                # same row and therefore on the same sha.
                sigma_floor_rungs=center_width_floor_rungs,
                width_tier_lambdas=(
                    (center_width_tiers or {}).get("lambdas") or None
                ),
                width_tier_boundaries=(
                    (center_width_tiers or {}).get("boundaries_rungs") or None
                ),
                ordered_keys=ordered_keys,
                key_centers_c=key_centers_c,
                excluded_buckets=sorted(excluded),
            )
            center_receipt["center_width_sha256"] = str(center_width_sha)
            if station_width_sha is not None:
                center_receipt["station_width_sha256"] = station_width_sha
            if fleet_regime:
                center_receipt["fleet_regime"] = dict(fleet_regime)
            lineage["center_width_json"] = json.dumps(
                center_receipt, sort_keys=True
            )
    # Outermost, as at P0 since 2026-08-23 -- before that P0 ran this layer
    # ahead of its centre/width scalars, so the two stages disagreed and this
    # comment was wrong about it.  This stage's own band table corrects the
    # claim the likelihood step actually produced.  P0's correction already
    # rode in through the root, so what is measured -- and corrected -- here
    # is only the residual this stage's own multiplication leaves.  Absent
    # or all-identity table means the distribution and its content hash stay
    # byte-identical to the pre-store behaviour.
    if band_table is not None:
        band_factors, band_sha = band_table
        probabilities, band_receipt = apply_band_reliability(
            probabilities, factors=band_factors
        )
        band_receipt["band_table_sha256"] = str(band_sha)
        lineage["band_reliability_json"] = json.dumps(
            band_receipt, sort_keys=True
        )
    # The clock, last of all.  Every layer above rescales a claim about which
    # bucket settles; this one answers a different question -- whether the day
    # has any room left above the high it has already reached -- and it must
    # be outermost because anything applied after it puts back the mass it
    # just took.  It ran inside the trajectory proof from 2026-08-23, and the
    # proof's posterior is an evidence record no consumer serves, so until
    # 2026-08-24 the layer never reached a served distribution at all: on the
    # 45 station-days of 2026-08-23 the proof held a median of 0.000 above the
    # running high and the committed envelope held 0.350.
    #
    # The artifact arrives from the caller for the same reason the band table
    # does: a replay must be able to rebuild this exact envelope, and a layer
    # that reads its own fitted file would rebuild a different one as soon as
    # the file was refitted.
    # Charter 99 (user ruling, 2026-09-06): a regional run issued during the
    # local day answers both halves of the ceiling's question -- how much
    # mass belongs above the bucket already holding the high, and how far
    # above -- from today's curve rather than a pooled rate and a dawn
    # shape.  It therefore stands in the ceiling's place when it speaks, and
    # the ceiling serves untouched whenever it does not.
    # Charter 95 step two, served (operator ruling 2026-09-07 "别影子了,直接上线").
    # The chain answers the ceiling's question -- how much mass belongs above
    # the bucket already holding the day's high, and how the buckets above it
    # are shaped -- from a regression over the clock, the reports and the
    # regional curve, instead of from a rate per hours-left bin.  It therefore
    # stands in the ceiling's place, and the ceiling and the regional path
    # prior serve untouched whenever it cannot speak.
    #
    # The translation is off.  It corrects the model's level, and production
    # already corrects that twice above -- the centre/width scalars and the
    # band table -- so serving it here would ship a third correction nobody
    # measured against the two that are already there.  ``-reanchor`` is also
    # the arm that measured best: 0.644 hit and -0.900 nat over 18096
    # walk-forward samples against production's 0.632 and -0.962.
    four_layer_spoke = False
    if (
        FOUR_LAYER_SERVE_ENABLED
        and four_layer_serving is not None
        and ceiling is not None
    ):
        # Guarded, and the guard is the point.  A layer that may raise at this
        # position is a layer that can stop a P1 from compiling, and a P1 that
        # does not compile is an order chain with no head.  The shadow has
        # carried this protection since it shipped; a layer that graduated to
        # serving does not get to drop it.  On any refusal the chain simply
        # does not speak and the ceiling serves, which is the pre-promotion
        # path, and the receipt says which refusal it was.
        chain: dict[str, float] | None = None
        chain_receipts: dict[str, Any] = {}
        chain_refused = ""
        try:
            chain, chain_receipts = compile_four_layer_posterior(
                root_probabilities=probabilities,
                bucket_ordinals=predecessor.bucket_ordinals,
                bucket_centers_c=four_layer_serving["bucket_centers_c"],
                bucket_intervals_c=four_layer_serving["bucket_intervals_c"],
                hard_excluded_buckets=sorted(excluded),
                running_high_c=float(four_layer_serving["running_high_c"]),
                running_high_bucket=str(ceiling["running_high_bucket"]),
                features=four_layer_serving["features"],
                hazard_artifact=four_layer_serving["hazard_artifact"],
                reanchor_artifact=None,
                bias_recent_c=four_layer_serving["bias_recent_c"],
                bias_report_count=int(four_layer_serving["bias_report_count"]),
                reanchor_enabled=False,
            )
        except Exception as exc:  # noqa: BLE001 - see above
            # Broad on purpose.  The first draft caught four named exception
            # types and a malformed artifact raised a fifth (IndexError, off
            # an empty intercept vector), which is precisely the shape of
            # failure that must not reach the compile.  At this position the
            # question is not which error it was but whether the day still
            # gets a P1, and the answer has to be yes for every error.
            chain_refused = f"{type(exc).__name__}:{exc}"[:120]
        residual = sum(chain.values()) if chain else 0.0
        if chain and math.isfinite(residual) and residual > 0.0:
            probabilities = {
                bucket: value / residual for bucket, value in chain.items()
            }
            four_layer_spoke = True
        lineage["p1_four_layer_serving_json"] = json.dumps(
            {
                "schema": "p1_four_layer_serving_receipt_v1",
                "applied": "true" if four_layer_spoke else "false",
                "reanchor_enabled": "false",
                **({"refused": chain_refused} if chain_refused else {}),
                **{
                    str(key): value
                    for key, value in dict(chain_receipts).items()
                },
            },
            sort_keys=True,
        )
    regional_spoke = False
    if (
        not four_layer_spoke
        and regional_prior is not None
        and ceiling is not None
    ):
        regional_probabilities, regional_receipt = apply_regional_path_prior(
            probabilities,
            ordinals=predecessor.bucket_ordinals,
            upper_bounds_c=regional_prior["upper_bounds_c"],
            running_high_bucket=str(ceiling["running_high_bucket"]),
            running_high_c=float(regional_prior["running_high_c"]),
            remaining_peak_c=float(regional_prior["remaining_peak_c"]),
            cell=regional_prior["cell"],
            lineage=regional_prior.get("lineage"),
        )
        if regional_receipt.get("regional_path_prior_applied") == "true":
            regional_probabilities = {
                bucket: (0.0 if bucket in excluded else value)
                for bucket, value in regional_probabilities.items()
            }
            residual = sum(regional_probabilities.values())
            if not math.isfinite(residual) or residual <= 0.0:
                raise ContractError(
                    "probability_posterior_empty_after_regional_prior"
                )
            probabilities = {
                bucket: value / residual
                for bucket, value in regional_probabilities.items()
            }
            regional_spoke = True
        regional_receipt = dict(regional_receipt)
        regional_receipt["schema"] = "regional_path_prior_receipt_v1"
        lineage["regional_path_prior_json"] = json.dumps(
            regional_receipt, sort_keys=True
        )
    if ceiling is not None and not regional_spoke and not four_layer_spoke:
        ceiling_probabilities, ceiling_receipt = apply_metar_ceiling(
            probabilities,
            ordinals=predecessor.bucket_ordinals,
            running_high_bucket=str(ceiling["running_high_bucket"]),
            event_date=predecessor.event_date,
            settlement_timezone=predecessor.settlement_timezone,
            as_of_utc=decision_at_utc,
            artifact=ceiling.get("artifact"),
            # v2 (2026-09-06): how far the newest formal report sits below
            # the running high; None serves the hour marginal.
            current_gap_c=ceiling.get("current_gap_c"),
        )
        # A rate may not revive what an observation killed, and it may not be
        # the thing that empties the support either.
        ceiling_probabilities = {
            bucket: (0.0 if bucket in excluded else value)
            for bucket, value in ceiling_probabilities.items()
        }
        residual = sum(ceiling_probabilities.values())
        if not math.isfinite(residual) or residual <= 0.0:
            raise ContractError("probability_posterior_empty_after_ceiling")
        probabilities = {
            bucket: value / residual
            for bucket, value in ceiling_probabilities.items()
        }
        ceiling_receipt = dict(ceiling_receipt)
        ceiling_receipt["schema"] = "metar_ceiling_receipt_v1"
        ceiling_receipt["metar_ceiling_artifact_sha256"] = str(
            ceiling.get("artifact_sha256") or ""
        )
        lineage["metar_ceiling_json"] = json.dumps(
            ceiling_receipt, sort_keys=True
        )
    # Last of all, after even the ceiling: no layer above may manufacture
    # an absolute zero.  A zero claim is a structural statement -- the
    # settlement rules make the bucket impossible -- and only the
    # observation hard bound has the authority to say it; everywhere else
    # a zero is a truncated tail wearing certainty's clothes.  Ruling
    # case: LFPB 2026-08-19 P1 served exactly 0.0 on "29°C or higher" at
    # 01:51Z (model centre ~3°C cold, combined tail truncated) and the
    # day settled 29.0°C -- the objective scored that scenario as
    # nonexistent and the chain lost 14.65.  Measured on 299 settled
    # stage rows the truth lands on a sub-1e-3 claim 2.7% of the time,
    # far above what those claims state -- the same under-dispersion the
    # tail calibration keeps finding.  Floor every non-excluded bucket
    # and renormalise; sub-floor claims (RKPK 2026-08-21 served 4.3e-6)
    # are numerical zeros and are lifted too.  A distribution already at
    # or above the floor everywhere is returned byte-identical, so
    # unaffected station-days keep their content hashes, and fits read
    # served claims on both sides of this change -- no serve/train split.
    floored = sorted(
        bucket for bucket, value in probabilities.items()
        if bucket not in excluded and value < PROBABILITY_MODEL_ZERO_FLOOR
    )
    if floored:
        lifted = {
            bucket: (
                value if bucket in excluded
                else max(value, PROBABILITY_MODEL_ZERO_FLOOR)
            )
            for bucket, value in probabilities.items()
        }
        lifted_total = sum(lifted.values())
        probabilities = {
            bucket: value / lifted_total
            for bucket, value in lifted.items()
        }
        lineage["probability_floor_json"] = json.dumps({
            "schema": "probability_model_zero_floor_v1",
            "epsilon": PROBABILITY_MODEL_ZERO_FLOOR,
            "floored_buckets": floored,
        }, sort_keys=True)
    identity = {
        "schema": MATERIALIZATION_SCHEMA,
        "stage": stage,
        "predecessor_content_sha256": predecessor.content_sha256,
        "decision_at_utc": decision_at_utc,
        "fresh_until_utc": fresh_until_utc,
        "probabilities": probabilities,
        "likelihoods": dict(likelihoods),
        "hard_excluded_buckets": sorted(excluded),
        "lineage": dict(lineage),
        "posterior_version": posterior_version,
        "bridge_sha256": bridge_sha256,
    }
    posterior_sha256 = content_sha256(identity)
    return ProbabilityEnvelope.from_mapping({
        "stage": stage,
        "checkpoint_id": checkpoint_id or f"{stage.lower().replace('-', '').replace('^', 'r')}_{posterior_sha256[:32]}",
        "station_id": predecessor.station_id,
        "event_date": predecessor.event_date,
        "settlement_timezone": predecessor.settlement_timezone,
        "decision_at_utc": decision_at_utc,
        "fresh_until_utc": fresh_until_utc,
        "bucket_ordinals": dict(predecessor.bucket_ordinals),
        "probabilities": probabilities,
        "posterior_version": posterior_version,
        "posterior_sha256": posterior_sha256,
        "bridge_sha256": _sha("bridge_sha256", bridge_sha256),
        "lineage": dict(lineage),
    })


def materialize_p0m_observed(
    predecessor: ProbabilityEnvelope | Mapping[str, Any],
    *,
    p0_root: ProbabilityEnvelope | Mapping[str, Any],
    forecast_revision_id: str,
    forecast_source_object_sha256: str,
    taf_tx_c: float | None,
    decision_at_utc: str,
    fresh_until_utc: str,
    bridge_sha256: str,
    checkpoint_id: str | None = None,
    # v2: the settlement-fact hard bound (charter 61) joined the stage's
    # arithmetic -- buckets the day's observed high has already ruled out
    # are excluded before normalisation, exactly as P1 has always done.
    posterior_version: str = "p0m_taf_observed_v2",
    observed_hard_excluded_buckets: Sequence[str] = (),
    observed_running_high_c: float | None = None,
    band_table: tuple[
        Sequence[tuple[tuple[float, float], float]], str
    ] | None = None,
    center_width: tuple[float, float, str] | None = None,
    center_width_floor_rungs: float = 0.0,
    center_width_tiers: Mapping[str, Any] | None = None,
    center_width_taf_cells: Mapping[str, Sequence[float]] | None = None,
    fleet_regime: Mapping[str, Any] | None = None,
    station_width: tuple[Mapping[str, float], str] | None = None,
) -> ProbabilityEnvelope:
    """Seat P0-M on a TAF that arrived without a calibrated correction.

    The stage does two separable things.  It corrects the distribution using
    a fitted TAF artifact -- and it marks a second moment at which the book
    can be entered, hours after listing, once the airport's own forecast is
    out.  Only the first needs the artifact, and requiring it for both is
    why P0-M has three station-days of history: the artifact covers RKPK and
    ZGSZ, while the inbox carries 256 captures across 44 stations, so 42
    stations' TAF arrives and is discarded.

    Here the likelihood is identity.  That is not a correction dressed as
    one: it states exactly what is known -- the forecast landed, and nothing
    calibrated exists to revise the distribution with -- so the probabilities
    are P0's, unchanged, and the lineage says so under
    ``taf_revision_state``.  What the stage contributes is the decision
    moment itself, which the entry walk then prices against whatever book
    exists at that hour.

    The observed TX is recorded but deliberately not used as an anchor: the
    calibrated path moves the centre only after the physical confirmation
    step, and moving it here on a raw TAF number would be the caller-authored
    likelihood the calibrated path exists to forbid.
    """

    previous = (
        predecessor
        if isinstance(predecessor, ProbabilityEnvelope)
        else ProbabilityEnvelope.from_mapping(predecessor)
    )
    root = (
        p0_root
        if isinstance(p0_root, ProbabilityEnvelope)
        else ProbabilityEnvelope.from_mapping(p0_root)
    )
    if previous.stage not in {"P0", "P0-M"}:
        raise ContractError("p0m_predecessor_stage_invalid")
    if root.stage != "P0":
        raise ContractError("p0m_root_stage_invalid")
    values = _lineage(
        "P0-M",
        {
            **_native_forward_lineage("P0-M", root=root, predecessor=previous),
            "forecast_revision_id": _text(
                "forecast_revision_id", forecast_revision_id
            ),
            "forecast_source_object_sha256": _sha(
                "forecast_source_object_sha256", forecast_source_object_sha256
            ),
            # The whole point of this row, stated where a reader looks.
            "taf_revision_state": "OBSERVED_NO_CALIBRATED_ARTIFACT",
            "taf_likelihood_mode": "IDENTITY_NO_ARTIFACT",
            "taf_observed_tx_c": (
                "NONE" if taf_tx_c is None else f"{float(taf_tx_c):.17g}"
            ),
            "taf_hard_exclusion": "FORBIDDEN",
            # Charter 61: the exclusion below is the OBSERVED-high fact,
            # not a TAF judgement -- the FORBIDDEN above still holds for
            # anything TAF-derived.
            "observed_running_high_c": (
                "NONE" if observed_running_high_c is None
                else f"{float(observed_running_high_c):.17g}"
            ),
            "observed_hard_excluded_buckets": (
                ",".join(str(b) for b in observed_hard_excluded_buckets)
                or "NONE"
            ),
        },
        predecessor=previous,
        required=("forecast_revision_id", "forecast_source_object_sha256"),
    )
    values["p0_root_checkpoint_id"] = root.checkpoint_id
    values["p0_root_content_sha256"] = root.content_sha256
    values["p0_root_posterior_sha256"] = root.posterior_sha256
    values["revision_mode"] = "observed_without_artifact"
    # Charter 57: the day's TAF direction picks which fitted cell of the
    # active table serves this decision.  The gap rule is the store's own
    # (``taf_direction_for_gap``), read against the root's centre, so the
    # fit and the serve classify a day identically; the table sha is
    # unchanged because the cells are part of the pinned version, and the
    # lineage records which cell was chosen.  A direction with no admitted
    # cell serves the pooled scalars, which is also every day until the
    # first cell earns its way in.
    if center_width is not None and center_width_taf_cells:
        raw_center = root.lineage.get("model_center_c")
        try:
            root_center = (
                None if raw_center in (None, "", "NONE")
                else float(raw_center)
            )
        except (TypeError, ValueError):
            root_center = None
        gap = (
            None
            if taf_tx_c is None or root_center is None
            else float(taf_tx_c) - root_center
        )
        cell_name = taf_direction_for_gap(gap)
        pair = center_width_taf_cells.get(cell_name)
        if pair is not None:
            center_width = (
                float(pair[0]), float(pair[1]), center_width[2],
            )
        values["center_width_taf_cell"] = cell_name
    return _posterior(
        root,
        stage="P0-M",
        likelihoods={bucket: 1.0 for bucket in root.probabilities},
        hard_excluded_buckets=tuple(observed_hard_excluded_buckets),
        lineage=values,
        checkpoint_id=checkpoint_id,
        posterior_version=posterior_version,
        decision_at_utc=_timestamp("decision_at_utc", decision_at_utc),
        fresh_until_utc=_timestamp("fresh_until_utc", fresh_until_utc),
        bridge_sha256=bridge_sha256,
        band_table=band_table,
        center_width=center_width,
        center_width_floor_rungs=center_width_floor_rungs,
        center_width_tiers=center_width_tiers,
        station_width=station_width,
        fleet_regime=fleet_regime,
    )


def materialize_p0m(
    predecessor: ProbabilityEnvelope | Mapping[str, Any],
    *,
    p0_root: ProbabilityEnvelope | Mapping[str, Any],
    taf_revision_artifact: TafRevisionArtifact | Mapping[str, Any],
    taf_revision_proof: TafRevisionProof | Mapping[str, Any],
    decision_at_utc: str,
    fresh_until_utc: str,
    bridge_sha256: str,
    checkpoint_id: str | None = None,
    # v2: charter 61 -- the observed-high hard bound is part of the stage's
    # arithmetic, same as the observed path.
    posterior_version: str = "p0m_taf_target_v2",
    revision: bool = False,
    band_table: tuple[
        Sequence[tuple[tuple[float, float], float]], str
    ] | None = None,
    center_width: tuple[float, float, str] | None = None,
    center_width_floor_rungs: float = 0.0,
    center_width_tiers: Mapping[str, Any] | None = None,
    center_width_taf_cells: Mapping[str, Sequence[float]] | None = None,
    fleet_regime: Mapping[str, Any] | None = None,
    station_width: tuple[Mapping[str, float], str] | None = None,
    observed_hard_excluded_buckets: Sequence[str] = (),
    observed_running_high_c: float | None = None,
) -> ProbabilityEnvelope:
    """Materialize TAF confirmation/correction as the P0-M checkpoint.

    The first TAF must consume NWP P0. Subsequent AMD/COR revisions consume
    P0-M. Every version is recomputed from the same immutable P0 root using
    an activated OOS artifact and a content-addressed bounded revision proof.
    No raw TAF/METAR/NWP payload or caller-authored likelihood ratio is
    accepted here.
    """

    previous = predecessor if isinstance(predecessor, ProbabilityEnvelope) else ProbabilityEnvelope.from_mapping(predecessor)
    root = p0_root if isinstance(p0_root, ProbabilityEnvelope) else ProbabilityEnvelope.from_mapping(p0_root)
    stage = "P0-M"
    if (previous.stage != "P0" and not revision) or (
        revision and previous.stage != "P0-M"
    ):
        raise ContractError("p0m_predecessor_stage_invalid")
    if root.stage != "P0":
        raise ContractError("p0m_root_stage_invalid")
    native_values = _native_forward_lineage(
        stage,
        root=root,
        predecessor=previous,
    )
    proof = validate_taf_revision_proof(
        taf_revision_proof,
        artifact=taf_revision_artifact,
        predecessor=previous,
        p0_root=root,
    )
    values = _lineage(
        stage,
        {
            **native_values,
            "forecast_revision_id": proof.forecast_revision_id,
            "forecast_source_object_sha256": proof.forecast_source_object_sha256,
            "taf_revision_artifact_id": proof.artifact_id,
            "taf_revision_artifact_sha256": proof.artifact_content_sha256,
            "taf_revision_activation_id": proof.activation_id,
            "taf_revision_proof_id": proof.proof_id,
            "taf_revision_proof_sha256": proof.content_sha256,
            "taf_revision_mode": proof.mode,
            "taf_probability_mass_transfer": f"{proof.probability_mass_transfer:.17g}",
            "taf_center_displacement_buckets": str(proof.center_displacement_buckets),
            "taf_applied_strength": f"{proof.applied_strength:.17g}",
            "taf_tx_calibrated_center_c": f"{proof.tx_calibrated_center_c:.17g}",
            "taf_tx_c": f"{proof.taf_tx_c:.17g}",
            "taf_physical_features_sha256": proof.physical_features_sha256,
            "taf_cloud_cover_fraction": (
                f"{proof.physical_features['cloud_cover_fraction']:.17g}"
            ),
            "taf_precipitation_probability": (
                f"{proof.physical_features['precipitation_probability']:.17g}"
            ),
            "taf_wind_speed_normalized": (
                f"{proof.physical_features['wind_speed_normalized']:.17g}"
            ),
            "taf_physically_confirmed_center_c": (
                f"{proof.physically_confirmed_center_c:.17g}"
            ),
            "taf_physical_adjustment_c": f"{proof.physical_adjustment_c:.17g}",
            "taf_physical_confirmation_state": proof.physical_confirmation_state,
            "taf_physical_consistency_factor": (
                f"{proof.physical_consistency_factor:.17g}"
            ),
            "taf_hard_exclusion": "FORBIDDEN",
        },
        predecessor=previous,
        required=("forecast_revision_id", "forecast_source_object_sha256"),
    )
    values["p0_root_checkpoint_id"] = root.checkpoint_id
    values["p0_root_content_sha256"] = root.content_sha256
    values["p0_root_posterior_sha256"] = root.posterior_sha256
    values["revision_mode"] = (
        "root_replacement" if revision else "first_taf_confirmation"
    )
    # Stage anchor, published under the same key P0 and P1 use.  P0-M's own
    # point estimate is the TAF TX center after calibration and the cloud /
    # precipitation / wind physical confirmation -- that is precisely "where
    # this stage's data points".  Anchoring on the posterior argmax instead
    # would inherit P0's edge-bucket defect: an open-ended bucket spans an
    # unbounded interval and can win an argmax without the forecast pointing
    # anywhere near it.
    anchor_bucket = _anchor_bucket_for_temperature(
        proof.physically_confirmed_center_c,
        root_lineage=root.lineage,
        bucket_ordinals=root.bucket_ordinals,
    )
    if anchor_bucket is not None:
        values["model_center_bucket"] = anchor_bucket
        values["model_center_c"] = (
            f"{proof.physically_confirmed_center_c:.12g}"
        )
    _sha("forecast_source_object_sha256", values["forecast_source_object_sha256"])
    # Charter 57, same selection as the observed path: the proof's own TX
    # against the root centre picks the fitted cell; a direction with no
    # admitted cell serves the pooled scalars.
    if center_width is not None and center_width_taf_cells:
        raw_center = root.lineage.get("model_center_c")
        try:
            root_center = (
                None if raw_center in (None, "", "NONE")
                else float(raw_center)
            )
        except (TypeError, ValueError):
            root_center = None
        gap = (
            None if root_center is None
            else float(proof.taf_tx_c) - root_center
        )
        cell_name = taf_direction_for_gap(gap)
        pair = center_width_taf_cells.get(cell_name)
        if pair is not None:
            center_width = (
                float(pair[0]), float(pair[1]), center_width[2],
            )
        values["center_width_taf_cell"] = cell_name
    # Charter 61: the observed-high fact, recorded and applied exactly as
    # on the observed path.
    values["observed_running_high_c"] = (
        "NONE" if observed_running_high_c is None
        else f"{float(observed_running_high_c):.17g}"
    )
    values["observed_hard_excluded_buckets"] = (
        ",".join(str(b) for b in observed_hard_excluded_buckets) or "NONE"
    )
    return _posterior(
        root,
        stage=stage,
        likelihoods=_likelihoods(
            root.probabilities,
            proof.likelihood_ratio_by_bucket,
            name="forecast_likelihood_ratio",
        ),
        hard_excluded_buckets=tuple(observed_hard_excluded_buckets),
        lineage=values,
        checkpoint_id=checkpoint_id,
        posterior_version=posterior_version,
        decision_at_utc=_timestamp("decision_at_utc", decision_at_utc),
        fresh_until_utc=_timestamp("fresh_until_utc", fresh_until_utc),
        bridge_sha256=bridge_sha256,
        band_table=band_table,
        center_width=center_width,
        center_width_floor_rungs=center_width_floor_rungs,
        center_width_tiers=center_width_tiers,
        station_width=station_width,
        fleet_regime=fleet_regime,
    )


def _p1_prediction_root_lineage(root: ProbabilityEnvelope) -> tuple[str, str]:
    """Return the immutable P0 reference for a legal P1 prediction root.

    P1 may be rooted at raw NWP P0 when no TAF checkpoint exists, or at P0-M
    after the TAF/physical confirmation is materialized.  The root stage,
    checkpoint and content hash are all written into P1 lineage, so neither
    form can be rebound across branches later.
    """

    if root.stage == "P0":
        return root.checkpoint_id, root.content_sha256
    if root.stage != "P0-M":
        raise ContractError("p1_prediction_root_stage_invalid")
    return (
        _text(
            "p1_prediction_root_p0_root_checkpoint_id",
            root.lineage.get("p0_root_checkpoint_id"),
        ),
        _sha(
            "p1_prediction_root_p0_root_content_sha256",
            root.lineage.get("p0_root_content_sha256"),
        ),
    )


def materialize_p1(
    predecessor: ProbabilityEnvelope | Mapping[str, Any],
    *,
    prediction_root: ProbabilityEnvelope | Mapping[str, Any],
    trajectory_artifact: MetarTrajectoryLikelihoodArtifact | Mapping[str, Any],
    trajectory_proof: MetarTrajectoryUpdateProof | Mapping[str, Any],
    trajectory_observations: Sequence[
        MetarTrajectoryObservation | Mapping[str, Any]
    ],
    bucket_centers_c: Mapping[str, Any],
    bucket_upper_bounds_c: Mapping[str, Any],
    reported_high_advanced: bool,
    decision_at_utc: str,
    fresh_until_utc: str,
    bridge_sha256: str,
    truth_observation_revision_content_sha256: str | None = None,
    checkpoint_id: str | None = None,
    # v3 (2026-08-23): the cooling-side ceiling now speaks inside the
    # posterior, and the F-market hard bound cuts at the first killing report
    # rather than half a Celsius degree past it.  Both change the served
    # distribution, so the version has to move with them -- the calibration
    # layer derives its evidence bound from exactly this string, and a
    # distribution nobody serves any more must not be fitted on.
    # v4 (2026-08-24): the conditioners run on the prefix's own evidence
    # clock instead of the decision stamp, and the ceiling speaks only on the
    # step whose running high the day still holds.  A root rebase used to
    # replay morning prefixes under an evening clock, and the ceiling then
    # crushed every bucket above a high the day had already beaten.
    # v5 (2026-08-24): the cooling-side ceiling reaches the served
    # distribution.  It had only ever been applied inside the trajectory
    # proof, which no consumer serves, so the shape production published
    # still held the mass the clock had already ruled out.
    # v6 (2026-09-02): the soft likelihood is suspended by ruling -- identity
    # ratios, zero strength -- so the served P1 is the root under the hard
    # bound and the clock ceiling.  Measured against the exact counterfactual
    # the soft layer cost 1 to 6 points of argmax accuracy at every fixed
    # local hour; see ``SOFT_LIKELIHOOD_POLICY``.  The version moves because
    # the served shape moved: the centre/width evidence clock reads this
    # string to know where the currently served distribution begins.
    # v7 (2026-09-06, operator ruling): the clock ceiling reads a second axis
    # -- how far the newest formal report sits below the running high -- and
    # speaks from 14 hours out (10:00 local) instead of 10.  Both move the
    # served shape, so the version moves with them.
    # v8 (2026-09-06, operator ruling): a regional model's curve, when one
    # was issued during the local day, states where the day tops out and
    # stands in the clock ceiling's place.  The served shape moves with it,
    # so the version moves too.
    # v9 (2026-09-07, operator ruling "别影子了,直接上线"): charter 95 step
    # two's chain stands in the ceiling's place -- the split above the running
    # high and the shape of the buckets above it now come from a regression
    # over the clock, the reports and the regional curve rather than a rate
    # per hours-left bin.  The served shape moves with it, so the version does.
    posterior_version: str = "p1_metar_trajectory_target_v9",
    ceiling_artifact: Mapping[str, Any] | None = None,
    # Charter 99: {"remaining_peak_c", "cell", "lineage"} for the freshest
    # admissible regional run, or None.  Handed in by the caller for the
    # same reason the ceiling artifact is: a replay must rebuild this
    # envelope, and a layer that read its own store would rebuild another.
    regional_prior: Mapping[str, Any] | None = None,
    # Charter 95 step two, served: the feature row, the hazard artifact and
    # the geometry the chain reads, handed in for the reason every fitted
    # table is -- a replay must rebuild this exact envelope.
    four_layer_serving: Mapping[str, Any] | None = None,
    band_table: tuple[
        Sequence[tuple[tuple[float, float], float]], str
    ] | None = None,
    center_width: tuple[float, float, str] | None = None,
    center_width_floor_rungs: float = 0.0,
    center_width_tiers: Mapping[str, Any] | None = None,
    station_width: tuple[Mapping[str, float], str] | None = None,
    fleet_regime: Mapping[str, Any] | None = None,
    # What the four layers would have served, carried beside what was served.
    # Never multiplied into the distribution; the keys are lineage and the
    # ledger scores them.  Absent means the caller was handed no artifacts,
    # and the lineage is then byte-identical to what it was before this
    # existed.
    four_layer_shadow: Mapping[str, str] | None = None,
) -> ProbabilityEnvelope:
    """Recompute P1 from one immutable P0/P0-M root and full METAR prefix.

    ``truth_observation_revision_content_sha256`` names the canonical AWC
    revision the closing observation was drawn from.  It is distinct from
    ``canonical_observation_sha256``, which hashes the raw METAR object: the
    revision fact is what the truth ledger is keyed on, so an envelope without
    it cannot be turned into atomic facts at all.  Optional here because
    callers that replay historical envelopes have no canonical store to resolve
    it against.
    """

    previous = predecessor if isinstance(predecessor, ProbabilityEnvelope) else ProbabilityEnvelope.from_mapping(predecessor)
    root = (
        prediction_root
        if isinstance(prediction_root, ProbabilityEnvelope)
        else ProbabilityEnvelope.from_mapping(prediction_root)
    )
    p0_root_checkpoint_id, p0_root_content_sha256 = _p1_prediction_root_lineage(
        root
    )
    if not isinstance(reported_high_advanced, bool):
        raise ContractError("p1_reported_high_advanced_invalid")
    if previous.stage == "P1":
        if not timestamp_lt(previous.decision_at_utc, decision_at_utc):
            raise ContractError("probability_decision_time_rewind")
        if previous.lineage.get("prediction_root_stage") != root.stage:
            raise ContractError("p1_predecessor_root_stage_mismatch")
        previous_root = _text(
            "p1_predecessor_prediction_root_checkpoint_id",
            previous.lineage.get("prediction_root_checkpoint_id"),
        )
        if previous_root != root.checkpoint_id:
            raise ContractError("p1_prediction_root_branch_mismatch")
        previous_root_content = _sha(
            "p1_predecessor_prediction_root_content_sha256",
            previous.lineage.get("prediction_root_content_sha256"),
        )
        if previous_root_content != root.content_sha256:
            raise ContractError("p1_prediction_root_content_mismatch")
        if (
            _text(
                "p1_predecessor_p0_root_checkpoint_id",
                previous.lineage.get("p0_root_checkpoint_id"),
            )
            != p0_root_checkpoint_id
            or _sha(
                "p1_predecessor_p0_root_content_sha256",
                (
                    previous.lineage.get("p0_root_content_sha256")
                    or (
                        previous.lineage.get("prediction_root_content_sha256")
                        if str(
                            previous.lineage.get("prediction_root_stage") or ""
                        ).strip().upper() == "P0"
                        else None
                    )
                ),
            )
            != p0_root_content_sha256
        ):
            raise ContractError("p1_p0_root_branch_mismatch")
    else:
        if previous.stage != root.stage:
            raise ContractError("p1_initial_predecessor_root_stage_mismatch")
        if previous.checkpoint_id != root.checkpoint_id:
            raise ContractError("p1_initial_predecessor_must_equal_prediction_root")
        if previous.content_sha256 != root.content_sha256:
            raise ContractError("p1_initial_predecessor_content_mismatch")
    native_values = _native_forward_lineage(
        "P1",
        root=root,
        predecessor=previous,
    )
    proof = validate_metar_trajectory_update_proof(
        trajectory_proof,
        prediction_root=root,
        observations=trajectory_observations,
        artifact=trajectory_artifact,
        bucket_centers_c=bucket_centers_c,
        bucket_upper_bounds_c=bucket_upper_bounds_c,
    )
    governed_trajectory_artifact = (
        trajectory_artifact
        if isinstance(trajectory_artifact, MetarTrajectoryLikelihoodArtifact)
        else MetarTrajectoryLikelihoodArtifact.from_mapping(trajectory_artifact)
    )
    conditional_path = (
        governed_trajectory_artifact.method_version in {
            METAR_TRAJECTORY_CONDITIONAL_PATH_METHOD_VERSION,
            METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION,
            METAR_TRAJECTORY_PHYSICAL_ABSOLUTE_TMAX_METHOD_VERSION,
        }
    )
    conditional_hourly_curve = (
        governed_trajectory_artifact.method_version
        == METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION
    )
    physical_absolute_tmax = (
        governed_trajectory_artifact.method_version
        == METAR_TRAJECTORY_PHYSICAL_ABSOLUTE_TMAX_METHOD_VERSION
    )
    if conditional_path and proof.conditional_path_state_sha256 is None:
        raise ContractError("p1_conditional_path_state_proof_missing")
    future_curve_sha = str(
        root.lineage.get("conditional_future_temperature_curve_sha256") or ""
    ).strip().lower()
    if conditional_hourly_curve and (
        len(future_curve_sha) != 64
        or any(char not in "0123456789abcdef" for char in future_curve_sha)
    ):
        raise ContractError("p1_conditional_future_curve_lineage_missing")
    if proof.as_of_utc != decision_at_utc:
        raise ContractError("p1_trajectory_proof_decision_time_mismatch")
    last_event_id = proof.canonical_observation_ids[-1]
    normalized_observations = tuple(
        item
        if isinstance(item, MetarTrajectoryObservation)
        else MetarTrajectoryObservation.from_mapping(item)
        for item in trajectory_observations
    )
    last_matches = tuple(
        item
        for item in normalized_observations
        if item.event.observation_id == last_event_id
    )
    if len(last_matches) != 1:
        raise ContractError(
            "p1_canonical_observation_identity_ambiguous"
        )
    last_observation = last_matches[0]
    # The ceiling's second axis (v2, 2026-09-06): the newest formal report
    # against the running high.  Formal lanes only, as the running high is.
    latest_formal_temperature_c = ceiling_latest_temperature_c(
        (item.event.observed_at_utc, item.temperature_c)
        for item in normalized_observations
        if item.event.source_lane not in SOFT_PATH_SOURCE_LANES
    )
    ceiling_gap_c = ceiling_current_gap_c(
        proof.running_high_c, latest_formal_temperature_c
    )
    surviving_buckets = [
        bucket
        for bucket in root.probabilities
        if bucket not in set(proof.hard_excluded_buckets)
    ]
    if not surviving_buckets:
        raise ContractError("p1_reported_high_bucket_missing")
    reported_high_bucket = min(
        surviving_buckets,
        key=lambda bucket: (root.bucket_ordinals[bucket], bucket),
    )
    values = _lineage(
        "P1",
        {
            **native_values,
            "canonical_observation_event_id": last_event_id,
            "canonical_observation_sha256": (
                last_observation.event.raw_sha256
            ),
            # The canonical revision fact behind the closing observation, as
            # distinct from the raw object hash above.  The truth ledger is
            # keyed on the revision, so an envelope missing this compiles and
            # then fails at the atomic-fact boundary.  No production path
            # produced it before, so that boundary had never been crossed.
            **(
                {
                    "truth_observation_revision_content_sha256": str(
                        truth_observation_revision_content_sha256
                    )
                }
                if truth_observation_revision_content_sha256
                else {}
            ),
            "canonical_observation_committed_at": (
                last_observation.canonical_committed_at_utc
            ),
            "settlement_timezone": (
                last_observation.event.settlement_timezone
            ),
            "source_lane": last_observation.event.source_lane.upper(),
            "report_type": last_observation.event.report_type,
            "canonical_observation_sequence_sha256": (
                proof.canonical_observation_sequence_sha256
            ),
            "canonical_observation_ids": ",".join(
                proof.canonical_observation_ids
            ),
            "hard_bound_id": "hardbound-" + proof.content_sha256[:32],
            "metar_trajectory_artifact_id": proof.artifact_id,
            "metar_trajectory_artifact_sha256": (
                proof.artifact_content_sha256
            ),
            "metar_trajectory_activation_id": proof.activation_id,
            "metar_trajectory_proof_id": proof.proof_id,
            "metar_trajectory_proof_sha256": proof.content_sha256,
            "metar_conditional_path_state_sha256": (
                proof.conditional_path_state_sha256 or "NONE"
            ),
            "metar_future_temperature_curve_sha256": (
                future_curve_sha or "NONE"
            ),
            "metar_trajectory_applied_strength": (
                f"{proof.applied_strength:.17g}"
            ),
            # The execution gate reads this: a zero strength is admissible
            # only when the proof itself declares the suspension.
            "metar_trajectory_soft_likelihood_policy": (
                proof.soft_likelihood_policy
            ),
            "metar_trajectory_degradation_reasons": (
                ",".join(proof.degradation_reasons) or "NONE"
            ),
            "metar_trajectory_running_high_c": (
                f"{proof.running_high_c:.17g}"
            ),
            "metar_trajectory_latest_formal_temperature_c": (
                "NONE" if latest_formal_temperature_c is None
                else f"{float(latest_formal_temperature_c):.17g}"
            ),
            "reported_high_c": f"{proof.running_high_c:.17g}",
            "reported_high_bucket": reported_high_bucket,
            "reported_high_ordinal": str(
                root.bucket_ordinals[reported_high_bucket]
            ),
            "reported_high_advanced": (
                "true" if reported_high_advanced else "false"
            ),
            "metar_trajectory_direction": proof.direction,
            "metar_trajectory_direction_role": (
                "DIAGNOSTIC_ONLY_BASKET_RECOMPUTES_POLICY_DIRECTION"
            ),
            "metar_trajectory_center_bucket": proof.center_bucket,
            # Stage anchor, read by the basket under the same key P0 uses.  The
            # trajectory's own argmax above stays diagnostic.
            "model_center_bucket": proof.model_center_bucket,
            "model_center_c": f"{proof.model_center_c:.12g}",
            "metar_trajectory_positive_core_buckets": ",".join(
                proof.positive_core_buckets
            ),
            "metar_trajectory_positive_core_role": (
                "DIAGNOSTIC_POSITIVE_PROBABILITY_SUPPORT_NOT_BASKET_CORE"
            ),
            "observation_likelihood_mode": (
                # Under suspension the artifact's method is lineage, not the
                # update: whatever was fitted, the served P1 is the hard
                # exclusion and the clock ceiling, and the mode says so.
                P1_SOFT_LR_SUSPENDED_LIKELIHOOD_MODE
                if proof.soft_likelihood_policy
                == SOFT_LIKELIHOOD_POLICY_SUSPENDED
                else "PHYSICAL_ABSOLUTE_TMAX_SOFT_LR_PLUS_HARD_EXCLUSION_V5"
                if physical_absolute_tmax
                else "CONDITIONAL_HOURLY_FUTURE_PATH_SOFT_LR_PLUS_HARD_EXCLUSION_V4"
                if conditional_hourly_curve
                else "CONDITIONAL_FUTURE_PATH_SOFT_LR_PLUS_HARD_EXCLUSION_V3"
                if conditional_path
                else "FULL_PREFIX_ROOT_RECOMPUTE_SOFT_LR_PLUS_HARD_EXCLUSION_V2"
            ),
        },
        predecessor=previous,
        required=(
            "canonical_observation_event_id",
            "canonical_observation_sha256",
            "canonical_observation_committed_at",
            "settlement_timezone",
            "source_lane",
            "report_type",
            "hard_bound_id",
            "metar_trajectory_proof_sha256",
        ),
    )
    for shadow_key in SHADOW_LINEAGE_KEYS:
        shadow_value = (four_layer_shadow or {}).get(shadow_key)
        if shadow_value:
            values[shadow_key] = str(shadow_value)
    values["prediction_root_stage"] = root.stage
    values["prediction_root_checkpoint_id"] = root.checkpoint_id
    values["prediction_root_content_sha256"] = root.content_sha256
    values["p0_root_checkpoint_id"] = p0_root_checkpoint_id
    values["p0_root_content_sha256"] = p0_root_content_sha256
    _sha("canonical_observation_sha256", values["canonical_observation_sha256"])
    values["hard_excluded_buckets"] = (
        ",".join(proof.hard_excluded_buckets)
        or "NONE"
    )
    values["event_consumed_once"] = "full_prefix_content_addressed"
    return _posterior(
        root,
        stage="P1",
        likelihoods=_likelihoods(
            root.probabilities,
            proof.likelihood_ratio_by_bucket,
            name="observation_likelihood",
        ),
        hard_excluded_buckets=proof.hard_excluded_buckets,
        lineage=values,
        checkpoint_id=checkpoint_id,
        posterior_version=posterior_version,
        decision_at_utc=_timestamp("decision_at_utc", decision_at_utc),
        fresh_until_utc=_timestamp("fresh_until_utc", fresh_until_utc),
        bridge_sha256=bridge_sha256,
        band_table=band_table,
        center_width=center_width,
        center_width_floor_rungs=center_width_floor_rungs,
        center_width_tiers=center_width_tiers,
        station_width=station_width,
        fleet_regime=fleet_regime,
        # The proof already decided both of the ceiling's preconditions: which
        # bucket the day's high sits in, and whether this step's high is the
        # current one.  Reading them off the proof keeps the served
        # distribution and the evidence record answering the same question.
        ceiling=(
            None
            if ceiling_artifact is None
            else {
                "running_high_bucket": reported_high_bucket,
                "artifact": ceiling_artifact,
                "artifact_sha256": content_sha256(dict(ceiling_artifact)),
                "current_gap_c": ceiling_gap_c,
            }
            if proof.prefix_is_current
            else None
        ),
        regional_prior=(
            None
            if regional_prior is None or not proof.prefix_is_current
            else {
                **dict(regional_prior),
                "running_high_c": float(proof.running_high_c),
                "upper_bounds_c": dict(bucket_upper_bounds_c),
            }
        ),
        four_layer_serving=(
            None
            if four_layer_serving is None or not proof.prefix_is_current
            else {
                **dict(four_layer_serving),
                "running_high_c": float(proof.running_high_c),
            }
        ),
    )


#: Lineage values stored once and referenced, instead of copied into every
#: envelope row.
#:
#: ``lineage_json`` is 95.5% of an envelope row and these three keys are 65%
#: of it, while carrying almost no information per row.  Measured over 4,000
#: committed envelopes from 2026-08-31: the live-compatibility document
#: repeats 49.7x (37.55 MB stored, 0.756 MB distinct), the forward
#: availability evidence 60.5x (6.56 MB / 0.108 MB) and the conditional curve
#: 4.5x (4.82 MB / 1.077 MB).  That is 11.75 KB of pure duplication per
#: envelope, about 208 MB a day at the fleet's 17,711 envelopes.
#:
#: Each of the three already carries a companion fingerprint in the same
#: lineage, verified 1:1 with its document over the same 4,000 rows -- so the
#: envelope's identity has always bound these bodies by hash.  This is a
#: storage encoding and nothing more: ``content_sha256`` is still computed
#: over the whole lineage, and the loader rehydrates before it verifies, so
#: every stored row and every consumer sees exactly the bytes it saw before.
DEHYDRATED_LINEAGE_KEYS: tuple[str, ...] = (
    "taf_nwp_live_compatibility_json",
    "native_forward_availability_evidence_json",
    "conditional_future_temperature_curve_json",
)
#: A dehydrated value.  Real documents are JSON objects or arrays, so they
#: begin with ``{`` or ``[`` and can never collide with this.
LINEAGE_DOCUMENT_REF_PREFIX = "@ref/"
LINEAGE_DOCUMENT_DDL = (
    "CREATE TABLE IF NOT EXISTS basket_probability_lineage_document("
    "  document_sha256 TEXT PRIMARY KEY"
    "    CHECK(length(document_sha256)=64),"
    "  document TEXT NOT NULL,"
    "  created_at_utc TEXT NOT NULL"
    ")"
)


def _store_lineage_document(
    conn: sqlite3.Connection,
    *,
    digest: str,
    document: str,
    created_at_utc: str,
) -> None:
    """Write one document, creating the table only if it is not there.

    Not ``CREATE TABLE IF NOT EXISTS`` before every write.  That statement is
    cheap to decide and expensive to ask: SQLite takes the schema lock and
    bumps the schema cookie, which invalidates every prepared statement on
    every connection to the file.  Asked once per dehydrated key of every
    envelope -- three a row, thousands of rows a cycle -- it turned an
    ordinary write into a schema-churn storm, and the first production cycle
    after it shipped ran 27 minutes past its own overdue bound without
    completing.

    Writing first costs nothing in the steady state and pays one caught
    exception the first time, which is also the only correct way to key this:
    a per-connection cache cannot be built on ``sqlite3.Connection`` (it
    supports no weak reference) and an id-keyed one is wrong the moment an id
    is recycled.
    """

    insert = (
        "INSERT OR IGNORE INTO basket_probability_lineage_document("
        "document_sha256,document,created_at_utc) VALUES(?,?,?)"
    )
    try:
        conn.execute(insert, (digest, document, str(created_at_utc)))
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        conn.execute(LINEAGE_DOCUMENT_DDL)
        conn.execute(insert, (digest, document, str(created_at_utc)))


def dehydrate_lineage_documents(
    conn: sqlite3.Connection,
    lineage: Mapping[str, Any],
    *,
    created_at_utc: str,
) -> dict[str, Any]:
    """Store each large lineage body once and reference it by its own hash.

    Content-addressed and insert-or-ignore, so a body already stored is not
    rewritten and two envelopes carrying the same document share one row.
    Written inside the caller's transaction: an envelope that commits without
    its bodies would be unreadable, and one that rolls back must take them
    with it.
    """

    pending = [
        (key, value)
        for key, value in (
            (key, lineage.get(key)) for key in DEHYDRATED_LINEAGE_KEYS
        )
        if isinstance(value, str)
        and value
        and not value.startswith(LINEAGE_DOCUMENT_REF_PREFIX)
    ]
    if not pending:
        return dict(lineage)
    dehydrated = dict(lineage)
    for key, value in pending:
        digest = content_sha256(value)
        _store_lineage_document(
            conn,
            digest=digest,
            document=value,
            created_at_utc=created_at_utc,
        )
        dehydrated[key] = LINEAGE_DOCUMENT_REF_PREFIX + digest
    return dehydrated


def rehydrate_lineage_documents(
    conn: sqlite3.Connection,
    lineage: Mapping[str, Any],
) -> dict[str, Any]:
    """Put back the bodies stored once and referenced by hash.

    The counterpart of ``dehydrate_lineage_documents``.  It runs before the
    envelope is built and therefore before its ``content_sha256`` is checked,
    which is what keeps the dehydration a storage encoding rather than a
    change of identity: the sha is computed over the rehydrated lineage and
    matches the one stored beside it, for rows written before this existed and
    after it alike.

    A reference whose body is missing fails closed.  It cannot be recovered --
    the body is the evidence, and an envelope that cannot present it is not
    servable -- and silently leaving the marker in place would hand a consumer
    the string ``@ref/...`` where it expected a document.
    """

    refs = {
        key: str(value)[len(LINEAGE_DOCUMENT_REF_PREFIX):]
        for key, value in lineage.items()
        if isinstance(value, str)
        and value.startswith(LINEAGE_DOCUMENT_REF_PREFIX)
    }
    if not refs:
        return dict(lineage)
    try:
        placeholders = ",".join("?" for _ in refs)
        stored = {
            str(row[0]): str(row[1])
            for row in conn.execute(
                "SELECT document_sha256,document"
                "  FROM basket_probability_lineage_document"
                f" WHERE document_sha256 IN ({placeholders})",
                tuple(sorted(set(refs.values()))),
            )
        }
    except sqlite3.Error as exc:
        raise ContractError(
            "probability_lineage_document_store_unavailable"
        ) from exc
    resolved = dict(lineage)
    for key, digest in refs.items():
        body = stored.get(digest)
        if body is None:
            raise ContractError(
                "probability_lineage_document_missing:" + key
            )
        resolved[key] = body
    return resolved


def insert_probability_envelope(
    conn: sqlite3.Connection,
    envelope: ProbabilityEnvelope,
    *,
    created_at_utc: str,
    state: str = "COMMITTED",
) -> dict[str, Any]:
    """Append one validated envelope; equal replay is idempotent."""

    envelope_call_started = time.perf_counter()
    status = production_ssot_schema_status(conn)
    if not status["ok"]:
        raise ContractError("production_ssot_schema_incomplete")
    assert_connection_target_capacity(conn)
    note_ledger_span(
        "env_schema_capacity",
        (time.perf_counter() - envelope_call_started) * 1000.0,
    )
    envelope_validate_started = time.perf_counter()
    if state not in {"COMMITTED", "QUARANTINED"}:
        raise ContractError("probability_state_invalid")
    if (
        envelope.stage == "P0"
        and envelope.lineage.get("probability_pathway")
        == "REPUBLISHER_NO_LIVE_P0_V1"
    ):
        try:
            authorized = int(
                conn.execute("SELECT canonical_p0_projection_authorized()").fetchone()[0]
            )
        except sqlite3.Error as exc:
            raise ContractError("republisher_p0_direct_envelope_writer_forbidden") from exc
        if authorized != 1:
            raise ContractError("republisher_p0_direct_envelope_writer_forbidden")
    settlement_day_window(envelope.event_date, envelope.settlement_timezone)
    created = _timestamp("created_at_utc", created_at_utc)
    if envelope.stage == "P1":
        assert_event_day_started(
            envelope.event_date,
            as_of_utc=envelope.decision_at_utc,
            target_timezone=envelope.settlement_timezone,
        )
    # Stored dehydrated, hashed whole.  ``envelope.content_sha256`` below is
    # still computed from the full lineage, so the row's identity and every
    # reference to it are byte-for-byte what they were; only the bytes on disk
    # are smaller.
    encoded_lineage = json.dumps(
        dehydrate_lineage_documents(
            conn, envelope.lineage, created_at_utc=created_at_utc
        ),
        ensure_ascii=False,
        sort_keys=True,
    )
    existing = conn.execute(
        "SELECT content_sha256 FROM basket_probability_envelope WHERE envelope_id=?",
        (envelope.checkpoint_id,),
    ).fetchone()
    if existing is not None:
        if str(existing[0]) != envelope.content_sha256:
            raise ContractError("probability_checkpoint_id_content_conflict")
        return {"schema": MATERIALIZATION_SCHEMA, "state": "ALREADY_COMMITTED", "wrote": False, "envelope_id": envelope.checkpoint_id}
    same_stage_instant = conn.execute(
        "SELECT checkpoint_id,content_sha256 "
        "FROM basket_probability_envelope "
        "WHERE station_id=? AND event_date=? AND stage=? "
        "AND decision_at_utc=? AND state='COMMITTED'",
        (
            envelope.station_id,
            envelope.event_date,
            envelope.stage,
            envelope.decision_at_utc,
        ),
    ).fetchall()
    if same_stage_instant:
        # Equal replay is handled by the checkpoint-id branch above.  Two
        # different committed authorities for one stage and decision instant
        # would make stage routing insertion-order dependent, so reject the
        # second write instead of letting a later reader choose accidentally.
        raise ContractError("probability_stage_decision_time_conflict")
    if envelope.stage == "P1":
        event_id = envelope.lineage.get("canonical_observation_event_id")
        if not event_id:
            raise ContractError("p1_canonical_event_id_missing")
        # Every committed P1 for the station-day, with its lineage JSON, on
        # every P1 insert -- and the JSON lives in overflow pages, so this is
        # a table lookup and a json.loads per prior envelope.  Timed so the
        # consume-once check can be told apart from the write it guards.
        with ledger_span("env_p1_lineage_scan"):
            prior_p1_rows = conn.execute(
                "SELECT lineage_json,content_sha256 FROM basket_probability_envelope WHERE station_id=? AND event_date=? AND stage='P1'",
                (envelope.station_id, envelope.event_date),
            ).fetchall()
        for row in prior_p1_rows:
            old_lineage = json.loads(str(row[0]))
            # Consume-once is a statement about one branch, so the key has to
            # be the branch the compiler itself works on: prediction root as
            # well as P0 root.  Keyed on the P0 root alone it also caught the
            # legitimate restart -- a fresh P0-M on the same P0 root ends the
            # old P1 branch, so the compiler correctly replays the day's
            # observations against the new root, emits a first (non-
            # reaffirming) P1 for that branch, and this guard read it as the
            # same evidence answered twice.  Measured 2026-08-26: EGLC took a
            # second P0-M at 05:54Z on an unchanged P0 root and P1 fail-closed
            # for the station every cycle afterwards; 29 station-days since
            # 08-20 sit on a superseded prediction root the same way.  Within
            # one branch nothing changes -- the same evidence still may not
            # produce two different answers.
            if (
                old_lineage.get("canonical_observation_event_id") == event_id
                and old_lineage.get("p0_root_checkpoint_id")
                == envelope.lineage.get("p0_root_checkpoint_id")
                and str(old_lineage.get("prediction_root_checkpoint_id") or "")
                == str(
                    envelope.lineage.get("prediction_root_checkpoint_id") or ""
                )
                and str(
                    old_lineage.get("prediction_root_content_sha256") or ""
                ).lower()
                == str(
                    envelope.lineage.get("prediction_root_content_sha256") or ""
                ).lower()
            ):
                if str(row[1]) == envelope.content_sha256:
                    return {"schema": MATERIALIZATION_SCHEMA, "state": "ALREADY_COMMITTED", "wrote": False, "envelope_id": envelope.checkpoint_id}
                if envelope.lineage.get("reaffirmation_of_checkpoint_id"):
                    # A reaffirmation is the declared exception to
                    # consume-once: same observation prefix, same root,
                    # deliberately re-clocked because the staleness gate
                    # measures time since evidence was examined, not time
                    # since the answer moved.  Its lineage names what it
                    # re-examined, so it is auditable rather than the silent
                    # nondeterminism this gate exists to kill.  Without this
                    # branch every hot night wedged P1 permanently: no new
                    # daily high -> no packets -> reaffirm -> same event and
                    # root with a new clock -> killed as corruption -> retry
                    # forever while the served P1 aged past its grace.
                    continue
                raise ContractError("p1_canonical_event_already_consumed")
    note_ledger_span(
        "env_validate",
        (time.perf_counter() - envelope_validate_started) * 1000.0,
    )
    envelope_insert_started = time.perf_counter()
    conn.execute(
        """INSERT INTO basket_probability_envelope(
            envelope_id,stage,checkpoint_id,station_id,event_date,settlement_timezone,decision_at_utc,
            fresh_until_utc,bucket_ordinals_json,probabilities_json,posterior_version,
            posterior_sha256,bridge_sha256,lineage_json,content_sha256,state,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            envelope.checkpoint_id,
            envelope.stage,
            envelope.checkpoint_id,
            envelope.station_id,
            envelope.event_date,
            envelope.settlement_timezone,
            envelope.decision_at_utc,
            envelope.fresh_until_utc,
            json.dumps(dict(envelope.bucket_ordinals), ensure_ascii=False, sort_keys=True),
            json.dumps(dict(envelope.probabilities), ensure_ascii=False, sort_keys=True),
            envelope.posterior_version,
            envelope.posterior_sha256,
            envelope.bridge_sha256,
            encoded_lineage,
            envelope.content_sha256,
            state,
            created,
        ),
    )
    note_ledger_span(
        "env_row_insert",
        (time.perf_counter() - envelope_insert_started) * 1000.0,
    )
    return {"schema": MATERIALIZATION_SCHEMA, "state": "COMMITTED", "wrote": True, "envelope_id": envelope.checkpoint_id}


__all__ = [
    "MATERIALIZATION_SCHEMA",
    "insert_probability_envelope",
    "materialize_p0m",
    "materialize_p1",
]
