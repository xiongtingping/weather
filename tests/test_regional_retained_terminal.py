from __future__ import annotations

import json
from pathlib import Path

import pytest

from bayes_basket_target.regional_retained_terminal import (
    RegionalRetainedTerminalError,
    build_regional_retained_terminal_bundle,
)


ROOT = Path(__file__).resolve().parents[1]
AUDITS = ROOT / "output" / "audits"
EVIDENCE = sorted((AUDITS / "bayes_basket_target_regional_28d_v2").glob("*.json"))
COVERAGE = AUDITS / "pr05_pr09_v13_model_oos_scope_bundle_20260803T210800Z_v6.json"


def test_real_retained_regional_raw_becomes_terminal_not_event() -> None:
    result = build_regional_retained_terminal_bundle(
        as_of_utc="2026-08-05T12:00:00Z",
        coverage_scope_path=COVERAGE,
        evidence_paths=EVIDENCE,
    )

    assert result["state"] == "MATERIALIZED_UNPUBLISHED"
    assert result["release_state"] == "BLOCKED"
    assert result["terminal_count"] == 44
    assert result["consumable"] is False
    assert result["writes_database"] is False
    assert result["publishes_pr04_pointer"] is False
    assert all(item["blocker"] == "LINEAGE_MISMATCH" for item in result["terminals"])
    assert all(item["lead_bucket"] == "d1" for item in result["terminals"])
    assert all(item["source_class"] == "aggregator_republisher" for item in result["terminals"])
    assert all(item["run_cycle"] is None for item in result["terminals"])
    assert all(item["retained_row_count"] >= 27 for item in result["terminals"])


def test_evidence_alias_tamper_fails_closed(tmp_path: Path) -> None:
    raw = json.loads(EVIDENCE[0].read_text(encoding="utf-8"))
    raw["provider_id"] = "OPEN_METEO"
    target = tmp_path / "tampered.json"
    target.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(
        RegionalRetainedTerminalError,
        match="evidence_content_sha256_mismatch",
    ):
        build_regional_retained_terminal_bundle(
            as_of_utc="2026-08-05T12:00:00Z",
            coverage_scope_path=COVERAGE,
            evidence_paths=[target],
        )
