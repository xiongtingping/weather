"""Two reports for one valid time: agreeing ones are one fact, disagreeing ones
are resolved by publication order.

`official_tmax_c` is `max(reported_temperature_c)` and `raw_sha256` only reaches
the audit payload, so two reports for the same valid time that agree on
temperature cannot change the settled truth whichever is kept.

Requiring the raw bytes to match too treated ordinary METAR behaviour as a
contradiction -- a COR revision, a truncated transmission and the clean report
all carry the same temperature group but hash differently.  That fail-closed
settled-truth ingest for every station (46 stations/day down to 4), while
exactly one scope in the archive actually disagreed on temperature.

A genuine disagreement used to fail closed as well.  Operator ruling
2026-09-02: the later report supersedes the earlier one.  Refusing the pair
stranded the station-day and preserved no truth -- it discarded both readings
-- and over the whole archive the later report was warmer four times and
cooler five, so publication order carries no directional bias.  What the rule
does behaviourally is covered by
``tests/test_bayes_basket_target_settlement_truth.py``; this file checks the
properties that must survive any rewrite of the merge itself.
"""

import inspect

from bayes_basket_target import settlement_truth


SOURCE = inspect.getsource(settlement_truth)


def _dedupe_block() -> str:
    """The merge itself, delimited by its own statements rather than a length.

    A character count made this file break every time the rationale above the
    comparison grew, which taught the reader to widen the window rather than
    to look at what changed.
    """

    start = SOURCE.index("previous_index = hourly_by_valid_at.get(")
    end = SOURCE.index(
        'hourly_by_valid_at[item["valid_at_utc"]] = len(hourly)', start
    )
    return SOURCE[start:end]


def test_agreement_is_decided_on_temperature_alone() -> None:
    block = _dedupe_block()
    condition_start = block.index("if (")
    condition = block[condition_start : block.index("hourly[previous_index] = item")]

    assert "reported_temperature_c" in condition
    # Raw bytes and report type differ for benign reasons and must not decide
    # whether two reports state the same fact.
    assert "raw_sha256" not in condition
    assert "report_type" not in condition


def test_a_disagreement_replaces_rather_than_refusing() -> None:
    block = _dedupe_block()

    # The ambiguity refusal is gone from the merge, and nothing else in the
    # module reintroduces it under the old name.
    assert "settlement_truth_hourly_revision_ambiguous" not in SOURCE
    # The later item takes the earlier one's place; the day continues.
    assert "hourly[previous_index] = item" in block


def test_the_kept_report_is_the_later_one() -> None:
    # Publication order is only meaningful if the scan delivers the reports in
    # it.  ``canonical_committed_at`` is when this archive accepted the bytes,
    # not a claim the report makes about itself.
    assert "ORDER BY valid_at,canonical_committed_at,observation_id" in SOURCE


def test_truth_is_the_max_reported_temperature() -> None:
    # This is what makes the merge safe when both reports agree: the kept
    # report cannot alter the max.
    assert 'official_tmax_c = max(' in SOURCE
    assert '"reported_temperature_c"' in SOURCE


def test_raw_sha_still_recorded_for_audit() -> None:
    # Relaxing the equality must not drop the raw hash from the evidence.
    assert '"raw_sha256": raw_sha' in SOURCE
    assert '"observations": hourly' in SOURCE


def test_raw_sha_is_still_validated() -> None:
    # Dropping it from the equality does not mean accepting a malformed hash.
    assert "settlement_truth_raw_sha256_invalid" in SOURCE
