import json
import tempfile
import unittest

from edge_path_runtime import estimate_path_probability


class EdgePathRuntimeTests(unittest.TestCase):
    def _write_calibration(self, payload):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        with tmp:
            json.dump(payload, tmp)
        return tmp.name

    def test_estimate_path_probability_exact_bucket_hit(self):
        key = "d0|s1|r6|e6|x1|ot:GTC"
        path = self._write_calibration(
            {
                "generated_at": "2026-03-03T00:00:00+00:00",
                "p_converge_buckets": {
                    key: {"p": 0.42, "total": 200},
                },
            }
        )
        out = estimate_path_probability(
            signal_alpha=0.1,
            spread=0.02,
            depth_ratio=2.0,
            hours_to_settle=12.0,
            calibration_bucket_key=key,
            calibration_path=path,
        )
        self.assertEqual(out.get("path_source"), "calibration_p_converge")
        self.assertAlmostEqual(float(out.get("p_reach")), 0.42, places=9)
        self.assertEqual(out.get("path_bucket_key_used"), key)
        self.assertEqual(out.get("path_fallback_rank"), 0)
        self.assertEqual(out.get("path_sample_n"), 200)

    def test_estimate_path_probability_fallback_bucket_hit(self):
        input_key = "d0|s1|r6|e6|x1|ot:GTC"
        fallback_key = "d0|s1|ru|eu|x1|ot:ANY"
        path = self._write_calibration(
            {
                "generated_at": "2026-03-03T00:00:00+00:00",
                "p_converge_buckets": {
                    fallback_key: {"p": 0.02945859872611465, "total": 1254},
                },
            }
        )
        out = estimate_path_probability(
            signal_alpha=0.05,
            spread=0.03,
            depth_ratio=1.0,
            hours_to_settle=24.0,
            calibration_bucket_key=input_key,
            calibration_path=path,
        )
        self.assertEqual(out.get("path_source"), "calibration_p_converge_fallback")
        self.assertAlmostEqual(float(out.get("p_reach")), 0.02945859872611465, places=12)
        self.assertEqual(out.get("path_bucket_key_used"), fallback_key)
        self.assertIsNotNone(out.get("path_fallback_rank"))
        self.assertGreater(int(out.get("path_fallback_rank")), 0)
        self.assertEqual(out.get("path_sample_n"), 1254)


if __name__ == "__main__":
    unittest.main()
