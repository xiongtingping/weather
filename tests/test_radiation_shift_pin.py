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
    radiation_shift_sigma_for_lead,
)

HISTORY = [float(value) for value in range(10, 40)]


class PerLeadCoefficientTest(unittest.TestCase):
    """The coefficient is a property of the lead, not of the fleet.

    Pooled, raising it is worth +0.008 bits and makes 63% of scopes worse.
    Split by lead the gain concentrates: h12_24 wins 75% of its scopes at
    +0.088 while both other leads are net negative, so a fleet constant was
    paying the two that lose to help the one that wins.
    """

    def test_charter_160_retired_the_h12_24_raise(self) -> None:
        """0.35 at h12_24 replayed 5% worse than 0.15 on its own decisions
        (RPS -0.032 [-0.051, -0.013]); every lead is fitted at one value."""

        for lead in ("h00_12", "h12_24", "h24_48", "h48_plus"):
            self.assertEqual(
                radiation_shift_sigma_for_lead(lead), RADIATION_SHIFT_SIGMA
            )

    def test_charter_173_fits_new_artifacts_at_0_07(self) -> None:
        """Refitted on as-of radiation at the served width: 0.073 [0.016,
        0.121], with 0.15 scoring worse than no shift (user ruling 「批准」)."""

        self.assertEqual(RADIATION_SHIFT_SIGMA, 0.07)
        self.assertEqual(LEGACY_RADIATION_SHIFT_SIGMA, 0.15)

    def test_an_unknown_lead_gets_the_unearned_default(self) -> None:
        """A lead nobody has measured is not a lead to experiment on."""

        for lead in (None, "", "d2", "h96_plus"):
            self.assertEqual(
                radiation_shift_sigma_for_lead(lead), RADIATION_SHIFT_SIGMA
            )


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

    def test_the_gate_pins_the_value_it_scores_with(self) -> None:
        """One number resolved once: scored, pinned, read back.

        A default on any of the three lets them drift silently, which is the
        whole failure this pin exists to prevent.
        """

        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[1]
            / "bayes_basket_target" / "native_ensemble_oos.py"
        ).read_text()
        self.assertIn(
            "scope_radiation_sigma = radiation_shift_sigma_for_lead(",
            source,
        )
        self.assertIn(
            "radiation_shift_sigma=scope_radiation_sigma", source
        )
        self.assertIn(
            "candidate['radiation_shift_sigma']", source
        )

    def test_the_server_reads_the_pin_only_for_clearness_scored_artifacts(
        self,
    ) -> None:
        """Charter 160: a pin scored on the retired absolute form describes
        arithmetic nobody serves, so it is served the fleet value until the
        rotation re-scores it; a clearness-scored artifact gets its own pin."""

        from bayes_basket_target.cross_consensus import (
            RADIATION_CLEARNESS_METHOD_TAG,
            served_radiation_shift_sigma,
        )

        legacy = "x::synoptic_cooling_shift_v1::radiation_shift_sigma=0.35::y"
        # Charter 173 moved the fleet constant, not these: an untagged
        # artifact keeps the 0.15 it was served at before the ruling.
        self.assertEqual(served_radiation_shift_sigma(legacy), 0.15)
        self.assertNotEqual(served_radiation_shift_sigma(legacy), RADIATION_SHIFT_SIGMA)
        # A clearness artifact minted before charter 173 keeps its own 0.15.
        self.assertEqual(
            served_radiation_shift_sigma(
                "x::radiation_shift_sigma=0.15::" + RADIATION_CLEARNESS_METHOD_TAG
            ),
            0.15,
        )
        scored = (
            "x::synoptic_cooling_shift_v1::radiation_shift_sigma=0.2::"
            + RADIATION_CLEARNESS_METHOD_TAG + "::y"
        )
        self.assertEqual(served_radiation_shift_sigma(scored), 0.2)


if __name__ == "__main__":
    unittest.main()
