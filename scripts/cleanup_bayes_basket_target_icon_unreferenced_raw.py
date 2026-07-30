#!/usr/bin/env python3
"""Remove only provably unreferenced Open-Meteo ICON raw objects.

This is deliberately narrower than a general data cleanup command.  It can
only operate on the isolated research raw directory created by the explicit
Open-Meteo historical replay and refuses every other path.  A raw object is
eligible only when its content-addressed filename is not referenced anywhere
else in the workspace (the raw directory itself is excluded from the scan).
The deletion manifest is written after the deletion as an audit tombstone and
is not an active lineage reference.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RAW_ROOT = (ROOT / "data/research/bayes_basket_target/model_arrivals/icon_global_open_meteo/raw").resolve()
AUDIT_ROOT = (ROOT / "output/audits").resolve()
REPORT = AUDIT_ROOT / "bayes_basket_target_open_meteo_icon_raw_cleanup_20260725.json"
HEX64 = set("0123456789abcdef")
RAW_NAME_RE = re.compile(rb"(?<![0-9a-f])[0-9a-f]{64}\.json(?![0-9a-f])")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _validate_target() -> None:
    expected = (ROOT / "data/research/bayes_basket_target/model_arrivals/icon_global_open_meteo/raw").resolve()
    if RAW_ROOT != expected or ROOT not in RAW_ROOT.parents:
        raise SystemExit("refusing_unexpected_raw_root")
    if not RAW_ROOT.is_dir():
        raise SystemExit("icon_raw_root_missing")


def _workspace_files() -> list[Path]:
    result = subprocess.run(
        [
            "rg", "--files", "--hidden", "--follow",
            "--glob", "!.git/**",
            "--glob", "!data/research/bayes_basket_target/model_arrivals/icon_global_open_meteo/raw/**",
            str(ROOT),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise SystemExit(f"workspace_file_scan_failed:{result.stderr[-300:]}")
    return [Path(line) for line in result.stdout.splitlines() if line]


def _reference_index(files: list[Path]) -> dict[str, list[str]]:
    """Read each candidate workspace file once and index exact raw names."""
    index: dict[str, list[str]] = {}
    for path in files:
        if path == REPORT or "open_meteo_icon_raw_cleanup_" in path.name:
            continue
        try:
            body = path.read_bytes()
        except OSError:
            continue
        for match in set(RAW_NAME_RE.findall(body)):
            filename = match.decode("ascii")
            index.setdefault(filename, []).append(str(path))
    return index


def _references(filename: str, index: dict[str, list[str]]) -> list[str]:
    # Cleanup tombstones are intentionally not active lineage references.
    return index.get(filename, [])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _raw_rows(files: list[Path], reference_index: dict[str, list[str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    eligible: list[dict[str, Any]] = []
    retained: list[dict[str, Any]] = []
    for path in files:
        if path.suffix != ".json" or len(path.stem) != 64 or any(ch not in HEX64 for ch in path.stem):
            raise SystemExit(f"unexpected_file_in_raw_root:{path}")
        actual = _sha256(path)
        row: dict[str, Any] = {"path": str(path), "sha256": actual, "bytes": path.stat().st_size}
        if actual != path.stem:
            raise SystemExit(f"content_address_mismatch:{path}")
        refs = _references(path.name, reference_index)
        if refs:
            row["active_references"] = refs[:32]
            row["active_reference_count"] = len(refs)
            retained.append(row)
        else:
            row["active_references"] = []
            row["active_reference_count"] = 0
            eligible.append(row)
    return eligible, retained


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="delete the exact eligible files")
    args = parser.parse_args()
    _validate_target()
    files = sorted(RAW_ROOT.glob("*.json"))
    workspace_files = _workspace_files()
    reference_index = _reference_index(workspace_files)
    eligible, retained = _raw_rows(files, reference_index)
    report: dict[str, Any] = {
        "schema": "bayes_basket_target_open_meteo_icon_raw_cleanup_v1",
        "generated_at_utc": _now(),
        "mode": "apply" if args.apply else "dry_run",
        "target_raw_root": str(RAW_ROOT),
        "workspace_scan_root": str(ROOT),
        "raw_file_count_before": len(files),
        "eligible_unreferenced_count": len(eligible),
        "eligible_unreferenced_bytes": sum(int(row["bytes"]) for row in eligible),
        "retained_referenced_count": len(retained),
        "retained_referenced_bytes": sum(int(row["bytes"]) for row in retained),
        "deleted": [],
        "eligible_unreferenced": eligible,
        "retained_referenced": retained,
    }
    if args.apply:
        for row in eligible:
            path = Path(str(row["path"])).resolve()
            if path.parent != RAW_ROOT or not path.is_file():
                raise SystemExit(f"refusing_unexpected_delete_target:{path}")
            path.unlink()
            report["deleted"].append(row)
        report["raw_file_count_after"] = len(list(RAW_ROOT.glob("*.json")))
    else:
        report["raw_file_count_after"] = len(files)
    AUDIT_ROOT.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "mode": report["mode"],
        "raw_file_count_before": report["raw_file_count_before"],
        "eligible_unreferenced_count": report["eligible_unreferenced_count"],
        "eligible_unreferenced_bytes": report["eligible_unreferenced_bytes"],
        "retained_referenced_count": report["retained_referenced_count"],
        "raw_file_count_after": report["raw_file_count_after"],
        "report": str(REPORT),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
