from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import scripts.market_dependency_refresh as refresh
from scripts.market_dependency_refresh import build_dependency_plan, _stable_release_identity


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


def _signal(*slugs: str) -> dict:
    return {
        "signal_schema": "weather_market_dependency_signal_v1",
        "trigger": "new_market",
        "window": {"start": "2026-07-22", "end": "2026-07-24"},
        "markets": [
            {
                "slug": slug,
                "event_date": "2026-07-22",
                "market_type": "temperature",
                "required_models": ["gfs_seamless", "ecmwf_ifs025"],
                "required_sources": ["amsc", "awc", "gfs", "ecmwf"],
            }
            for slug in slugs
        ],
    }


def test_new_market_plan_is_idempotent_after_success(tmp_path: Path) -> None:
    catalog = tmp_path / "config" / "forecast_sources" / "official_nwp_zuuu_v1.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text('{"catalog":"v1"}\n', encoding="utf-8")
    first = build_dependency_plan(signal=_signal("market-a"), state={}, root=tmp_path, now=NOW)
    assert first["state"] == "planned"
    assert first["target_day_offsets"] == [1, 3]
    state = {"last_success_at": "2026-07-21T12:01:00Z", "market_generation": first["market_generation"], "source_generation": first["source_generation"]}
    repeated = build_dependency_plan(signal=_signal("market-a"), state=state, root=tmp_path, now=NOW)
    assert repeated["state"] == "noop"
    assert repeated["actions"] == []
    changed = build_dependency_plan(signal=_signal("market-a", "market-b"), state=state, root=tmp_path, now=NOW)
    assert changed["state"] == "planned"
    assert "model_scan" not in changed["actions"]
    assert changed["actions"] == ["model_release_probe"]


def test_model_release_generation_ignores_probe_clocks() -> None:
    base = {
        "event_date": "2026-07-22",
        "generated_at": "2026-07-21T12:00:00Z",
        "models": [{"model": "gfs_seamless", "status": "complete", "latest_file_last_modified": "2026-07-21T10:00:00Z", "available_forecast_hours": [1, 2], "missing_forecast_hours": []}],
    }
    later = {**base, "generated_at": "2026-07-21T12:01:00Z", "models": [{**base["models"][0], "checked_at": "2026-07-21T12:01:00Z"}]}
    assert _stable_release_identity(base) == _stable_release_identity(later)


def test_source_generation_change_reopens_refresh(tmp_path: Path) -> None:
    catalog = tmp_path / "config" / "forecast_sources" / "official_nwp_zuuu_v1.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps({"catalog": "v1"}), encoding="utf-8")
    first = build_dependency_plan(signal=_signal("market-a"), state={}, root=tmp_path, now=NOW)
    state = {"last_success_at": "2026-07-21T12:01:00Z", "market_generation": first["market_generation"], "source_generation": first["source_generation"]}
    catalog.write_text(json.dumps({"catalog": "v2"}), encoding="utf-8")
    refreshed = build_dependency_plan(signal=_signal("market-a"), state=state, root=tmp_path, now=NOW)
    assert refreshed["source_changed"] is True
    assert refreshed["state"] == "planned"


def test_provider_failure_does_not_skip_other_sources(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    timeouts: list[float] = []

    def fake_command(*, command, env, timeout_sec, output_path):
        calls.append(list(command))
        timeouts.append(float(timeout_sec))
        provider_failed = "--provider" in command and command[command.index("--provider") + 1] == "ecmwf"
        return {"ok": not provider_failed, "returncode": 1 if provider_failed else 0, "command": list(command)}

    monkeypatch.setattr(refresh, "_command_result", fake_command)
    plan = {
        "actions": ["model_release_probe", "model_scan", "official_nwp_daily_curve"],
        "target_event_dates": ["2026-07-22"],
        "target_day_offsets": [1],
        "required_models": ["gfs_seamless", "ecmwf_ifs025"],
        "required_providers": ["ecmwf", "gfs"],
        "market_count": 1,
    }
    result = refresh.execute_dependency_plan(plan=plan, root=tmp_path, env={})
    assert result["state"] == "partial_or_blocked"
    assert timeouts
    curve_timeouts = [
        timeout
        for command, timeout in zip(calls, timeouts)
        if "run_official_nwp_daily_curve_monitor_job.py" in " ".join(command)
    ]
    assert curve_timeouts
    assert all(60.0 <= timeout <= 900.0 for timeout in curve_timeouts)
    assert any("--provider" in command and command[command.index("--provider") + 1] == "gfs" for command in calls)
    assert not any("p1" in " ".join(command).lower() for command in calls)


def test_unready_official_peak_window_skips_broad_model_scan(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(refresh, "_run_release_probes", lambda **_: [])

    def fake_command(*, command, env, timeout_sec, output_path):
        calls.append(list(command))
        return {"ok": True, "returncode": 0, "command": list(command)}

    monkeypatch.setattr(refresh, "_command_result", fake_command)
    plan = {
        "actions": ["model_release_probe", "model_scan"],
        "target_event_dates": ["2026-07-23"],
        "target_day_offsets": [1],
        "nwp_target_day_offsets": [1, 2],
        "required_models": ["gfs_seamless", "ecmwf_ifs025"],
        "required_providers": ["ecmwf", "gfs", "icon"],
        "ready_provider_day_offsets": {"1": [], "2": []},
        "market_count": 1,
    }

    result = refresh.execute_dependency_plan(plan=plan, root=tmp_path, env={})

    assert result["ok"] is True
    assert not any(command and command[-1].endswith("model_scan.sh") for command in calls)
    assert any(
        item.get("action") == "model_scan"
        and item.get("skipped") is True
        and item.get("state") == "waiting_for_official_peak_window"
        for item in result["results"]
    )


def test_target_day_release_file_wins_over_older_global_release_file() -> None:
    release = {
        "entries": [
            {
                "event_date": "2026-07-23",
                "models": [{"model": "ecmwf_ifs025", "status": "complete"}],
            },
            {
                "global": {
                    "event_date": "2026-07-23",
                    "models": [{"model": "ecmwf_ifs025", "status": "no_files"}],
                }
            },
        ]
    }
    ready = refresh._ready_provider_day_offsets(
        release=release,
        target_days=["2026-07-23"],
        today=NOW.date(),
    )
    assert ready["2"] == ["ecmwf"]
