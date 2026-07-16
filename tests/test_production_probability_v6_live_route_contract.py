from __future__ import annotations

import shutil
from pathlib import Path

from production_probability_v6_live_route_contract import (
    ROUTE_FILES,
    activate_v6_live_routes,
    audit_v6_live_routes,
    build_v6_live_route_targets,
)


ROOT = Path(__file__).resolve().parents[1]


def _fixture_root(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    for relative in ROUTE_FILES:
        shutil.copy2(ROOT / relative, tmp_path / relative)
    return tmp_path


def test_v6_live_route_activation_is_deterministic_and_idempotent(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    expected = build_v6_live_route_targets(root)
    first = activate_v6_live_routes(root)
    after_first = build_v6_live_route_targets(root)
    second = activate_v6_live_routes(root)
    after_second = build_v6_live_route_targets(root)

    assert first["ok"] is True
    assert second["ok"] is True
    assert expected == after_first == after_second
    assert audit_v6_live_routes(root)["ok"] is True


def test_v6_target_routes_cover_all_realtime_and_prewarm_lanes(tmp_path: Path) -> None:
    targets = build_v6_live_route_targets(_fixture_root(tmp_path))
    text = "\n".join(value.decode("utf-8") for value in targets.values())

    assert "apply_amsc_probability_fast_lane_v6_from_plan.py" in text
    assert "apply_v6_amsc_archive_probability_ingress" in text
    assert "apply_amsc_taf_forecast_revision_v6_from_plan.py" in text
    assert "prepare_next_day_production_probability_v6.py" in text
    assert "PREPARE_NEXT_DAY_PRODUCTION_PHYSICAL_STATE_BAYES_V6" in text
    assert "production_reported_x_amsc_archive_probability_ingress" not in text
    assert "apply_reported_x_amsc_archive_probability_ingress" not in text
