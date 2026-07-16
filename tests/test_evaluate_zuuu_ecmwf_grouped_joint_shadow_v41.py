from __future__ import annotations

from scripts import evaluate_zuuu_ecmwf_grouped_joint_shadow_v41 as grouped


def test_wind_family_groups_without_overlapping_850_component() -> None:
    assert grouped._wind_family("850wind") == "850_involved"
    assert grouped._wind_family("850wind+shear") == "850_involved"
    assert grouped._wind_family("700wind+shear") == "shear_without_850"
    assert grouped._wind_family("700wind") == "700_only"
    assert grouped._wind_family("none") is None
