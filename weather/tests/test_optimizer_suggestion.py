import os


def test_suggest_edge_bias_gate_from_payload_basic() -> None:
    from ui.server import suggest_edge_bias_gate_from_payload

    os.environ["PM_EDGE_BIAS_OPT_TARGET_COVERAGE"] = "0.80"
    os.environ["PM_EDGE_BIAS_OPT_MIN_BIN_COUNT"] = "10"

    payload = {
        "ok": True,
        "event_basis": {
            "ok": True,
            "calibration": {
                "bins": [
                    # pred_mean must be >0 to be considered
                    {"range": [0, 2], "count": 50, "pred_mean": 1.0, "real_mean": 0.5, "ratio": 0.5},
                    {"range": [2, 5], "count": 50, "pred_mean": 3.0, "real_mean": 2.7, "ratio": 0.9},
                ]
            },
        },
    }
    out = suggest_edge_bias_gate_from_payload(payload, days=30)
    assert isinstance(out, dict)
    assert out.get("ok") is True
    proposal = out.get("proposal") or {}
    t = proposal.get("entry_edge_confidence_min")
    assert isinstance(t, float)
    assert 0.0 <= t <= 2.0

