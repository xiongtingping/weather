import trade_strategy_loop_audit as audit


def _probability(ok=True):
    return {"ok": ok, "blockers": [] if ok else ["manifest_drift"], "warnings": []}


def _market(ready=True):
    return {
        "ok": True,
        "state": "production_ready" if ready else "safe_fail_closed_accumulating",
        "production_market_projection_ready": ready,
        "promotion_blockers": [] if ready else ["bridge_coverage_below_minimum"],
        "safety_blockers": [],
        "warnings": [],
    }


def _execution(ready=True):
    return {
        "live_activation_ready": ready,
        "live_activation_blockers": [] if ready else ["market_bucket_projection_unavailable"],
        "resident_deployment": {
            "all_live_flags_zero": not ready,
            "persistent_disabled": not ready,
            "loaded": ready,
        },
        "database": {"unresolved_intent_count": 0},
    }


def _policy(ok=True):
    return {"ok": ok, "blockers": [] if ok else ["legacy_ev_gate_present"]}


def _safety(ok=True):
    return {
        "ok": ok,
        "checks": {
            "stale_decision_blocks": ok,
            "duplicate_order_blocks": ok,
            "idempotency_contract_present": ok,
        },
        "warnings": [],
        "blockers": [] if ok else ["stale_decision_not_blocked"],
    }


def _build(**overrides):
    values = {
        "probability_report": _probability(),
        "market_report": _market(),
        "execution_report": _execution(),
        "policy_report": _policy(),
        "safety_report": _safety(),
    }
    values.update(overrides)
    return audit.build_report(**values)


def test_trade_strategy_loop_reports_live_ready_only_when_all_current_gates_close():
    report = _build()

    assert report["ok"] is True
    assert report["state"] == "live_ready"
    assert report["live_ready"] is True
    assert report["checks"]["runtime_policy_has_no_ev_edge_gate"] is True
    assert report["guards"]["places_orders"] is False


def test_trade_loop_consumes_readiness_boolean_not_nonexistent_ready_state():
    market = _market(True)
    market["state"] = "production_ready"
    report = _build(market_report=market)
    assert report["live_ready"] is True
    assert report["checks"]["market_projection_ready"] is True


def test_bridge_accumulation_is_safe_fail_closed_not_architecture_failure():
    report = _build(
        probability_report={
            "ok": False,
            "blockers": ["reported_x_successor_runtime_manifest_drift"],
            "warnings": [],
        },
        market_report=_market(False),
        execution_report=_execution(False),
    )

    assert report["ok"] is True
    assert report["state"] == "safe_fail_closed"
    assert report["live_ready"] is False
    assert "bridge_coverage_below_minimum" in report["promotion_blockers"]
    assert report["safety_blockers"] == []


def test_structured_probability_blocker_is_normalized_without_subprocess_noise():
    probability = {
        "ok": False,
        "blockers": [
            {
                "blocker": "reported_x_successor_runtime_manifest_drift",
                "details": {"authorized": "old", "current": "new"},
            },
            "subprocess_returncode_nonzero",
        ],
        "warnings": [],
    }
    report = _build(
        probability_report=probability,
        market_report=_market(False),
        execution_report=_execution(False),
    )

    assert report["ok"] is True
    assert "reported_x_successor_runtime_manifest_drift" in report["promotion_blockers"]
    assert "subprocess_returncode_nonzero" not in report["promotion_blockers"]


def test_structural_probability_uniqueness_failure_is_a_safety_blocker():
    report = _build(
        probability_report={
            "ok": False,
            "blockers": ["retired_probability_model_fallback_present"],
            "warnings": [],
        },
        market_report=_market(False),
        execution_report=_execution(False),
    )

    assert report["ok"] is False
    assert report["state"] == "unsafe_blocked"
    assert report["live_ready"] is False
    assert report["safety_blockers"] == [
        {
            "probability_mainline_safety_blockers": [
                "retired_probability_model_fallback_present"
            ]
        }
    ]


def test_trade_strategy_loop_blocks_runtime_policy_failure():
    report = _build(policy_report=_policy(False))

    assert report["ok"] is False
    assert report["state"] == "unsafe_blocked"
    assert report["safety_blockers"]
