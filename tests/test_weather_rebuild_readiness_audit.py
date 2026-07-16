from __future__ import annotations

import json
import sqlite3

from resolution_final_source_runtime import PHYSICAL_ROLE, PHYSICAL_SOURCE_ID
from resolution_final_source_shadow_writer import SOURCE_TABLE
from scripts.weather_rebuild_readiness_audit import _physical_x_training_label_counts


def test_physical_x_counts_read_nested_final_label_payload_separately_from_market_labels(tmp_path) -> None:
    path = tmp_path / "finals.db"
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE {SOURCE_TABLE} (source_id TEXT, role TEXT, observation_json TEXT)")
    for eligible in (True, False):
        conn.execute(
            f"INSERT INTO {SOURCE_TABLE}(source_id, role, observation_json) VALUES (?, ?, ?)",
            (PHYSICAL_SOURCE_ID, PHYSICAL_ROLE, json.dumps({"payload": {"label_finality": "final", "strict_asof_training_eligible": eligible}})),
        )
    conn.commit()
    conn.close()

    counts = _physical_x_training_label_counts(path)

    assert counts["physical_final_x_sources"] == 2
    assert counts["strict_asof_training_eligible_physical_x"] == 1
    assert counts["non_strict_physical_x"] == 1
