import math
import random
import unittest


class TestPlattCalibration(unittest.TestCase):
    def test_fit_platt_recovers_reasonable_params(self) -> None:
        from scan_weather_market import _fit_platt_logistic, _logit_safe, _sigmoid_safe

        rng = random.Random(0)
        true_a = 1.4
        true_b = -0.2
        samples = []
        # Grid of raw probabilities.
        for i in range(1, 99):
            p_raw = i / 100.0
            x = _logit_safe(p_raw)
            prob = _sigmoid_safe(true_a * x + true_b)
            y = 1 if rng.random() < prob else 0
            samples.append((x, y, 1.0))
        fit = _fit_platt_logistic(samples, l2=0.001)
        self.assertIsNotNone(fit)
        a = float(fit["a"])
        b = float(fit["b"])
        self.assertTrue(math.isfinite(a) and math.isfinite(b))
        # Loose bounds: we mainly want to avoid degenerate/NaN fits.
        self.assertLess(abs(a - true_a), 1.0)
        self.assertLess(abs(b - true_b), 1.0)

    def test_platt_predict_is_in_0_1(self) -> None:
        from scan_weather_market import _platt_predict

        for p_raw in [0.0, 1e-9, 0.1, 0.5, 0.9, 1 - 1e-9, 1.0]:
            val = _platt_predict(p_raw, 1.0, 0.0)
            self.assertIsNotNone(val)
            self.assertGreaterEqual(val or 0.0, 0.0)
            self.assertLessEqual(val or 0.0, 1.0)


if __name__ == "__main__":
    unittest.main()

