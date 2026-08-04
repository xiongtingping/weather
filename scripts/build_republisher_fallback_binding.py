#!/usr/bin/env python3
"""Build the immutable Open-Meteo no-live fallback pointer.

This command only reads retained audit artifacts and writes a small
content-addressed pointer.  It never fetches Open-Meteo/ECMWF data, writes the
production database, or changes native-arrival/P1/formal authority.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.republisher_fallback_binding import (  # noqa: E402
    build_republisher_fallback_binding,
    validate_republisher_fallback_binding,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    load_bounded_json,
    write_bounded_json_if_changed,
)


DEFAULT_AIFS = ROOT / "output/audits/open_meteo_aifs_deterministic_historical_oos_20260802.json"
DEFAULT_ICON = ROOT / "output/audits/open_meteo_icon_global_deterministic_historical_oos_20260802.json"
DEFAULT_REGIONAL = ROOT / "output/audits/regional_model_candidate_closure_20260802_latest.json"
DEFAULT_OUTPUT = ROOT / "config/bayes_basket_target/republisher_no_live_fallback_binding_v1.json"
DEFAULT_AUDIT_OUTPUT = ROOT / "output/audits/republisher_no_live_fallback_binding_20260802.json"


def _load(path: Path) -> Mapping[str, Any]:
    value = load_bounded_json(path.expanduser().resolve(strict=True), limit=MAX_JSON_INPUT_BYTES)
    if not isinstance(value, Mapping):
        raise SystemExit(f"mapping_required:{path}")
    return value


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _as_of(value: str | None, reports: list[Mapping[str, Any]]) -> str:
    if value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = max(
            datetime.fromisoformat(str(report.get("as_of_utc")).replace("Z", "+00:00"))
            for report in reports
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SystemExit("as_of_utc_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aifs", type=Path, default=DEFAULT_AIFS)
    parser.add_argument("--icon", type=Path, default=DEFAULT_ICON)
    parser.add_argument("--regional", type=Path, default=DEFAULT_REGIONAL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--audit-output", type=Path, default=DEFAULT_AUDIT_OUTPUT)
    parser.add_argument("--as-of-utc")
    args = parser.parse_args()

    aifs = _load(args.aifs)
    icon = _load(args.icon)
    regional = _load(args.regional)
    inputs = [args.aifs, args.icon, args.regional]
    source_artifacts = []
    for path, value in zip(inputs, [aifs, icon, regional]):
        resolved = path.expanduser().resolve(strict=True)
        source_artifacts.append({
            "path": str(resolved.relative_to(ROOT)) if resolved.is_relative_to(ROOT) else str(resolved),
            "file_sha256": _sha_file(resolved),
            "content_sha256": str(value.get("content_sha256") or ""),
            "schema": str(value.get("schema") or ""),
        })
    binding = build_republisher_fallback_binding(
        deterministic_reports=[aifs, icon],
        regional_audit=regional,
        as_of_utc=_as_of(args.as_of_utc, [aifs, icon]),
        source_artifacts=source_artifacts,
    )
    validate_republisher_fallback_binding(binding)
    output = args.output.expanduser().resolve()
    audit_output = args.audit_output.expanduser().resolve()
    wrote_output = write_bounded_json_if_changed(output, binding, limit=MAX_JSON_INPUT_BYTES)
    wrote_audit = write_bounded_json_if_changed(audit_output, binding, limit=MAX_JSON_INPUT_BYTES)
    print(json.dumps({
        "schema": binding["schema"],
        "state": binding["state"],
        "binding_count": binding["binding_count"],
        "bound_model_ids": binding["bound_model_ids"],
        "candidate_only_model_ids": binding["candidate_only_model_ids"],
        "formal_p0_eligible": binding["formal_p0_eligible"],
        "native_arrival_index_written": binding["native_arrival_index_written"],
        "p1_authority_written": binding["p1_authority_written"],
        "formal_live_authority_written": binding["formal_live_authority_written"],
        "production_database_written": binding["production_database_written"],
        "calls_order_gateway": binding["calls_order_gateway"],
        "orders_submitted": binding["orders_submitted"],
        "output": str(output),
        "audit_output": str(audit_output),
        "wrote_output": wrote_output,
        "wrote_audit": wrote_audit,
        "content_sha256": binding["content_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
