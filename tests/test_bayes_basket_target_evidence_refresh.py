from __future__ import annotations

import scripts.refresh_bayes_basket_target_evidence as refresh


def test_evidence_refresh_uses_one_clock_and_never_claims_mutation(monkeypatch, tmp_path) -> None:
    clock = "2026-07-23T22:00:52Z"
    calls: list[tuple[str, str, bool | None, object, object]] = []

    monkeypatch.setattr(refresh, "_timezone_map", lambda _path: {"ZUUU": "Asia/Shanghai"})
    monkeypatch.setattr(refresh, "_lane_map", lambda _path: {"ZUUU": "amsc"})

    def canonical(**kwargs):
        calls.append(("canonical", kwargs["as_of_utc"], None, None, None))
        return {
            "state": "EXPORTED",
            "observation_count": 1,
            "accepted_truth_count": 1,
            "quarantined_truth_count": 0,
            "receipt_sha256": "a" * 64,
        }

    def et_history(**kwargs):
        calls.append(("et_history", kwargs["as_of_utc"], kwargs["history_only"], kwargs["historical_rebuild_paths"], kwargs["station_ids"]))
        return {
            "schema": "bayes_basket_target_et_history_evidence_v1",
            "state": "READY_FOR_STRICT_ASOF",
            "receipt_sha256": "b" * 64,
            "input_counts": {"joined_station_days": 1},
        }

    def calibration(**kwargs):
        calls.append(("calibration", kwargs["as_of_utc"], None, kwargs.get("model_arrival_evidence_path"), kwargs["station_ids"]))
        return {"calibration_artifacts": 1, "ensemble_artifacts": 1, "promotion_allowed": False}

    monkeypatch.setattr(refresh, "export_canonical_observation_evidence", canonical)
    monkeypatch.setattr(refresh, "build_et_history", et_history)
    monkeypatch.setattr(refresh, "build_calibration", calibration)
    monkeypatch.setattr(refresh, "audit", lambda **_kwargs: {"state": "blocked", "blockers": ["x"]})
    monkeypatch.setattr(refresh, "build_manifest", lambda **_kwargs: {
        "release_state": "BLOCKED_NOT_RELEASE",
        "manifest_sha256": "c" * 64,
    })

    written: dict[str, object] = {}

    def write(path, payload):
        written[str(path)] = payload

    monkeypatch.setattr(refresh, "write_bounded_json", write)

    result = refresh.run(
        as_of_utc=clock,
        source_db=tmp_path / "archive.db",
        timezone_registry=tmp_path / "registry.json",
        lane_map=tmp_path / "lane.json",
        native_decode=tmp_path / "native.json",
        station_ids={"zuuu"},
        canonical_output=tmp_path / "canonical.json",
        et_history_output=tmp_path / "history.json",
        calibration_output=tmp_path / "calibration.json",
        readiness_output=tmp_path / "readiness.json",
        release_output=tmp_path / "release.json",
        receipt_output=tmp_path / "refresh.json",
    )

    assert calls == [
        ("canonical", clock, None, None, None),
        ("et_history", clock, True, [], {"ZUUU"}),
        ("calibration", clock, None, None, {"ZUUU"}),
    ]
    assert result["state"] == "READY_FOR_STRICT_ASOF"
    assert result["as_of_utc"] == clock
    receipt = next(value for key, value in written.items() if key.endswith("refresh.json"))
    assert receipt["boundary"]["writes_shared_live_database"] is False
    assert receipt["boundary"]["writes_target_database"] is False
    assert receipt["boundary"]["calls_order_gateway"] is False
    assert receipt["boundary"]["restarts_services"] is False
    assert receipt["scope"]["excluded_source_lanes"] == ["awc"]


def test_refresh_scope_excludes_only_unowned_source_lanes() -> None:
    lane_map = {"ZBAA": "amsc", "KAAA": "awc"}
    assert refresh._excluded_source_lanes({"ZBAA"}, lane_map) == ["awc"]
    assert refresh._excluded_source_lanes({"KAAA"}, lane_map) == ["amsc"]
    assert refresh._excluded_source_lanes({"ZBAA", "KAAA"}, lane_map) == []


def test_evidence_refresh_binds_arrival_and_historical_inputs(monkeypatch, tmp_path) -> None:
    clock = "2026-07-25T04:00:00Z"
    arrival_path = tmp_path / "arrival.json"
    arrival_path.write_text('{"schema":"bayes_basket_target_model_arrival_evidence_v1","as_of_utc":"' + clock + '","arrivals":[]}', encoding="utf-8")
    historical_path = tmp_path / "historical.json"
    historical_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(refresh, "_timezone_map", lambda _path: {"ZUUU": "Asia/Shanghai"})
    monkeypatch.setattr(refresh, "_lane_map", lambda _path: {"ZUUU": "amsc"})
    monkeypatch.setattr(refresh, "export_canonical_observation_evidence", lambda **_kwargs: {"state": "EXPORTED"})
    et_calls: list[object] = []
    calibration_calls: list[object] = []
    monkeypatch.setattr(refresh, "build_et_history", lambda **kwargs: et_calls.append(kwargs) or {
        "schema": "bayes_basket_target_et_history_evidence_v1",
        "state": "READY_FOR_STRICT_ASOF",
        "receipt_sha256": "b" * 64,
        "input_counts": {"joined_station_days": 1},
    })
    monkeypatch.setattr(refresh, "build_calibration", lambda **kwargs: calibration_calls.append(kwargs) or {
        "calibration_artifacts": 0,
        "ensemble_artifacts": 0,
        "promotion_allowed": False,
    })
    monkeypatch.setattr(refresh, "audit", lambda **_kwargs: {"state": "blocked", "blockers": []})
    monkeypatch.setattr(refresh, "build_manifest", lambda **_kwargs: {"release_state": "BLOCKED_NOT_RELEASE", "manifest_sha256": "c" * 64})
    monkeypatch.setattr(refresh, "write_bounded_json", lambda *_args: None)

    refresh.run(
        as_of_utc=clock,
        source_db=tmp_path / "archive.db",
        timezone_registry=tmp_path / "registry.json",
        lane_map=tmp_path / "lane.json",
        native_decode=tmp_path / "native.json",
        model_arrival_evidence=arrival_path,
        historical_rebuild_paths=[historical_path],
        canonical_output=tmp_path / "canonical.json",
        et_history_output=tmp_path / "history.json",
        calibration_output=tmp_path / "calibration.json",
        readiness_output=tmp_path / "readiness.json",
        release_output=tmp_path / "release.json",
        receipt_output=tmp_path / "refresh.json",
    )

    assert et_calls[0]["historical_rebuild_paths"] == [historical_path]
    assert calibration_calls[0]["model_arrival_evidence_path"] == arrival_path.resolve()


def test_awc_scoped_refresh_filters_source_lane_and_uses_isolated_ws_manifest(monkeypatch, tmp_path) -> None:
    clock = "2026-07-26T04:00:00Z"
    canonical_calls: list[dict[str, object]] = []
    readiness_calls: list[dict[str, object]] = []

    monkeypatch.setattr(refresh, "_timezone_map", lambda _path: {"KAAA": "America/New_York"})
    monkeypatch.setattr(refresh, "_lane_map", lambda _path: {"KAAA": "awc"})

    def canonical(**kwargs):
        canonical_calls.append(kwargs)
        return {"state": "EXPORTED", "observation_count": 1, "accepted_truth_count": 1, "quarantined_truth_count": 0, "receipt_sha256": "a" * 64}

    monkeypatch.setattr(refresh, "export_canonical_observation_evidence", canonical)
    monkeypatch.setattr(refresh, "build_et_history", lambda **_kwargs: {
        "schema": "bayes_basket_target_et_history_evidence_v1",
        "state": "READY_FOR_STRICT_ASOF",
        "receipt_sha256": "b" * 64,
        "input_counts": {"joined_station_days": 1},
    })
    monkeypatch.setattr(refresh, "build_calibration", lambda **_kwargs: {"calibration_artifacts": 0, "ensemble_artifacts": 0, "promotion_allowed": False})

    def readiness(**kwargs):
        readiness_calls.append(kwargs)
        return {"state": "blocked", "blockers": ["official_ws_manifest_missing"]}

    monkeypatch.setattr(refresh, "audit", readiness)
    monkeypatch.setattr(refresh, "build_manifest", lambda **_kwargs: {"release_state": "BLOCKED_NOT_RELEASE", "manifest_sha256": "c" * 64})
    monkeypatch.setattr(refresh, "write_bounded_json", lambda *_args: None)

    refresh.run(
        as_of_utc=clock,
        source_db=tmp_path / "archive.db",
        timezone_registry=tmp_path / "registry.json",
        lane_map=tmp_path / "lane.json",
        native_decode=tmp_path / "native.json",
        station_ids={"KAAA"},
        canonical_output=tmp_path / "canonical.json",
        et_history_output=tmp_path / "history.json",
        calibration_output=tmp_path / "calibration.json",
        readiness_output=tmp_path / "readiness.json",
        release_output=tmp_path / "release.json",
        receipt_output=tmp_path / "refresh.json",
    )

    assert canonical_calls[0]["station_ids"] == ["KAAA"]
    assert canonical_calls[0]["source_lanes"] == ["awc"]
    assert readiness_calls[0]["station_ids"] == {"KAAA"}
    assert readiness_calls[0]["manifest_path"] == refresh.AWC_ONLY_TARGET_WS_MANIFEST
