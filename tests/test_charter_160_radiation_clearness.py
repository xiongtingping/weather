"""Charter 160: the radiation shift standardises the clearness index.

The absolute standardisation compared a September day with a history that runs
through July, so the sun's own seasonal descent read as cloud (fleet September
z -0.44 absolute, -0.05 as a clearness index), and that part carried no
information.  The index divides out the astronomy and keeps the weather.
"""

from __future__ import annotations

import sqlite3
import unittest

from bayes_basket_target import native_ensemble_p0 as P0
from bayes_basket_target.cross_consensus import (
    RADIATION_CLEARNESS_METHOD_TAG,
    RADIATION_SHIFT_SIGMA_COLD,
    RADIATION_SHIFT_SIGMA_WARM,
    clearness_index,
    extraterrestrial_radiation_mj,
    forecast_clearness_shift_sigma,
    forecast_radiation_shift_sigma,
)

# The pair serving applies (charter 222); the coefficient is a required
# argument, so every call here names the one it is scored at.
SERVED = {
    "shift_sigma_per_unit": RADIATION_SHIFT_SIGMA_WARM,
    "cold_shift_sigma_per_unit": RADIATION_SHIFT_SIGMA_COLD,
}


class ExtraterrestrialTest(unittest.TestCase):
    def test_fao56_values_at_chengdu(self) -> None:
        """ZUUU (30.6N): about 40 MJ/m^2 in mid-July, 32 at the equinox,
        19 at the winter solstice."""

        self.assertAlmostEqual(extraterrestrial_radiation_mj(30.576, "2026-07-15"), 40.4, delta=0.3)
        self.assertAlmostEqual(extraterrestrial_radiation_mj(30.576, "2026-09-20"), 32.2, delta=0.3)
        self.assertAlmostEqual(extraterrestrial_radiation_mj(30.576, "2026-12-21"), 19.3, delta=0.3)

    def test_the_southern_spring_rises_while_the_north_falls(self) -> None:
        north = [extraterrestrial_radiation_mj(40.0, d) for d in ("2026-08-20", "2026-09-20")]
        south = [extraterrestrial_radiation_mj(-35.0, d) for d in ("2026-08-20", "2026-09-20")]
        self.assertGreater(north[0], north[1])
        self.assertLess(south[0], south[1])

    def test_unreadable_inputs_are_none(self) -> None:
        self.assertIsNone(extraterrestrial_radiation_mj(None, "2026-09-20"))
        self.assertIsNone(extraterrestrial_radiation_mj(30.0, "not-a-date"))
        self.assertIsNone(clearness_index(None, 30.0, "2026-09-20"))


def _seasonal_history(latitude: float, kt: float, start_month: int = 4) -> list[tuple[str, float]]:
    """A station whose weather never changes: the same clearness every day,
    so its absolute radiation follows the sun alone."""

    from datetime import date, timedelta

    day = date(2026, start_month, 15)
    rows = []
    while day < date(2026, 9, 20):
        top = extraterrestrial_radiation_mj(latitude, day.isoformat())
        rows.append((day.isoformat(), kt * top))
        day += timedelta(days=1)
    return rows


class SeasonIsNotCloudTest(unittest.TestCase):
    def test_a_constant_weather_day_is_not_called_dark(self) -> None:
        """The case the charter exists for: same weather as every past day.

        The absolute form calls a September day at the station's usual
        clearness a dark day (the July rows are brighter); the index calls it
        ordinary.  Noise is added so the history has a spread.
        """

        history = [
            (d, v * (1.0 + 0.2 * ((i * 7919) % 11 - 5) / 5.0))
            for i, (d, v) in enumerate(_seasonal_history(30.6, 0.45))
        ]
        today = 0.45 * extraterrestrial_radiation_mj(30.6, "2026-09-20")
        new, receipt = forecast_clearness_shift_sigma(
            radiation_sum=today, event_date="2026-09-20",
            latitude_deg=30.6, station_history=history, **SERVED,
        )
        old, old_receipt = forecast_radiation_shift_sigma(
            radiation_sum=today, station_history=[v for _d, v in history],
            **SERVED,
        )
        self.assertEqual(receipt["state"], "RESOLVED")
        self.assertEqual(receipt["method_tag"], RADIATION_CLEARNESS_METHOD_TAG)
        self.assertLess(abs(receipt["standardised"]), 0.3)
        # The absolute form calls it dark and pushes it cold.
        self.assertLess(old_receipt["standardised"], -1.0)
        self.assertLess(old, 0.0)
        side = (
            RADIATION_SHIFT_SIGMA_COLD if receipt["standardised"] < 0.0
            else RADIATION_SHIFT_SIGMA_WARM
        )
        self.assertEqual(receipt["shift_sigma_per_unit"], side)
        # Charter 222: net of the pair's centre on the station's history.
        self.assertAlmostEqual(
            new, side * receipt["standardised"] - receipt["side_centre_sigma"], places=12
        )

    def test_history_on_or_after_the_event_day_is_ignored(self) -> None:
        history = _seasonal_history(30.6, 0.45)
        with_future = history + [("2026-09-20", 1.0), ("2026-09-21", 1.0), ("2026-09-25", 1.0)]
        a = forecast_clearness_shift_sigma(
            radiation_sum=10.0, event_date="2026-09-20", latitude_deg=30.6,
            station_history=[(d, v * 1.01 if i % 2 else v) for i, (d, v) in enumerate(history)],
            **SERVED,
        )[1]
        b = forecast_clearness_shift_sigma(
            radiation_sum=10.0, event_date="2026-09-20", latitude_deg=30.6,
            station_history=[(d, v * 1.01 if i % 2 else v) for i, (d, v) in enumerate(history)]
            + with_future[len(history):],
            **SERVED,
        )[1]
        self.assertEqual(a["sample_count"], b["sample_count"])
        self.assertAlmostEqual(a["standardised"], b["standardised"], places=12)

    def test_missing_pieces_refuse_for_the_caller_to_fall_back(self) -> None:
        self.assertEqual(
            forecast_clearness_shift_sigma(
                radiation_sum=10.0, event_date="2026-09-20", latitude_deg=None,
                station_history=_seasonal_history(30.6, 0.45), **SERVED,
            )[1]["state"], "NO_LATITUDE")
        self.assertEqual(
            forecast_clearness_shift_sigma(
                radiation_sum=10.0, event_date="2026-09-20", latitude_deg=30.6,
                station_history=_seasonal_history(30.6, 0.45)[-5:], **SERVED,
            )[1]["state"], "HISTORY_TOO_THIN")
        self.assertEqual(
            forecast_clearness_shift_sigma(
                radiation_sum=None, event_date="2026-09-20", latitude_deg=30.6,
                station_history=[], **SERVED,
            )[1]["state"], "NO_FORECAST_RADIATION")


class ServingReaderTest(unittest.TestCase):
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE calibration_station_forecast_physics("
            "station_id TEXT, local_date TEXT, shortwave_radiation_sum REAL,"
            " reference_only INTEGER)"
        )
        rows = [("ZUUU", d, v, 1) for d, v in _seasonal_history(30.6, 0.45)]
        rows += [("ZUUU", "2026-09-20", 10.6, 1), ("ZUUU", "2026-09-21", 16.8, 1)]
        conn.executemany("INSERT INTO calibration_station_forecast_physics VALUES(?,?,?,?)", rows)
        return conn

    def test_the_dated_reader_stops_before_the_event_day(self) -> None:
        conn = self._conn()
        rows = P0._forecast_radiation_history_dated(conn, station_id="ZUUU", event_date="2026-09-20")
        self.assertTrue(rows)
        self.assertTrue(all(d < "2026-09-20" for d, _v in rows))
        self.assertLessEqual(len(rows), P0.FORECAST_PHYSICS_HISTORY_DAYS)

    def test_p0_serves_the_clearness_form_and_keeps_the_shadow(self) -> None:
        source = __import__("inspect").getsource(P0.compile_native_ensemble_p0)
        self.assertIn("forecast_clearness_shift_sigma(", source)
        self.assertIn('"legacy_v1"', source)
        self.assertIn("served_radiation_shift_sigmas(", source)
        # v8 since charter 190 (the width layer's open edges), v9 since
        # charter 198 (the rung and the activation read); the clearness form
        # is unchanged by either.  v16 since charter 222 (its coefficient's
        # two sides).
        self.assertEqual(P0.POSTERIOR_VERSION, "calibrated_native_cross_ensemble_p0_v32")


if __name__ == "__main__":
    unittest.main()
