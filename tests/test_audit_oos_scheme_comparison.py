"""Pooling the three schemes, and intervalling the equal-minus-learned gap.

The comparison itself is arithmetic; the part that can quietly go wrong is the
resampling unit.  Forty-five stations share one atmosphere, so their errors on
a date are anything but independent, and resampling station-days rather than
days shrinks the interval by roughly the square root of the station count --
enough to report a settled finding where the evidence is a fortnight of
weather.  These tests pin the unit, the sign convention, and the refusal to
quote an interval that cannot be estimated.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from audit_oos_scheme_comparison import _bootstrap, compare  # noqa: E402


def _row(
    day: str,
    *,
    equal_nll: float,
    learned_nll: float,
    station: str = "KLGA",
) -> dict[str, object]:
    def metrics(nll: float) -> dict[str, float]:
        return {"crps": nll / 2.0, "brier": nll / 3.0, "nll": nll}

    return {
        "station_id": station,
        "scope": (station, "JJA", "h00_12", "00Z"),
        "oos_state": "PRODUCTION_NO_LIVE_OOS_PASS",
        "event_date": day,
        "candidate_set": ("ECMWF_IFS_ENS_OPEN_PF50", "NOAA_GEFS_05"),
        "climate": metrics(2.9),
        "equal_weight": metrics(equal_nll),
        "learned": metrics(learned_nll),
    }


class ResamplingUnitTest(unittest.TestCase):
    def test_the_day_is_the_unit_not_the_station_day(self) -> None:
        """Same numbers, two groupings: the clustered one must be wider.

        Every station on a date is given the identical value here, which is
        the limiting case of the correlation the real data has.  Treating them
        as independent draws is what manufactures a narrow interval.
        """

        by_day = {f"d{index:02d}": [0.1 * index] * 45 for index in range(20)}
        by_station_day = {
            f"d{index:02d}_s{seat:02d}": [0.1 * index]
            for index in range(20)
            for seat in range(45)
        }
        clustered = _bootstrap(by_day, draws=2000, seed=1)
        flattened = _bootstrap(by_station_day, draws=2000, seed=1)
        self.assertAlmostEqual(
            clustered["mean"], flattened["mean"], places=9
        )
        self.assertGreater(
            clustered["ci_high"] - clustered["ci_low"],
            3.0 * (flattened["ci_high"] - flattened["ci_low"]),
        )

    def test_an_unestimable_interval_is_refused_not_invented(self) -> None:
        single = _bootstrap({"d0": [0.1, 0.2, 0.3]}, draws=500, seed=1)
        self.assertEqual(single["day_count"], 1)
        self.assertEqual(single["draws"], 0)
        self.assertNotEqual(single["ci_low"], single["ci_low"])  # NaN
        self.assertAlmostEqual(single["mean"], 0.2, places=9)

    def test_the_interval_brackets_a_clean_effect(self) -> None:
        by_day = {f"d{index:02d}": [0.5] for index in range(12)}
        interval = _bootstrap(by_day, draws=2000, seed=3)
        self.assertAlmostEqual(interval["mean"], 0.5, places=9)
        self.assertLessEqual(interval["ci_low"], 0.5)
        self.assertLessEqual(0.5, interval["ci_high"])


class SchemeComparisonTest(unittest.TestCase):
    def test_positive_gap_means_the_learned_weights_won(self) -> None:
        """Every score is a loss, so equal-minus-learned > 0 favours learned."""

        rows = [
            _row(f"2026-08-{day:02d}", equal_nll=2.0, learned_nll=1.8)
            for day in range(1, 13)
        ]
        report = compare(rows, draws=1500, seed=5)
        gap = report["equal_minus_learned"]
        self.assertAlmostEqual(gap["nll"]["mean"], 0.2, places=9)
        self.assertGreater(gap["log_score_bits"]["mean"], 0.0)
        self.assertEqual(
            report["learned_beats_equal_station_day_share"], 1.0
        )

    def test_negative_gap_survives_when_averaging_wins(self) -> None:
        rows = [
            _row(f"2026-08-{day:02d}", equal_nll=1.7, learned_nll=1.9)
            for day in range(1, 13)
        ]
        report = compare(rows, draws=1500, seed=5)
        gap = report["equal_minus_learned"]
        self.assertLess(gap["nll"]["mean"], 0.0)
        self.assertLess(gap["log_score_bits"]["ci_high"], 0.0)
        self.assertEqual(
            report["learned_beats_equal_station_day_share"], 0.0
        )

    def test_all_three_schemes_are_reported_on_every_metric(self) -> None:
        rows = [
            _row(f"2026-08-{day:02d}", equal_nll=2.0, learned_nll=1.9)
            for day in range(1, 6)
        ]
        means = compare(rows, draws=200, seed=5)["scheme_means"]
        self.assertEqual(
            sorted(means), ["climate", "equal_weight", "learned"]
        )
        for scheme in means:
            self.assertEqual(
                sorted(means[scheme]),
                ["brier", "crps", "log_score_bits", "nll"],
            )

    def test_the_candidate_set_is_named_in_the_report(self) -> None:
        """Pooling across different candidate sets has to be visible."""

        rows = [
            _row(f"2026-08-{day:02d}", equal_nll=2.0, learned_nll=1.9)
            for day in range(1, 6)
        ]
        rows.append({
            **_row("2026-08-06", equal_nll=2.0, learned_nll=1.9),
            "candidate_set": ("ECCC_GEM_GLOBAL_ENS", "NOAA_GEFS_05"),
        })
        report = compare(rows, draws=200, seed=5)
        self.assertEqual(len(report["distinct_candidate_sets"]), 2)

    def test_counts_separate_days_from_station_days(self) -> None:
        rows = [
            _row("2026-08-01", equal_nll=2.0, learned_nll=1.9, station=st)
            for st in ("KLGA", "KJFK", "KBOS")
        ] + [
            _row("2026-08-02", equal_nll=2.0, learned_nll=1.9, station=st)
            for st in ("KLGA", "KJFK")
        ]
        report = compare(rows, draws=200, seed=5)
        self.assertEqual(report["station_day_count"], 5)
        self.assertEqual(report["day_count"], 2)
        self.assertEqual(report["station_count"], 3)
        self.assertEqual(report["resampling_unit"], "event_date")


if __name__ == "__main__":
    unittest.main()
