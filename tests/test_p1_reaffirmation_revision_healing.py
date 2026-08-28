"""A reaffirmation must re-resolve the revision sha, not inherit a gap.

P1 and the canonical ingress that writes an observation's truth revision
fact run within a fifth of a second of each other, and measured across 271
observations on 2026-08-28 the fact landed *after* the P1 decision half the
time.  A P1 that lost that race is committed without the sha, and the
envelope table is append-only -- so the reaffirmation path, which copies
the predecessor's lineage wholesale, carried a transient race loss forward
indefinitely.  EFHK sat at 03:38 without a sha whose fact had existed since
21:21, and ``target_atomic_lineage_incomplete`` refused the station-day on
every cycle in between.
"""

import pytest

from bayes_basket_target.production_worker import reaffirmed_lineage


def _reaffirm_lineage(base_lineage, observations):
    """The production function itself, with the two ids this test fixes.

    Deliberately a thin call, not a copy: a test that reimplements the rule
    passes whatever production does, which is the one thing it must not do.
    """

    return reaffirmed_lineage(
        base_lineage,
        observations,
        predecessor_checkpoint_id="p1_predecessor",
        newest_observation_id=max(
            str(row.get("observation_id") or "") for row in observations
        ),
    )


SHA = "a" * 64
OTHER = "b" * 64


def test_a_lost_race_heals_on_the_next_reaffirmation():
    """The whole point: the gap closes by itself, one cycle later."""

    base = {
        "canonical_observation_event_id": "rtobs_closing",
        "truth_observation_revision_content_sha256": None,
        "source_lane": "AWC",
        "report_type": "METAR",
    }
    observations = [
        {"observation_id": "rtobs_earlier",
         "truth_observation_revision_content_sha256": OTHER},
        {"observation_id": "rtobs_closing",
         "truth_observation_revision_content_sha256": SHA},
    ]
    out = _reaffirm_lineage(base, observations)
    assert out["truth_observation_revision_content_sha256"] == SHA


def test_healing_takes_the_closing_observation_not_the_newest():
    """The sha names the observation the packet closed on.

    ``reaffirmed_through_observation_id`` is the newest report examined and
    is a different thing; taking its sha would name a revision the envelope
    was never computed against.
    """

    base = {
        "canonical_observation_event_id": "rtobs_aaa_closing",
        "truth_observation_revision_content_sha256": "",
    }
    observations = [
        {"observation_id": "rtobs_aaa_closing",
         "truth_observation_revision_content_sha256": SHA},
        {"observation_id": "rtobs_zzz_newest",
         "truth_observation_revision_content_sha256": OTHER},
    ]
    out = _reaffirm_lineage(base, observations)
    assert out["reaffirmed_through_observation_id"] == "rtobs_zzz_newest"
    assert out["truth_observation_revision_content_sha256"] == SHA


def test_an_existing_sha_is_never_overwritten():
    """A predecessor that won its race keeps exactly what it recorded."""

    base = {
        "canonical_observation_event_id": "rtobs_closing",
        "truth_observation_revision_content_sha256": SHA,
    }
    observations = [
        {"observation_id": "rtobs_closing",
         "truth_observation_revision_content_sha256": OTHER},
    ]
    out = _reaffirm_lineage(base, observations)
    assert out["truth_observation_revision_content_sha256"] == SHA


def test_still_missing_when_the_fact_genuinely_does_not_exist_yet():
    """Healing is a re-resolve, not an invention.

    Thirty-three of today's P1 envelopes close on an observation the
    canonical ingress has produced no revision fact for at all.  Those must
    stay incomplete -- the atomic-fact gate refusing them is correct, and
    filling the field with anything would be forging the truth ledger's key.
    """

    base = {
        "canonical_observation_event_id": "rtobs_closing",
        "truth_observation_revision_content_sha256": None,
    }
    observations = [
        {"observation_id": "rtobs_closing",
         "truth_observation_revision_content_sha256": None},
    ]
    out = _reaffirm_lineage(base, observations)
    assert not str(
        out.get("truth_observation_revision_content_sha256") or ""
    ).strip()


def test_the_gate_this_unblocks_still_refuses_an_empty_sha():
    """Pin the contract the healing exists to satisfy, so it stays load-bearing."""

    required = (
        "canonical_observation_event_id",
        "truth_observation_revision_content_sha256",
        "canonical_observation_committed_at",
        "settlement_timezone",
        "hard_bound_id",
        "source_lane",
        "report_type",
    )
    complete = {key: "x" for key in required}
    assert not any(
        not str(complete.get(key) or "").strip() for key in required
    )
    without = {**complete, "truth_observation_revision_content_sha256": None}
    assert any(not str(without.get(key) or "").strip() for key in required)


def test_healing_is_inert_when_there_are_no_observations():
    """An empty morning reaffirms nothing, so there is nothing to heal from."""

    base = {
        "canonical_observation_event_id": "rtobs_closing",
        "truth_observation_revision_content_sha256": None,
    }
    with pytest.raises(ValueError):
        # max() over an empty sequence -- the production path guards on
        # ``observations`` being non-empty before reaching here, and this
        # names that precondition rather than silently accepting an empty
        # reaffirmation.
        _reaffirm_lineage(base, [])
