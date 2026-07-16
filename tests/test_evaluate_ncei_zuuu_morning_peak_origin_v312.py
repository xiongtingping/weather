from scripts.evaluate_ncei_zuuu_morning_peak_origin_v312 import morning_peak_origin


def test_morning_peak_origin_requires_a_midnight_running_high():
    assert morning_peak_origin({"local_hour": 9.0, "hours_since_observed_max": 9.0}) is True
    assert morning_peak_origin({"local_hour": 9.0, "hours_since_observed_max": 7.0}) is False
    assert morning_peak_origin({"local_hour": 5.0, "hours_since_observed_max": 5.0}) is False
