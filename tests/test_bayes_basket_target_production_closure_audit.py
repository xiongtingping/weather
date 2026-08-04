from __future__ import annotations

import json
import plistlib
from pathlib import Path
from types import SimpleNamespace

import scripts.bayes_basket_target_production_closure_audit as closure_audit
from scripts.bayes_basket_target_production_closure_audit import (
    TARGET_SERVICE_LABELS,
    audit,
)


SOURCE_FILES = (
    "scripts/weather_basket_yes_daemon.py",
    "scripts/market_ws_listener.py",
    "scripts/clob_order_service.mjs",
    "bayes_basket_target/runtime.py",
    "bayes_basket_target/production.py",
    "bayes_basket_target/atomic_ledger.py",
    "bayes_basket_target/production_worker.py",
    "bayes_basket_target/basket_kernel/delta_v2.py",
    closure_audit.TARGET_ACTIVATION,
)


def _write_isolation_manifest(root: Path) -> None:
    activation = closure_audit.TARGET_ACTIVATION
    source_closure = [name for name in SOURCE_FILES if name != activation]
    resources = []
    for name in [activation, *source_closure]:
        resources.append(
            {
                "path": name,
                "sha256": closure_audit._sha256_bytes((root / name).read_bytes()),
                "owner": closure_audit.TARGET_RUNTIME_OWNER,
                "consumers": [closure_audit.TARGET_RUNTIME_CONSUMER],
            }
        )
    protected = {
        "path": closure_audit.PROTECTED_LEGACY_CONFIG,
        "sha256": closure_audit._sha256_bytes(
            (root / closure_audit.PROTECTED_LEGACY_CONFIG).read_bytes()
        ),
        "owner": closure_audit.PROTECTED_LEGACY_OWNER,
        "consumers": [closure_audit.PROTECTED_LEGACY_CONSUMER],
    }
    manifest = {
        "schema": closure_audit.RUNTIME_ISOLATION_SCHEMA,
        "manifest_version": 1,
        "target": {
            "owner": closure_audit.TARGET_RUNTIME_OWNER,
            "activation": resources[0],
            "source_closure": resources[1:],
        },
        "protected_legacy_config": [protected],
        "forbidden_cross_read": [
            {
                "consumer_owner": closure_audit.TARGET_RUNTIME_OWNER,
                "resource_path": closure_audit.PROTECTED_LEGACY_CONFIG,
                "reason": "target_must_not_read_protected_l0_config",
            }
        ],
    }
    manifest["manifest_sha256"] = closure_audit._manifest_digest(manifest)
    _write_text(
        root,
        closure_audit.RUNTIME_ISOLATION_MANIFEST,
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _write_text(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _write_plist(root: Path, label: str, script: str, *, environment: dict[str, str] | None = None) -> None:
    path = root / "launchd" / f"{label}.plist"
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "Label": label,
        "ProgramArguments": ["/bin/sh", f"/tmp/{script}"],
    }
    if environment:
        value["EnvironmentVariables"] = environment
    path.write_bytes(plistlib.dumps(value))


def _fixture(tmp_path: Path) -> Path:
    _write_text(
        tmp_path,
        closure_audit.HARD_NO_AUTHORITY_SOURCE,
        """
def _write_decision(conn):
    conn.execute('INSERT INTO auto_trade_decisions(strategy_lane, hard_no) VALUES(?, ?)')
strategy_lane = 'hard_no'
""",
    )
    _write_text(
        tmp_path,
        "scripts/weather_basket_yes_daemon.py",
        """
DEFAULT_DB = 'data/archive.db'
DEFAULT_TARGET_DB = 'data/bayes_basket_target_ssot.db'
parser = 'args.db default DEFAULT_DB'
lane_workers = True
commit_conn = open_target_writer_if_enabled(DEFAULT_TARGET_DB)
worker = TargetProductionWorker(DEFAULT_TARGET_DB, commit_conn=commit_conn)
TARGET_ONLY = True
execution_mode = 'production_no_live'
live_submit_authorized = False
authorizes_order = False
places_orders = False
calls_order_gateway = False
auto_trade_decisions = 'sole blocking authority'
will_order = False
decided_state = 'blocked'
materialized_probability = True
P0 = P0M = P1 = True
regime = 'neutral'
official = 'reported METAR hard_lower_bound'
hard_no_bayes_consumed = False
""",
    )
    _write_text(tmp_path, "scripts/market_ws_listener.py", "official_clob_ws = True\nauthorizes_order = False\n")
    _write_text(
        tmp_path,
        "scripts/clob_order_service.mjs",
        "const source = 'official_clob_ws'; const authorizes_order = false;\n"
        "const route = '/place-order'; const exchange_post_started = false;\n"
        "const RETIRED_STRATEGY_IDS = new Set(['weather_copy_follow_v1']);\n"
        "const retired = {error:'retired_strategy_forbidden', exchange_post_started: false};\n",
    )
    _write_text(
        tmp_path,
        "bayes_basket_target/runtime.py",
        "runtime_contract = 'materialized probability P0 P0-M P1 regime official WS'\n"
        "execution_mode = 'production_no_live'\nlive_submit_authorized = False\n",
    )
    _write_text(tmp_path, "bayes_basket_target/production.py", "contract = 'auto_trade_decisions will_order decided_state official reported hard_lower_bound production_no_live commit_target_atomic_bundle basket'\n")
    _write_text(tmp_path, "bayes_basket_target/atomic_ledger.py", "contract = 'auto_trade_decisions official reported hard_lower_bound hard_no commit_target_atomic_bundle basket conn.execute'\n")
    _write_text(
        tmp_path,
        "bayes_basket_target/production_worker.py",
        """
DEFAULT_TARGET_DB = 'data/bayes_basket_target_ssot.db'
def open_target_writer_if_enabled(path):
    return sqlite3.connect(DEFAULT_TARGET_DB)
class TargetProductionWorker:
    pass
""",
    )
    _write_text(tmp_path, "bayes_basket_target/basket_kernel/delta_v2.py", "contract = 'materialized probability P0 P0-M P1 regime official WS'\n")
    _write_text(
        tmp_path,
        closure_audit.TARGET_ACTIVATION,
        json.dumps(
            {
                "target_only": True,
                "execution_mode": "production_no_live",
                "live_submit_authorized": False,
                "live_order_authorized": False,
                "live_decision_write_authorized": False,
                "authorizes_order": False,
                "places_orders": False,
                "calls_order_gateway": False,
                "writes_production_db": False,
                "production_mutation": False,
            }
        ),
    )
    _write_text(tmp_path, closure_audit.PROTECTED_LEGACY_CONFIG, '{"basket":{"convergence_widths":[4,3,2],"all_legs_required":true}}\n')
    user_env = {"WEATHER_SERVICE_DOMAIN": "user"}
    _write_plist(
        tmp_path,
        TARGET_SERVICE_LABELS[0],
        "weather_basket_yes_daemon.sh",
        environment=user_env,
    )
    _write_plist(
        tmp_path,
        TARGET_SERVICE_LABELS[1],
        "market_ws_listener.sh",
        environment=user_env,
    )
    _write_plist(
        tmp_path,
        TARGET_SERVICE_LABELS[2],
        "clob_order_service.sh",
        environment=user_env,
    )
    _write_isolation_manifest(tmp_path)
    return tmp_path


def _run(root: Path, **kwargs):
    params = {
        "project_root": root,
        "source_files": SOURCE_FILES,
        "required_source_names": SOURCE_FILES,
        "service_snapshot": {"system": [], "user": list(TARGET_SERVICE_LABELS)},
    }
    params.update(kwargs)
    return audit(
        **params,
    )


def _load_manifest(root: Path) -> dict:
    path = root / closure_audit.RUNTIME_ISOLATION_MANIFEST
    return json.loads(path.read_text(encoding="utf-8"))


def _save_manifest(root: Path, manifest: dict, *, reseal: bool = True) -> None:
    if reseal:
        manifest.pop("manifest_sha256", None)
        manifest["manifest_sha256"] = closure_audit._manifest_digest(manifest)
    _write_text(
        root,
        closure_audit.RUNTIME_ISOLATION_MANIFEST,
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def test_compliant_fixture_passes_and_receipt_is_compact_and_repeatable(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    first = _run(root)
    second = _run(root)

    assert first["ok"] is True
    assert first["receipt_sha256"] == second["receipt_sha256"]
    assert first["violations"] == []
    assert first["safe_deletion_candidates"]["count"] == 0
    assert first["safe_deletion_candidates"]["deletion_authorized"] is False
    assert first["retired_runtime_candidates"]["deletion_authorized"] is False
    assert first["reference_graph"]["edge_count"] == 0
    assert first["checks"]["writer_authorities_and_database_bindings_closed"] is True
    assert first["writer_authority_closure"]["evidence"]["writer_authorities"]["hard_no"]["database"] == closure_audit.ARCHIVE_DB_PATH
    assert first["writer_authority_closure"]["evidence"]["writer_authorities"]["basket"]["database"] == closure_audit.TARGET_DB_PATH
    assert first["runtime_config_isolation"]["checks"]["protected_legacy_config_content_addressed"] is True
    assert not any(item["code"].startswith("config_forbidden_") for item in first["violations"])
    assert len(json.dumps(first, ensure_ascii=False)) < 20_000


def test_retired_copy_follow_gateway_entry_must_be_denied_and_not_parallel(
    tmp_path: Path,
) -> None:
    root = _fixture(tmp_path)
    gateway = root / "scripts/clob_order_service.mjs"
    gateway.write_text(
        gateway.read_text(encoding="utf-8").replace(
            "const retired = {error:'retired_strategy_forbidden', exchange_post_started: false};",
            "const copy_follow_parallel_submission = true; "
            "const copy_follow_parallel = true;",
        ),
        encoding="utf-8",
    )
    manifest = _load_manifest(root)
    for item in manifest["target"]["source_closure"]:
        if item["path"] == "scripts/clob_order_service.mjs":
            item["sha256"] = closure_audit._sha256_bytes(
                gateway.read_bytes()
            )
    _save_manifest(root, manifest)

    result = _run(root)

    assert result["ok"] is False
    assert result["checks"]["retired_gateway_entry_closed"] is False
    codes = {item["code"] for item in result["violations"]}
    assert (
        "retired_gateway_check_failed:"
        "copy_follow_gateway_denied_before_submit"
    ) in codes
    assert (
        "retired_gateway_check_failed:"
        "copy_follow_parallel_gateway_lane_absent"
    ) in codes


def test_comments_docs_and_disabled_bayes_consumed_flag_are_not_semantic_hits(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    _write_text(root, "docs/old.md", "dynamic_three_leg target_realization 4→3→2")
    _write_text(root, "tests/test_old.py", "calls_order_gateway = true\nhard_no_probability = true\n")
    result = _run(root)

    assert result["ok"] is True
    assert not any(item["code"] == "hard_no_lane_reads_probability_or_weather_field" for item in result["violations"])
    assert result["production_boundary"]["gateway_owners_observed"] == ["scripts/clob_order_service.mjs"]


def test_gamma_registry_metadata_rest_is_not_clob_book_rest(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    _write_text(
        root,
        "scripts/weather_basket_yes_daemon.py",
        (root / "scripts/weather_basket_yes_daemon.py").read_text(encoding="utf-8")
        + "\nfetch_gamma_events_window()\nmarket_registry_metadata = True\n",
    )
    result = _run(root)

    assert not any(
        item["code"] == "forbidden_market_rest_or_tick_dependency"
        for item in result["violations"]
    )


def test_hard_no_engine_target_database_crossing_fails_closed(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    _write_text(
        root,
        closure_audit.HARD_NO_AUTHORITY_SOURCE,
        """
DEFAULT_TARGET_DB = 'data/bayes_basket_target_ssot.db'
def _write_decision(commit_conn):
    commit_conn.execute('INSERT INTO auto_trade_decisions(strategy_lane, hard_no) VALUES(?, ?)')
strategy_lane = 'hard_no'
""",
    )
    result = _run(root)

    assert result["ok"] is False
    codes = {item["code"] for item in result["violations"]}
    assert "hard_no_writer_target_database_reference" in codes
    assert "writer_authority_check_failed:cross_lane_writer_isolation" in codes


def test_target_writer_archive_database_crossing_fails_closed(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    daemon = (root / "scripts/weather_basket_yes_daemon.py").read_text(encoding="utf-8")
    _write_text(
        root,
        "scripts/weather_basket_yes_daemon.py",
        daemon + "\nTargetProductionWorker(DEFAULT_DB, commit_conn=commit_conn)\n",
    )
    result = _run(root)

    assert result["ok"] is False
    codes = {item["code"] for item in result["violations"]}
    assert "basket_writer_archive_database_reference" in codes


def test_system_domain_target_is_distinguished_as_residue(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    result = _run(root, service_snapshot={"system": list(TARGET_SERVICE_LABELS), "user": [TARGET_SERVICE_LABELS[0]]})

    assert result["ok"] is False
    assert any(
        item["code"] == "target_service_system_domain_residue"
        for item in result["violations"]
    )
    assert result["service_closure"]["evidence"]["loaded_user"] == [TARGET_SERVICE_LABELS[0]]


def test_empty_loaded_snapshot_is_not_inferred_from_plist_declarations(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    result = _run(root, service_snapshot={"system": [], "user": []})

    assert result["ok"] is False
    assert result["service_closure"]["evidence"]["loaded_system"] == []
    assert result["service_closure"]["evidence"]["declared_service_count"] == 3
    assert not any(item["code"] == "forbidden_loaded_service" for item in result["violations"])
    assert any(
        item["code"] == "target_service_not_loaded_in_user_domain"
        for item in result["violations"]
    )


def test_probe_includes_non_target_declared_services_and_blocks_fourth_system_registration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = _fixture(tmp_path)
    fourth = "com.weather.forecast_source_forward_capture"
    _write_plist(root, fourth, "capture_current_forecast_source_forward.py")
    queried: list[str] = []

    def fake_run(command, **_kwargs):
        target = str(command[-1])
        queried.append(target)
        loaded = {
            *(f"system/{label}" for label in TARGET_SERVICE_LABELS),
            f"system/{fourth}",
        }
        return SimpleNamespace(returncode=0 if target in loaded else 113, stderr=b"")

    monkeypatch.setattr(closure_audit.subprocess, "run", fake_run)
    result = audit(
        project_root=root,
        source_files=SOURCE_FILES,
        required_source_names=SOURCE_FILES,
        probe_services=True,
    )

    assert f"system/{fourth}" in queried
    assert result["ok"] is False
    assert result["service_closure"]["checks"]["target_services_user_only"] is False
    assert fourth in result["service_closure"]["evidence"]["loaded_system"]


def test_legacy_fallback_and_fourth_target_manifest_fail_closed(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    _write_text(root, "bayes_basket_target/runtime.py", "legacy_target_realization_fallback = True\n")
    _write_plist(root, "com.weather.fourth_target_service", "fourth_target_worker.sh")
    result = _run(root)

    assert result["ok"] is False
    codes = {item["code"] for item in result["violations"]}
    assert "legacy_runtime_dependency" in codes
    assert "unexpected_target_service_manifest" in codes


def test_dynamic_and_sequential_residues_in_clob_owner_are_retained(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    _write_text(
        root,
        "scripts/clob_order_service.mjs",
        "const route = '/place-order'; const exchange_post_started = false;\n"
        "const dynamic_three_leg_snapshot = true; const sequential_bayes_execution_contract = true;\n",
    )
    result = _run(root)

    assert result["ok"] is False
    residues = [item for item in result["violations"] if item["code"] == "legacy_runtime_dependency"]
    assert {item["evidence"] for item in residues} >= {"dynamic_three_leg", "sequential_bayes"}


def test_live_flag_and_wrong_dependency_fail(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    _write_text(root, closure_audit.TARGET_ACTIVATION, '{"target_only":true,"live_submit_authorized":true}')
    _write_text(root, "bayes_basket_target/production.py", "from dynamic_three_leg import execute\n")
    result = _run(root)

    assert result["ok"] is False
    codes = {item["code"] for item in result["violations"]}
    assert "config_live_or_mutating_flag_true" in codes
    assert "legacy_runtime_import" in codes


def test_basket_lane_allows_content_addressed_p0_nwp_version_label(
    tmp_path: Path,
) -> None:
    root = _fixture(tmp_path)
    _write_text(
        root,
        "bayes_basket_target/runtime.py",
        "posterior_version = 'p0_nwp_historical_meta_anchor_v4'\n"
        "runtime_contract = 'materialized probability regime official WS'\n",
    )
    manifest = _load_manifest(root)
    for item in manifest["target"]["source_closure"]:
        if item["path"] == "bayes_basket_target/runtime.py":
            item["sha256"] = closure_audit._sha256_bytes(
                (root / item["path"]).read_bytes()
            )
    _save_manifest(root, manifest)

    result = _run(root)

    assert not any(
        item["code"]
        == "basket_lane_reads_forbidden_weather_or_hard_no_field"
        for item in result["violations"]
    )


def test_basket_lane_direct_nwp_identifier_still_fails_closed(
    tmp_path: Path,
) -> None:
    root = _fixture(tmp_path)
    _write_text(
        root,
        "bayes_basket_target/runtime.py",
        "nwp_payload = {'temperature': 30}\n"
        "runtime_contract = 'materialized probability regime official WS'\n",
    )
    manifest = _load_manifest(root)
    for item in manifest["target"]["source_closure"]:
        if item["path"] == "bayes_basket_target/runtime.py":
            item["sha256"] = closure_audit._sha256_bytes(
                (root / item["path"]).read_bytes()
            )
    _save_manifest(root, manifest)

    result = _run(root)

    assert any(
        item["code"]
        == "basket_lane_reads_forbidden_weather_or_hard_no_field"
        and item["evidence"] == "nwp"
        for item in result["violations"]
    )


def test_resource_overflow_fails_closed(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    result = _run(root, max_file_bytes=16, max_total_bytes=64)

    assert result["ok"] is False
    assert result["checks"]["bounded_scan_closed"] is False
    assert result["scope"]["source_bytes"] <= 64


def test_isolation_manifest_missing_fails_closed(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    (root / closure_audit.RUNTIME_ISOLATION_MANIFEST).unlink()

    result = _run(root)

    assert result["ok"] is False
    assert any(item["code"] == "runtime_isolation_manifest_missing" for item in result["violations"])
    assert result["checks"]["runtime_config_isolation_closed"] is False


def test_isolation_manifest_hash_tampering_fails_closed(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    manifest = _load_manifest(root)
    manifest["target"]["activation"]["sha256"] = "0" * 64
    _save_manifest(root, manifest, reseal=False)

    result = _run(root)

    assert result["ok"] is False
    codes = {item["code"] for item in result["violations"]}
    assert "runtime_isolation_manifest_hash_mismatch" in codes
    assert "runtime_isolation_target_resource_hash_mismatch" in codes


def test_target_source_cross_read_fails_even_with_valid_resealed_hashes(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    _write_text(
        root,
        "bayes_basket_target/runtime.py",
        "legacy_path = 'config/trading/basket_yes_v1.json'\n"
        "runtime_contract = 'materialized probability P0 P0-M P1 regime official WS'\n",
    )
    manifest = _load_manifest(root)
    for item in manifest["target"]["source_closure"]:
        if item["path"] == "bayes_basket_target/runtime.py":
            item["sha256"] = closure_audit._sha256_bytes(
                (root / item["path"]).read_bytes()
            )
    _save_manifest(root, manifest)

    result = _run(root)

    assert result["ok"] is False
    assert any(item["code"] == "target_source_cross_read_protected_l0" for item in result["violations"])
    assert result["runtime_config_isolation"]["evidence"]["target_reference_graph"]["edge_count"] == 1


def test_multiple_owner_declaration_fails_closed_even_when_manifest_is_resealed(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    manifest = _load_manifest(root)
    manifest["target"]["source_closure"].append(
        {
            "path": closure_audit.PROTECTED_LEGACY_CONFIG,
            "sha256": closure_audit._sha256_bytes(
                (root / closure_audit.PROTECTED_LEGACY_CONFIG).read_bytes()
            ),
            "owner": closure_audit.TARGET_RUNTIME_OWNER,
            "consumers": [closure_audit.TARGET_RUNTIME_CONSUMER],
        }
    )
    _save_manifest(root, manifest)

    result = _run(root)

    assert result["ok"] is False
    codes = {item["code"] for item in result["violations"]}
    assert "runtime_isolation_manifest_multiple_owner" in codes
    assert "runtime_isolation_target_source_scope_mismatch" in codes


def test_protected_legacy_hash_tampering_fails_closed(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    _write_text(root, closure_audit.PROTECTED_LEGACY_CONFIG, '{"basket":{"convergence_widths":[9]}}\n')

    result = _run(root)

    assert result["ok"] is False
    assert any(item["code"] == "runtime_isolation_protected_resource_hash_mismatch" for item in result["violations"])


def test_current_worktree_has_no_retired_declaration_but_empty_probe_fails_closed() -> None:
    root = Path(__file__).resolve().parents[1]
    result = audit(project_root=root, service_snapshot={"system": [], "user": []})

    assert result["ok"] is False
    codes = {item["code"] for item in result["violations"]}
    assert "forbidden_service_declaration_residue" not in codes
    assert "target_service_not_loaded_in_user_domain" in codes
    assert result["service_closure"]["checks"]["forbidden_service_declarations_absent"] is True
    assert result["service_closure"]["evidence"]["loaded_system"] == []
    assert result["service_closure"]["evidence"]["forbidden_loaded_service_count"] == 0
    assert result["safe_deletion_candidates"]["count"] == 0
    assert result["retired_runtime_candidates"]["count"] >= 0
    assert result["safe_deletion_candidates"]["deletion_authorized"] is False
    assert "content_sha256" in result["reference_graph"]
