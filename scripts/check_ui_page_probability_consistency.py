#!/usr/bin/env python3
"""Read-only audit for same-page temperature probability consistency.

The page should render probability fields from one SSOT snapshot. This audit
checks that distribution rows, compact CTP bucket probabilities, target bucket
fields, target outlook, path distribution, and capital plan all agree.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_URL = "http://127.0.0.1:3002/api/ui_snapshot_summary?limit=24&refresh=1"
DEFAULT_TOLERANCE = 5e-4


def _to_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _prob(value: Any, *, pct: bool = False) -> float | None:
    val = _to_float(value)
    if val is None:
        return None
    if pct:
        val /= 100.0
    elif 1.0 < val <= 100.0:
        # Keep the audit tolerant of legacy fields that accidentally store pct.
        val /= 100.0
    return max(0.0, min(1.0, float(val))) if math.isfinite(float(val)) else None


def _label(value: Any) -> str:
    return str(value or "").strip()


def _bucket_label(row: Mapping[str, Any]) -> str:
    return _label(
        row.get("outcome")
        or row.get("bucket")
        or row.get("target_bucket")
        or row.get("label")
        or row.get("name")
    )


def _identity(item: Mapping[str, Any], index: int) -> dict[str, Any]:
    return {
        "index": index,
        "slug": item.get("slug"),
        "event_date": item.get("event_date"),
        "run_date": item.get("run_date"),
    }


def _items_from_snapshot(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    facts = payload.get("facts")
    if isinstance(facts, Mapping) and isinstance(facts.get("items"), list):
        return [item for item in facts.get("items") or [] if isinstance(item, Mapping)]
    if isinstance(payload.get("items"), list):
        return [item for item in payload.get("items") or [] if isinstance(item, Mapping)]
    return []


def _snapshot_run_date(payload: Mapping[str, Any]) -> str:
    facts = payload.get("facts") if isinstance(payload.get("facts"), Mapping) else {}
    return _label(payload.get("run_date") or facts.get("run_date")).split("T", 1)[0]


def _snapshot_source(payload: Mapping[str, Any]) -> str:
    facts = payload.get("facts") if isinstance(payload.get("facts"), Mapping) else {}
    return _label(facts.get("source") or payload.get("snapshot_source") or payload.get("source"))


def _snapshot_is_rebuilding(payload: Mapping[str, Any]) -> bool:
    source = _snapshot_source(payload)
    return bool(
        payload.get("busy") is True
        or payload.get("cache_rebuild_in_flight") is True
        or source == "ui_snapshot_rebuild_in_flight"
    )


def _snapshot_is_transient(payload: Mapping[str, Any]) -> bool:
    return bool(
        _snapshot_is_rebuilding(payload)
        or payload.get("cache_stale") is True
        or payload.get("cache_rebuild_started") is True
    )


def _distribution_probability_map(item: Mapping[str, Any]) -> tuple[dict[str, float], str, list[str]]:
    rows = item.get("distribution")
    if not isinstance(rows, list) or not rows:
        return {}, "", ["distribution_missing"]
    out: dict[str, float] = {}
    warnings: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            continue
        label = _bucket_label(row)
        if not label:
            warnings.append(f"distribution[{index}].label_missing")
            continue
        prob = _prob(row.get("q_temperature_bucket_posterior"))
        source = "distribution.q_temperature_bucket_posterior"
        if prob is None:
            prob = _prob(row.get("q_bayes_posterior"))
            source = "distribution.q_bayes_posterior"
        if prob is None:
            prob = _prob(row.get("posterior_temperature_distribution_probability"))
            source = "distribution.posterior_temperature_distribution_probability"
        if prob is None:
            warnings.append(f"distribution[{index}].posterior_probability_missing")
            continue
        out[label] = prob
    if not out:
        warnings.append("distribution_probability_map_empty")
        return {}, "", warnings
    return out, source, warnings


def _ctp_probability_map(item: Mapping[str, Any]) -> tuple[dict[str, float], str]:
    posterior = item.get("posterior_temperature_distribution")
    if not isinstance(posterior, Mapping):
        return {}, ""
    rows = posterior.get("bucket_probabilities")
    if not isinstance(rows, list) or not rows:
        return {}, ""
    out: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        label = _bucket_label(row)
        prob = _prob(row.get("probability"))
        if prob is None:
            prob = _prob(row.get("probability_pct"), pct=True)
        if label and prob is not None:
            out[label] = prob
    source = _label(posterior.get("ssot_source") or "posterior_temperature_distribution.bucket_probabilities")
    return out, source


def _canonical_probability_map(item: Mapping[str, Any]) -> tuple[dict[str, float], str, list[str]]:
    dist_map, dist_source, warnings = _distribution_probability_map(item)
    if dist_map:
        return dist_map, dist_source or "distribution.q_temperature_bucket_posterior", warnings
    ctp_map, ctp_source = _ctp_probability_map(item)
    if ctp_map:
        return ctp_map, ctp_source or "posterior_temperature_distribution.bucket_probabilities", warnings
    return {}, "", warnings


def _top_buckets(source_map: Mapping[str, float], limit: int = 3) -> list[dict[str, Any]]:
    ranked = sorted(source_map.items(), key=lambda item: (-float(item[1]), item[0]))
    return [
        {"bucket": label, "probability": prob, "probability_pct": prob * 100.0}
        for label, prob in ranked[: max(0, int(limit or 0))]
    ]


def _append_mismatch(
    mismatches: list[dict[str, Any]],
    *,
    field: str,
    bucket: str,
    actual: Any,
    expected: Any,
    expected_source: str,
    code: str = "probability_mismatch",
) -> None:
    actual_prob = actual if isinstance(actual, (float, int)) else _to_float(actual)
    expected_prob = expected if isinstance(expected, (float, int)) else _to_float(expected)
    diff = (
        float(actual_prob) - float(expected_prob)
        if actual_prob is not None and expected_prob is not None
        else None
    )
    mismatches.append(
        {
            "code": code,
            "field": field,
            "bucket": bucket,
            "actual": actual,
            "expected": expected,
            "diff": diff,
            "diff_pp": diff * 100.0 if diff is not None else None,
            "expected_source": expected_source,
        }
    )


def _compare_probability(
    container: Mapping[str, Any],
    source_map: Mapping[str, float],
    *,
    label_field: str,
    prob_field: str,
    prefix: str,
    source_name: str,
    tolerance: float,
    mismatches: list[dict[str, Any]],
    pct: bool = False,
    fallback_bucket: Any = None,
) -> int:
    if prob_field not in container:
        return 0
    bucket = _label(container.get(label_field) or fallback_bucket)
    actual = _prob(container.get(prob_field), pct=pct)
    if actual is None:
        return 0
    if not bucket:
        _append_mismatch(
            mismatches,
            field=f"{prefix}.{prob_field}",
            bucket="",
            actual=actual,
            expected="bucket_label_required",
            expected_source=source_name,
            code="bucket_label_missing",
        )
        return 1
    if bucket not in source_map:
        _append_mismatch(
            mismatches,
            field=f"{prefix}.{prob_field}",
            bucket=bucket,
            actual=actual,
            expected="bucket_present_in_page_probability_map",
            expected_source=source_name,
            code="bucket_missing_from_canonical_map",
        )
        return 1
    expected = float(source_map[bucket])
    if abs(float(actual) - expected) > tolerance:
        _append_mismatch(
            mismatches,
            field=f"{prefix}.{prob_field}",
            bucket=bucket,
            actual=float(actual),
            expected=expected,
            expected_source=source_name,
        )
    return 1


def _compare_top_probability(
    container: Mapping[str, Any],
    source_map: Mapping[str, float],
    *,
    label_field: str,
    prob_field: str,
    prefix: str,
    source_name: str,
    tolerance: float,
    mismatches: list[dict[str, Any]],
    pct: bool = False,
) -> int:
    if label_field not in container and prob_field not in container:
        return 0
    top = _top_buckets(source_map, 1)
    if not top:
        return 0
    expected_label = str(top[0]["bucket"])
    actual_label = _label(container.get(label_field))
    checked = 0
    if actual_label:
        checked += 1
        if actual_label != expected_label:
            _append_mismatch(
                mismatches,
                field=f"{prefix}.{label_field}",
                bucket=actual_label,
                actual=actual_label,
                expected=expected_label,
                expected_source=source_name,
                code="top_bucket_label_mismatch",
            )
    checked += _compare_probability(
        container,
        source_map,
        label_field=label_field,
        prob_field=prob_field,
        prefix=prefix,
        source_name=source_name,
        tolerance=tolerance,
        mismatches=mismatches,
        pct=pct,
        fallback_bucket=expected_label,
    )
    return checked


def _check_distribution_rows(
    item: Mapping[str, Any],
    source_map: Mapping[str, float],
    *,
    source_name: str,
    tolerance: float,
    mismatches: list[dict[str, Any]],
) -> int:
    rows = item.get("distribution")
    if not isinstance(rows, list):
        return 0
    checked = 0
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            continue
        label = _bucket_label(row)
        if not label or label not in source_map:
            continue
        for field in (
            "q_temperature_bucket_posterior",
            "q_bayes_posterior",
            "posterior_temperature_distribution_probability",
            "target_bucket_probability",
        ):
            if field not in row:
                continue
            actual = _prob(row.get(field))
            if actual is None:
                continue
            checked += 1
            expected = float(source_map[label])
            if abs(float(actual) - expected) > tolerance:
                _append_mismatch(
                    mismatches,
                    field=f"distribution[{index}].{field}",
                    bucket=label,
                    actual=float(actual),
                    expected=expected,
                    expected_source=source_name,
                    code="distribution_probability_mismatch",
                )
    return checked


def _check_ctp_bucket_probabilities(
    item: Mapping[str, Any],
    source_map: Mapping[str, float],
    *,
    source_name: str,
    tolerance: float,
    mismatches: list[dict[str, Any]],
) -> int:
    posterior = item.get("posterior_temperature_distribution")
    rows = posterior.get("bucket_probabilities") if isinstance(posterior, Mapping) else None
    if not isinstance(rows, list):
        return 0
    checked = 0
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            continue
        label = _bucket_label(row)
        actual = _prob(row.get("probability"))
        if actual is None:
            actual = _prob(row.get("probability_pct"), pct=True)
        if not label or actual is None:
            continue
        checked += 1
        if label not in source_map:
            _append_mismatch(
                mismatches,
                field=f"posterior_temperature_distribution.bucket_probabilities[{index}].probability",
                bucket=label,
                actual=float(actual),
                expected="bucket_present_in_page_probability_map",
                expected_source=source_name,
                code="bucket_missing_from_canonical_map",
            )
            continue
        expected = float(source_map[label])
        if abs(float(actual) - expected) > tolerance:
            _append_mismatch(
                mismatches,
                field=f"posterior_temperature_distribution.bucket_probabilities[{index}].probability",
                bucket=label,
                actual=float(actual),
                expected=expected,
                expected_source=source_name,
                code="ctp_probability_mismatch",
            )
    return checked


def _check_common_target_container(
    container: Mapping[str, Any],
    source_map: Mapping[str, float],
    *,
    prefix: str,
    source_name: str,
    tolerance: float,
    mismatches: list[dict[str, Any]],
) -> int:
    checked = 0
    pairs: Sequence[tuple[str, str, bool]] = (
        ("target_bucket", "target_probability", False),
        ("target_bucket", "target_probability_pct", True),
        ("target_bucket", "target_bucket_probability", False),
        ("target_bucket", "target_bucket_probability_pct", True),
        ("target_bucket", "target_final_probability", False),
        ("target_bucket", "target_final_probability_pct", True),
        ("final_bucket", "final_bucket_probability", False),
        ("final_bucket", "final_bucket_probability_pct", True),
        ("final_bucket", "final_bucket_posterior_probability_pct", True),
        ("posterior_final_bucket", "posterior_final_bucket_probability_pct", True),
        ("cumulative_posterior_final_bucket", "cumulative_posterior_final_bucket_probability_pct", True),
        ("second_target_bucket", "second_target_probability", False),
        ("second_target_bucket", "second_target_probability_pct", True),
        ("second_target_bucket", "second_target_final_probability", False),
        ("second_target_bucket", "second_target_final_probability_pct", True),
        ("second_target_bucket", "second_target_posterior", False),
        ("second_target_bucket", "second_target_posterior_pct", True),
        ("candidate_bucket", "candidate_probability", False),
        ("candidate_bucket", "candidate_probability_pct", True),
    )
    for label_field, prob_field, is_pct in pairs:
        checked += _compare_probability(
            container,
            source_map,
            label_field=label_field,
            prob_field=prob_field,
            prefix=prefix,
            source_name=source_name,
            tolerance=tolerance,
            mismatches=mismatches,
            pct=is_pct,
        )
    checked += _compare_top_probability(
        container,
        source_map,
        label_field="posterior_highest_bucket_label",
        prob_field="posterior_highest_probability_pct",
        prefix=prefix,
        source_name=source_name,
        tolerance=tolerance,
        mismatches=mismatches,
        pct=True,
    )
    return checked


def _check_target_selection(
    container: Mapping[str, Any],
    source_map: Mapping[str, float],
    *,
    prefix: str,
    source_name: str,
    tolerance: float,
    mismatches: list[dict[str, Any]],
) -> int:
    checked = _check_common_target_container(
        container,
        source_map,
        prefix=prefix,
        source_name=source_name,
        tolerance=tolerance,
        mismatches=mismatches,
    )
    row = container.get("target_row")
    if isinstance(row, Mapping):
        checked += _check_distribution_like_row(
            row,
            source_map,
            prefix=f"{prefix}.target_row",
            source_name=source_name,
            tolerance=tolerance,
            mismatches=mismatches,
        )
    watch_rows = container.get("watch_buckets")
    if isinstance(watch_rows, list):
        for index, raw in enumerate(watch_rows):
            if isinstance(raw, Mapping):
                checked += _check_distribution_like_row(
                    raw,
                    source_map,
                    prefix=f"{prefix}.watch_buckets[{index}]",
                    source_name=source_name,
                    tolerance=tolerance,
                    mismatches=mismatches,
                )
    return checked


def _check_distribution_like_row(
    row: Mapping[str, Any],
    source_map: Mapping[str, float],
    *,
    prefix: str,
    source_name: str,
    tolerance: float,
    mismatches: list[dict[str, Any]],
) -> int:
    label = _bucket_label(row)
    checked = 0
    for field, is_pct in (
        ("q_temperature_bucket_posterior", False),
        ("q_bayes_posterior", False),
        ("posterior", False),
        ("posterior_pct", True),
        ("probability", False),
        ("probability_pct", True),
        ("target_bucket_probability", False),
        ("target_bucket_probability_pct", True),
    ):
        if field not in row:
            continue
        checked += _compare_probability(
            row,
            source_map,
            label_field="outcome",
            prob_field=field,
            prefix=prefix,
            source_name=source_name,
            tolerance=tolerance,
            mismatches=mismatches,
            pct=is_pct,
            fallback_bucket=label,
        )
    return checked


def _check_temperature_path_distribution(
    container: Mapping[str, Any],
    source_map: Mapping[str, float],
    *,
    prefix: str,
    source_name: str,
    tolerance: float,
    mismatches: list[dict[str, Any]],
) -> int:
    checked = 0
    pairs: Sequence[tuple[str, str, bool]] = (
        ("target_bucket", "target_bucket_posterior", False),
        ("target_bucket", "target_bucket_posterior_pct", True),
        ("second_target_bucket", "second_target_bucket_posterior", False),
        ("second_target_bucket", "second_target_bucket_posterior_pct", True),
    )
    for label_field, prob_field, is_pct in pairs:
        checked += _compare_probability(
            container,
            source_map,
            label_field=label_field,
            prob_field=prob_field,
            prefix=prefix,
            source_name=source_name,
            tolerance=tolerance,
            mismatches=mismatches,
            pct=is_pct,
        )
    for key in ("target", "second_target"):
        nested = container.get(key)
        if isinstance(nested, Mapping):
            checked += _check_distribution_like_row(
                nested,
                source_map,
                prefix=f"{prefix}.{key}",
                source_name=source_name,
                tolerance=tolerance,
                mismatches=mismatches,
            )
    rows = container.get("bucket_distribution")
    if isinstance(rows, list):
        for index, row in enumerate(rows):
            if isinstance(row, Mapping):
                checked += _check_distribution_like_row(
                    row,
                    source_map,
                    prefix=f"{prefix}.bucket_distribution[{index}]",
                    source_name=source_name,
                    tolerance=tolerance,
                    mismatches=mismatches,
                )
    return checked


def _check_capital_plan(
    container: Mapping[str, Any],
    source_map: Mapping[str, float],
    *,
    prefix: str,
    source_name: str,
    tolerance: float,
    mismatches: list[dict[str, Any]],
) -> int:
    checked = _check_common_target_container(
        container,
        source_map,
        prefix=prefix,
        source_name=source_name,
        tolerance=tolerance,
        mismatches=mismatches,
    )
    risk = container.get("ui_risk_explanation")
    if not isinstance(risk, Mapping):
        risk = container.get("risk_explanation")
    if isinstance(risk, Mapping):
        checked += _check_common_target_container(
            risk,
            source_map,
            prefix=f"{prefix}.ui_risk_explanation",
            source_name=source_name,
            tolerance=tolerance,
            mismatches=mismatches,
        )
    for list_key in ("legs", "buy_new_legs", "buy_old_reverse_legs"):
        rows = container.get(list_key)
        if isinstance(rows, list):
            for index, row in enumerate(rows):
                if isinstance(row, Mapping):
                    checked += _check_distribution_like_row(
                        row,
                        source_map,
                        prefix=f"{prefix}.{list_key}[{index}]",
                        source_name=source_name,
                        tolerance=tolerance,
                        mismatches=mismatches,
                    )
    for key in ("buy_new_leg", "buy_old_reverse_leg"):
        row = container.get(key)
        if isinstance(row, Mapping):
            checked += _check_distribution_like_row(
                row,
                source_map,
                prefix=f"{prefix}.{key}",
                source_name=source_name,
                tolerance=tolerance,
                mismatches=mismatches,
            )
    return checked


def audit_item(item: Mapping[str, Any], index: int, *, tolerance: float = DEFAULT_TOLERANCE) -> dict[str, Any]:
    source_map, source_name, warnings = _canonical_probability_map(item)
    identity = _identity(item, index)
    if not source_map:
        return {
            **identity,
            "ok": True,
            "skipped": True,
            "skip_reason": "posterior_probability_map_missing",
            "checked_fields": 0,
            "canonical_source": "",
            "top_buckets": [],
            "warnings": warnings,
            "mismatches": [],
        }

    mismatches: list[dict[str, Any]] = []
    checked_fields = 0
    checked_fields += _check_distribution_rows(
        item,
        source_map,
        source_name=source_name,
        tolerance=tolerance,
        mismatches=mismatches,
    )
    checked_fields += _check_ctp_bucket_probabilities(
        item,
        source_map,
        source_name=source_name,
        tolerance=tolerance,
        mismatches=mismatches,
    )
    checked_fields += _check_common_target_container(
        item,
        source_map,
        prefix="item",
        source_name=source_name,
        tolerance=tolerance,
        mismatches=mismatches,
    )

    for key in ("target_bucket_selection", "posterior_target_bucket_selection"):
        value = item.get(key)
        if isinstance(value, Mapping):
            checked_fields += _check_target_selection(
                value,
                source_map,
                prefix=key,
                source_name=source_name,
                tolerance=tolerance,
                mismatches=mismatches,
            )
    outlook = item.get("target_bucket_outlook")
    if isinstance(outlook, Mapping):
        checked_fields += _check_common_target_container(
            outlook,
            source_map,
            prefix="target_bucket_outlook",
            source_name=source_name,
            tolerance=tolerance,
            mismatches=mismatches,
        )
        rows = outlook.get("rows")
        if isinstance(rows, list):
            for row_index, row in enumerate(rows):
                if isinstance(row, Mapping):
                    checked_fields += _check_distribution_like_row(
                        row,
                        source_map,
                        prefix=f"target_bucket_outlook.rows[{row_index}]",
                        source_name=source_name,
                        tolerance=tolerance,
                        mismatches=mismatches,
                    )
    path = item.get("temperature_path_distribution")
    if isinstance(path, Mapping):
        checked_fields += _check_temperature_path_distribution(
            path,
            source_map,
            prefix="temperature_path_distribution",
            source_name=source_name,
            tolerance=tolerance,
            mismatches=mismatches,
        )
    plan = item.get("target_realization_capital_plan")
    if isinstance(plan, Mapping):
        checked_fields += _check_capital_plan(
            plan,
            source_map,
            prefix="target_realization_capital_plan",
            source_name=source_name,
            tolerance=tolerance,
            mismatches=mismatches,
        )
    switch = item.get("target_switch_context")
    if isinstance(switch, Mapping):
        checked_fields += _check_common_target_container(
            switch,
            source_map,
            prefix="target_switch_context",
            source_name=source_name,
            tolerance=tolerance,
            mismatches=mismatches,
        )

    summary = item.get("chengdu_intraday_summary")
    if isinstance(summary, Mapping):
        summary_selection = summary.get("target_bucket_selection")
        if isinstance(summary_selection, Mapping):
            checked_fields += _check_target_selection(
                summary_selection,
                source_map,
                prefix="chengdu_intraday_summary.target_bucket_selection",
                source_name=source_name,
                tolerance=tolerance,
                mismatches=mismatches,
            )
        summary_outlook = summary.get("target_bucket_outlook")
        if isinstance(summary_outlook, Mapping):
            checked_fields += _check_common_target_container(
                summary_outlook,
                source_map,
                prefix="chengdu_intraday_summary.target_bucket_outlook",
                source_name=source_name,
                tolerance=tolerance,
                mismatches=mismatches,
            )
        summary_path = summary.get("temperature_path_distribution")
        if isinstance(summary_path, Mapping):
            checked_fields += _check_temperature_path_distribution(
                summary_path,
                source_map,
                prefix="chengdu_intraday_summary.temperature_path_distribution",
                source_name=source_name,
                tolerance=tolerance,
                mismatches=mismatches,
            )

    return {
        **identity,
        "ok": not mismatches,
        "skipped": False,
        "checked_fields": checked_fields,
        "canonical_source": source_name,
        "top_buckets": _top_buckets(source_map, 3),
        "warnings": warnings,
        "mismatches": mismatches,
    }


def audit_payload(payload: Mapping[str, Any], *, source: str = "payload", tolerance: float = DEFAULT_TOLERANCE) -> dict[str, Any]:
    facts = payload.get("facts") if isinstance(payload.get("facts"), Mapping) else {}
    items = _items_from_snapshot(payload)
    audited = [audit_item(item, index, tolerance=tolerance) for index, item in enumerate(items)]
    mismatches: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    checked_items = 0
    skipped_items = 0
    checked_fields = 0
    for item_result in audited:
        index = int(item_result.get("index") or 0)
        identity = _identity(item_result, index)
        if item_result.get("skipped"):
            skipped_items += 1
        else:
            checked_items += 1
        checked_fields += int(item_result.get("checked_fields") or 0)
        for warning in item_result.get("warnings") or []:
            warnings.append({**identity, "warning": warning})
        for mismatch in item_result.get("mismatches") or []:
            mismatches.append({**identity, **mismatch})

    blockers: list[str] = []
    if mismatches:
        blockers.append("page_probability_mismatch")
    if not checked_items and not _snapshot_is_rebuilding(payload):
        blockers.append("no_probability_items_checked")

    return {
        "ok": not blockers,
        "temporary": _snapshot_is_transient(payload),
        "source": source,
        "snapshot_source": facts.get("source") or payload.get("source") or payload.get("snapshot_source"),
        "run_date": _snapshot_run_date(payload),
        "item_count": len(items),
        "checked_items": checked_items,
        "skipped_items": skipped_items,
        "checked_fields": checked_fields,
        "mismatch_count": len(mismatches),
        "blockers": blockers,
        "mismatches": mismatches,
        "warnings": warnings,
        "items": audited,
    }


def _fetch_runtime_snapshot(url: str, timeout: float) -> Mapping[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _url_with_run_date_and_limit(url: str, run_date: str, limit: int, refresh: bool) -> str:
    parsed = urllib.parse.urlparse(url)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    if run_date:
        query["run_date"] = run_date
    if limit:
        query["limit"] = str(limit)
    if refresh:
        query["refresh"] = "1"
    else:
        query.pop("refresh", None)
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))


def _fetch_source_snapshot(run_date: str, limit: int, *, force_rebuild: bool = False) -> Mapping[str, Any]:
    from ui import server

    return server.ui_snapshot_summary(run_date, limit, force_rebuild=force_rebuild)


def _runtime_audit_with_retries(
    url: str,
    *,
    timeout: float,
    tolerance: float,
    retries: int,
    retry_delay_sec: float,
    min_checked_items: int,
) -> dict[str, Any]:
    attempts = max(1, int(retries or 0) + 1)
    last_result: dict[str, Any] = {}
    for attempt in range(attempts):
        try:
            payload = _fetch_runtime_snapshot(url, timeout)
        except Exception as exc:
            last_result = {
                "ok": False,
                "temporary": True,
                "source": "runtime_api",
                "blockers": ["runtime_snapshot_fetch_failed"],
                "error": str(exc),
                "attempt": attempt + 1,
                "mismatch_count": 0,
                "mismatches": [],
                "warnings": [],
                "items": [],
            }
            if attempt < attempts - 1:
                time.sleep(max(0.0, float(retry_delay_sec)))
                continue
            return last_result
        result = audit_payload(payload, source="runtime_api", tolerance=tolerance)
        result["attempt"] = attempt + 1
        last_result = result
        checked_items = int(result.get("checked_items") or 0)
        if result.get("ok") and checked_items >= int(min_checked_items or 0):
            return result
        if attempt >= attempts - 1:
            if result.get("ok") and checked_items < int(min_checked_items or 0):
                result["ok"] = False
                blockers = list(result.get("blockers") or [])
                blockers.append("insufficient_probability_items_checked")
                result["blockers"] = blockers
                result["required_checked_items"] = int(min_checked_items or 0)
            return result
        if result.get("temporary") or result.get("mismatches") or checked_items < int(min_checked_items or 0):
            time.sleep(max(0.0, float(retry_delay_sec)))
            continue
        return result
    return last_result


def _summary_lines(result: Mapping[str, Any]) -> list[str]:
    blockers = ",".join(str(v) for v in (result.get("blockers") or [])) or "-"
    line = (
        f"ok={bool(result.get('ok'))} "
        f"source={result.get('source') or '-'} "
        f"snapshot_source={result.get('snapshot_source') or '-'} "
        f"run_date={result.get('run_date') or '-'} "
        f"items={int(result.get('item_count') or 0)} "
        f"checked_items={int(result.get('checked_items') or 0)} "
        f"checked_fields={int(result.get('checked_fields') or 0)} "
        f"mismatches={int(result.get('mismatch_count') or 0)} "
        f"blockers={blockers}"
    )
    lines = [line]
    if result.get("url"):
        lines.append(f"url={result.get('url')}")
    if result.get("error"):
        lines.append(f"error={result.get('error')}")
    mismatches = result.get("mismatches") or []
    if isinstance(mismatches, list):
        for mismatch in mismatches[:5]:
            if not isinstance(mismatch, Mapping):
                continue
            lines.append(
                "mismatch "
                f"slug={mismatch.get('slug') or '-'} "
                f"event_date={mismatch.get('event_date') or '-'} "
                f"field={mismatch.get('field') or '-'} "
                f"expected={mismatch.get('expected')} "
                f"actual={mismatch.get('actual')} "
                f"bucket={mismatch.get('bucket_label') or mismatch.get('bucket') or '-'}"
            )
        if len(mismatches) > 5:
            lines.append(f"mismatch_more={len(mismatches) - 5}")
    warnings = result.get("warnings") or []
    if isinstance(warnings, list) and warnings:
        lines.append(f"warnings={len(warnings)}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only audit that UI page probability fields are consistent within one SSOT snapshot."
    )
    parser.add_argument("--runtime", action="store_true", help="Check the running API instead of importing ui.server.")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--run-date", default="")
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--refresh", action="store_true", default=True)
    parser.add_argument("--no-refresh", dest="refresh", action="store_false")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=6)
    parser.add_argument("--retry-delay-sec", type=float, default=1.5)
    parser.add_argument("--min-checked-items", type=int, default=1)
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE)
    parser.add_argument("--force-rebuild", action="store_true", help="Only applies to source import mode.")
    parser.add_argument("--summary", action="store_true", help="Print a compact one-line summary instead of full JSON.")
    args = parser.parse_args()

    if args.runtime:
        url = _url_with_run_date_and_limit(args.url, args.run_date, args.limit, args.refresh)
        result = _runtime_audit_with_retries(
            url,
            timeout=args.timeout,
            tolerance=args.tolerance,
            retries=args.retries,
            retry_delay_sec=args.retry_delay_sec,
            min_checked_items=args.min_checked_items,
        )
        result["url"] = url
    else:
        payload = _fetch_source_snapshot(args.run_date, args.limit, force_rebuild=args.force_rebuild)
        result = audit_payload(payload, source="current_source_import", tolerance=args.tolerance)

    if args.summary:
        print("\n".join(_summary_lines(result)))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
