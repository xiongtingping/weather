#!/usr/bin/env python3
"""Fail-closed audit for documentation authority and code/document identity alignment."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
REQUIRED_KEYS = {"status", "authority", "owner", "effective_from"}
ALLOWED_STATUS = {"accepted", "research", "superseded", "archived"}
ALLOWED_AUTHORITY = {"L0", "L1", "L2", "L3"}
RETIRED_RUNTIME_FILES = {
    "hierarchical_temperature_posterior_runtime.py",
    "ncei_zuuu_synoptic_v3_runtime.py",
    "ncei_zuuu_deduplicated_shadow_runtime.py",
}
FORBIDDEN_NORMATIVE_IDENTITIES = {
    "sequential_remaining_warming_bayes_runtime_v2",
    "v2_sequential_fallback",
    "v2_sequential_state_fallback",
    "v3_gfs_synoptic_live",
    "build_v3_synoptic_posterior",
    "生产概率身份只有一个：`joint_weak_physical_strong_composer_v1`",
    "唯一 live composer 为 `joint_weak_physical_strong_composer_v1`",
    "当前生产概率模型仍为 v3.1-only",
    "候选必须携带 v3.1 composer",
    "当前 joint composer 保持唯一生产概率链",
    "现有 joint production 保持唯一权威",
    "# 目标架构 v2（Shadow-Only）",
}
LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def _front_matter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    values: dict[str, Any] = {}
    for raw in text[4:end].splitlines():
        if not raw or raw[:1].isspace() or ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        values[key.strip()] = value.strip()
    return values, text[end + 5 :]


def build_audit(root: Path = ROOT) -> dict[str, Any]:
    repo = Path(root).resolve()
    docs = repo / "docs"
    violations: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for path in sorted(docs.rglob("*.md")):
        rel = path.relative_to(repo).as_posix()
        text = path.read_text(encoding="utf-8")
        meta, body = _front_matter(text)
        missing = sorted(REQUIRED_KEYS - set(meta))
        if missing:
            violations.append({"code": "document_metadata_missing", "path": rel, "details": missing})
        status = str(meta.get("status") or "")
        authority = str(meta.get("authority") or "")
        if status and status not in ALLOWED_STATUS:
            violations.append({"code": "document_status_invalid", "path": rel, "details": status})
        if authority and authority not in ALLOWED_AUTHORITY:
            violations.append({"code": "document_authority_invalid", "path": rel, "details": authority})
        if status == "accepted" and authority in {"L0", "L1"}:
            hits = sorted(token for token in FORBIDDEN_NORMATIVE_IDENTITIES if token in body)
            if hits:
                violations.append({"code": "retired_identity_in_normative_document", "path": rel, "details": hits})
        for target in LINK_RE.findall(body):
            target_text = str(target).strip()
            if not target_text or target_text.startswith(("http://", "https://", "#", "mailto:")):
                continue
            clean = target_text.split("#", 1)[0]
            if clean and not (path.parent / clean).resolve().exists():
                violations.append({"code": "document_link_target_missing", "path": rel, "details": target_text})
        rows.append({"path": rel, "status": status, "authority": authority})

    for retired in sorted(RETIRED_RUNTIME_FILES):
        if (repo / retired).exists():
            violations.append({"code": "retired_runtime_file_present", "path": retired, "details": {}})
    successor_contract_path = repo / "production_probability_successor_activation_contract.py"
    if successor_contract_path.exists():
        successor_contract = successor_contract_path.read_text(encoding="utf-8")
        required_successor_tokens = (
            "p0_prior_z_weak_once_then_canonical_cumulative_evidence_replacement_delta_log_lr_",
            'COMPOSER_SCHEMA = "sequential_bayes_filter_reported_x_v5_cumulative_replacement"',
            'PUBLIC_SSOT_SCHEMA = "sequential_temperature_posterior_ssot_v3_reported_x"',
        )
        missing_successor_tokens = [
            token for token in required_successor_tokens if token not in successor_contract
        ]
        if missing_successor_tokens:
            violations.append(
                {
                    "code": "reported_x_v5_identity_not_bound_in_code",
                    "path": "production_probability_successor_activation_contract.py",
                    "details": missing_successor_tokens,
                }
            )

    return {
        "schema": "document_authority_audit_v1",
        "ok": not violations,
        "read_only": True,
        "document_count": len(rows),
        "documents": rows,
        "violations": violations,
        "guards": {
            "writes_files": False,
            "writes_archive_db": False,
            "places_orders": False,
            "restarts_services": False,
        },
    }


def main() -> int:
    report = build_audit()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
