from __future__ import annotations

from pathlib import Path

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.taf_exact_join_audit import (
    MIN_TAF_NATIVE_OOS_SAMPLE_N,
    audit_taf_p0m_exact_join_45station,
)


def _registry() -> list[dict[str, str]]:
    return [{
        "station_id": f"T{index:03d}",
        "timezone_name": "Etc/UTC",
        "source_lane": "AWC",
        "settlement_definition": "official_daily_tmax_c",
    } for index in range(expected_station_count())]


def _p0_inbox() -> dict:
    packet = {
        "stage": "P0",
        "station_id": "T000",
        "settlement_timezone": "Etc/UTC",
        "native_probability_envelope": {
            "station_id": "T000",
            "lineage": {
                "season_bin": "JJA",
                "lead_bucket": "d2",
                "run_cycle": "18Z",
                "settlement_definition": "official_daily_tmax_c",
            },
        },
    }
    identity = {
        "schema": "bayes_basket_target_p0_inbox_v1",
        "generated_at_utc": "2026-08-02T00:00:00Z",
        "packets": [{"packet": packet, "packet_content_sha256": "a" * 64}],
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _sample(event_date: str, *, capture: str = "a" * 64) -> dict:
    return {
        "station_id": "T000",
        "event_date": event_date,
        "settlement_timezone": "Etc/UTC",
        "calibration_scope": {
            "season_bin": "JJA",
            "lead_bucket": "d2",
            "run_cycle": "18Z",
            "settlement_definition": "official_daily_tmax_c",
        },
        "native_p0": {
            "prediction_oos": True,
            "ensemble_system_ids": ["ECMWF_TEST", "NOAA_TEST"],
            "independence_family_ids": ["ECMWF", "NOAA_GEFS"],
        },
        "taf": {
            "capture_content_sha256": capture,
            "source_class": "official_forward_native_receipt",
        },
        "truth": {
            "canonical_truth_id": f"truth-{event_date}",
            "source_lane": "AWC",
        },
    }


def _exact_join(samples: list[dict], *, excluded: list[dict] | None = None) -> dict:
    excluded = list(excluded or [])
    identity = {
        "schema": "taf_native_ensemble_strict_oos_exact_join_v1",
        "source_class": "strict_oos_native_p0_taf_truth_exact_join",
        "station_id": "T000",
        "season_bin": "JJA",
        "lead_bucket": "d2",
        "run_cycle": "18Z",
        "settlement_definition": "official_daily_tmax_c",
        "settlement_timezone": "Etc/UTC",
        "sample_count": len(samples),
        "samples": samples,
        "excluded_correction_sample_count": len(excluded),
        "excluded_correction_samples": excluded,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _pointers(sample_dates: list[str]) -> tuple[list[dict], list[dict]]:
    captures = [{
        "kind": "FORWARD",
        "content_sha256": "a" * 64,
        "station_id": "T000",
        "event_date": date,
        "settlement_timezone": "Etc/UTC",
        "revision_id": f"taf-{date}",
        "revision_kind": "TAF",
        "provider_available_at_utc": "2026-07-01T00:00:00Z",
        "source_object_sha256": "c" * 64,
        "source_class": "official_forward_native_receipt",
    } for date in sample_dates]
    # A capture hash is globally unique in production.  Tests which need many
    # exact samples vary the content address below to preserve that invariant.
    if len(captures) > 1:
        captures = [{
            **row,
            "content_sha256": f"{index:064x}",
        } for index, row in enumerate(captures, start=1)]
    truths = [{
        "truth_id": f"truth-{date}",
        "station_id": "T000",
        "event_date": date,
        "settlement_definition": "official_daily_tmax_c",
        "source_lane": "AWC",
        "qc_status": "ACCEPTED",
        "authority_scope": "FORMAL_NATIVE_ELIGIBLE",
        "source_payload_sha256": "d" * 64,
        "latest_accepted": True,
    } for date in sample_dates]
    return captures, truths


def _entry(result: dict, station: str = "T000") -> dict:
    return next(item for item in result["entries"] if item["station_id"] == station)


def test_audit_preserves_24_sample_gate_and_reports_all_45_stations() -> None:
    sample = _sample("2026-07-01")
    captures, truths = _pointers(["2026-07-01"])
    result = audit_taf_p0m_exact_join_45station(
        station_registry=_registry(),
        p0_inbox=_p0_inbox(),
        p0_inbox_path=Path("/tmp/p0-inbox.json"),
        exact_joins=[(Path("T000.json"), _exact_join([sample]))],
        taf_capture_pointers=captures,
        truth_pointers=truths,
    )

    entry = _entry(result)
    assert result["canonical_station_count"] == expected_station_count()
    assert result["as_of_utc"] == "2026-08-02T00:00:00.000Z"
    assert result["input_artifacts"]["p0_inbox"] == {
        "content_sha256": _p0_inbox()["content_sha256"],
        "generated_at_utc": "2026-08-02T00:00:00Z",
        "path": str(Path("/tmp/p0-inbox.json").resolve()),
    }
    assert result["content_sha256"] == content_sha256({
        key: value for key, value in result.items() if key != "content_sha256"
    })
    assert entry["minimum_sample_n"] == MIN_TAF_NATIVE_OOS_SAMPLE_N == 24
    assert entry["missing_to_24"] == 24
    assert "taf_native_oos_sample_n_insufficient" in entry["blockers"]
    assert "forward_taf_observational_not_oos" in entry["blockers"]
    assert (
        result["blocker_counts"]["exact_join_artifact_missing"]
        == expected_station_count() - 1
    )
    assert result["totals"] == {
        "qualified_scope_count_at_24": 0,
        "sample_count_total": 0,
        "max_sample_count": 0,
        "quarantined_station_day_count": 0,
    }


def test_audit_accepts_only_closed_historical_taf_as_oos() -> None:
    sample = _sample("2026-07-01")
    sample["taf"]["source_class"] = (
        "official_date_addressed_historical_replay"
    )
    captures, truths = _pointers(["2026-07-01"])
    captures[0] = {
        key: value
        for key, value in captures[0].items()
        if key not in {"kind", "revision_id", "revision_kind"}
    }
    captures[0]["kind"] = "HISTORICAL"
    captures[0]["source_class"] = (
        "official_date_addressed_historical_replay"
    )
    result = audit_taf_p0m_exact_join_45station(
        station_registry=_registry(),
        p0_inbox=_p0_inbox(),
        exact_joins=[(Path("T000.json"), _exact_join([sample]))],
        taf_capture_pointers=captures,
        truth_pointers=truths,
    )
    entry = _entry(result)
    assert entry["exact_join"]["sample_count"] == 1
    assert entry["exact_join"]["non_oos_observational_event_count"] == 0


def test_future_taf_event_is_observational_and_cannot_count_toward_taf24() -> None:
    sample = _sample("2026-08-03")
    captures, truths = _pointers(["2026-08-03"])
    result = audit_taf_p0m_exact_join_45station(
        station_registry=_registry(),
        p0_inbox=_p0_inbox(),
        exact_joins=[(Path("T000.json"), _exact_join([sample]))],
        taf_capture_pointers=captures,
        truth_pointers=truths,
    )

    exact = _entry(result)["exact_join"]
    assert exact["sample_count"] == 0
    assert exact["non_oos_observational_event_dates"] == ["2026-08-03"]
    assert result["totals"]["sample_count_total"] == 0


def test_audit_keeps_amd_cor_out_of_samples_and_records_quarantine_pointer() -> None:
    sample = _sample("2026-07-01")
    correction = {
        "kind": "FORWARD",
        "content_sha256": "b" * 64,
        "station_id": "T000",
        "event_date": "2026-07-02",
        "settlement_timezone": "Etc/UTC",
        "revision_id": "taf-amendment-1",
        "revision_kind": "AMD",
        "provider_available_at_utc": "2026-07-01T03:00:00Z",
        "source_object_sha256": "e" * 64,
        "source_class": "official_forward_native_receipt",
    }
    captures, truths = _pointers(["2026-07-01"])
    result = audit_taf_p0m_exact_join_45station(
        station_registry=_registry(),
        p0_inbox=_p0_inbox(),
        exact_joins=[(Path("T000.json"), _exact_join([sample], excluded=[{
            "station_id": "T000",
            "event_date": "2026-07-02",
            "reason": "forward_taf_correction_requires_manual_review",
            "corrections": [{"content_sha256": "b" * 64}],
        }]))],
        taf_capture_pointers=[*captures, correction],
        truth_pointers=truths,
    )

    entry = _entry(result)
    assert entry["excluded_amd_cor_station_day_count"] == 1
    assert entry["exact_join"]["excluded_correction_pointers"] == [{
        "event_date": "2026-07-02",
        "correction_content_sha256": ["b" * 64],
    }]
    assert result["totals"]["quarantined_station_day_count"] == 1
    assert "amd_cor_station_day_not_quarantined" not in entry["blockers"]
    assert "amd_cor_quarantine_pointer_mismatch" not in entry["blockers"]


def test_audit_blocks_an_amd_cor_capture_used_as_a_taf_feature() -> None:
    sample = _sample("2026-07-01", capture="b" * 64)
    correction = {
        "kind": "FORWARD",
        "content_sha256": "b" * 64,
        "station_id": "T000",
        "event_date": "2026-07-01",
        "settlement_timezone": "Etc/UTC",
        "revision_id": "taf-correction-1",
        "revision_kind": "COR",
        "provider_available_at_utc": "2026-07-01T03:00:00Z",
        "source_object_sha256": "e" * 64,
        "source_class": "official_forward_native_receipt",
    }
    _captures, truths = _pointers(["2026-07-01"])
    result = audit_taf_p0m_exact_join_45station(
        station_registry=_registry(),
        p0_inbox=_p0_inbox(),
        exact_joins=[(Path("T000.json"), _exact_join([sample]))],
        taf_capture_pointers=[correction],
        truth_pointers=truths,
    )

    entry = _entry(result)
    assert "amd_cor_used_as_taf_feature" in entry["blockers"]
    assert "forward_taf_observational_not_oos" in entry["blockers"]
    assert "amd_cor_station_day_not_quarantined" in entry["blockers"]
    assert entry["exact_join"]["unquarantined_correction_pointers"] == [{
        "event_date": "2026-07-01",
        "correction_content_sha256": ["b" * 64],
    }]


def test_audit_blocks_legacy_join_without_amd_cor_quarantine_evidence() -> None:
    sample = _sample("2026-07-01")
    legacy = _exact_join([sample])
    legacy.pop("excluded_correction_samples")
    legacy.pop("excluded_correction_sample_count")
    legacy["content_sha256"] = content_sha256({
        key: value for key, value in legacy.items() if key != "content_sha256"
    })
    captures, truths = _pointers(["2026-07-01"])

    result = audit_taf_p0m_exact_join_45station(
        station_registry=_registry(),
        p0_inbox=_p0_inbox(),
        exact_joins=[(Path("T000.json"), legacy)],
        taf_capture_pointers=captures,
        truth_pointers=truths,
    )

    assert "amd_cor_quarantine_evidence_missing" in _entry(result)["blockers"]
