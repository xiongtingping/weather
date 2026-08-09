"""A source gap must not become a standstill.

126 station-days from 2026-08-02 had no eligible hourly report -- the AWC feed
simply had nothing for them.  They filled the 45-scope batch window, so every
batch came back all-failed; the caller treated that as a system failure and
declined to commit the cursor, and the same unsettleable scopes were reselected
on every cycle.  No later station-day was ever reached.  Settled truth stopped
for all 45 stations, and with it P1 and the METAR trajectory training folds.

"Nothing to settle here" and "what is here cannot be trusted" are different
answers and only the second one should stop the batch.
"""

from __future__ import annotations

import pytest

from bayes_basket_target.settlement_truth import (
    AVAILABILITY_BLOCKER_REASONS,
    blocker_class,
)


@pytest.mark.parametrize(
    "reason",
    [
        "ContractError:settlement_truth_hourly_observation_missing",
        "ContractError:settlement_truth_canonical_ssot_fact_missing",
        "ContractError:settlement_truth_scope_missing",
    ],
)
def test_an_absent_source_is_an_availability_blocker(reason: str) -> None:
    # Retrying these forever cannot change the answer: the day had no eligible
    # report and never will.
    assert blocker_class(reason) == "AVAILABILITY"


@pytest.mark.parametrize(
    "reason",
    [
        # Two reports claiming different temperatures for one valid time.
        "ContractError:settlement_truth_hourly_revision_ambiguous",
        "ContractError:settlement_truth_temperature_invalid",
        "ContractError:settlement_truth_observation_time_invalid",
        "ContractError:settlement_truth_canonical_scope_mismatch",
    ],
)
def test_a_contradiction_is_a_consistency_blocker(reason: str) -> None:
    # Skipping past a contradiction leaves it behind unresolved, so these still
    # stop the batch and hold the cursor.
    assert blocker_class(reason) == "CONSISTENCY"


def test_an_unrecognised_reason_defaults_to_stopping_the_batch() -> None:
    # A new error code should have to be classified on purpose.  Defaulting the
    # other way is how a real contradiction would get walked past.
    assert blocker_class("ValueError:something_nobody_has_seen") == "CONSISTENCY"
    assert blocker_class("") == "CONSISTENCY"


def test_a_suffixed_reason_is_classified_on_its_head() -> None:
    # Several codes carry a detail suffix; the class comes from the code.
    assert blocker_class(
        "ContractError:settlement_truth_observation_source_policy_invalid"
        ":awc_metar_timing_invalid"
    ) == "CONSISTENCY"


def test_availability_set_is_a_deliberate_allowlist() -> None:
    # Small and explicit rather than pattern-matched: every member is a reason
    # someone decided was safe to walk past.
    assert AVAILABILITY_BLOCKER_REASONS
    assert all(
        reason.startswith("settlement_truth_")
        for reason in AVAILABILITY_BLOCKER_REASONS
    )
    assert "settlement_truth_hourly_revision_ambiguous" not in (
        AVAILABILITY_BLOCKER_REASONS
    )
