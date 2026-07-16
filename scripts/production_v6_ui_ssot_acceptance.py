#!/usr/bin/env python3
"""Read-only acceptance for the production v6 SSOT-to-UI projection."""

from __future__ import annotations

import argparse
import json
import math
import urllib.request
from typing import Any, Mapping


DEFAULT_URL = (
    "http://127.0.0.1:3002/api/ui_snapshot_summary"
    "?run_date=2026-07-16&limit=2&city=chengdu"
)
V6_SCHEMAS = {
    "sequential_temperature_posterior_ssot_v3_reported_x",
    "sequential_temperature_posterior_ssot_v4_reported_x",
}


def _items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    facts = payload.get("facts")
    rows = facts.get("items") if isinstance(facts, Mapping) else None
    return [row for row in rows or [] if isinstance(row, Mapping)]


def audit(payload: Mapping[str, Any]) -> dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []
    matched: Mapping[str, Any] | None = None
    for item in _items(payload):
        ssot = item.get("posterior_ssot_fact")
        if isinstance(ssot, Mapping) and str(ssot.get("schema") or "") in V6_SCHEMAS:
            matched = item
            break
    if matched is None:
        blockers.append("production_v6_reported_x_item_missing")
        return {
            "schema": "production_v6_ui_ssot_acceptance_v1",
            "ok": False,
            "state": "blocked",
            "blockers": blockers,
            "warnings": warnings,
            "read_only": True,
            "places_orders": False,
        }

    ssot = matched.get("posterior_ssot_fact")
    ssot = ssot if isinstance(ssot, Mapping) else {}
    rows = ssot.get("reported_physical_x_distribution")
    rows = rows if isinstance(rows, list) else []
    probabilities = []
    temperatures = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        try:
            temperature = float(row.get("temperature_c"))
            probability = float(row.get("probability"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(temperature) and math.isfinite(probability):
            temperatures.append(temperature)
            probabilities.append(probability)
    if not probabilities:
        blockers.append("reported_physical_x_distribution_missing")
    elif abs(sum(probabilities) - 1.0) > 1e-6:
        blockers.append("reported_physical_x_distribution_not_normalized")

    sections = matched.get("ui_sections")
    sections = sections if isinstance(sections, Mapping) else {}
    physical = sections.get("physical_high_posterior")
    amsc = sections.get("amsc_metar_ssot")
    strong = sections.get("observation_strong")
    market = sections.get("bayes_posterior_ssot")
    physical = physical if isinstance(physical, Mapping) else {}
    amsc = amsc if isinstance(amsc, Mapping) else {}
    strong = strong if isinstance(strong, Mapping) else {}
    market = market if isinstance(market, Mapping) else {}

    if str(physical.get("state") or "") != "available":
        blockers.append("physical_high_posterior_ui_missing")
    if str(amsc.get("state") or "") != "available":
        blockers.append("amsc_metar_ssot_ui_missing")
    if str(strong.get("state") or "") in {"", "missing"}:
        blockers.append("strong_observation_ui_missing")
    if not isinstance(strong.get("termination_risk"), Mapping):
        blockers.append("termination_risk_ui_missing")

    market_rows = ssot.get("market_bucket_probabilities")
    if not market_rows and str(market.get("state") or "") != "market_projection_na":
        blockers.append("market_projection_must_be_explicit_na_without_bridge")
    if not isinstance(ssot.get("peak_timing_state"), Mapping):
        warnings.append("peak_timing_state_pending_probability_runtime_cutover")

    labels = [str(row.get("label") or row.get("bucket") or "") for row in rows if isinstance(row, Mapping)]
    if any("below" in label.lower() or "higher" in label.lower() for label in labels):
        blockers.append("reported_x_ui_contains_market_edge_bucket_label")

    return {
        "schema": "production_v6_ui_ssot_acceptance_v1",
        "ok": not blockers,
        "state": "accepted" if not blockers else "blocked",
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "slug": matched.get("slug"),
        "event_date": matched.get("event_date"),
        "posterior_schema": ssot.get("schema"),
        "reported_x_temperature_count": len(temperatures),
        "amsc_display_source": amsc.get("source"),
        "strong_observation_state": strong.get("state"),
        "market_projection_state": market.get("state"),
        "external_disk_required": False,
        "read_only": True,
        "places_orders": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--input-json", default="")
    args = parser.parse_args()
    if args.input_json:
        with open(args.input_json, encoding="utf-8") as handle:
            payload = json.load(handle)
    else:
        with urllib.request.urlopen(args.url, timeout=15) as response:
            payload = json.load(response)
    result = audit(payload)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
