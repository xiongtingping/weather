"""The board reads what production computed; it does not compute it again.

User rulings 2026-10-02 「所有前端都改为只读」 and 「只读是读后端算出来的」.
The board had grown six recomputations of production's own logic: the
single-system fit (~170k refits a build), the L4 forward skill and
realisation re-judgements, the centre/width layer re-applied to every P0
envelope, the fusion weights re-solved per seat, charter 131's seat rule run
on every served cell, and the P1 rise logit rebuilt from the receipt.  Each
now reads what production stored.  This pins it: none of production's
fitting, compiling, re-judging, re-weighting or rule functions is called
from the board's sources.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOARD_SOURCES = (
    ROOT / "scripts" / "calibration_dashboard.py",
)
FORBIDDEN_CALLS = (
    "_fit_parameters", "_load_cases", "_logloss_refit",
    "compile_native_ensemble_p0",
    "forward_skill", "realisation",
    "apply_center_width_calibration",
    "_seat_weights", "stable_pool_weights", "_minimum_variance_weights",
    "seat_shape_windows_v1", "_alternate_ladder_center", "_skew_extension_sign",
    "phase_rise_probability", "phase_clock_intercept", "phase_rise_terms",
)


def test_the_board_calls_none_of_productions_compute_functions() -> None:
    found = []
    for path in BOARD_SOURCES:
        source = path.read_text(encoding="utf-8")
        for name in FORBIDDEN_CALLS:
            for match in re.finditer(r"\b%s\s*\(" % re.escape(name), source):
                line = source.count("\n", 0, match.start()) + 1
                found.append(f"{path.name}:{line} {name}")
    assert found == []


def test_every_seated_system_keeps_its_own_short_name() -> None:
    """2026-10-02: the consensus bridge cut system ids at the first "_", so
    IFS and AIFS both became "ECMWF" and GEFS and AIGEFS both "NOAA" -- six
    seated systems shown as four columns.  One short name per system, from
    the board's single table, and no id cut at its first underscore."""

    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    import calibration_dashboard as C  # noqa: PLC0415
    from bayes_basket_target.native_ensemble_oos import (  # noqa: PLC0415
        OFFICIAL_ARCHIVE_SCOPE_CONTRACTS,
    )

    labels = [C.MEMBER_FAMILY_LABELS.get(s, s) for s in OFFICIAL_ARCHIVE_SCOPE_CONTRACTS]
    assert len(set(labels)) == len(labels) >= 6
    for path in BOARD_SOURCES:
        assert 'split("_")[0]' not in path.read_text(encoding="utf-8"), path.name
