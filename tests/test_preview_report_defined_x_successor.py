from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3

from scripts import preview_report_defined_x_successor as preview


def _event(event_id: str, report_type: str, temperature_c: float) -> dict:
    return {
        "canonical_observation_event_id": event_id,
        "report_type": report_type,
        "valid_time": "2026-07-15T00:30:00Z",
        "parsed_observation": {"temperature_c": temperature_c},
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_preview_keeps_speci_soft_projects_integer_x_and_is_read_only(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "archive.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
        conn.execute("INSERT INTO sentinel(value) VALUES ('unchanged')")
    before = _sha256(db)
    events = [
        _event("metar-31", "METAR", 31.0),
        _event("speci-34", "SPECI", 34.0),
    ]
    joint = [
        {"temperature_c": 30.75, "state": "terminated", "probability": 0.7},
        {"temperature_c": 31.75, "state": "warming", "probability": 0.3},
    ]
    checkpoint = {
        "joint_posterior": joint,
        "source_reliability_state": {},
        "observed_max_lower_bound_c": 30.5,
    }

    monkeypatch.setattr(preview, "load_production_probability_activation", lambda: {})
    monkeypatch.setattr(
        preview,
        "build_production_p0_inputs",
        lambda **_: {
            "ok": True,
            "target_identity": "physical_weather_day_v1:ZUUU:2026-07-15",
            "runtime_artifacts": {"weak_evidence_snapshot_id": "weak-1"},
            "prior_context": {"prior_context_id": "prior-1"},
            "strong_lr_artifact": {},
            "resolution_contract": {"resolution_contract_id": "resolution-1"},
        },
    )
    monkeypatch.setattr(
        preview,
        "compile_production_p0_candidate",
        lambda **_: {"ok": True, "candidate": {"checkpoint": checkpoint}},
    )
    monkeypatch.setattr(preview, "_load_events", lambda *_args, **_kwargs: events)

    def replay(**kwargs):
        # The isolated replay must install the SPECI-soft lower-bound policy.
        assert preview.filter_runtime._event_lower_bound(events[1]) is None
        assert preview.filter_runtime._event_lower_bound(events[0]) == 30.5
        return {
            "ok": True,
            "updates": [
                {"candidate_post_joint": joint},
                {"candidate_post_joint": joint},
            ],
            "checkpoint": checkpoint,
        }

    monkeypatch.setattr(preview.filter_runtime, "replay_canonical_sequence", replay)
    original_lower_bound = preview.filter_runtime._event_lower_bound

    result = preview.build_preview(
        db=db,
        event_date="2026-07-15",
        decision_as_of="2026-07-15T01:00:00Z",
    )

    assert result["ok"] is True
    assert result["reported_x_lower_bound_c"] == 31
    assert result["timeline"][1]["speci_changed_reported_x_bound"] is False
    assert set(result["reported_x_distribution"]) == {"31", "32"}
    assert all(float(value).is_integer() for value in result["reported_x_distribution"])
    assert min(map(int, result["reported_x_distribution"])) == 31
    assert result["reported_x_termination"]["updates_posterior"] is False
    assert result["read_only"] is True
    assert result["writes_archive_db"] is False
    assert result["places_orders"] is False
    assert _sha256(db) == before
    assert preview.filter_runtime._event_lower_bound is original_lower_bound
