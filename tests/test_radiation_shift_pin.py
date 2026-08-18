"""The radiation coefficient is served at the value its holdout was scored at.

The shift itself was gated from the start, but its size was a bare module
constant: nothing recorded which value a given artifact had been scored
under, so raising it would have moved the served arithmetic for every
artifact already admitted at the old one.  That is the same serve-against-
train split as the affine form, the spread cap and the health check's RMSE --
one contract living in two places, one of them updated.
"""

from __future__ import annotations

import unittest

from bayes_basket_target.cross_consensus import (
    LEGACY_RADIATION_SHIFT_SIGMA,
    RADIATION_SHIFT_SIGMA,
    forecast_radiation_shift_sigma,
    pinned_radiation_shift_sigma,
)

HISTORY = [float(value) for value in range(10, 40)]


class PinTest(unittest.TestCase):
    def test_a_pin_is_read_even_when_it_differs_from_the_constant(
        self,
    ) -> None:
        """The two are equal today and the pin exists so they need not be.

        A test asserting they differ would fail the day a coefficient is
        rolled back, which is the moment the pin matters most: the artifacts
        built at the other value are still out there being served.
        """

        self.assertEqual(
            pinned_radiation_shift_sigma("a::radiation_shift_sigma=0.35::b"),
            0.35,
        )
        self.assertNotEqual(0.35, RADIATION_SHIFT_SIGMA)

    def test_a_pinned_artifact_is_served_its_own_coefficient(self) -> None:
        method = (
            "train_rmse_inverse_family_capped_v1::PRODUCTION_NO_LIVE"
            "::synoptic_cooling_v1::radiation_shift_sigma=0.35"
        )
        self.assertEqual(pinned_radiation_shift_sigma(method), 0.35)

    def test_an_artifact_from_before_the_pin_keeps_the_old_value(self) -> None:
        """Its holdout was scored at 0.15; serving it 0.35 would be math its
        gate never saw."""

        method = (
            "train_rmse_inverse_family_capped_v1::PRODUCTION_NO_LIVE"
            "::synoptic_cooling_v1"
        )
        self.assertEqual(
            pinned_radiation_shift_sigma(method),
            LEGACY_RADIATION_SHIFT_SIGMA,
        )

    def test_an_unreadable_pin_falls_back_rather_than_raising(self) -> None:
        method = "a::radiation_shift_sigma=not-a-number::b"
        self.assertEqual(
            pinned_radiation_shift_sigma(method),
            LEGACY_RADIATION_SHIFT_SIGMA,
        )

    def test_the_coefficient_scales_the_shift_it_is_given(self) -> None:
        small, _receipt = forecast_radiation_shift_sigma(
            radiation_sum=45.0,
            station_history=HISTORY,
            shift_sigma_per_unit=0.15,
        )
        large, receipt = forecast_radiation_shift_sigma(
            radiation_sum=45.0,
            station_history=HISTORY,
            shift_sigma_per_unit=0.35,
        )
        assert small is not None and large is not None
        self.assertAlmostEqual(large / small, 0.35 / 0.15, places=9)
        self.assertEqual(receipt["shift_sigma_per_unit"], 0.35)

    def test_the_gate_pins_the_constant_it_scores_with(self) -> None:
        """A default on either side lets the two drift silently."""

        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[1]
            / "bayes_basket_target" / "native_ensemble_oos.py"
        ).read_text()
        self.assertIn("shift_sigma_per_unit=RADIATION_SHIFT_SIGMA", source)
        self.assertIn("radiation_shift_sigma=", source)

    def test_the_server_reads_the_pin_and_not_the_constant(self) -> None:
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[1]
            / "bayes_basket_target" / "native_ensemble_p0.py"
        ).read_text()
        self.assertIn("pinned_radiation_shift_sigma", source)


if __name__ == "__main__":
    unittest.main()
