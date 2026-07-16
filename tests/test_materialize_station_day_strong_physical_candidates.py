from __future__ import annotations

import json
import sqlite3

from resolution_final_source_runtime import PHYSICAL_ROLE, PHYSICAL_SOURCE_ID
from resolution_final_source_shadow_writer import SOURCE_TABLE
from scripts import materialize_station_day_strong_physical_candidates as materializer


def test_materializer_quarantines_non_strict_physical_x_without_market_dependency(tmp_path, monkeypatch) -> None:
    p2 = tmp_path / "p2.db"
    final = tmp_path / "final.db"
    root = tmp_path / "research"
    monkeypatch.setattr(materializer, "DEFAULT_RESEARCH_ROOT", root)
    conn = sqlite3.connect(p2)
    conn.execute("CREATE TABLE canonical_observation_events (canonical_observation_event_id TEXT,target_identity TEXT,station_id TEXT,report_type TEXT,valid_time TEXT,first_actionable_seen_at TEXT,state TEXT,parsed_observation_json TEXT,quality_flags_json TEXT)")
    conn.execute("INSERT INTO canonical_observation_events VALUES (?,?,?,?,?,?,?,?,?)", ("evt-1", "physical_weather_day_v1:ZUUU:2026-07-12", "ZUUU", "METAR", "2026-07-12T04:00:00Z", "2026-07-12T04:01:00Z", "accepted_primary", json.dumps({"temperature_c": 33.0}), "[]"))
    conn.commit(); conn.close()
    conn = sqlite3.connect(final)
    conn.execute(f"CREATE TABLE {SOURCE_TABLE} (source_id TEXT,role TEXT,observation_json TEXT)")
    conn.execute(f"INSERT INTO {SOURCE_TABLE} VALUES (?,?,?)", (PHYSICAL_SOURCE_ID, PHYSICAL_ROLE, json.dumps({"event_date": "2026-07-12", "fetched_at": "2026-07-12T16:00:00Z", "raw_sha256": "a" * 64, "source_observation_id": "x-1", "payload": {"label_finality": "final", "final_physical_high_c": 33.0, "strict_asof_training_eligible": False}})))
    conn.commit(); conn.close()

    result = materializer.materialize_station_day_strong_physical_candidates(p2_db=p2, final_source_db=final, research_root=root, allow_research_write=True)

    assert result["state"] == "materialized"
    item = result["outcomes"][0]
    payload = json.loads(open(item["path"], encoding="utf-8").read())
    assert payload["state"] == "quarantined"
    assert "final_physical_x_not_strict_asof_training_eligible" in payload["block_reasons"]
    assert payload["boundary"]["market_finality_not_required"] is True
