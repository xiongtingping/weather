#!/usr/bin/env python3
"""只读审计 target ``production_no_live`` runtime 的唯一性与闭包。

该审计不打开数据库、不访问网络、不修改 launchd，也不把离线历史
``Shadow`` 包视为当前 target 运行路径。它只检查唯一 worker owner、resident
daemon 调度边界、旧包 import 闭包和 live/gateway 边界。
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402


SCHEMA = "bayes_basket_target_runtime_uniqueness_audit_v1"
WORKER = "bayes_basket_target/production_worker.py"
DAEMON = "scripts/reported_trajectory_no_daemon.py"
FORBIDDEN_MODULE_ROOTS = frozenset({"adaptive_basket_shadow", "websocket", "websockets", "requests", "httpx", "aiohttp"})
FORBIDDEN_CALLS = frozenset({"submit_order", "create_order", "post_order", "send_order", "place_order"})


def _modules(tree: ast.AST) -> list[str]:
    result: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.append(node.module)
    return result


def _calls(tree: ast.AST) -> list[str]:
    result: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            result.append(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            result.append(node.func.attr)
    return result


def audit(*, project_root: Path = ROOT) -> dict[str, Any]:
    root = project_root.resolve()
    worker_path = root / WORKER
    daemon_path = root / DAEMON
    files = (worker_path, daemon_path)
    syntax_errors: list[dict[str, Any]] = []
    trees: dict[str, ast.AST] = {}
    sources: dict[str, str] = {}
    for path in files:
        relative = str(path.relative_to(root))
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative)
        except (OSError, SyntaxError) as exc:
            syntax_errors.append({"file": relative, "error": str(exc)})
            continue
        sources[relative] = source
        trees[relative] = tree

    class_owners: list[str] = []
    for relative, tree in trees.items():
        if any(isinstance(node, ast.ClassDef) and node.name == "TargetProductionWorker" for node in ast.walk(tree)):
            class_owners.append(relative)

    forbidden_imports: list[dict[str, str]] = []
    forbidden_calls: list[dict[str, str]] = []
    for relative, tree in trees.items():
        for module in _modules(tree):
            if module.split(".", 1)[0].lower() in FORBIDDEN_MODULE_ROOTS:
                forbidden_imports.append({"file": relative, "module": module})
        for call in _calls(tree):
            if call in FORBIDDEN_CALLS:
                forbidden_calls.append({"file": relative, "call": call})

    worker_source = sources.get(WORKER, "")
    daemon_source = sources.get(DAEMON, "")
    checks = {
        "worker_file_present": worker_path.is_file(),
        "daemon_file_present": daemon_path.is_file(),
        "single_target_worker_owner": class_owners == [WORKER],
        "daemon_only_schedules_worker": "from bayes_basket_target.production_worker import" in daemon_source
        and "class TargetProductionWorker" not in daemon_source,
        "single_compile_route": worker_source.count("compile_production_cycle_from_ssot") >= 1
        and daemon_source.count("compile_production_cycle_from_ssot") == 1,
        "target_db_boundary": "DEFAULT_TARGET_DB" in worker_source
        and "archive.db" not in worker_source,
        "no_forbidden_imports": not forbidden_imports,
        "no_order_submit_calls": not forbidden_calls,
        "no_live_submit_authorized": '"live_submit_authorized": False' in worker_source,
        "no_gateway_boundary": '"calls_order_gateway": False' in worker_source,
        "retention_guard_present": "MAX_PREPARED_CYCLES_PER_SCOPE" in worker_source
        and "target_prepared_cycle_retention_guard" in worker_source,
        "syntax_clean": not syntax_errors,
    }
    identity = {
        "schema": SCHEMA,
        "runtime_mode": "production_no_live",
        "worker_owner": WORKER,
        "resident_scheduler": DAEMON,
        "class_owners": class_owners,
        "forbidden_imports": forbidden_imports,
        "forbidden_calls": forbidden_calls,
        "syntax_errors": syntax_errors,
        "checks": checks,
        "boundary": {
            "authorizes_order": False,
            "live_submit_authorized": False,
            "calls_order_gateway": False,
            "writes_shared_live_db": False,
            "network_access": False,
            "read_only_audit": True,
        },
    }
    digest = __import__("hashlib").sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {**identity, "ok": all(checks.values()), "receipt_sha256": digest}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit(project_root=args.root.expanduser().resolve())
    if args.output:
        write_bounded_json(args.output.expanduser().resolve(), result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

