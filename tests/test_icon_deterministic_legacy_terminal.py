from __future__ import annotations

import json
from pathlib import Path

import pytest

from bayes_basket_target.icon_deterministic_legacy_terminal import (
    ICONLegacyTerminalError,
    build_icon_legacy_terminal_bundle,
)


ROOT = Path(__file__).resolve().parents[1]
AUDITS = ROOT / "output" / "audits"
REPORT = AUDITS / "open_meteo_icon_global_deterministic_historical_oos_20260802.json"
BINDING = AUDITS / "republisher_no_live_fallback_binding_20260803.json"
COVERAGE = AUDITS / "pr05_pr09_v13_model_oos_scope_bundle_20260803T210800Z_v6.json"


def _build(**overrides: Path) -> dict[str, object]:
    return build_icon_legacy_terminal_bundle(
        as_of_utc="2026-08-05T12:00:00Z",
        report_path=overrides.get("report_path", REPORT),
        binding_path=overrides.get("binding_path", BINDING),
        coverage_scope_path=overrides.get("coverage_scope_path", COVERAGE),
    )


def test_legacy_passes_are_bound_as_lineage_terminals_only() -> None:
    result = _build()
    assert result["state"] == "MATERIALIZED_UNPUBLISHED"
    assert result["release_state"] == "BLOCKED"
    assert result["terminal_count"] == 44
    assert result["legacy_pass_count"] == 43
    assert result["legacy_rejected_count"] == 1
    assert result["consumable"] is False
    assert result["writes_database"] is False
    assert all(item["blocker"] == "LINEAGE_MISMATCH" for item in result["terminals"])
    assert all(
        item["legacy_holdout_decision_after_event_count"] == 8
        for item in result["terminals"]
    )
    assert {
        item["station_id"] for item in result["terminals"]
        if item["legacy_claimed_state"] == "OOS_REJECTED"
    } == {"OPKC"}


def test_pass_without_exact_binding_fails_closed(tmp_path: Path) -> None:
    raw = json.loads(BINDING.read_text(encoding="utf-8"))
    raw["bindings"] = [
        item for item in raw["bindings"]
        if not (item["model_id"] == "ICON" and item["station_id"] == "CYYZ")
    ]
    raw["binding_count"] -= 1
    raw.pop("content_sha256")
    from bayes_basket_target.contracts import content_sha256
    raw["content_sha256"] = content_sha256(raw)
    target = tmp_path / "binding.json"
    target.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ICONLegacyTerminalError, match="pass_binding_missing"):
        _build(binding_path=target)
