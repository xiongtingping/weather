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
    assert "claim_factor *= center_window_claim_factor(raw_win)" in after
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
