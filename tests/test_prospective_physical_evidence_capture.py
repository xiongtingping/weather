from __future__ import annotations

import json
from pathlib import Path
import subprocess

import prospective_physical_evidence_capture as capture_module
from prospective_physical_evidence_capture import (
    ProspectivePhysicalEvidenceCaptureWorker,
    _completed_provider_runs,
    _provider_rotation,
    capture_amsc_taf_amd,
    capture_official_nwp_tier_a,
    capture_official_nwp_tier_a_subprocess,
    load_capture_config,
)


def _config(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema": "identifiable_physical_evidence_capture_config_v1",
                "state": "enabled",
                "taf_amd": {
                    "enabled": True,
                    "success_interval_sec": 1800,
                    "failure_retry_sec": 300,
                    "count": 12,
                    "timeout_sec": 15,
                },
                "official_nwp": {
                    "enabled": True,
                    "success_interval_sec": 21600,
                    "failure_retry_sec": 900,
                    "target_day_offset": 1,
                    "providers": ["ecmwf", "icon", "gfs"],
                    "max_provider_runs_per_cycle": 3,
                    "runtime_python": "/approved/nwp/python",
                    "cycle_timeout_sec": 600,
                },
                "boundary": {
                    "hard_no_lane_reads_this_capture": False,
                    "writes_posterior": False,
                    "writes_ssot": False,
                    "writes_decision_or_intent": False,
                    "places_orders": False,
                    "adds_fourth_service": False,
                    "nwp_child_is_ephemeral": True,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_config_rejects_any_order_or_hard_no_boundary(tmp_path: Path) -> None:
    path = _config(tmp_path / "capture.json")
    assert load_capture_config(path)["state"] == "enabled"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["boundary"]["places_orders"] = True
    path.write_text(json.dumps(value), encoding="utf-8")

    try:
        load_capture_config(path)
    except ValueError as exc:
        assert str(exc) == "physical_evidence_capture_boundary_invalid"
    else:
        raise AssertionError("unsafe evidence boundary must fail closed")


def test_worker_is_activation_independent_and_cadence_bounded(tmp_path: Path) -> None:
    calls: list[tuple[str, dict]] = []

    def taf_capture(**kwargs):
        calls.append(("taf", kwargs))
        return {"ok": True, "state": "captured", "writes_posterior": False}

    def nwp_capture(**kwargs):
        calls.append(("nwp", kwargs))
        return {"ok": True, "state": "captured", "writes_posterior": False}

    worker = ProspectivePhysicalEvidenceCaptureWorker(
        tmp_path / "unused.db",
        config_path=_config(tmp_path / "capture.json"),
        taf_archive_dir=tmp_path / "taf",
        nwp_root=tmp_path / "nwp",
        nwp_status_path=tmp_path / "status.json",
        taf_capture=taf_capture,
        nwp_capture=nwp_capture,
    )

    first = worker.run()
    second = worker.run()

    assert [name for name, _ in calls] == ["taf", "nwp"]
    assert calls[1][1]["target_day_offset"] == 1
    assert calls[1][1]["providers"] == ["ecmwf", "icon", "gfs"]
    assert calls[1][1]["runtime_python"] == Path("/approved/nwp/python")
    assert calls[1][1]["cycle_timeout_sec"] == 600
    assert first["activation_independent"] is True
    assert first["hard_no_lane_consumes_capture"] is False
    assert first["writes_posterior"] is False
    assert first["writes_ssot"] is False
    assert first["places_orders"] is False
    assert second["state"] == "not_due"
    assert second["last_capture"]["taf_amd"]["state"] == "captured"
    assert second["last_capture"]["official_nwp"]["state"] == "captured"


def test_taf_curl_fallback_is_not_promoted_to_tier_a(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(capture_module, "token_candidates", lambda: ["token"])
    monkeypatch.setattr(
        capture_module,
        "fetch_reports_with_transport",
        lambda *_: (
            {"code": 200, "data": [{"content": "TAF ZUUU 190000Z 1900/1924 TX35/1907Z="}]},
            {
                "response_started_at": "",
                "response_headers_observed": False,
                "transport_mode": "curl_response_start_unobserved",
            },
        ),
    )

    result = capture_amsc_taf_amd(
        db_path=tmp_path / "must-not-exist.db",
        archive_dir=tmp_path / "must-not-exist",
        count=12,
        timeout_sec=15,
    )

    assert result["state"] == "blocked_non_tier_a_transport"
    assert result["writes_posterior"] is False
    assert result["places_orders"] is False
    assert not (tmp_path / "must-not-exist.db").exists()
    assert not (tmp_path / "must-not-exist").exists()


def test_completed_run_cache_is_scoped_to_target_event_date(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    path.write_text(
        json.dumps(
            {
                "target_event_date": "2026-07-20",
                "completed_provider_runs": {"gfs": "2026-07-19T06:00:00Z"},
            }
        ),
        encoding="utf-8",
    )

    assert _completed_provider_runs(
        path, expected_target_event_date="2026-07-20"
    ) == {"gfs": "2026-07-19T06:00:00Z"}
    assert _completed_provider_runs(
        path, expected_target_event_date="2026-07-21"
    ) == {}


def test_provider_rotation_advances_even_after_a_failed_cycle(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    first, next_index = _provider_rotation(
        path, providers=["ecmwf", "icon", "gfs"], cycle_size=1
    )
    path.write_text(
        json.dumps(
            {
                "ok": False,
                "state": "partial_or_blocked",
                "next_provider_index": next_index,
            }
        ),
        encoding="utf-8",
    )
    second, following = _provider_rotation(
        path, providers=["ecmwf", "icon", "gfs"], cycle_size=1
    )

    assert first == ["ecmwf"]
    assert second == ["icon"]
    assert following == 2


def test_direct_nwp_path_returns_materialization_and_persists_rotation(
    tmp_path: Path, monkeypatch
) -> None:
    seen = {}

    def fake_monitor(**kwargs):
        seen.update(kwargs)
        return {
            "ok": True,
            "state": "executed",
            "target_event_date": "2026-07-20",
            "completed_provider_runs": {"ecmwf": "2026-07-19T00:00:00Z"},
            "provider_results": [
                {
                    "provider_id": "ecmwf",
                    "ok": True,
                    "state": "collected",
                    "run_at_utc": "2026-07-19T00:00:00Z",
                }
            ],
        }

    monkeypatch.setattr(
        capture_module, "build_official_nwp_daily_curve_monitor_job", fake_monitor
    )
    monkeypatch.setattr(
        capture_module,
        "materialize_official_nwp_daily_peak_p1",
        lambda **_: {"ok": True, "state": "applied", "outcomes": []},
    )
    status = tmp_path / "status.json"
    result = capture_official_nwp_tier_a(
        db_path=tmp_path / "archive.db",
        curve_source_root=tmp_path / "curves",
        status_path=status,
        providers=["ecmwf", "icon", "gfs"],
        max_provider_runs=1,
        target_day_offset=1,
    )

    assert result["ok"] is True
    assert result["state"] == "official_nwp_tier_a_captured"
    assert result["selected_providers"] == ["ecmwf"]
    assert result["next_provider_index"] == 1
    assert result["p1_materialization"]["state"] == "applied"
    assert seen["job_contract"]["providers"] == ["ecmwf"]
    persisted = json.loads(status.read_text(encoding="utf-8"))
    assert persisted["next_provider_index"] == 1
    assert persisted["provider_rotation"]["advance_on_failure"] is True


def test_nwp_collectors_run_in_bounded_approved_ephemeral_runtime(
    tmp_path: Path, monkeypatch
) -> None:
    base_runtime = tmp_path / "base-python"
    base_runtime.write_text("", encoding="utf-8")
    base_runtime.chmod(0o755)
    runtime = tmp_path / "venv-python"
    runtime.symlink_to(base_runtime)
    calls = {}

    class FakeProcess:
        pid = 123
        returncode = 0

        def communicate(self, timeout=None):
            calls["timeout"] = timeout
            return (
                json.dumps(
                    {
                        "schema": "prospective_physical_evidence_capture_v1",
                        "ok": True,
                        "state": "official_nwp_tier_a_captured",
                        "writes_posterior": False,
                        "places_orders": False,
                    }
                ),
                "",
            )

    def fake_popen(command, **kwargs):
        calls["command"] = command
        calls["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(capture_module.subprocess, "Popen", fake_popen)
    result = capture_official_nwp_tier_a_subprocess(
        db_path=tmp_path / "archive.db",
        curve_source_root=tmp_path / "curves",
        status_path=tmp_path / "status.json",
        providers=["ecmwf", "icon", "gfs"],
        max_provider_runs=1,
        target_day_offset=1,
        runtime_python=runtime,
        cycle_timeout_sec=600,
    )

    assert calls["command"][0] == str(runtime.absolute())
    assert calls["command"][0] != str(runtime.resolve())
    assert "--official-nwp-once" in calls["command"]
    assert calls["kwargs"]["start_new_session"] is True
    assert calls["timeout"] == 600
    assert result["ok"] is True
    assert result["ephemeral_child_process"] is True
    assert result["adds_fourth_service"] is False
    assert result["writes_posterior"] is False
    assert result["places_orders"] is False


def test_nwp_ephemeral_runtime_timeout_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "python"
    runtime.write_text("", encoding="utf-8")
    runtime.chmod(0o755)

    class TimeoutProcess:
        pid = 456
        returncode = None

        def __init__(self):
            self.calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("nwp", timeout)
            return ("", "")

    killed = []
    monkeypatch.setattr(
        capture_module.subprocess, "Popen", lambda *_, **__: TimeoutProcess()
    )
    monkeypatch.setattr(
        capture_module.os, "killpg", lambda pid, sig: killed.append((pid, sig))
    )
    result = capture_official_nwp_tier_a_subprocess(
        db_path=tmp_path / "archive.db",
        curve_source_root=tmp_path / "curves",
        status_path=tmp_path / "status.json",
        providers=["ecmwf", "icon", "gfs"],
        max_provider_runs=1,
        target_day_offset=1,
        runtime_python=runtime,
        cycle_timeout_sec=60,
    )

    assert result["state"] == "blocked_official_nwp_cycle_timeout"
    assert result["child_process_group_terminated"] is True
    assert killed == [(456, capture_module.signal.SIGTERM)]
    assert result["adds_fourth_service"] is False
    assert result["writes_posterior"] is False
    assert result["places_orders"] is False
    failure_status = json.loads((tmp_path / "status.json").read_text())
    assert failure_status["next_provider_index"] == 1
    assert failure_status["provider_rotation"]["selected_providers"] == ["ecmwf"]
