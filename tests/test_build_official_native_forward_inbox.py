from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.official_native_forward_packet import (
    ECCC_GEPS_MULTISTATION_SCHEMA,
    ECMWF_MULTISTATION_SCHEMA,
    NOAA_GEFS_MULTISTATION_SCHEMA,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_official_native_forward_inbox.py"


def _archive(
    path: Path,
    *,
    schema: str,
    system_id: str,
    family_id: str,
    intervals: dict[str, list[float | None]],
    station_ids: tuple[str, ...] = ("ZUUU",),
) -> Path:
    captures = []
    for station_id in station_ids:
        members = [
            {
                "member_id": "member_01",
                "member_role": "PERTURBED",
                "raw_tmax_c": 29.25,
            },
            {
                "member_id": "member_02",
                "member_role": "PERTURBED",
                "raw_tmax_c": 30.25,
            },
        ]
        member_identity = [
            {
                "member_id": row["member_id"],
                "member_role": row["member_role"],
                "raw_tmax_c": row["raw_tmax_c"],
            }
            for row in members
        ]
        capture_identity = {
            "station_id": station_id,
            "event_date": "2026-08-01",
            "settlement_timezone": "Asia/Shanghai",
            "run_cycle": "18Z",
            "ensemble_system_id": system_id,
            "independence_family_id": family_id,
            "member_count": len(members),
            "members": members,
            "member_set_sha256": content_sha256(member_identity),
            "bucket_intervals": intervals,
            "bucket_contract_sha256": content_sha256(intervals),
        }
        captures.append({
            **capture_identity,
            "content_sha256": content_sha256(capture_identity),
        })
    archive_identity = {
        "schema": schema,
        "event_date": "2026-08-01",
        "archive_lead_bucket": "d2",
        "run_cycle": "18Z",
        "ensemble_system_id": system_id,
        "independence_family_id": family_id,
        "member_count": 2,
        "run_init_utc": "2026-07-30T18:00:00Z",
        "archive_query_at_utc": "2026-07-31T00:00:00Z",
        "captures": captures,
        "training_only": True,
        "holdout_eligible": False,
    }
    payload = {
        **archive_identity,
        "content_sha256": content_sha256(archive_identity),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _registry(path: Path) -> Path:
    buckets = [
        ("29°C or below", None, 29, "m-low"),
        ("30°C", 30, 30, "m-mid"),
        ("31°C or higher", 31, None, "m-high"),
    ]
    rows = []
    for outcome, lower, upper, market_id in buckets:
        rows.append({
            "slug": f"highest-temperature-{market_id}",
            "event_date": "2026-08-01",
            "outcome": outcome,
            "city_slug": "chengdu",
            "station_id": "ZUUU",
            "settlement_timezone": "Asia/Shanghai",
            "settlement_unit": "C",
            "source_lane": "amsc",
            "source_eligible": 1,
            "source_block_reason": "",
            "resolution_url": "https://example.invalid/rules",
            "rules_sha256": "a" * 64,
            "yes_token": f"yes-{market_id}",
            "no_token": f"no-{market_id}",
            "market_active": 1,
            "market_accepting_orders": 1,
            "registry_json": json.dumps({
                "market_id": market_id,
                "lower": lower,
                "upper": upper,
                "condition_id": f"condition-{market_id}",
            }),
            "refreshed_at": "2026-07-31T00:00:00Z",
        })
    identity = {
        "schema": "bayes_basket_target_market_registry_snapshot_v1",
        "source_schema": "reported_trajectory_market_registry_v1",
        "source_scope": "shared_live_registry_export_only",
        "source_database_path": "/isolated/test.db",
        "exported_at_utc": "2026-07-31T00:00:00Z",
        "source_row_count": len(rows),
        "row_count": len(rows),
        "rows": rows,
    }
    payload = {
        **identity,
        "snapshot_sha256": content_sha256(identity),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _run(
    tmp_path: Path,
    archives: list[Path],
    *,
    eccc: list[Path] | None = None,
    registry: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    output = tmp_path / "official_native_ensemble_forward_inbox.json"
    command = [sys.executable, str(SCRIPT)]
    for archive in archives:
        command.extend(["--archive", str(archive)])
    for archive in eccc or []:
        command.extend(["--eccc-geps-archive", str(archive)])
    if registry is not None:
        command.extend(["--market-registry", str(registry)])
    command.extend(["--station-id", "ZUUU", "--output", str(output)])
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    payload = json.loads(output.read_text(encoding="utf-8")) if output.is_file() else {}
    return result, payload


def _base_archives(tmp_path: Path, *, mismatched: bool = False) -> list[Path]:
    gefs_intervals = {
        "29°C or below": [None, 29.5],
        "30°C or higher": [29.5, None],
    }
    ecmwf_intervals = (
        {
            "28°C or below": [None, 28.5],
            "29°C": [28.5, 29.5],
            "30°C or higher": [29.5, None],
        }
        if mismatched
        else gefs_intervals
    )
    return [
        _archive(
            tmp_path / "gefs.json",
            schema=NOAA_GEFS_MULTISTATION_SCHEMA,
            system_id="NOAA_GEFS_05",
            family_id="NOAA_GEFS",
            intervals=gefs_intervals,
        ),
        _archive(
            tmp_path / "ecmwf.json",
            schema=ECMWF_MULTISTATION_SCHEMA,
            system_id="ECMWF_IFS_ENS_OPEN_PF50",
            family_id="ECMWF",
            intervals=ecmwf_intervals,
        ),
    ]


def test_optional_eccc_archive_joins_three_independent_families(
    tmp_path: Path,
) -> None:
    archives = _base_archives(tmp_path)
    eccc = _archive(
        tmp_path / "eccc.json",
        schema=ECCC_GEPS_MULTISTATION_SCHEMA,
        system_id="ECCC_GEM_GLOBAL_ENS",
        family_id="ECCC_GEM",
        intervals={
            "29°C or below": [None, 29.5],
            "30°C or higher": [29.5, None],
        },
    )
    result, inbox = _run(tmp_path, archives, eccc=[eccc])

    assert result.returncode == 0, result.stderr
    packet = inbox["packets"][0]
    assert {
        system["ensemble_system_id"] for system in packet["systems"]
    } == {
        "NOAA_GEFS_05",
        "ECMWF_IFS_ENS_OPEN_PF50",
        "ECCC_GEM_GLOBAL_ENS",
    }
    assert set(packet["bucket_intervals"]) == {
        "29°C or below",
        "30°C or higher",
    }
    assert {
        row["independence_family_id"]
        for row in packet["source_archives"]
    } == {"NOAA_GEFS", "ECMWF", "ECCC_GEM"}
    assert {
        row["transport_provider_id"]
        for row in packet["source_archives"]
    } == {"NOAA_GEFS_PDS_S3", "ECMWF_OPEN_DATA_S3", "ECCC_DD_WEATHER"}
    expected_archive_hashes = {}
    for path in [*archives, eccc]:
        archive = json.loads(path.read_text(encoding="utf-8"))
        identity = {
            key: value
            for key, value in archive.items()
            if key != "content_sha256"
        }
        expected_archive_hashes[archive["independence_family_id"]] = (
            content_sha256(identity)
        )
    assert {
        row["independence_family_id"]: row["archive_content_sha256"]
        for row in packet["source_archives"]
    } == expected_archive_hashes
    assert packet["calls_order_gateway"] is False
    assert packet["orders_submitted"] == 0
    assert packet["live_submit_authorized"] is False


def test_missing_optional_eccc_keeps_exact_two_family_packet(
    tmp_path: Path,
) -> None:
    result, inbox = _run(tmp_path, _base_archives(tmp_path))

    assert result.returncode == 0, result.stderr
    packet = inbox["packets"][0]
    assert {
        row["independence_family_id"] for row in packet["source_archives"]
    } == {"NOAA_GEFS", "ECMWF"}
    assert all(
        "ECCC" not in str(row)
        for row in packet["source_archives"]
    )


def test_station_without_eccc_capture_falls_back_to_two_families(
    tmp_path: Path,
) -> None:
    archives = _base_archives(tmp_path)
    eccc = _archive(
        tmp_path / "eccc-other-station.json",
        schema=ECCC_GEPS_MULTISTATION_SCHEMA,
        system_id="ECCC_GEM_GLOBAL_ENS",
        family_id="ECCC_GEM",
        intervals={
            "29°C or below": [None, 29.5],
            "30°C or higher": [29.5, None],
        },
        station_ids=("CYYZ",),
    )
    result, inbox = _run(tmp_path, archives, eccc=[eccc])

    assert result.returncode == 0, result.stderr
    packet = inbox["packets"][0]
    assert {
        row["independence_family_id"] for row in packet["source_archives"]
    } == {"NOAA_GEFS", "ECMWF"}


def test_three_system_packet_uses_authoritative_registry_contract(
    tmp_path: Path,
) -> None:
    archives = _base_archives(tmp_path, mismatched=True)
    eccc = _archive(
        tmp_path / "eccc.json",
        schema=ECCC_GEPS_MULTISTATION_SCHEMA,
        system_id="ECCC_GEM_GLOBAL_ENS",
        family_id="ECCC_GEM",
        intervals={
            "27°C or below": [None, 27.5],
            "28°C or higher": [27.5, None],
        },
    )
    result, inbox = _run(
        tmp_path,
        archives,
        eccc=[eccc],
        registry=_registry(tmp_path / "registry.json"),
    )

    assert result.returncode == 0, result.stderr
    packet = inbox["packets"][0]
    assert packet["bucket_intervals"] == {
        "29°C or below": [None, 29.5],
        "30°C": [29.5, 30.5],
        "31°C or higher": [30.5, None],
    }
    assert packet["bucket_contract_authority"]["source_class"] == (
        "AUTHORITATIVE_MARKET_REGISTRY_STATION_DAY"
    )
    assert {
        row["source_bucket_contract_sha256"]
        for row in packet["source_archives"]
    } == {
        content_sha256(archive["captures"][0]["bucket_intervals"])
        for archive in [
            json.loads(path.read_text(encoding="utf-8"))
            for path in [*archives, eccc]
        ]
    }


def test_explicit_eccc_option_rejects_non_eccc_archive(tmp_path: Path) -> None:
    archives = _base_archives(tmp_path)
    result, _ = _run(tmp_path, archives[:1], eccc=[archives[1]])

    assert result.returncode != 0
    assert "official_native_forward_eccc_archive_schema_invalid" in (
        result.stderr + result.stdout
    )
