#!/usr/bin/env python3
"""Deterministically reseal the target runtime-isolation manifest.

The manifest is an audit/config boundary only.  This command never restarts a
service, opens a database, touches live state, or authorizes an order.  It
accepts no path additions: the existing manifest must already declare exactly
the frozen target closure before hashes can be refreshed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json_if_changed,
)
from scripts.bayes_basket_target_production_closure_audit import (  # noqa: E402
    DEFAULT_SOURCE_SCOPE,
    PROTECTED_LEGACY_CONFIG,
    PROTECTED_LEGACY_CONSUMER,
    PROTECTED_LEGACY_OWNER,
    RUNTIME_ISOLATION_MANIFEST,
    RUNTIME_ISOLATION_SCHEMA,
    TARGET_ACTIVATION,
    TARGET_RUNTIME_CONSUMER,
    TARGET_RUNTIME_OWNER,
    _manifest_digest,
)


MAX_RESOURCE_BYTES = 1_000_000
MAX_MANIFEST_BYTES = 256_000


def _sha256_file(root: Path, relative: str) -> str:
    path = (root / relative).resolve()
    if root != path and root not in path.parents:
        raise RuntimeError("runtime_isolation_resource_outside_root")
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(
            f"runtime_isolation_resource_missing_or_symlink:{relative}"
        )
    if path.stat().st_size > MAX_RESOURCE_BYTES:
        raise RuntimeError(
            f"runtime_isolation_resource_bytes_exceeded:{relative}"
        )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _entry(
    value: Mapping[str, Any],
    *,
    path: str,
    owner: str,
    consumer: str,
) -> dict[str, Any]:
    if (
        str(value.get("path") or "") != path
        or str(value.get("owner") or "") != owner
        or value.get("consumers") != [consumer]
    ):
        raise RuntimeError(
            f"runtime_isolation_declaration_drift:{path}"
        )
    return {
        "consumers": [consumer],
        "owner": owner,
        "path": path,
    }


def build_refreshed_manifest(
    *,
    project_root: Path = ROOT,
    manifest_path: Path | None = None,
    allow_scope_upgrade: bool = False,
) -> dict[str, Any]:
    root = project_root.expanduser().resolve()
    path = (
        manifest_path.expanduser().resolve()
        if manifest_path is not None
        else (root / RUNTIME_ISOLATION_MANIFEST).resolve()
    )
    if root != path and root not in path.parents:
        raise RuntimeError("runtime_isolation_manifest_outside_root")
    raw = load_bounded_json(path, limit=MAX_MANIFEST_BYTES)
    if not isinstance(raw, Mapping):
        raise RuntimeError("runtime_isolation_manifest_mapping_required")
    if (
        raw.get("schema") != RUNTIME_ISOLATION_SCHEMA
        or raw.get("manifest_version") != 1
    ):
        raise RuntimeError("runtime_isolation_manifest_contract_drift")
    if raw.get("manifest_sha256") != _manifest_digest(raw):
        raise RuntimeError("runtime_isolation_manifest_sha_drift")
    target = raw.get("target")
    if not isinstance(target, Mapping):
        raise RuntimeError("runtime_isolation_target_mapping_required")
    if target.get("owner") != TARGET_RUNTIME_OWNER:
        raise RuntimeError("runtime_isolation_target_owner_drift")
    activation = target.get("activation")
    closure = target.get("source_closure")
    protected = raw.get("protected_legacy_config")
    if (
        not isinstance(activation, Mapping)
        or not isinstance(closure, list)
        or not isinstance(protected, list)
        or len(protected) != 1
        or not isinstance(protected[0], Mapping)
    ):
        raise RuntimeError("runtime_isolation_manifest_shape_drift")
    expected_closure = [
        item for item in DEFAULT_SOURCE_SCOPE
        if item != TARGET_ACTIVATION
    ]
    closure_by_path = {
        str(item.get("path") or ""): item
        for item in closure
        if isinstance(item, Mapping)
    }
    observed_closure = list(closure_by_path)
    if (
        len(observed_closure) != len(closure)
        or (
            not allow_scope_upgrade
            and observed_closure != expected_closure
        )
        or (
            allow_scope_upgrade
            and not set(observed_closure).issubset(
                set(expected_closure)
            )
        )
    ):
        raise RuntimeError("runtime_isolation_source_scope_drift")
    activation_out = _entry(
        activation,
        path=TARGET_ACTIVATION,
        owner=TARGET_RUNTIME_OWNER,
        consumer=TARGET_RUNTIME_CONSUMER,
    )
    activation_out["sha256"] = _sha256_file(root, TARGET_ACTIVATION)
    closure_out: list[dict[str, Any]] = []
    for relative in expected_closure:
        item = closure_by_path.get(relative)
        if item is None:
            if not allow_scope_upgrade:
                raise RuntimeError(
                    "runtime_isolation_source_scope_drift"
                )
            declaration = {
                "consumers": [TARGET_RUNTIME_CONSUMER],
                "owner": TARGET_RUNTIME_OWNER,
                "path": relative,
            }
        else:
            declaration = _entry(
                item,
                path=relative,
                owner=TARGET_RUNTIME_OWNER,
                consumer=TARGET_RUNTIME_CONSUMER,
            )
        declaration["sha256"] = _sha256_file(root, relative)
        closure_out.append(declaration)
    protected_out = _entry(
        protected[0],
        path=PROTECTED_LEGACY_CONFIG,
        owner=PROTECTED_LEGACY_OWNER,
        consumer=PROTECTED_LEGACY_CONSUMER,
    )
    protected_out["sha256"] = _sha256_file(
        root,
        PROTECTED_LEGACY_CONFIG,
    )
    rules = raw.get("forbidden_cross_read")
    if not isinstance(rules, list) or not any(
        isinstance(item, Mapping)
        and item.get("consumer_owner") == TARGET_RUNTIME_OWNER
        and item.get("resource_path") == PROTECTED_LEGACY_CONFIG
        for item in rules
    ):
        raise RuntimeError("runtime_isolation_forbidden_rule_drift")
    identity = {
        "forbidden_cross_read": rules,
        "manifest_version": 1,
        "protected_legacy_config": [protected_out],
        "schema": RUNTIME_ISOLATION_SCHEMA,
        "target": {
            "activation": activation_out,
            "owner": TARGET_RUNTIME_OWNER,
            "source_closure": closure_out,
        },
    }
    return {
        **identity,
        "manifest_sha256": _manifest_digest(identity),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / RUNTIME_ISOLATION_MANIFEST,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="atomically write only when canonical bytes changed",
    )
    parser.add_argument(
        "--upgrade-scope",
        action="store_true",
        help=(
            "add only missing paths from the frozen DEFAULT_SOURCE_SCOPE; "
            "extra or unknown paths remain forbidden"
        ),
    )
    args = parser.parse_args()
    refreshed = build_refreshed_manifest(
        project_root=ROOT,
        manifest_path=args.manifest,
        allow_scope_upgrade=args.upgrade_scope,
    )
    changed = False
    if args.apply:
        changed = write_bounded_json_if_changed(
            args.manifest,
            refreshed,
            limit=MAX_MANIFEST_BYTES,
        )
    print(json.dumps({
        "schema": "bayes_basket_target_runtime_isolation_refresh_v1",
        "state": (
            "UPDATED"
            if changed
            else ("UNCHANGED" if args.apply else "DRY_RUN")
        ),
        "manifest_path": str(args.manifest.resolve()),
        "manifest_sha256": refreshed["manifest_sha256"],
        "resource_count": (
            len(refreshed["target"]["source_closure"]) + 2
        ),
        "scope_upgrade_authorized": bool(args.upgrade_scope),
        "writes_manifest": bool(args.apply),
        "changes_services": False,
        "opens_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
