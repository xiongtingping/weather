from scripts.dynamic_bias_correction_shadow_report import build_report


def test_dynamic_bias_shadow_report_replaces_static_shift_not_additive() -> None:
    payload = {
        "generated_at": "2026-06-08T19:16:10+00:00",
        "start_date": "2025-06-09",
        "end_date": "2026-06-08",
        "history_window_policy": "available_history",
        "station_id": "ZUUU",
        "city": "chengdu",
        "bias_correction": {
            "recommended_scope": "wu_only",
            "wu_only": {
                "stats_by_model": {
                    "gfs_seamless": {"n": 365, "mean_bias_c": -1.0, "recommended_temp_shift_c": 1.0, "variance_c2": 1.0},
                    "icon_seamless": {"n": 365, "mean_bias_c": -0.5, "recommended_temp_shift_c": 0.5, "variance_c2": 0.5},
                }
            },
        },
    }
    report = build_report(payload, target_date=__import__("datetime").date(2026, 6, 11))

    assert report["double_correction_guard"]["must_not_add_to_existing_static_shift"] is True
    assert report["models"]["gfs_seamless"]["double_correction_guard"]["mode"] == "replace_static_shift_not_additive"
    assert report["models"]["gfs_seamless"]["static_shift_c"] == 1.0
    assert report["models"]["icon_seamless"]["static_shift_c"] == 0.5


def test_dynamic_bias_shadow_report_uses_season_month_recent_components() -> None:
    payload = {
        "generated_at": "2026-06-08T19:16:10+00:00",
        "start_date": "2025-06-09",
        "end_date": "2026-06-08",
        "history_window_policy": "available_history",
        "station_id": "ZUUU",
        "city": "chengdu",
        "bias_correction": {
            "recommended_scope": "wu_only",
            "wu_only": {
                "stats_by_model": {
                    "gfs_seamless": {"recommended_temp_shift_c": 1.0},
                },
                "pairs": [
                    {"event_date": "2026-05-01", "model": "gfs_seamless", "bias_c": -1.0},
                    {"event_date": "2026-05-02", "model": "gfs_seamless", "bias_c": -2.0},
                    {"event_date": "2026-06-01", "model": "gfs_seamless", "bias_c": -1.5},
                    {"event_date": "2026-06-02", "model": "gfs_seamless", "bias_c": 0.5},
                ],
            },
        },
    }
    report = build_report(payload, target_date=__import__("datetime").date(2026, 6, 11))
    model = report["models"]["gfs_seamless"]

    names = {c["name"] for c in model["components"]}
    assert {"long_term", "season", "month", "recent", "day_of_year_window"} <= names
    assert model["dynamic_shift_c"] is not None
    assert model["dynamic_shift_c"] != model["static_shift_c"]


def test_dynamic_bias_shadow_report_includes_walk_forward_backtest() -> None:
    pairs = [
        {"event_date": f"2026-01-{day:02d}", "model": "gfs_seamless", "bias_c": -1.0}
        for day in range(1, 29)
    ] + [
        {"event_date": f"2026-02-{day:02d}", "model": "gfs_seamless", "bias_c": -0.5}
        for day in range(1, 15)
    ]
    payload = {
        "generated_at": "2026-06-08T19:16:10+00:00",
        "start_date": "2026-01-01",
        "end_date": "2026-02-14",
        "history_window_policy": "available_history",
        "station_id": "ZUUU",
        "city": "chengdu",
        "bias_correction": {
            "recommended_scope": "wu_only",
            "wu_only": {
                "stats_by_model": {"gfs_seamless": {"recommended_temp_shift_c": 0.9}},
                "pairs": pairs,
            },
        },
    }
    report = build_report(
        payload,
        target_date=__import__("datetime").date(2026, 2, 15),
        min_backtest_history_days=10,
    )

    backtest = report["walk_forward_backtest"]
    assert backtest["method"] == "walk_forward_dynamic_shift_on_model_mean_residuals"
    assert backtest["evaluated"] > 0
    assert backtest["apply_gate"]["status"] == "not_auto_approved"


def test_dynamic_bias_shadow_report_carries_d0_afternoon_underestimation_slice() -> None:
    payload = {
        "generated_at": "2026-06-22T08:56:00+00:00",
        "start_date": "2026-06-22",
        "end_date": "2026-06-22",
        "history_window_policy": "explicit_start_date",
        "station_id": "ZUUU",
        "city": "chengdu",
        "bias_correction": {
            "recommended_scope": "wu_only",
            "wu_only": {
                "stats_by_model": {"gfs_seamless": {"recommended_temp_shift_c": 1.0}},
                "pairs": [{"event_date": "2026-06-21", "model": "gfs_seamless", "bias_c": -1.0}],
            },
        },
        "d0_afternoon_high_temp_underestimation": {
            "available": True,
            "underestimated_count": 1,
            "shadow_parameter_gate": {
                "status": "insufficient_samples",
                "runtime_probability_write": False,
            },
            "rows": [
                {
                    "event_date": "2026-06-22",
                    "reference_actual_or_observed_max_c": 33.0,
                    "soft_peak_max_c": 31.2,
                    "underestimate_gap_c": 1.8,
                }
            ],
        },
    }

    report = build_report(payload, target_date=__import__("datetime").date(2026, 6, 22))

    assert report["d0_afternoon_high_temp_underestimation"]["available"] is True
    assert report["d0_afternoon_high_temp_underestimation"]["underestimated_count"] == 1
    assert report["d0_afternoon_underestimation_shadow_gate"]["status"] == "insufficient_samples"
    assert report["apply_recommendation"]["d0_afternoon_underestimation_shadow_gate_status"] == "insufficient_samples"
