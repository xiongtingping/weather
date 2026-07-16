from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/materialize_production_probability_activation.py"
spec = importlib.util.spec_from_file_location("materialize_production_probability_activation", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def _fixture_root(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "weather"
    for name in module.RUNTIME_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"runtime:{name}\n", encoding="utf-8")
    deployment = "\n".join(
        (
            "AMSC_ZUUU_PRODUCTION_PROBABILITY_UPDATE_ENABLED",
            "PM_AMSC_ZUUU_SPECI_PRODUCTION_PROBABILITY_INGRESS_ENABLED",
            "apply_amsc_probability_fast_lane_from_plan.py",
            "--production-probability-ingress",
        )
    )
    for name in module.RETIREMENT_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(deployment, encoding="utf-8")
    installed = tmp_path / "installed.plist"
    installed.write_bytes((root / "launchd/com.weather.monitor.daemon.plist").read_bytes())
    return root, installed


def _db(path: Path) -> Path:
    with sqlite3.connect(path) as conn:
        for table in module.REQUIRED_TABLES:
            conn.execute(f"CREATE TABLE {table}(id TEXT)")
    return path


def test_activation_plan_binds_current_artifacts_and_is_permanently_orderless(tmp_path, monkeypatch) -> None:
    root, installed = _fixture_root(tmp_path)
    db = _db(tmp_path / "archive.db")
    backup = tmp_path / "archive.backup.db"
    backup.write_bytes(b"verified-backup-fixture")
    monkeypatch.setattr(
        module,
        "build_production_p0_inputs",
        lambda **_: {
            "ok": True,
            "runtime_artifacts": {
                "prior_model_artifact_sha256": "a" * 64,
                "weak_likelihood_artifact_sha256": "b" * 64,
                "strong_lr_artifact_sha256": "c" * 64,
            },
        },
    )
    monkeypatch.setattr(
        module,
        "run_drill",
        lambda _: {"ok": True, "state": "temporary_no_order_drill_passed", "places_orders": False},
    )

    plan = module.build_activation_plan(
        db=db,
        backup=backup,
        event_date="2026-07-14",
        decision_as_of="2026-07-14T06:00:00Z",
        root=root,
        installed_monitor_path=installed,
    )

    assert plan["ok"] is True
    assert plan["blockers"] == []
    assert plan["activation"]["state"] == "production_enabled"
    assert plan["activation"]["legacy_probability_writer_disabled"] is True
    assert plan["activation"]["places_orders"] is False
    assert plan["activation"]["authorizes_clob"] is False
    assert plan["installed_monitor_deployment"]["matches_repo"] is True


def test_activation_plan_blocks_stale_installed_monitor(tmp_path, monkeypatch) -> None:
    root, installed = _fixture_root(tmp_path)
    installed.write_text("stale deployment", encoding="utf-8")
    db = _db(tmp_path / "archive.db")
    backup = tmp_path / "archive.backup.db"
    backup.write_bytes(b"verified-backup-fixture")
    monkeypatch.setattr(module, "build_production_p0_inputs", lambda **_: {"ok": True, "runtime_artifacts": {}})
    monkeypatch.setattr(module, "run_drill", lambda _: {"ok": True, "places_orders": False})

    plan = module.build_activation_plan(
        db=db,
        backup=backup,
        event_date="2026-07-14",
        decision_as_of="2026-07-14T06:00:00Z",
        root=root,
        installed_monitor_path=installed,
    )

    assert plan["ok"] is False
    assert "installed_monitor_plist_not_current_probability_deployment" in plan["blockers"]
