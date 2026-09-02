"""M5 atomic decision and capital ledger for the Bayes basket target lane.

This module is the only target-lane writer for decision facts.  It is a
database boundary, not an executor: it never opens a network connection,
calls CLOB, or submits an order.  A committed intent carries the live
authority of the plan that produced it -- ``live_submit_authorized`` is 0 for
a no-live plan and 1 for a live one, which is what tells the two books apart
downstream -- and the single blocking authority remains
``auto_trade_decisions``.  Writing an order is still somebody else's job:
this module records what was decided, never reaches a gateway, and a live
flag here is a statement about the decision, not permission to act on it.

The writer deliberately accepts already compiled observation/bound facts and
does not inspect weather, probabilities, TAF, NWP, or market REST data.  The
five facts are committed under one SQLite ``BEGIN IMMEDIATE`` transaction:
observation, bound, decision, intent(s), and capital reservation.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .asos_substitute_observation_contract import (
    CanonicalSubstituteObservationRevisionFact,
    is_substitute_revision_mapping,
)
from .awc_observation_contract import (
    CanonicalAWCObservationContract,
    CanonicalAWCObservationRevisionFact,
)
from .contracts import ContractError, canonical_json, content_sha256
from .basket_kernel.state_machine import basket_lifecycle_projection
from .et_history import assert_event_day_started, event_day_window
from .execution_deadline import (
    TargetExecutionDeadlineError,
    TargetExecutionHardDeadline,
)
from .execution_version_vector import (
    ExecutionVersionVector,
    NoExecutionVersionPolicy,
    NO_EXECUTION_POLICY_SCHEMA,
    build_execution_version_vector,
    build_no_execution_version_policy,
)
from .ssot import (
    PRODUCTION_LEDGER_DDL,
    upgrade_execution_vector_contract,
)


SCHEMA = "bayes_basket_target_atomic_ledger_v2"
OBSERVATION_SCHEMA = "basket_target_observation_projection_v2"
BOUND_SCHEMA = "basket_target_bound_projection_v2"
# The pre-observation chain head.  Deliberately its own schema rather than a
# relaxed observation projection: an observed fact and a model's evidence are
# different claims, and a replay must not be able to read one as the other.
STAGE_EVIDENCE_ROOT_SCHEMA = "basket_target_stage_evidence_projection_v1"
PLAN_SCHEMA = "basket_target_plan_v1"
DECISION_SCHEMA = "basket_target_decision_fact_v1"
# v2: the decision is addressed by what it was made of, not by when.
DECISION_SCHEMA_V2 = "basket_target_decision_fact_v2"
# The keys of ``details`` that describe the decision rather than the moment.
# Everything else in ``details`` is either the clock in another form
# (``target_execution_hard_deadline_at`` is created_at + 3s;
# ``forward_evidence_binding`` carries ``decision_at_utc``) or a receipt
# flag that does not change what was decided.
_ECONOMIC_DETAIL_KEYS = (
    "core_buckets", "direction", "current_core_buckets", "current_direction",
    "delta_v2_content_sha256", "production_plan_sha256", "execution_mode",
    "delta_v2_authorized", "candidate_will_order",
)


def economic_decision_identity(
    *,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    strategy_lane: str,
    decided_state: str,
    will_order: int,
    reason_codes: Sequence[str],
    details: Mapping[str, Any],
    decision_scope_key: str | None,
) -> dict[str, Any]:
    """What a decision is made of (user ruling, 2026-09-02).

    Four legs -- probability, the objective's sizing output, the position
    book, the policy artefacts -- plus what was decided.  Wall clocks are
    audit metadata and are not in here.

    The old identity carried ``target_execution_hard_deadline_at``, which
    is the commit clock plus three seconds, so every re-decision was a new
    decision by construction: measured 2026-09-02 over 11,364 basket
    decisions from 08-28, 11,364 distinct shas against 5,080 distinct
    economic contents.  MMMX 2026-09-02 wrote 99 decisions for 5.

    Two readings, one key.  A decision with a training fact carries
    ``decision_scope_key``, which is exactly the four legs (see
    ``delta_v2_decision_scope_key``) and is used whole.  A decision without
    one -- every blocked decision to date, whose fact is built but never
    stored -- is addressed by the economic subset of its ``details`` and
    its reasons, which is everything about it that was ever written down.
    The two are kept as separate fields so a key from one reading can never
    collide with a key from the other.
    """

    lifecycle = details.get("basket_lifecycle")
    economic_details = {
        key: details.get(key) for key in _ECONOMIC_DETAIL_KEYS
        if key in details
    }
    if isinstance(lifecycle, Mapping):
        economic_details["basket_lifecycle"] = {
            key: lifecycle.get(key)
            for key in ("state", "reason", "final_support", "candidate_support")
            if key in lifecycle
        }
    return {
        "scope": {
            "station_id": str(station_id),
            "event_date": str(event_date),
            "settlement_timezone": str(settlement_timezone),
            "strategy_lane": str(strategy_lane),
        },
        "decided": {
            "decided_state": str(decided_state),
            "will_order": int(will_order),
            "reason_codes": sorted(str(code) for code in reason_codes),
        },
        "decision_scope_key": (
            str(decision_scope_key) if decision_scope_key else None
        ),
        "economic_details": economic_details,
    }
INTENT_SCHEMA = "basket_target_order_intent_v1"
RESERVATION_SCHEMA = "basket_target_capital_reservation_v1"
STAGE_ZERO_EXECUTION_ANCHOR_SCHEMA = (
    "basket_target_stage_zero_execution_anchor_v1"
)
TARGET_PROJECTION_AUTHORIZATION_FUNCTION = (
    "target_canonical_projection_ingress_authorized"
)

TARGET_LEDGER_TABLES = (
    "basket_target_observation_facts",
    "basket_target_bound_facts",
    "basket_target_execution_cycles",
    "auto_trade_decisions",
    "basket_target_order_intents",
    "basket_target_capital_pool",
    "basket_target_capital_reservations",
    "basket_target_prepared_supersession_receipts",
    "basket_target_capital_epoch_migrations",
    "basket_target_writer_lock_wait_telemetry",
)

CYCLE_STATES = frozenset({"prepared", "submitting", "submitted", "reconcile_required", "settled"})
INTENT_STATES = frozenset({"prepared", "submitting", "submitted", "reconcile_required", "filled", "cancelled", "failed"})
# ``expired`` is accepted by the table's CHECK and written by nothing, here or
# anywhere else in the repository -- no row has ever held it.  It was carried
# in with a bulk landing, not by an expiry path: the basket lane has no reaper
# and should not grow one on ``target_execution_hard_deadline_at``, which is a
# three-second submission-freshness bound checked against the intent's own
# creation instant, not a lifetime.  A day that is genuinely over is retired
# by ``close_settled_day_cycle`` instead.  The value stays in the CHECK
# because SQLite cannot alter one in place, and rebuilding a content-addressed
# table to drop an unused enum member would fork every existing database from
# a fresh one.
RESERVATION_STATES = frozenset({"active", "released", "consumed", "expired"})
# A cycle that produced any of these reached the exchange, so neither
# terminal path may retire it.  Shared so the two paths admit exactly the
# same class of cycle rather than drifting apart.
_EXECUTION_FACT_CHECKS = (
    ("basket_target_order_events", "order_intent_id"),
    ("basket_target_order_reconciliations", "order_intent_id"),
    ("basket_target_execution_receipts", "order_intent_id"),
    ("basket_target_execution_transition_versions", "order_intent_id"),
    ("basket_target_post_fill_recompute_requests", "order_intent_id"),
)
# ``hard_no`` records may exist in historical SQLite files, but that is not a
# production lane.  All target-lane writes are now the pure-YES basket.
LANES = frozenset({"basket"})

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
# A target day should only have a small append-only chain per probability
# stage.  Keep the stage-currentness check bounded even if a corrupt source
# tries to fan out checkpoints for one station-day.
_STAGE_ZERO_PROBABILITY_SCAN_LIMIT = 256


class AtomicLedgerError(ContractError):
    """Raised when the target atomic ledger cannot safely proceed."""


class InjectedAtomicFault(RuntimeError):
    """Test-only fault used to prove transaction rollback at each write point."""


def _assert_event_day_started(
    event_date: str,
    *,
    settlement_timezone: str,
    as_of_utc: str,
) -> None:
    """Reject future registry settlement days at the decision write boundary.

    Any externally discovered future market must be quarantined before it
    reaches this ledger.  Keeping this guard here makes the invariant hold
    even when an upstream caller bypasses the resident worker's scope filter.
    """

    try:
        assert_event_day_started(
            event_date,
            as_of_utc=as_of_utc,
            target_timezone=_text(
                "settlement_timezone", settlement_timezone
            ),
        )
    except ContractError as exc:
        raise AtomicLedgerError(str(exc)) from exc
    except Exception as exc:
        raise AtomicLedgerError("event_day_window_invalid") from exc


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise AtomicLedgerError(f"{name}_missing")
    return result


def _date(name: str, value: Any) -> str:
    result = _text(name, value)
    if not _DATE_RE.fullmatch(result):
        raise AtomicLedgerError(f"{name}_invalid_date")
    try:
        datetime.strptime(result, "%Y-%m-%d")
    except ValueError as exc:
        raise AtomicLedgerError(f"{name}_invalid_date") from exc
    return result


def _settlement_timezone(name: str, value: Any) -> str:
    """Require one exact, valid registry timezone without normalization.

    The raw IANA name is immutable lineage: accepting an alias and silently
    replacing it with another spelling would change the source fact.  We only
    use ``ZoneInfo`` as a validity fence and return the supplied exact value.
    """

    result = _text(name, value)
    try:
        ZoneInfo(result)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise AtomicLedgerError(f"{name}_invalid") from exc
    return result


def _formal_observation_source_lane(name: str, value: Any) -> str:
    """Accept the formal observation lanes, canonically spelled.

    AWC is the registry lane.  MADIS_HFMETAR and ASOS_1MIN are the scheduled
    ASOS substitute carriers -- the same sensor filling the station's own
    frozen METAR slot when the relay missed it, admitted behind an OOS_READY
    exact-match artifact.  A substitute-closed P1 may therefore root an
    atomic plan.  Everything else (AMSC, garbage) stays refused.
    """

    lane = _text(name, value).upper()
    if lane not in {"AWC", "MADIS_HFMETAR", "ASOS_1MIN"}:
        raise AtomicLedgerError(f"{name}_must_be_formal_observation_lane")
    return lane


_FORMAL_REPORT_TYPES_BY_LANE = {
    "AWC": frozenset({"METAR", "SPECI"}),
    "MADIS_HFMETAR": frozenset({"ASOS_HF"}),
    "ASOS_1MIN": frozenset({"ASOS_1MIN"}),
}


def _timestamp(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AtomicLedgerError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AtomicLedgerError(f"{name}_timezone_required")
    return result


def _timestamp_instant(name: str, value: Any) -> datetime:
    """Parse an ISO-8601 timestamp to its UTC instant for CAS equality.

    SSOT records preserve the publisher's textual precision (for example,
    ``.309Z``), whereas a compile adapter can canonically render that same
    instant as ``.309000Z``.  Version checks must bind to the instant, not to
    an incidental fractional-second spelling.  Callers still bind the
    immutable snapshot id and content hash separately.
    """

    raw = _timestamp(name, value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:  # _timestamp already validates; retain a fence.
        raise AtomicLedgerError(f"{name}_invalid_iso8601") from exc
    return parsed.astimezone(timezone.utc)


def _stage_zero_visible_probability_rows(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    stage: str,
    as_of: datetime,
) -> list[tuple[str, datetime]]:
    """Return bounded committed rows visible at ``as_of`` by UTC instant.

    SQLite text ordering is not a time ordering once publishers retain
    different RFC3339 fractional precision.  This deliberately parses each
    bounded row in Python rather than accepting SQLite's permissive date
    coercion: an invalid committed timestamp is a fail-closed condition.
    """

    rows = conn.execute(
        "SELECT checkpoint_id,decision_at_utc FROM basket_probability_envelope "
        "WHERE station_id=? AND event_date=? AND settlement_timezone=? "
        "AND stage=? AND state='COMMITTED' LIMIT ?",
        (
            station_id,
            event_date,
            settlement_timezone,
            stage,
            _STAGE_ZERO_PROBABILITY_SCAN_LIMIT + 1,
        ),
    ).fetchall()
    if len(rows) > _STAGE_ZERO_PROBABILITY_SCAN_LIMIT:
        raise AtomicLedgerError("stage_zero_execution_probability_scan_limit")
    parsed_rows: list[tuple[str, datetime]] = []
    for raw_checkpoint_id, raw_decision_at in rows:
        decision_at = _timestamp_instant(
            "stage_zero_execution_visible_probability_decision_at",
            raw_decision_at,
        )
        if decision_at <= as_of:
            parsed_rows.append((str(raw_checkpoint_id), decision_at))
    return parsed_rows


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if not _HEX64_RE.fullmatch(result):
        raise AtomicLedgerError(f"{name}_invalid_sha256")
    return result


def _number(name: str, value: Any, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise AtomicLedgerError(f"{name}_invalid_number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise AtomicLedgerError(f"{name}_invalid_number") from exc
    if not math.isfinite(result) or (result <= 0 if positive else result < 0):
        raise AtomicLedgerError(f"{name}_invalid_number")
    return result


def _mapping(name: str, value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise AtomicLedgerError(f"{name}_json_invalid") from exc
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise AtomicLedgerError(f"{name}_mapping_required")


def _target_execution_deadline(
    value: Any,
    *,
    now: str | None = None,
    required: bool = True,
) -> str | None:
    if value in (None, ""):
        if required:
            raise AtomicLedgerError("target_execution_hard_deadline_missing")
        return None
    try:
        deadline = TargetExecutionHardDeadline.from_value(value)
        if now is not None:
            deadline.assert_not_expired(now)
        return deadline.isoformat
    except TargetExecutionDeadlineError as exc:
        raise AtomicLedgerError(str(exc)) from exc


def _rows(name: str, value: Any) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise AtomicLedgerError(f"{name}_list_required")
    result = [row for row in value if isinstance(row, Mapping)]
    if len(result) != len(value):
        raise AtomicLedgerError(f"{name}_row_invalid")
    return result


def _basket_lane(name: str, value: Any) -> str:
    """Accept the sole production lane and make retired input explicit.

    Historical rows remain readable by SQLite migrations.  This writer must
    never turn a historical ``hard_no`` value into a new cycle, decision,
    reservation, capital-arbitration request, or order intent.
    """

    lane = _text(name, value).lower()
    if lane == "hard_no":
        raise AtomicLedgerError("strategy_lane_hard_no_retired")
    if lane != "basket":
        raise AtomicLedgerError("strategy_lane_invalid")
    return lane


def _derive_id(prefix: str, identity: Mapping[str, Any]) -> str:
    return f"{prefix}_{content_sha256(identity)[:40]}"


def _verify_or_set_id(name: str, supplied: Any, expected: str) -> str:
    if supplied not in (None, "") and str(supplied) != expected:
        raise AtomicLedgerError(f"{name}_content_address_mismatch")
    return expected


def _content_hash(schema: str, identity: Mapping[str, Any]) -> str:
    return content_sha256({"schema": schema, **dict(identity)})


def _reason_codes(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise AtomicLedgerError("reason_codes_list_required")
    result = [str(item).strip() for item in value]
    if any(not item for item in result):
        raise AtomicLedgerError("reason_code_empty")
    if len(result) != len(set(result)):
        raise AtomicLedgerError("reason_codes_duplicate")
    return result


def _assert_plan_observation_bound_lineage(
    *,
    plan_lineage: Mapping[str, Any],
    observation: Mapping[str, Any],
    bound: Mapping[str, Any],
) -> None:
    """Bind the P1 plan to the exact canonical observation and hard bound.

    The atomic writer receives already-materialized facts, but it must not
    trust the caller to have joined them to the selected P1 envelope.  These
    identities are therefore checked both while building and immediately
    before committing a bundle.
    """

    if str(observation.get("probability_stage") or "") in {"P0", "P0-M"}:
        # A stage-rooted plan names the evidence it was compiled from, which is
        # the root's own identity rather than a report that does not exist yet.
        expected = {
            "stage_evidence_root_id": str(
                observation.get("observation_id") or ""
            ),
            "stage_evidence_content_sha256": str(
                observation.get("evidence_root_content_sha256") or ""
            ).lower(),
            "probability_checkpoint_id": str(
                observation.get("checkpoint_id") or ""
            ),
            "soft_bound_id": str(bound.get("bound_id") or ""),
        }
    else:
        expected = {
            "canonical_observation_event_id": str(
                observation.get("observation_id") or ""
            ),
            "truth_observation_revision_content_sha256": str(
                observation.get("truth_observation_revision_content_sha256") or ""
            ).lower(),
            # The bare name, not ``..._utc``.  Materialization publishes
            # ``canonical_observation_committed_at`` and nothing has ever
            # written the suffixed spelling, so demanding it here could only
            # ever report the field missing -- the same swap that moved this
            # root onto evidence the live stage could not produce (2026-08-06)
            # renamed it, and production_io's own copy of this check was
            # corrected earlier while this one was not.
            "canonical_observation_committed_at": str(
                observation.get("canonical_committed_at") or ""
            ),
            "hard_bound_id": str(bound.get("bound_id") or ""),
            "source_lane": str(observation.get("source_lane") or "").upper(),
            "report_type": str(observation.get("report_type") or "").upper(),
        }
    for field, expected_value in expected.items():
        actual = str(plan_lineage.get(field) or "")
        if field == "truth_observation_revision_content_sha256":
            actual = actual.lower()
        elif field in {"source_lane", "report_type"}:
            actual = actual.upper()
        if not actual:
            raise AtomicLedgerError(
                f"plan_lineage_{field}_missing"
            )
        if field.endswith("committed_at"):
            # Bind to the instant, exactly as ``_timestamp_instant`` was
            # written for: the plan carries the observation's commit at
            # microsecond precision while the canonical fact states the same
            # instant in milliseconds, so a raw string test reports a
            # mismatch about trailing zeroes -- RKPK 2026-08-21,
            # ``...744000Z`` against ``...744Z``.
            try:
                if _timestamp_instant(
                    f"plan_lineage_{field}", actual
                ) != _timestamp_instant(f"observation_{field}", expected_value):
                    raise AtomicLedgerError(
                        f"plan_lineage_{field}_mismatch"
                    )
            except AtomicLedgerError:
                raise
            except Exception:  # noqa: BLE001
                raise AtomicLedgerError(
                    f"plan_lineage_{field}_mismatch"
                ) from None
            continue
        if actual != expected_value:
            raise AtomicLedgerError(
                f"plan_lineage_{field}_mismatch"
            )


def _assert_plan_lane_contract(
    *,
    lane: str,
    plan: Mapping[str, Any],
    plan_lineage: Mapping[str, Any],
    commit_boundary: bool = False,
) -> None:
    if lane != "basket":
        raise AtomicLedgerError("strategy_lane_hard_no_retired")
    # All three stages open or adjust a position: P0 on the ensemble, P0-M on
    # the TAF revision, P1 on observed METAR.  Requiring P1 here made the two
    # that precede an observation unable to hold one at all, which is not the
    # conservatism it looked like -- it left the opening stage with no way to
    # act on the evidence it is built from.
    if str(plan_lineage.get("probability_stage") or "") not in {
        "P0", "P0-M", "P1",
    }:
        raise AtomicLedgerError(
            "target_atomic_commit_stage_invalid"
            if commit_boundary
            else "target_atomic_bundle_stage_invalid"
        )


def build_target_atomic_bundle(
    *,
    awc_observation_contract: Mapping[str, Any] | None = None,
    observed_revision_fact: Mapping[str, Any] | None = None,
    truth_observation_revision_content_sha256: str = "",
    stage_evidence_root: Mapping[str, Any] | None = None,
    stage_soft_bound: Mapping[str, Any] | None = None,
    bound: Mapping[str, Any] | None = None,
    plan: Mapping[str, Any],
    decision: Mapping[str, Any],
    intents: Sequence[Mapping[str, Any]] = (),
    reserved_amount_usdc: float | None = None,
    execution_version_vector: Mapping[str, Any] | ExecutionVersionVector | None = None,
    execution_version_vectors: Sequence[Mapping[str, Any] | ExecutionVersionVector] | None = None,
    no_execution_policy: Mapping[str, Any] | NoExecutionVersionPolicy | None = None,
    training_fact: Mapping[str, Any] | None = None,
    target_execution_hard_deadline_at: str | datetime | None = None,
    decision_scope_key: str | None = None,
    created_at_utc: str,
) -> dict[str, Any]:
    """Normalize a candidate and derive every identity from immutable content."""

    created_at = _timestamp("created_at_utc", created_at_utc)
    # Two chain roots, one shape.  P1 roots on an observed METAR fact; P0 and
    # P0-M exist before the event day has produced one and root on the stage
    # evidence they do have -- the ensemble selection/activation for P0, the
    # TAF revision proof for P0-M.  Everything downstream derives scope and
    # identity from whichever root applies, so only the head of the chain
    # differs and the P1 path below is untouched.
    # Three ways to name the head of the chain, exactly one of them at a time.
    # ``stage_evidence_root`` is P0/P0-M's, below.  The two observed forms
    # differ only in who created the fact they root on: a contract carries the
    # revision so the commit can stage it, while ``observed_revision_fact``
    # points at one already staged by the observation-identity lane and the
    # commit only verifies it.  The second exists because the first made the
    # facts a by-product of the trading they gate -- and because carrying a
    # whole contract per candidate means embedding its authority projection,
    # which is 30 KB of document to re-state something already in the store.
    roots_supplied = sum(
        1
        for value in (
            stage_evidence_root,
            awc_observation_contract,
            observed_revision_fact,
        )
        if value is not None
    )
    if roots_supplied > 1:
        raise AtomicLedgerError("target_atomic_root_ambiguous")
    if roots_supplied == 0:
        raise AtomicLedgerError("target_atomic_root_missing")
    stage_rooted = stage_evidence_root is not None
    fact_rooted = observed_revision_fact is not None
    if stage_rooted:
        # P0 and P0-M exist before the event day has produced an observation,
        # so they root the chain on the evidence they do have: the ensemble
        # selection/activation for P0, the TAF revision proof for P0-M.  The
        # shape matches the observed root exactly -- one scope, one immutable
        # content hash -- so everything downstream is unchanged and only the
        # head of the chain differs.
        root = dict(stage_evidence_root)
        obs = {
            "observation_id": _text(
                "stage_evidence_root_id", root.get("evidence_root_id")
            ),
            "station_id": _text(
                "stage_evidence_station_id", root.get("station_id")
            ),
            "event_date": _date(
                "stage_evidence_event_date", root.get("event_date")
            ),
            "settlement_timezone": _settlement_timezone(
                "stage_evidence_settlement_timezone",
                root.get("settlement_timezone"),
            ),
            "probability_stage": _text(
                "stage_evidence_probability_stage",
                root.get("probability_stage"),
            ),
            "position_role": _text(
                "stage_evidence_position_role", root.get("position_role")
            ),
            "bound_kind": _text(
                "stage_evidence_bound_kind", root.get("bound_kind")
            ),
            "checkpoint_id": _text(
                "stage_evidence_checkpoint_id", root.get("checkpoint_id")
            ),
            "posterior_sha256": _sha(
                "stage_evidence_posterior_sha256", root.get("posterior_sha256")
            ),
            "evidence_root_content_sha256": _sha(
                "stage_evidence_content_sha256", root.get("content_sha256")
            ),
        }
        if obs["probability_stage"] not in {"P0", "P0-M"}:
            raise AtomicLedgerError("stage_evidence_probability_stage_invalid")
        obs_sha = _content_hash(STAGE_EVIDENCE_ROOT_SCHEMA, obs)
        observation_fact_id = obs["observation_id"]
        soft = dict(stage_soft_bound or {})
        if not soft:
            raise AtomicLedgerError("stage_evidence_soft_bound_missing")
        # The soft bound answers the same question the hard one does -- which
        # buckets may be opened -- so it is projected into the same shape and
        # the rest of the chain never learns which kind it holds.  It carries
        # no ``hard_lower_bound``: nothing has been observed to exclude a
        # bucket, and inventing an integer floor here would let a forecast
        # masquerade as a settled fact.
        bound = {
            "bound_id": _text("soft_bound_id", soft.get("bound_id")),
            "station_id": obs["station_id"],
            "event_date": obs["event_date"],
            "bound_generation": 0,
            "hard_lower_bound": None,
            "bound_json": {
                "bound_kind": _text(
                    "soft_bound_kind", soft.get("bound_kind")
                ),
                "probability_stage": obs["probability_stage"],
                "soft_lower_ordinal": int(soft["soft_lower_ordinal"]),
                "soft_upper_ordinal": int(soft["soft_upper_ordinal"]),
                "admitted_buckets": list(soft.get("admitted_buckets") or []),
                "excluded_buckets": list(soft.get("excluded_buckets") or []),
                "content_sha256": _sha(
                    "soft_bound_content_sha256", soft.get("content_sha256")
                ),
            },
        }
    else:
        if bound is None:
            raise AtomicLedgerError("target_atomic_bound_missing")
        revision_sha = _sha(
            "truth_observation_revision_content_sha256",
            truth_observation_revision_content_sha256,
        )
        if fact_rooted:
            # One fact, validated by its own canonical class, and it has to be
            # the one named.  Nothing weaker: the sha is what the commit
            # boundary looks the row up by, so a fact whose content hashes to
            # something else would root the chain on a report the store never
            # agreed to.  Two identity lanes share this root: PR12 truth
            # revisions for AWC reports, and the parallel substitute identity
            # facts for scheduled ASOS slot fills -- each validated by its
            # own canonical class, dispatched on the lane the fact declares.
            named_fact = (
                CanonicalSubstituteObservationRevisionFact.from_mapping(
                    observed_revision_fact
                )
                if is_substitute_revision_mapping(observed_revision_fact)
                else CanonicalAWCObservationRevisionFact.from_mapping(
                    observed_revision_fact
                )
            )
            if named_fact.content_sha256 != revision_sha:
                raise AtomicLedgerError(
                    "truth_observation_revision_reference_not_exact"
                )
            revision = named_fact.raw
        else:
            canonical_contract = CanonicalAWCObservationContract.from_mapping(
                awc_observation_contract
            )
            matching_revisions = [
                fact for fact in canonical_contract.revision_facts
                if fact.content_sha256 == revision_sha
            ]
            if len(matching_revisions) != 1:
                raise AtomicLedgerError(
                    "truth_observation_revision_reference_not_exact"
                )
            revision = matching_revisions[0].raw
        # Identity from the fact, day scope from the trade.  The canonical
        # observation lane keys its facts on the ET operations day by explicit
        # doctrine (``FOUR_DOCTRINE_CANONICAL_DAY_OVERRIDE_20260805_V1``),
        # while a contract settles on the station's own day -- Asia/Seoul for
        # RKPK, Asia/Manila for RPLL, per the market registry that resolves
        # it.  Charter clause 23 keeps those two days apart on purpose and
        # forbids substituting one for the other.  Importing the fact's day
        # scope into the chain therefore compared ET against the station's
        # timezone and could never agree; before 2026-08-06 this root took its
        # scope from the observation the plan carried and cited the fact only
        # for identity, which is what it does again here.  Station identity is
        # not day-scoped, so the fact must still agree about which station it
        # describes.
        fact_station = _text("observation_station_id", revision.get("station_id"))
        scope_station = _text(
            "observation_station_id", bound.get("station_id", fact_station)
        )
        if fact_station != scope_station:
            raise AtomicLedgerError("observation_station_scope_mismatch")
        obs = {
            "observation_id": _text("observation_id", revision.get("observation_id")),
            "station_id": scope_station,
            "event_date": _date(
                "observation_event_date",
                bound.get("event_date", revision.get("event_date")),
            ),
            "settlement_timezone": _settlement_timezone(
                "observation_settlement_timezone",
                _mapping("plan", plan).get("settlement_timezone")
                or revision.get("settlement_timezone"),
            ),
            "source_lane": _formal_observation_source_lane(
                "observation_source_lane", revision.get("source_lane")
            ),
            "report_type": _text("observation_report_type", revision.get("report_type")).upper(),
            "observed_at_utc": _timestamp(
                "observation_observed_at_utc", revision.get("observed_at_utc")
            ),
            "raw_sha256": _sha("observation_raw_sha256", revision.get("raw_object_sha256")),
            # The projection's source-receipt slot.  The key keeps its
            # historical name -- it hashes into OBSERVATION_SCHEMA and names
            # a table column -- but the slot holds whichever fetch receipt
            # the fact's own lane carries: the AWC batch receipt for a PR12
            # revision, the MADIS object / IEM request receipt for a
            # substitute identity fact.
            "awc_source_receipt_sha256": _sha(
                "observation_awc_source_receipt_sha256",
                revision.get("awc_source_receipt_sha256")
                or revision.get("source_receipt_sha256"),
            ),
            # The observation's own canonical commit, which is what the plan
            # consumed and carries.  The fact's ``canonical_committed_at_utc``
            # is a derived ``max`` of that and the origin record's commit, so
            # it runs a few milliseconds later whenever the origin landed
            # second -- measured on LFPB 2026-08-21: plan 08:02:49.295Z
            # against a derived 08:02:49.299Z, which is a mismatch about
            # nothing.  Older facts predate the explicit field and carry only
            # the derived value.
            "canonical_committed_at": _timestamp(
                "observation_canonical_committed_at",
                revision.get("source_observation_committed_at_utc")
                or revision.get("canonical_committed_at_utc"),
            ),
            "truth_observation_revision_content_sha256": revision_sha,
        }
        if obs["report_type"] not in _FORMAL_REPORT_TYPES_BY_LANE[
            obs["source_lane"]
        ]:
            raise AtomicLedgerError("observation_report_type_invalid")
        obs_sha = _content_hash(OBSERVATION_SCHEMA, obs)
        observation_fact_id = _verify_or_set_id(
            "observation_fact_id", None, _derive_id("obsfact", obs)
        )

    bound_payload = _mapping("bound_json", bound.get("bound_json", bound.get("hard_bound" , {})))
    bound_identity = {
        "bound_id": _text("bound_id", bound.get("bound_id")),
        "observation_id": obs["observation_id"],
        "station_id": _text("bound_station_id", bound.get("station_id", obs["station_id"])),
        "event_date": _date("bound_event_date", bound.get("event_date", obs["event_date"])),
        "bound_generation": int(bound.get("bound_generation", 0)),
        "hard_lower_bound": None if bound.get("hard_lower_bound") in (None, "") else int(bound["hard_lower_bound"]),
        "bound_json": bound_payload,
        # Present only on an observed root.  A stage-rooted bound has no truth
        # revision to name -- the day has produced none -- and writing an empty
        # string would claim one exists.
        "truth_observation_revision_content_sha256": obs.get(
            "truth_observation_revision_content_sha256"
        ),
    }
    if bound_identity["bound_generation"] < 0:
        raise AtomicLedgerError("bound_generation_invalid")
    if bound_identity["station_id"] != obs["station_id"] or bound_identity["event_date"] != obs["event_date"]:
        raise AtomicLedgerError("bound_observation_scope_mismatch")
    bound_sha = _content_hash(BOUND_SCHEMA, bound_identity)
    bound_fact_id = _verify_or_set_id(
        "bound_fact_id", bound.get("bound_fact_id"), _derive_id("boundfact", bound_identity)
    )

    plan_payload = _mapping("plan", plan)
    plan_lineage = plan_payload.get("lineage")
    if not isinstance(plan_lineage, Mapping):
        raise AtomicLedgerError("target_atomic_bundle_lineage_missing")
    plan_lineage = dict(plan_lineage)
    if not stage_rooted:
        # The source lane names which observation feed the plan consumed, so
        # it belongs to an observed root only.  Demanding it of a stage-rooted
        # plan asked P0 to declare a feed it never read.
        plan_lineage["source_lane"] = _formal_observation_source_lane(
            "plan_lineage_source_lane", plan_lineage.get("source_lane")
        )
    plan_payload["lineage"] = plan_lineage
    lane = _basket_lane(
        "strategy_lane",
        decision.get("strategy_lane", plan_payload.get("strategy_lane")),
    )
    _assert_plan_lane_contract(
        lane=lane,
        plan=plan_payload,
        plan_lineage=plan_lineage,
    )
    settlement_timezone = _settlement_timezone(
        "plan_settlement_timezone", plan_payload.get("settlement_timezone")
    )
    lineage_timezone = _settlement_timezone(
        "plan_lineage_settlement_timezone",
        plan_lineage.get("settlement_timezone"),
    )
    if (
        settlement_timezone != lineage_timezone
        or settlement_timezone != obs["settlement_timezone"]
    ):
        raise AtomicLedgerError("plan_lineage_settlement_timezone_mismatch")
    _assert_plan_observation_bound_lineage(
        plan_lineage=plan_lineage,
        observation=obs,
        bound=bound_identity,
    )
    # ``plan_id`` is an optional caller-facing label, not an input to its own
    # hash.  Excluding it avoids the usual self-referential content-address
    # trap while still rejecting a supplied ID that does not match the plan.
    plan_identity = {key: value for key, value in plan_payload.items() if key != "plan_id"}
    plan_sha = _content_hash(PLAN_SCHEMA, plan_identity)
    plan_id = _verify_or_set_id("plan_id", plan_payload.get("plan_id"), _derive_id("plan", plan_identity))
    station_id = _text("decision_station_id", decision.get("station_id", obs["station_id"]))
    event_date = _date("decision_event_date", decision.get("event_date", obs["event_date"]))
    if station_id != obs["station_id"] or event_date != obs["event_date"]:
        raise AtomicLedgerError("decision_observation_scope_mismatch")
    cycle_identity = {
        "plan_id": plan_id,
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "strategy_lane": lane,
    }
    cycle_id = _verify_or_set_id("cycle_id", decision.get("cycle_id"), _derive_id("cycle", cycle_identity))

    decided_state = _text("decided_state", decision.get("decided_state", "prepared")).lower()
    if decided_state not in {"prepared", "blocked"}:
        raise AtomicLedgerError("initial_decided_state_invalid")
    will_order = int(bool(decision.get("will_order", False)))
    # This target deployment is explicitly no-live.  The strategy's desired
    # action can remain in details.candidate_will_order, but the authority
    # table may never authorize a submit here.
    if will_order != 0:
        raise AtomicLedgerError("target_no_live_will_order_forbidden")
    reasons = _reason_codes(decision.get("reason_codes", []))
    if decided_state == "blocked" and not reasons:
        raise AtomicLedgerError("blocked_decision_reason_missing")
    details = _mapping("decision_details", decision.get("details", {}))
    lifecycle = basket_lifecycle_projection(plan_payload, required=False)
    if lifecycle is not None and details.get("basket_lifecycle") != lifecycle:
        raise AtomicLedgerError("decision_basket_lifecycle_plan_drift")
    candidate_will_order = details.get("candidate_will_order") is True
    delta_v2_audit = plan_payload.get("delta_v2_audit")
    delta_v2_authorized = (
        isinstance(delta_v2_audit, Mapping)
        and delta_v2_audit.get("authorized") is True
    )
    delta_v2_required = plan_payload.get(
        "delta_v2_required"
    ) is True
    if (
        candidate_will_order
        and delta_v2_required
        and not delta_v2_authorized
    ):
        raise AtomicLedgerError(
            "candidate_delta_v2_authorization_missing"
        )
    if (
        candidate_will_order
        and delta_v2_required
        and details.get("delta_v2_authorized") is not True
    ):
        raise AtomicLedgerError(
            "decision_delta_v2_authorization_missing"
        )

    # Resolve one immutable deadline before deriving decision/intent hashes.
    # A missing deadline is permitted only for a true no-intent blocker.
    deadline_candidates: list[Any] = []
    if target_execution_hard_deadline_at not in (None, ""):
        deadline_candidates.append(target_execution_hard_deadline_at)
    if decision.get("target_execution_hard_deadline_at") not in (None, ""):
        deadline_candidates.append(decision.get("target_execution_hard_deadline_at"))
    for raw in intents:
        if raw.get("target_execution_hard_deadline_at") not in (None, ""):
            deadline_candidates.append(raw.get("target_execution_hard_deadline_at"))
        raw_json = raw.get("intent_json")
        if isinstance(raw_json, Mapping) and raw_json.get("target_execution_hard_deadline_at") not in (None, ""):
            deadline_candidates.append(raw_json.get("target_execution_hard_deadline_at"))
    deadline: str | None = None
    if deadline_candidates:
        normalized = [_target_execution_deadline(value) for value in deadline_candidates]
        if len(set(normalized)) != 1:
            raise AtomicLedgerError("target_execution_hard_deadline_content_drift")
        deadline = normalized[0]
    if intents:
        if deadline is None:
            raise AtomicLedgerError("target_execution_hard_deadline_missing")
        _target_execution_deadline(deadline, now=created_at)

    # Economic identity, not clocked (user ruling, 2026-09-02).  ``cycle_id``
    # and ``plan_id`` are transport handles for this pass and stay on the
    # row; they are no longer part of what makes the decision itself.
    # The scope key comes from the training fact when the bundle carries
    # one -- built for prepared and blocked alike at production.py:1043 --
    # and the deadline, which was the clock in another form, is gone.
    training_fact_scope = str(
        decision_scope_key
        or (
            training_fact.get("decision_scope_key")
            if isinstance(training_fact, Mapping) else ""
        )
        or ""
    )
    decision_identity = economic_decision_identity(
        station_id=station_id,
        event_date=event_date,
        settlement_timezone=settlement_timezone,
        strategy_lane=lane,
        decided_state=decided_state,
        will_order=will_order,
        reason_codes=reasons,
        details=details,
        decision_scope_key=training_fact_scope or None,
    )
    decision_sha = _content_hash(DECISION_SCHEMA_V2, decision_identity)
    decision_id = _verify_or_set_id(
        "decision_id", decision.get("decision_id"), _derive_id("decision", decision_identity)
    )
    # The row, as distinct from the identity.  Every column the table has
    # is still written -- the transport handles and the deadline included --
    # they just no longer decide what the decision *is*.
    decision_row = {
        "cycle_id": cycle_id,
        "plan_id": plan_id,
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "strategy_lane": lane,
        "decided_state": decided_state,
        "will_order": will_order,
        "reason_codes": reasons,
        "details": details,
        "target_execution_hard_deadline_at": deadline,
        "decision_scope_key": training_fact_scope or None,
    }

    intent_rows: list[dict[str, Any]] = []
    for index, raw in enumerate(intents):
        item = {
            "bucket": _text(f"intent_{index}_bucket", raw.get("bucket")),
            "token_id": _text(f"intent_{index}_token_id", raw.get("token_id")),
            "side": _text(f"intent_{index}_side", raw.get("side")).upper(),
            "order_action": _text(f"intent_{index}_order_action", raw.get("order_action", "BUY")).upper(),
            "limit_price": _number(f"intent_{index}_limit_price", raw.get("limit_price"), positive=True),
            "requested_size": _number(f"intent_{index}_requested_size", raw.get("requested_size"), positive=True),
            "requested_notional_usdc": _number(
                f"intent_{index}_requested_notional_usdc", raw.get("requested_notional_usdc"), positive=True
            ),
            "state": _text(f"intent_{index}_state", raw.get("state", "prepared")).lower(),
            "intent_json": _mapping(f"intent_{index}_json", raw.get("intent_json", raw)),
            "target_execution_hard_deadline_at": deadline,
        }
        item["intent_json"]["target_execution_hard_deadline_at"] = deadline
        if not 0 < item["limit_price"] < 1:
            raise AtomicLedgerError(f"intent_{index}_limit_price_out_of_range")
        if item["side"] not in {"YES", "NO"} or item["order_action"] not in {"BUY", "SELL"}:
            raise AtomicLedgerError(f"intent_{index}_side_or_action_invalid")
        if item["state"] not in INTENT_STATES:
            raise AtomicLedgerError(f"intent_{index}_state_invalid")
        if (item["side"], item["order_action"]) != ("YES", "BUY"):
            raise AtomicLedgerError("basket_intent_must_be_yes_buy")
        identity = {"decision_id": decision_id, "ordinal": index, **item}
        item["order_intent_id"] = _verify_or_set_id(
            f"intent_{index}_order_intent_id", raw.get("order_intent_id"), _derive_id("intent", identity)
        )
        item["idempotency_key"] = _derive_id("intentempotency", {"order_intent_id": item["order_intent_id"]})
        item["intent_sha256"] = _content_hash(INTENT_SCHEMA, identity)
        intent_rows.append(item)

    if decided_state == "blocked" and intent_rows:
        raise AtomicLedgerError("blocked_decision_cannot_have_intents")
    total_notional = sum(float(item["requested_notional_usdc"]) for item in intent_rows)
    requested_reserve = total_notional if reserved_amount_usdc is None else _number(
        "reserved_amount_usdc", reserved_amount_usdc
    )
    if requested_reserve + 1e-9 < total_notional:
        raise AtomicLedgerError("reservation_below_intent_notional")
    if decided_state == "blocked" and requested_reserve > 1e-9:
        raise AtomicLedgerError("blocked_decision_cannot_reserve_capital")

    if execution_version_vector is not None and execution_version_vectors is not None:
        raise AtomicLedgerError("execution_version_vectors_ambiguous")
    raw_vectors: list[Mapping[str, Any] | ExecutionVersionVector] = []
    if execution_version_vectors is not None:
        raw_vectors = list(execution_version_vectors)
    elif execution_version_vector is not None:
        raw_vectors = [execution_version_vector]
    if len(raw_vectors) != len(intent_rows):
        raise AtomicLedgerError("execution_version_vector_intent_count_mismatch")
    normalized_vectors = [
        build_execution_version_vector(raw.to_dict() if isinstance(raw, ExecutionVersionVector) else raw)
        for raw in raw_vectors
    ]
    if intent_rows and normalized_vectors:
        for index, vector in enumerate(normalized_vectors):
            intent_price_id = intent_rows[index]["intent_json"].get("target_price_id")
            if intent_price_id not in (None, "") and str(intent_price_id) != vector.target_price_id:
                raise AtomicLedgerError(f"execution_version_vector_{index}_target_price_drift")

    no_execution: NoExecutionVersionPolicy | None = None
    if decided_state == "blocked" and not intent_rows:
        supplied_policy = no_execution_policy
        if supplied_policy is None:
            supplied_policy = {
                "policy": "NO_EXECUTION",
                "reason_codes": reasons,
            }
        no_execution = build_no_execution_version_policy(
            supplied_policy.to_dict() if isinstance(supplied_policy, NoExecutionVersionPolicy) else supplied_policy
        )
    elif no_execution_policy is not None:
        raise AtomicLedgerError("no_execution_policy_requires_zero_intents")
    reservation_identity = {
        "cycle_id": cycle_id,
        "strategy_lane": lane,
        "amount_usdc": round(requested_reserve, 8),
    }
    reservation_id = _derive_id("reserve", reservation_identity)
    reservation_sha = _content_hash(RESERVATION_SCHEMA, reservation_identity)
    result = {
        "schema": SCHEMA,
        "created_at": created_at,
        "awc_observation_contract": (
            None
            if stage_rooted or fact_rooted
            else dict(awc_observation_contract)
        ),
        "observed_revision_fact": (
            dict(observed_revision_fact) if fact_rooted else None
        ),
        "stage_evidence_root": (
            dict(stage_evidence_root) if stage_rooted else None
        ),
        "observation": {**obs, "observation_fact_id": observation_fact_id, "content_sha256": obs_sha},
        "bound": {**bound_identity, "bound_fact_id": bound_fact_id, "content_sha256": bound_sha},
        "plan": {"plan_id": plan_id, "plan_sha256": plan_sha, "plan_json": plan_payload},
        "cycle": {
            **cycle_identity,
            "cycle_id": cycle_id,
            "plan_sha256": plan_sha,
            "requested_reserve_usdc": requested_reserve,
            "state": "prepared",
        },
        "decision": {
            **decision_row,
            "decision_id": decision_id,
            "decision_sha256": decision_sha,
        },
        "intents": intent_rows,
        "reservation": {
            "reservation_id": reservation_id,
            "cycle_id": cycle_id,
            "strategy_lane": lane,
            "amount_usdc": requested_reserve,
            "state": "active",
            "idempotency_key": _derive_id("reservation_idempotency", {"reservation_id": reservation_id}),
            "reservation_sha256": reservation_sha,
        },
        "execution_version_vectors": [vector.to_dict() for vector in normalized_vectors],
        "no_execution_policy": no_execution.to_dict() if no_execution is not None else None,
    }
    if training_fact is not None:
        result["training_fact"] = dict(training_fact)
    return result


def apply_target_ledger_schema(conn: sqlite3.Connection, *, commit: bool = True) -> dict[str, Any]:
    """Install only the explicit M5 ledger DDL on a migration connection."""

    conn.create_function(
        TARGET_PROJECTION_AUTHORIZATION_FUNCTION,
        0,
        lambda: 0,
        deterministic=True,
    )
    upgrade_execution_vector_contract(conn)
    statement = ""
    for line in PRODUCTION_LEDGER_DDL.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            if sql:
                conn.execute(sql)
            statement = ""
    if statement.strip():
        raise AtomicLedgerError("sqlite_ledger_script_incomplete_statement")
    for table in (
        "basket_target_observation_facts",
        "basket_target_bound_facts",
        "basket_target_execution_cycles",
        "auto_trade_decisions",
        "basket_target_order_intents",
    ):
        columns = {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
        }
        if (
            table in {"basket_target_observation_facts", "basket_target_bound_facts"}
            and "truth_observation_revision_content_sha256" not in columns
        ):
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN "
                "truth_observation_revision_content_sha256 TEXT "
                "CHECK(length(truth_observation_revision_content_sha256)=64)"
            )
        # The observed root grew three columns together; only the revision
        # hash above was ever migrated, so a store built before them accepted
        # the insert's column list right up to the write and then refused it
        # ("table basket_target_observation_facts has no column named
        # observed_at_utc").  Nothing reached that write between 2026-08-01
        # and today, which is why it went unseen.
        if table == "basket_target_observation_facts":
            for column, ddl in (
                ("observed_at_utc", "observed_at_utc TEXT"),
                (
                    "awc_source_receipt_sha256",
                    "awc_source_receipt_sha256 TEXT "
                    "CHECK(awc_source_receipt_sha256 IS NULL "
                    "OR length(awc_source_receipt_sha256)=64)",
                ),
            ):
                if column not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        if table == "basket_target_execution_cycles" and "settlement_timezone" not in columns:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN settlement_timezone TEXT"
            )
        if table == "auto_trade_decisions" and "target_execution_hard_deadline_at" not in columns:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN target_execution_hard_deadline_at TEXT"
            )
        if table == "basket_target_order_intents" and "target_execution_hard_deadline_at" not in columns:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN target_execution_hard_deadline_at TEXT"
            )
    if commit:
        conn.commit()
    return target_ledger_schema_status(conn)


def target_ledger_schema_status(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    required_columns = {
        "basket_target_observation_facts": {
            "observation_fact_id", "observation_id", "station_id",
            "event_date", "settlement_timezone", "source_lane",
            "report_type", "raw_sha256", "canonical_committed_at",
            "truth_observation_revision_content_sha256",
            "content_sha256",
        },
        "basket_target_bound_facts": {
            "bound_fact_id", "bound_id", "observation_id", "station_id",
            "event_date", "truth_observation_revision_content_sha256",
            "content_sha256",
        },
        "auto_trade_decisions": {
            "decision_id", "cycle_id", "plan_id", "settlement_timezone",
            "decided_state", "will_order", "target_execution_hard_deadline_at",
            "reason_codes_json", "decision_sha256"
        },
        "basket_target_execution_cycles": {
            "cycle_id", "plan_id", "settlement_timezone", "state"
        },
        "basket_target_execution_version_vectors": {
            "order_intent_id", "cycle_id", "decision_id", "vector_policy",
            "target_price_id", "quote_observed_at", "quote_expires_at",
            "probability_sha256", "official_ws_quote_sha256", "inventory_sha256",
            "order_state_sha256", "station_policy_sha256",
            "cash_policy_decision_sha256", "strict_delta_artifact_sha256",
            "economic_policy_schema", "economic_policy_artifact_sha256",
            "vector_json", "vector_sha256",
        },
        "basket_target_order_intents": {
            "order_intent_id", "decision_id", "cycle_id", "state",
            "live_submit_authorized", "target_execution_hard_deadline_at",
            "intent_json", "intent_sha256",
        },
        "basket_target_capital_pool": {"pool_id", "balance_usdc", "reserved_usdc", "capital_epoch_id", "version"},
        "basket_target_capital_epoch_migrations": {
            "migration_id", "previous_epoch_id", "next_epoch_id",
            "runtime_contract_sha256", "content_sha256",
        },
    }
    missing_columns: dict[str, list[str]] = {}
    for table, required in required_columns.items():
        columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")} if table in tables else set()
        missing_columns[table] = sorted(required - columns)
    vector_columns = (
        {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(basket_target_execution_version_vectors)"
            )
        }
        if "basket_target_execution_version_vectors" in tables
        else set()
    )
    forbidden_vector_columns = sorted(
        vector_columns.intersection(
            {
                "posterior_version",
                "posterior_sha256",
                "snapshot_sha256",
                "quote_version",
                "inventory_version",
                "order_state_version",
            }
        )
    )
    vector_table_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='basket_target_execution_version_vectors'"
    ).fetchone()
    vector_table_sql = str(vector_table_sql_row[0] or "") if vector_table_sql_row else ""
    legacy_vector_policy_present = "LEGACY_CAS" in vector_table_sql
    missing_tables = sorted(set(TARGET_LEDGER_TABLES) - tables)
    triggers = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )
    }
    required_projection_triggers = {
        "basket_target_observation_canonical_insert_only",
        "basket_target_bound_canonical_insert_only",
    }
    missing_projection_triggers = sorted(required_projection_triggers - triggers)
    return {
        "schema": SCHEMA,
        "ok": (
            not missing_tables
            and not any(missing_columns.values())
            and not forbidden_vector_columns
            and not legacy_vector_policy_present
            and not missing_projection_triggers
        ),
        "missing_tables": missing_tables,
        "missing_columns": {key: value for key, value in missing_columns.items() if value},
        "forbidden_execution_vector_columns": forbidden_vector_columns,
        "legacy_execution_vector_policy_present": legacy_vector_policy_present,
        "missing_projection_ingress_triggers": missing_projection_triggers,
        "blocking_authority": "auto_trade_decisions",
        "places_orders": False,
        "calls_order_gateway": False,
    }


def initialize_capital_pool(
    conn: sqlite3.Connection,
    *,
    balance_usdc: float,
    capital_epoch_id: str,
    updated_at_utc: str,
    commit: bool = True,
) -> dict[str, Any]:
    """Create the single shared pool; changing an existing epoch fails closed."""

    status = target_ledger_schema_status(conn)
    if not status["ok"]:
        raise AtomicLedgerError("target_ledger_schema_incomplete")
    balance = _number("balance_usdc", balance_usdc)
    epoch = _text("capital_epoch_id", capital_epoch_id)
    updated = _timestamp("updated_at_utc", updated_at_utc)
    row = conn.execute(
        "SELECT balance_usdc,reserved_usdc,capital_epoch_id,version FROM basket_target_capital_pool WHERE pool_id=1"
    ).fetchone()
    if row is not None:
        if str(row[2]) != epoch or abs(float(row[0]) - balance) > 1e-9:
            raise AtomicLedgerError("capital_pool_epoch_or_balance_drift")
        return {"state": "already_initialized", "version": int(row[3])}
    conn.execute(
        "INSERT INTO basket_target_capital_pool(pool_id,balance_usdc,reserved_usdc,capital_epoch_id,version,updated_at) VALUES(1,?,0,?,0,?)",
        (balance, epoch, updated),
    )
    if commit:
        conn.commit()
    return {"state": "initialized", "version": 0, "balance_usdc": balance, "capital_epoch_id": epoch}


def record_writer_lock_wait(
    conn: sqlite3.Connection,
    *,
    operation: str,
    wait_ms: float,
    acquired_at_utc: str,
) -> None:
    """Upsert one bounded aggregate of a real target writer acquisition."""

    operation_text = _text("writer_operation", operation)
    wait = _number("writer_lock_wait_ms", wait_ms)
    existing = conn.execute(
        "SELECT sample_count,total_wait_ms,max_wait_ms "
        "FROM basket_target_writer_lock_wait_telemetry WHERE singleton=1"
    ).fetchone()
    if existing is None:
        sample_count = 1
        total_wait = wait
        maximum = wait
    else:
        sample_count = int(existing[0]) + 1
        total_wait = float(existing[1]) + wait
        maximum = max(float(existing[2]), wait)
    updated_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    identity = {
        "singleton": 1,
        "sample_count": sample_count,
        "total_wait_ms": round(total_wait, 6),
        "max_wait_ms": round(maximum, 6),
        "last_wait_ms": round(wait, 6),
        "last_operation": operation_text,
        "last_acquired_at": acquired_at_utc,
        "updated_at": updated_at,
    }
    sha = content_sha256({"schema": "basket_target_writer_lock_wait_telemetry_v1", **identity})
    conn.execute(
        """INSERT INTO basket_target_writer_lock_wait_telemetry(
             singleton,sample_count,total_wait_ms,max_wait_ms,last_wait_ms,
             last_operation,last_acquired_at,updated_at,content_sha256
           ) VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(singleton) DO UPDATE SET
             sample_count=excluded.sample_count,
             total_wait_ms=excluded.total_wait_ms,
             max_wait_ms=excluded.max_wait_ms,
             last_wait_ms=excluded.last_wait_ms,
             last_operation=excluded.last_operation,
             last_acquired_at=excluded.last_acquired_at,
             updated_at=excluded.updated_at,
             content_sha256=excluded.content_sha256""",
        (
            identity["singleton"], identity["sample_count"], identity["total_wait_ms"],
            identity["max_wait_ms"], identity["last_wait_ms"], identity["last_operation"],
            identity["last_acquired_at"], identity["updated_at"], sha,
        ),
    )


# Every writer in this process serializes on ``BEGIN IMMEDIATE`` against one
# SSOT file, so the question that matters during a stall is not how long a
# lane waited but which lane was holding while it waited -- and the singleton
# telemetry row answers neither: it keeps one global maximum and only the
# *last* operation name, so an 86s transaction is visible as a number with
# nothing attached to it.  Keep a per-operation aggregate in memory, split
# into the wait to acquire and the time held, so the holder can be named from
# the status file without attaching a debugger to a wedged resident.
_WRITER_LOCK_PROFILE_LOCK = threading.Lock()
_WRITER_LOCK_PROFILE: dict[str, dict[str, float]] = {}


def _note_writer_lock_sample(
    operation: str,
    *,
    wait_ms: float,
    hold_ms: float,
) -> None:
    with _WRITER_LOCK_PROFILE_LOCK:
        row = _WRITER_LOCK_PROFILE.setdefault(
            operation,
            {
                "count": 0.0,
                "wait_ms_total": 0.0,
                "wait_ms_max": 0.0,
                "hold_ms_total": 0.0,
                "hold_ms_max": 0.0,
            },
        )
        row["count"] += 1.0
        row["wait_ms_total"] += wait_ms
        row["hold_ms_total"] += hold_ms
        if wait_ms > row["wait_ms_max"]:
            row["wait_ms_max"] = wait_ms
        if hold_ms > row["hold_ms_max"]:
            row["hold_ms_max"] = hold_ms


# The lock profile says which operation held the writer and for how long.
# It cannot say which statement inside that operation was slow, and for
# ``commit_blocked_scope_decision`` -- 43 calls holding 352s, one of them for
# 45s -- that is the only remaining question.  Same shape as the worker's
# span store, kept here because these are module functions with no worker to
# hang state on; the worker merges both into one report.
_LEDGER_SPAN_LOCK = threading.Lock()
_LEDGER_SPANS: dict[str, dict[str, float]] = {}


def note_ledger_span(name: str, elapsed_ms: float) -> None:
    """Record one already-measured span, for spans not shaped as a block."""

    with _LEDGER_SPAN_LOCK:
        row = _LEDGER_SPANS.setdefault(
            name, {"count": 0.0, "total_ms": 0.0, "max_ms": 0.0}
        )
        row["count"] += 1.0
        row["total_ms"] += elapsed_ms
        if elapsed_ms > row["max_ms"]:
            row["max_ms"] = elapsed_ms


@contextmanager
def ledger_span(name: str) -> Iterator[None]:
    """Accumulate one repeated ledger phase into the cycle's span report."""

    started = time.perf_counter()
    try:
        yield
    finally:
        note_ledger_span(name, (time.perf_counter() - started) * 1000.0)


def ledger_span_profile() -> dict[str, dict[str, Any]]:
    with _LEDGER_SPAN_LOCK:
        snapshot = {name: dict(row) for name, row in _LEDGER_SPANS.items()}
    return {
        name: {
            "count": int(row["count"]),
            "total_ms": round(row["total_ms"], 1),
            "max_ms": round(row["max_ms"], 1),
            "mean_ms": round(row["total_ms"] / max(1.0, row["count"]), 1),
            "slowest": "",
        }
        for name, row in sorted(
            snapshot.items(), key=lambda item: -item[1]["total_ms"]
        )
    }


def reset_ledger_spans() -> None:
    with _LEDGER_SPAN_LOCK:
        _LEDGER_SPANS.clear()


def note_writer_lock_sample(
    operation: str,
    *,
    wait_ms: float,
    hold_ms: float,
) -> None:
    """Record a write-lock acquisition taken outside this module.

    ``production_worker._writer_transaction`` opens its own
    ``BEGIN IMMEDIATE`` against the same single-writer file and was invisible
    here, so the profile named only the ledger's own transactions and made
    the resident's largest lock holder -- the P1 scope write -- look like it
    did not exist.  One lock, one profile.
    """

    _note_writer_lock_sample(operation, wait_ms=wait_ms, hold_ms=hold_ms)


def writer_lock_profile(*, top: int = 8) -> dict[str, Any]:
    """Report the busiest write-lock operations since the last reset."""

    with _WRITER_LOCK_PROFILE_LOCK:
        snapshot = {
            operation: dict(row)
            for operation, row in _WRITER_LOCK_PROFILE.items()
        }
    ranked = sorted(
        snapshot.items(),
        key=lambda item: item[1]["hold_ms_total"],
        reverse=True,
    )[: max(1, top)]
    return {
        "schema": "basket_target_writer_lock_profile_v1",
        "operation_count": len(snapshot),
        "hold_ms_total": round(
            sum(row["hold_ms_total"] for row in snapshot.values()), 1
        ),
        "wait_ms_total": round(
            sum(row["wait_ms_total"] for row in snapshot.values()), 1
        ),
        "operations": [
            {
                "operation": operation,
                "count": int(row["count"]),
                "hold_ms_total": round(row["hold_ms_total"], 1),
                "hold_ms_max": round(row["hold_ms_max"], 1),
                "wait_ms_total": round(row["wait_ms_total"], 1),
                "wait_ms_max": round(row["wait_ms_max"], 1),
            }
            for operation, row in ranked
        ],
    }


def reset_writer_lock_profile() -> None:
    with _WRITER_LOCK_PROFILE_LOCK:
        _WRITER_LOCK_PROFILE.clear()


@contextmanager
def _atomic_transaction(conn: sqlite3.Connection, *, operation: str = "target_atomic") -> Iterator[None]:
    own = not conn.in_transaction
    savepoint = "basket_target_m5_atomic"
    lock_started_ns = time.perf_counter_ns()
    acquired_at_utc = ""
    if own:
        conn.execute("BEGIN IMMEDIATE")
        acquired_at_utc = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    else:
        conn.execute(f"SAVEPOINT {savepoint}")
    # ``BEGIN IMMEDIATE`` has returned, so the lock is in hand: everything
    # after this instant is hold, not wait.  Timing the whole body as "wait"
    # is what let a 4.6s transaction report itself as a 4.6s lock wait and
    # hid the fact that the resident is its own contention.
    acquired_ns = time.perf_counter_ns()
    try:
        yield
    except Exception:
        if own:
            conn.rollback()
        else:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
        raise
    else:
        if own:
            wait_ms = (acquired_ns - lock_started_ns) / 1_000_000.0
            _note_writer_lock_sample(
                operation,
                wait_ms=wait_ms,
                hold_ms=(time.perf_counter_ns() - acquired_ns) / 1_000_000.0,
            )
            record_writer_lock_wait(
                conn,
                operation=operation,
                wait_ms=wait_ms,
                acquired_at_utc=acquired_at_utc,
            )
            conn.commit()
        else:
            conn.execute(f"RELEASE {savepoint}")


def _fault(fault_after: str | None, point: str) -> None:
    if fault_after == point:
        raise InjectedAtomicFault(point)


def _assert_existing_or_absent(conn: sqlite3.Connection, table: str, key: str, value: str, sha_column: str, sha: str) -> bool:
    row = conn.execute(f"SELECT {sha_column} FROM {table} WHERE {key}=?", (value,)).fetchone()
    if row is None:
        return False
    if str(row[0] or "") != sha:
        raise AtomicLedgerError(f"{table}_content_address_drift")
    return True


def _ensure_pool(conn: sqlite3.Connection) -> tuple[float, float, str, int]:
    row = conn.execute(
        "SELECT balance_usdc,reserved_usdc,capital_epoch_id,version FROM basket_target_capital_pool WHERE pool_id=1"
    ).fetchone()
    if row is None:
        raise AtomicLedgerError("capital_pool_missing")
    return float(row[0]), float(row[1]), str(row[2]), int(row[3])


def _reserve(conn: sqlite3.Connection, row: Mapping[str, Any], *, created_at: str) -> None:
    balance, reserved, _epoch, _version = _ensure_pool(conn)
    amount = float(row["amount_usdc"])
    if amount > balance - reserved + 1e-9:
        raise AtomicLedgerError("capital_pool_insufficient")
    cur = conn.execute(
        "UPDATE basket_target_capital_pool SET reserved_usdc=reserved_usdc+?,version=version+1,updated_at=? "
        "WHERE pool_id=1 AND balance_usdc-reserved_usdc>=?",
        (amount, created_at, amount),
    )
    if cur.rowcount != 1:
        raise AtomicLedgerError("capital_pool_cas_conflict")
    conn.execute(
        "INSERT INTO basket_target_capital_reservations(reservation_id,cycle_id,strategy_lane,amount_usdc,state,idempotency_key,reservation_sha256,release_reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            row["reservation_id"], row["cycle_id"], row["strategy_lane"], amount, row["state"],
            row["idempotency_key"], row["reservation_sha256"], None, created_at, created_at,
        ),
    )


def _execution_vector_rows(
    bundle: Mapping[str, Any],
    *,
    cycle_id: str,
    decision_id: str,
) -> list[dict[str, Any]]:
    """Normalize the vector binding rows at the SQL write boundary."""

    intents = [dict(item) for item in bundle.get("intents", [])]
    raw_vectors = bundle.get("execution_version_vectors") or []
    if not isinstance(raw_vectors, Sequence) or isinstance(raw_vectors, (str, bytes, bytearray)):
        raise AtomicLedgerError("execution_version_vectors_list_required")
    if len(raw_vectors) != len(intents):
        raise AtomicLedgerError("execution_version_vector_intent_count_mismatch")
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_vectors):
        if not isinstance(raw, Mapping):
            raise AtomicLedgerError("execution_version_vector_mapping_required")
        vector = build_execution_version_vector(raw)
        intent_id = str(intents[index].get("order_intent_id") or "").strip()
        if not intent_id:
            raise AtomicLedgerError("execution_version_vector_intent_missing")
        rows.append({
            "order_intent_id": intent_id,
            "cycle_id": cycle_id,
            "decision_id": decision_id,
            "training_fact_id": None,
            "vector_policy": "EXECUTION",
            "strict_delta_artifact_sha256": None,
            **vector.to_dict(),
            "vector_json": canonical_json({"schema": vector.schema, **vector.to_dict()}),
            "vector_sha256": vector.vector_sha256,
        })
    policy_raw = bundle.get("no_execution_policy")
    if policy_raw is not None:
        if intents:
            raise AtomicLedgerError("no_execution_policy_requires_zero_intents")
        if str(bundle.get("decision", {}).get("decided_state") or "").lower() != "blocked":
            raise AtomicLedgerError("no_execution_policy_requires_blocked_decision")
        if not isinstance(policy_raw, Mapping):
            raise AtomicLedgerError("no_execution_policy_mapping_required")
        policy = build_no_execution_version_policy(policy_raw)
        rows.append({
            "order_intent_id": None,
            "cycle_id": cycle_id,
            "decision_id": decision_id,
            "training_fact_id": None,
            "vector_policy": "NO_EXECUTION",
            "target_price_id": None,
            "quote_observed_at": None,
            "quote_expires_at": None,
            "probability_sha256": None,
            "official_ws_quote_sha256": None,
            "inventory_sha256": None,
            "order_state_sha256": None,
            "station_policy_sha256": None,
            "cash_policy_decision_sha256": None,
            "strict_delta_artifact_sha256": None,
            "economic_policy_schema": None,
            "economic_policy_artifact_sha256": None,
            "vector_json": canonical_json(policy.to_dict()),
            "vector_sha256": policy.policy_sha256,
        })
    return rows


def _sqlite_text_or_none(value: Any) -> Any:
    """Normalize SQLite TEXT adapters without changing its value.

    ``sqlite3.Row`` is still a positional sequence, but a connection may use
    a bytes ``text_factory`` or a driver-specific TEXT adapter.  Decode only
    valid UTF-8 bytes; leave every other type untouched so a malformed legacy
    row fails closed at the contract comparison below.
    """

    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value
    return value


def _assert_existing_blocked_no_execution_vector(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    decision_id: str,
    expected_policy: NoExecutionVersionPolicy,
    expected_training_fact_id: str | None = None,
) -> None:
    """Verify the exact current NO_EXECUTION row at a blocked-cycle replay.

    The policy digest is the content address.  JSON key order/whitespace is
    not content, so it is parsed and revalidated before comparison.  The
    schema/version and the complete NULL execution shape remain hard gates:
    an older row without the current policy schema, an execution-shaped row,
    or a training-fact binding different from the exact requested fact
    requires explicit offline migration and cannot be silently replayed.
    """

    rows = conn.execute(
        "SELECT order_intent_id,cycle_id,decision_id,training_fact_id,vector_policy,"
        "target_price_id,quote_observed_at,quote_expires_at,probability_sha256,"
        "official_ws_quote_sha256,inventory_sha256,order_state_sha256,"
        "station_policy_sha256,cash_policy_decision_sha256,strict_delta_artifact_sha256,"
        "economic_policy_schema,economic_policy_artifact_sha256,"
        "vector_json,vector_sha256 "
        "FROM basket_target_execution_version_vectors WHERE cycle_id=?",
        (cycle_id,),
    ).fetchall()

    def drift(reason: str) -> None:
        raise AtomicLedgerError(
            "blocked_cycle_no_execution_vector_content_address_drift:" + reason
        )

    if len(rows) != 1:
        drift("row_count")
    row = rows[0]
    values = [_sqlite_text_or_none(value) for value in tuple(row)]
    (
        order_intent_id,
        stored_cycle_id,
        stored_decision_id,
        training_fact_id,
        vector_policy,
        target_price_id,
        quote_observed_at,
        quote_expires_at,
        probability_sha256,
        official_ws_quote_sha256,
        inventory_sha256,
        order_state_sha256,
        station_policy_sha256,
        cash_policy_decision_sha256,
        strict_delta_artifact_sha256,
        economic_policy_schema,
        economic_policy_artifact_sha256,
        vector_json,
        vector_sha256,
    ) = values

    if (
        order_intent_id is not None
        or stored_cycle_id != cycle_id
        or stored_decision_id != decision_id
        or training_fact_id != expected_training_fact_id
        or vector_policy != "NO_EXECUTION"
        or target_price_id is not None
        or quote_observed_at is not None
        or quote_expires_at is not None
        or any(
            value is not None
            for value in (
                probability_sha256,
                official_ws_quote_sha256,
                inventory_sha256,
                order_state_sha256,
                station_policy_sha256,
                cash_policy_decision_sha256,
                strict_delta_artifact_sha256,
                economic_policy_schema,
                economic_policy_artifact_sha256,
            )
        )
    ):
        drift("null_shape_or_identity")
    if not isinstance(vector_json, str) or not isinstance(vector_sha256, str):
        drift("sqlite_text_type")

    def reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, value in pairs:
            if key in payload:
                raise ValueError("duplicate_json_key")
            payload[key] = value
        return payload

    try:
        stored_payload = json.loads(
            vector_json,
            object_pairs_hook=reject_duplicate_json_keys,
        )
    except (TypeError, ValueError):
        drift("json_invalid")
    if not isinstance(stored_payload, Mapping):
        drift("json_shape")
    if stored_payload.get("schema") != NO_EXECUTION_POLICY_SCHEMA:
        drift("policy_version_migration_required")
    try:
        stored_policy = build_no_execution_version_policy(stored_payload)
    except ContractError:
        drift("policy_invalid")
    if stored_policy.to_dict() != expected_policy.to_dict():
        drift("policy_content")
    if vector_sha256.lower() != expected_policy.policy_sha256:
        drift("policy_sha256")


def _build_stage_zero_training_fact(
    conn: sqlite3.Connection,
    *,
    anchor: Mapping[str, Any],
    cycle_id: str,
    plan_id: str,
    plan_sha256: str,
    decision_id: str,
    decision_sha256: str,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    no_execution_policy: NoExecutionVersionPolicy,
    created_at_utc: str,
) -> dict[str, Any]:
    """Build the P0/P0-M fact that is bound to one zero-execution cycle.

    This deliberately stays out of the P1 M5 observation/bound writer.  The
    pre-final fact binds the complete compiled decision to its exact official
    WS, inventory, order-state and NO_EXECUTION policy under the same SQLite
    transaction which commits the blocked decision and its vector.
    """

    compiled_plan = _mapping(
        "stage_zero_training_compiled_plan", anchor.get("compiled_plan")
    )
    if compiled_plan.get("schema") != "bayes_basket_production_plan_v1":
        raise AtomicLedgerError("stage_zero_training_compiled_plan_schema_invalid")
    audit = _mapping(
        "stage_zero_training_delta_v2_audit",
        compiled_plan.get("delta_v2_audit"),
    )
    if audit.get("authorized") is not True:
        raise AtomicLedgerError("stage_zero_training_delta_v2_not_authorized")
    market_snapshot = _mapping(
        "stage_zero_training_market_snapshot",
        compiled_plan.get("production_market_snapshot"),
    )
    plan_json = _mapping(
        "stage_zero_training_plan", compiled_plan.get("plan")
    )
    atomic_bundle = {
        "cycle": {
            "cycle_id": cycle_id,
            "plan_id": plan_id,
            "station_id": station_id,
            "event_date": event_date,
            "settlement_timezone": settlement_timezone,
            "strategy_lane": "basket",
        },
        "decision": {
            "decision_id": decision_id,
            "cycle_id": cycle_id,
            "plan_id": plan_id,
            "decision_sha256": decision_sha256,
        },
        # A stage-zero cycle's ledger plan is intentionally a small blocker
        # record.  The full economic plan remains in the immutable compiled
        # artifact used for features; its content-addressed blocker plan SHA
        # is still the fact's exact decision-plan reference.
        "plan": {
            "plan_id": plan_id,
            "plan_sha256": plan_sha256,
            "plan_json": plan_json,
        },
        "intents": [],
        "execution_version_vectors": [],
        "no_execution_policy": no_execution_policy.to_dict(),
    }
    try:
        from .delta_v2_training import build_stage_zero_delta_v2_training_fact

        return build_stage_zero_delta_v2_training_fact(
            conn,
            compiled_plan=compiled_plan,
            market_snapshot=market_snapshot,
            atomic_bundle=atomic_bundle,
            created_at_utc=created_at_utc,
        )
    except ContractError as exc:
        raise AtomicLedgerError(str(exc)) from exc


def _assert_training_fact_scope(
    fact: Mapping[str, Any],
    *,
    cycle_id: str,
    decision_id: str,
) -> None:
    if str(fact.get("cycle_id") or "") != cycle_id:
        raise AtomicLedgerError("training_fact_cycle_lineage_mismatch")
    if str(fact.get("decision_id") or "") != decision_id:
        raise AtomicLedgerError("training_fact_decision_lineage_mismatch")
    if not str(fact.get("fact_id") or "").strip():
        raise AtomicLedgerError("training_fact_id_missing")
    if not str(fact.get("content_sha256") or "").strip():
        raise AtomicLedgerError("training_fact_content_sha256_missing")


def _insert_training_fact(conn: sqlite3.Connection, fact: Mapping[str, Any]) -> str:
    """Append through the one canonical durable-forward-fact validator.

    The atomic ledger owns the transaction, while the forward fact validator
    owns the fact schema, content address, idempotency and decision-scope
    conflict rules.  Keeping the import local avoids making those contracts
    mutually import each other at module load time.
    """

    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='basket_policy_training_fact'"
    ).fetchone()
    if table_exists is None:
        raise AtomicLedgerError("training_fact_table_missing")
    try:
        if fact.get("schema") != "delta_basket_score_training_fact_v2":
            raise AtomicLedgerError(
                "production_training_fact_schema_not_delta_v2"
            )
        from .delta_v2_training import commit_delta_v2_training_fact

        receipt = commit_delta_v2_training_fact(conn, fact=fact)
    except ContractError as exc:
        raise AtomicLedgerError(str(exc)) from exc
    except sqlite3.Error as exc:
        raise AtomicLedgerError("training_fact_canonical_write_failed") from exc
    state = str(receipt.get("state") or "")
    if state not in {"committed", "already_committed"}:
        raise AtomicLedgerError("training_fact_commit_state_invalid")
    return state


def _assert_existing_cycle_training_fact_binding_timed(
    conn: sqlite3.Connection,
    **kwargs: Any,
) -> None:
    """Timed passthrough, so the replay path's cost is attributable."""

    with ledger_span("blocked_txn_fact_binding"):
        _assert_existing_cycle_training_fact_binding(conn, **kwargs)


def _assert_existing_cycle_training_fact_binding(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    decision_id: str,
    training_fact: Mapping[str, Any] | None,
    vector_rows: Sequence[Mapping[str, Any]],
) -> None:
    """Require an existing cycle's fact and vector bindings to agree exactly."""

    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND "
        "name='basket_policy_training_fact'"
    ).fetchone()
    vector_fact_ids = {
        str(row.get("training_fact_id") or "")
        for row in vector_rows
        if row.get("training_fact_id") is not None
    }
    if table_exists is None:
        if training_fact is not None or vector_fact_ids:
            raise AtomicLedgerError("training_fact_canonical_table_missing")
        return

    stored = conn.execute(
        "SELECT fact_id,content_sha256,decision_scope_key "
        "FROM basket_policy_training_fact "
        "WHERE cycle_id=? AND decision_id=? ORDER BY fact_id",
        (cycle_id, decision_id),
    ).fetchall()
    if training_fact is None:
        if stored or vector_fact_ids:
            raise AtomicLedgerError("cycle_existing_training_fact_presence_drift")
        return

    expected = (
        str(training_fact.get("fact_id") or ""),
        str(training_fact.get("content_sha256") or ""),
        str(training_fact.get("decision_scope_key") or ""),
    )
    if not all(expected):
        raise AtomicLedgerError("training_fact_binding_fields_missing")
    if len(stored) != 1 or tuple(str(value or "") for value in stored[0]) != expected:
        raise AtomicLedgerError("cycle_existing_training_fact_binding_drift")
    if vector_fact_ids != {expected[0]}:
        raise AtomicLedgerError("cycle_existing_training_fact_vector_binding_drift")


def _assert_existing_cycle_weather_fact_binding(
    conn: sqlite3.Connection,
    *,
    observation: Mapping[str, Any],
    bound: Mapping[str, Any],
) -> None:
    """Fail closed if an idempotent replay has lost either immutable fact."""

    observation_row = conn.execute(
        "SELECT observation_fact_id,settlement_timezone,"
        "truth_observation_revision_content_sha256,content_sha256 FROM "
        "basket_target_observation_facts WHERE observation_id=?",
        (str(observation.get("observation_id") or ""),),
    ).fetchone()
    expected_observation = (
        str(observation.get("observation_fact_id") or ""),
        str(observation.get("settlement_timezone") or ""),
        str(observation.get("truth_observation_revision_content_sha256") or ""),
        str(observation.get("content_sha256") or ""),
    )
    if (
        observation_row is None
        or tuple(str(value or "") for value in observation_row)
        != expected_observation
    ):
        raise AtomicLedgerError(
            "cycle_existing_observation_fact_binding_drift"
        )

    bound_row = conn.execute(
        "SELECT bound_fact_id,observation_id,"
        "truth_observation_revision_content_sha256,content_sha256 FROM "
        "basket_target_bound_facts WHERE bound_id=?",
        (str(bound.get("bound_id") or ""),),
    ).fetchone()
    expected_bound = (
        str(bound.get("bound_fact_id") or ""),
        str(observation.get("observation_id") or ""),
        str(observation.get("truth_observation_revision_content_sha256") or ""),
        str(bound.get("content_sha256") or ""),
    )
    if (
        bound_row is None
        or tuple(str(value or "") for value in bound_row)
        != expected_bound
    ):
        raise AtomicLedgerError(
            "cycle_existing_bound_fact_binding_drift"
        )


def _assert_existing_cycle_stage_evidence_binding(
    conn: sqlite3.Connection,
    *,
    observation: Mapping[str, Any],
    bound: Mapping[str, Any],
) -> None:
    """Fail closed if a stage-rooted replay has lost its persisted evidence.

    The immutable pair for a pre-observation cycle lives in the stage tables:
    the root row named by the plan and the soft bound derived from the same
    envelope.  A replay that cannot find both with matching content hashes is
    citing evidence that was never written, and must not look idempotent.
    """

    root_row = conn.execute(
        "SELECT content_sha256 FROM basket_target_stage_evidence_roots "
        "WHERE evidence_root_id=?",
        (str(observation.get("observation_id") or ""),),
    ).fetchone()
    if root_row is None or str(root_row[0]) != str(
        observation.get("evidence_root_content_sha256") or ""
    ):
        raise AtomicLedgerError(
            "cycle_existing_stage_evidence_root_binding_drift"
        )
    bound_row = conn.execute(
        "SELECT evidence_root_id FROM basket_target_stage_bound_facts "
        "WHERE bound_id=?",
        (str(bound.get("bound_id") or ""),),
    ).fetchone()
    if bound_row is None or str(bound_row[0]) != str(
        observation.get("observation_id") or ""
    ):
        raise AtomicLedgerError(
            "cycle_existing_stage_bound_binding_drift"
        )


def _stage_canonical_target_projections(
    conn: sqlite3.Connection,
    *,
    observation: Mapping[str, Any],
    bound: Mapping[str, Any],
    revision_sha: str,
    created_at: str,
    fault_after: str | None,
) -> None:
    """Write target projections only after exact canonical revision binding."""

    gate = {"enabled": False}
    conn.create_function(
        TARGET_PROJECTION_AUTHORIZATION_FUNCTION,
        0,
        lambda: 1 if gate["enabled"] else 0,
        deterministic=True,
    )
    try:
        gate["enabled"] = True
        existing = conn.execute(
            "SELECT settlement_timezone,"
            "truth_observation_revision_content_sha256,content_sha256 FROM "
            "basket_target_observation_facts WHERE observation_id=?",
            (observation["observation_id"],),
        ).fetchone()
        if existing is not None:
            if tuple(str(value or "") for value in existing) != (
                str(observation["settlement_timezone"]),
                revision_sha,
                str(observation["content_sha256"]),
            ):
                raise AtomicLedgerError(
                    "basket_target_observation_facts_content_address_drift"
                )
        else:
            conn.execute(
                "INSERT INTO basket_target_observation_facts("
                "observation_fact_id,observation_id,station_id,event_date,"
                "settlement_timezone,source_lane,report_type,observed_at_utc,"
                "raw_sha256,awc_source_receipt_sha256,canonical_committed_at,"
                "truth_observation_revision_content_sha256,content_sha256,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    observation["observation_fact_id"], observation["observation_id"],
                    observation["station_id"], observation["event_date"],
                    observation["settlement_timezone"], observation["source_lane"],
                    observation["report_type"], observation["observed_at_utc"],
                    observation["raw_sha256"], observation["awc_source_receipt_sha256"],
                    observation["canonical_committed_at"], revision_sha,
                    observation["content_sha256"], created_at,
                ),
            )
        _fault(fault_after, "projection")
        if not _assert_existing_or_absent(
            conn, "basket_target_bound_facts", "bound_id", bound["bound_id"],
            "content_sha256", bound["content_sha256"],
        ):
            conn.execute(
                "INSERT INTO basket_target_bound_facts("
                "bound_fact_id,bound_id,observation_id,station_id,event_date,"
                "bound_generation,hard_lower_bound,bound_json,"
                "truth_observation_revision_content_sha256,content_sha256,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    bound["bound_fact_id"], bound["bound_id"], bound["observation_id"],
                    bound["station_id"], bound["event_date"], bound["bound_generation"],
                    bound["hard_lower_bound"], canonical_json(bound["bound_json"]),
                    revision_sha, bound["content_sha256"], created_at,
                ),
            )
        _fault(fault_after, "bound_projection")
    finally:
        gate["enabled"] = False
        conn.create_function(
            TARGET_PROJECTION_AUTHORIZATION_FUNCTION,
            0,
            lambda: 0,
            deterministic=True,
        )


def commit_target_atomic_bundle(
    conn: sqlite3.Connection,
    *,
    bundle: Mapping[str, Any],
    fault_after: str | None = None,
    execution_version_vector: Mapping[str, Any] | None = None,
    execution_version_vectors: Sequence[Mapping[str, Any]] | None = None,
    no_execution_policy: Mapping[str, Any] | None = None,
    training_fact: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Commit all M5 facts or none; repeated identical commits are idempotent."""

    if any(value is not None for value in (execution_version_vector, execution_version_vectors, no_execution_policy, training_fact)):
        mutable_bundle = dict(bundle)
        if execution_version_vector is not None:
            if execution_version_vectors is not None:
                raise AtomicLedgerError("execution_version_vectors_ambiguous")
            mutable_bundle["execution_version_vectors"] = [execution_version_vector]
        elif execution_version_vectors is not None:
            mutable_bundle["execution_version_vectors"] = list(execution_version_vectors)
        if no_execution_policy is not None:
            mutable_bundle["no_execution_policy"] = no_execution_policy
        if training_fact is not None:
            mutable_bundle["training_fact"] = training_fact
        bundle = mutable_bundle

    status = target_ledger_schema_status(conn)
    if not status["ok"]:
        raise AtomicLedgerError("target_ledger_schema_incomplete")
    if bundle.get("schema") != SCHEMA:
        raise AtomicLedgerError("atomic_bundle_schema_invalid")
    # Re-normalization is intentionally not implicit: callers must use the
    # builder so that stored hashes and IDs are checked before entering SQL.
    required = (
        "awc_observation_contract", "observation", "bound", "plan", "cycle",
        "decision", "intents", "reservation",
    )
    if any(key not in bundle for key in required):
        raise AtomicLedgerError("atomic_bundle_incomplete")
    obs = dict(bundle["observation"])
    # A stage-rooted bundle (P0/P0-M) cites the evidence it was compiled from;
    # there is no canonical AWC report yet, so the METAR-shaped checks below
    # would only be satisfiable by fabricating one.  The stage checks that
    # replace them live in the builder and in _assert_plan_observation_bound_
    # lineage; the commit boundary re-verifies content addresses either way.
    stage_rooted = bundle.get("stage_evidence_root") is not None
    if not stage_rooted:
        obs["source_lane"] = _formal_observation_source_lane(
            "observation_source_lane", obs.get("source_lane")
        )
    bound = dict(bundle["bound"])
    fact_rooted = bundle.get("observed_revision_fact") is not None
    if stage_rooted:
        revision_sha = ""
    else:
        revision_sha = _sha(
            "truth_observation_revision_content_sha256",
            obs.get("truth_observation_revision_content_sha256"),
        )
        if fact_rooted:
            # The fact is already in the store, so there is nothing here to
            # stage -- only to agree with.  The named fact must hash to the
            # sha the chain cites, and the row lookup below is what proves
            # the store holds it; a report the lane has not yet identified
            # simply fails closed here rather than being written on the way
            # past.
            try:
                named_fact = (
                    CanonicalSubstituteObservationRevisionFact.from_mapping(
                        bundle["observed_revision_fact"]
                    )
                    if is_substitute_revision_mapping(
                        bundle["observed_revision_fact"]
                    )
                    else CanonicalAWCObservationRevisionFact.from_mapping(
                        bundle["observed_revision_fact"]
                    )
                )
            except ContractError as exc:
                raise AtomicLedgerError(
                    "atomic_awc_observation_revision_fact_invalid"
                ) from exc
            if named_fact.content_sha256 != revision_sha:
                raise AtomicLedgerError("atomic_awc_revision_reference_not_exact")
        else:
            try:
                awc_contract = CanonicalAWCObservationContract.from_mapping(
                    bundle["awc_observation_contract"]
                )
            except ContractError as exc:
                raise AtomicLedgerError("atomic_awc_observation_contract_invalid") from exc
            revisions = [
                fact for fact in awc_contract.revision_facts
                if fact.content_sha256 == revision_sha
            ]
            if len(revisions) != 1:
                raise AtomicLedgerError("atomic_awc_revision_reference_not_exact")
        if bound.get("truth_observation_revision_content_sha256") != revision_sha:
            raise AtomicLedgerError("atomic_bound_revision_reference_mismatch")
    cycle = dict(bundle["cycle"])
    decision = dict(bundle["decision"])
    intents = [dict(item) for item in bundle["intents"]]
    reservation = dict(bundle["reservation"])
    training_fact = bundle.get("training_fact")
    if training_fact is not None and not isinstance(training_fact, Mapping):
        raise AtomicLedgerError("training_fact_mapping_required")
    created_at = _timestamp("bundle_created_at", bundle.get("created_at"))
    cycle_timezone = _settlement_timezone(
        "cycle_settlement_timezone", cycle.get("settlement_timezone")
    )
    if not stage_rooted:
        # Opening before the day exists is the entire purpose of a stage-rooted
        # position; only observation-rooted commits must wait for the day.
        _assert_event_day_started(
            str(cycle.get("event_date") or ""),
            settlement_timezone=cycle_timezone,
            as_of_utc=created_at,
        )

    # Do not trust a previously normalized mapping merely because it carries
    # a hash.  Recompute every content address at the write boundary so a
    # caller cannot mutate details after planning and still reuse a cycle ID.
    observation_timezone = _settlement_timezone(
        "observation_settlement_timezone", obs.get("settlement_timezone")
    )
    obs_identity = {key: value for key, value in obs.items() if key not in {"observation_fact_id", "content_sha256"}}
    if obs.get("content_sha256") != _content_hash(
        STAGE_EVIDENCE_ROOT_SCHEMA if stage_rooted else OBSERVATION_SCHEMA,
        obs_identity,
    ):
        raise AtomicLedgerError("observation_content_address_drift")
    if obs.get("observation_fact_id") != (
        # The builder names a stage projection by its evidence root rather
        # than minting a separate obsfact id for a report that does not exist.
        obs.get("observation_id")
        if stage_rooted
        else _derive_id("obsfact", obs_identity)
    ):
        raise AtomicLedgerError("observation_fact_id_content_address_drift")
    bound_identity = {key: value for key, value in bound.items() if key not in {"bound_fact_id", "content_sha256"}}
    if bound.get("content_sha256") != _content_hash(BOUND_SCHEMA, bound_identity):
        raise AtomicLedgerError("bound_content_address_drift")
    plan_identity = {key: value for key, value in dict(bundle["plan"].get("plan_json") or {}).items() if key != "plan_id"}
    plan_lineage = plan_identity.get("lineage")
    if not isinstance(plan_lineage, Mapping):
        raise AtomicLedgerError("target_atomic_commit_lineage_missing")
    plan_lineage = dict(plan_lineage)
    if not stage_rooted:
        plan_lineage["source_lane"] = _formal_observation_source_lane(
            "plan_lineage_source_lane", plan_lineage.get("source_lane")
        )
    plan_identity["lineage"] = plan_lineage
    # The intent's live flag, from the plan that authorized it.  This was the
    # literal 0 in the INSERT below -- the fourth of the same family as the
    # compiled plan's own hard-coded False, and the one furthest downstream.
    # It is the marker everything uses to tell a live intent from a no-live
    # one: the ledger keys its live book on it, the dashboard's live card
    # counts rows with it set, and the go-live migration widened this table's
    # CHECK precisely so it could hold 1.  Written as 0 unconditionally, a
    # live deployment mints only no-live-looking intents and reports itself
    # as having placed nothing.
    #
    # The plan is the authority, not the execution mode: an intent is live
    # because the decision that produced it carried live authority, and a
    # plan compiled without it stays 0 whatever the process thinks it is.
    intent_live_submit_authorized = (
        1 if plan_identity.get("live_submit_authorized") is True else 0
    )
    if cycle.get("plan_sha256") != _content_hash(PLAN_SCHEMA, plan_identity):
        raise AtomicLedgerError("plan_content_address_drift")
    cycle_lane = _basket_lane("cycle_strategy_lane", cycle.get("strategy_lane"))
    _assert_plan_lane_contract(
        lane=cycle_lane,
        plan=plan_identity,
        plan_lineage=plan_lineage,
        commit_boundary=True,
    )
    plan_timezone = _settlement_timezone(
        "plan_settlement_timezone", plan_identity.get("settlement_timezone")
    )
    lineage_timezone = _settlement_timezone(
        "plan_lineage_settlement_timezone",
        plan_lineage.get("settlement_timezone"),
    )
    if (
        plan_timezone != lineage_timezone
        or plan_timezone != observation_timezone
        or plan_timezone != cycle_timezone
        or _settlement_timezone(
            "decision_settlement_timezone",
            decision.get("settlement_timezone"),
        )
        != cycle_timezone
    ):
        raise AtomicLedgerError("atomic_settlement_timezone_mismatch")
    _assert_plan_observation_bound_lineage(
        plan_lineage=plan_lineage,
        observation=obs,
        bound=bound,
    )
    expected_plan_id = _derive_id("plan", plan_identity)
    if bundle["plan"].get("plan_id") != expected_plan_id or cycle.get("plan_id") != expected_plan_id:
        raise AtomicLedgerError("plan_id_content_address_drift")
    cycle_identity = {
        "plan_id": expected_plan_id,
        "station_id": cycle.get("station_id"),
        "event_date": cycle.get("event_date"),
        "settlement_timezone": cycle_timezone,
        "strategy_lane": cycle.get("strategy_lane"),
    }
    if cycle.get("cycle_id") != _derive_id("cycle", cycle_identity):
        raise AtomicLedgerError("cycle_id_content_address_drift")
    deadline = _target_execution_deadline(
        decision.get("target_execution_hard_deadline_at"),
        now=created_at if intents else None,
        required=bool(intents),
    )
    if any(item.get("target_execution_hard_deadline_at") != deadline for item in intents):
        raise AtomicLedgerError("target_execution_hard_deadline_content_drift")
    # Re-derived the way it was minted: from what the decision is made of,
    # with the row's own scope key.  The transport handles and the deadline
    # are on the row but not in the address.
    decision_identity = economic_decision_identity(
        station_id=str(decision.get("station_id") or ""),
        event_date=str(decision.get("event_date") or ""),
        settlement_timezone=str(decision.get("settlement_timezone") or ""),
        strategy_lane=str(decision.get("strategy_lane") or ""),
        decided_state=str(decision.get("decided_state") or ""),
        will_order=int(decision.get("will_order") or 0),
        reason_codes=_reason_codes(decision.get("reason_codes")),
        details=_mapping("decision_details", decision.get("details")),
        decision_scope_key=(
            str(decision.get("decision_scope_key") or "") or None
        ),
    )
    if decision.get("decision_sha256") != _content_hash(DECISION_SCHEMA_V2, decision_identity):
        raise AtomicLedgerError("decision_content_address_drift")
    if decision.get("decision_id") != _derive_id("decision", decision_identity):
        raise AtomicLedgerError("decision_id_content_address_drift")
    if decision.get("decision_id") != _derive_id("decision", decision_identity):
        raise AtomicLedgerError("decision_id_content_address_drift")
    for ordinal, item in enumerate(intents):
        intent_identity = {
            "decision_id": decision["decision_id"],
            "ordinal": ordinal,
            **{key: value for key, value in item.items() if key not in {"order_intent_id", "idempotency_key", "intent_sha256"}},
        }
        if item.get("intent_sha256") != _content_hash(INTENT_SCHEMA, intent_identity):
            raise AtomicLedgerError("intent_content_address_drift")
        expected_intent_id = _derive_id("intent", intent_identity)
        if item.get("order_intent_id") != expected_intent_id:
            raise AtomicLedgerError("intent_id_content_address_drift")
        if item.get("idempotency_key") != _derive_id(
            "intentempotency",
            {"order_intent_id": expected_intent_id},
        ):
            raise AtomicLedgerError("intent_idempotency_key_drift")
    reservation_identity = {
        "cycle_id": reservation.get("cycle_id"),
        "strategy_lane": reservation.get("strategy_lane"),
        "amount_usdc": round(float(reservation.get("amount_usdc", 0.0)), 8),
    }
    if reservation.get("reservation_sha256") != _content_hash(RESERVATION_SCHEMA, reservation_identity):
        raise AtomicLedgerError("reservation_content_address_drift")
    expected_reservation_id = _derive_id("reserve", reservation_identity)
    if reservation.get("reservation_id") != expected_reservation_id:
        raise AtomicLedgerError("reservation_id_content_address_drift")
    if reservation.get("idempotency_key") != _derive_id(
        "reservation_idempotency",
        {"reservation_id": expected_reservation_id},
    ):
        raise AtomicLedgerError("reservation_idempotency_key_drift")
    if cycle.get("state") != "prepared":
        raise AtomicLedgerError("initial_cycle_state_invalid")
    if decision.get("will_order") != 0:
        raise AtomicLedgerError("target_no_live_will_order_forbidden")
    if decision.get("decided_state") == "blocked" and (intents or float(reservation.get("amount_usdc", 0.0)) > 1e-9):
        raise AtomicLedgerError("blocked_bundle_has_execution_facts")
    if decision.get("cycle_id") != cycle.get("cycle_id") or decision.get("plan_id") != cycle.get("plan_id"):
        raise AtomicLedgerError("decision_cycle_lineage_mismatch")
    if reservation.get("cycle_id") != cycle.get("cycle_id"):
        raise AtomicLedgerError("reservation_cycle_lineage_mismatch")
    total_notional = sum(float(item.get("requested_notional_usdc", 0.0)) for item in intents)
    if total_notional > float(reservation.get("amount_usdc", 0.0)) + 1e-9:
        raise AtomicLedgerError("reservation_below_intent_notional")
    if any(item.get("state") not in INTENT_STATES for item in intents):
        raise AtomicLedgerError("intent_state_invalid")
    vector_rows = _execution_vector_rows(
        bundle,
        cycle_id=str(cycle["cycle_id"]),
        decision_id=str(decision["decision_id"]),
    )
    if training_fact is not None:
        _assert_training_fact_scope(
            training_fact,
            cycle_id=str(cycle["cycle_id"]),
            decision_id=str(decision["decision_id"]),
        )
    training_fact_id = (
        str(training_fact.get("fact_id"))
        if training_fact is not None
        else None
    )
    for row in vector_rows:
        row["training_fact_id"] = training_fact_id
    with _atomic_transaction(conn, operation="commit_target_atomic_bundle"):
        if not stage_rooted:
            if fact_rooted:
                # Nothing to stage: the observation-identity lane put this
                # fact in the store before the trade existed.  The binding
                # check below is unchanged and is now the whole of it.
                expected_revision = named_fact.raw
            else:
                try:
                    from .canonical_ingress import stage_awc_observation_contract

                    stage_awc_observation_contract(
                        conn, awc_contract, ingested_at_utc=created_at
                    )
                except ContractError as exc:
                    raise AtomicLedgerError("atomic_awc_canonical_stage_rejected") from exc
                expected_revision = revisions[0].raw
            _fault(fault_after, "canonical_stage")
            canonical_rows = conn.execute(
                # Either identity lane: PR12 truth revisions for AWC
                # reports, parallel substitute identity facts for scheduled
                # ASOS slot fills.  Content addressing keeps the sha unique
                # across the two.
                "SELECT fact_json,content_sha256 FROM canonical_ssot_fact "
                "WHERE fact_type IN ("
                "  'truth_observation_revision',"
                "  'substitute_observation_revision'"
                ") AND content_sha256=? ORDER BY fact_id",
                (revision_sha,),
            ).fetchall()
            if (
                len(canonical_rows) != 1
                or str(canonical_rows[0][1]) != revision_sha
                or json.loads(str(canonical_rows[0][0])) != expected_revision
            ):
                raise AtomicLedgerError("atomic_awc_canonical_revision_binding_invalid")
        existing_cycle = conn.execute(
            "SELECT plan_sha256,requested_reserve_usdc,state,settlement_timezone "
            "FROM basket_target_execution_cycles WHERE cycle_id=?",
            (cycle["cycle_id"],),
        ).fetchone()
        if existing_cycle is not None:
            if (
                str(existing_cycle[0]) != str(cycle["plan_sha256"])
                or abs(float(existing_cycle[1]) - float(cycle["requested_reserve_usdc"])) > 1e-9
                or str(existing_cycle[3] or "") != cycle_timezone
            ):
                raise AtomicLedgerError("cycle_content_address_drift")
            decision_row = conn.execute(
                "SELECT decision_id,decision_sha256 FROM auto_trade_decisions WHERE cycle_id=?",
                (cycle["cycle_id"],),
            ).fetchone()
            reservation_row = conn.execute(
                "SELECT reservation_id,reservation_sha256 FROM basket_target_capital_reservations WHERE cycle_id=?",
                (cycle["cycle_id"],),
            ).fetchone()
            if (
                decision_row is None
                or str(decision_row[0]) != str(decision["decision_id"])
                or str(decision_row[1]) != str(decision["decision_sha256"])
            ):
                raise AtomicLedgerError("cycle_existing_decision_drift")
            if (
                reservation_row is None
                or str(reservation_row[0]) != str(reservation["reservation_id"])
                or str(reservation_row[1]) != str(reservation["reservation_sha256"])
            ):
                raise AtomicLedgerError("cycle_existing_reservation_drift")
            existing_intents = {
                (str(row[0]), str(row[1]), str(row[2]))
                for row in conn.execute(
                    "SELECT order_intent_id,intent_sha256,idempotency_key "
                    "FROM basket_target_order_intents WHERE cycle_id=?",
                    (cycle["cycle_id"],),
                ).fetchall()
            }
            expected_intents = {
                (
                    str(item["order_intent_id"]),
                    str(item["intent_sha256"]),
                    str(item["idempotency_key"]),
                )
                for item in intents
            }
            if existing_intents != expected_intents:
                raise AtomicLedgerError("cycle_existing_intents_drift")
            if stage_rooted:
                _assert_existing_cycle_stage_evidence_binding(
                    conn,
                    observation=obs,
                    bound=bound,
                )
            else:
                _assert_existing_cycle_weather_fact_binding(
                    conn,
                    observation=obs,
                    bound=bound,
                )
            _assert_existing_cycle_training_fact_binding(
                conn,
                cycle_id=str(cycle["cycle_id"]),
                decision_id=str(decision["decision_id"]),
                training_fact=training_fact,
                vector_rows=vector_rows,
            )
            vector_signature = lambda row: (
                row["order_intent_id"], row["cycle_id"], row["decision_id"],
                row["training_fact_id"],
                row["vector_policy"], row["target_price_id"], row["quote_observed_at"],
                row["quote_expires_at"], row["probability_sha256"],
                row["official_ws_quote_sha256"], row["inventory_sha256"],
                row["order_state_sha256"], row["station_policy_sha256"],
                row["cash_policy_decision_sha256"], row["strict_delta_artifact_sha256"],
                row["economic_policy_schema"],
                row["economic_policy_artifact_sha256"],
                row["vector_json"], row["vector_sha256"],
            )
            existing_vectors = {
                tuple(row)
                for row in conn.execute(
                    "SELECT order_intent_id,cycle_id,decision_id,training_fact_id,"
                    "vector_policy,target_price_id,quote_observed_at,quote_expires_at,"
                    "probability_sha256,official_ws_quote_sha256,inventory_sha256,"
                    "order_state_sha256,station_policy_sha256,cash_policy_decision_sha256,"
                    "strict_delta_artifact_sha256,economic_policy_schema,"
                    "economic_policy_artifact_sha256,vector_json,vector_sha256 "
                    "FROM basket_target_execution_version_vectors WHERE cycle_id=?",
                    (cycle["cycle_id"],),
                ).fetchall()
            }
            expected_vectors = {vector_signature(row) for row in vector_rows}
            if existing_vectors != expected_vectors:
                raise AtomicLedgerError("cycle_existing_version_vectors_drift")
            return {
                "schema": SCHEMA, "state": "already_committed", "idempotent": True,
                "cycle_id": cycle["cycle_id"], "decision_id": decision["decision_id"],
                "reservation_id": reservation["reservation_id"], "orders_submitted": 0,
                "places_orders": False, "calls_order_gateway": False,
            }

        # The atomic ledger, not only the resident worker, owns uniqueness of
        # an actionable no-live candidate.  A caller that bypasses the worker
        # must not append a second active prepared authority; it has to use
        # ``supersede_prepared_cycle_and_commit_bundle`` so retirement and the
        # successor commit share one transaction.
        training_fact_columns = (
            {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(basket_policy_training_fact)"
                ).fetchall()
            }
            if training_fact is not None
            else set()
        )
        # This cycle's version-vector rows are written before the fact writer
        # is asked, and they carry the candidate's own fact_id.  The fact
        # writer is idempotent on decision_scope_key, so when the key is
        # already held it keeps the stored row and this candidate's fact never
        # lands -- leaving those rows pointing at nothing and, on replay,
        # ``_assert_existing_cycle_training_fact_binding`` finding no fact for
        # this cycle at all.  cycle_id is inside the fact's content address,
        # so a stored row under a different fact_id is by construction another
        # cycle's; no ordering of these writes makes the reference valid.
        # Refuse to open a second cycle for a decision already recorded.  The
        # resident's own scope guard answers this first and retains without a
        # write, so reaching here means a caller bypassed it.
        #
        # The scope question is asked through the forward fact validator, not
        # re-implemented here: a stored row that disagrees on a scope-defining
        # field must still raise the validator's own named conflict, naming
        # the field, rather than being flattened into this coarser refusal.
        existing_training_scope = None
        held_fact_id: str | None = None
        if (
            training_fact is not None
            and "decision_scope_key" in training_fact_columns
        ):
            from .delta_v2_training import committed_scope_fact_id

            try:
                held_fact_id = committed_scope_fact_id(
                    conn, fact=training_fact
                )
            except ContractError as exc:
                raise AtomicLedgerError(str(exc)) from exc
            existing_training_scope = (
                None if held_fact_id is None else (held_fact_id,)
            )
        # The refusal itself waits for the decision-identity check below.
        # A re-decision that resolves to a standing decision id is retained
        # there as ``already_committed_decision`` -- the same decision, not
        # an error -- and this refusal must not pre-empt it: it did, once, on
        # KATL 2026-09-02 at 03:40Z, refusing a stage-open that the retain
        # would have answered.  What is left for this refusal is the case the
        # retain cannot see: a fact holding the key with no standing decision
        # under it, which is a store the migration left with an orphan.
        held_foreign_fact_id = (
            held_fact_id
            if (
                training_fact is not None
                and existing_training_scope is not None
                and held_fact_id != str(training_fact.get("fact_id") or "")
            )
            else None
        )
        if (
            decision.get("decided_state") == "prepared"
            and training_fact is not None
            and "decision_scope_key" in training_fact_columns
            and existing_training_scope is None
        ):
            conflicting_active = conn.execute(
                "SELECT c.cycle_id FROM basket_target_execution_cycles c "
                "JOIN auto_trade_decisions d ON d.cycle_id=c.cycle_id "
                "WHERE c.station_id=? AND c.event_date=? "
                "AND c.strategy_lane=? AND c.cycle_id<>? "
                "AND c.state IN "
                "('prepared','submitting','submitted','reconcile_required') "
                "AND d.decided_state<>'blocked' LIMIT 1",
                (
                    cycle["station_id"],
                    cycle["event_date"],
                    cycle["strategy_lane"],
                    cycle["cycle_id"],
                ),
            ).fetchone()
            if conflicting_active is not None:
                raise AtomicLedgerError(
                    "target_active_candidate_supersession_required"
                )
        # The same decision, arriving on a new cycle, is a restatement and
        # writes nothing (user ruling, 2026-09-02).  Under the clocked
        # identity this could not happen -- every cycle minted a fresh
        # decision id -- so this branch is what the economic identity
        # *means* at the write boundary: a re-decision that believes,
        # prices, holds and is governed identically to one already recorded
        # resolves to that record.  It is not an error and it is not a
        # second row.  The cycle handle differs by construction and is not
        # what is being compared.
        standing = conn.execute(
            "SELECT cycle_id, decision_sha256 FROM auto_trade_decisions"
            " WHERE decision_id=?",
            (decision["decision_id"],),
        ).fetchone()
        if standing is not None and str(standing[0]) != cycle["cycle_id"]:
            if str(standing[1]) != str(decision["decision_sha256"]):
                raise AtomicLedgerError("decision_id_collision_content_drift")
            return {
                "schema": SCHEMA,
                "state": "already_committed_decision",
                "idempotent": True,
                "cycle_id": str(standing[0]),
                "decision_id": decision["decision_id"],
                "intent_count": 0,
                "reserved_amount_usdc": 0.0,
                "orders_submitted": 0,
                "places_orders": False,
                "calls_order_gateway": False,
                "live_submit_authorized": False,
                "execution_version_vector_count": 0,
                "training_fact_state": "already_committed_decision",
            }
        if held_foreign_fact_id is not None:
            raise AtomicLedgerError(
                "target_training_fact_decision_scope_already_committed"
            )

        if not stage_rooted:
            # The METAR-only projection tables carry CHECK constraints naming
            # observed report types; stage evidence is persisted through its
            # own append-only pair by the production adapter instead.
            _stage_canonical_target_projections(
                conn,
                observation=obs,
                bound=bound,
                revision_sha=revision_sha,
                created_at=created_at,
                fault_after=fault_after,
            )
        conn.execute(
            "INSERT INTO basket_target_execution_cycles(cycle_id,plan_id,station_id,event_date,settlement_timezone,strategy_lane,plan_sha256,requested_reserve_usdc,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (cycle["cycle_id"], cycle["plan_id"], cycle["station_id"], cycle["event_date"], cycle_timezone, cycle["strategy_lane"], cycle["plan_sha256"], cycle["requested_reserve_usdc"], "prepared", created_at, created_at),
        )
        _fault(fault_after, "cycle")
        conn.execute(
            "INSERT INTO auto_trade_decisions(decision_id,cycle_id,plan_id,station_id,event_date,settlement_timezone,strategy_lane,decided_state,will_order,target_execution_hard_deadline_at,reason_codes_json,details_json,decision_sha256,created_at,decision_scope_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (decision["decision_id"], decision["cycle_id"], decision["plan_id"], decision["station_id"], decision["event_date"], cycle_timezone, decision["strategy_lane"], decision["decided_state"], decision["will_order"], deadline, canonical_json(decision["reason_codes"]), canonical_json(decision["details"]), decision["decision_sha256"], created_at, decision.get("decision_scope_key")),
        )
        _fault(fault_after, "decision")
        for item in intents:
            conn.execute(
                "INSERT INTO basket_target_order_intents(order_intent_id,decision_id,cycle_id,bucket,token_id,side,order_action,limit_price,requested_size,requested_notional_usdc,state,live_submit_authorized,target_execution_hard_deadline_at,idempotency_key,intent_json,intent_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item["order_intent_id"], decision["decision_id"], cycle["cycle_id"], item["bucket"], item["token_id"], item["side"], item["order_action"], item["limit_price"], item["requested_size"], item["requested_notional_usdc"], item["state"], intent_live_submit_authorized, item["target_execution_hard_deadline_at"], item["idempotency_key"], canonical_json(item["intent_json"]), item["intent_sha256"], created_at),
            )
        _fault(fault_after, "intent")
        for row in vector_rows:
            conn.execute(
                "INSERT INTO basket_target_execution_version_vectors("
                "order_intent_id,cycle_id,decision_id,training_fact_id,vector_policy,"
                "target_price_id,quote_observed_at,"
                "quote_expires_at,probability_sha256,official_ws_quote_sha256,inventory_sha256,"
                "order_state_sha256,station_policy_sha256,cash_policy_decision_sha256,"
                "strict_delta_artifact_sha256,economic_policy_schema,"
                "economic_policy_artifact_sha256,vector_json,vector_sha256,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["order_intent_id"], row["cycle_id"], row["decision_id"],
                    row["training_fact_id"], row["vector_policy"], row["target_price_id"],
                    row["quote_observed_at"], row["quote_expires_at"], row["probability_sha256"],
                    row["official_ws_quote_sha256"], row["inventory_sha256"],
                    row["order_state_sha256"], row["station_policy_sha256"],
                    row["cash_policy_decision_sha256"], row["strict_delta_artifact_sha256"],
                    row["economic_policy_schema"],
                    row["economic_policy_artifact_sha256"],
                    row["vector_json"], row["vector_sha256"], created_at,
                ),
            )
        _fault(fault_after, "version_vector")
        training_state = None
        if training_fact is not None:
            training_state = _insert_training_fact(conn, training_fact)
        _fault(fault_after, "training_fact")
        _reserve(conn, reservation, created_at=created_at)
        _fault(fault_after, "reserve")
    return {
        "schema": SCHEMA, "state": "committed", "idempotent": False,
        "cycle_id": cycle["cycle_id"], "decision_id": decision["decision_id"],
        "reservation_id": reservation["reservation_id"], "intent_count": len(intents),
        "reserved_amount_usdc": float(reservation["amount_usdc"]), "orders_submitted": 0,
        "places_orders": False, "calls_order_gateway": False,
        "execution_version_vector_count": len(vector_rows),
        "training_fact_state": training_state,
    }


def commit_blocked_scope_decision(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    reason_codes: Sequence[str],
    details: Mapping[str, Any] | None = None,
    created_at_utc: str,
    strategy_lane: str = "basket",
    _pre_event_preselection_id: str = "",
    _stage_zero_execution_anchor: Mapping[str, Any] | None = None,
    _stage_zero_training_fact_authorized: bool = False,
) -> dict[str, Any]:
    """Persist a scope blocker without inventing execution facts.

    This is the deliberately smaller companion to ``commit_target_atomic_bundle``
    for a decision point that cannot identify a committed P1 observation/bound
    or a complete market snapshot yet.  It writes exactly one execution cycle
    and its blocking ``auto_trade_decisions`` row with ``will_order=0``.  It
    does not write observation facts, bound facts, intents, reservations,
    inventory, quotes, or order events.  The identity is content-addressed, so
    repeated daemon polls of the same blocker are idempotent and a later
    blocker change can be represented by a new immutable decision.
    """

    with ledger_span("blocked_schema_status"):
        status = target_ledger_schema_status(conn)
    if not status["ok"]:
        raise AtomicLedgerError("target_ledger_schema_incomplete")
    station = _text("blocked_station_id", station_id)
    day = _date("blocked_event_date", event_date)
    timezone_name = _text(
        "blocked_settlement_timezone", settlement_timezone
    )
    lane = _basket_lane("blocked_strategy_lane", strategy_lane)
    reasons = _reason_codes(reason_codes)
    if not reasons:
        raise AtomicLedgerError("blocked_decision_reason_missing")
    payload_details = _mapping("blocked_decision_details", details or {})
    created_at = _timestamp("created_at_utc", created_at_utc)
    preselection_id = str(_pre_event_preselection_id or "").strip()
    stage_zero_anchor = _stage_zero_execution_anchor
    stage_zero_training_authorized = _stage_zero_training_fact_authorized
    if not isinstance(stage_zero_training_authorized, bool):
        raise AtomicLedgerError("stage_zero_training_fact_authorization_invalid")
    if preselection_id and stage_zero_anchor is not None:
        raise AtomicLedgerError("blocked_decision_private_modes_conflict")
    if stage_zero_training_authorized and stage_zero_anchor is None:
        raise AtomicLedgerError("stage_zero_training_fact_anchor_missing")
    if preselection_id:
        if lane != "basket":
            raise AtomicLedgerError(
                "preliminary_decision_strategy_lane_invalid"
            )
        if reasons != ["preliminary_no_live_p1_pending"]:
            raise AtomicLedgerError(
                "preliminary_decision_reason_invalid"
            )
        with ledger_span("blocked_preselection_lookup"):
            preselection = conn.execute(
                "SELECT station_id,event_date,checkpoint_id,probability_stage,"
                "quote_manifest_sha256,plan_sha256,state,final_authority,"
                "live_submit_authorized,calls_order_gateway,orders_submitted "
                "FROM basket_p0_preselection_plan WHERE preselection_id=?",
                (preselection_id,),
            ).fetchone()
        if preselection is None:
            raise AtomicLedgerError(
                "preliminary_decision_preselection_missing"
            )
        if (
            str(preselection[0]) != station
            or str(preselection[1]) != day
            or str(preselection[3]) not in {"P0", "P0-M"}
            or str(preselection[6]) != "PRELIMINARY_NO_LIVE"
            or str(preselection[7]) != "P1"
            or int(preselection[8]) != 0
            or int(preselection[9]) != 0
            or int(preselection[10]) != 0
        ):
            raise AtomicLedgerError(
                "preliminary_decision_preselection_contract_invalid"
            )
        expected_details = {
            "preliminary_decision_version": 1,
            "preselection_id": preselection_id,
            "checkpoint_id": str(preselection[2]),
            "probability_stage": str(preselection[3]),
            "quote_manifest_sha256": str(preselection[4]),
            "preselection_plan_sha256": str(preselection[5]),
            "final_authority": "P1",
            # Rebuilt from the payload rather than from the preselection:
            # the blocker names why *this* stage-open did not open, which is
            # a property of the attempt and not of the row it references.
            # It is still checked -- the writer refuses an empty one -- and
            # omitting it here is what made every preliminary record fail
            # its own details comparison the moment the field was added.
            "stage_open_blocker": str(
                payload_details.get("stage_open_blocker") or ""
            ),
            "capital_authorized": False,
            "intents_written": 0,
            "reservations_written": 0,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }
        if not str(expected_details["stage_open_blocker"]).strip():
            raise AtomicLedgerError(
                "preliminary_decision_stage_open_blocker_missing"
            )
        if payload_details != expected_details:
            raise AtomicLedgerError(
                "preliminary_decision_details_invalid"
            )

    plan_payload = {
        "blocked_scope_plan_version": 1,
        "station_id": station,
        "event_date": day,
        "settlement_timezone": timezone_name,
        "strategy_lane": lane,
        "reason_codes": reasons,
        "details": payload_details,
        "target_execution_hard_deadline_at": None,
    }
    plan_sha = _content_hash(PLAN_SCHEMA, plan_payload)
    plan_id = _derive_id("plan", plan_payload)
    cycle_identity = {
        "plan_id": plan_id,
        "station_id": station,
        "event_date": day,
        "settlement_timezone": timezone_name,
        "strategy_lane": lane,
    }
    cycle_id = _derive_id("cycle", cycle_identity)
    # A stage-zero block carries no training fact, so it is addressed by
    # the economic subset of its details and its reasons -- the same reading
    # the migration gives every historical blocked row.
    decision_identity = economic_decision_identity(
        station_id=station,
        event_date=day,
        settlement_timezone=timezone_name,
        strategy_lane=lane,
        decided_state="blocked",
        will_order=0,
        reason_codes=reasons,
        details=payload_details,
        decision_scope_key=None,
    )
    decision_sha = _content_hash(DECISION_SCHEMA_V2, decision_identity)
    decision_id = _derive_id("decision", decision_identity)
    no_execution_policy = build_no_execution_version_policy(
        {
            "policy": "NO_EXECUTION",
            "reason_codes": reasons,
        }
    )
    no_execution_json = canonical_json(no_execution_policy.to_dict())

    with _atomic_transaction(conn, operation="commit_blocked_scope_decision"):
        if stage_zero_anchor is not None:
            with ledger_span("blocked_txn_anchor_current"):
                _assert_stage_zero_execution_anchor_current(
                    conn,
                    anchor=stage_zero_anchor,
                    created_at_utc=created_at,
                )
        with ledger_span("blocked_txn_existing_cycle"):
            existing_cycle = conn.execute(
                "SELECT plan_id,plan_sha256,requested_reserve_usdc,state,settlement_timezone "
                "FROM basket_target_execution_cycles WHERE cycle_id=?",
                (cycle_id,),
            ).fetchone()
        if existing_cycle is not None:
            if (
                str(existing_cycle[0]) != plan_id
                or str(existing_cycle[1]) != plan_sha
                or abs(float(existing_cycle[2] or 0.0)) > 1e-9
                or str(existing_cycle[4] or "") != timezone_name
            ):
                raise AtomicLedgerError("blocked_cycle_content_address_drift")
            with ledger_span("blocked_txn_existing_decision"):
                existing_decision = conn.execute(
                    "SELECT decision_id,decision_sha256,decided_state,will_order,settlement_timezone,target_execution_hard_deadline_at "
                    "FROM auto_trade_decisions WHERE cycle_id=?",
                    (cycle_id,),
                ).fetchone()
            if existing_decision is None:
                raise AtomicLedgerError("blocked_cycle_decision_missing")
            if (
                str(existing_decision[0]) != decision_id
                or str(existing_decision[1]) != decision_sha
                or str(existing_decision[2]) != "blocked"
                or int(existing_decision[3]) != 0
                or str(existing_decision[4] or "") != timezone_name
                or existing_decision[5] is not None
            ):
                raise AtomicLedgerError("blocked_cycle_decision_content_address_drift")
            with ledger_span("blocked_txn_existing_training_fact"):
                existing_training_fact = conn.execute(
                    "SELECT training_fact_id FROM basket_target_execution_version_vectors "
                    "WHERE cycle_id=?",
                    (cycle_id,),
                ).fetchone()
            if stage_zero_training_authorized and (
                existing_training_fact is None
                or existing_training_fact[0] is None
            ):
                # Historical stage-zero cycles were committed before the
                # pre-final d2fact contract existed.  They are immutable;
                # never attach a fact by UPDATE during a replay.
                _assert_existing_blocked_no_execution_vector(
                    conn,
                    cycle_id=cycle_id,
                    decision_id=decision_id,
                    expected_policy=no_execution_policy,
                )
                return {
                    "schema": SCHEMA,
                    "state": "already_committed_legacy_no_training_fact",
                    "idempotent": True,
                    "cycle_id": cycle_id,
                    "decision_id": decision_id,
                    "intent_count": 0,
                    "reserved_amount_usdc": 0.0,
                    "orders_submitted": 0,
                    "places_orders": False,
                    "calls_order_gateway": False,
                    "live_submit_authorized": False,
                    "execution_version_vector_count": 1,
                    "training_fact_state": "legacy_not_retrofitted",
                }
            training_fact = None
            if stage_zero_training_authorized:
                with ledger_span("blocked_txn_training_fact_replay"):
                    training_fact = _build_stage_zero_training_fact(
                        conn,
                        anchor=stage_zero_anchor,
                        cycle_id=cycle_id,
                        plan_id=plan_id,
                        plan_sha256=plan_sha,
                        decision_id=decision_id,
                        decision_sha256=decision_sha,
                        station_id=station,
                        event_date=day,
                        settlement_timezone=timezone_name,
                        no_execution_policy=no_execution_policy,
                        created_at_utc=created_at,
                    )
            with ledger_span("blocked_txn_vector_assert"):
                _assert_existing_blocked_no_execution_vector(
                    conn,
                    cycle_id=cycle_id,
                    decision_id=decision_id,
                    expected_policy=no_execution_policy,
                    expected_training_fact_id=(
                        str(training_fact.get("fact_id") or "")
                        if training_fact is not None
                        else None
                    ),
                )
            _assert_existing_cycle_training_fact_binding_timed(
                conn,
                cycle_id=cycle_id,
                decision_id=decision_id,
                training_fact=training_fact,
                vector_rows=[
                    {
                        "training_fact_id": (
                            str(training_fact.get("fact_id") or "")
                            if training_fact is not None
                            else None
                        ),
                    }
                ],
            )
            return {
                "schema": SCHEMA,
                "state": "already_committed",
                "idempotent": True,
                "cycle_id": cycle_id,
                "decision_id": decision_id,
                "intent_count": 0,
                "reserved_amount_usdc": 0.0,
                "orders_submitted": 0,
                "places_orders": False,
                "calls_order_gateway": False,
                "live_submit_authorized": False,
                "execution_version_vector_count": 1,
                "training_fact_state": (
                    "already_committed" if training_fact is not None else None
                ),
            }

        if preselection_id:
            try:
                day_state = event_day_window(
                    day,
                    as_of_utc=created_at,
                    target_timezone=timezone_name,
                )["state"]
            except Exception as exc:
                raise AtomicLedgerError(
                    "preliminary_decision_event_window_invalid"
                ) from exc
            if day_state == "closed":
                raise AtomicLedgerError(
                    "preliminary_decision_event_day_closed"
                )
        elif stage_zero_anchor is None:
            _assert_event_day_started(
                day,
                settlement_timezone=timezone_name,
                as_of_utc=created_at,
            )
        # A stage-zero anchor has already had its day classified above,
        # under this same write lock: closed refused, a listed not-started
        # day admitted (clause 48).  Re-asking the started-day guard here
        # would only rename the refusal -- future_event_day_not_allowed one
        # line behind stage_zero_execution_event_day_not_open -- which is
        # exactly the shape of the five gates cleared on 2026-08-31.

        conflicting_plan = conn.execute(
            "SELECT cycle_id,plan_sha256 FROM basket_target_execution_cycles WHERE plan_id=?",
            (plan_id,),
        ).fetchone()
        if conflicting_plan is not None:
            raise AtomicLedgerError("blocked_plan_id_collision")
        training_fact = None
        if stage_zero_training_authorized:
            # Build and compare before writing the cycle.  The v2 scope key
            # intentionally excludes WS transport refreshes, so a new quote
            # keepalive with the same economic decision must not become a
            # second immutable d2 fact (nor a new no-execution tick row).
            with ledger_span("blocked_txn_training_fact_build"):
                training_fact = _build_stage_zero_training_fact(
                    conn,
                    anchor=stage_zero_anchor,
                    cycle_id=cycle_id,
                    plan_id=plan_id,
                    plan_sha256=plan_sha,
                    decision_id=decision_id,
                    decision_sha256=decision_sha,
                    station_id=station,
                    event_date=day,
                    settlement_timezone=timezone_name,
                    no_execution_policy=no_execution_policy,
                    created_at_utc=created_at,
                )
            existing_scope = conn.execute(
                "SELECT fact_id,content_sha256,cycle_id,decision_id "
                "FROM basket_policy_training_fact WHERE decision_scope_key=?",
                (str(training_fact.get("decision_scope_key") or ""),),
            ).fetchone()
            if existing_scope is not None:
                expected_fact_id = str(training_fact.get("fact_id") or "")
                expected_fact_sha = str(training_fact.get("content_sha256") or "")
                if (
                    str(existing_scope[0] or "") != expected_fact_id
                    or str(existing_scope[1] or "") != expected_fact_sha
                ):
                    return {
                        "schema": SCHEMA,
                        "state": "already_committed_decision_scope",
                        "idempotent": True,
                        "cycle_id": str(existing_scope[2] or ""),
                        "decision_id": str(existing_scope[3] or ""),
                        "intent_count": 0,
                        "reserved_amount_usdc": 0.0,
                        "orders_submitted": 0,
                        "places_orders": False,
                        "calls_order_gateway": False,
                        "live_submit_authorized": False,
                        "execution_version_vector_count": 1,
                        "training_fact_state": "already_committed_decision_scope",
                        "training_fact_id": str(existing_scope[0] or ""),
                    }
        # The same blocked decision, arriving on a new cycle, is a restatement
        # and writes nothing -- the blocked-path twin of the standing retain
        # in ``commit_target_atomic_bundle``.  The cycle id is derived from
        # the whole details payload while the decision id is derived from
        # its economic subset, so a forward scope whose evidence binding or
        # observation status moved but whose decision did not mints a new
        # cycle under a decision id already on file.  Until 2026-09-02 that
        # reached the INSERT below and died on the primary key, which the
        # worker reported as ``blocked_fail_closed`` and which threw away
        # the scope's compile for the cycle: five forward stations a cycle
        # (OPKC, ZBAA, ZSPD, ZSQD, ZUCK on 2026-09-03), every cycle.  The
        # scope retain above only sees this when a stored fact holds the
        # same scope key; a block with no fact, or with a fact whose key
        # moved with the quote, has to be answered by the decision itself.
        standing = conn.execute(
            "SELECT cycle_id, decision_sha256 FROM auto_trade_decisions"
            " WHERE decision_id=?",
            (decision_id,),
        ).fetchone()
        if standing is not None and str(standing[0]) != cycle_id:
            if str(standing[1]) != decision_sha:
                raise AtomicLedgerError("decision_id_collision_content_drift")
            return {
                "schema": SCHEMA,
                "state": "already_committed_decision",
                "idempotent": True,
                "cycle_id": str(standing[0]),
                "decision_id": decision_id,
                "intent_count": 0,
                "reserved_amount_usdc": 0.0,
                "orders_submitted": 0,
                "places_orders": False,
                "calls_order_gateway": False,
                "live_submit_authorized": False,
                "execution_version_vector_count": 0,
                "training_fact_state": "already_committed_decision",
            }
        # Everything above this point is lookups and derivation; from here it
        # is the three-row write the transaction exists for.  Timed as one
        # span because the rows are written together or not at all.
        blocked_write_started = time.perf_counter()
        conn.execute(
            "INSERT INTO basket_target_execution_cycles("
            "cycle_id,plan_id,station_id,event_date,settlement_timezone,strategy_lane,plan_sha256,"
            "requested_reserve_usdc,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (cycle_id, plan_id, station, day, timezone_name, lane, plan_sha, 0.0, "prepared", created_at, created_at),
        )
        conn.execute(
            "INSERT INTO auto_trade_decisions("
            "decision_id,cycle_id,plan_id,station_id,event_date,settlement_timezone,strategy_lane,"
            "decided_state,will_order,target_execution_hard_deadline_at,reason_codes_json,details_json,decision_sha256,created_at,decision_scope_key) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                decision_id, cycle_id, plan_id, station, day, timezone_name, lane, "blocked", 0,
                None, canonical_json(reasons), canonical_json(payload_details), decision_sha, created_at,
                None,
            ),
        )
        conn.execute(
            "INSERT INTO basket_target_execution_version_vectors("
            "order_intent_id,cycle_id,decision_id,training_fact_id,vector_policy,"
            "target_price_id,quote_observed_at,quote_expires_at,vector_json,"
            "vector_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                None,
                cycle_id,
                decision_id,
                (
                    str(training_fact.get("fact_id") or "")
                    if training_fact is not None
                    else None
                ),
                "NO_EXECUTION",
                None,
                None,
                None,
                no_execution_json,
                no_execution_policy.policy_sha256,
                created_at,
            ),
        )
        training_state = None
        if training_fact is not None:
            training_state = _insert_training_fact(conn, training_fact)
        note_ledger_span(
            "blocked_txn_write_rows",
            (time.perf_counter() - blocked_write_started) * 1000.0,
        )
    return {
        "schema": SCHEMA,
        "state": "committed",
        "idempotent": False,
        "cycle_id": cycle_id,
        "decision_id": decision_id,
        "intent_count": 0,
        "reserved_amount_usdc": 0.0,
        "orders_submitted": 0,
        "places_orders": False,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "execution_version_vector_count": 1,
        "training_fact_state": training_state,
    }


def _assert_stage_zero_execution_anchor_current(
    conn: sqlite3.Connection,
    *,
    anchor: Mapping[str, Any],
    created_at_utc: str,
) -> None:
    """Recheck no-execution probability and execution evidence under write lock."""

    if anchor.get("schema") != STAGE_ZERO_EXECUTION_ANCHOR_SCHEMA:
        raise AtomicLedgerError("stage_zero_execution_anchor_schema_invalid")
    supplied_sha = _sha(
        "stage_zero_execution_anchor_sha256",
        anchor.get("content_sha256"),
    )
    if supplied_sha != content_sha256({
        key: value for key, value in anchor.items() if key != "content_sha256"
    }):
        raise AtomicLedgerError("stage_zero_execution_anchor_sha_mismatch")
    evaluation_at = _timestamp_instant(
        "stage_zero_execution_evaluation_at_utc",
        anchor.get("evaluation_at_utc"),
    )
    created_at = _timestamp_instant(
        "stage_zero_execution_created_at_utc", created_at_utc
    )
    if evaluation_at != created_at:
        raise AtomicLedgerError("stage_zero_execution_clock_mismatch")
    probability = _mapping(
        "stage_zero_execution_probability_envelope",
        anchor.get("probability_envelope"),
    )
    stage = _text(
        "stage_zero_execution_probability_stage",
        probability.get("stage"),
    )
    if stage not in {"P0", "P0-M"}:
        raise AtomicLedgerError("stage_zero_execution_probability_stage_invalid")
    checkpoint_id = _text(
        "stage_zero_execution_checkpoint_id",
        probability.get("checkpoint_id"),
    )
    envelope_id = _text(
        "stage_zero_execution_envelope_id",
        probability.get("envelope_id"),
    )
    if envelope_id != checkpoint_id:
        raise AtomicLedgerError(
            "stage_zero_execution_envelope_checkpoint_mismatch"
        )
    station_id = _text(
        "stage_zero_execution_station_id", anchor.get("station_id")
    )
    event_date = _date(
        "stage_zero_execution_event_date", anchor.get("event_date")
    )
    settlement_timezone = _text(
        "stage_zero_execution_settlement_timezone",
        anchor.get("settlement_timezone"),
    )
    try:
        day_state = event_day_window(
            event_date,
            as_of_utc=created_at_utc,
            target_timezone=settlement_timezone,
        )["state"]
    except Exception as exc:
        raise AtomicLedgerError(
            "stage_zero_execution_event_window_invalid"
        ) from exc
    # A listed day that has not started is a stage-zero day, not a future
    # one.  Clause 48 (2026-08-14) makes listing the entry, and clause 46
    # already lets a stage-rooted P0/P0-M *position* open before the day
    # exists (commit_target_atomic_bundle skips the started-day guard for
    # it).  This recheck predates both -- it was written 2026-08-04 for a
    # ledger that only ever saw the current day -- and until 2026-09-02 it
    # was the sixth gate on the listing path: every listing-day P0
    # candidate compiled, evaluated to no execution, and then failed here,
    # so no decision row and no training fact were ever written for it (18
    # to 23 scopes per cycle).  The admission is the one _decision_scopes
    # already applies to a not-started day -- a committed P0/P0-M authority
    # and no P1 -- and every clause of it is enforced below by the checks
    # that follow: the anchored envelope must be the current committed row
    # of its own stage, and any committed P1 refuses the anchor outright.
    # Closed stays closed.
    if day_state not in {"open", "not_started"}:
        raise AtomicLedgerError("stage_zero_execution_event_day_not_open")
    probability_sha = _sha(
        "stage_zero_execution_probability_sha256",
        probability.get("content_sha256"),
    )
    row = conn.execute(
        "SELECT stage,station_id,event_date,settlement_timezone,"
        "decision_at_utc,fresh_until_utc,content_sha256,state "
        "FROM basket_probability_envelope "
        "WHERE envelope_id=? AND checkpoint_id=?",
        (envelope_id, checkpoint_id),
    ).fetchone()
    if row is None:
        raise AtomicLedgerError("stage_zero_execution_probability_missing")
    try:
        anchor_decision_at = _timestamp_instant(
            "stage_zero_execution_anchor_probability_decision_at",
            probability.get("decision_at_utc"),
        )
        anchor_fresh_until = _timestamp_instant(
            "stage_zero_execution_anchor_probability_fresh_until",
            probability.get("fresh_until_utc"),
        )
        row_decision_at = _timestamp_instant(
            "stage_zero_execution_row_probability_decision_at", row[4]
        )
        row_fresh_until = _timestamp_instant(
            "stage_zero_execution_row_probability_fresh_until", row[5]
        )
    except AtomicLedgerError as exc:
        raise AtomicLedgerError("stage_zero_execution_probability_drift") from exc
    if (
        str(row[0]) != stage
        or str(row[1]) != station_id
        or str(row[2]) != event_date
        or str(row[3]) != settlement_timezone
        or row_decision_at != anchor_decision_at
        or row_fresh_until != anchor_fresh_until
        or str(row[6]) != probability_sha
        or str(row[7]) != "COMMITTED"
        or row_decision_at > created_at
        or created_at >= row_fresh_until
    ):
        raise AtomicLedgerError("stage_zero_execution_probability_drift")
    # A P1 materialization is final authority even when its own freshness
    # window has elapsed.  This check deliberately has no freshness/as-of
    # predicate: it closes the P1-arrives-between-compile-and-commit race and
    # forbids an old P0/P0-M anchor from reappearing after final authority.
    p1 = conn.execute(
        "SELECT 1 FROM basket_probability_envelope "
        "WHERE station_id=? AND event_date=? AND settlement_timezone=? "
        "AND stage='P1' AND state='COMMITTED' LIMIT 1",
        (station_id, event_date, settlement_timezone),
    ).fetchone()
    if p1 is not None:
        raise AtomicLedgerError("stage_zero_execution_higher_stage_visible")
    if stage == "P0":
        try:
            p0m = _stage_zero_visible_probability_rows(
                conn,
                station_id=station_id,
                event_date=event_date,
                settlement_timezone=settlement_timezone,
                stage="P0-M",
                as_of=created_at,
            )
        except AtomicLedgerError as exc:
            raise AtomicLedgerError(
                "stage_zero_execution_higher_stage_visible"
            ) from exc
        if p0m:
            raise AtomicLedgerError("stage_zero_execution_higher_stage_visible")
    try:
        same_stage = _stage_zero_visible_probability_rows(
            conn,
            station_id=station_id,
            event_date=event_date,
            settlement_timezone=settlement_timezone,
            stage=stage,
            as_of=created_at,
        )
    except AtomicLedgerError as exc:
        raise AtomicLedgerError("stage_zero_execution_not_current_stage") from exc
    latest_same_stage_at = max(
        (decision_at for _, decision_at in same_stage), default=None
    )
    current_same_stage = (
        [
            row_checkpoint_id
            for row_checkpoint_id, decision_at in same_stage
            if decision_at == latest_same_stage_at
        ]
        if latest_same_stage_at is not None
        else []
    )
    if (
        len(current_same_stage) != 1
        or current_same_stage[0] != checkpoint_id
    ):
        raise AtomicLedgerError("stage_zero_execution_not_current_stage")

    regime_sha = _sha(
        "stage_zero_execution_regime_sha256",
        anchor.get("regime_artifact_sha256"),
    )
    regime = conn.execute(
        "SELECT valid_from_utc,valid_until_utc FROM basket_regime_artifact WHERE station_id=? "
        "AND event_date=? AND stage=? AND checkpoint_id=? "
        "AND probability_sha256=? AND content_sha256=? AND state='COMMITTED' "
        "LIMIT 1",
        (
            station_id, event_date, stage, checkpoint_id,
            probability_sha, regime_sha,
        ),
    ).fetchone()
    try:
        regime_valid_from = _timestamp_instant(
            "stage_zero_execution_regime_valid_from",
            regime[0] if regime is not None else None,
        )
        regime_valid_until = _timestamp_instant(
            "stage_zero_execution_regime_valid_until",
            regime[1] if regime is not None else None,
        )
    except AtomicLedgerError:
        raise AtomicLedgerError("stage_zero_execution_regime_drift") from None
    if (
        regime is None
        or regime_valid_from > created_at
        or created_at >= regime_valid_until
    ):
        raise AtomicLedgerError("stage_zero_execution_regime_drift")

    ws = _mapping(
        "stage_zero_execution_official_ws",
        anchor.get("official_ws_snapshot"),
    )
    ws_row = conn.execute(
        "SELECT quote_source,quote_observed_at,quote_expires_at,state "
        "FROM basket_official_ws_snapshot WHERE snapshot_id=? "
        "AND station_id=? AND event_date=? AND content_sha256=?",
        (
            _text("stage_zero_execution_ws_id", ws.get("snapshot_id")),
            station_id,
            event_date,
            _sha("stage_zero_execution_ws_sha", ws.get("content_sha256")),
        ),
    ).fetchone()
    # ``quote_observed_at`` is publisher evidence and may retain millisecond
    # precision in SQLite.  The compiler normalizes anchor timestamps through
    # ``datetime.isoformat()``, which expands the same instant to microseconds.
    # Compare instants here, while the immutable snapshot id/content hash
    # above remain the exact evidence binding.  Do not downgrade this to a
    # latest-snapshot lookup: a different WS generation must still fail.
    try:
        anchor_quote_observed_at = _timestamp_instant(
            "stage_zero_execution_anchor_quote_observed_at",
            ws.get("quote_observed_at"),
        )
        anchor_quote_expires_at = _timestamp_instant(
            "stage_zero_execution_anchor_quote_expires_at",
            ws.get("quote_expires_at"),
        )
        row_quote_observed_at = _timestamp_instant(
            "stage_zero_execution_row_quote_observed_at",
            ws_row[1] if ws_row is not None else None,
        )
        row_quote_expires_at = _timestamp_instant(
            "stage_zero_execution_row_quote_expires_at",
            ws_row[2] if ws_row is not None else None,
        )
    except AtomicLedgerError:
        raise AtomicLedgerError("stage_zero_execution_official_ws_drift")
    if (
        ws_row is None
        or str(ws_row[0]).upper() != "OFFICIAL_CLOB_WS"
        or row_quote_observed_at != anchor_quote_observed_at
        or row_quote_expires_at != anchor_quote_expires_at
        or str(ws_row[3]) != "COMMITTED"
        or row_quote_observed_at > created_at
        or created_at >= row_quote_expires_at
    ):
        raise AtomicLedgerError("stage_zero_execution_official_ws_drift")

    for name, table, id_column, anchor_key, completeness_column in (
        (
            "inventory", "basket_confirmed_inventory_snapshot",
            "inventory_snapshot_id", "inventory_snapshot", "complete",
        ),
        (
            "order_state", "basket_order_state_snapshot",
            "order_state_snapshot_id", "order_state_snapshot", "known",
        ),
    ):
        snapshot = _mapping(
            f"stage_zero_execution_{name}", anchor.get(anchor_key)
        )
        found = conn.execute(
            f"SELECT observed_at,{completeness_column},state FROM {table} "
            f"WHERE {id_column}=? AND station_id=? AND event_date=? "
            "AND content_sha256=?",
            (
                _text(
                    f"stage_zero_execution_{name}_id",
                    snapshot.get(id_column),
                ),
                station_id,
                event_date,
                _sha(
                    f"stage_zero_execution_{name}_sha256",
                    snapshot.get("content_sha256"),
                ),
            ),
        ).fetchone()
        try:
            snapshot_observed_at = _timestamp_instant(
                f"stage_zero_execution_{name}_anchor_observed_at",
                snapshot.get("observed_at"),
            )
            found_observed_at = _timestamp_instant(
                f"stage_zero_execution_{name}_row_observed_at",
                found[0] if found is not None else None,
            )
        except AtomicLedgerError:
            raise AtomicLedgerError(
                f"stage_zero_execution_{name}_drift"
            ) from None
        if (
            found is None
            or found_observed_at != snapshot_observed_at
            or int(found[1]) != 1
            or str(found[2]) != "COMMITTED"
            or found_observed_at > created_at
        ):
            raise AtomicLedgerError(
                f"stage_zero_execution_{name}_drift"
            )


def commit_stage_zero_execution_scope_decision(
    conn: sqlite3.Connection,
    *,
    anchor: Mapping[str, Any],
    created_at_utc: str,
    training_fact_authorized: bool = False,
    forward_evidence_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically commit one open-day NO_EXECUTION decision."""

    if not isinstance(anchor, Mapping):
        raise AtomicLedgerError("stage_zero_execution_anchor_mapping_required")
    if not isinstance(training_fact_authorized, bool):
        raise AtomicLedgerError("stage_zero_training_fact_authorization_invalid")
    if forward_evidence_binding is not None and not isinstance(
        forward_evidence_binding, Mapping
    ):
        raise AtomicLedgerError("stage_zero_forward_evidence_binding_mapping_required")
    probability = _mapping(
        "stage_zero_execution_probability_envelope",
        anchor.get("probability_envelope"),
    )
    ws = _mapping(
        "stage_zero_execution_official_ws_snapshot",
        anchor.get("official_ws_snapshot"),
    )
    inventory = _mapping(
        "stage_zero_execution_inventory_snapshot",
        anchor.get("inventory_snapshot"),
    )
    order_state = _mapping(
        "stage_zero_execution_order_state_snapshot",
        anchor.get("order_state_snapshot"),
    )
    cash_phase = _mapping(
        "stage_zero_execution_cash_phase", anchor.get("cash_phase")
    )
    details = {
        "stage_zero_execution_version": 1,
        "anchor_sha256": _sha(
            "stage_zero_execution_anchor_sha256",
            anchor.get("content_sha256"),
        ),
        "probability_stage": str(probability.get("stage") or ""),
        "checkpoint_id": str(probability.get("checkpoint_id") or ""),
        "probability_sha256": str(probability.get("content_sha256") or ""),
        "regime_artifact_sha256": str(
            anchor.get("regime_artifact_sha256") or ""
        ),
        "official_ws_snapshot_id": str(ws.get("snapshot_id") or ""),
        "official_ws_snapshot_sha256": str(ws.get("content_sha256") or ""),
        "inventory_snapshot_id": str(
            inventory.get("inventory_snapshot_id") or ""
        ),
        "inventory_snapshot_sha256": str(
            inventory.get("content_sha256") or ""
        ),
        "order_state_snapshot_id": str(
            order_state.get("order_state_snapshot_id") or ""
        ),
        "order_state_snapshot_sha256": str(
            order_state.get("content_sha256") or ""
        ),
        "station_policy_sha256": str(
            anchor.get("station_policy_sha256") or ""
        ),
        "cash_policy_decision_sha256": str(
            anchor.get("cash_policy_decision_sha256") or ""
        ),
        "cash_phase": str(cash_phase.get("phase") or ""),
        "delta_v2_artifact_sha256": str(
            anchor.get("delta_v2_artifact_sha256") or ""
        ),
        "delta_v2_evaluation_sha256": str(
            anchor.get("delta_v2_evaluation_sha256") or ""
        ),
        "compiled_plan_sha256": str(
            anchor.get("compiled_plan_sha256") or ""
        ),
        "stage_zero_execution_anchor": dict(anchor),
        "final_authority": "P1",
        "capital_authorized": False,
        "intents_written": 0,
        "reservations_written": 0,
        "training_facts_written": int(training_fact_authorized),
        "execution_mode": "production_no_live",
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }
    if training_fact_authorized:
        compiled_plan = _mapping(
            "stage_zero_training_compiled_plan", anchor.get("compiled_plan")
        )
        audit = _mapping(
            "stage_zero_training_delta_v2_audit",
            compiled_plan.get("delta_v2_audit"),
        )
        if audit.get("authorized") is not True:
            raise AtomicLedgerError("stage_zero_training_delta_v2_not_authorized")
        if forward_evidence_binding is None:
            raise AtomicLedgerError("stage_zero_forward_evidence_binding_missing")
        binding = dict(forward_evidence_binding)
        if (
            binding.get("schema")
            != "bayes_basket_target_forward_evidence_binding_v1"
            or str(binding.get("station_id") or "")
            != str(anchor.get("station_id") or "")
            or str(binding.get("event_date") or "")
            != str(anchor.get("event_date") or "")
            or str(binding.get("settlement_timezone") or "")
            != str(anchor.get("settlement_timezone") or "")
            or str(binding.get("probability_stage") or "")
            != str(probability.get("stage") or "")
            or str(binding.get("decision_at_utc") or "")
            != str(anchor.get("evaluation_at_utc") or "")
        ):
            raise AtomicLedgerError("stage_zero_forward_evidence_binding_drift")
        supplied_binding_sha = _sha(
            "stage_zero_forward_evidence_binding_sha256",
            binding.get("content_sha256"),
        )
        if supplied_binding_sha != content_sha256({
            key: value for key, value in binding.items()
            if key != "content_sha256"
        }):
            raise AtomicLedgerError("stage_zero_forward_evidence_binding_sha_mismatch")
        details["delta_v2_authorized"] = True
        details["forward_observation_status"] = "ELIGIBLE"
        details["forward_evidence_binding"] = binding
    return commit_blocked_scope_decision(
        conn,
        station_id=str(anchor.get("station_id") or ""),
        event_date=str(anchor.get("event_date") or ""),
        settlement_timezone=str(anchor.get("settlement_timezone") or ""),
        strategy_lane="basket",
        reason_codes=["p1_final_authority_pending"],
        details=details,
        created_at_utc=created_at_utc,
        _stage_zero_execution_anchor=anchor,
        _stage_zero_training_fact_authorized=training_fact_authorized,
    )


def commit_preliminary_scope_decision(
    conn: sqlite3.Connection,
    *,
    preselection_id: str,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    checkpoint_id: str,
    probability_stage: str,
    quote_manifest_sha256: str,
    preselection_plan_sha256: str,
    created_at_utc: str,
    stage_open_blocker: str = "",
) -> dict[str, Any]:
    """Materialize a P0/P0-M decision with no execution authority.

    The referenced append-only preselection is the sole authority for every
    identity below.  This permits a not-yet-started registry day to complete
    ``official WS -> preselection -> decision`` without weakening the formal
    P1/truth/hard-NO start-day gate or creating intents/capital reservations.
    """

    stage = _text("preliminary_probability_stage", probability_stage)
    if stage not in {"P0", "P0-M"}:
        raise AtomicLedgerError(
            "preliminary_decision_probability_stage_invalid"
        )
    # Charter 46 makes this a receipt, never a gate: it is only legitimate
    # once stage-open has been attempted and named its failure.  Refusing to
    # write it without that name is what stops it drifting back into being
    # the reason a forward day did not open -- which is exactly what it had
    # become (22 of 50 blocked decisions on 2026-08-16 carried this code and
    # no blocker beside it).
    blocker = str(stage_open_blocker or "").strip()
    if not blocker:
        raise AtomicLedgerError(
            "preliminary_decision_stage_open_blocker_missing"
        )
    details = {
        "preliminary_decision_version": 1,
        "preselection_id": _text(
            "preliminary_preselection_id", preselection_id
        ),
        "checkpoint_id": _text(
            "preliminary_checkpoint_id", checkpoint_id
        ),
        "probability_stage": stage,
        "quote_manifest_sha256": _sha(
            "preliminary_quote_manifest_sha256",
            quote_manifest_sha256,
        ),
        "preselection_plan_sha256": _sha(
            "preliminary_plan_sha256",
            preselection_plan_sha256,
        ),
        "final_authority": "P1",
        "stage_open_blocker": blocker,
        "capital_authorized": False,
        "intents_written": 0,
        "reservations_written": 0,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }
    return commit_blocked_scope_decision(
        conn,
        station_id=station_id,
        event_date=event_date,
        settlement_timezone=settlement_timezone,
        strategy_lane="basket",
        reason_codes=["preliminary_no_live_p1_pending"],
        details=details,
        created_at_utc=created_at_utc,
        _pre_event_preselection_id=str(preselection_id),
    )


_CYCLE_TRANSITIONS: dict[str, frozenset[str]] = {
    "prepared": frozenset({"submitting", "reconcile_required"}),
    "submitting": frozenset({"submitted", "reconcile_required"}),
    "submitted": frozenset({"reconcile_required"}),
    "reconcile_required": frozenset(),
    "settled": frozenset(),
}


def transition_target_cycle_state(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    expected_state: str,
    new_state: str,
    updated_at_utc: str,
) -> dict[str, Any]:
    """Perform a compare-and-set lifecycle transition; no implicit jumps."""

    cycle = _text("cycle_id", cycle_id)
    expected = _text("expected_state", expected_state).lower()
    target = _text("new_state", new_state).lower()
    updated = _timestamp("updated_at_utc", updated_at_utc)
    if expected not in CYCLE_STATES or target not in CYCLE_STATES:
        raise AtomicLedgerError("cycle_state_invalid")
    if target not in _CYCLE_TRANSITIONS[expected]:
        raise AtomicLedgerError("cycle_state_transition_invalid")
    if expected == "prepared" and target == "submitting":
        raise AtomicLedgerError(
            "cycle_submission_requires_persisted_candidate_cas_boundary"
        )
    with _atomic_transaction(conn, operation="transition_target_cycle_state"):
        row = conn.execute("SELECT state FROM basket_target_execution_cycles WHERE cycle_id=?", (cycle,)).fetchone()
        if row is None:
            raise AtomicLedgerError("cycle_missing")
        current = str(row[0])
        if current != expected:
            raise AtomicLedgerError("cycle_state_cas_conflict")
        conn.execute("UPDATE basket_target_execution_cycles SET state=?,updated_at=? WHERE cycle_id=? AND state=?", (target, updated, cycle, expected))
    return {"schema": SCHEMA, "state": "transitioned", "cycle_id": cycle, "previous_state": expected, "new_state": target}


def close_target_cycle(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    outcome: str,
    closed_at_utc: str,
    reason: str,
) -> dict[str, Any]:
    """Settle a cycle and release or consume its unique active reservation."""

    cycle = _text("cycle_id", cycle_id)
    outcome_value = _text("outcome", outcome).lower()
    if outcome_value not in {"released", "consumed"}:
        raise AtomicLedgerError("cycle_close_outcome_invalid")
    closed_at = _timestamp("closed_at_utc", closed_at_utc)
    close_reason = _text("close_reason", reason)
    with _atomic_transaction(conn, operation="close_target_cycle"):
        row = conn.execute("SELECT state FROM basket_target_execution_cycles WHERE cycle_id=?", (cycle,)).fetchone()
        if row is None:
            raise AtomicLedgerError("cycle_missing")
        current = str(row[0])
        reservation = conn.execute(
            "SELECT reservation_id,amount_usdc,state FROM basket_target_capital_reservations WHERE cycle_id=?", (cycle,)
        ).fetchone()
        if current == "settled":
            if reservation is not None and str(reservation[2]) == "active":
                raise AtomicLedgerError("settled_cycle_active_reservation")
            if reservation is None:
                raise AtomicLedgerError("settled_cycle_outcome_drift")
            settled_state = str(reservation[2])
            if settled_state != outcome_value:
                raise AtomicLedgerError("settled_cycle_outcome_drift")
            return {"schema": SCHEMA, "state": "already_settled", "cycle_id": cycle, "outcome": outcome_value}
        if current not in {"prepared", "submitted", "reconcile_required"}:
            raise AtomicLedgerError("cycle_close_state_invalid")
        if reservation is None:
            raise AtomicLedgerError("reservation_missing")
        reservation_id, amount, reservation_state = str(reservation[0]), float(reservation[1]), str(reservation[2])
        if reservation_state == "active":
            balance, reserved, _epoch, _version = _ensure_pool(conn)
            if amount > reserved + 1e-9:
                raise AtomicLedgerError("pool_reserved_balance_drift")
            if outcome_value == "consumed" and amount > balance + 1e-9:
                raise AtomicLedgerError("pool_balance_drift")
            if outcome_value == "consumed":
                conn.execute("UPDATE basket_target_capital_pool SET balance_usdc=MAX(0.0,balance_usdc-?),reserved_usdc=MAX(0.0,reserved_usdc-?),version=version+1,updated_at=? WHERE pool_id=1", (amount, amount, closed_at))
            else:
                # Clamp at zero rather than subtracting straight.  Reserved is
                # a running sum of reservations added and removed in a
                # different order than they were made, so by the last release
                # it carries binary residue the stored amount does not: the
                # final 0.40791 met a reserved of 0.4079099999999879 and the
                # difference, -1.2e-14, tripped the CHECK and made that
                # reservation unreleasable for good.  The guard above already
                # refused anything more than a nanounit over, so everything
                # this can absorb is arithmetic noise, not capital.
                conn.execute("UPDATE basket_target_capital_pool SET reserved_usdc=MAX(0.0,reserved_usdc-?),version=version+1,updated_at=? WHERE pool_id=1", (amount, closed_at))
            conn.execute("UPDATE basket_target_capital_reservations SET state=?,release_reason=?,updated_at=? WHERE reservation_id=? AND state='active'", (outcome_value, close_reason, closed_at, reservation_id))
            settled_amount = amount
        elif reservation_state in {"released", "consumed"}:
            if reservation_state != outcome_value:
                raise AtomicLedgerError("settled_cycle_outcome_drift")
            settled_amount = 0.0
        else:
            raise AtomicLedgerError("reservation_state_invalid")
        conn.execute("UPDATE basket_target_execution_cycles SET state='settled',updated_at=? WHERE cycle_id=?", (closed_at, cycle))
    return {"schema": SCHEMA, "state": "settled", "cycle_id": cycle, "outcome": outcome_value, "released_or_consumed_usdc": float(settled_amount)}


def close_settled_day_cycle(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    settlement_day_end_utc: str,
    closed_at_utc: str,
    reason: str = "settlement_day_closed",
) -> dict[str, Any]:
    """Retire a prepared cycle whose settlement day closed without a successor.

    A prepared cycle holds its capital reservation until a newer decision on
    the same station-day supersedes it, and ``supersede_prepared_cycle_and_
    commit_bundle`` is the only path in production that releases one.  A
    station-day whose settlement window has closed can never receive that
    successor, so the last cycle of every day that traded strands its
    reservation for good: the pool's ``reserved_usdc`` never comes back and
    the Tier-1 portfolio gate keeps counting exposure nobody holds.  Measured
    2026-08-26 -- 161.32 USDC across 54 station-days against a 180.00 breach
    threshold that zeroes every station's budget.

    The caller proves the day is over by passing that day's settlement-window
    end; this only checks the close happens at or after it, so the timezone
    rule stays in ``settlement_time.settlement_day_state`` alone.

    Refuses anything still live: an intent authorised to submit, an intent
    past ``prepared``, or any execution fact against any intent the cycle
    ever had.  It admits two cases supersession refuses -- a cycle whose
    intents were already cancelled, and a cycle with none -- because
    supersession runs while the day can still trade and this runs after it
    cannot, where both are simply inert.
    """

    cycle = _text("cycle_id", cycle_id)
    day_end = _timestamp("settlement_day_end_utc", settlement_day_end_utc)
    closed_at = _timestamp("closed_at_utc", closed_at_utc)
    close_reason = _text("close_reason", reason)
    # Compare instants, not spellings.  Text ordering agrees with time only
    # while both sides are Z-suffixed at the same precision: a ``+14:00``
    # close sorts before an earlier ``Z`` day end and would be let through,
    # and a ``.000Z`` close sorts after ``Z`` at the same instant and would
    # be refused.
    if _timestamp_instant("closed_at_utc", closed_at) < _timestamp_instant(
        "settlement_day_end_utc", day_end
    ):
        raise AtomicLedgerError("settled_day_close_before_settlement_day_end")

    with _atomic_transaction(conn, operation="close_settled_day_cycle"):
        row = conn.execute(
            "SELECT state FROM basket_target_execution_cycles WHERE cycle_id=?",
            (cycle,),
        ).fetchone()
        if row is None:
            raise AtomicLedgerError("cycle_missing")
        if str(row[0]) == "settled":
            return {
                "schema": SCHEMA,
                "state": "already_settled",
                "cycle_id": cycle,
                "cancelled_intent_count": 0,
                "released_usdc": 0.0,
            }

        intents = conn.execute(
            "SELECT order_intent_id,state,live_submit_authorized "
            "FROM basket_target_order_intents WHERE cycle_id=? "
            "ORDER BY order_intent_id",
            (cycle,),
        ).fetchall()
        if any(int(item[2] or 0) != 0 for item in intents):
            raise AtomicLedgerError("settled_day_close_live_intent_present")
        if any(
            str(item[1]) not in {"prepared", "cancelled", "failed"}
            for item in intents
        ):
            raise AtomicLedgerError("settled_day_close_intent_not_quiescent")

        # Ask about every intent the cycle ever had, not just the ones still
        # open.  A cycle whose intents were all cancelled has an empty open
        # list, and hanging the inertness checks off that list skipped them
        # entirely -- a cancelled intent carrying a FILL event released its
        # reservation without a single table being read.
        all_intent_ids = [str(item[0]) for item in intents]
        if all_intent_ids:
            placeholders = ",".join("?" for _ in all_intent_ids)
            execution_tables = {
                str(item[0])
                for item in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for table, column in _EXECUTION_FACT_CHECKS:
                if table not in execution_tables:
                    continue
                if conn.execute(
                    f"SELECT 1 FROM {table} WHERE {column} IN ({placeholders}) "
                    "LIMIT 1",
                    all_intent_ids,
                ).fetchone() is not None:
                    raise AtomicLedgerError(
                        "settled_day_close_execution_fact_present"
                    )

        open_intent_ids = [
            str(item[0]) for item in intents if str(item[1]) == "prepared"
        ]
        if open_intent_ids:
            placeholders = ",".join("?" for _ in open_intent_ids)
            conn.execute(
                f"UPDATE basket_target_order_intents SET state='cancelled' "
                f"WHERE order_intent_id IN ({placeholders}) AND state='prepared'",
                open_intent_ids,
            )
            if conn.execute("SELECT changes()").fetchone()[0] != len(
                open_intent_ids
            ):
                raise AtomicLedgerError("settled_day_close_intent_cas_conflict")

        # Reuse the one implementation of the release arithmetic rather than
        # writing a third: it nests on a savepoint inside this transaction,
        # so a refusal there rolls the intent cancellations back with it.
        closed = close_target_cycle(
            conn,
            cycle_id=cycle,
            outcome="released",
            closed_at_utc=closed_at,
            reason=close_reason,
        )

    return {
        "schema": SCHEMA,
        "state": closed["state"],
        "cycle_id": cycle,
        "cancelled_intent_count": len(open_intent_ids),
        "released_usdc": float(closed.get("released_or_consumed_usdc") or 0.0),
    }


def supersede_prepared_cycle_and_commit_bundle(
    conn: sqlite3.Connection,
    *,
    old_cycle_id: str,
    new_decision_scope_key: str,
    bundle: Mapping[str, Any],
    superseded_at_utc: str,
    fault_after: str | None = None,
) -> dict[str, Any]:
    """Atomically retire one inert no-live candidate and commit its successor.

    Supersession is permitted only while every old intent is still local
    ``prepared`` state, no execution/reconciliation fact exists, and the
    reservation remains active.  The immutable old decision and training fact
    are retained; an append-only receipt links them to the new authority
    scope.  Any failure in the new bundle rolls the entire retirement back.
    """

    cycle_id = _text("old_cycle_id", old_cycle_id)
    new_scope = _text("new_decision_scope_key", new_decision_scope_key)
    superseded_at = _timestamp("superseded_at_utc", superseded_at_utc)
    if bundle.get("schema") != SCHEMA:
        raise AtomicLedgerError("supersession_bundle_schema_invalid")
    new_cycle = _text(
        "supersession_new_cycle_id",
        (bundle.get("cycle") or {}).get("cycle_id")
        if isinstance(bundle.get("cycle"), Mapping)
        else None,
    )
    new_decision = _text(
        "supersession_new_decision_id",
        (bundle.get("decision") or {}).get("decision_id")
        if isinstance(bundle.get("decision"), Mapping)
        else None,
    )

    with _atomic_transaction(
        conn, operation="supersede_prepared_cycle_and_commit_bundle"
    ):
        existing = conn.execute(
            "SELECT new_cycle_id,new_decision_id,new_decision_scope_key,"
            "content_sha256 FROM basket_target_prepared_supersession_receipts "
            "WHERE old_cycle_id=?",
            (cycle_id,),
        ).fetchone()
        if existing is not None:
            if (
                str(existing[0]) != new_cycle
                or str(existing[1]) != new_decision
                or str(existing[2]) != new_scope
            ):
                raise AtomicLedgerError("prepared_supersession_content_drift")
            committed = commit_target_atomic_bundle(conn, bundle=bundle)
            return {
                **committed,
                "supersession_state": "already_committed",
                "supersession_content_sha256": str(existing[3]),
                "superseded_cycle_id": cycle_id,
            }

        row = conn.execute(
            "SELECT c.state,d.decision_id,d.decided_state,d.will_order,"
            "f.fact_id,f.decision_scope_key "
            "FROM basket_target_execution_cycles c "
            "JOIN auto_trade_decisions d ON d.cycle_id=c.cycle_id "
            "JOIN basket_policy_training_fact f "
            "ON f.cycle_id=c.cycle_id AND f.decision_id=d.decision_id "
            "WHERE c.cycle_id=?",
            (cycle_id,),
        ).fetchall()
        if len(row) != 1:
            raise AtomicLedgerError(
                "prepared_supersession_authority_binding_invalid"
            )
        (
            cycle_state,
            old_decision_id,
            decision_state,
            will_order,
            old_fact_id,
            old_scope,
        ) = row[0]
        if str(cycle_state) != "prepared" or str(decision_state) != "prepared":
            raise AtomicLedgerError("prepared_supersession_state_invalid")
        if int(will_order) != 0:
            raise AtomicLedgerError("prepared_supersession_will_order_forbidden")
        if str(old_scope) == new_scope:
            raise AtomicLedgerError("prepared_supersession_scope_unchanged")

        intents = conn.execute(
            "SELECT order_intent_id,state,live_submit_authorized "
            "FROM basket_target_order_intents WHERE cycle_id=? "
            "ORDER BY order_intent_id",
            (cycle_id,),
        ).fetchall()
        if not intents:
            raise AtomicLedgerError("prepared_supersession_intent_missing")
        if any(
            str(item[1]) != "prepared" or int(item[2]) != 0
            for item in intents
        ):
            raise AtomicLedgerError("prepared_supersession_intent_not_quiescent")
        intent_ids = [str(item[0]) for item in intents]
        placeholders = ",".join("?" for _ in intent_ids)
        execution_tables = {
            str(item[0])
            for item in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for table, column in _EXECUTION_FACT_CHECKS:
            if table not in execution_tables:
                continue
            found = conn.execute(
                f"SELECT 1 FROM {table} WHERE {column} IN ({placeholders}) "
                "LIMIT 1",
                intent_ids,
            ).fetchone()
            if found is not None:
                raise AtomicLedgerError(
                    "prepared_supersession_execution_fact_present"
                )

        reservation = conn.execute(
            "SELECT reservation_id,amount_usdc,state "
            "FROM basket_target_capital_reservations WHERE cycle_id=?",
            (cycle_id,),
        ).fetchone()
        if reservation is None or str(reservation[2]) != "active":
            raise AtomicLedgerError(
                "prepared_supersession_reservation_not_active"
            )
        reservation_id = str(reservation[0])
        released_amount = float(reservation[1])
        _balance, reserved, _epoch, _version = _ensure_pool(conn)
        if released_amount > reserved + 1e-9:
            raise AtomicLedgerError("pool_reserved_balance_drift")

        cancelled_json = canonical_json(intent_ids)
        receipt_body = {
            "schema": "basket_target_prepared_supersession_receipt_v1",
            "old_cycle_id": cycle_id,
            "old_decision_id": str(old_decision_id),
            "old_training_fact_id": str(old_fact_id),
            "old_decision_scope_key": str(old_scope),
            "new_cycle_id": new_cycle,
            "new_decision_id": new_decision,
            "new_decision_scope_key": new_scope,
            "reservation_id": reservation_id,
            "released_amount_usdc": round(released_amount, 8),
            "cancelled_intent_ids": intent_ids,
            "reason": "authority_input_superseded",
            "superseded_at": superseded_at,
        }
        receipt_sha = content_sha256(receipt_body)
        supersession_id = _derive_id("supersession", receipt_body)

        conn.execute(
            f"UPDATE basket_target_order_intents SET state='cancelled' "
            f"WHERE order_intent_id IN ({placeholders}) AND state='prepared'",
            intent_ids,
        )
        if conn.execute("SELECT changes()").fetchone()[0] != len(intent_ids):
            raise AtomicLedgerError("prepared_supersession_intent_cas_conflict")
        conn.execute(
            "UPDATE basket_target_capital_reservations "
            "SET state='released',release_reason='authority_input_superseded',"
            "updated_at=? WHERE reservation_id=? AND state='active'",
            (superseded_at, reservation_id),
        )
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            raise AtomicLedgerError(
                "prepared_supersession_reservation_cas_conflict"
            )
        conn.execute(
            "UPDATE basket_target_capital_pool "
            "SET reserved_usdc=reserved_usdc-?,version=version+1,updated_at=? "
            "WHERE pool_id=1 AND reserved_usdc>=?",
            (released_amount, superseded_at, released_amount),
        )
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            raise AtomicLedgerError("prepared_supersession_pool_cas_conflict")
        conn.execute(
            "UPDATE basket_target_execution_cycles "
            "SET state='settled',updated_at=? "
            "WHERE cycle_id=? AND state='prepared'",
            (superseded_at, cycle_id),
        )
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            raise AtomicLedgerError("prepared_supersession_cycle_cas_conflict")
        _fault(fault_after, "supersede_old")

        committed = commit_target_atomic_bundle(
            conn,
            bundle=bundle,
            fault_after=(
                fault_after.removeprefix("new_")
                if isinstance(fault_after, str)
                and fault_after.startswith("new_")
                else None
            ),
        )
        conn.execute(
            "INSERT INTO basket_target_prepared_supersession_receipts("
            "supersession_id,old_cycle_id,old_decision_id,"
            "old_training_fact_id,old_decision_scope_key,new_cycle_id,"
            "new_decision_id,new_decision_scope_key,reservation_id,"
            "released_amount_usdc,cancelled_intent_ids_json,reason,"
            "superseded_at,receipt_json,content_sha256) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                supersession_id,
                cycle_id,
                str(old_decision_id),
                str(old_fact_id),
                str(old_scope),
                new_cycle,
                new_decision,
                new_scope,
                reservation_id,
                released_amount,
                cancelled_json,
                "authority_input_superseded",
                superseded_at,
                canonical_json(receipt_body),
                receipt_sha,
            ),
        )
        _fault(fault_after, "supersession_receipt")
    return {
        **committed,
        "supersession_state": "committed",
        "supersession_id": supersession_id,
        "supersession_content_sha256": receipt_sha,
        "superseded_cycle_id": cycle_id,
        "released_amount_usdc": released_amount,
        "cancelled_intent_count": len(intent_ids),
    }


def arbitrate_capital_requests(
    requests: Sequence[Mapping[str, Any]],
    *,
    available_usdc: float,
    aging_step: float = 10.0,
) -> dict[str, Any]:
    """Deterministically choose all-or-none requests from the shared pool.

    ``priority`` is a policy input, not a source or market quota.  Aging is
    bounded only by the supplied request values and eventually lets an older
    low-priority request outrank a newer one, which prevents starvation while
    preserving a stable tie break.
    """

    available = _number("available_usdc", available_usdc)
    step = _number("aging_step", aging_step)
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(requests):
        request_id = _text(f"request_{index}_id", raw.get("request_id"))
        if request_id in seen:
            raise AtomicLedgerError("capital_request_duplicate")
        seen.add(request_id)
        lane = _basket_lane(f"request_{index}_lane", raw.get("strategy_lane"))
        amount = _number(f"request_{index}_amount", raw.get("amount_usdc"), positive=True)
        try:
            priority = float(raw.get("priority", 0.0))
            starvation = float(raw.get("starvation_count", 0.0))
        except (TypeError, ValueError) as exc:
            raise AtomicLedgerError("capital_request_priority_invalid") from exc
        if not math.isfinite(priority) or not math.isfinite(starvation) or priority < 0 or starvation < 0:
            raise AtomicLedgerError("capital_request_priority_invalid")
        requested_at = _timestamp(f"request_{index}_requested_at", raw.get("requested_at"))
        effective = priority + starvation * step
        normalized.append({
            "request_id": request_id, "strategy_lane": lane, "amount_usdc": amount,
            "priority": priority, "starvation_count": starvation, "requested_at": requested_at,
            "effective_priority": effective,
        })
    normalized.sort(key=lambda row: (-row["effective_priority"], row["requested_at"], row["request_id"]))
    remaining = available
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in normalized:
        if row["amount_usdc"] <= remaining + 1e-9:
            accepted.append({**row, "state": "accepted"})
            remaining -= row["amount_usdc"]
        else:
            rejected.append({**row, "state": "deferred_insufficient_capital"})
    return {
        "schema": "basket_target_capital_arbitration_v1",
        "accepted": accepted,
        "rejected": rejected,
        "available_usdc": available,
        "accepted_usdc": available - remaining,
        "remaining_usdc": remaining,
        "deterministic": True,
        "shared_pool": True,
        "starvation_protection": True,
    }


__all__ = [
    "AtomicLedgerError",
    "CYCLE_STATES",
    "InjectedAtomicFault",
    "INTENT_STATES",
    "LANES",
    "PRODUCTION_LEDGER_DDL",
    "SCHEMA",
    "TARGET_LEDGER_TABLES",
    "apply_target_ledger_schema",
    "arbitrate_capital_requests",
    "build_target_atomic_bundle",
    "close_settled_day_cycle",
    "close_target_cycle",
    "supersede_prepared_cycle_and_commit_bundle",
    "commit_blocked_scope_decision",
    "commit_preliminary_scope_decision",
    "commit_stage_zero_execution_scope_decision",
    "commit_target_atomic_bundle",
    "initialize_capital_pool",
    "ledger_span",
    "ledger_span_profile",
    "note_ledger_span",
    "note_writer_lock_sample",
    "record_writer_lock_wait",
    "reset_ledger_spans",
    "reset_writer_lock_profile",
    "writer_lock_profile",
    "target_ledger_schema_status",
    "transition_target_cycle_state",
]
