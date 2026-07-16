from __future__ import annotations

import json
import sqlite3

from scripts.initialize_p10_shadow_runtime_schema import CONFIRMATION, main
from sequential_bayes_public_ssot_shadow_writer import initialize_isolated_public_ssot_shadow_schema
from p10_shadow_runtime_schema import initialize_p10_shadow_runtime_schema


def test_command_bootstraps_a_new_empty_p10_runtime_db(tmp_path, capsys) -> None:
    db = tmp_path / "p10_runtime.db"

    assert main(["--shadow-db", str(db), "--apply", "--confirm", CONFIRMATION]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["ok"] is True
    assert result["bootstrap"]["bootstrapped_isolated_manifest"] is True
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM canonical_observation_events").fetchone()[0] == 0


def test_command_refuses_to_upgrade_p2_like_db_with_canonical_evidence(tmp_path, capsys) -> None:
    db = tmp_path / "p2_canonical_amsc_shadow.db"
    with sqlite3.connect(db) as conn:
        initialize_isolated_public_ssot_shadow_schema(conn)
        initialize_p10_shadow_runtime_schema(conn)
        conn.execute(
            """
            INSERT INTO canonical_observation_events(
              canonical_observation_event_id,canonical_observation_key,target_identity,station_id,report_type,
              valid_time,revision_no,normalized_physical_sha256,selected_source_observation_id,selected_source_id,
              selection_role,selection_policy_version,first_transport_seen_at,first_actionable_seen_at,accepted_at,
              state,parsed_observation_json,quality_flags_json,created_at
            ) VALUES ('canobs_legacy','legacy-key','target','ZUUU','METAR','2026-07-12T07:30:00Z',0,'sha',
              'srcobs_legacy','source','primary','policy','2026-07-12T07:30:48Z','2026-07-12T07:30:48Z',
              '2026-07-12T07:30:48Z','accepted_primary','{}','[]','2026-07-12T07:30:48Z')
            """
        )

    assert main(["--shadow-db", str(db), "--apply", "--confirm", CONFIRMATION]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["blockers"] == ["p10_shadow_runtime_schema_failed:ValueError"]
