"""The radiation coefficient is served at the value its holdout was scored at.

The shift itself was gated from the start, but its size was a bare module
constant: nothing recorded which value a given artifact had been scored
under, so raising it would have moved the served arithmetic for every
artifact already admitted at the old one.  That is the same serve-against-
train split as the affine form, the spread cap and the health check's RMSE --
one contract living in two places, one of them updated.

Charter 222 split the one coefficient into a warm side and a cold side; the
pin carries both, and a pin written before it (one coefficient) still reads
back as that coefficient on both sides.
"""

from __future__ import annotations

import unittest

from bayes_basket_target.cross_consensus import (
    LEGACY_RADIATION_SHIFT_SIGMA,
    RADIATION_SHIFT_SIGMA_COLD,
    RADIATION_SHIFT_SIGMA_WARM,
    forecast_radiation_shift_sigma,
    pinned_radiation_shift_sigmas,
    radiation_shift_sigmas_for_lead,
)

HISTORY = [float(value) for value in range(10, 40)]
FLEET = (RADIATION_SHIFT_SIGMA_WARM, RADIATION_SHIFT_SIGMA_COLD)


class PerLeadCoefficientTest(unittest.TestCase):
    """The coefficient was once a property of the lead, not of the fleet.

    Pooled, raising it was worth +0.008 bits and made 63% of scopes worse.
    Split by lead the gain concentrated at h12_24, so the raise was tried
    there alone -- and retired by charter 160.
    """

    def test_charter_160_retired_the_h12_24_raise(self) -> None:
        """0.35 at h12_24 replayed 5% worse than 0.15 on its own decisions
        (RPS -0.032 [-0.051, -0.013]); every lead is fitted at one pair."""

        for lead in ("h00_12", "h12_24", "h24_48", "h48_plus"):
            self.assertEqual(radiation_shift_sigmas_for_lead(lead), FLEET)

    def test_charter_222_fits_new_artifacts_at_the_two_sided_pair(self) -> None:
        """Charter 173 took the one coefficient to 0.07 (0.15 scored worse
        than no shift); charter 222 split it: warm 0.124 [0.099, 0.148], cold
        0.045 [0.024, 0.069] on charter 210's objective (user ruling
        「把辐射系数拆成升温、降温两侧」)."""

        self.assertEqual(FLEET, (0.18, 0.06))
        self.assertGreater(RADIATION_SHIFT_SIGMA_WARM, RADIATION_SHIFT_SIGMA_COLD)
        self.assertEqual(LEGACY_RADIATION_SHIFT_SIGMA, 0.15)

    def test_an_unknown_lead_gets_the_unearned_default(self) -> None:
        """A lead nobody has measured is not a lead to experiment on."""

        for lead in (None, "", "d2", "h96_plus"):
            self.assertEqual(radiation_shift_sigmas_for_lead(lead), FLEET)


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
            pinned_radiation_shift_sigmas(
                "a::radiation_shift_sigma_warm=0.35::radiation_shift_sigma_cold=0.2::b"
            ),
            (0.35, 0.2),
        )
        self.assertNotEqual((0.35, 0.2), FLEET)

    def test_a_one_coefficient_pin_reads_as_both_sides(self) -> None:
        """Every artifact minted before charter 222 pins one coefficient,
        which its holdout applied on both sides."""

        method = (
            "train_rmse_inverse_family_capped_v1::PRODUCTION_NO_LIVE"
            "::synoptic_cooling_v1::radiation_shift_sigma=0.35"
        )
        self.assertEqual(pinned_radiation_shift_sigmas(method), (0.35, 0.35))

    def test_a_half_pair_reads_as_the_one_coefficient_or_the_legacy(self) -> None:
        """A pair missing one side is not a pair: without a single pin beside
        it the artifact reads as unpinned, never as a guessed other side."""

        self.assertEqual(
            pinned_radiation_shift_sigmas("a::radiation_shift_sigma_warm=0.12::b"),
            (LEGACY_RADIATION_SHIFT_SIGMA, LEGACY_RADIATION_SHIFT_SIGMA),
        )
        self.assertEqual(
            pinned_radiation_shift_sigmas(
                "a::radiation_shift_sigma=0.07::radiation_shift_sigma_cold=0.04"
            ),
            (0.07, 0.07),
        )

    def test_an_artifact_from_before_the_pin_keeps_the_old_value(self) -> None:
        """Its holdout was scored at 0.15; serving it 0.35 would be math its
        gate never saw."""

        method = (
            "train_rmse_inverse_family_capped_v1::PRODUCTION_NO_LIVE"
            "::synoptic_cooling_v1"
        )
        self.assertEqual(
            pinned_radiation_shift_sigmas(method),
            (LEGACY_RADIATION_SHIFT_SIGMA, LEGACY_RADIATION_SHIFT_SIGMA),
        )

    def test_an_unreadable_pin_falls_back_rather_than_raising(self) -> None:
        for method in (
            "a::radiation_shift_sigma=not-a-number::b",
            "a::radiation_shift_sigma_warm=x::radiation_shift_sigma_cold=0.04::b",
        ):
            self.assertEqual(
                pinned_radiation_shift_sigmas(method),
                (LEGACY_RADIATION_SHIFT_SIGMA, LEGACY_RADIATION_SHIFT_SIGMA),
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

    def test_the_gate_pins_the_value_it_scores_with(self) -> None:
        """One pair resolved once: scored, pinned, read back.

        A default on any of the three lets them drift silently, which is the
        whole failure this pin exists to prevent.
        """

        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[1]
            / "bayes_basket_target" / "native_ensemble_oos.py"
        ).read_text()
        self.assertIn(
            "scope_radiation_sigmas = radiation_shift_sigmas_for_lead(",
            source,
        )
        self.assertIn(
            "radiation_shift_sigmas=scope_radiation_sigmas", source
        )
        self.assertIn(
            "candidate['radiation_shift_sigmas'][0]", source
        )
        self.assertIn(
            "candidate['radiation_shift_sigmas'][1]", source
        )

    def test_every_artifact_is_served_the_fleet_coefficient(self) -> None:
        """Charter 210 (user ruling 「改P0辐射系数」): the pin is recorded, not
        served.  Certificate renewal copies an artifact's method whole, so a
        pin-served 0.15 would have outlived the rotation on every renewed
        scope.  Charter 222 kept the rule and split the value."""

        from bayes_basket_target.cross_consensus import (
            RADIATION_CLEARNESS_METHOD_TAG,
            served_radiation_shift_sigmas,
        )

        for method in (
            "x::synoptic_cooling_shift_v1::radiation_shift_sigma=0.35::y",
            "x::radiation_shift_sigma=0.15::" + RADIATION_CLEARNESS_METHOD_TAG,
            "x::synoptic_cooling_shift_v1::radiation_shift_sigma=0.2::"
            + RADIATION_CLEARNESS_METHOD_TAG + "::y",
            "x::radiation_shift_sigma=0.07::" + RADIATION_CLEARNESS_METHOD_TAG,
            "x::radiation_shift_sigma_warm=0.3::radiation_shift_sigma_cold=0.3",
            "",
        ):
            self.assertEqual(served_radiation_shift_sigmas(method), FLEET)
        # The pin itself still reads back, for the receipt and the shadow arm.
        self.assertEqual(
            pinned_radiation_shift_sigmas(
                "x::radiation_shift_sigma=0.15::" + RADIATION_CLEARNESS_METHOD_TAG
            ),
            (0.15, 0.15),
        )


if __name__ == "__main__":
    unittest.main()
