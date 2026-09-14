from __future__ import annotations

from pathlib import Path

from bayes_basket_target.p1_observation_dependency_audit import (
    audit_p1_observation_dependencies,
)


def test_formal_p1_chain_is_registry_scoped_and_enforces_global_awc() -> None:
    receipt = audit_p1_observation_dependencies(
        project_root=Path(__file__).resolve().parents[1],
    )

    assert receipt["registry_scoped_p1"] is True
    assert receipt["missing_files"] == []
    assert receipt["hardcoded_station_hits"] == []
    assert receipt["single_lane_filter_hits"] == []
    assert receipt["awc_global_gate_enforced"] is True
    assert len(receipt["awc_global_gate_hits"]) == 1
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


def test_awc_comparisons_pass_only_behind_the_global_gate(tmp_path: Path) -> None:
    gated = tmp_path / "gated_p1.py"
    gated.write_text(
        "def admit(lanes, truth):\n"
        "    if lanes != {'awc'}:\n"
        "        raise ValueError('non_awc_registry_row')\n"
        "    return truth.source_lane == 'AWC'\n",
        encoding="utf-8",
    )
    receipt = audit_p1_observation_dependencies(
        project_root=tmp_path, files=(gated,),
    )
    assert receipt["registry_scoped_p1"] is True
    assert receipt["single_lane_filter_hits"] == []
    assert [hit["expression"] for hit in receipt["awc_policy_consistent_hits"]] == [
        "truth.source_lane == 'AWC'"
    ]

    amsc = tmp_path / "amsc_p1.py"
    amsc.write_text(
        "def admit(lanes, row):\n"
        "    if lanes != {'awc'}:\n"
        "        raise ValueError('non_awc_registry_row')\n"
        "    return row.source_lane == 'AMSC'\n",
        encoding="utf-8",
    )
    receipt = audit_p1_observation_dependencies(
        project_root=tmp_path, files=(amsc,),
    )
    assert receipt["registry_scoped_p1"] is False
    assert "AMSC" in receipt["single_lane_filter_hits"][0]["expression"]
