"""Agreeing reports for one valid time are one fact; disagreeing ones fail closed.

`official_tmax_c` is `max(reported_temperature_c)` and `raw_sha256` only reaches
the audit payload, so two reports for the same valid time that agree on
temperature cannot change the settled truth whichever is kept.

Requiring the raw bytes to match too treated ordinary METAR behaviour as a
contradiction -- a COR revision, a truncated transmission and the clean report
all carry the same temperature group but hash differently.  That fail-closed
settled-truth ingest for every station (46 stations/day down to 4), while
exactly one scope in the archive actually disagreed on temperature.
"""

import inspect

from bayes_basket_target import settlement_truth


SOURCE = inspect.getsource(settlement_truth)


def _dedupe_block() -> str:
    start = SOURCE.index("previous = hourly_by_valid_at.get(")
    return SOURCE[start : start + 1800]


def test_agreement_is_decided_on_temperature_alone() -> None:
    block = _dedupe_block()
    condition_start = block.index("if (")
    condition = block[condition_start : block.index("raise ContractError")]

    assert "reported_temperature_c" in condition
    # Raw bytes and report type differ for benign reasons and must not decide
    # whether two reports state the same fact.
    assert "raw_sha256" not in condition
    assert "report_type" not in condition


def test_temperature_disagreement_still_fails_closed() -> None:
    block = _dedupe_block()

    assert "settlement_truth_hourly_revision_ambiguous" in block
    # The raise must remain reachable, i.e. it is not inside the equality arm.
    assert block.index("continue") < block.index("raise ContractError")


def test_truth_is_the_max_reported_temperature() -> None:
    # This is what makes the merge safe: the kept report cannot alter the max
    # when both report the same temperature.
    assert 'official_tmax_c = max(' in SOURCE
    assert '"reported_temperature_c"' in SOURCE


def test_raw_sha_still_recorded_for_audit() -> None:
    # Relaxing the equality must not drop the raw hash from the evidence.
    assert '"raw_sha256": raw_sha' in SOURCE
    assert '"observations": hourly' in SOURCE


def test_raw_sha_is_still_validated() -> None:
    # Dropping it from the equality does not mean accepting a malformed hash.
    assert "settlement_truth_raw_sha256_invalid" in SOURCE
