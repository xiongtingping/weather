from __future__ import annotations

from scripts.collect_ecmwf_ifs_zuuu import _bounded_float_env, _bounded_int_env


def test_ecmwf_retry_policy_clamps_legacy_unbounded_values(monkeypatch) -> None:
    monkeypatch.setenv("PM_ECMWF_INDEX_MAX_RETRIES", "500")
    monkeypatch.setenv("PM_ECMWF_INDEX_TIMEOUT_SEC", "120")
    monkeypatch.setenv("PM_ECMWF_INDEX_WALL_CLOCK_SEC", "9999")
    monkeypatch.setenv("PM_ECMWF_RETRIEVE_WALL_CLOCK_SEC", "9999")

    assert _bounded_int_env("PM_ECMWF_INDEX_MAX_RETRIES", 3, minimum=1, maximum=4) == 4
    assert _bounded_float_env("PM_ECMWF_INDEX_TIMEOUT_SEC", 15.0, minimum=1.0, maximum=30.0) == 30.0
    assert _bounded_float_env("PM_ECMWF_INDEX_WALL_CLOCK_SEC", 45.0, minimum=5.0, maximum=90.0) == 90.0
    assert _bounded_float_env("PM_ECMWF_RETRIEVE_WALL_CLOCK_SEC", 120.0, minimum=15.0, maximum=300.0) == 300.0


def test_ecmwf_default_policy_is_finite() -> None:
    assert _bounded_int_env("PM_ECMWF_INDEX_MAX_RETRIES", 3, minimum=1, maximum=4) == 3
    assert _bounded_float_env("PM_ECMWF_INDEX_TIMEOUT_SEC", 15.0, minimum=1.0, maximum=30.0) == 15.0
    assert _bounded_float_env("PM_ECMWF_INDEX_WALL_CLOCK_SEC", 45.0, minimum=5.0, maximum=90.0) == 45.0
    assert _bounded_float_env("PM_ECMWF_RETRIEVE_WALL_CLOCK_SEC", 120.0, minimum=15.0, maximum=300.0) == 120.0
