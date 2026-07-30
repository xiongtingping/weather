from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from scripts import bayes_basket_target_post_et_refresh_scheduler as scheduler


def test_scheduler_waits_outside_post_et_window_without_network(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "_run", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network_not_allowed")))
    result = scheduler.run(
        now_utc=datetime(2026, 7, 26, 0, 25, tzinfo=timezone.utc),
        lane_map=tmp_path / "lane.json",
        state_path=tmp_path / "state.json",
    )
    assert result["state"] == "NOOP_OUTSIDE_POST_ET_WINDOW"
    assert result["refresh_invoked"] is False


def test_scheduler_runs_final_awc_backfill_then_refresh_once(tmp_path: Path, monkeypatch) -> None:
    lane = tmp_path / "lane.json"
    lane.write_text(json.dumps({"stations": {"KAAA": "awc", "ZUUU": "amsc"}}), encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(command, *, timeout_sec=900):
        calls.append(command)
        if command[1].endswith("rebuild_bayes_basket_target_historical_truth.py"):
            output = Path(command[command.index("--output") + 1])
            observations = Path(command[command.index("--observations-output") + 1])
            reference = Path(command[command.index("--reference-output") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps({
                    "state": "CLOSED_HISTORICAL",
                    "pending_event_dates": [],
                    "blockers": [],
                    "station_day_count": 1,
                    "observation_count": 1,
                    "source_counts": {"awc": 1},
                }),
                encoding="utf-8",
            )
            observations.write_text("{}", encoding="utf-8")
            reference.write_text("{}", encoding="utf-8")
            return {"returncode": 0, "timed_out": False, "stdout": "{}", "stderr": ""}
        return {"returncode": 0, "timed_out": False, "stdout": "{}", "stderr": ""}

    monkeypatch.setattr(scheduler, "_run", fake_run)
    monkeypatch.setattr(scheduler, "DEFAULT_HISTORICAL_CANONICAL", tmp_path / "historical.json")
    monkeypatch.setattr(scheduler, "DEFAULT_REFERENCE", tmp_path / "reference.json")
    (tmp_path / "historical.json").write_text("{}", encoding="utf-8")
    (tmp_path / "reference.json").write_text("{}", encoding="utf-8")

    result = scheduler.run(
        now_utc=datetime(2026, 7, 26, 4, 5, tzinfo=timezone.utc),
        lane_map=lane,
        proxy_config=tmp_path / "proxy.json",
        raw_root=tmp_path / "raw",
        output_root=tmp_path / "out",
        state_path=tmp_path / "state.json",
        gate_receipt=tmp_path / "gate.json",
        historical_canonical=tmp_path / "historical.json",
        reference_evidence=tmp_path / "reference.json",
    )

    assert result["state"] == "REFRESHED"
    assert len(calls) == 2
    assert calls[0][calls[0].index("--event-date") + 1] == "2026-07-25"
    assert calls[0].count("--station-id") == 1
    assert calls[0][calls[0].index("--station-id") + 1] == "KAAA"
    assert "--historical-canonical-evidence" in calls[1]
    assert "--historical-reference-evidence" in calls[1]

    second = scheduler.run(
        now_utc=datetime(2026, 7, 26, 4, 10, tzinfo=timezone.utc),
        lane_map=lane,
        output_root=tmp_path / "out",
        state_path=tmp_path / "state.json",
        gate_receipt=tmp_path / "gate.json",
        historical_canonical=tmp_path / "historical.json",
        reference_evidence=tmp_path / "reference.json",
    )
    assert second["state"] == "ALREADY_PROCESSED"
    assert len(calls) == 2
