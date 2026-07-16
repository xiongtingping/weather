from pathlib import Path

from scripts.collect_jma_gsm_zuuu import collect


ROOT = Path(__file__).resolve().parents[1]


def test_jma_unconfigured_authorized_delivery_is_an_explicit_missing_source(tmp_path: Path) -> None:
    result = collect(
        run_at_utc="2026-07-12T00:00:00Z",
        valid_time_utc="2026-07-12T06:00:00Z",
        source_root=tmp_path / "sources",
        catalog_path=ROOT / "config" / "forecast_sources" / "official_nwp_zuuu_v1.json",
    )

    assert result["ok"] is False
    assert result["blockers"] == ["jma_authorized_delivery_not_configured"]
