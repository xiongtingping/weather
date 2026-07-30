#!/usr/bin/env python3
"""Install the reviewed v6 peak-timing integration after writers are paused.

Default mode is a read-only preview.  The controlled cutover wrapper owns
service pause, backup, activation regeneration, replay, acceptance and
rollback; this installer only performs three exact source transformations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "production_probability_v6_canonical_event_drain_runtime.py"
CONFIRMATION = "INSTALL_V6_PATH_AUXILIARY_RUNTIME"


IMPORT_BEFORE = """from production_probability_v6_public_ssot_writer import (
    commit_v6_public_physical_transaction,
    materialize_v6_public_physical_fact_row,
)
"""
IMPORT_AFTER = IMPORT_BEFORE + """from production_probability_v6_path_auxiliary_runtime import (
    enrich_v6_candidate_with_post_event_auxiliary,
    inject_v6_peak_timing_into_public_fact,
)
"""

WEAK_BEFORE = """    responsibilities = _parse(snapshot.get(\"candidate_scenario_responsibilities_json\"), {})
    if not paths or not responsibilities:
        raise ValueError(\"v6_drain_weak_future_controls_missing\")
    return {
        \"artifact_sha256\": _text(snapshot.get(\"weak_likelihood_artifact_sha256\")),
        \"joint_forecast_weak_likelihood_sha256\": _text(snapshot.get(\"weak_likelihood_sha256\")),
        \"forecast_scenario_paths\": paths,
        \"candidate_scenario_responsibilities\": responsibilities,
    }
"""
WEAK_AFTER = """    responsibilities = _parse(snapshot.get(\"candidate_scenario_responsibilities_json\"), {})
    peak_timing = _parse(snapshot.get(\"forecast_peak_timing_prior_json\"), {})
    if not paths or not responsibilities:
        raise ValueError(\"v6_drain_weak_future_controls_missing\")
    if not peak_timing:
        raise ValueError(\"v6_drain_forecast_peak_timing_prior_missing\")
    return {
        \"artifact_sha256\": _text(snapshot.get(\"weak_likelihood_artifact_sha256\")),
        \"joint_forecast_weak_likelihood_sha256\": _text(snapshot.get(\"weak_likelihood_sha256\")),
        \"forecast_scenario_paths\": paths,
        \"candidate_scenario_responsibilities\": responsibilities,
        \"forecast_peak_timing_prior\": peak_timing,
    }
"""

COMPOSE_BEFORE = """        fact_id = \"fact-v6-event-\" + _sha({\"checkpoint\": candidate[\"checkpoint_row\"][\"posterior_checkpoint_id\"]})
        public = materialize_v6_public_physical_fact_row(
"""
COMPOSE_AFTER = """        candidate = enrich_v6_candidate_with_post_event_auxiliary(
            candidate=candidate,
            canonical_events_through_current=[*history, event],
            forecast_peak_timing_prior=(
                weak.get(\"forecast_peak_timing_prior\")
                if isinstance(weak.get(\"forecast_peak_timing_prior\"), Mapping)
                else {}
            ),
        )
        fact_id = \"fact-v6-event-\" + _sha({\"checkpoint\": candidate[\"checkpoint_row\"][\"posterior_checkpoint_id\"]})
        public = materialize_v6_public_physical_fact_row(
"""

PUBLIC_BEFORE = """            market_bucket_probabilities_na_reason=\"resolution_bridge_not_closed\",
        )
        committed = commit_v6_public_physical_transaction(
"""
PUBLIC_AFTER = """            market_bucket_probabilities_na_reason=\"resolution_bridge_not_closed\",
        )
        public = inject_v6_peak_timing_into_public_fact(
            public_fact_row=public,
            candidate=candidate,
        )
        committed = commit_v6_public_physical_transaction(
"""


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def transform(source: str) -> str:
    # The final market-Transaction-B successor deliberately folds the public
    # peak projection into commit_v6_candidate_transaction and narrows the
    # auxiliary import to the pre-commit enrichment function.  That source is
    # already a semantic successor of this installer even though the original
    # exact import/public anchors no longer exist.  Recognize the complete
    # successor closure as idempotent; never accept a partial marker set.
    market_successor_markers = (
        "from production_probability_v6_path_auxiliary_runtime import (\n"
        "    enrich_v6_candidate_with_post_event_auxiliary,\n)",
        "from production_probability_v6_transaction_commit_runtime import (\n"
        "    commit_v6_candidate_transaction,\n)",
        "candidate = enrich_v6_candidate_with_post_event_auxiliary(",
        "branch_commit = commit_v6_candidate_transaction(",
        '"forecast_peak_timing_prior": peak_timing,',
    )
    successor_hits = [marker in source for marker in market_successor_markers]
    if all(successor_hits):
        return source
    # Only the transaction-commit markers distinguish the successor from the
    # normal path-auxiliary result; enrichment and peak-prior markers are
    # intentionally shared by both complete forms.
    if successor_hits[1] or successor_hits[3]:
        raise ValueError("v6_path_auxiliary_market_successor_partial_install")
    result = source
    replacements = (
        (IMPORT_BEFORE, IMPORT_AFTER, "import"),
        (WEAK_BEFORE, WEAK_AFTER, "weak_peak_prior"),
        (COMPOSE_BEFORE, COMPOSE_AFTER, "candidate_enrichment"),
        (PUBLIC_BEFORE, PUBLIC_AFTER, "public_projection"),
    )
    for before, after, label in replacements:
        if result.count(after) == 1:
            continue
        if result.count(before) != 1:
            raise ValueError(f"v6_path_auxiliary_install_anchor_invalid:{label}")
        result = result.replace(before, after, 1)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    target = root / TARGET.relative_to(ROOT)
    source = target.read_text(encoding="utf-8")
    transformed = transform(source)
    changed = transformed != source
    if args.apply:
        if args.confirm != CONFIRMATION:
            raise SystemExit("exact_confirmation_required")
        if changed:
            temporary = target.with_name(target.name + ".tmp-v6-path-auxiliary")
            temporary.write_text(transformed, encoding="utf-8")
            temporary.replace(target)
    print(
        json.dumps(
            {
                "ok": True,
                "state": (
                    "installed" if args.apply and changed else
                    "already_installed" if args.apply else
                    "preview_ready"
                ),
                "target": str(target),
                "changed": changed,
                "source_sha256": _sha(source),
                "candidate_sha256": _sha(transformed),
                "writes_source": bool(args.apply and changed),
                "places_orders": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
