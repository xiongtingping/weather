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


def test_hotfix_cannot_silently_switch_to_operations_date(tmp_path: Path) -> None:
    for relative in (
        "docs/constitution/SYSTEM_CHARTER.md",
        "scripts/cutover_physical_state_bayes_v6_runtime_hotfix.sh",
        "scripts/amsc_zuuu_metar_taf_poll_once.sh",
        "scripts/polymarket_monitor_daemon.sh",
        "scripts/cutover_v6_probability_and_soft_market_projection.sh",
    ):
        source = ROOT / relative
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    hotfix = tmp_path / "scripts/cutover_physical_state_bayes_v6_runtime_hotfix.sh"
    hotfix.write_text(
        hotfix.read_text(encoding="utf-8").replace(
            "TZ=Asia/Shanghai date +%F", "TZ=America/New_York date +%F"
        ),
        encoding="utf-8",
    )

    result = audit(tmp_path)
    assert result["ok"] is False
    assert (
        "resolution_date_boundary_failed:hotfix_uses_zuuu_resolution_local_date"
        in result["blockers"]
    )
