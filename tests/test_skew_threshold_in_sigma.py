"""The skew gate is in units of the served sigma (user ruling, 2026-09-11).

Half a bucket was an absolute gate.  As serving narrowed from 1.7 to 0.65
bucket steps the mean could no longer leave the mode's bucket and nine
days in ten read SYMMETRIC whatever their shape.  The same offset must
read as a lean on a narrow day and as nothing on a wide one.
"""
from __future__ import annotations

import importlib
import math
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

from bayes_basket_target.basket_kernel import support as kernel_support
from bayes_basket_target.basket_kernel.support import (
    MEAN_MODE_SKEW_THRESHOLD_SIGMA,
    mean_mode_skew_reading_v1,
    mean_mode_skew_sign_v1,
)

ORDINALS = {f"{v}": i for i, v in enumerate(range(20, 33))}


def _gaussian(centre: float, sd: float) -> dict[str, float]:
    p = {k: math.exp(-0.5 * ((float(k) - centre) / sd) ** 2) for k in ORDINALS}
    total = sum(p.values())
    return {k: v / total for k, v in p.items()}


def _reading(centre: float, sd: float) -> dict:
    return mean_mode_skew_reading_v1(_gaussian(centre, sd), ORDINALS)


def test_the_same_offset_leans_on_a_narrow_day_and_not_on_a_wide_one() -> None:
    # Mean 0.3 buckets above the mode's centre.
    narrow = _reading(26.3, 0.65)
    wide = _reading(26.3, 2.0)
    assert narrow["mode_ordinal"] == wide["mode_ordinal"] == ORDINALS["26"]
    assert narrow["gap_buckets"] == pytest.approx(0.3, abs=0.05)
    assert wide["gap_buckets"] == pytest.approx(0.3, abs=0.05)
    # 0.3 buckets is 0.46 sigma on the narrow day (a lean) and 0.15 sigma
    # on the wide day (nothing).
    assert narrow["sign"] == 1 and narrow["state"] == "RIGHT_SKEWED"
    assert wide["sign"] == 0 and wide["state"] == "SYMMETRIC"
    # Under the old absolute gate both read SYMMETRIC (0.3 < 0.5).
    assert narrow["threshold_basis"] == "sigma"
    assert narrow["threshold_sigma"] == MEAN_MODE_SKEW_THRESHOLD_SIGMA == 0.35


def test_the_reading_carries_the_gate_it_was_judged_by_in_both_units() -> None:
    reading = _reading(26.3, 0.65)
    assert reading["threshold_buckets"] == pytest.approx(
        0.35 * reading["sigma_buckets"], abs=1e-6
    )
    # ... and the sign is exactly that comparison.
    assert (abs(reading["gap_buckets"]) >= reading["threshold_buckets"]) == (
        reading["sign"] != 0
    )
    empty = mean_mode_skew_reading_v1({}, ORDINALS)
    assert empty["threshold_sigma"] == 0.35
    assert empty["threshold_buckets"] is None
    assert empty["threshold_basis"] == "sigma"
    assert not empty["readable"]


def test_a_wide_day_that_really_leans_still_reads_the_lean() -> None:
    # A warm tail on a 1.1-sigma day: mean 0.75 buckets above the mode,
    # 0.66 sigma, a lean under either gate.
    warm = {"25": 0.10, "26": 0.40, "27": 0.25, "28": 0.15, "29": 0.10}
    reading = mean_mode_skew_reading_v1(warm, ORDINALS)
    assert reading["mode_ordinal"] == ORDINALS["26"]
    assert reading["gap_buckets"] == pytest.approx(0.75)
    assert reading["sigma_buckets"] == pytest.approx(1.135, abs=0.01)
    assert reading["sign"] == 1
    # Mirror.
    cold = {"29": 0.10, "28": 0.40, "27": 0.25, "26": 0.15, "25": 0.10}
    assert mean_mode_skew_sign_v1(cold, ORDINALS) == -1
    # Zero sigma (one bucket) is symmetric, not a division by zero.
    assert mean_mode_skew_sign_v1({"26": 1.0}, ORDINALS) == 0


def test_the_threshold_is_a_kwarg_in_sigma_and_the_old_unit_is_refused() -> None:
    assert mean_mode_skew_sign_v1(
        _gaussian(26.3, 0.65), ORDINALS, threshold_sigma=0.6
    ) == 0
    with pytest.raises(TypeError):
        mean_mode_skew_reading_v1(
            _gaussian(26.3, 0.65), ORDINALS, threshold_buckets=0.5,  # type: ignore[call-arg]
        )


def test_the_runtime_refuses_the_retired_bucket_unit_variable() -> None:
    from bayes_basket_target import delta_v2_runtime

    with mock.patch.dict(os.environ, {"DELTA_V2_SKEW_GAP_THRESHOLD": "0.5"}):
        with pytest.raises(RuntimeError, match="SIGMA"):
            importlib.reload(delta_v2_runtime)
    importlib.reload(delta_v2_runtime)
    assert delta_v2_runtime.SKEW_GAP_THRESHOLD_SIGMA == 0.35


def test_production_and_ledger_read_one_gate() -> None:
    from bayes_basket_target import delta_v2_runtime

    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    import paper_trading_ledger as ledger  # noqa: PLC0415

    assert ledger.SKEW_GAP_THRESHOLD_SIGMA == delta_v2_runtime.SKEW_GAP_THRESHOLD_SIGMA == 0.35
    assert delta_v2_runtime._skew_extension_sign(
        _gaussian(26.3, 0.65), ORDINALS
    ) == 1
    source = (root / "scripts" / "paper_trading_ledger.py").read_text(encoding="utf-8")
    assert '"skew_gap_threshold_basis": "sigma"' in source
