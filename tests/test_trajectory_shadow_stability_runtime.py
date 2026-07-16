from trajectory_shadow_stability_runtime import build_shadow_stability_audit


def _candidate(as_of: str, probabilities: dict[str, float], state: str, *, hard_floor=None, selected_points=None):
    return {
        "as_of": as_of,
        "candidate_only": True,
        "production_applied": False,
        "canonical_observation_snapshot": {
            "as_of": as_of,
            "selected_points": selected_points or [],
        },
        "temperature_trajectory_posterior": {"state": state},
        "remaining_daily_high_distribution": {"hard_floor": hard_floor or {key: 1.0 for key in probabilities}},
        "metar_strong_likelihood_candidate": {"likelihood_map": probabilities},
    }


def test_stability_audit_measures_probability_and_state_switches_without_treating_them_as_violations():
    report = build_shadow_stability_audit(
        [
            _candidate("2026-07-05T02:00:00Z", {"35°C": 0.7, "36°C": 0.3}, "emerging_warming"),
            _candidate("2026-07-05T02:30:00Z", {"35°C": 0.2, "36°C": 0.8}, "sustained_warming"),
        ]
    )

    assert report["ok"] is True
    assert report["candidate_count"] == 2
    assert report["transition_count"] == 1
    assert report["top_bucket_switch_count"] == 1
    assert report["trajectory_state_switch_count"] == 1
    assert report["max_total_variation_distance"] == 0.5
    assert report["max_bucket_probability_shift"] == 0.5


def test_stability_audit_blocks_future_late_and_hard_floor_leaks():
    report = build_shadow_stability_audit(
        [
            _candidate(
                "2026-07-05T02:00:00Z",
                {"35°C": 0.1, "36°C": 0.9},
                "confirmed_cooling_cutoff",
                hard_floor={"35°C": 0.0, "36°C": 1.0},
                selected_points=[
                    {
                        "verified_metar_valid_time": "2026-07-05T02:30:00Z",
                        "first_seen_at": "2026-07-05T02:30:00Z",
                    }
                ],
            )
        ]
    )

    issues = {row["issue"] for row in report["violations"]}
    assert report["ok"] is False
    assert "future_observation_leak" in issues
    assert "late_arrival_observation_leak" in issues
    assert "hard_floor_violation:35°C" in issues
