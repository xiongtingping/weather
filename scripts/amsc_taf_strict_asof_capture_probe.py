#!/usr/bin/env python3
"""Read-only audit of AMSC raw-archive TAF strict-as-of eligibility."""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from amsc_taf_strict_asof_capture_runtime import (  # noqa: E402
    SOURCE_ID,
    build_amsc_taf_strict_asof_capture_proposal,
)


SCHEMA_VERSION = "amsc_taf_strict_asof_capture_probe_v1"
DEFAULT_ARCHIVE_DIR = ROOT / "data" / "amsc_raw_history"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return _mapping(value) or None


def _read_json_gz(path: Path) -> dict[str, Any] | None:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return _mapping(value) or None


def build_amsc_taf_strict_asof_capture_probe(archive_dir: Path) -> dict[str, Any]:
    """Audit existing raw TAF objects without treating historic retrieval as new data."""

    root = Path(archive_dir).expanduser().resolve()
    manifests: list[tuple[str, dict[str, Any]]] = []
    for path in sorted((root / "manifests").glob("**/*.json")) if (root / "manifests").exists() else []:
        value = _read_json(path)
        if value is not None:
            manifests.append((str(path.relative_to(root)), value))
    by_raw_sha: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for ref, manifest in manifests:
        for item in manifest.get("raw_objects") or []:
            row = _mapping(item)
            raw_sha = _text(row.get("raw_sha256"))
            if raw_sha:
                by_raw_sha.setdefault(raw_sha, []).append((ref, manifest))

    rows: list[dict[str, Any]] = []
    unreadable_objects = 0
    for path in sorted((root / "objects").glob("**/*.json.gz")) if (root / "objects").exists() else []:
        raw = _read_json_gz(path)
        if raw is None:
            unreadable_objects += 1
            continue
        if _text(raw.get("source_id")) != SOURCE_ID:
            continue
        raw_sha = _text(raw.get("raw_sha256"))
        first_seen = _text(raw.get("first_seen_at"))
        object_ref = str(path.relative_to(root))
        first_receipts = [
            (ref, manifest)
            for ref, manifest in by_raw_sha.get(raw_sha, [])
            if _text(manifest.get("response_completed_at")) == first_seen
        ]
        if len(first_receipts) != 1:
            rows.append(
                {
                    "raw_object_ref": object_ref,
                    "raw_sha256": raw_sha,
                    "first_seen_at": first_seen,
                    "state": "blocked",
                    "block_reasons": ["amsc_taf_first_manifest_missing_or_ambiguous"],
                    "strict_training_eligible": False,
                }
            )
            continue
        manifest_ref, manifest = first_receipts[0]
        proposal = build_amsc_taf_strict_asof_capture_proposal(
            raw_object=raw,
            raw_object_ref=object_ref,
            first_poll_manifest=manifest,
        )
        capture = _mapping(proposal.get("capture"))
        normalized = _mapping(proposal.get("normalized_forecast"))
        rows.append(
            {
                "raw_object_ref": object_ref,
                "raw_sha256": raw_sha,
                "first_manifest_ref": manifest_ref,
                "first_seen_at": first_seen,
                "event_date": _text(normalized.get("target_event_date_local") or capture.get("event_date")),
                "forecast_peak_c": normalized.get("forecast_peak_c"),
                "forecast_peak_valid_time": _text(normalized.get("forecast_peak_valid_time")),
                "state": _text(proposal.get("state")),
                "block_reasons": list(proposal.get("block_reasons") or []),
                "evidence_tier": _text(capture.get("evidence_tier")),
                "strict_training_eligible": capture.get("strict_training_eligible") is True,
            }
        )
    rows.sort(key=lambda row: (row["first_seen_at"], row["raw_sha256"]))
    eligible = [row for row in rows if row["strict_training_eligible"] is True]
    return {
        "schema": SCHEMA_VERSION,
        "ok": unreadable_objects == 0,
        "state": "eligible_samples_found" if eligible else "no_eligible_samples",
        "archive_dir": str(root),
        "taf_raw_object_count": len(rows),
        "strict_training_eligible_count": len(eligible),
        "unreadable_object_count": unreadable_objects,
        "rows": rows,
        "guards": {
            "writes_archive_db": False,
            "writes_research_files": False,
            "creates_p1_forecast_revision": False,
            "creates_model_artifact": False,
            "creates_posterior": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
        "read_only": True,
        "next_boundary": (
            "stage_only_each_new_tier_a_taf_capture_after_separate_review"
            if eligible
            else "continue_prospective_amsc_transport_capture_without_historical_promotion"
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR)
    parser.add_argument("--require-eligible", action="store_true")
    args = parser.parse_args(argv)
    report = build_amsc_taf_strict_asof_capture_probe(args.archive_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not args.require_eligible or report["strict_training_eligible_count"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
