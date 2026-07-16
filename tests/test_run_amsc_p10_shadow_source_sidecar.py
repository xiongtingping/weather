from __future__ import annotations

import json
import ssl
import urllib.error
from pathlib import Path

from scripts import run_amsc_p10_shadow_source_sidecar as runner


ROOT = Path(__file__).resolve().parents[1]


def test_runner_noops_without_token_lookup_on_unscheduled_overnight_half_slot(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(runner, "_utc_now_iso", lambda: "2026-07-10T16:30:01Z")

    def token_lookup_must_not_run():
        raise AssertionError("token lookup must not run on an unscheduled slot")

    monkeypatch.setattr(runner, "token_candidates", token_lookup_must_not_run)
    status_path = tmp_path / "status.json"
    rc = runner.main(["--status-path", str(status_path)])

    result = json.loads(capsys.readouterr().out)
    persisted = json.loads(status_path.read_text(encoding="utf-8"))
    assert rc == 0
    assert result["state"] == "skipped_unscheduled_regular_metar_slot"
    assert result["raw_archive_dir"].endswith("data/shadow/p10_amsc_raw")
    assert result["schedule"]["scheduled_regular_metar_slot"] is False
    assert persisted == result


def test_runner_records_sanitized_tls_failure_type_without_leaking_token(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(runner, "_utc_now_iso", lambda: "2026-07-10T17:00:01Z")
    monkeypatch.setattr(runner, "token_candidates", lambda: ["secret-token-must-not-appear"])
    monkeypatch.setattr(
        runner,
        "fetch_amsc_with_observed_transport",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate_verify_failed"))
        ),
    )
    status_path = tmp_path / "status.json"
    rc = runner.main(["--status-path", str(status_path), "--attempts", "1"])

    result = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert result["state"] == "source_fetch_failed"
    assert "URLError:SSLCertVerificationError" in result["blockers"][0]
    assert "secret-token-must-not-appear" not in json.dumps(result)


def test_monitor_owns_p10_source_capture_without_a_parallel_launchagent() -> None:
    """P10 keeps an independent raw root but runs under the sole monitor daemon."""
    monitor = (ROOT / "scripts" / "polymarket_monitor_daemon.sh").read_text(encoding="utf-8")

    assert "start_amsc_p10_shadow_source_capture()" in monitor
    assert "run_amsc_p10_shadow_source_sidecar.py" in monitor
    assert "AMSC_P10_SHADOW_SOURCE_PID" in monitor
    assert not (ROOT / "launchd" / "com.weather.amsc_p10_shadow_source_sidecar.plist").exists()
    assert not (ROOT / "scripts" / "install_amsc_p10_shadow_source_launchagent.sh").exists()
