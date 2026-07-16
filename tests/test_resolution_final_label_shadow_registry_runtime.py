from __future__ import annotations

import sqlite3

from resolution_final_label_shadow_registry_runtime import build_resolution_final_label_shadow_registry_audit
from resolution_final_source_shadow_writer import initialize_isolated_resolution_final_source_staging_schema
from resolution_label_shadow_writer import initialize_isolated_resolution_label_staging_schema
from resolution_observed_x_s_pair_shadow import initialize_isolated_resolution_observed_x_s_pair_schema
from resolution_target_contract_shadow_writer import initialize_isolated_resolution_target_contract_schema
from resolution_target_capture_cursor import initialize_resolution_target_capture_cursor
from market_resolution_finality_shadow_writer import initialize_isolated_market_resolution_finality_schema
from sequential_bayes_public_ssot_shadow_writer import initialize_isolated_public_ssot_shadow_schema


def test_final_label_registry_audit_requires_isolation_and_reports_empty_state(tmp_path) -> None:
    path = tmp_path / "labels.db"
    missing = build_resolution_final_label_shadow_registry_audit(db_path=path)
    assert missing["ok"] is False
    assert missing["state"] == "missing"

    with sqlite3.connect(path) as conn:
        initialize_isolated_public_ssot_shadow_schema(conn)
        initialize_isolated_resolution_target_contract_schema(conn)
        initialize_resolution_target_capture_cursor(conn, forward_capture_not_before="2026-07-12T05:00:00Z")
        initialize_isolated_resolution_final_source_staging_schema(conn)
        initialize_isolated_resolution_observed_x_s_pair_schema(conn)
        initialize_isolated_market_resolution_finality_schema(conn)
        initialize_isolated_resolution_label_staging_schema(conn)
    report = build_resolution_final_label_shadow_registry_audit(db_path=path)
    assert report["ok"] is True
    assert report["state"] == "initialized_empty"
    assert report["counts"] == {
        "target_contracts": 0,
        "market_finality_attestations": 0,
        "final_sources": 0,
        "observed_x_s_pairs": 0,
        "final_labels": 0,
    }
    assert report["forward_capture_not_before"] == "2026-07-12T05:00:00Z"
