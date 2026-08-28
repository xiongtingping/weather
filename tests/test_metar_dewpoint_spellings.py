"""A dewpoint the parser could not spell took one station off the air.

OEJN sat at trajectory generation 1 since 2026-08-17 while the fleet median
reached 73, and the receipt on file blamed missing hourly-curve folds.  That
receipt was ten days stale.  What actually stopped it was one report --

    METAR OEJN 180100Z 32008KT CAVOK 32/// Q1003 NOSIG

-- whose dewpoint uses the ICAO slash form.  The parser accepted ``XX`` for
"not available" and not ``//``, and it raises rather than skipping, so a
single report out of 866 took the station's whole training packet with it.
No other station in the fleet has produced one, which is exactly why it read
as a station-specific evidence gap instead of a parser gap.
"""

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.metar_path_state import parse_metar_physical_state


THE_REPORT = "METAR OEJN 180100Z 32008KT CAVOK 32/// Q1003 NOSIG"


def _state(raw, expected):
    return parse_metar_physical_state(raw, expected_temperature_c=expected)


def test_the_report_that_stopped_oejn_parses():
    state = _state(THE_REPORT, 32.0).to_dict()
    assert state["dewpoint_c"] is None
    # Absent, not invented: the state is honestly incomplete.
    assert state["complete"] is False


@pytest.mark.parametrize(
    "raw,expected_dewpoint",
    [
        ("METAR X 180100Z 32008KT CAVOK 32/24 Q1003", 24.0),
        ("METAR X 180100Z 32008KT CAVOK 32/M04 Q1003", -4.0),
        ("METAR X 180100Z 32008KT CAVOK 32/XX Q1003", None),
        ("METAR X 180100Z 32008KT CAVOK 32/// Q1003", None),
    ],
)
def test_every_dewpoint_spelling_the_wire_uses(raw, expected_dewpoint):
    assert _state(raw, 32.0).to_dict()["dewpoint_c"] == expected_dewpoint


def test_a_genuinely_absent_group_still_raises():
    """Widening the spelling must not widen it into accepting nothing.

    A report with no temperature group at all cannot be placed on the path,
    and silently treating that as "temperature missing" would put a fold in
    the training set with no temperature in it.
    """

    with pytest.raises(ContractError) as excinfo:
        _state("METAR X 180100Z 32008KT CAVOK Q1003 NOSIG", 32.0)
    assert "temperature_dewpoint_missing" in str(excinfo.value)


def test_the_temperature_half_is_still_checked_against_the_record():
    """The slash form loosens the dewpoint, never the temperature."""

    with pytest.raises(ContractError) as excinfo:
        _state(THE_REPORT, 18.0)
    assert "temperature_mismatch" in str(excinfo.value)


def test_slashes_do_not_leak_into_the_temperature_field():
    with pytest.raises(ContractError):
        _state("METAR X 180100Z 32008KT CAVOK ///24 Q1003", 32.0)
