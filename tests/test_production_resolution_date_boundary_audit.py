from pathlib import Path

from scripts.production_resolution_date_boundary_audit import audit


ROOT = Path(__file__).resolve().parents[1]


def test_production_resolution_date_boundary_is_aligned() -> None:
    result = audit(ROOT)

    assert result["ok"] is True
    assert result["blockers"] == []
    assert all(result["checks"].values())
    assert result["resolution_local_timezone"] == "Asia/Shanghai"
    assert result["operations_timezone"] == "America/New_York"
    assert result["read_only"] is True
    assert result["places_orders"] is False


# Retired 2026-09-07 with its subject.
#
# test_hotfix_cannot_silently_switch_to_operations_date copied the runtime
# hotfix cutover script into a temp tree, rewrote its timezone to New York,
# and checked the audit caught the switch.  That script was deleted on
# 2026-08-04 when the cutover finished; the test could only raise
# FileNotFoundError.  The audit itself now names the check as a retired
# subject rather than silently failing it, which is the property this test
# was protecting -- and the surviving test above asserts the audit is
# aligned, retired subjects and all.
