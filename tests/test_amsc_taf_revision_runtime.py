import pytest

from amsc_taf_revision_runtime import parse_zuuu_taf_revision, taf_revision_order_key


def test_taf_amd_is_a_distinct_weak_forecast_revision() -> None:
    result = parse_zuuu_taf_revision(
        "TAF AMD ZUUU 150530Z 1506/1606 02004MPS 6000 SCT040 TX35/1508Z TN26/1522Z=",
        first_seen_at="2026-07-15T05:31:02Z",
    )
    assert result["forecast_revision_kind"] == "amendment"
    assert result["issue_time_utc"] == "2026-07-15T05:30:00Z"
    assert result["forecast_peaks"] == [
        {
            "temperature_c": 35.0,
            "valid_time_utc": "2026-07-15T08:00:00Z",
            "event_date_local": "2026-07-15",
            "forecast_weather_regime": "clear_dry",
        }
    ]
    assert result["weak_forecast_only"] is True
    assert result["hard_observation_eligible"] is False


def test_taf_regime_is_evaluated_at_tx_peak_not_from_earlier_storm() -> None:
    result = parse_zuuu_taf_revision(
        "TAF AMD ZUUU 161632Z 1612/1712 33004MPS 6000 SCT050 "
        "TX35/1707Z TN26/1622Z TEMPO 1612/1614 -SHRA FEW030CB SCT030 "
        "BECMG 1615/1616 03005MPS -TSRA FEW030CB SCT030 "
        "BECMG 1619/1620 NSW SCT050=",
        first_seen_at="2026-07-16T16:49:01Z",
    )

    assert result["forecast_weather_regime"] == "clear_dry"


def test_taf_tempo_at_tx_peak_selects_convective_regime() -> None:
    result = parse_zuuu_taf_revision(
        "TAF ZUUU 170100Z 1700/1800 03004MPS SCT040 TX34/1707Z "
        "TEMPO 1706/1709 -TSRA SCT030CB=",
        first_seen_at="2026-07-17T01:01:00Z",
    )

    assert result["forecast_weather_regime"] == "convection_or_showers"


def test_original_and_correction_have_content_addressed_revision_identity() -> None:
    original = parse_zuuu_taf_revision(
        "TAF ZUUU 150500Z 1506/1606 02004MPS 6000 SCT040 TX36/1508Z TN26/1522Z=",
        first_seen_at="2026-07-15T05:01:00Z",
    )
    corrected = parse_zuuu_taf_revision(
        "TAF COR ZUUU 150500Z 1506/1606 02004MPS 6000 SCT040 TX35/1508Z TN26/1522Z=",
        first_seen_at="2026-07-15T05:16:00Z",
    )
    assert original["forecast_revision_kind"] == "original"
    assert corrected["forecast_revision_kind"] == "correction"
    assert original["source_revision_id"] != corrected["source_revision_id"]


def test_taf_revision_order_uses_issue_time_then_modifier_not_late_discovery() -> None:
    newer_original = taf_revision_order_key(
        issue_time_utc="2026-07-14T15:08:00Z",
        revision_kind="original",
        first_seen_at="2026-07-14T15:35:00Z",
        revision_id="newer-original",
    )
    late_old_amd = taf_revision_order_key(
        issue_time_utc="2026-07-14T12:49:00Z",
        revision_kind="amendment",
        first_seen_at="2026-07-14T17:30:00Z",
        revision_id="late-old-amd",
    )
    same_issue_cor = taf_revision_order_key(
        issue_time_utc="2026-07-14T15:08:00Z",
        revision_kind="correction",
        first_seen_at="2026-07-14T15:36:00Z",
        revision_id="same-issue-cor",
    )

    assert newer_original > late_old_amd
    assert same_issue_cor > newer_original


def test_taf_without_tx_is_not_silently_promoted_to_peak_forecast() -> None:
    with pytest.raises(ValueError, match="zuuu_taf_tx_missing"):
        parse_zuuu_taf_revision(
            "TAF AMD ZUUU 150530Z 1506/1606 02004MPS 6000 SCT040=",
            first_seen_at="2026-07-15T05:31:02Z",
        )
