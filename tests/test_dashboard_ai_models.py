"""The board's AI-models section reads each product from where it lives.

AIFS ENS (charter 204) off the SSOT's P0 envelopes, the deterministic
feature family (charter 201) off its gate artifact, AIGEFS off the research
database -- and a missing file or volume is a reading, not an error.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from bayes_basket_target.contracts import ProbabilityEnvelope
from bayes_basket_target.probability_bridge import materialize_probability_bridge
from bayes_basket_target.ssot import apply_production_ssot_schema
from scripts import dashboard_ai_models as AI
from tests.test_bayes_basket_target_production_worker import _taf_live_compatibility_lineage

NOW = datetime(2026, 9, 23, 15, 0, tzinfo=timezone.utc)


def _p0(conn, station: str, day: str, weights: dict, decided: str, *, feature=None,
        probabilities: dict | None = None) -> None:
    # A recompile that changes nothing is a restatement and writes nothing
    # (``_restated_p0_checkpoint``), so a second P0 on a scope moves its vector.
    lineage = {
        **_taf_live_compatibility_lineage(),
        "settlement_timezone": "UTC",
        "season_bin": "summer", "lead_bucket": "d2_fallback", "run_cycle": "18Z",
        "settlement_definition": "official_daily_tmax_c",
        # After the helper: it carries test weights of its own.
        "system_weights_json": json.dumps(weights, sort_keys=True),
        "bucket_centers_c_json": json.dumps({"20": 20.0, "21": 21.0, "22": 22.0}),
        "bucket_intervals_c_json": json.dumps({"20": [None, 20.5], "21": [20.5, 21.5], "22": [21.5, None]}),
    }
    if feature is not None:
        lineage["deterministic_feature_json"] = json.dumps(feature)
    root = ProbabilityEnvelope.from_mapping({
        "stage": "P0", "checkpoint_id": f"p0-{station}-{day}-{decided[11:19]}",
        "station_id": station, "event_date": day, "settlement_timezone": "UTC",
        "decision_at_utc": decided, "fresh_until_utc": "2026-09-26T00:00:00Z",
        "bucket_ordinals": {"20": 0, "21": 1, "22": 2},
        "probabilities": probabilities or {"20": 0.2, "21": 0.6, "22": 0.2},
        "posterior_version": "calibrated_native_cross_ensemble_p0_v10",
        "posterior_sha256": "a" * 64, "bridge_sha256": "b" * 64, "lineage": lineage,
    })
    materialize_probability_bridge(conn, root, created_at_utc=decided)


def test_served_reading_is_per_station_day_and_names_the_weight(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    four = {"DWD_ICON_GLOBAL_EPS": 0.3, "ECCC_GEM_GLOBAL_ENS": 0.2,
            "ECMWF_IFS_ENS_OPEN_PF50": 0.25, "NOAA_GEFS_05": 0.25}
    five = {**{k: v * 0.8 for k, v in four.items()}, AI.AIFS_ENS_SYSTEM_ID: 0.2}
    _p0(conn, "EDDM", "2026-09-24", four, "2026-09-23T10:00:00.000Z")
    _p0(conn, "EDDM", "2026-09-24", five, "2026-09-23T14:00:00.000Z",
        feature={"state": "SHADOW", "centre_c": 21.0, "shadow_centre_c": 20.6},
        probabilities={"20": 0.1, "21": 0.6, "22": 0.3})
    _p0(conn, "EDDM", "2026-09-25", four, "2026-09-23T14:00:00.000Z")
    _p0(conn, "ZUUU", "2026-09-24", four, "2026-09-23T14:00:00.000Z")
    conn.commit()

    served = AI.aifs_ens_served(conn, now=NOW)
    assert served["p0_packets"] == 4 and served["with_aifs"] == 1
    assert served["scopes_seen"] == 3 and served["scopes_seated"] == 1
    assert served["by_event_date"] == {
        "2026-09-24": {"scopes": 2, "with_aifs": 1, "with_aigefs": 0},
        "2026-09-25": {"scopes": 1, "with_aifs": 0, "with_aigefs": 0},
    }
    eddm = next(s for s in served["scopes"] if s["station_id"] == "EDDM" and s["event_date"] == "2026-09-24")
    assert eddm["aifs_weight"] == 0.2 and AI.AIFS_ENS_SYSTEM_ID in eddm["systems"]
    assert served["pools"][0]["count"] == 3
    assert served["deterministic_shadow"] == {
        "states": {"SHADOW": 1}, "mean_abs_shift_c": 0.4, "max_abs_shift_c": 0.4,
    }


def test_the_served_read_seeks_the_stage_clock_index() -> None:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    plan = " ".join(str(r[-1]) for r in conn.execute(
        "EXPLAIN QUERY PLAN " + AI.SERVED_SQL, ("2026-09-22T15:00:00", 10)
    ))
    assert "basket_probability_bridge_history_stage_decided_idx" in plan
    assert "SCAN b" not in plan


def test_forward_gate_and_research_readings_from_files(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox.json"
    inbox.write_text(json.dumps({"packets": [
        {"station_id": "EDDM", "event_date": "2026-09-24",
         "run_init_utc_by_system": {AI.AIFS_ENS_SYSTEM_ID: "2026-09-23T06:00:00Z",
                                    "NOAA_GEFS_05": "2026-09-23T06:00:00Z"},
         "run_cycle_by_system": {AI.AIFS_ENS_SYSTEM_ID: "06Z"},
         "lead_bucket_by_system": {AI.AIFS_ENS_SYSTEM_ID: "h12_24"},
         "received_at_utc": "2026-09-23T13:00:00Z"},
        {"station_id": "ZUUU", "event_date": "2026-09-25",
         "run_init_utc_by_system": {"NOAA_GEFS_05": "2026-09-23T06:00:00Z"}},
    ]}))
    forward = AI.aifs_ens_forward(inbox, now=NOW)
    assert forward["packets"] == 2 and forward["packets_with_aifs"] == 1
    assert forward["stations"]["EDDM"]["run_cycle"] == "06Z"
    assert forward["latest_run_age_hours"] == 9.0
    assert AI.aifs_ens_forward(tmp_path / "missing.json", now=NOW) == {"available": False}

    gate = tmp_path / "gate.json"
    gate.write_text(json.dumps({
        "fitted_at_utc": "2026-09-23T10:51:02Z", "window": ["2026-07-25", "2026-09-22"],
        "stations": {
            "CYYZ": {"candidate_system": "NCEP_HRRR_POINT", "selected_system": None,
                     "sources": {"NCEP_HRRR_POINT": {"event_days": 4, "d_rps": [-0.15, -0.3, -0.01], "verdict": "INSUFFICIENT"},
                                 AI.AIFS_SINGLE_SYSTEM_ID: {"event_days": 4, "d_rps": [0.01, -0.1, 0.1], "verdict": "INSUFFICIENT"}}},
            "EDDM": {"candidate_system": None, "selected_system": None, "sources": {}},
        },
    }))
    family = AI.deterministic_family(gate)
    assert family["gate_available"] and family["stations"] == 2 and family["selected"] == 0
    assert family["aifs_single_stations"] == 1
    cyyz = family["by_station"][0]
    assert cyyz["candidate_system"] == "NCEP_HRRR_POINT" and cyyz["aifs_single"] == [0.01, -0.1, 0.1]
    assert AI.deterministic_family(tmp_path / "no-gate.json")["gate_available"] is False

    research_dir = tmp_path / "aigefs"
    research_dir.mkdir()
    (research_dir / "plan_2026-06-17_2026-09-20.json").write_text("{}")
    absent = AI.aigefs_research(research_dir / "none.db", research_dir, now=NOW)
    assert absent["available"] is False and absent["plan"] == {"from": "2026-06-17", "to": "2026-09-20"}
    db = research_dir / "aigefs_ens_allruns.db"
    rc = sqlite3.connect(db)
    rc.execute("CREATE TABLE research_aigefs_runs(run_init_utc TEXT, rows INTEGER, station_days INTEGER, finished_at_utc TEXT)")
    rc.execute("INSERT INTO research_aigefs_runs VALUES('2026-09-01T00:00:00Z', 100, 45, '2026-09-23T14:00:00Z')")
    rc.execute("CREATE TABLE research_aigefs_missing_runs(run_init_utc TEXT)")
    rc.commit(); rc.close()
    present = AI.aigefs_research(db, research_dir, now=NOW)
    assert present["available"] and present["runs"]["count"] == 1 and present["missing_runs"] == 0
    assert present["in_production"] is True
