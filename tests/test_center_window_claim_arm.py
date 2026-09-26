"""The centre-holding claim multiplier is a research arm on both sides.

Re-measured 2026-09-05 on served claims, centre-holding windows realise
1.10-1.30 of what they claim; the arm multiplies such a window's claim by a
per-band factor so the positive-EV gate, the ranking, the partition and
charter 68's agreement all read the same lifted belief.  Retired
2026-09-12: off by default on both sides (the raw claim is what the gate
and the solver read), the old table reachable only as the env arm, and the
ledger fingerprint moves with whatever is set.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


def test_charter_81_is_retired_off_by_default_and_the_table_is_the_arm(monkeypatch) -> None:
    import bayes_basket_target.basket_kernel.delta_v2 as K
    import bayes_basket_target.delta_v2_runtime as R

    bands = ((0.35, 1.0), (0.55, 1.21), (1.01, 1.08))
    # One implementation, in the kernel; the runtime re-exports it.
    assert R.CENTER_WINDOW_CLAIM_BANDS is K.CENTER_WINDOW_CLAIM_BANDS
    assert R.center_window_claim_factor is K.center_window_claim_factor
    assert K.CENTER_WINDOW_CLAIM_BANDS == ()
    assert K.CENTER_WINDOW_CLAIM_DEFAULT == "[]"
    assert R._parse_claim_bands("") == ()
    assert R._parse_claim_bands(K.CENTER_WINDOW_CLAIM_RETIRED_TABLE) == bands
    assert R.center_window_claim_factor(0.45) == 1.0  # retired: identity by default
    assert R._parse_claim_bands("off") == ()
    assert R._parse_claim_bands("[]") == ()
    assert R._parse_claim_bands("[[0.35, 1.0], [0.55, 1.21], [1.01, 1.08]]") == bands
    monkeypatch.setattr(K, "CENTER_WINDOW_CLAIM_BANDS", ())
    assert R.center_window_claim_factor(0.45) == 1.0
    monkeypatch.setattr(K, "CENTER_WINDOW_CLAIM_BANDS", bands)
    assert R.center_window_claim_factor(0.30) == 1.0
    assert R.center_window_claim_factor(0.45) == pytest.approx(1.21)
    assert R.center_window_claim_factor(0.70) == pytest.approx(1.08)
    assert R.center_window_claim_factor(1.5) == 1.0
    with pytest.raises(RuntimeError):
        R._parse_claim_bands("[[0.55, 1.21], [0.35, 1.0]]")
    with pytest.raises(RuntimeError):
        R._parse_claim_bands("[[0.55, 0.0]]")


def test_the_arm_multiplies_only_centre_holding_windows() -> None:
    import bayes_basket_target.delta_v2_runtime as R

    source = inspect.getsource(R._support_windows)
    at = source.index("claim_factor = window_claim_factor(")
    after = source[at:at + 700]
    assert "if holds_center:" in after
    # Charter 81's factor is still computed on its own line, so the sizing
    # lane can divide it back out (2026-09-06 split).
    assert "center_lift = center_window_claim_factor(raw_win)" in after
    assert '"center_window_claim_bands"' in source

    # The multiply itself moved into the kernel on 2026-09-08, so the
    # enumeration and the band-axis replay compute one number from one
    # implementation.  It is still conditional on holding the centre -- the
    # gap is what expresses that -- and the deflation is still applied first.
    import inspect as _inspect

    import bayes_basket_target.basket_kernel.delta_v2 as K

    helper = _inspect.getsource(K.window_claim_factor)
    assert "gap_buckets=center_gap, bands=off_centre_bands" in helper
    assert "if float(center_gap) <= 0.0:" in helper
    assert "factor *= center_window_claim_factor(float(raw_win))" in helper
    assert K.window_claim_factor(0.45, center_gap=0) == (
        K.off_center_claim_factor(0.45, gap_buckets=0)
        * K.center_window_claim_factor(0.45)
    )
    assert K.window_claim_factor(0.45, center_gap=2) == K.off_center_claim_factor(
        0.45, gap_buckets=2
    )


# Charter 230 retired the lambda / second-belief arm this test pinned.




def test_the_admission_sizing_split_is_off_by_default_and_touches_only_the_stake() -> None:
    """Charter 81's lift is an admission knob; the curve may be spent on the
    stake alone (user ruling, 2026-09-06: 拆).  Off by default, which is
    charter 68's one belief and the shipped rule.
    """
    import bayes_basket_target.basket_kernel.delta_v2 as K
    import bayes_basket_target.delta_v2_runtime as R

    assert K.SIZING_CLAIM_BANDS == ()
    assert R.SIZING_CLAIM_BANDS is K.SIZING_CLAIM_BANDS
    assert K.sizing_claim_factor(0.45) == 1.0
    assert K._parse_claim_bands("", default="[]") == ()
    bands = K._parse_claim_bands('[[0.35,1.35],[0.55,1.19],[1.01,1.05]]')
    assert bands == ((0.35, 1.35), (0.55, 1.19), (1.01, 1.05))

    runtime = (ROOT / "bayes_basket_target" / "delta_v2_runtime.py").read_text(
        encoding="utf-8"
    )
    # The split divides charter 81's own component back out and keeps the
    # off-centre deflation and lambda(a) exactly as admission computed them.
    assert 'seated_lift = float(row.get("center_window_claim_factor") or 1.0)' in runtime
    assert "claim_factor / seated_lift" in runtime
    assert "sizing_probabilities = None" in runtime
    # It reaches the stake formula and nothing else: the ranking, the grid
    # and every gate keep reading probabilities_by_bucket.
    at = runtime.index("kelly_candidate = _whole_basket_kelly_candidate(")
    assert "sizing_probabilities_by_bucket" in runtime[at:at + 900]
    for other in (
        "_continuous_optimum_candidates(",
        "_centre_anchored_candidates(",
    ):
        start = runtime.index(other)
        assert "sizing_probabilities_by_bucket" not in runtime[start:start + 900], other
    # Default receipts stay byte-identical.
    assert '"sizing_claim_bands"' in runtime and 'if SIZING_CLAIM_BANDS else {}' in runtime


def test_the_off_centre_deflation_is_retired_and_identity_by_default(monkeypatch) -> None:
    """Second cut of the claim-chain clean-up (2026-09-12): the off-centre
    deflation serves 1.0 on every path unless the arm is switched on, the
    ledger hashes the mode unconditionally, and the refit lane is a no-op."""
    import bayes_basket_target.basket_kernel.delta_v2 as K

    assert K.OFF_CENTRE_CLAIM_MODE == "off"
    assert K.off_center_claim_factor(0.15, gap_buckets=1) == 1.0
    assert K.off_center_claim_factor(0.15, gap_buckets=3, bands=((0.2, 0.5), (1.01, 1.0))) == 1.0
    assert K.window_claim_factor(0.15, center_gap=2) == 1.0
    monkeypatch.setattr(K, "OFF_CENTRE_CLAIM_MODE", "on")
    assert K.off_center_claim_factor(0.15, gap_buckets=1) == pytest.approx(0.88)
    cli = (ROOT / "scripts" / "refit_off_centre_claim.py").read_text(encoding="utf-8")
    assert 'if OFF_CENTRE_CLAIM_MODE == "off":' in cli
