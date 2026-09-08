"""The centre-holding claim multiplier is a research arm on both sides.

Re-measured 2026-09-05 on served claims, centre-holding windows realise
1.10-1.30 of what they claim; the arm multiplies such a window's claim by a
per-band factor so the positive-EV gate, the ranking, the partition and
charter 68's agreement all read the same lifted belief.  Off by default,
production's constant, hashed into the ledger fingerprint when on.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


def test_charter_81_is_the_default_and_off_is_the_arm(monkeypatch) -> None:
    import bayes_basket_target.basket_kernel.delta_v2 as K
    import bayes_basket_target.delta_v2_runtime as R

    bands = ((0.35, 1.0), (0.55, 1.21), (1.01, 1.08))
    # One implementation, in the kernel; the runtime re-exports it.
    assert R.CENTER_WINDOW_CLAIM_BANDS is K.CENTER_WINDOW_CLAIM_BANDS
    assert R.center_window_claim_factor is K.center_window_claim_factor
    assert K.CENTER_WINDOW_CLAIM_BANDS == bands
    assert R._parse_claim_bands("") == bands
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
    at = source.index("claim_factor = off_center_claim_factor(raw_win, gap_buckets=center_gap)")
    after = source[at:at + 400]
    assert "if holds_center:" in after
    # Charter 81's factor is computed on its own line so the sizing lane can
    # divide it back out (2026-09-06 split); the multiply is unconditional
    # only inside the holds_center branch.
    assert "center_lift = center_window_claim_factor(raw_win)" in after
    assert "claim_factor *= center_lift" in after
    assert '"center_window_claim_bands"' in source


def test_the_ledger_reads_productions_constant_and_hashes_it() -> None:
    import bayes_basket_target.delta_v2_runtime as R
    import paper_trading_ledger as L  # noqa: PLC0415

    assert L.CENTER_WINDOW_CLAIM_BANDS is R.CENTER_WINDOW_CLAIM_BANDS
    ledger = (ROOT / "scripts" / "paper_trading_ledger.py").read_text(encoding="utf-8")
    # Hashed unconditionally, like the other shipped seat rules.
    assert '"center_window_claim": [' in ledger
    assert '"含中心窗抬价" if CENTER_WINDOW_CLAIM_BANDS else None' in ledger


def test_the_lambda_store_fits_agreement_on_the_lifted_claim() -> None:
    """Charter 81 (二): the store and the solver read one basis for a."""
    import bayes_basket_target.effective_edge_store as S

    source = inspect.getsource(S.fit_lambda_table)
    assert "claim = raw_claim * center_window_claim_factor(raw_claim)" in source
    assert "slot = tally[_band_index(price / claim, incumbent)]" in source


def test_the_admission_sizing_split_is_off_by_default_and_touches_only_the_stake() -> None:
    """Charter 81's lift is an admission knob; the curve may be spent on the
    stake alone (user ruling, 2026-09-06: 拆).  Off by default, which is
    charter 68's one belief and the shipped rule.
    """
    import bayes_basket_target.basket_kernel.delta_v2 as K
    import bayes_basket_target.delta_v2_runtime as R
    import paper_trading_ledger as L  # noqa: PLC0415

    assert K.SIZING_CLAIM_BANDS == ()
    assert R.SIZING_CLAIM_BANDS is K.SIZING_CLAIM_BANDS
    assert L.SIZING_CLAIM_BANDS is K.SIZING_CLAIM_BANDS
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
    # Default receipts and the ledger fingerprint stay byte-identical.
    assert '"sizing_claim_bands"' in runtime and 'if SIZING_CLAIM_BANDS else {}' in runtime
    ledger = (ROOT / "scripts" / "paper_trading_ledger.py").read_text(encoding="utf-8")
    assert '{"sizing_claim": [' in ledger
    assert '"定注另读校准曲线" if SIZING_CLAIM_BANDS else None' in ledger
