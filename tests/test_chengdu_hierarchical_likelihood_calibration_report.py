from scripts.chengdu_hierarchical_likelihood_calibration_report import (
    Bucket,
    actual_bucket_index,
    bucket_bounds_from_label,
    hierarchical_joint_distribution,
    score_distribution,
    state_probabilities,
)


def test_bucket_bounds_and_actual_mapping():
    assert bucket_bounds_from_label("25°C or below") == (None, 25.5)
    assert bucket_bounds_from_label("31°C") == (30.5, 31.5)
    assert bucket_bounds_from_label("35°C or higher") == (34.5, None)

    buckets = [
        Bucket("25°C or below", None, 25.5),
        Bucket("26°C", 25.5, 26.5),
        Bucket("27°C", 26.5, 27.5),
    ]
    assert actual_bucket_index(25.49, buckets) == 0
    assert actual_bucket_index(25.5, buckets) == 1
    assert actual_bucket_index(27.0, buckets) == 2


def test_state_probabilities_opening_environment():
    probs = state_probabilities(
        {
            "effective_cloud_cover_pct": 35.0,
            "low_cloud_cover_pct": 20.0,
            "visibility_good": True,
            "visibility_poor": False,
            "no_precip_confirmed": True,
            "precip_observed": False,
            "showery_or_convective": False,
            "temp_dewpoint_spread_c": 6.0,
            "drying": True,
            "moistening": False,
            "high_dewpoint": False,
        }
    )
    assert abs(sum(probs.values()) - 1.0) < 1e-9
    assert probs["S2_open_boundary_layer"] > probs["S0_wet_layer_closed"]
    assert probs["S1_opening"] > probs["S3_convective_cutoff"]


def test_hierarchical_distribution_respects_observed_today_max_floor():
    buckets = [
        Bucket("29°C", 28.5, 29.5),
        Bucket("30°C", 29.5, 30.5),
        Bucket("31°C", 30.5, 31.5),
        Bucket("32°C", 31.5, 32.5),
    ]
    prior = [0.25, 0.25, 0.25, 0.25]
    probs, states = hierarchical_joint_distribution(
        buckets=buckets,
        prior=prior,
        inputs={
            "current_temp_c": 30.0,
            "today_max_c": 31.0,
            "momentum_rate_c_per_hour": 0.0,
            "local_hour": 15.5,
            "effective_cloud_cover_pct": 80.0,
            "low_cloud_cover_pct": 60.0,
            "visibility_good": False,
            "visibility_poor": True,
            "no_precip_confirmed": False,
            "precip_observed": True,
            "showery_or_convective": True,
            "temp_dewpoint_spread_c": 2.0,
        },
    )
    assert probs is not None
    assert states
    assert probs[0] < 1e-6
    assert probs[1] < 1e-6
    assert abs(sum(probs) - 1.0) < 1e-9


def test_score_distribution_multiclass_metrics():
    score = score_distribution([0.1, 0.7, 0.2], actual_index=1)
    assert score["top_hit"] is True
    assert score["actual_prob"] == 0.7
    assert score["nll"] > 0
    assert score["brier"] < 0.2
