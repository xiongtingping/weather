#!/usr/bin/env python3
"""Read-only UI snapshot lineage consistency audit.

The audit verifies that the facts rendered by /api/ui_snapshot point back to
the same event_fact_history rows and fact snapshot IDs used by the SSOT chain
replay. It never writes to the database and does not call external weather
sources.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_DB = ROOT / "data" / "archive.db"


def _safe_json(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _append_unique(out: List[str], raw: Any) -> None:
    text = str(raw or "").strip()
    if text and text not in out:
        out.append(text)


def _to_int(raw: Any) -> Optional[int]:
    try:
        value = int(raw)
    except Exception:
        return None
    return value


def _unique_text(values: Iterable[Any]) -> List[str]:
    out: List[str] = []
    for value in values:
        _append_unique(out, value)
    return out


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _display_only_source_observation(row: Any) -> bool:
    if not isinstance(row, Mapping):
        return False
    if bool(row.get("ui_display_derived_from_fact_features")):
        return True
    if bool(row.get("not_posterior_input")):
        return True
    if bool(row.get("display_only")):
        return True
    payload = row.get("payload")
    payload_map = payload if isinstance(payload, Mapping) else {}
    if bool(payload_map.get("ui_display_derived_from_fact_features")):
        return True
    if bool(payload_map.get("not_posterior_input")):
        return True
    boundary = str(payload_map.get("boundary") or row.get("boundary") or "")
    return "display_only_from_existing_fact_fields" in boundary


def _display_only_source_ids_from_payload(value: Any) -> Dict[str, List[str]]:
    if not isinstance(value, Mapping):
        return {"source_event_ids": [], "source_observation_ids": []}
    rows: List[Mapping[str, Any]] = []
    for row in value.get("source_observations") or []:
        if isinstance(row, Mapping):
            rows.append(row)
    trace = value.get("source_acquisition_trace")
    if isinstance(trace, Mapping):
        for row in trace.get("source_observations") or []:
            if isinstance(row, Mapping):
                rows.append(row)
    source_event_ids: List[str] = []
    source_observation_ids: List[str] = []
    for row in rows:
        if not _display_only_source_observation(row):
            continue
        _append_unique(source_event_ids, row.get("source_event_id"))
        _append_unique(source_observation_ids, row.get("source_observation_id"))
    return {
        "source_event_ids": source_event_ids,
        "source_observation_ids": source_observation_ids,
    }


def _display_only_source_ids_from_items(items: Sequence[Mapping[str, Any]]) -> Dict[str, List[str]]:
    source_event_ids: List[str] = []
    source_observation_ids: List[str] = []
    for item in items or []:
        ids = _display_only_source_ids_from_payload(item)
        for value in ids.get("source_event_ids") or []:
            _append_unique(source_event_ids, value)
        for value in ids.get("source_observation_ids") or []:
            _append_unique(source_observation_ids, value)
    return {
        "source_event_ids": source_event_ids,
        "source_observation_ids": source_observation_ids,
    }


def _to_float(raw: Any) -> Optional[float]:
    try:
        value = float(raw)
    except Exception:
        return None
    return value if math.isfinite(value) else None


def _prob_close(a: Any, b: Any, *, abs_tol: float = 1e-9) -> bool:
    left = _to_float(a)
    right = _to_float(b)
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    return math.isclose(left, right, rel_tol=0.0, abs_tol=abs_tol)


def _source_event_ids_from_payload(value: Any) -> List[str]:
    if not isinstance(value, Mapping):
        return []
    out: List[str] = []
    posterior_ssot = value.get("posterior_ssot_fact")
    if isinstance(posterior_ssot, Mapping):
        _append_unique(out, posterior_ssot.get("last_canonical_event_id"))
    lineage = value.get("lineage_state")
    if isinstance(lineage, Mapping):
        for source_event_id in lineage.get("source_event_ids") or []:
            _append_unique(out, source_event_id)
        _append_unique(out, lineage.get("source_event_id"))
    for row in value.get("source_observations") or []:
        if isinstance(row, Mapping):
            _append_unique(out, row.get("source_event_id"))
    trace = value.get("source_acquisition_trace")
    if isinstance(trace, Mapping):
        for source_event_id in trace.get("source_event_ids") or []:
            _append_unique(out, source_event_id)
        for row in trace.get("source_observations") or []:
            if isinstance(row, Mapping):
                _append_unique(out, row.get("source_event_id"))
    for key in ("signal_to_order_timing", "hard_source_acceptance_state"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            _append_unique(out, nested.get("source_event_id"))
    _append_unique(out, value.get("source_event_id"))
    return out


def _source_observation_ids_from_payload(value: Any) -> List[str]:
    if not isinstance(value, Mapping):
        return []
    out: List[str] = []
    lineage = value.get("lineage_state")
    if isinstance(lineage, Mapping):
        for source_observation_id in lineage.get("source_observation_ids") or []:
            _append_unique(out, source_observation_id)
        _append_unique(out, lineage.get("source_observation_id"))
    posterior_ssot = value.get("posterior_ssot_fact")
    posterior_lineage = posterior_ssot.get("lineage") if isinstance(posterior_ssot, Mapping) else {}
    if isinstance(posterior_lineage, Mapping):
        for source_observation_id in posterior_lineage.get("source_observation_ids") or []:
            _append_unique(out, source_observation_id)
    for source_observation_id in value.get("source_observation_ids") or []:
        _append_unique(out, source_observation_id)
    for row in value.get("source_observations") or []:
        if isinstance(row, Mapping):
            _append_unique(out, row.get("source_observation_id"))
    trace = value.get("source_acquisition_trace")
    if isinstance(trace, Mapping):
        for source_observation_id in trace.get("source_observation_ids") or []:
            _append_unique(out, source_observation_id)
        for row in trace.get("source_observations") or []:
            if isinstance(row, Mapping):
                _append_unique(out, row.get("source_observation_id"))
    for key in ("signal_to_order_timing", "hard_source_acceptance_state"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            _append_unique(out, nested.get("source_observation_id"))
    _append_unique(out, value.get("source_observation_id"))
    return out


def _fact_snapshot_id_from_payload(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    lineage = value.get("lineage_state")
    lineage_map = lineage if isinstance(lineage, Mapping) else {}
    ssot = value.get("posterior_ssot_fact")
    ssot_map = ssot if isinstance(ssot, Mapping) else {}
    return str(
        value.get("fact_snapshot_id")
        or lineage_map.get("fact_snapshot_id")
        or ssot_map.get("fact_snapshot_id")
        or ""
    ).strip()


def _posterior_run_id_from_payload(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    lineage = value.get("lineage_state")
    lineage_map = lineage if isinstance(lineage, Mapping) else {}
    posterior_runtime = value.get("posterior_runtime")
    posterior_runtime_map = posterior_runtime if isinstance(posterior_runtime, Mapping) else {}
    ssot = value.get("posterior_ssot_fact")
    ssot_map = ssot if isinstance(ssot, Mapping) else {}
    return str(
        value.get("posterior_run_id")
        or lineage_map.get("posterior_run_id")
        or posterior_runtime_map.get("posterior_run_id")
        or ssot_map.get("posterior_run_id")
        or ""
    ).strip()


def _posterior_ssot_fact_from_payload(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    ssot = value.get("posterior_ssot_fact")
    return ssot if isinstance(ssot, Mapping) else {}


def _bucket_label(row: Mapping[str, Any]) -> str:
    return str(row.get("outcome") or row.get("bucket") or row.get("label") or row.get("target_bucket") or "").strip()


def _bucket_probability(row: Mapping[str, Any]) -> Optional[float]:
    for key in (
        "probability",
        "target_probability",
        "posterior_probability",
        "q_temperature_bucket_posterior",
        "q_bayes_posterior",
        "target_bucket_probability",
    ):
        if row.get(key) is not None:
            return _to_float(row.get(key))
    pct = _to_float(row.get("probability_pct"))
    if pct is not None:
        return pct / 100.0
    return None


def _distribution_rows(value: Any) -> List[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _distribution_by_label(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Mapping[str, Any]]:
    out: Dict[str, Mapping[str, Any]] = {}
    for row in rows:
        label = _bucket_label(row)
        if label:
            out.setdefault(label, row)
    return out


def _sorted_distribution_rows(rows: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            -(_bucket_probability(row) if _bucket_probability(row) is not None else -1.0),
            _bucket_label(row),
        ),
    )


def _posterior_distribution_issues(
    *,
    fact_ssot: Mapping[str, Any],
    ui_ssot: Mapping[str, Any],
) -> List[str]:
    issues: List[str] = []
    fact_rows = _distribution_rows(fact_ssot.get("bucket_probabilities"))
    ui_rows = _distribution_rows(ui_ssot.get("bucket_probabilities"))
    ui_ranked_rows = _distribution_rows(ui_ssot.get("ranked_bucket_probabilities"))
    if fact_rows and not ui_rows:
        issues.append("ui_snapshot_posterior_ssot_bucket_probabilities_missing")
        return issues

    fact_by_label = _distribution_by_label(fact_rows)
    ui_by_label = _distribution_by_label(ui_rows)
    for label, fact_row in fact_by_label.items():
        ui_row = ui_by_label.get(label)
        if ui_row is None:
            issues.append("ui_snapshot_posterior_ssot_bucket_probability_row_missing")
            continue
        if not _prob_close(_bucket_probability(ui_row), _bucket_probability(fact_row)):
            issues.append("ui_snapshot_posterior_ssot_bucket_probability_mismatch")

    ui_target = str(ui_ssot.get("target_bucket") or "").strip()
    if ui_target and ui_rows:
        target_row = ui_by_label.get(ui_target)
        if target_row is None:
            issues.append("ui_snapshot_posterior_ssot_target_bucket_missing_from_distribution")
        elif not _prob_close(_bucket_probability(target_row), ui_ssot.get("target_probability")):
            issues.append("ui_snapshot_posterior_ssot_target_probability_not_from_distribution")

    if not ui_ranked_rows:
        return issues
    if not ui_rows:
        issues.append("ui_snapshot_posterior_ssot_ranked_without_bucket_probabilities")
        return issues

    sorted_ui_rows = _sorted_distribution_rows(ui_rows)
    for idx, ranked_row in enumerate(ui_ranked_rows):
        label = _bucket_label(ranked_row)
        if not label:
            issues.append("ui_snapshot_posterior_ssot_ranked_bucket_label_missing")
            continue
        source_row = ui_by_label.get(label)
        if source_row is None:
            issues.append("ui_snapshot_posterior_ssot_ranked_bucket_not_in_distribution")
            continue
        ranked_probability = _bucket_probability(ranked_row)
        source_probability = _bucket_probability(source_row)
        if not _prob_close(ranked_probability, source_probability):
            issues.append("ui_snapshot_posterior_ssot_ranked_probability_mismatch")
        if idx < len(sorted_ui_rows):
            expected_probability = _bucket_probability(sorted_ui_rows[idx])
            if (
                ranked_probability is not None
                and expected_probability is not None
                and expected_probability > ranked_probability + 1e-9
            ):
                issues.append("ui_snapshot_posterior_ssot_ranked_skips_higher_probability_bucket")
    return issues


def _target_selection_from_payload(value: Any, key: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    selection = value.get(key)
    return selection if isinstance(selection, Mapping) else {}


def _selection_lineage_issues(
    *,
    ui_selection: Mapping[str, Any],
    fact_selection: Mapping[str, Any],
    fact_ssot: Mapping[str, Any],
    name: str,
) -> List[str]:
    issues: List[str] = []
    if not ui_selection:
        issues.append(f"ui_snapshot_{name}_missing")
        return issues
    ui_source = str(
        ui_selection.get("ssot_source")
        or ui_selection.get("target_bucket_selection_source")
        or ui_selection.get("source")
        or ""
    )
    if "posterior_ssot_fact" not in ui_source:
        issues.append(f"ui_snapshot_{name}_not_from_posterior_ssot_fact")
    fact_bucket = str(fact_selection.get("target_bucket") or fact_ssot.get("target_bucket") or "").strip()
    ui_bucket = str(ui_selection.get("target_bucket") or "").strip()
    if fact_bucket and ui_bucket and fact_bucket != ui_bucket:
        issues.append(f"ui_snapshot_{name}_target_bucket_mismatch")
    fact_probability = fact_selection.get("target_probability")
    if fact_probability is None:
        fact_probability = fact_ssot.get("target_probability")
    if ui_selection.get("target_probability") is not None and not _prob_close(
        ui_selection.get("target_probability"),
        fact_probability,
    ):
        issues.append(f"ui_snapshot_{name}_target_probability_mismatch")
    probability_field = str(
        ui_selection.get("target_probability_field")
        or ui_selection.get("posterior_field")
        or ""
    )
    if probability_field and "posterior_ssot_fact" not in probability_field:
        issues.append(f"ui_snapshot_{name}_probability_field_not_posterior_ssot_fact")
    return issues


def _posterior_ssot_lineage_issues(
    *,
    ui_item: Mapping[str, Any],
    fact_payload: Mapping[str, Any],
) -> List[str]:
    issues: List[str] = []
    fact_ssot = _posterior_ssot_fact_from_payload(fact_payload)
    if not fact_ssot:
        return issues
    fact_schema = str(fact_ssot.get("schema") or "")
    ui_ssot = _posterior_ssot_fact_from_payload(ui_item)
    if fact_schema == "sequential_temperature_posterior_ssot_v2":
        if str(ui_ssot.get("schema") or "") != fact_schema:
            return ["ui_snapshot_sequential_posterior_ssot_fact_missing"]
        for field in ("fact_snapshot_id", "posterior_run_id", "posterior_checkpoint_id"):
            fact_value = str(fact_ssot.get(field) or "").strip()
            ui_value = str(ui_ssot.get(field) or "").strip()
            if fact_value and ui_value != fact_value:
                issues.append(f"ui_snapshot_sequential_posterior_{field}_mismatch")
        fact_physical = fact_ssot.get("physical_high_distribution")
        ui_physical = ui_ssot.get("physical_high_distribution")
        if not isinstance(fact_physical, Mapping) or not fact_physical:
            issues.append("fact_sequential_physical_high_distribution_missing")
        if not isinstance(ui_physical, list) or not ui_physical:
            issues.append("ui_snapshot_sequential_physical_high_distribution_missing")
        else:
            fact_top_temperature, fact_top_probability = max(
                ((str(key), _to_float(value)) for key, value in fact_physical.items()),
                key=lambda pair: float(pair[1] if pair[1] is not None else -1.0),
            )
            ui_top = ui_physical[0] if isinstance(ui_physical[0], Mapping) else {}
            if not _prob_close(ui_top.get("probability"), fact_top_probability):
                issues.append("ui_snapshot_sequential_physical_top_probability_mismatch")
            if _to_float(ui_top.get("temperature_c")) != _to_float(fact_top_temperature):
                issues.append("ui_snapshot_sequential_physical_top_temperature_mismatch")
        physical_section = _item_ui_section(ui_item, "physical_high_posterior")
        if str(physical_section.get("state") or "") != "available":
            issues.append("ui_snapshot_sequential_physical_high_section_missing")
        market_section = _item_ui_section(ui_item, "bayes_posterior_ssot")
        if not market_section:
            issues.append("ui_snapshot_sequential_market_projection_section_missing")
        elif not fact_ssot.get("market_bucket_probabilities"):
            if str(market_section.get("state") or "") != "market_projection_na":
                issues.append("ui_snapshot_sequential_market_projection_not_na")
            if not str(market_section.get("na_reason") or "").strip():
                issues.append("ui_snapshot_sequential_market_projection_na_reason_missing")
        return issues
    if fact_schema != "bayes_posterior_ssot_fact_v1":
        return issues
    if str(fact_ssot.get("state") or "") != "available":
        return issues
    fact_bucket = str(fact_ssot.get("target_bucket") or "").strip()
    fact_probability = fact_ssot.get("target_probability")
    ui_ssot_state = str(ui_ssot.get("state") or "").strip() if ui_ssot else ""
    if ui_ssot_state and ui_ssot_state != "available":
        for selection_name in ("posterior_target_bucket_selection", "target_bucket_selection"):
            ui_selection = _target_selection_from_payload(ui_item, selection_name)
            if ui_selection:
                issues.append(f"ui_snapshot_{selection_name}_present_after_posterior_ssot_invalidated")
        return issues
    if ui_ssot:
        ui_bucket = str(ui_ssot.get("target_bucket") or "").strip()
        if fact_bucket and ui_bucket and fact_bucket != ui_bucket:
            issues.append("ui_snapshot_posterior_ssot_fact_target_bucket_mismatch")
        if ui_ssot.get("target_probability") is not None and not _prob_close(
            ui_ssot.get("target_probability"),
            fact_probability,
        ):
            issues.append("ui_snapshot_posterior_ssot_fact_target_probability_mismatch")
        for field in ("fact_snapshot_id", "posterior_run_id"):
            fact_value = str(fact_ssot.get(field) or "").strip()
            ui_value = str(ui_ssot.get(field) or "").strip()
            if fact_value and ui_value and fact_value != ui_value:
                issues.append(f"ui_snapshot_posterior_ssot_fact_{field}_mismatch")
        issues.extend(_posterior_distribution_issues(fact_ssot=fact_ssot, ui_ssot=ui_ssot))
    elif not (
        _target_selection_from_payload(ui_item, "posterior_target_bucket_selection")
        or _target_selection_from_payload(ui_item, "target_bucket_selection")
    ):
        issues.append("ui_snapshot_posterior_ssot_fact_missing")

    for selection_name in ("posterior_target_bucket_selection", "target_bucket_selection"):
        ui_selection = _target_selection_from_payload(ui_item, selection_name)
        fact_selection = _target_selection_from_payload(fact_payload, selection_name)
        issues.extend(
            _selection_lineage_issues(
                ui_selection=ui_selection,
                fact_selection=fact_selection,
                fact_ssot=fact_ssot,
                name=selection_name,
            )
        )
    return issues


def _lineage_items_from_snapshot(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    lineage = payload.get("lineage_state")
    lineage_items = _mapping(lineage).get("items")
    if isinstance(lineage_items, list) and lineage_items:
        return [dict(item) for item in lineage_items if isinstance(item, Mapping)]
    facts = payload.get("facts")
    fact_items = _mapping(facts).get("items")
    if not isinstance(fact_items, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in fact_items:
        if not isinstance(item, Mapping):
            continue
        lineage_map = _mapping(item.get("lineage_state"))
        fact_id = _to_int(item.get("fact_id"))
        fact_snapshot_id = str(item.get("fact_snapshot_id") or lineage_map.get("fact_snapshot_id") or "").strip()
        if fact_id is None and not fact_snapshot_id:
            continue
        out.append(
            {
                "slug": str(item.get("slug") or ""),
                "event_date": str(item.get("event_date") or ""),
                "fact_id": fact_id,
                "fact_snapshot_id": fact_snapshot_id,
                "posterior_run_id": str(item.get("posterior_run_id") or lineage_map.get("posterior_run_id") or ""),
                "source_event_ids": _source_event_ids_from_payload(item),
                "source_observation_ids": _source_observation_ids_from_payload(item),
                "signal_id": str(
                    _mapping(item.get("signal_to_order_timing")).get("signal_id")
                    or _mapping(item.get("hard_source_acceptance_state")).get("signal_id")
                    or item.get("signal_id")
                    or ""
                ),
            }
        )
    return out


def _source_fact_ids_from_snapshot(payload: Mapping[str, Any], lineage_items: Sequence[Mapping[str, Any]]) -> List[int]:
    raw_ids: List[Any] = []
    raw_ids.extend(payload.get("source_fact_ids") or [])
    lineage = payload.get("lineage_state")
    raw_ids.extend(_mapping(lineage).get("source_fact_ids") or [])
    raw_ids.extend(item.get("fact_id") for item in lineage_items)
    out: List[int] = []
    for raw in raw_ids:
        value = _to_int(raw)
        if value is not None and value not in out:
            out.append(value)
    return out


def _fact_items_by_fact_id_from_snapshot(payload: Mapping[str, Any]) -> Dict[int, List[Mapping[str, Any]]]:
    facts = payload.get("facts")
    fact_items = _mapping(facts).get("items")
    if not isinstance(fact_items, list):
        return {}
    out: Dict[int, List[Mapping[str, Any]]] = {}
    for item in fact_items:
        if not isinstance(item, Mapping):
            continue
        fact_id = _to_int(item.get("fact_id"))
        if fact_id is None:
            continue
        out.setdefault(fact_id, []).append(item)
    return out


def _load_fact_rows(conn: sqlite3.Connection, fact_ids: Sequence[int]) -> Dict[int, sqlite3.Row]:
    if not fact_ids:
        return {}
    placeholders = ",".join("?" for _ in fact_ids)
    rows = conn.execute(
        f"""
        SELECT id, slug, event_date, fact_as_of, created_at, fact_json
        FROM event_fact_history
        WHERE id IN ({placeholders})
        """,
        tuple(int(value) for value in fact_ids),
    ).fetchall()
    return {int(row["id"]): row for row in rows}


def _replay_resolved_values(replay: Mapping[str, Any]) -> Dict[str, List[str]]:
    resolved = _mapping(_mapping(replay.get("anchor_trace")).get("resolved"))
    lineage = _mapping(replay.get("lineage_consistency"))
    out = {
        "slug": _unique_text([replay.get("effective_slug"), resolved.get("slug")]),
        "event_date": _unique_text([replay.get("effective_event_date"), resolved.get("event_date")]),
        "fact_snapshot_ids": _unique_text(
            [resolved.get("fact_snapshot_id")] + list(lineage.get("fact_snapshot_ids") or [])
        ),
        "posterior_run_ids": _unique_text(
            [resolved.get("posterior_run_id")] + list(lineage.get("posterior_run_ids") or [])
        ),
        "source_event_ids": _unique_text(
            [replay.get("effective_source_event_id"), resolved.get("source_event_id")]
            + list(resolved.get("source_event_ids") or [])
        ),
        "source_observation_ids": _unique_text(
            [replay.get("effective_source_observation_id"), resolved.get("source_observation_id")]
            + list(resolved.get("source_observation_ids") or [])
        ),
        "signal_ids": _unique_text([replay.get("effective_signal_id"), resolved.get("signal_id")]),
    }
    return out


def _ui_expected_values_for_fact(
    *,
    fact_result: Mapping[str, Any],
    lineage_items: Sequence[Mapping[str, Any]],
) -> Dict[str, List[str]]:
    return {
        "slug": _unique_text([fact_result.get("slug")] + [item.get("slug") for item in lineage_items]),
        "event_date": _unique_text(
            [fact_result.get("event_date")] + [item.get("event_date") for item in lineage_items]
        ),
        "fact_snapshot_ids": _unique_text(
            [fact_result.get("fact_snapshot_id")] + [item.get("fact_snapshot_id") for item in lineage_items]
        ),
        "posterior_run_ids": _unique_text(
            [fact_result.get("posterior_run_id")] + [item.get("posterior_run_id") for item in lineage_items]
        ),
        "source_event_ids": _unique_text(
            list(fact_result.get("source_event_ids") or [])
            + [source_id for item in lineage_items for source_id in (item.get("source_event_ids") or [])]
        ),
        "source_observation_ids": _unique_text(
            list(fact_result.get("source_observation_ids") or [])
            + [source_id for item in lineage_items for source_id in (item.get("source_observation_ids") or [])]
        ),
        "signal_ids": _unique_text([item.get("signal_id") for item in lineage_items]),
    }


def _ui_replay_consistency_for_fact(
    *,
    fact_id: int,
    fact_result: Mapping[str, Any],
    lineage_items: Sequence[Mapping[str, Any]],
    replay: Mapping[str, Any],
) -> Dict[str, Any]:
    expected = _ui_expected_values_for_fact(fact_result=fact_result, lineage_items=lineage_items)
    resolved = _replay_resolved_values(replay)
    blockers: List[str] = []
    warnings: List[str] = []

    def require_present_and_contains(field: str, blocker_prefix: str) -> None:
        expected_values = expected.get(field) or []
        if not expected_values:
            return
        replay_values = resolved.get(field) or []
        if not replay_values:
            blockers.append(f"{blocker_prefix}_missing")
            return
        missing = [value for value in expected_values if value not in replay_values]
        if missing:
            blockers.append(f"{blocker_prefix}_mismatch")

    for field, prefix in (
        ("slug", "ui_replay_slug"),
        ("event_date", "ui_replay_event_date"),
        ("fact_snapshot_ids", "ui_replay_fact_snapshot_id"),
        ("posterior_run_ids", "ui_replay_posterior_run_id"),
        ("source_event_ids", "ui_replay_source_event_id"),
        ("source_observation_ids", "ui_replay_source_observation_id"),
    ):
        require_present_and_contains(field, prefix)

    expected_signal_ids = expected.get("signal_ids") or []
    replay_signal_ids = resolved.get("signal_ids") or []
    if expected_signal_ids and replay_signal_ids:
        missing_signal_ids = [value for value in expected_signal_ids if value not in replay_signal_ids]
        if missing_signal_ids:
            blockers.append("ui_replay_signal_id_mismatch")
    elif expected_signal_ids and not replay_signal_ids and int(replay.get("signal_count") or 0) > 0:
        blockers.append("ui_replay_signal_id_missing")
    elif expected_signal_ids and not replay_signal_ids:
        warnings.append("ui_replay_signal_id_not_applicable")

    replay_blockers = list(replay.get("blockers") or [])
    if replay_blockers:
        blockers.append("ui_replay_underlying_replay_blocked")
    return {
        "schema": "ui_replay_consistency_v1",
        "fact_id": int(fact_id),
        "ok": not blockers,
        "expected": expected,
        "resolved": resolved,
        "replay_ok": bool(replay.get("ok")),
        "replay_signal_count": int(replay.get("signal_count") or 0),
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
    }


def _aggregate_ui_replay_consistency(items: Sequence[Mapping[str, Any]], *, enabled: bool) -> Dict[str, Any]:
    blockers = sorted({blocker for item in items for blocker in (item.get("blockers") or [])})
    warnings = sorted({warning for item in items for warning in (item.get("warnings") or [])})
    return {
        "schema": "ui_replay_consistency_contract_v1",
        "enabled": bool(enabled),
        "read_only": True,
        "ok": not blockers,
        "checked_fact_count": len(items),
        "blockers": blockers,
        "warnings": warnings,
        "items": list(items),
        "guards": {
            "writes_archive_db": False,
            "fetches_external_weather_sources": False,
            "places_orders": False,
            "historical_backfill": False,
        },
    }


def _facts_items_from_payload(payload: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    facts = payload.get("facts")
    fact_items = _mapping(facts).get("items")
    if not isinstance(fact_items, list):
        return []
    return [item for item in fact_items if isinstance(item, Mapping)]


def _ui_section(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    sections = payload.get("ui_sections")
    sections_map = sections if isinstance(sections, Mapping) else {}
    section = sections_map.get(key)
    return section if isinstance(section, Mapping) else {}


def _item_ui_section(item: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    sections = item.get("ui_sections")
    sections_map = sections if isinstance(sections, Mapping) else {}
    section = sections_map.get(key)
    return section if isinstance(section, Mapping) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _desktop_first_screen_contract(payload: Mapping[str, Any], *, max_items_output: int = 20) -> Dict[str, Any]:
    blockers: List[str] = []
    warnings: List[str] = []
    items = _facts_items_from_payload(payload)
    first_item = items[0] if items else {}
    dashboard_sections = payload.get("ui_sections")
    dashboard = dashboard_sections if isinstance(dashboard_sections, Mapping) else {}
    panel_freshness = payload.get("panel_freshness")
    panel_map = panel_freshness if isinstance(panel_freshness, Mapping) else {}
    bayes_section = _item_ui_section(first_item, "bayes_posterior_ssot")
    physical_section = _item_ui_section(first_item, "physical_high_posterior")
    decision_section = _ui_section(payload, "decisions")

    snapshot_freshness_state = str(payload.get("snapshot_freshness_state") or "").strip()
    if not snapshot_freshness_state:
        blockers.append("desktop_first_screen_snapshot_freshness_missing")
    if not str(payload.get("snapshot_id") or "").strip():
        blockers.append("desktop_first_screen_snapshot_id_missing")
    if str(dashboard.get("schema") or "") != "ui_snapshot_dashboard_sections_v1":
        blockers.append("desktop_first_screen_dashboard_sections_missing")
    if str(dashboard.get("source") or "") != "/api/ui_snapshot":
        blockers.append("desktop_first_screen_dashboard_source_not_ui_snapshot")
    if dashboard and dashboard.get("read_only") is not True:
        blockers.append("desktop_first_screen_dashboard_not_read_only")
    for required_key in ("facts", "posterior", "decisions"):
        if not isinstance(dashboard.get(required_key), Mapping):
            blockers.append(f"desktop_first_screen_{required_key}_section_missing")
        if required_key not in panel_map:
            blockers.append(f"desktop_first_screen_{required_key}_panel_freshness_missing")

    if not items:
        blockers.append("desktop_first_screen_fact_item_missing")
    if not bayes_section:
        blockers.append("desktop_first_screen_bayes_posterior_ssot_section_missing")
    first_ssot = _posterior_ssot_fact_from_payload(first_item)
    is_sequential_v2 = str(first_ssot.get("schema") or "") == "sequential_temperature_posterior_ssot_v2"
    if is_sequential_v2:
        if str(physical_section.get("state") or "") != "available":
            blockers.append("desktop_first_screen_physical_high_posterior_section_missing")
        if physical_section.get("top_temperature_c") is None:
            blockers.append("desktop_first_screen_physical_high_top_temperature_missing")
        if _to_float(physical_section.get("top_probability")) is None:
            blockers.append("desktop_first_screen_physical_high_top_probability_missing")
        if str(physical_section.get("source") or "") != (
            "facts.items[].posterior_ssot_fact.physical_high_distribution"
        ):
            blockers.append("desktop_first_screen_physical_high_source_not_ssot")

    posterior_state = str(bayes_section.get("state") or "").strip()
    posterior_target = str(bayes_section.get("target_bucket") or "").strip()
    posterior_prob = _to_float(bayes_section.get("target_probability"))
    posterior_na_reason = str(bayes_section.get("na_reason") or "").strip()
    if bayes_section:
        if bayes_section.get("read_only") is not True:
            blockers.append("desktop_first_screen_posterior_section_not_read_only")
        if bool(bayes_section.get("frontend_recompute_allowed")):
            blockers.append("desktop_first_screen_frontend_recompute_allowed")
        if bool(bayes_section.get("decision_recompute_allowed")):
            blockers.append("desktop_first_screen_decision_recompute_allowed")
        if posterior_state == "available":
            if not posterior_target:
                blockers.append("desktop_first_screen_posterior_target_bucket_missing")
            if posterior_prob is None:
                blockers.append("desktop_first_screen_posterior_target_probability_missing")
            probability_field = str(bayes_section.get("target_probability_field") or "")
            if "posterior_ssot_fact" not in probability_field:
                blockers.append("desktop_first_screen_posterior_probability_field_not_ssot")
        elif not posterior_na_reason:
            blockers.append("desktop_first_screen_posterior_na_reason_missing")

    phase = _first_text(bayes_section.get("phase"), first_item.get("phase"))
    phase_reason = _first_text(bayes_section.get("phase_reason"), first_item.get("phase_reason"))
    if posterior_state == "available" and not phase:
        blockers.append("desktop_first_screen_phase_missing")
    if posterior_state == "available" and not phase_reason:
        blockers.append("desktop_first_screen_phase_reason_missing")

    hard_mask_reason = _first_text(bayes_section.get("hard_mask_reason"), first_item.get("hard_mask_reason"))
    if posterior_state == "available" and not hard_mask_reason:
        blockers.append("desktop_first_screen_hard_mask_reason_missing")

    decision_source = str(decision_section.get("source") or "")
    decision_state = str(decision_section.get("state") or "").strip()
    decision_na_reason = str(decision_section.get("na_reason") or "").strip()
    decision_stale_reason = str(decision_section.get("stale_reason") or "").strip()
    blocked_reasons = decision_section.get("blocked_reasons")
    blocked_rows = [row for row in blocked_reasons or [] if isinstance(row, Mapping)]
    latest_decisions = [row for row in decision_section.get("latest") or [] if isinstance(row, Mapping)]
    latest_reason_codes = [
        str(code or "").strip()
        for row in latest_decisions
        for code in (row.get("reason_codes") or [])
        if str(code or "").strip()
    ]
    if not decision_section:
        blockers.append("desktop_first_screen_decision_section_missing")
    else:
        if "auto_trade_decisions" not in decision_source:
            blockers.append("desktop_first_screen_decision_source_not_auto_trade_decisions")
        if not (blocked_rows or latest_reason_codes or decision_na_reason or decision_stale_reason or decision_state):
            blockers.append("desktop_first_screen_decision_blocker_or_na_reason_missing")

    return {
        "schema": "ui_snapshot_desktop_first_screen_contract_v1",
        "read_only": True,
        "ok": not blockers,
        "checked_item_count": 1 if first_item else 0,
        "snapshot": {
            "snapshot_id": str(payload.get("snapshot_id") or ""),
            "snapshot_freshness_state": snapshot_freshness_state,
            "snapshot_freshness_reason": str(payload.get("snapshot_freshness_reason") or ""),
            "panel_freshness_keys": sorted(str(key) for key in panel_map.keys()),
        },
        "posterior": {
            "state": posterior_state,
            "source": str(bayes_section.get("source") or ""),
            "target_bucket": posterior_target,
            "target_probability": posterior_prob,
            "target_probability_field": str(bayes_section.get("target_probability_field") or ""),
            "phase": phase,
            "phase_reason": phase_reason,
            "hard_mask_reason": hard_mask_reason,
            "na_reason": posterior_na_reason,
        },
        "decision_blocker": {
            "state": decision_state,
            "source": decision_source,
            "na_reason": decision_na_reason,
            "stale_reason": decision_stale_reason,
            "blocked_reason_count": len(blocked_rows),
            "latest_reason_codes": latest_reason_codes[:20],
        },
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "guards": {
            "writes_archive_db": False,
            "fetches_external_weather_sources": False,
            "places_orders": False,
            "frontend_recompute_allowed": False,
        },
        "items": [
            {
                "slug": str(first_item.get("slug") or ""),
                "event_date": str(first_item.get("event_date") or ""),
                "fact_id": _to_int(first_item.get("fact_id")),
                "fact_snapshot_id": _fact_snapshot_id_from_payload(first_item),
                "posterior_run_id": _posterior_run_id_from_payload(first_item),
            }
        ][: max(0, int(max_items_output))] if first_item else [],
    }


def audit_payload(
    payload: Mapping[str, Any],
    *,
    db_path: Path | str = DEFAULT_DB,
    run_replay: bool = False,
    since_hours: float = 72.0,
    replay_limit: int = 200,
    max_items_output: int = 50,
) -> Dict[str, Any]:
    db = Path(db_path)
    blockers: List[str] = []
    warnings: List[str] = []
    lineage_items = _lineage_items_from_snapshot(payload)
    source_fact_ids = _source_fact_ids_from_snapshot(payload, lineage_items)
    snapshot_id = str(payload.get("snapshot_id") or "").strip()
    lineage_state = _mapping(payload.get("lineage_state"))
    fact_items = _mapping(payload.get("facts")).get("items")
    fact_item_count = len(fact_items) if isinstance(fact_items, list) else 0

    if not snapshot_id:
        blockers.append("ui_snapshot_id_missing")
    if fact_item_count and not source_fact_ids:
        blockers.append("ui_snapshot_source_fact_ids_missing")
    if fact_item_count and lineage_state.get("placeholder") is True:
        blockers.append("ui_snapshot_lineage_placeholder_with_fact_items")

    item_by_fact_id: Dict[int, List[Mapping[str, Any]]] = {}
    for item in lineage_items:
        fact_id = _to_int(item.get("fact_id"))
        if fact_id is not None:
            item_by_fact_id.setdefault(fact_id, []).append(item)
    ui_fact_items_by_id = _fact_items_by_fact_id_from_snapshot(payload)

    fact_results: List[Dict[str, Any]] = []
    replay_results: List[Dict[str, Any]] = []
    ui_replay_consistency_items: List[Dict[str, Any]] = []
    with _connect_readonly(db) as conn:
        rows_by_id = _load_fact_rows(conn, source_fact_ids)
        for fact_id in source_fact_ids:
            row = rows_by_id.get(int(fact_id))
            if row is None:
                blockers.append("ui_snapshot_fact_id_not_found")
                fact_results.append({"fact_id": fact_id, "ok": False, "blocker": "ui_snapshot_fact_id_not_found"})
                continue
            fact_json = _safe_json(row["fact_json"])
            fact_payload = fact_json if isinstance(fact_json, Mapping) else {}
            actual_fact_snapshot_id = _fact_snapshot_id_from_payload(fact_payload)
            actual_posterior_run_id = _posterior_run_id_from_payload(fact_payload)
            actual_source_event_ids = _source_event_ids_from_payload(fact_payload)
            actual_source_observation_ids = _source_observation_ids_from_payload(fact_payload)
            expected_items = item_by_fact_id.get(int(fact_id), [])
            ui_fact_items = ui_fact_items_by_id.get(int(fact_id), [])
            display_only_ids = _display_only_source_ids_from_items(ui_fact_items)
            display_only_source_event_ids = set(display_only_ids.get("source_event_ids") or [])
            display_only_source_observation_ids = set(display_only_ids.get("source_observation_ids") or [])
            item_blockers: List[str] = []
            item_warnings: List[str] = []
            if not expected_items:
                item_blockers.append("ui_snapshot_lineage_item_missing_for_fact_id")
            for item in expected_items:
                expected_snapshot_id = str(item.get("fact_snapshot_id") or "").strip()
                expected_posterior_run_id = str(item.get("posterior_run_id") or "").strip()
                expected_source_event_ids = _unique_text(item.get("source_event_ids") or [])
                expected_source_observation_ids = _unique_text(item.get("source_observation_ids") or [])
                expected_source_event_ids = [
                    source_event_id
                    for source_event_id in expected_source_event_ids
                    if source_event_id not in display_only_source_event_ids
                ]
                expected_source_observation_ids = [
                    source_observation_id
                    for source_observation_id in expected_source_observation_ids
                    if source_observation_id not in display_only_source_observation_ids
                ]
                expected_slug = str(item.get("slug") or "").strip()
                expected_event_date = str(item.get("event_date") or "").strip()
                if expected_slug and expected_slug != str(row["slug"] or ""):
                    item_blockers.append("ui_snapshot_slug_mismatch")
                if expected_event_date and expected_event_date != str(row["event_date"] or ""):
                    item_blockers.append("ui_snapshot_event_date_mismatch")
                if actual_fact_snapshot_id and not expected_snapshot_id:
                    item_blockers.append("ui_snapshot_fact_snapshot_id_missing")
                if expected_snapshot_id and not actual_fact_snapshot_id:
                    item_blockers.append("fact_json_fact_snapshot_id_missing")
                if expected_snapshot_id and actual_fact_snapshot_id and expected_snapshot_id != actual_fact_snapshot_id:
                    item_blockers.append("ui_snapshot_fact_snapshot_id_mismatch")
                if expected_posterior_run_id and actual_posterior_run_id and expected_posterior_run_id != actual_posterior_run_id:
                    item_blockers.append("ui_snapshot_posterior_run_id_mismatch")
                if expected_posterior_run_id and not actual_posterior_run_id:
                    item_blockers.append("fact_json_posterior_run_id_missing")
                if actual_posterior_run_id and not expected_posterior_run_id:
                    item_warnings.append("ui_snapshot_posterior_run_id_missing")
                if actual_source_event_ids and not expected_source_event_ids:
                    item_blockers.append("ui_snapshot_source_event_ids_missing")
                unexpected_source_ids = [
                    source_event_id
                    for source_event_id in expected_source_event_ids
                    if source_event_id not in actual_source_event_ids
                ]
                if unexpected_source_ids:
                    item_blockers.append("ui_snapshot_source_event_id_not_in_fact_json")
                if actual_source_observation_ids and not expected_source_observation_ids:
                    item_blockers.append("ui_snapshot_source_observation_ids_missing")
                unexpected_source_observation_ids = [
                    source_observation_id
                    for source_observation_id in expected_source_observation_ids
                    if source_observation_id not in actual_source_observation_ids
                ]
                if unexpected_source_observation_ids:
                    item_blockers.append("ui_snapshot_source_observation_id_not_in_fact_json")
            fact_ssot = _posterior_ssot_fact_from_payload(fact_payload)
            if fact_ssot and str(fact_ssot.get("schema") or "") in {
                "bayes_posterior_ssot_fact_v1",
                "sequential_temperature_posterior_ssot_v2",
            }:
                if not ui_fact_items:
                    item_blockers.append("ui_snapshot_fact_item_missing_for_posterior_ssot_fact")
                for ui_item in ui_fact_items:
                    item_blockers.extend(
                        _posterior_ssot_lineage_issues(
                            ui_item=ui_item,
                            fact_payload=fact_payload,
                        )
                    )
            for blocker in item_blockers:
                _append_unique(blockers, blocker)
            for warning in item_warnings:
                _append_unique(warnings, warning)
            fact_results.append(
                {
                    "fact_id": fact_id,
                    "slug": str(row["slug"] or ""),
                    "event_date": str(row["event_date"] or ""),
                    "fact_snapshot_id": actual_fact_snapshot_id,
                    "posterior_run_id": actual_posterior_run_id,
                    "source_event_ids": actual_source_event_ids[:20],
                    "source_observation_ids": actual_source_observation_ids[:20],
                    "ok": not item_blockers,
                    "blockers": sorted(set(item_blockers)),
                    "warnings": sorted(set(item_warnings)),
                }
            )

    if run_replay and source_fact_ids:
        from scripts import ssot_chain_replay_audit

        for fact_id in source_fact_ids[: max(1, int(max_items_output))]:
            replay = ssot_chain_replay_audit.audit(
                db_path=db,
                fact_id=str(fact_id),
                since_hours=float(since_hours),
                limit=int(replay_limit),
                max_signals_output=0,
            )
            replay_blockers = list(replay.get("blockers") or [])
            replay_warnings = list(replay.get("warnings") or [])
            for blocker in replay_blockers:
                _append_unique(blockers, f"replay:{blocker}")
            for warning in replay_warnings:
                _append_unique(warnings, f"replay:{warning}")
            replay_results.append(
                {
                    "fact_id": fact_id,
                    "ok": bool(replay.get("ok")),
                    "signal_count": int(replay.get("signal_count") or 0),
                    "blockers": replay_blockers,
                    "warnings": replay_warnings,
                    "source_counts": replay.get("source_counts") or {},
                }
            )
            fact_result = next((item for item in fact_results if int(item.get("fact_id") or -1) == int(fact_id)), {})
            consistency = _ui_replay_consistency_for_fact(
                fact_id=int(fact_id),
                fact_result=fact_result,
                lineage_items=item_by_fact_id.get(int(fact_id), []),
                replay=replay,
            )
            ui_replay_consistency_items.append(consistency)
            for blocker in consistency.get("blockers") or []:
                _append_unique(blockers, blocker)
            for warning in consistency.get("warnings") or []:
                _append_unique(warnings, warning)

    output_limit = max(0, int(max_items_output))
    ui_replay_consistency = _aggregate_ui_replay_consistency(
        ui_replay_consistency_items,
        enabled=bool(run_replay),
    )
    desktop_first_screen = _desktop_first_screen_contract(
        payload,
        max_items_output=max_items_output,
    )
    for blocker in desktop_first_screen.get("blockers") or []:
        _append_unique(blockers, blocker)
    for warning in desktop_first_screen.get("warnings") or []:
        _append_unique(warnings, warning)
    return {
        "ok": not blockers,
        "schema": "ui_snapshot_lineage_audit_v1",
        "read_only": True,
        "db": str(db),
        "snapshot": {
            "snapshot_id": snapshot_id,
            "snapshot_etag": str(payload.get("snapshot_etag") or ""),
            "run_date": str(payload.get("run_date") or ""),
            "snapshot_freshness_state": str(payload.get("snapshot_freshness_state") or ""),
            "lineage_state_schema": str(lineage_state.get("schema") or ""),
            "lineage_placeholder": bool(lineage_state.get("placeholder")),
            "fact_lineage_complete": bool(lineage_state.get("fact_lineage_complete")),
            "complete": bool(lineage_state.get("complete")),
        },
        "counts": {
            "fact_items": fact_item_count,
            "lineage_items": len(lineage_items),
            "source_fact_ids": len(source_fact_ids),
            "checked_facts": len(fact_results),
            "replayed_facts": len(replay_results),
        },
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "fact_results": fact_results[:output_limit] if output_limit else [],
        "fact_results_truncated": len(fact_results) > output_limit,
        "replay_results": replay_results[:output_limit] if output_limit else [],
        "ui_replay_consistency": {
            **ui_replay_consistency,
            "items": ui_replay_consistency_items[:output_limit] if output_limit else [],
            "items_truncated": len(ui_replay_consistency_items) > output_limit,
        },
        "desktop_first_screen": {
            **desktop_first_screen,
            "items": list(desktop_first_screen.get("items") or [])[:output_limit] if output_limit else [],
            "items_truncated": len(desktop_first_screen.get("items") or []) > output_limit,
        },
        "next_boundary": "wire_ui_snapshot_lineage_audit_into_daily_review_or_browser_qa",
    }


def load_ui_snapshot(
    *,
    mode: str,
    run_date: str,
    limit: int,
    city_filter: str = "",
    force_rebuild: bool = False,
) -> Dict[str, Any]:
    import ui.server as server

    if mode == "summary":
        return server.ui_snapshot_summary(
            run_date=run_date,
            limit=limit,
            force_rebuild=force_rebuild,
            city_filter=city_filter,
        )
    return server.ui_snapshot(run_date=run_date, limit=limit, force_rebuild=force_rebuild)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--mode", choices=("full", "summary"), default="full")
    parser.add_argument("--run-date", default="")
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--city-filter", default="")
    parser.add_argument("--force-rebuild", action="store_true")
    parser.add_argument("--payload-json", type=Path, default=None)
    parser.add_argument("--run-replay", action="store_true")
    parser.add_argument("--since-hours", type=float, default=72.0)
    parser.add_argument("--replay-limit", type=int, default=200)
    parser.add_argument("--max-items-output", type=int, default=50)
    args = parser.parse_args(argv)

    if args.payload_json:
        payload = _safe_json(args.payload_json.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            print(json.dumps({"ok": False, "error": "payload_json_not_object"}, ensure_ascii=False, indent=2))
            return 2
    else:
        payload = load_ui_snapshot(
            mode=str(args.mode),
            run_date=str(args.run_date or ""),
            limit=int(args.limit),
            city_filter=str(args.city_filter or ""),
            force_rebuild=bool(args.force_rebuild),
        )
    result = audit_payload(
        payload,
        db_path=args.db,
        run_replay=bool(args.run_replay),
        since_hours=float(args.since_hours),
        replay_limit=int(args.replay_limit),
        max_items_output=int(args.max_items_output),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
