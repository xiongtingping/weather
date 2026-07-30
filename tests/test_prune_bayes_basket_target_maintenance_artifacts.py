from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import scripts.prune_bayes_basket_target_maintenance_artifacts as prune


def _sqlite(path: Path, value: int) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE proof(value INTEGER NOT NULL)")
        connection.execute("INSERT INTO proof VALUES(?)", (value,))


def test_pruner_keeps_newest_recovery_image_and_bounds_failed_receipts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "workspace"
    data = root / "data"
    audits = root / "output" / "audits"
    data.mkdir(parents=True)
    audits.mkdir(parents=True)
    active = data / "bayes_basket_target_ssot.db"
    _sqlite(active, 0)
    backups = [
        data / f"bayes_basket_target_ssot.pre_v4_{index}.backup.db"
        for index in range(3)
    ]
    for index, backup in enumerate(backups):
        _sqlite(backup, index)
        backup.touch()
    failures = [
        audits
        / f"bayes_basket_target_ssot_v4_cutover_failed_{index}.json"
        for index in range(10)
    ]
    for failure in failures:
        failure.write_text("{}", encoding="utf-8")
        failure.touch()

    monkeypatch.setattr(prune, "ROOT", root)
    monkeypatch.setattr(prune, "DATA", data)
    monkeypatch.setattr(prune, "AUDITS", audits)
    monkeypatch.setattr(prune, "ACTIVE_DB", active)

    plans = dict(prune.build_plans())
    assert plans["target_backups"].retained_count == 2
    assert plans["target_backups"].delete_count == 1
    assert plans["failed_cutover_receipts"].retained_count == 8
    assert plans["failed_cutover_receipts"].delete_count == 2


def test_cli_requires_exact_apply_confirmation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["prune", "--apply", "--confirm", "WRONG"],
    )
    try:
        prune.main()
    except SystemExit as exc:
        assert str(exc) == "maintenance_retention_confirmation_invalid"
    else:
        raise AssertionError("apply unexpectedly accepted invalid confirmation")


def test_cli_plan_writes_bounded_receipt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "workspace"
    data = root / "data"
    audits = root / "output" / "audits"
    data.mkdir(parents=True)
    audits.mkdir(parents=True)
    active = data / "bayes_basket_target_ssot.db"
    _sqlite(active, 0)
    backup = data / "hard_no_decisions.pre_timezone_1.backup.db"
    _sqlite(backup, 1)
    receipt = audits / "retention.json"
    monkeypatch.setattr(prune, "ROOT", root)
    monkeypatch.setattr(prune, "DATA", data)
    monkeypatch.setattr(prune, "AUDITS", audits)
    monkeypatch.setattr(prune, "ACTIVE_DB", active)
    monkeypatch.setattr(
        "sys.argv",
        ["prune", "--receipt", str(receipt)],
    )

    assert prune.main() == 0
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["state"] == "planned"
    assert payload["calls_order_gateway"] is False
    assert payload["orders_submitted"] == 0
