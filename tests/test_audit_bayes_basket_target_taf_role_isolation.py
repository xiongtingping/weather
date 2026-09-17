from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scripts.audit_bayes_basket_target_taf_role_isolation import (
    DEFAULT_SOURCE_ROOT,
    MAX_RECEIPT_BYTES,
    REQUIRED_SOURCE_FILES,
    audit_taf_role_isolation,
)


def _source_copy(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    for relative in REQUIRED_SOURCE_FILES:
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(DEFAULT_SOURCE_ROOT / relative, destination)
    return root


def test_unknown_kind_allowlist_drift_is_blocked(tmp_path: Path) -> None:
    root = _source_copy(tmp_path)
    path = root / "bayes_basket_target" / "event_contracts.py"
    source = path.read_text(encoding="utf-8")
    assert '{"TAF", "AMD", "COR"}' in source
    path.write_text(
        source.replace('{"TAF", "AMD", "COR"}', '{"TAF", "AMD", "COR", "UNKNOWN"}', 1),
        encoding="utf-8",
    )

    receipt = audit_taf_role_isolation(root)

    assert receipt["status"] == "BLOCKED"
    assert any(item["code"] == "contract_kind_allowlist_drift" for item in receipt["blockers"])


def test_shared_writer_taf_to_canonical_is_blocked(tmp_path: Path) -> None:
    root = _source_copy(tmp_path)
    path = root / "bayes_basket_target" / "materialization.py"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            "\n\ndef injected_shared_writer(conn, forecast_revision, canonical_observation):\n"
            "    conn.execute(\"INSERT INTO canonical_observation_events VALUES (?)\", (forecast_revision,))\n"
        )

    receipt = audit_taf_role_isolation(root)

    assert receipt["status"] == "BLOCKED"
    codes = {item["code"] for item in receipt["blockers"]}
    assert "shared_writer" in codes
    assert "taf_writes_canonical_or_p1" in codes


def test_taf_writer_outside_revision_or_inbox_is_blocked(tmp_path: Path) -> None:
    root = _source_copy(tmp_path)
    path = root / "scripts" / "capture_bayes_basket_target_awc_taf_revision.py"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            "\n\ndef injected_taf_writer(conn, taf_revision):\n"
            "    conn.execute(\"INSERT INTO unrelated VALUES (?)\", (taf_revision,))\n"
        )

    receipt = audit_taf_role_isolation(root)

    assert receipt["status"] == "BLOCKED"
    assert any(
        item["code"] == "forecast_writer_outside_taf_revision_or_inbox"
        for item in receipt["blockers"]
    )


@pytest.mark.parametrize(
    ("function_name", "argument", "sql", "expected_code"),
    (
        (
            "injected_taf_hard_no_writer",
            "taf_revision",
            "INSERT INTO station_day_bounds VALUES (?)",
            "taf_writes_hard_no_or_station_day_bounds",
        ),
        (
            "injected_metar_forecast_writer",
            "metar_observation",
            "INSERT INTO forecast_revision VALUES (?)",
            "metar_speci_writes_forecast_revision",
        ),
    ),
)
def test_cross_domain_writer_routes_are_blocked(
    tmp_path: Path,
    function_name: str,
    argument: str,
    sql: str,
    expected_code: str,
) -> None:
    root = _source_copy(tmp_path)
    path = root / "bayes_basket_target" / "canonical_ingress.py"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            f"\n\ndef {function_name}(conn, {argument}):\n"
            f"    conn.execute({sql!r}, ({argument},))\n"
        )

    receipt = audit_taf_role_isolation(root)

    assert receipt["status"] == "BLOCKED"
    assert any(item["code"] == expected_code for item in receipt["blockers"])


def test_missing_source_file_is_fail_closed(tmp_path: Path) -> None:
    root = _source_copy(tmp_path)
    (root / "bayes_basket_target" / "taf_revision.py").unlink()

    receipt = audit_taf_role_isolation(root)

    assert receipt["status"] == "BLOCKED"
    assert any(
        item["code"] == "source_file_missing"
        and item.get("details", {}).get("file") == "bayes_basket_target/taf_revision.py"
        for item in receipt["blockers"]
    )


def test_scan_limit_is_blocked_before_reading_routes(tmp_path: Path) -> None:
    receipt = audit_taf_role_isolation(_source_copy(tmp_path), max_files=1)

    assert receipt["status"] == "BLOCKED"
    assert any(item["code"] == "scan_file_limit_exceeded" for item in receipt["blockers"])
    assert receipt["counts"]["files_parsed"] == 0


def test_receipt_is_atomic_bounded_and_repeatable(tmp_path: Path) -> None:
    root = _source_copy(tmp_path)
    output = tmp_path / "receipt" / "taf-role-isolation.json"

    first = audit_taf_role_isolation(root, output_path=output)
    first_bytes = output.read_bytes()
    second = audit_taf_role_isolation(root, output_path=output)
    second_bytes = output.read_bytes()

    assert len(first_bytes) < MAX_RECEIPT_BYTES
    assert first_bytes == second_bytes
    assert json.loads(first_bytes) == first == second
    assert not list(output.parent.glob(".*.tmp-*"))
