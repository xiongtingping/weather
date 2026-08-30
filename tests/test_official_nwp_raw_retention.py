from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time

import pytest

import scripts.official_nwp_raw_retention as retention_module
from scripts.official_nwp_raw_retention import (
    acknowledge_missing_references,
    apply_budget_cold_archive,
    apply_native_unreferenced,
    apply_unreferenced,
    build_native_plan,
    build_plan,
    plan_budget_cold_archive,
)


@pytest.fixture(autouse=True)
def _no_host_capture_processes(monkeypatch) -> None:
    """The host runs continuous captures; the lane guard is tested directly."""

    monkeypatch.setattr(
        retention_module, "_native_active_capture_processes", lambda: []
    )


def _raw(root: Path, provider: str, body: bytes, suffix: str = ".grib2") -> Path:
    digest = hashlib.sha256(body).hexdigest()
    path = root / provider / "raw" / f"{digest}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def test_retention_protects_referenced_raw_and_plans_unreferenced(tmp_path: Path) -> None:
    curve_root = tmp_path / "curve"
    referenced = _raw(curve_root, "icon", b"referenced")
    unreferenced = _raw(curve_root, "icon", b"unreferenced")
    history = curve_root / "icon" / "history" / "snapshot.json"
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text(
        json.dumps({"raw_object_ref": str(referenced), "raw_sha256": referenced.name.split(".", 1)[0]})
        + "\n",
        encoding="utf-8",
    )

    plan = build_plan(curve_root, providers=("icon",))

    assert plan["active_capture_processes"] == []
    assert [row["path"] for row in plan["unreferenced_files"]] == [str(unreferenced)]
    assert plan["old_referenced_files"] == []

    applied = apply_unreferenced(plan)
    assert applied["deleted_count"] == 1
    assert applied["deleted_bytes"] == len(b"unreferenced")
    assert referenced.is_file()
    assert not unreferenced.exists()


def test_retention_plan_does_not_treat_missing_provider_as_blocking(tmp_path: Path) -> None:
    plan = build_plan(tmp_path / "curve", providers=("ecmwf", "icon", "gfs"))

    assert plan["state"] == "retention_plan_ready"
    assert plan["apply_allowed"] is True
    assert all(row["raw_file_count"] == 0 for row in plan["provider_rows"])


def test_retention_blocks_apply_when_an_existing_raw_reference_is_dangling(tmp_path: Path) -> None:
    curve_root = tmp_path / "curve"
    history = curve_root / "icon" / "history" / "snapshot.json"
    history.parent.mkdir(parents=True, exist_ok=True)
    missing = curve_root / "icon" / "raw" / ("a" * 64 + ".grib2.bz2")
    history.write_text(json.dumps({"raw_object_ref": str(missing)}) + "\n", encoding="utf-8")

    plan = build_plan(curve_root, providers=("icon",))

    assert plan["missing_raw_reference_count"] == 1
    assert plan["apply_allowed"] is False


def test_retention_keeps_referenced_old_run_and_discards_only_unreferenced(tmp_path: Path) -> None:
    curve_root = tmp_path / "curve"
    old_raw = _raw(curve_root, "icon", b"old")
    new_raw = _raw(curve_root, "icon", b"new")
    receipts = curve_root / "icon" / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    for name, run, raw in (("old.json", "2026-07-20T00:00:00Z", old_raw), ("new.json", "2026-07-23T00:00:00Z", new_raw)):
        (receipts / name).write_text(
            json.dumps(
                {
                    "provider_id": "icon",
                    "run_at_utc": run,
                    "valid_time_utc": run,
                    "raw_receipts": [
                        {
                            "raw_object_ref": str(raw),
                            "raw_sha256": raw.name.split(".", 1)[0],
                            "requested_at": run,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    plan = build_plan(curve_root, providers=("icon",), retain_latest_runs=1)

    assert plan["provider_rows"][0]["hot_runs"] == ["2026-07-23T00:00:00Z"]
    assert [row["path"] for row in plan["old_referenced_files"]] == [str(old_raw)]
    assert [row["path"] for row in plan["hot_files"]] == [str(new_raw)]

    applied = apply_unreferenced(plan)

    assert applied["deleted_count"] == 0
    assert old_raw.exists()
    assert new_raw.exists()


def test_acknowledge_missing_references_removes_only_the_cleanup_block(tmp_path: Path) -> None:
    curve_root = tmp_path / "curve"
    missing = curve_root / "icon" / "raw" / ("b" * 64 + ".grib2.bz2")
    history = curve_root / "icon" / "history" / "snapshot.json"
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text(json.dumps({"raw_object_ref": str(missing)}) + "\n", encoding="utf-8")

    plan = build_plan(curve_root, providers=("icon",))
    acknowledged = acknowledge_missing_references(plan)
    refreshed = build_plan(curve_root, providers=("icon",))

    assert acknowledged["acknowledged_count"] == 1
    assert refreshed["missing_raw_reference_count"] == 0
    assert refreshed["acknowledged_missing_raw_reference_count"] == 1
    assert refreshed["apply_allowed"] is True


def _native_body(store: Path, body: bytes, suffix: str = ".grib2", *, age_days: float = 30.0) -> Path:
    digest = hashlib.sha256(body).hexdigest()
    store.mkdir(parents=True, exist_ok=True)
    path = store / f"{digest}{suffix}"
    path.write_bytes(body)
    stamp = time.time() - age_days * 86400.0
    os.utime(path, (stamp, stamp))
    return path


def test_native_lane_reads_references_from_roots_outside_the_object_store(tmp_path: Path) -> None:
    """The packet that keeps a native body alive lives under output/audits."""

    source_root = tmp_path / "official_nwp_sources"
    store = source_root / "ecmwf_pf50" / "raw"
    referenced = _native_body(store, b"referenced")
    unreferenced = _native_body(store, b"unreferenced")
    audits = tmp_path / "audits"
    audits.mkdir()
    (audits / "packet.json").write_text(
        json.dumps(
            {
                "captures": [
                    {
                        "field_receipts": [
                            {
                                "object_key": "20260810/00z/ifs/enfo.grib2",
                                "range_start": 0,
                                "range_end": 9,
                                "message_sha256": referenced.name.split(".", 1)[0],
                            }
                        ]
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    plan = build_native_plan([source_root], reference_roots=[audits])

    assert plan["referenced_hash_count"] == 1
    assert [row["path"] for row in plan["unreferenced_files"]] == [str(unreferenced)]

    applied = apply_native_unreferenced(plan)

    assert applied["deleted_count"] == 1
    assert referenced.is_file()
    assert not unreferenced.exists()
    manifest = source_root / "raw_cold_archive_manifest.jsonl"
    recorded = json.loads(manifest.read_text(encoding="utf-8").strip())
    assert recorded["sha256"] == unreferenced.name.split(".", 1)[0]
    assert recorded["state"] == "body_discarded"


def test_native_lane_ignores_the_range_alias_download_index(tmp_path: Path) -> None:
    """``<store>/ranges/<coordinate>.json`` is a cache index, not a reference.

    The alias names the body's ``message_sha256`` so a re-run can skip the
    download, but it does not need the body: the lookup verifies the digest and
    falls back to a fresh download when the file is gone.  Counting it made
    every body in ``ecmwf_pf50``/``gefs`` look referenced and the daily pass
    delete nothing for thirteen days while the store grew 2.3 GiB a day.
    """

    from bayes_basket_target.native_raw_object_store import store_range_alias

    source_root = tmp_path / "official_nwp_sources"
    store = source_root / "ecmwf_pf50" / "raw"
    body = _native_body(store, b"cached-range")
    store_range_alias(
        root=store,
        object_key="20260810/00z/ifs/0p25/enfo/20260810000000-30h-enfo-ef.grib2",
        range_start=0,
        range_end=11,
        message_sha256=body.name.split(".", 1)[0],
        last_modified="Mon, 10 Aug 2026 01:00:00 GMT",
    )
    alias_dir = store / "ranges"
    assert list(alias_dir.glob("*.json")), "alias layout changed; update the skip"

    # The source root is itself a reference root in production (data/runtime).
    plan = build_native_plan([source_root], reference_roots=[tmp_path])

    assert plan["referenced_hash_count"] == 0
    assert plan["skipped_range_alias_dirs"] == [str(alias_dir.resolve())]
    assert [row["path"] for row in plan["unreferenced_files"]] == [str(body)]

    apply_native_unreferenced(plan)

    assert not body.exists()
    # The index itself survives; a later hit simply re-downloads.
    assert list(alias_dir.glob("*.json"))


def test_native_lane_withholds_bodies_young_enough_to_be_mid_capture(tmp_path: Path) -> None:
    source_root = tmp_path / "official_nwp_sources"
    store = source_root / "gefs" / "raw"
    fresh = _native_body(store, b"fresh", age_days=0.5)
    stale = _native_body(store, b"stale", age_days=9.0)

    plan = build_native_plan([source_root], reference_roots=[tmp_path / "absent"], min_age_days=2)

    assert [row["path"] for row in plan["unreferenced_files"]] == [str(stale)]
    assert [row["path"] for row in plan["withheld_young_files"]] == [str(fresh)]

    apply_native_unreferenced(plan)

    assert fresh.is_file()
    assert not stale.exists()


def test_native_lane_never_deletes_receipts_filed_beside_the_bodies(tmp_path: Path) -> None:
    """``gem_gdps_official/raw`` holds ``<sha>.receipt.json`` evidence."""

    source_root = tmp_path / "model_arrivals"
    store = source_root / "gem_gdps_official" / "raw"
    receipt = _native_body(store, b"receipt-evidence", suffix=".receipt.json")
    payload = _native_body(store, b"open-meteo-response", suffix=".json")

    plan = build_native_plan([source_root], reference_roots=[tmp_path / "absent"])

    assert plan["unreferenced_files"] == []
    assert plan["in_store_json_delete_forbidden"] is True
    reported = {row["path"] for row in plan["in_store_evidence_files"]}
    assert reported == {str(receipt), str(payload)}

    apply_native_unreferenced(plan)

    assert receipt.is_file()
    assert payload.is_file()


def test_native_lane_discovers_nested_stores_and_attributes_a_provider(tmp_path: Path) -> None:
    source_root = tmp_path / "model_arrivals"
    nested = source_root / "icon_global_eps_official" / "grid_r03b06" / "raw"
    _native_body(nested, b"grid-asset", suffix=".nc.bz2")

    plan = build_native_plan([source_root], reference_roots=[tmp_path / "absent"])

    assert [row["provider"] for row in plan["store_rows"]] == ["icon_global_eps_official"]
    assert plan["unreferenced_count"] == 1


def test_native_lane_refuses_the_pass_when_a_reference_document_is_unreadable(tmp_path: Path) -> None:
    source_root = tmp_path / "official_nwp_sources"
    _native_body(source_root / "gefs" / "raw", b"body")
    audits = tmp_path / "audits"
    audits.mkdir()
    blocked = audits / "packet.json"
    blocked.write_text("{}", encoding="utf-8")
    blocked.chmod(0o000)
    try:
        with pytest.raises(RuntimeError, match="reference_unreadable"):
            build_native_plan([source_root], reference_roots=[audits])

        plan = build_native_plan(
            [source_root], reference_roots=[audits], allow_unreadable_references=True
        )
    finally:
        blocked.chmod(0o600)

    assert plan["unreadable_reference_document_count"] == 1
    assert plan["unreferenced_count"] == 1


def test_native_lane_ignores_its_own_cold_archive_manifest(tmp_path: Path) -> None:
    """A tombstone must not resurrect a re-downloaded body as referenced."""

    source_root = tmp_path / "official_nwp_sources"
    store = source_root / "gefs" / "raw"
    body = _native_body(store, b"redownloaded")
    (source_root / "raw_cold_archive_manifest.jsonl").write_text(
        json.dumps({"state": "body_discarded", "sha256": body.name.split(".", 1)[0]}) + "\n",
        encoding="utf-8",
    )

    plan = build_native_plan([source_root], reference_roots=[source_root])

    assert [row["path"] for row in plan["unreferenced_files"]] == [str(body)]


def test_native_lane_ignores_its_own_audit_output_under_a_reference_root(tmp_path: Path) -> None:
    """The plan lands in output/audits, which is itself a reference root."""

    source_root = tmp_path / "official_nwp_sources"
    store = source_root / "ecmwf_pf50" / "raw"
    body = _native_body(store, b"orphan")
    audits = tmp_path / "audits"
    audits.mkdir()

    first = build_native_plan([source_root], reference_roots=[audits])
    assert [row["path"] for row in first["unreferenced_files"]] == [str(body)]

    (audits / "official_nwp_native_raw_retention_plan_latest.json").write_text(
        json.dumps(first), encoding="utf-8"
    )
    second = build_native_plan([source_root], reference_roots=[audits])

    assert second["referenced_hash_count"] == 0
    assert [row["path"] for row in second["unreferenced_files"]] == [str(body)]
    assert second["skipped_self_plan_documents"] == [
        str(audits / "official_nwp_native_raw_retention_plan_latest.json")
    ]


def test_budget_lane_takes_the_fewest_oldest_bodies_that_fit_the_root(tmp_path: Path) -> None:
    source_root = tmp_path / "model_arrivals"
    store = source_root / "icon_global_official" / "raw"
    audits = tmp_path / "audits"
    audits.mkdir()
    bodies = []
    for index in range(4):
        body = _native_body(store, b"x" * 1000 + str(index).encode(), age_days=30 - index)
        bodies.append(body)
    (audits / "packet.json").write_text(
        json.dumps([path.name.split(".", 1)[0] for path in bodies]), encoding="utf-8"
    )

    plan = build_native_plan([source_root], reference_roots=[audits])
    assert plan["unreferenced_count"] == 0
    assert plan["cold_archive_candidate_count"] == 4

    total = sum(path.stat().st_size for path in bodies)
    selection = plan_budget_cold_archive(
        plan,
        budget_root=source_root,
        budget_bytes=total - bodies[0].stat().st_size,
        store_keys=["icon_global_official/raw"],
    )

    assert selection["fits_budget"] is True
    # Oldest first, and only as many as the budget actually requires.
    assert [row["path"] for row in selection["selected_files"]] == [str(bodies[0])]

    apply_budget_cold_archive(plan, selection)

    assert not bodies[0].exists()
    assert all(path.is_file() for path in bodies[1:])
    manifest = json.loads((source_root / "raw_cold_archive_manifest.jsonl").read_text().strip())
    assert manifest["deletion_reason"] == "referenced_body_cold_archived_to_hold_root_budget"
    assert manifest["receipt_retained_outside_store"] is True


def test_budget_lane_counts_the_unreferenced_purge_it_runs_first(tmp_path: Path) -> None:
    """One pass, two lanes: the free bytes must not be bought twice.

    ``_run_native_lane`` discards the unreferenced bodies before the budget
    selection is applied, so measuring the selection against the pre-purge tree
    would discard re-obtainable bodies the budget never needed.
    """

    source_root = tmp_path / "model_arrivals"
    store = source_root / "icon_global_official" / "raw"
    audits = tmp_path / "audits"
    audits.mkdir()
    named = [
        _native_body(store, b"y" * 1000 + str(index).encode(), age_days=30 - index)
        for index in range(3)
    ]
    orphan = _native_body(store, b"z" * 1000, age_days=40)
    (audits / "packet.json").write_text(
        json.dumps([path.name.split(".", 1)[0] for path in named]), encoding="utf-8"
    )

    plan = build_native_plan([source_root], reference_roots=[audits])
    assert [row["path"] for row in plan["unreferenced_files"]] == [str(orphan)]

    each = named[0].stat().st_size
    total = sum(path.stat().st_size for path in named) + orphan.stat().st_size
    # The orphan alone closes the gap; nothing referenced needs to go.
    selection = plan_budget_cold_archive(
        plan,
        budget_root=source_root,
        budget_bytes=total - orphan.stat().st_size,
        store_keys=["icon_global_official/raw"],
    )

    assert selection["pending_unreferenced_bytes"] == orphan.stat().st_size
    assert selection["selected_files"] == []
    assert selection["fits_budget"] is True

    # One body deeper than the orphan: exactly one referenced body, the oldest.
    deeper = plan_budget_cold_archive(
        plan,
        budget_root=source_root,
        budget_bytes=total - orphan.stat().st_size - each,
        store_keys=["icon_global_official/raw"],
    )

    assert [row["path"] for row in deeper["selected_files"]] == [str(named[0])]


def test_budget_lane_only_touches_the_stores_it_was_given(tmp_path: Path) -> None:
    source_root = tmp_path / "model_arrivals"
    audits = tmp_path / "audits"
    audits.mkdir()
    named = _native_body(source_root / "icon_global_official" / "raw", b"named", age_days=40)
    spared = _native_body(source_root / "ecmwf_ifs025_official" / "raw", b"spared", age_days=99)
    (audits / "packet.json").write_text(
        json.dumps([named.name.split(".", 1)[0], spared.name.split(".", 1)[0]]),
        encoding="utf-8",
    )

    plan = build_native_plan([source_root], reference_roots=[audits])
    selection = plan_budget_cold_archive(
        plan,
        budget_root=source_root,
        budget_bytes=0,
        store_keys=["icon_global_official/raw"],
    )

    # ecmwf is older and would be taken first by age alone, but it was not named.
    assert [row["path"] for row in selection["selected_files"]] == [str(named)]
    assert selection["fits_budget"] is False


def test_budget_lane_never_takes_a_body_named_only_from_inside_its_own_store(
    tmp_path: Path,
) -> None:
    """A body whose only receipt sits beside it loses its evidence if dropped."""

    source_root = tmp_path / "model_arrivals"
    store = source_root / "gem_gdps_official" / "raw"
    body = _native_body(store, b"body", age_days=40)
    digest = body.name.split(".", 1)[0]
    (store / f"{digest}.receipt.json").write_text(
        json.dumps({"raw_sha256": digest}), encoding="utf-8"
    )

    plan = build_native_plan([source_root], reference_roots=[source_root])

    assert plan["referenced_hash_count"] == 1
    assert plan["cold_archive_candidate_count"] == 0
    selection = plan_budget_cold_archive(
        plan,
        budget_root=source_root,
        budget_bytes=0,
        store_keys=["gem_gdps_official/raw"],
    )
    assert selection["selected_files"] == []
