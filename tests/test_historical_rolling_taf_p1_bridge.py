from __future__ import annotations

import json
from pathlib import Path

from bayes_basket_target.contracts import content_sha256


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "output/audits/historical_rolling_taf_p1_bridge_20260803_0728.json"


def test_historical_rolling_bridge_is_candidate_only_and_historical() -> None:
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    identity = {key: value for key, value in payload.items() if key != "content_sha256"}
    assert payload["schema"] == "bayes_basket_target_historical_rolling_taf_p1_bridge_v1"
    assert payload["historical_event_date_cutoff"] == "2026-07-28"
    assert payload["oos_data_policy"] == "HISTORICAL_CLOSED_DAY_ONLY"
    assert payload["future_data_policy"] == "OBSERVATIONAL_CANDIDATE_NOT_OOS"
    assert payload["summary"]["taf_qualified_scope_count_at_24"] == 0
    assert payload["summary"]["current_p0_root_bound_count"] == 0
    assert payload["boundary"]["future_data_used_as_oos"] is False
    assert payload["boundary"]["writes_target_database"] is False
    assert payload["boundary"]["production_p0_materialized"] is False
    assert payload["boundary"]["production_p0m_materialized"] is False
    assert payload["boundary"]["p1_materialized"] is False
    assert payload["content_sha256"] == content_sha256(identity)
