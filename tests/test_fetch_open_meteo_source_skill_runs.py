from scripts.fetch_open_meteo_source_skill_runs import CONFIRMATION, execute_request_plan
from urllib.error import HTTPError
from io import BytesIO


def test_fetcher_is_no_network_no_write_by_default(tmp_path):
    called = []
    report = execute_request_plan(
        [{"source_id": "gfs_model_peak", "provider_family": "nwp_gfs", "url": "https://example.test/data"}],
        output_root=tmp_path,
        fetcher=lambda *_args: called.append(True),
    )
    assert report["authorized"] is False
    assert called == []
    assert report["guards"]["downloads_historical_forecasts"] is False


def test_fetch_requires_explicit_research_write_gate(tmp_path):
    report = execute_request_plan(
        [{"source_id": "gfs_model_peak", "provider_family": "nwp_gfs", "url": "https://example.test/data"}],
        fetch=True,
        confirmation=CONFIRMATION,
        allow_research_write=False,
        output_root=tmp_path,
        fetcher=lambda *_args: (200, "{}"),
    )
    assert report["authorized"] is False


def test_confirmation_phrase_is_not_required_after_workspace_rule_removal(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "scripts.fetch_open_meteo_source_skill_runs.write_forward_snapshot",
        lambda _envelope, **_kwargs: {"ok": True, "state": "written"},
    )
    report = execute_request_plan(
        [{"source_id": "gfs_model_peak", "provider_family": "nwp_gfs", "url": "https://example.test/data"}],
        fetch=True,
        allow_research_write=True,
        output_root=tmp_path,
        fetcher=lambda *_args: (200, "{}"),
        min_interval_seconds=0.0,
    )

    assert report["authorized"] is True
    assert report["confirmation"]["confirmation_phrase_required"] is False


def test_request_bound_blocks_accidental_broad_fetch(tmp_path):
    report = execute_request_plan(
        [{"source_id": "gfs_model_peak", "url": "https://example.test/data"}] * 2,
        fetch=True,
        confirmation=CONFIRMATION,
        allow_research_write=True,
        max_requests=1,
        output_root=tmp_path,
        fetcher=lambda *_args: (200, "{}"),
    )
    assert report["authorized"] is False
    assert "request_count_exceeds_explicit_bound" in report["issues"]


def test_authorized_fetch_retries_transient_failure_before_immutable_write(monkeypatch, tmp_path):
    calls = []

    def fetcher(_url, _timeout):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("temporary")
        return 200, "{}"

    monkeypatch.setattr(
        "scripts.fetch_open_meteo_source_skill_runs.write_forward_snapshot",
        lambda envelope, **_kwargs: {"ok": True, "state": "written", "sha": envelope["response_sha256"]},
    )
    report = execute_request_plan(
        [
            {
                "source_id": "gfs_model_peak",
                "provider_family": "nwp_gfs",
                "url": "https://example.test/data",
                "provider_available_at_conservative": "2026-07-10T12:10:00Z",
                "availability_quality": "conservative",
            }
        ],
        fetch=True,
        confirmation=CONFIRMATION,
        allow_research_write=True,
        output_root=tmp_path,
        fetcher=fetcher,
        max_retries=1,
        min_interval_seconds=0.0,
        sleep_fn=lambda _seconds: None,
    )
    assert report["success_count"] == 1
    assert len(report["results"][0]["attempts"]) == 2


def test_provider_run_unavailable_is_explicit_na_and_not_retried(tmp_path):
    calls = []

    def fetcher(url, _timeout):
        calls.append(url)
        raise HTTPError(
            url,
            400,
            "Bad Request",
            hdrs=None,
            fp=BytesIO(b'{"error":true,"reason":"The requested model run is not available"}'),
        )

    report = execute_request_plan(
        [{"source_id": "ecmwf_model_peak", "url": "https://example.test/missing"}],
        fetch=True,
        allow_research_write=True,
        max_retries=3,
        min_interval_seconds=0.0,
        output_root=tmp_path,
        fetcher=fetcher,
        sleep_fn=lambda _seconds: None,
    )

    assert len(calls) == 1
    assert report["results"][0]["state_reason"] == "provider_run_unavailable"
    assert report["results"][0]["attempts"][0]["provider_run_unavailable"] is True
