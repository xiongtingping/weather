"""What each probability stage may open and adjust a position on.

The three stages are three data sources, not three confidence levels: P0 opens
on the NWP ensemble before the event day exists, P0-M re-confirms on the TAF
revision, and P1 adjusts on observed METAR.  The atomic ledger was written for
the last of those alone -- its chain roots on a METAR fact and its bucket floor
is the reported running high -- so the two stages that precede an observation
had no way to express their own evidence at all.

Each stage names the lineage its envelope must already carry, and the bucket
floor it is entitled to.  P1 keeps the hard bound: an observed high has ruled
those buckets out and no forecast can put them back.  P0 and P0-M record a
soft bound from the model envelope instead -- the interval the calibrated
member centers actually span.  Since charter 82 (2026-09-05) that soft bound
is an audit fact persisted beside the open, not a gate on it: the "model
envelope as an admissibility boundary" idea was retired from every other layer
by charters 42, 45, 56 and 76, and under charters 50/59/79 a legal window is
wider than most envelopes.  ``assert_intent_buckets_within_bound`` is the
retired gate arm, reachable only through ``PM_CHARTER_82_ENVELOPE_SOFT_BOUND=gate``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import sqlite3
from typing import Any

from .contracts import ContractError, content_sha256


SCHEMA = "bayes_basket_target_stage_evidence_root_v1"
HARD_BOUND_KIND = "OBSERVED_REPORTED_HIGH_HARD"
SOFT_BOUND_KIND = "MODEL_ENVELOPE_SOFT"

# The lineage keys each stage's envelope already publishes.  Nothing here is
# new evidence: it is the evidence the stage always had, named so the ledger
# can bind to it the way it binds to an observation.
STAGE_EVIDENCE_FIELDS: Mapping[str, tuple[str, ...]] = {
    "P0": (
        "native_selection_evidence_sha256",
        "native_cross_activation_sha256",
        "native_cross_ensemble_artifact_sha256",
    ),
    "P0-M": (
        "taf_revision_proof_sha256",
        "taf_revision_artifact_sha256",
        "taf_revision_activation_id",
        "forecast_revision_id",
    ),
    # What the P1 envelope itself publishes, which is not the same list the
    # ledger's plan lineage demands: the plan additionally carries
    # ``truth_observation_revision_content_sha256``, which most committed P1
    # envelopes still do not.  ("None ever has" was true when this was
    # written; measured 2026-08-26 it is 11,889 of 105,828 since 08-20, one
    # for every observation the PR12 lane has identified.)  Requiring it here
    # would mislabel that separate, real coverage gap as an evidence-root
    # failure.
    "P1": (
        "canonical_observation_event_id",
        "canonical_observation_committed_at",
        "hard_bound_id",
        "source_lane",
        "report_type",
    ),
}
STAGE_BOUND_KIND: Mapping[str, str] = {
    "P0": SOFT_BOUND_KIND,
    "P0-M": SOFT_BOUND_KIND,
    "P1": HARD_BOUND_KIND,
}
# P0 opens the position; the later stages adjust one.  Recorded so a receipt
# says which of the two a cycle was, rather than leaving it to be inferred
# from whether a position happened to exist.
STAGE_POSITION_ROLE: Mapping[str, str] = {
    "P0": "OPEN",
    "P0-M": "ADJUST",
    "P1": "ADJUST",
}


class StageEvidenceError(ContractError):
    """Raised when a stage cannot substantiate the position it is asking for."""


def _stage(value: Any) -> str:
    stage = str(value or "").strip().upper()
    if stage == "P0M":
        stage = "P0-M"
    if stage not in STAGE_EVIDENCE_FIELDS:
        raise StageEvidenceError("stage_evidence_probability_stage_invalid")
    return stage


def _ordinal(field: str, value: Any) -> int:
    try:
        if isinstance(value, bool):
            raise TypeError(field)
        return int(value)
    except (TypeError, ValueError) as exc:
        raise StageEvidenceError(f"stage_evidence_{field}_invalid") from exc


def build_stage_evidence_root(
    *,
    probability_stage: str,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    checkpoint_id: str,
    posterior_sha256: str,
    lineage: Mapping[str, Any],
) -> dict[str, Any]:
    """Name the evidence one stage stands on, as a content-addressed root.

    This is the analogue of the observation fact at the head of the P1 chain:
    the thing every later identity derives its scope from, and the thing a
    replay has to reproduce byte for byte.
    """

    stage = _stage(probability_stage)
    fields = STAGE_EVIDENCE_FIELDS[stage]
    if stage == "P0-M" and not str(
        lineage.get("taf_revision_proof_sha256") or ""
    ).strip():
        # Three routes produce a TAF-confirmation stage, and this contract
        # knew two of them.  The transport route carries a calibrated revision
        # artifact and names it with the fields above.  The member route --
        # the forecaster's TX serving inside the cross ensemble, where a
        # recompile whose only change is a fresh TAF issuance is staged P0-M
        # -- has no transport artifact; its evidence is the ensemble evidence
        # plus the issuance witness that drove it.
        #
        # The observed route (``materialize_p0m_observed``) seats the stage on
        # the forecast alone: the likelihood is identity, the probabilities
        # are the P0 root's unchanged, and the lineage says so under
        # ``revision_mode`` and ``taf_revision_state``.  It was added on
        # 2026-08-17, four days after this contract was written, and has
        # been every P0-M envelope in production since -- 4,708 rows, the
        # transport route's last 45 being from July and the member route's
        # count being zero.  Landing here without a branch of its own, it
        # was asked for the member route's witness, which it structurally
        # never carries, so no P0-M evidence root has ever been built:
        # 10,552 P0 roots, none for P0-M, and on 2026-09-01 the stage-open
        # of 808 decisions across the forward day refused on it.
        #
        # What this route stands on is exactly what it records: which
        # forecast revision, from which source object, what TX it read,
        # and which P0 root it left unchanged.  All six are present on every
        # observed row ever written.
        if str(lineage.get("revision_mode") or "").strip() == (
            "observed_without_artifact"
        ):
            fields = (
                "forecast_revision_id",
                "forecast_source_object_sha256",
                "taf_revision_state",
                "taf_observed_tx_c",
                "p0_root_content_sha256",
                "p0_root_posterior_sha256",
            )
        else:
            fields = (
                "taf_envelope_witness_json",
                "native_selection_evidence_sha256",
                "native_cross_activation_sha256",
                "native_cross_ensemble_artifact_sha256",
            )
    evidence: dict[str, str] = {}
    for field in fields:
        value = str(lineage.get(field) or "").strip()
        if not value:
            raise StageEvidenceError(
                f"stage_evidence_{field}_missing"
            )
        evidence[field] = value
    identity = {
        "schema": SCHEMA,
        "probability_stage": stage,
        "position_role": STAGE_POSITION_ROLE[stage],
        "bound_kind": STAGE_BOUND_KIND[stage],
        "station_id": str(station_id or "").strip().upper(),
        "event_date": str(event_date or "").strip(),
        "settlement_timezone": str(settlement_timezone or "").strip(),
        "checkpoint_id": str(checkpoint_id or "").strip(),
        "posterior_sha256": str(posterior_sha256 or "").strip().lower(),
        "evidence": evidence,
    }
    for field in (
        "station_id", "event_date", "settlement_timezone",
        "checkpoint_id", "posterior_sha256",
    ):
        if not identity[field]:
            raise StageEvidenceError(f"stage_evidence_{field}_missing")
    return {
        **identity,
        "evidence_root_id": "stageroot-" + content_sha256(identity)[:32],
        "content_sha256": content_sha256(identity),
    }


def build_model_envelope_soft_bound(
    *,
    probability_stage: str,
    lineage: Mapping[str, Any],
    bucket_ordinals: Mapping[str, int],
) -> dict[str, Any]:
    """Describe the model envelope beside a pre-observation open.

    The envelope is the interval the calibrated member centers span, so a
    bucket outside it is one no member pointed at.  It is persisted as an
    audit fact with the stage root (charter 82); a stage with no envelope
    published still cannot describe one and is refused here, because an
    absent envelope must not read as an empty description.
    """

    stage = _stage(probability_stage)
    if STAGE_BOUND_KIND[stage] != SOFT_BOUND_KIND:
        raise StageEvidenceError("stage_evidence_soft_bound_not_applicable")
    ordinals = {
        str(bucket): _ordinal("bucket_ordinal", ordinal)
        for bucket, ordinal in (bucket_ordinals or {}).items()
    }
    if not ordinals:
        raise StageEvidenceError("stage_evidence_bucket_ordinals_missing")
    raw_lower = lineage.get("model_envelope_lower_ordinal")
    raw_upper = lineage.get("model_envelope_upper_ordinal")
    if raw_lower is None or raw_upper is None:
        # Envelopes committed before the ordinals were published name the
        # boundary buckets only, and those names resolve through the
        # envelope's own ordinal contract.  Deriving them is what lets the
        # bound apply to the committed fleet instead of only to whatever is
        # written from now on -- an unresolvable name still fails.
        named_lower = str(lineage.get("model_envelope_lower_bucket") or "")
        named_upper = str(lineage.get("model_envelope_upper_bucket") or "")
        if not named_lower or not named_upper:
            raise StageEvidenceError("stage_evidence_model_envelope_missing")
        if named_lower not in ordinals or named_upper not in ordinals:
            raise StageEvidenceError("stage_evidence_model_envelope_invalid")
        raw_lower = ordinals[named_lower]
        raw_upper = ordinals[named_upper]
    lower = _ordinal("model_envelope_lower_ordinal", raw_lower)
    upper = _ordinal("model_envelope_upper_ordinal", raw_upper)
    if lower > upper:
        raise StageEvidenceError("stage_evidence_model_envelope_invalid")
    admitted = sorted(
        (bucket for bucket, value in ordinals.items() if lower <= value <= upper),
        key=lambda bucket: (ordinals[bucket], bucket),
    )
    if not admitted:
        raise StageEvidenceError("stage_evidence_model_envelope_admits_nothing")
    excluded = sorted(
        (bucket for bucket in ordinals if bucket not in set(admitted)),
        key=lambda bucket: (ordinals[bucket], bucket),
    )
    identity = {
        "schema": SCHEMA,
        "bound_kind": SOFT_BOUND_KIND,
        "probability_stage": stage,
        "soft_lower_ordinal": lower,
        "soft_upper_ordinal": upper,
        "admitted_buckets": admitted,
        "excluded_buckets": excluded,
    }
    return {
        **identity,
        "bound_id": "softbound-" + content_sha256(identity)[:32],
        "content_sha256": content_sha256(identity),
    }


def persist_stage_evidence(
    conn: sqlite3.Connection,
    *,
    evidence_root: Mapping[str, Any],
    soft_bound: Mapping[str, Any] | None = None,
    created_at_utc: str,
) -> dict[str, Any]:
    """Append the root, and the soft bound when the stage has one.

    Both tables are append-only, so a re-derived identity is a no-op rather
    than a conflict: the same evidence yields the same content hash, and a
    second pass over an unchanged envelope must not look like a new position.
    """

    created_at = str(created_at_utc or "").strip()
    if not created_at:
        raise StageEvidenceError("stage_evidence_created_at_missing")
    root_sha = str(evidence_root.get("content_sha256") or "")
    if len(root_sha) != 64:
        raise StageEvidenceError("stage_evidence_root_sha_invalid")
    root_id = str(evidence_root.get("evidence_root_id") or "")
    if not root_id:
        raise StageEvidenceError("stage_evidence_root_id_missing")
    existing = conn.execute(
        "SELECT evidence_root_id FROM basket_target_stage_evidence_roots "
        "WHERE content_sha256=?",
        (root_sha,),
    ).fetchone()
    wrote_root = existing is None
    if wrote_root:
        conn.execute(
            "INSERT INTO basket_target_stage_evidence_roots("
            "evidence_root_id,probability_stage,position_role,bound_kind,"
            "station_id,event_date,settlement_timezone,checkpoint_id,"
            "posterior_sha256,evidence_json,content_sha256,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                root_id,
                str(evidence_root["probability_stage"]),
                str(evidence_root["position_role"]),
                str(evidence_root["bound_kind"]),
                str(evidence_root["station_id"]),
                str(evidence_root["event_date"]),
                str(evidence_root["settlement_timezone"]),
                str(evidence_root["checkpoint_id"]),
                str(evidence_root["posterior_sha256"]),
                json.dumps(
                    dict(evidence_root.get("evidence") or {}),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                root_sha,
                created_at,
            ),
        )
    else:
        root_id = str(existing[0])
    wrote_bound = False
    bound_id = ""
    if soft_bound is not None:
        bound_sha = str(soft_bound.get("content_sha256") or "")
        if len(bound_sha) != 64:
            raise StageEvidenceError("stage_evidence_bound_sha_invalid")
        bound_id = str(soft_bound.get("bound_id") or "")
        if not bound_id:
            raise StageEvidenceError("stage_evidence_bound_id_missing")
        prior = conn.execute(
            "SELECT bound_id FROM basket_target_stage_bound_facts "
            "WHERE content_sha256=?",
            (bound_sha,),
        ).fetchone()
        wrote_bound = prior is None
        if wrote_bound:
            conn.execute(
                "INSERT INTO basket_target_stage_bound_facts("
                "stage_bound_fact_id,bound_id,evidence_root_id,"
                "probability_stage,bound_kind,soft_lower_ordinal,"
                "soft_upper_ordinal,admitted_buckets_json,"
                "excluded_buckets_json,content_sha256,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "stagebound-" + bound_sha[:32],
                    bound_id,
                    root_id,
                    str(soft_bound["probability_stage"]),
                    str(soft_bound["bound_kind"]),
                    int(soft_bound["soft_lower_ordinal"]),
                    int(soft_bound["soft_upper_ordinal"]),
                    json.dumps(
                        list(soft_bound.get("admitted_buckets") or []),
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        list(soft_bound.get("excluded_buckets") or []),
                        separators=(",", ":"),
                    ),
                    bound_sha,
                    created_at,
                ),
            )
        else:
            bound_id = str(prior[0])
    return {
        "schema": SCHEMA,
        "state": "committed" if (wrote_root or wrote_bound) else "idempotent",
        "evidence_root_id": root_id,
        "bound_id": bound_id,
        "wrote_evidence_root": wrote_root,
        "wrote_soft_bound": wrote_bound,
        "orders_submitted": 0,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
    }


def assert_intent_buckets_within_bound(
    *,
    bound: Mapping[str, Any],
    intent_buckets: Sequence[str],
) -> None:
    """The retired gate arm (charter 82): refuse a basket outside the soft bound.

    Production calls this only under ``PM_CHARTER_82_ENVELOPE_SOFT_BOUND=gate``;
    the default ``audit`` persists the bound and opens on the windows charters
    49/58/79 admit.
    """

    admitted = {str(bucket) for bucket in (bound.get("admitted_buckets") or ())}
    if not admitted:
        raise StageEvidenceError("stage_evidence_bound_admits_nothing")
    outside = sorted({
        str(bucket) for bucket in intent_buckets
    } - admitted)
    if outside:
        raise StageEvidenceError(
            "stage_evidence_intent_outside_bound:" + ",".join(outside)
        )


__all__ = [
    "HARD_BOUND_KIND",
    "SCHEMA",
    "SOFT_BOUND_KIND",
    "STAGE_BOUND_KIND",
    "STAGE_EVIDENCE_FIELDS",
    "STAGE_POSITION_ROLE",
    "StageEvidenceError",
    "assert_intent_buckets_within_bound",
    "build_model_envelope_soft_bound",
    "build_stage_evidence_root",
    "persist_stage_evidence",
]
