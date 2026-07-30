from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dependency_refresh_never_requests_referenced_raw_purge() -> None:
    source = (ROOT / "scripts" / "market_dependency_refresh.py").read_text(encoding="utf-8")

    assert "\"--purge-unreferenced\"" in source
    assert "\"--purge-referenced\"" not in source


def test_retention_worker_declares_referenced_body_delete_forbidden() -> None:
    source = (ROOT / "scripts" / "official_nwp_raw_retention.py").read_text(encoding="utf-8")

    assert '"referenced_body_delete_forbidden": True' in source
    assert "--purge-referenced" not in source
