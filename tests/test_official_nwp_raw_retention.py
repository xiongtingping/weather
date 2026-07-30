from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.official_nwp_raw_retention import acknowledge_missing_references, apply_unreferenced, build_plan


def _raw(root: Path, provider: str, body: bytes, suffix: str = ".grib2") -> Path:
    digest = hashlib.sha256(body).hexdigest()
    path = root / provider / "raw" / f"{digest}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def test_retention_protects_referenced_raw_and_plans_unreferenced(tmp_path: Path) -> None:
    curve_root = tmp_path / "curve"
    referenced = _raw(curve_root, "icon", b"referenced")
    unreferenced = _raw(curve_root, "icon", b"unreferenced")
    history = curve_root / "icon" / "history" / "snapshot.json"
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text(
        json.dumps({"raw_object_ref": str(referenced), "raw_sha256": referenced.name.split(".", 1)[0]})
        + "\n",
        encoding="utf-8",
    )

    plan = build_plan(curve_root, providers=("icon",))

    assert plan["active_capture_processes"] == []
    assert [row["path"] for row in plan["unreferenced_files"]] == [str(unreferenced)]
    assert plan["old_referenced_files"] == []

    applied = apply_unreferenced(plan)
    assert applied["deleted_count"] == 1
    assert applied["deleted_bytes"] == len(b"unreferenced")
    assert referenced.is_file()
    assert not unreferenced.exists()


def test_retention_plan_does_not_treat_missing_provider_as_blocking(tmp_path: Path) -> None:
    plan = build_plan(tmp_path / "curve", providers=("ecmwf", "icon", "gfs"))

    assert plan["state"] == "retention_plan_ready"
    assert plan["apply_allowed"] is True
    assert all(row["raw_file_count"] == 0 for row in plan["provider_rows"])


def test_retention_blocks_apply_when_an_existing_raw_reference_is_dangling(tmp_path: Path) -> None:
    curve_root = tmp_path / "curve"
    history = curve_root / "icon" / "history" / "snapshot.json"
    history.parent.mkdir(parents=True, exist_ok=True)
    missing = curve_root / "icon" / "raw" / ("a" * 64 + ".grib2.bz2")
    history.write_text(json.dumps({"raw_object_ref": str(missing)}) + "\n", encoding="utf-8")

    plan = build_plan(curve_root, providers=("icon",))

    assert plan["missing_raw_reference_count"] == 1
    assert plan["apply_allowed"] is False


def test_retention_keeps_referenced_old_run_and_discards_only_unreferenced(tmp_path: Path) -> None:
    curve_root = tmp_path / "curve"
    old_raw = _raw(curve_root, "icon", b"old")
    new_raw = _raw(curve_root, "icon", b"new")
    receipts = curve_root / "icon" / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    for name, run, raw in (("old.json", "2026-07-20T00:00:00Z", old_raw), ("new.json", "2026-07-23T00:00:00Z", new_raw)):
        (receipts / name).write_text(
            json.dumps(
                {
                    "provider_id": "icon",
                    "run_at_utc": run,
                    "valid_time_utc": run,
                    "raw_receipts": [
                        {
                            "raw_object_ref": str(raw),
                            "raw_sha256": raw.name.split(".", 1)[0],
                            "requested_at": run,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    plan = build_plan(curve_root, providers=("icon",), retain_latest_runs=1)

    assert plan["provider_rows"][0]["hot_runs"] == ["2026-07-23T00:00:00Z"]
    assert [row["path"] for row in plan["old_referenced_files"]] == [str(old_raw)]
    assert [row["path"] for row in plan["hot_files"]] == [str(new_raw)]

    applied = apply_unreferenced(plan)

    assert applied["deleted_count"] == 0
    assert old_raw.exists()
    assert new_raw.exists()


def test_acknowledge_missing_references_removes_only_the_cleanup_block(tmp_path: Path) -> None:
    curve_root = tmp_path / "curve"
    missing = curve_root / "icon" / "raw" / ("b" * 64 + ".grib2.bz2")
    history = curve_root / "icon" / "history" / "snapshot.json"
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text(json.dumps({"raw_object_ref": str(missing)}) + "\n", encoding="utf-8")

    plan = build_plan(curve_root, providers=("icon",))
    acknowledged = acknowledge_missing_references(plan)
    refreshed = build_plan(curve_root, providers=("icon",))

    assert acknowledged["acknowledged_count"] == 1
    assert refreshed["missing_raw_reference_count"] == 0
    assert refreshed["acknowledged_missing_raw_reference_count"] == 1
    assert refreshed["apply_allowed"] is True
