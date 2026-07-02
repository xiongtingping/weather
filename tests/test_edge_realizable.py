import json
import os
import tempfile
import unittest
from unittest.mock import patch

from edge_realizable import edge_realizable_v2


class EdgeRealizableV2Test(unittest.TestCase):
    def _write_calibration(self, payload):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        try:
            json.dump(payload, handle)
            handle.flush()
            return handle.name
        finally:
            handle.close()

    def tearDown(self):
        for path in getattr(self, "_paths", []):
            try:
                os.unlink(path)
            except OSError:
                pass

    def test_defaults_are_not_bucket_evidence_for_p_realize(self):
        self._paths = []
        path = self._write_calibration(
            {
                "generated_at": "2026-05-05T00:00:00Z",
                "p_fill_entry_default": 0.80,
                "p_converge_default": 0.85,
                "p_fill_exit_default": 0.80,
                "hold_cost_params": {"mode": "linear", "k_per_hour": 0},
                "p_fill_entry_buckets": {},
                "p_converge_buckets": {},
                "p_fill_exit_buckets": {},
            }
        )
        self._paths.append(path)

        with patch.dict(
            os.environ,
            {
                "PM_EDGE_REALIZABLE_ALLOW_GLOBAL_DEFAULTS": "1",
                "PM_EDGE_REALIZABLE_MIN_SAMPLES": "20",
            },
        ):
            edge, meta = edge_realizable_v2(
                0.10,
                0.01,
                0.0,
                target_exit_price=0.06,
                base_bid=0.02,
                spread=0.004,
                depth_ratio=2.0,
                days_to_settle=1.0,
                order_type="GTC",
                calibration_path=path,
            )

        self.assertAlmostEqual(meta["p_realize"], 0.544)
        self.assertAlmostEqual(edge, 0.0544)
        self.assertTrue(meta["p_realize_missing"])
        self.assertTrue(meta["p_realize_uses_default"])
        self.assertEqual(meta["p_realize_evidence_status"], "default_only")
        self.assertEqual(
            meta["p_realize_na_reason"],
            "calibration_defaults_without_bucket_evidence",
        )
        self.assertEqual(
            meta["p_realize_missing_tables"],
            "p_converge_buckets,p_fill_entry_buckets,p_fill_exit_buckets",
        )

    def test_bucket_probabilities_are_execution_evidence(self):
        self._paths = []
        seed_path = self._write_calibration(
            {
                "p_fill_entry_default": 0.80,
                "p_converge_default": 0.85,
                "p_fill_exit_default": 0.80,
                "p_fill_entry_buckets": {},
                "p_converge_buckets": {},
                "p_fill_exit_buckets": {},
            }
        )
        self._paths.append(seed_path)
        with patch.dict(os.environ, {"PM_EDGE_REALIZABLE_ALLOW_GLOBAL_DEFAULTS": "1"}):
            _, seed_meta = edge_realizable_v2(
                0.10,
                0.01,
                0.0,
                target_exit_price=0.06,
                base_bid=0.02,
                spread=0.004,
                depth_ratio=2.0,
                days_to_settle=1.0,
                order_type="GTC",
                calibration_path=seed_path,
            )
        converge_key = seed_meta["p_converge_meta"]["bucket"]
        entry_key = seed_meta["p_fill_entry_meta"]["bucket"]
        exit_key = seed_meta["p_fill_exit_meta"]["bucket"]
        path = self._write_calibration(
            {
                "generated_at": "2026-05-05T00:00:00Z",
                "hold_cost_params": {"mode": "linear", "k_per_hour": 0},
                "p_converge_buckets": {converge_key: {"p": 0.50, "hit": 20, "total": 40}},
                "p_fill_entry_buckets": {entry_key: {"p": 0.60, "hit": 30, "total": 50}},
                "p_fill_exit_buckets": {exit_key: {"p": 0.70, "hit": 35, "total": 50}},
            }
        )
        self._paths.append(path)

        with patch.dict(
            os.environ,
            {
                "PM_EDGE_REALIZABLE_ALLOW_GLOBAL_DEFAULTS": "1",
                "PM_EDGE_REALIZABLE_MIN_SAMPLES": "20",
            },
        ):
            edge, meta = edge_realizable_v2(
                0.10,
                0.01,
                0.0,
                target_exit_price=0.06,
                base_bid=0.02,
                spread=0.004,
                depth_ratio=2.0,
                days_to_settle=1.0,
                order_type="GTC",
                calibration_path=path,
            )

        self.assertAlmostEqual(meta["p_realize"], 0.21)
        self.assertAlmostEqual(edge, 0.021)
        self.assertFalse(meta["p_realize_missing"])
        self.assertFalse(meta["p_realize_uses_default"])
        self.assertEqual(meta["p_realize_evidence_status"], "bucket_evidence")
        self.assertEqual(meta["p_realize_na_reason"], "")
        self.assertEqual(meta["p_realize_missing_tables"], "")

    def test_converge_uses_lower_delta_bucket_fallback(self):
        self._paths = []
        seed_path = self._write_calibration(
            {
                "p_fill_entry_default": 0.80,
                "p_converge_default": 0.85,
                "p_fill_exit_default": 0.80,
                "p_fill_entry_buckets": {},
                "p_converge_buckets": {},
                "p_fill_exit_buckets": {},
            }
        )
        self._paths.append(seed_path)
        with patch.dict(os.environ, {"PM_EDGE_REALIZABLE_ALLOW_GLOBAL_DEFAULTS": "1"}):
            _, seed_meta = edge_realizable_v2(
                0.10,
                0.01,
                0.0,
                target_exit_price=0.72,
                base_bid=0.02,
                spread=0.004,
                depth_ratio=2.0,
                days_to_settle=1.0,
                order_type="GTC",
                calibration_path=seed_path,
            )
        converge_key = seed_meta["p_converge_meta"]["bucket"]
        lower_converge_key = converge_key.replace("|x7|", "|x2|").replace("|ot:GTC", "|ot:ANY")
        entry_key = seed_meta["p_fill_entry_meta"]["bucket"]
        exit_key = seed_meta["p_fill_exit_meta"]["bucket"]
        path = self._write_calibration(
            {
                "generated_at": "2026-05-05T00:00:00Z",
                "hold_cost_params": {"mode": "linear", "k_per_hour": 0},
                "p_converge_buckets": {lower_converge_key: {"p": 0.40, "hit": 20, "total": 40}},
                "p_fill_entry_buckets": {entry_key: {"p": 0.60, "hit": 30, "total": 50}},
                "p_fill_exit_buckets": {exit_key: {"p": 0.70, "hit": 35, "total": 50}},
            }
        )
        self._paths.append(path)

        with patch.dict(
            os.environ,
            {
                "PM_EDGE_REALIZABLE_ALLOW_GLOBAL_DEFAULTS": "1",
                "PM_EDGE_REALIZABLE_MIN_SAMPLES": "20",
            },
        ):
            edge, meta = edge_realizable_v2(
                0.10,
                0.01,
                0.0,
                target_exit_price=0.72,
                base_bid=0.02,
                spread=0.004,
                depth_ratio=2.0,
                days_to_settle=1.0,
                order_type="GTC",
                calibration_path=path,
            )

        self.assertAlmostEqual(meta["p_converge"], 0.40)
        self.assertAlmostEqual(meta["p_realize"], 0.168)
        self.assertAlmostEqual(edge, 0.0168)
        self.assertFalse(meta["p_realize_missing"])
        self.assertEqual(meta["p_realize_evidence_status"], "bucket_evidence")
        self.assertEqual(meta["p_converge_bucket_key"], converge_key)
        self.assertEqual(meta["p_converge_bucket_used_key"], lower_converge_key)

    def test_entry_probability_uses_min_spread_fallback_when_spread_unknown(self):
        self._paths = []
        seed_path = self._write_calibration(
            {
                "p_fill_entry_default": 0.80,
                "p_converge_default": 0.85,
                "p_fill_exit_default": 0.80,
                "p_fill_entry_buckets": {},
                "p_converge_buckets": {},
                "p_fill_exit_buckets": {},
            }
        )
        self._paths.append(seed_path)
        with patch.dict(os.environ, {"PM_EDGE_REALIZABLE_ALLOW_GLOBAL_DEFAULTS": "1"}):
            _, seed_meta = edge_realizable_v2(
                0.10,
                0.01,
                0.0,
                target_exit_price=0.06,
                base_bid=0.02,
                spread=None,
                depth_ratio=2.0,
                days_to_settle=1.0,
                order_type="GTC",
                calibration_path=seed_path,
            )
        entry_key = seed_meta["p_fill_entry_meta"]["bucket"]
        exit_key = seed_meta["p_fill_exit_meta"]["bucket"]
        entry_fallback_key = entry_key.replace("|su|", "|s1|").replace("|ot:GTC", "|ot:ANY")
        exit_fallback_key = exit_key.replace("|su|", "|s1|").replace("|ot:GTC", "|ot:ANY")
        converge_key = seed_meta["p_converge_meta"]["bucket"]
        path = self._write_calibration(
            {
                "generated_at": "2026-05-05T00:00:00Z",
                "hold_cost_params": {"mode": "linear", "k_per_hour": 0},
                "p_converge_buckets": {converge_key: {"p": 0.50, "hit": 20, "total": 40}},
                "p_fill_entry_buckets": {entry_fallback_key: {"p": 0.60, "filled": 30, "total": 50}},
                "p_fill_exit_buckets": {exit_fallback_key: {"p": 0.70, "filled": 35, "total": 50}},
            }
        )
        self._paths.append(path)

        with patch.dict(
            os.environ,
            {
                "PM_EDGE_REALIZABLE_ALLOW_GLOBAL_DEFAULTS": "1",
                "PM_EDGE_REALIZABLE_MIN_SAMPLES": "20",
            },
        ):
            edge, meta = edge_realizable_v2(
                0.10,
                0.01,
                0.0,
                target_exit_price=0.06,
                base_bid=0.02,
                spread=None,
                depth_ratio=2.0,
                days_to_settle=1.0,
                order_type="GTC",
                calibration_path=path,
            )

        self.assertAlmostEqual(meta["p_realize"], 0.21)
        self.assertAlmostEqual(edge, 0.021)
        self.assertFalse(meta["p_realize_missing"])
        self.assertEqual(meta["p_fill_entry_bucket_key"], entry_key)
        self.assertEqual(meta["p_fill_entry_bucket_used_key"], entry_fallback_key)
        self.assertEqual(meta["p_fill_exit_bucket_key"], exit_key)
        self.assertEqual(meta["p_fill_exit_bucket_used_key"], exit_fallback_key)

    def test_low_sample_primary_bucket_uses_higher_sample_any_fallback(self):
        self._paths = []
        seed_path = self._write_calibration(
            {
                "p_fill_entry_default": 0.80,
                "p_converge_default": 0.85,
                "p_fill_exit_default": 0.80,
                "p_fill_entry_buckets": {},
                "p_converge_buckets": {},
                "p_fill_exit_buckets": {},
            }
        )
        self._paths.append(seed_path)
        with patch.dict(os.environ, {"PM_EDGE_REALIZABLE_ALLOW_GLOBAL_DEFAULTS": "1"}):
            _, seed_meta = edge_realizable_v2(
                0.10,
                0.01,
                0.0,
                target_exit_price=0.06,
                base_bid=0.02,
                spread=0.004,
                depth_ratio=2.0,
                days_to_settle=1.0,
                order_type="GTC",
                calibration_path=seed_path,
            )
        converge_key = seed_meta["p_converge_meta"]["bucket"]
        entry_key = seed_meta["p_fill_entry_meta"]["bucket"]
        exit_key = seed_meta["p_fill_exit_meta"]["bucket"]
        entry_any_key = entry_key.replace("|ot:GTC", "|ot:ANY")
        exit_any_key = exit_key.replace("|ot:GTC", "|ot:ANY")
        path = self._write_calibration(
            {
                "generated_at": "2026-05-05T00:00:00Z",
                "hold_cost_params": {"mode": "linear", "k_per_hour": 0},
                "p_converge_buckets": {converge_key: {"p": 0.50, "hit": 20, "total": 40}},
                "p_fill_entry_buckets": {
                    entry_key: {"p": 0.20, "filled": 1, "total": 3},
                    entry_any_key: {"p": 0.60, "filled": 30, "total": 50},
                },
                "p_fill_exit_buckets": {
                    exit_key: {"p": 0.30, "filled": 2, "total": 4},
                    exit_any_key: {"p": 0.70, "filled": 35, "total": 50},
                },
            }
        )
        self._paths.append(path)

        with patch.dict(
            os.environ,
            {
                "PM_EDGE_REALIZABLE_ALLOW_GLOBAL_DEFAULTS": "1",
                "PM_EDGE_REALIZABLE_MIN_SAMPLES": "20",
            },
        ):
            edge, meta = edge_realizable_v2(
                0.10,
                0.01,
                0.0,
                target_exit_price=0.06,
                base_bid=0.02,
                spread=0.004,
                depth_ratio=2.0,
                days_to_settle=1.0,
                order_type="GTC",
                calibration_path=path,
            )

        self.assertAlmostEqual(meta["p_realize"], 0.21)
        self.assertAlmostEqual(edge, 0.021)
        self.assertEqual(meta["p_realize_evidence_status"], "bucket_evidence")
        self.assertEqual(meta["p_fill_entry_evidence_status"], "bucket_evidence")
        self.assertEqual(meta["p_fill_exit_evidence_status"], "bucket_evidence")
        self.assertEqual(meta["p_fill_entry_bucket_key"], entry_key)
        self.assertEqual(meta["p_fill_entry_bucket_used_key"], entry_any_key)
        self.assertEqual(meta["p_fill_entry_samples"], 50)
        self.assertTrue(meta["p_fill_entry_meta"]["fallback_from_low_sample"])
        self.assertEqual(meta["p_fill_entry_meta"]["fallback_replaced_low_sample_total"], 3)
        self.assertEqual(meta["p_fill_exit_bucket_key"], exit_key)
        self.assertEqual(meta["p_fill_exit_bucket_used_key"], exit_any_key)
        self.assertEqual(meta["p_fill_exit_samples"], 50)
        self.assertTrue(meta["p_fill_exit_meta"]["fallback_from_low_sample"])
        self.assertEqual(meta["p_fill_exit_meta"]["fallback_replaced_low_sample_total"], 4)


if __name__ == "__main__":
    unittest.main()
