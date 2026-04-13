import unittest

from scan_weather_market import (
    _bucket_expected_value_from_label,
    _bucket_label_for_value,
    apply_rounding,
)


class TestTemperatureLogic(unittest.TestCase):
    def test_open_interval_expected_value_no_offset(self) -> None:
        self.assertEqual(_bucket_expected_value_from_label("10 C or higher", None), 10.0)
        self.assertEqual(_bucket_expected_value_from_label("5 C or below", None), 5.0)

    def test_whole_degrees_floor_rounding(self) -> None:
        self.assertEqual(apply_rounding(-2.1, "whole_degrees", "C"), -3.0)
        self.assertEqual(apply_rounding(2.9, "whole_degrees", "C"), 2.0)

    def test_range_expected_value_floor_negative(self) -> None:
        expected = _bucket_expected_value_from_label("-1-0 C", "whole_degrees")
        self.assertEqual(expected, -1.0)

    def test_sparse_buckets_return_empty_label(self) -> None:
        rows = [{"outcome": "0-1 C"}, {"outcome": "3-4 C"}]
        self.assertEqual(_bucket_label_for_value(rows, 2.0, None), "")

    def test_bucket_label_uses_floor_for_negative(self) -> None:
        rows = [{"outcome": "-1-0 C"}, {"outcome": "0-1 C"}]
        self.assertEqual(_bucket_label_for_value(rows, -0.1, "whole_degrees"), "-1-0 C")


if __name__ == "__main__":
    unittest.main()
