"""Sub-degree official temperature from the METAR ``Tsnnnsnnn`` remark group.

A METAR states its temperature twice.  The body group ``TT/DD`` is whole
degrees Celsius, and the United States ASOS stations repeat it in the remark
section as ``Tsnnnsnnn`` -- a leading sign digit (``0`` positive, ``1``
negative) and three digits of tenths, for temperature and dew point.  The two
state the same measurement at different resolutions, so the remark group is
never a second opinion to reconcile: it is the same reading with the tenth
that the body group rounded away.

Whole degrees are enough for a market that settles in 1C buckets and are not
enough for one that settles in 2F buckets.  A 2F bucket is 1.111C, so a whole
Celsius reading -- true value anywhere in a 1.0C interval, 1.8F wide -- lands
inside a single 2F bucket only when it falls in the middle 0.2F of it.  Eight
integer Celsius values in ten therefore admit two different winning buckets,
and nothing downstream of the rounding can tell which one.  Tenths close it:
a 0.1C reading is 0.18F wide and settles the bucket outright.

This module is pure text: no clock, no database, no network.
"""

from __future__ import annotations

import re

from .contracts import ContractError


SCHEMA = "bayes_basket_target_metar_remark_temperature_precision_v1"

# ``Tsnnnsnnn`` in the remark section only.  The body never carries this token
# -- measured over every archived AWC report, 7826 of 7959 carry it in the
# remark section and none in the body -- so anchoring the search to the
# remarks costs no coverage and removes a whole class of false positive.
REMARK_TEMPERATURE_RE = re.compile(
    r"(?<![A-Z0-9])T(?P<temp>[01]\d{3})(?P<dew>[01]\d{3})(?![A-Z0-9])"
)
REMARK_SEPARATOR = " RMK "

WHOLE_DEGREE_RESOLUTION_C = 1.0
REMARK_RESOLUTION_C = 0.1

# The remark group restates the body group, so it must round back to it.  The
# body group is the reading rounded to the nearest whole degree, which puts
# the two at most half a degree apart; a wider gap means the two halves of one
# report disagree about the reading and the finer one has not earned the
# right to be believed.
MAX_REMARK_BODY_DISAGREEMENT_C = 0.5
_TOLERANCE = 1e-9


def normalize_report_text(raw_text: str) -> str:
    """Collapse a report to the single-line form the parsers agree on."""

    return " ".join(
        str(raw_text or "").replace("\r", " ").replace("\n", " ").split()
    ).rstrip("=")


def decode_remark_tenths_c(group: str) -> float:
    """Decode one signed ``snnn`` remark field into Celsius."""

    text = str(group or "").strip()
    if len(text) != 4 or text[0] not in "01" or not text.isdigit():
        raise ContractError("metar_remark_temperature_group_invalid")
    return (-1.0 if text[0] == "1" else 1.0) * int(text[1:]) / 10.0


def remark_temperature_c(raw_text: str) -> float | None:
    """Return the remark-group temperature in Celsius, or None if absent."""

    normalized = normalize_report_text(raw_text)
    parts = normalized.split(REMARK_SEPARATOR, 1)
    if len(parts) != 2:
        return None
    match = REMARK_TEMPERATURE_RE.search(parts[1])
    if match is None:
        return None
    return decode_remark_tenths_c(match.group("temp"))


def official_temperature_c(
    raw_text: str,
    *,
    whole_degree_c: float,
) -> tuple[float, float]:
    """Return ``(temperature_c, resolution_c)`` at the best stated resolution.

    Falls back to the whole-degree body group when the remark group is absent
    or contradicts it.  Falling back rather than raising is deliberate: this
    sits on a capture path that must not stop, and the fallback is exactly the
    value the caller would have used anyway.  The returned resolution is what
    makes the difference visible to the evidence payload, so a station that
    quietly stops publishing the remark group shows up as a resolution
    change instead of as silence.
    """

    whole = float(whole_degree_c)
    precise = remark_temperature_c(raw_text)
    if precise is None:
        return whole, WHOLE_DEGREE_RESOLUTION_C
    if abs(precise - whole) > MAX_REMARK_BODY_DISAGREEMENT_C + _TOLERANCE:
        return whole, WHOLE_DEGREE_RESOLUTION_C
    return precise, REMARK_RESOLUTION_C


__all__ = [
    "MAX_REMARK_BODY_DISAGREEMENT_C",
    "REMARK_RESOLUTION_C",
    "REMARK_SEPARATOR",
    "REMARK_TEMPERATURE_RE",
    "SCHEMA",
    "WHOLE_DEGREE_RESOLUTION_C",
    "decode_remark_tenths_c",
    "normalize_report_text",
    "official_temperature_c",
    "remark_temperature_c",
]
