from __future__ import annotations

import json
from pathlib import Path

from scripts.bayes_basket_target_resource_audit import audit


def test_resource_audit_protects_referenced_and_reports_only_unreferenced(tmp_path: Path) -> None:
    root = tmp_path / "arrivals"
    raw = root / "icon_global_official" / "raw"
    receipts = root / "icon_global_official" / "receipts"
    raw.mkdir(parents=True)
    receipts.mkdir(parents=True)
    referenced = raw / ("a" * 64 + ".bz2")
    list_referenced = raw / ("c" * 64 + ".bz2")
    unreferenced = raw / ("b" * 64 + ".bz2")
    referenced.write_bytes(b"keep")
    list_referenced.write_bytes(b"keep-list")
    unreferenced.write_bytes(b"candidate")
    receipts.joinpath("receipt.json").write_text(
        json.dumps({
            "raw_sha256": "a" * 64,
            "raw_payload_sha256s": ["c" * 64],
            "raw_object_ref": str(referenced),
        }),
        encoding="utf-8",
    )

    result = audit(root=root, now=referenced.stat().st_mtime + 7200)

    assert result["state"] == "AUDIT_ONLY"
    assert result["cleanup_performed"] is False
    assert [row["path"] for row in result["unreferenced_raw"]] == [str(unreferenced.resolve())]
    assert referenced.is_file()
    assert list_referenced.is_file()
    assert unreferenced.is_file()
