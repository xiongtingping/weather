#!/usr/bin/env python3
"""Audit that CTP v1 is the only runtime temperature posterior path."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"
REQUIRED_POSTERIOR_KEYS = {
    "grid",
    "cdf_values",
    "quantiles_c",
    "quantile_grid",
    "bucket_probabilities",
}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text()


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except Exception:
        return str(path)


def _function_body(text: str, function_name: str) -> str:
    start = text.find(f"def {function_name}(")
    if start < 0:
        return ""
    next_def = text.find("\ndef ", start + 1)
    if next_def < 0:
        return text[start:]
    return text[start:next_def]


def audit_additive_runtime_path() -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    path = ROOT / "posterior_pipeline_entry_runtime.py"
    text = _read(path)
    body = _function_body(text, "apply_bayesian_probability_update")
    forbidden_patterns = [
        ("legacy_source_dirichlet_bma", r"dirichlet_bma"),
        ("legacy_alpha_formula", r"alpha\s*=\s*\("),
        ("legacy_alpha_total_sum", r"alpha_total\s*=\s*max\(EPS,\s*sum\(alphas\)\)"),
        ("legacy_score_components_runtime", r"\"history\"\s*:\s*prior_strength\s*\*"),
    ]
    for code, pattern in forbidden_patterns:
        if re.search(pattern, body):
            issues.append(
                {
                    "code": code,
                    "file": str(path.relative_to(ROOT)),
                    "detail": "legacy additive evidence runtime marker remains in apply_bayesian_probability_update",
                }
            )
    if "_apply_hierarchical_bayesian_probability_update(" not in body:
        issues.append(
            {
                "code": "hierarchical_runtime_call_missing",
                "file": str(path.relative_to(ROOT)),
                "detail": "apply_bayesian_probability_update does not call the hierarchical runtime",
            }
        )
    return issues


def audit_ui_recompute() -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    path = ROOT / "ui" / "server.py"
    text = _read(path)
    suspicious = [
        "_refresh_target_bucket_outlook_from_current_distribution",
        "_sync_temperature_path_distribution_posterior_from_current_distribution",
        "q_bayes_posterior",
        "q_temperature_bucket_posterior",
    ]
    for token in suspicious:
        if token in text and "posterior_temperature_distribution" not in text:
            issues.append(
                {
                    "code": "ui_temperature_probability_recompute_or_legacy_read",
                    "file": str(path.relative_to(ROOT)),
                    "detail": f"UI references {token} without posterior_temperature_distribution contract",
                }
            )
            break
    return issues


def audit_fallback() -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    patterns = [
        r"PM_TEMPERATURE_POSTERIOR_ENGINE\s*=\s*fallback",
        r"PM_BAYES_ADDITIVE_EVIDENCE_POOL_ENABLED\s*=\s*1",
        r"dirichlet_bma[_a-z]*fallback",
        r"additive[_a-z\s-]*pool[_a-z\s-]*fallback",
        r"old[_a-z\s-]*additive[_a-z\s-]*fallback",
    ]
    for rel in ("posterior_pipeline_entry_runtime.py", "temperature_posterior_runtime.py", "ui/server.py"):
        path = ROOT / rel
        text = _read(path)
        for pattern in patterns:
            for match in re.finditer(pattern, text, flags=re.I):
                issues.append(
                    {
                        "code": "fallback_marker",
                        "file": rel,
                        "offset": match.start(),
                        "detail": f"CTP/additive fallback marker remains: {match.group(0)}",
                    }
                )
    return issues


def _json_obj(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def audit_latest_fact_ctp_coverage(db_path: Path = DEFAULT_DB) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    if not db_path.exists():
        issues.append(
            {
                "code": "archive_db_missing",
                "file": _rel(db_path),
                "detail": "cannot verify posterior_temperature_distribution coverage without archive DB",
            }
        )
        return issues
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT h.slug, h.event_date, h.fact_as_of, h.fact_json
            FROM event_fact_latest l
            JOIN event_fact_history h ON h.id = l.fact_id
            WHERE lower(h.slug) LIKE '%temperature%'
            ORDER BY h.event_date, h.slug
            """
        ).fetchall()
    except sqlite3.Error as exc:
        conn.close()
        return [
            {
                "code": "latest_fact_coverage_query_failed",
                "file": _rel(db_path),
                "detail": str(exc),
            }
        ]
    finally:
        try:
            conn.close()
        except Exception:
            pass
    checked = 0
    missing: List[Dict[str, Any]] = []
    for row in rows:
        payload = _json_obj(row["fact_json"])
        if not isinstance(payload.get("distribution"), list) or not payload.get("distribution"):
            continue
        checked += 1
        posterior = payload.get("posterior_temperature_distribution")
        if not isinstance(posterior, dict) or not REQUIRED_POSTERIOR_KEYS.issubset(posterior.keys()):
            missing.append(
                {
                    "slug": row["slug"],
                    "event_date": row["event_date"],
                    "fact_as_of": row["fact_as_of"],
                    "missing_keys": (
                        sorted(REQUIRED_POSTERIOR_KEYS - set(posterior.keys()))
                        if isinstance(posterior, dict)
                        else sorted(REQUIRED_POSTERIOR_KEYS)
                    ),
                }
            )
    if checked <= 0:
        issues.append(
            {
                "code": "no_temperature_latest_facts_checked",
                "file": _rel(db_path),
                "detail": "no latest temperature facts with distributions were found",
            }
        )
    if missing:
        issues.append(
            {
                "code": "posterior_temperature_distribution_coverage_incomplete",
                "file": _rel(db_path),
                "detail": f"{len(missing)}/{checked} latest temperature facts lack complete CTP distribution",
                "sample": missing[:10],
            }
        )
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fail-on-additive-runtime-path", action="store_true")
    parser.add_argument("--fail-on-ui-recompute", action="store_true")
    parser.add_argument("--fail-on-fallback", action="store_true")
    args = parser.parse_args()

    result: Dict[str, Any] = {
        "status": "ok",
        "issues": [],
        "checks": {
            "additive_runtime_path": bool(args.fail_on_additive_runtime_path),
            "ui_recompute": bool(args.fail_on_ui_recompute),
            "fallback": bool(args.fail_on_fallback),
            "latest_fact_ctp_coverage": True,
        },
    }
    if args.fail_on_additive_runtime_path:
        result["issues"].extend(audit_additive_runtime_path())
    if args.fail_on_ui_recompute:
        result["issues"].extend(audit_ui_recompute())
    if args.fail_on_fallback:
        result["issues"].extend(audit_fallback())
    result["issues"].extend(audit_latest_fact_ctp_coverage())
    if result["issues"]:
        result["status"] = "fail"
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if result["issues"] else 0


if __name__ == "__main__":
    sys.exit(main())
