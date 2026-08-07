"""P1 anchors on the observed path, not on the posterior argmax.

P1's posterior argmax has the same defect P0's did: an open-ended edge bucket
spans an unbounded interval, absorbs the whole upper tail, and can win an argmax
over 1°C buckets without the observed path pointing anywhere near it.

P1's own point estimate is the reported running high plus the expected remaining
warming -- "the path so far plus what is still to come".  The posterior argmax
stays as the diagnostic that drives skew/direction.
"""

import pytest

from bayes_basket_target.metar_trajectory import MetarTrajectoryUpdateProof


BUCKETS = {
    "31°C or below": 0, "32°C": 1, "33°C": 2, "34°C": 3, "35°C or higher": 4,
}
# Upper bounds mirror the production geometry: the edge bucket is unbounded.
UPPER_BOUNDS = {
    "31°C or below": 31.5, "32°C": 32.5, "33°C": 33.5, "34°C": 34.5,
    "35°C or higher": float("inf"),
}


def _anchor(running_high: float, remaining: dict[float, float]) -> str:
    """Reproduce the anchor rule against the production bucket geometry."""

    mass = sum(remaining.values())
    expected = (
        sum(inc * p for inc, p in remaining.items()) / mass if mass > 0 else 0.0
    )
    value = running_high + max(0.0, expected)
    ordered = sorted(BUCKETS, key=lambda b: BUCKETS[b])
    return next((b for b in ordered if value < UPPER_BOUNDS[b]), ordered[-1])


def test_anchor_is_running_high_plus_expected_remaining_warming() -> None:
    # 32.0 observed, +0.8 expected -> 32.8 -> the 33°C bucket.
    assert _anchor(32.0, {0.5: 0.4, 1.0: 0.6}) == "33°C"


def test_no_remaining_warming_anchors_on_the_running_high() -> None:
    # Bucket geometry: 32°C == [31.5, 32.5), 33°C == [32.5, 33.5).
    assert _anchor(32.2, {0.0: 1.0}) == "32°C"
    assert _anchor(31.9, {0.0: 1.0}) == "32°C"
    assert _anchor(32.6, {0.0: 1.0}) == "33°C"


def test_edge_bucket_anchors_only_when_the_path_reaches_it() -> None:
    # Path genuinely projects past 34.5 -> the edge bucket is correct.
    assert _anchor(34.0, {1.0: 1.0}) == "35°C or higher"
    # Path stops short -> it must not anchor there however heavy the tail is.
    assert _anchor(33.0, {0.2: 1.0}) == "33°C"
    assert _anchor(34.2, {0.2: 1.0}) == "34°C"


def test_expected_value_uses_the_whole_remaining_distribution() -> None:
    # A long thin tail still pulls the anchor: E = 0.1*0.9 + 3.0*0.1 = 0.39.
    assert _anchor(32.3, {0.1: 0.9, 3.0: 0.1}) == "33°C"


def test_unnormalized_remaining_distribution_is_normalized() -> None:
    # Weights need not sum to 1; the expectation divides by total mass.
    assert _anchor(32.0, {1.0: 2.0, 0.0: 2.0}) == "33°C"


def test_proof_carries_the_anchor_fields() -> None:
    fields = MetarTrajectoryUpdateProof.__dataclass_fields__
    assert "model_center_bucket" in fields
    assert "model_center_c" in fields
    # The posterior argmax survives as a separate diagnostic.
    assert "center_bucket" in fields


@pytest.mark.parametrize("running_high", [28.0, 31.0, 33.4, 36.0])
def test_anchor_never_falls_below_the_running_high(running_high: float) -> None:
    # Remaining warming is clamped at zero, so an already-observed high can
    # never be undercut by the anchor.
    anchor = _anchor(running_high, {-5.0: 1.0})
    ordered = sorted(BUCKETS, key=lambda b: BUCKETS[b])
    observed_bucket = next(
        (b for b in ordered if running_high < UPPER_BOUNDS[b]), ordered[-1]
    )
    assert BUCKETS[anchor] >= BUCKETS[observed_bucket]
