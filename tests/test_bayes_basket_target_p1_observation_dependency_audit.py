from __future__ import annotations

from pathlib import Path

from bayes_basket_target.p1_observation_dependency_audit import (
    audit_p1_observation_dependencies,
)


def test_formal_p1_chain_has_no_zuuu_or_single_lane_filter() -> None:
    receipt = audit_p1_observation_dependencies(
        project_root=Path(__file__).resolve().parents[1],
    )

    assert receipt["registry_scoped_p1"] is True
    assert receipt["missing_files"] == []
    assert receipt["hardcoded_station_hits"] == []
    assert receipt["single_lane_filter_hits"] == []
    assert receipt["syntax_hits"] == []
    assert receipt["writes_production_db"] is False
    assert receipt["calls_order_gateway"] is False


def test_audit_detects_station_and_single_lane_code_filters(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "bad_p1.py"
    candidate.write_text(
        "def select(row):\n"
        "    if row['station_id'] == 'ZUUU':\n"
        "        return row['source_lane'] == 'AMSC'\n"
        "    return False\n",
        encoding="utf-8",
    )

    receipt = audit_p1_observation_dependencies(
        project_root=tmp_path,
        files=(candidate,),
    )

    assert receipt["registry_scoped_p1"] is False
    assert receipt["hardcoded_station_hits"][0]["station_id"] == "ZUUU"
    assert "AMSC" in receipt["single_lane_filter_hits"][0]["expression"]


def test_audit_detects_single_lane_sql_filter(tmp_path: Path) -> None:
    candidate = tmp_path / "bad_p1_sql.py"
    candidate.write_text(
        "QUERY = \"SELECT * FROM observations "
        "WHERE source_lane = 'AWC'\"\n",
        encoding="utf-8",
    )

    receipt = audit_p1_observation_dependencies(
        project_root=tmp_path,
        files=(candidate,),
    )

    assert receipt["registry_scoped_p1"] is False
    assert receipt["single_lane_filter_hits"]
