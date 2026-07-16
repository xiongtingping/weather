#!/usr/bin/env python3
"""Install the reviewed default-off v6 market Transaction-B branch.

Preview is read-only.  Apply is allowed only inside the controlled writer-
quiescent cutover, where source, activation and database rollback are owned by
the wrapper.  The transform also includes the already-reviewed peak auxiliary
transform so one candidate source is content addressed as a whole.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.install_v6_path_auxiliary_runtime import transform as add_peak_auxiliary


DRAIN = ROOT / "production_probability_v6_canonical_event_drain_runtime.py"
INGRESS = ROOT / "production_probability_v6_amsc_archive_ingress.py"
CONFIRMATION = "INSTALL_V6_MARKET_TRANSACTION_BRANCH_RUNTIME"


DRAIN_IMPORT_BEFORE = """from production_probability_v6_public_ssot_writer import (
    commit_v6_public_physical_transaction,
    materialize_v6_public_physical_fact_row,
)
from production_probability_v6_path_auxiliary_runtime import (
    enrich_v6_candidate_with_post_event_auxiliary,
    inject_v6_peak_timing_into_public_fact,
)
"""
DRAIN_IMPORT_AFTER = """from production_probability_v6_path_auxiliary_runtime import (
    enrich_v6_candidate_with_post_event_auxiliary,
)
from production_probability_v6_transaction_commit_runtime import (
    commit_v6_candidate_transaction,
)
"""

DRAIN_SIGNATURE_BEFORE = """    event_date: str,
    public_slug: str = "",
    max_events: int = 64,
) -> dict[str, Any]:
"""
DRAIN_SIGNATURE_AFTER = """    event_date: str,
    public_slug: str = "",
    max_events: int = 64,
    market_inputs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
"""

DRAIN_COMMIT_BEFORE = """        fact_id = "fact-v6-event-" + _sha({"checkpoint": candidate["checkpoint_row"]["posterior_checkpoint_id"]})
        public = materialize_v6_public_physical_fact_row(
            activation=authorized,
            strong_update_row=candidate["strong_update_row"],
            checkpoint_row=candidate["checkpoint_row"],
            fact_snapshot_id=fact_id,
            slug=public_slug or build_zuuu_physical_public_slug(day),
            event_date=day,
            fact_as_of=_text(candidate["checkpoint_row"].get("committed_at")),
            correlation_id=_text(candidate["strong_update_row"].get("strong_observation_update_id")),
            market_bucket_probabilities_na_reason="resolution_bridge_not_closed",
        )
        public = inject_v6_peak_timing_into_public_fact(
            public_fact_row=public,
            candidate=candidate,
        )
        committed = commit_v6_public_physical_transaction(
            conn,
            activation=authorized,
            expected_previous_checkpoint_id=_text(checkpoint.get("posterior_checkpoint_id")),
            expected_previous_posterior_run_id=_text(checkpoint.get("posterior_run_id")),
            strong_update_row=candidate["strong_update_row"],
            checkpoint_row=candidate["checkpoint_row"],
            public_fact_row=public,
        )
"""
DRAIN_COMMIT_AFTER = """        fact_id = "fact-v6-event-" + _sha({"checkpoint": candidate["checkpoint_row"]["posterior_checkpoint_id"]})
        branch_commit = commit_v6_candidate_transaction(
            conn,
            activation=authorized,
            previous_checkpoint=checkpoint,
            candidate=candidate,
            canonical_observation_event=event,
            market_inputs=market_inputs,
            fact_snapshot_id=fact_id,
            slug=public_slug or build_zuuu_physical_public_slug(day),
            event_date=day,
            fact_as_of=_text(candidate["checkpoint_row"].get("committed_at")),
            correlation_id=_text(candidate["strong_update_row"].get("strong_observation_update_id")),
        )
        committed = branch_commit["commit"]
"""

DRAIN_OUTCOME_BEFORE = """                "event_id": event_id,
                "commit": committed,
                "probability_transaction_elapsed_ms": round((time.monotonic() - event_started) * 1000.0, 3),
"""
DRAIN_OUTCOME_AFTER = """                "event_id": event_id,
                "commit": committed,
                "writer_route": _text(branch_commit.get("writer_route")),
                "market_projection_available": bool(branch_commit.get("market_projection_available")),
                "market_projection_na_reason": _text(branch_commit.get("market_projection_na_reason")),
                "probability_transaction_elapsed_ms": round((time.monotonic() - event_started) * 1000.0, 3),
"""

INGRESS_IMPORT_BEFORE = """from production_probability_v6_canonical_event_drain_runtime import drain_v6_canonical_events, ensure_v6_daily_p0
from weather_fact_snapshot_writer import persist_source_observations_for_payload, utc_now_iso
"""
INGRESS_IMPORT_AFTER = """from production_probability_v6_canonical_event_drain_runtime import drain_v6_canonical_events, ensure_v6_daily_p0
from production_probability_v6_market_inputs_runtime import prewarm_v6_market_inputs
from weather_fact_snapshot_writer import persist_source_observations_for_payload, utc_now_iso
"""

INGRESS_PREWARM_BEFORE = """    incremental_ids: list[str] | None = None
    with archive_db_write_gate(db, owner="physical_state_bayes_v6_amsc_ingress", wait_sec=writer_queue_wait_sec):
"""
INGRESS_PREWARM_AFTER = """    incremental_ids: list[str] | None = None
    stage_started = time.monotonic()
    market_inputs = prewarm_v6_market_inputs(
        event_date=day,
        resolution_authority_path=resolution_authority_path,
    )
    stages["market_inputs_prewarm_ms"] = round((time.monotonic() - stage_started) * 1000.0, 3)
    with archive_db_write_gate(db, owner="physical_state_bayes_v6_amsc_ingress", wait_sec=writer_queue_wait_sec):
"""

INGRESS_DRAIN_BEFORE = """                    public_slug=public_slug,
                    max_events=max_events,
                )
"""
INGRESS_DRAIN_AFTER = """                    public_slug=public_slug,
                    max_events=max_events,
                    market_inputs=market_inputs,
                )
"""

INGRESS_RESULT_BEFORE = """        "probability": probability,
        "latency_stages_ms": stages,
"""
INGRESS_RESULT_AFTER = """        "probability": probability,
        "market_branch": {
            "state": _text(market_inputs.get("state")),
            "market_projection_available": bool(market_inputs.get("market_projection_available")),
            "market_projection_na_reason": _text(market_inputs.get("market_projection_na_reason")),
            "blocks_physical_x": False,
        },
        "latency_stages_ms": stages,
"""


def _replace_once(source: str, before: str, after: str, label: str) -> str:
    if source.count(after) == 1:
        return source
    if source.count(before) != 1:
        raise ValueError(f"v6_market_branch_install_anchor_invalid:{label}")
    return source.replace(before, after, 1)


def transform_drain(source: str) -> str:
    result = source if source.count(DRAIN_IMPORT_AFTER) == 1 else add_peak_auxiliary(source)
    for before, after, label in (
        (DRAIN_IMPORT_BEFORE, DRAIN_IMPORT_AFTER, "drain_import"),
        (DRAIN_SIGNATURE_BEFORE, DRAIN_SIGNATURE_AFTER, "drain_signature"),
        (DRAIN_COMMIT_BEFORE, DRAIN_COMMIT_AFTER, "drain_commit"),
        (DRAIN_OUTCOME_BEFORE, DRAIN_OUTCOME_AFTER, "drain_outcome"),
    ):
        result = _replace_once(result, before, after, label)
    return result


def transform_ingress(source: str) -> str:
    result = source
    for before, after, label in (
        (INGRESS_IMPORT_BEFORE, INGRESS_IMPORT_AFTER, "ingress_import"),
        (INGRESS_PREWARM_BEFORE, INGRESS_PREWARM_AFTER, "ingress_prewarm"),
        (INGRESS_DRAIN_BEFORE, INGRESS_DRAIN_AFTER, "ingress_drain"),
        (INGRESS_RESULT_BEFORE, INGRESS_RESULT_AFTER, "ingress_result"),
    ):
        result = _replace_once(result, before, after, label)
    return result


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _install(path: Path, transformed: str) -> None:
    temporary = path.with_name(path.name + ".tmp-v6-market-branch")
    temporary.write_text(transformed, encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    drain = root / DRAIN.relative_to(ROOT)
    ingress = root / INGRESS.relative_to(ROOT)
    sources = {
        "drain": drain.read_text(encoding="utf-8"),
        "ingress": ingress.read_text(encoding="utf-8"),
    }
    candidates = {
        "drain": transform_drain(sources["drain"]),
        "ingress": transform_ingress(sources["ingress"]),
    }
    changed = {key: candidates[key] != sources[key] for key in sources}
    if args.apply:
        if args.confirm != CONFIRMATION:
            raise SystemExit("exact_confirmation_required")
        if changed["drain"]:
            _install(drain, candidates["drain"])
        if changed["ingress"]:
            _install(ingress, candidates["ingress"])
    print(
        json.dumps(
            {
                "ok": True,
                "state": "installed" if args.apply and any(changed.values()) else "already_installed" if args.apply else "preview_ready",
                "changed": changed,
                "files": {
                    key: {
                        "source_sha256": _sha(sources[key]),
                        "candidate_sha256": _sha(candidates[key]),
                    }
                    for key in sources
                },
                "writes_source": bool(args.apply and any(changed.values())),
                "places_orders": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
