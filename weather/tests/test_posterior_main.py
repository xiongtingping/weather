import sys
import types


def _posterior_main_from_fact_payload():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    if "certifi" not in sys.modules:
        sys.modules["certifi"] = types.SimpleNamespace(where=lambda: "")
    from ui.server import posterior_main_from_fact_payload

    return posterior_main_from_fact_payload


def test_posterior_main_prefers_corrected_equal_bucket_and_calibrated_q() -> None:
    payload = {
        "corrected_top_bucket": {"equal": "28°C"},
        "rows": [
            {"outcome": "27°C", "q_round": 0.2061, "q_calibrated": 0.2061},
            {"outcome": "28°C", "q_round": 0.19, "q_calibrated": 0.31},
        ],
    }

    label, q = _posterior_main_from_fact_payload()(payload)

    assert label == "28°C"
    assert q == 0.31


def test_posterior_main_falls_back_to_q_round_top() -> None:
    payload = {
        "rows": [
            {"outcome": "27°C", "q_round": 0.2061},
            {"outcome": "28°C", "q_round": 0.19},
        ],
    }

    label, q = _posterior_main_from_fact_payload()(payload)

    assert label == "27°C"
    assert q == 0.2061
