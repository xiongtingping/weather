from datetime import datetime, timezone
import os
import unittest

from lcb_stage_runtime import resolve_lcb_stage_z


class LcbStageRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = os.environ.copy()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)

    def test_stage_map_can_override_d0_alias(self) -> None:
        os.environ["PM_TEST_Z_BY_STAGE"] = "d2_plus=1.64,d1=1.28,d0=0.8"

        out = resolve_lcb_stage_z(
            "PM_TEST_Z",
            default=1.0,
            days_to_event=0,
            decision_ts=datetime(2026, 5, 31, 15, 0, tzinfo=timezone.utc),
            stage_defaults={},
        )

        self.assertEqual(out["stage"], "d0_afternoon")
        self.assertAlmostEqual(float(out["z"]), 0.8)
        self.assertEqual(out["source"], "PM_TEST_Z_BY_STAGE:d0")

    def test_direct_stage_env_wins_over_defaults(self) -> None:
        os.environ["PM_TEST_Z_D1"] = "1.11"

        out = resolve_lcb_stage_z(
            "PM_TEST_Z",
            default=1.64,
            days_to_event=1,
            decision_ts=datetime(2026, 5, 31, 9, 0, tzinfo=timezone.utc),
            stage_defaults={"d1": 1.28},
        )

        self.assertEqual(out["stage"], "d1")
        self.assertAlmostEqual(float(out["z"]), 1.11)
        self.assertEqual(out["source"], "PM_TEST_Z_D1")


if __name__ == "__main__":
    unittest.main()
