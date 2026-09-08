#!/usr/bin/env python3
"""Read-only authority audit for the Bayes basket target doctrine."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json  # noqa: E402

DEFAULT_MANIFEST = ROOT / "config/bayes_basket_target/document_authority_v1.json"
DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_document_authority.json"
FORBIDDEN_MACHINE_STAGE = re.compile(r"P0-F(?:\^r|_r)?")
MAX_AUTHORITY_DOCUMENT_BYTES = 16 * 1024 * 1024


def _resolve(relative: str) -> Path:
    path = (ROOT / relative).resolve()
    if ROOT not in path.parents:
        raise ValueError("document_authority_path_outside_repository")
    return path


def _document_sha256(path: Path) -> str:
    size = int(path.stat().st_size)
    if size < 0 or size > MAX_AUTHORITY_DOCUMENT_BYTES:
        raise ValueError("document_authority_file_bytes_exceeded")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _field(text: str, name: str) -> str:
    patterns = (
        rf"(?m)^-\s*{re.escape(name)}[：:]\s*`?([^`\n]+?)`?\s*$",
        rf"(?m)^{re.escape(name)}[：:]\s*(\S+)\s*$",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    return ""


def _machine_text(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _machine_stage_contract(machine: Mapping[str, Any]) -> dict[str, bool]:
    stages = machine.get("probability_stages")
    rows = (
        [item for item in stages if isinstance(item, Mapping)]
        if isinstance(stages, list)
        else []
    )
    names = [str(item.get("stage") or "") for item in rows]
    by_name = {str(item.get("stage") or ""): item for item in rows}
    p0_text = _machine_text(by_name.get("P0", {}))
    p0m_text = _machine_text(by_name.get("P0-M", {}))
    p1_text = _machine_text(by_name.get("P1", {}))
    entire = _machine_text(machine)
    return {
        "machine_probability_stages_exact": names == ["P0", "P0-M", "P1"],
        "machine_forbids_p0f": FORBIDDEN_MACHINE_STAGE.search(entire) is None,
        "machine_p0_is_nwp_model_stage": (
            bool(p0_text)
            and any(token in p0_text for token in ("NWP", "ECMWF", "模型"))
            and "TAF" not in str(by_name.get("P0", {}).get("evidence") or "")
        ),
        "machine_p0m_is_atomic_taf_physical_confirmation": all(
            token in p0m_text
            for token in ("TAF", "TX", "云量", "降水", "风", "原子", "P0-M")
        ),
        "machine_p1_is_metar_speci_stage": (
            "METAR/SPECI" in p1_text and "P1" in p1_text
        ),
    }


def _markdown_stage_contract(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    return (
        any(
            "P0" in line and ("NWP" in line or "模型" in line)
            for line in lines
        )
        and any(
            "TAF" in line
            and "P0-M" in line
            and "云量" in line
            and "降水" in line
            and "风" in line
            for line in lines
        )
        and "METAR/SPECI" in text
        and "P1" in text
    )


def build_audit(manifest_path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    parsed = load_bounded_json(manifest_path)
    if not isinstance(parsed, Mapping):
        raise ValueError("document_authority_manifest_mapping_required")
    authorities = parsed.get("current_logical_authorities")
    if not isinstance(authorities, list):
        raise ValueError("document_authority_current_list_required")
    checks: dict[str, bool] = {
        "manifest_schema_valid": parsed.get("schema") == "bayes_basket_target_document_authority_v1",
        "exactly_one_current_logical_authority": len(authorities) == 1,
        "execution_mode_no_live": parsed.get("execution_mode") == "production_no_live",
        "live_submit_disabled": parsed.get("live_submit_authorized") is False,
        "gateway_disabled": parsed.get("calls_order_gateway") is False,
    }
    documents: list[dict[str, Any]] = []
    if len(authorities) == 1 and isinstance(authorities[0], Mapping):
        authority = authorities[0]
        markdown_path = _resolve(str(authority.get("markdown") or ""))
        json_path = _resolve(str(authority.get("json") or ""))
        markdown = markdown_path.read_text(encoding="utf-8")
        machine = load_bounded_json(json_path)
        machine = machine if isinstance(machine, Mapping) else {}
        markdown_version = _field(markdown, "版本")
        markdown_status = _field(markdown, "状态")
        expected_version = str(authority.get("version") or "")
        expected_status = str(authority.get("status") or "")
        authority_checks = {
            "markdown_present": markdown_path.is_file(),
            "json_present": json_path.is_file(),
            "markdown_version_matches": markdown_version == expected_version,
            "json_version_matches": str(machine.get("version") or "") == expected_version,
            "markdown_status_matches": markdown_status == expected_status,
            "json_status_matches": str(machine.get("status") or "") == expected_status,
            "machine_live_order_disabled": machine.get("live_order_authorized") is False,
            "markdown_stage_contract_present": _markdown_stage_contract(
                markdown
            ),
            **_machine_stage_contract(machine),
        }
        checks["authority_pair_consistent"] = all(authority_checks.values())
        documents.append({
            "document_id": str(authority.get("document_id") or ""),
            "markdown": str(markdown_path),
            "json": str(json_path),
            "version": expected_version,
            "status": expected_status,
            "markdown_sha256": _document_sha256(markdown_path),
            "json_sha256": _document_sha256(json_path),
            "checks": authority_checks,
        })
    else:
        checks["authority_pair_consistent"] = False

    roadmap = parsed.get("roadmap")
    if not isinstance(roadmap, Mapping):
        checks["roadmap_consistent"] = False
        roadmap_evidence: dict[str, Any] = {}
    else:
        roadmap_path = _resolve(str(roadmap.get("path") or ""))
        roadmap_json_path = _resolve(
            str(roadmap.get("json") or "")
        ) if roadmap.get("json") else None
        roadmap_text = roadmap_path.read_text(encoding="utf-8")
        roadmap_machine = (
            load_bounded_json(roadmap_json_path)
            if roadmap_json_path is not None
            else {}
        )
        roadmap_machine = (
            roadmap_machine
            if isinstance(roadmap_machine, Mapping)
            else {}
        )
        expected = str(roadmap.get("version") or "")
        roadmap_checks = {
            "present": roadmap_path.is_file(),
            "json_present": (
                roadmap_json_path is not None
                and roadmap_json_path.is_file()
            ),
            "version_matches": _field(roadmap_text, "version") == expected,
            "json_version_matches": (
                str(roadmap_machine.get("version") or "") == expected
            ),
            "status_matches": _field(roadmap_text, "status") == str(roadmap.get("status") or ""),
            "json_status_matches": (
                str(roadmap_machine.get("status") or "")
                == str(roadmap.get("status") or "")
            ),
            "architecture_reference_matches": (
                f"`bayes_basket_development_roadmap_v1_6.md`（v{expected}）"
                in (Path(documents[0]["markdown"]).read_text(encoding="utf-8") if documents else "")
            ),
            "markdown_stage_contract_present": _markdown_stage_contract(
                roadmap_text
            ),
            "machine_forbids_p0f": (
                FORBIDDEN_MACHINE_STAGE.search(
                    _machine_text(roadmap_machine)
                )
                is None
            ),
        }
        checks["roadmap_consistent"] = all(roadmap_checks.values())
        roadmap_evidence = {
            "path": str(roadmap_path),
            "json": str(roadmap_json_path or ""),
            "version": expected,
            "markdown_sha256": _document_sha256(roadmap_path),
            "json_sha256": (
                _document_sha256(roadmap_json_path)
                if roadmap_json_path is not None
                else ""
            ),
            "checks": roadmap_checks,
        }

    subordinate_evidence: list[dict[str, Any]] = []
    for raw_contract in parsed.get("subordinate_contracts") or []:
        if isinstance(raw_contract, Mapping):
            markdown_path = _resolve(
                str(raw_contract.get("markdown") or "")
            )
            json_path = _resolve(str(raw_contract.get("json") or ""))
            text = markdown_path.read_text(encoding="utf-8")
            machine = load_bounded_json(json_path)
            machine = machine if isinstance(machine, Mapping) else {}
            # Each file is pinned to ITS OWN version, because these pairs do
            # not carry the same one and never have.  The markdown is prose
            # and the json is a machine contract an order of magnitude larger
            # -- 23KB against 258KB for the basket formula -- authored by
            # different passes.  e9d31583 brought all four under hash tracking
            # on 2026-08-26 and pinned them to the json's number, which was
            # already three weeks and five patch versions behind the markdown
            # sitting beside it, so ``subordinate_contracts_safe`` has been
            # False since the day the check was written.  Demanding one number
            # could only be satisfied by typing a version into the json that
            # its content does not correspond to.  Two files, two pins, and
            # the gap between them reported rather than collapsed.
            expected_markdown_version = str(
                raw_contract.get("markdown_version")
                or raw_contract.get("version")
                or ""
            )
            expected_json_version = str(
                raw_contract.get("json_version")
                or raw_contract.get("version")
                or ""
            )
            expected_status = str(raw_contract.get("status") or "")
            markdown_version = _field(text, "version")
            json_version = str(machine.get("version") or "")
            subordinate_evidence.append({
                "path": str(markdown_path),
                "json": str(json_path),
                "markdown_sha256": _document_sha256(markdown_path),
                "json_sha256": _document_sha256(json_path),
                "markdown_version": markdown_version,
                "json_version": json_version,
                # Reported, never gated: the json trailing the markdown is a
                # fact about how these were authored, not a safety failure.
                # Every safety check below is evaluated on both files.
                "json_behind_markdown": json_version != markdown_version,
                "present": markdown_path.is_file() and json_path.is_file(),
                "version_matches": (
                    markdown_version == expected_markdown_version
                    and json_version == expected_json_version
                ),
                "status_matches": (
                    _field(text, "status") == expected_status
                    and str(machine.get("status") or "") == expected_status
                ),
                "does_not_authorize_live": (
                    machine.get("live_order_authorized") is False
                    and not bool(
                        re.search(
                            r"(?im)^live_(?:order|submit)_authorized:\s*true\s*$",
                            text,
                        )
                    )
                ),
                "machine_forbids_p0f": (
                    FORBIDDEN_MACHINE_STAGE.search(
                        _machine_text(machine)
                    )
                    is None
                ),
                "markdown_stage_contract_present": (
                    _markdown_stage_contract(text)
                ),
            })
            continue
        path = _resolve(str(raw_contract))
        text = path.read_text(encoding="utf-8")
        subordinate_evidence.append({
            "path": str(path),
            "markdown_sha256": _document_sha256(path),
            "present": path.is_file(),
            "version_matches": True,
            "status_matches": True,
            "does_not_authorize_live": not bool(
                re.search(r"(?im)^live_(?:order|submit)_authorized:\s*true\s*$", text)
            ),
            "machine_forbids_p0f": True,
            "markdown_stage_contract_present": True,
        })
    checks["subordinate_contracts_safe"] = bool(subordinate_evidence) and all(
        item["present"]
        and item["version_matches"]
        and item["status_matches"]
        and item["does_not_authorize_live"]
        and item["machine_forbids_p0f"]
        for item in subordinate_evidence
    )
    identity = {
        "schema": "bayes_basket_target_document_authority_audit_v1",
        "status": "PASS" if all(checks.values()) else "BLOCKED",
        "current_authority_count": len(authorities),
        "authority_id": str(parsed.get("authority_id") or ""),
        "manifest_sha256": _document_sha256(manifest_path.resolve()),
        "checks": checks,
        "documents": documents,
        "roadmap": roadmap_evidence,
        "subordinate_contracts": subordinate_evidence,
        "boundary": {
            "read_only": True,
            "writes_production_db": False,
            "reads_shared_live_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    identity["receipt_sha256"] = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return identity


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = build_audit(args.manifest)
    write_bounded_json(args.output, result)
    print(json.dumps({
        "schema": result["schema"],
        "status": result["status"],
        "current_authority_count": result["current_authority_count"],
        "output": str(args.output.expanduser().resolve()),
        "receipt_sha256": result["receipt_sha256"],
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
