"""Basket support must be a contiguous, bounded temperature range.

Peripheral risk buckets pass evidence/Maker/probability/price gates that say
nothing about adjacency, so `base | bias | edge` could contain holes.  A live
RKPK basket selected ordinals {0, 2, 3, 5}, skipping two buckets inside its own
span: settlement in a skipped bucket pays nothing while the basket was priced as
if that outcome were covered.

Convergence only ever removes buckets, so every survivor keeps its original
admission evidence -- a hole is never bridged with a bucket that failed a gate.
"""

import pytest

from bayes_basket_target.basket_kernel.contracts import ContractError
from bayes_basket_target.basket_kernel.support import (
    _converge_contiguous_support,
)


ORDINALS = {
    "31°C or below": 0, "32°C": 1, "33°C": 2, "34°C": 3, "35°C": 4,
    "36°C": 5, "37°C": 6, "38°C": 7, "39°C": 8, "40°C": 9,
    "41°C or higher": 10,
}
BY_ORDINAL = {value: key for key, value in ORDINALS.items()}


def _converge(
    ordinals,
    *,
    center,
    probabilities=None,
    maximum=4,
    hedge_values=None,
    breakthrough_is_upper=None,
):
    admitted = {BY_ORDINAL[value] for value in ordinals}
    mass = {
        BY_ORDINAL[ordinal]: value
        for ordinal, value in (probabilities or {}).items()
    }
    hedge = {
        BY_ORDINAL[ordinal]: value
        for ordinal, value in (hedge_values or {}).items()
    }
    result = _converge_contiguous_support(
        admitted,
        center_bucket=BY_ORDINAL[center],
        ordinal_by_bucket=ORDINALS,
        probability_by_bucket={
            bucket: mass.get(bucket, 0.0) for bucket in ORDINALS
        },
        maximum_buckets=maximum,
        hedge_value_by_bucket=(
            {bucket: hedge.get(bucket, 0.0) for bucket in ORDINALS}
            if hedge_values is not None
            else None
        ),
        breakthrough_is_upper=breakthrough_is_upper,
    )
    return sorted(ORDINALS[bucket] for bucket in result)


def test_live_rkpk_hole_is_converged_to_the_run_containing_center() -> None:
    # The exact observed basket: center 34°C (ordinal 3), support {0, 2, 3, 5}.
    result = _converge([0, 2, 3, 5], center=3)

    # 2-3 is the only admitted run touching the center.  Ordinals 0 and 5 are
    # cut off by the failed buckets at 1 and 4, so they are dropped, not
    # bridged -- bridging would buy 32°C and 35°C, which failed their gates.
    assert result == [2, 3]


def test_already_contiguous_support_is_untouched() -> None:
    assert _converge([3, 4, 5, 6], center=4) == [3, 4, 5, 6]


def test_support_is_capped_at_four_buckets() -> None:
    result = _converge([3, 4, 5, 6, 7, 8], center=5)

    assert len(result) == 4
    assert result == sorted(result)
    assert 5 in result


def test_cap_drops_the_lower_probability_end_first() -> None:
    # Mass sits on the upper side, so the lower end is trimmed away.
    result = _converge(
        [2, 3, 4, 5, 6],
        center=4,
        probabilities={2: 0.01, 3: 0.02, 4: 0.40, 5: 0.30, 6: 0.20},
        maximum=3,
    )

    assert result == [4, 5, 6]


def test_cap_keeps_the_center_even_when_it_sits_at_an_end() -> None:
    result = _converge(
        [4, 5, 6, 7, 8],
        center=4,
        probabilities={4: 0.5, 5: 0.2, 6: 0.1, 7: 0.1, 8: 0.1},
    )

    assert result == [4, 5, 6, 7]


def test_edge_bucket_adjacent_to_core_is_kept_as_a_tail_hedge() -> None:
    # "31°C or below" absorbs the whole lower tail; when it stays adjacent to
    # the core it remains in support and hedges that tail.
    result = _converge([0, 1, 2, 3], center=2)

    assert result == [0, 1, 2, 3]
    assert BY_ORDINAL[0] == "31°C or below"


def test_sharper_probability_naturally_yields_a_smaller_basket() -> None:
    # Fewer admitted neighbours -> shorter run -> smaller basket, with no
    # separate convergence schedule.
    diffuse = _converge([2, 3, 4, 5], center=3)
    sharp = _converge([3, 4], center=3)

    assert len(diffuse) == 4
    assert len(sharp) == 2


def test_isolated_center_yields_a_single_bucket() -> None:
    assert _converge([0, 3, 7], center=3) == [3]


def test_center_outside_admitted_set_fails_closed() -> None:
    with pytest.raises(ContractError, match="support_center_not_admitted"):
        _converge([1, 2], center=5)


def test_result_never_contains_a_bucket_that_was_not_admitted() -> None:
    admitted = {0, 2, 3, 5, 6}
    result = _converge(sorted(admitted), center=5)

    assert set(result).issubset(admitted)


@pytest.mark.parametrize("maximum", [2, 3, 4, 5])
def test_result_is_always_contiguous_and_within_cap(maximum: int) -> None:
    result = _converge([0, 1, 2, 4, 5, 6, 7, 9], center=5, maximum=maximum)

    assert result == list(range(result[0], result[-1] + 1))
    assert len(result) <= maximum


# --- Hedge preservation -----------------------------------------------------
#
# The far bucket on the breakthrough side is held precisely because it is
# unlikely: it pays when the core pays nothing.  Ranking ends by raw
# probability always loses that comparison, so trimming would delete the tail
# cover first and leave the four dullest buckets.


def test_hedge_value_outranks_probability_when_trimming() -> None:
    # Ordinal 6 is the hedge: lowest probability, highest coverage value.
    result = _converge(
        [3, 4, 5, 6],
        center=4,
        probabilities={3: 0.20, 4: 0.45, 5: 0.30, 6: 0.05},
        hedge_values={3: 0.0, 4: 0.0, 5: 0.0, 6: 0.9},
        maximum=3,
    )

    assert result == [4, 5, 6]


def test_without_hedge_values_probability_still_decides() -> None:
    # Same shape, no coverage-value evidence: the old probability rule stands
    # and the improbable end goes first.
    result = _converge(
        [3, 4, 5, 6],
        center=4,
        probabilities={3: 0.20, 4: 0.45, 5: 0.30, 6: 0.05},
        maximum=3,
    )

    assert result == [3, 4, 5]


def test_direction_breaks_an_exact_tie_toward_the_breakthrough_end() -> None:
    result = _converge(
        [3, 4, 5],
        center=4,
        probabilities={3: 0.10, 4: 0.80, 5: 0.10},
        breakthrough_is_upper=True,
        maximum=2,
    )

    assert result == [4, 5]


def test_direction_down_keeps_the_lower_end_on_a_tie() -> None:
    result = _converge(
        [3, 4, 5],
        center=4,
        probabilities={3: 0.10, 4: 0.80, 5: 0.10},
        breakthrough_is_upper=False,
        maximum=2,
    )

    assert result == [3, 4]


def test_undirected_tie_stays_deterministic() -> None:
    result = _converge(
        [3, 4, 5],
        center=4,
        probabilities={3: 0.10, 4: 0.80, 5: 0.10},
        maximum=2,
    )

    assert result == [4, 5]


def test_zuuu_shape_core_plus_detached_hedge_stays_contiguous() -> None:
    # The reported shape: core at 32/33, hedge at 35, 34 pulled in to keep the
    # range whole.  Four contiguous buckets, hedge retained at the far end.
    result = _converge(
        [1, 2, 3, 4],
        center=2,
        probabilities={1: 0.34, 2: 0.38, 3: 0.14, 4: 0.06},
        hedge_values={4: 0.7},
        maximum=4,
    )

    assert result == [1, 2, 3, 4]


def test_hedge_still_cannot_survive_as_a_detached_island() -> None:
    # A hedge separated from the core by a bucket that failed its own gates is
    # dropped, not bridged: convergence never invents an unadmitted bucket.
    result = _converge(
        [2, 3, 6],
        center=3,
        probabilities={2: 0.30, 3: 0.50, 6: 0.05},
        hedge_values={6: 5.0},
        maximum=4,
    )

    assert result == [2, 3]
