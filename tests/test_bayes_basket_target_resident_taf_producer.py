from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

import bayes_basket_target.resident_taf_producer as producer
from bayes_basket_target import ContractError, content_sha256
from bayes_basket_target.event_contracts import validate_forecast_revision_chain
from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json
from bayes_basket_target.taf_revision import (
    TAF_NWP_CROSSFIT_ARTIFACT_KIND,
    TAF_NWP_LIVE_COMPATIBILITY_CONTRACT_VERSION,
)


AS_OF = "2026-07-28T03:00:00Z"
STATION = "RKPK"
DAY = "2026-07-28"
TIMEZONE = "Asia/Seoul"


def _artifact(**overrides) -> dict:
    source = (
        Path(__file__).resolve().parents[1]
        / "config"
        / "artifacts"
        / "bayes_basket_target_taf_revision_active.json"
    )
    raw = json.loads(source.read_text(encoding="utf-8"))
    # The shipped file became a registry envelope -- {schema, artifacts:[...],
    # content_sha256} -- when bound artifacts started being kept beside the
    # registry they assemble into.  This helper wants one artifact to use as
    # a template, and handing the envelope to the single-artifact parser is
    # what left twenty-five tests in this file failing on
    # taf_revision_artifact_unknown_fields:artifacts rather than on anything
    # they assert.  Production reads the same file through
    # validate_taf_artifact_registry and was never affected.
    members = raw.get("artifacts")
    if isinstance(members, list) and members and isinstance(members[0], dict):
        raw = dict(members[0])
    raw.update({
        "station_id": STATION,
        "settlement_timezone": TIMEZONE,
        "trained_through_utc": "2026-07-25T15:00:00Z",
        "valid_from_utc": "2026-07-26T00:00:00Z",
        "valid_until_utc": "2026-09-01T00:00:00Z",
        "nwp_combination_artifact_kind": TAF_NWP_CROSSFIT_ARTIFACT_KIND,
        "nwp_live_compatibility_contract_version": (
            TAF_NWP_LIVE_COMPATIBILITY_CONTRACT_VERSION
        ),
        "nwp_live_compatibility_fingerprint_sha256": "4" * 64,
        "nwp_live_compatibility_evidence_sha256": "5" * 64,
        "nwp_live_compatibility_validated": True,
    })
    raw.update(overrides)
    identity = {key: value for key, value in raw.items() if key != "content_sha256"}
    return {**identity, "content_sha256": content_sha256(identity)}


def _capture(
    *,
    sequence: int = 1,
    kind: str = "TAF",
    received: str = "2026-07-28T02:00:00Z",
    supersedes: str | None = None,
    tx_c: float = 31.0,
    station: str = STATION,
    day: str = DAY,
    timezone_name: str = TIMEZONE,
    revision_id: str | None = None,
) -> dict:
    revision_id = revision_id or f"revision-{station}-{day}-{sequence}"
    raw_taf = (
        f"{station} {kind} SEQ{sequence} TX{int(tx_c):02d}/2812Z "
        "FEW020 18008KT"
    )
    raw_sha = hashlib.sha256(raw_taf.encode("utf-8")).hexdigest()
    revision = {
        "schema": "bayes_basket_forecast_revision_v1",
        "revision_id": revision_id,
        "station_id": station,
        "event_date": day,
        "revision_kind": kind,
        "sequence": sequence,
        "issued_at_utc": received,
        "received_at_utc": received,
        "valid_from_utc": "2026-07-27T18:00:00Z",
        "valid_until_utc": "2026-07-29T00:00:00Z",
        "source_object_sha256": raw_sha,
        "supersedes_revision_id": supersedes,
    }
    identity = {
        "schema": "bayes_basket_target_forward_taf_capture_v1",
        "source_class": "official_forward_native_receipt",
        "provider": "NOAA Aviation Weather Center",
        "station_id": station,
        "event_date": day,
        "settlement_timezone": timezone_name,
        "source_url": "https://aviationweather.gov/api/data/taf",
        "response_sha256": "a" * 64,
        "response_bytes": 256,
        "provider_available_at_utc": received,
        "local_received_at_utc": received,
        "forecast_revision": revision,
        "taf_fields": {
            "tx_c": tx_c,
            "cloud_cover_fraction": 0.25,
            "precipitation_probability": 0.10,
            "wind_speed_mps": 4.0,
        },
        "raw_taf": raw_taf,
        "raw_taf_sha256": raw_sha,
        "boundary": {
            "native_local_receipt": True,
            "retrospective_replay_only": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _inbox(captures: list[dict]) -> dict:
    identity = {
        "schema": producer.INBOX_SCHEMA,
        "capture_count": len(captures),
        "captures": captures,
        "boundary": {
            "forward_only": True,
            "max_captures": producer.MAX_CAPTURES,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _inputs(tmp_path: Path, captures: list[dict]) -> dict:
    artifact = tmp_path / "source-artifact.json"
    source_inbox = tmp_path / "source-inbox.json"
    write_bounded_json(artifact, _artifact())
    write_bounded_json(source_inbox, _inbox(captures))
    return {
        "source_inbox_paths": [source_inbox],
        "artifact_path": artifact,
        "output_inbox_path": tmp_path / "resident-inbox.json",
        "output_artifact_path": tmp_path / "resident-artifact.json",
        "as_of_utc": AS_OF,
    }


def test_builds_exact_worker_inputs_and_is_zero_write_when_unchanged(
    tmp_path: Path,
) -> None:
    kwargs = _inputs(tmp_path, [_capture()])
    first = producer.refresh_resident_taf_inputs(**kwargs)

    assert first["state"] == "updated"
    assert first["capture_count"] == 1
    assert first["station_count"] == 1
    assert first["checkpoint_contract"] == {
        "stage": "P0-M",
        "single_atomic_checkpoint": True,
        "tx_calibration": True,
        "cloud_precipitation_wind_secondary_confirmation": True,
        "second_probability_center_forbidden": True,
        "revision_chain_root": "same_P0_checkpoint",
    }
    inbox = load_bounded_json(kwargs["output_inbox_path"])
    artifact = load_bounded_json(kwargs["output_artifact_path"])
    assert inbox["schema"] == "bayes_basket_target_taf_revision_inbox_v1"
    assert artifact["schema"] == "bayes_basket_taf_revision_artifact_v2"
    assert artifact["content_sha256"] == first["artifact_content_sha256"]
    before = (
        kwargs["output_inbox_path"].stat().st_mtime_ns,
        kwargs["output_artifact_path"].stat().st_mtime_ns,
    )

    second = producer.refresh_resident_taf_inputs(**kwargs)

    assert second["state"] == "unchanged_zero_write"
    assert before == (
        kwargs["output_inbox_path"].stat().st_mtime_ns,
        kwargs["output_artifact_path"].stat().st_mtime_ns,
    )


def test_missing_formal_inbox_emits_not_configured_without_outputs(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "artifact.json"
    write_bounded_json(artifact, _artifact())
    output_inbox = tmp_path / "resident-inbox.json"
    output_artifact = tmp_path / "resident-artifact.json"

    result = producer.refresh_resident_taf_inputs(
        source_inbox_paths=[],
        artifact_path=artifact,
        output_inbox_path=output_inbox,
        output_artifact_path=output_artifact,
        as_of_utc=AS_OF,
    )

    assert result["state"] == "not_configured"
    assert result["missing"] == ["forward_forecast_revision_inbox"]
    assert result["writes_production_inputs"] is False
    assert not output_inbox.exists()
    assert not output_artifact.exists()


def test_unbridged_taf_artifact_fails_before_publishing_inputs(
    tmp_path: Path,
) -> None:
    artifact = _artifact()
    for key in (
        "nwp_combination_artifact_kind",
        "nwp_live_compatibility_contract_version",
        "nwp_live_compatibility_fingerprint_sha256",
        "nwp_live_compatibility_evidence_sha256",
        "nwp_live_compatibility_validated",
    ):
        artifact.pop(key)
    artifact["content_sha256"] = content_sha256({
        key: value
        for key, value in artifact.items()
        if key != "content_sha256"
    })
    artifact_path = tmp_path / "unbridged.json"
    inbox_path = tmp_path / "source.json"
    write_bounded_json(artifact_path, artifact)
    write_bounded_json(inbox_path, _inbox([_capture()]))

    with pytest.raises(
        ContractError,
        match="nwp_live_compatibility_bridge_missing",
    ):
        producer.refresh_resident_taf_inputs(
            source_inbox_paths=[inbox_path],
            artifact_path=artifact_path,
            output_inbox_path=tmp_path / "out-inbox.json",
            output_artifact_path=tmp_path / "out-artifact.json",
            as_of_utc=AS_OF,
        )
    assert not (tmp_path / "out-inbox.json").exists()
    assert not (tmp_path / "out-artifact.json").exists()


def test_missing_artifact_is_not_configured_not_fake_activation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.json"
    write_bounded_json(source, _inbox([_capture()]))

    result = producer.refresh_resident_taf_inputs(
        source_inbox_paths=[source],
        artifact_path=tmp_path / "missing-artifact.json",
        output_inbox_path=tmp_path / "resident-inbox.json",
        output_artifact_path=tmp_path / "resident-artifact.json",
        as_of_utc=AS_OF,
    )

    assert result["state"] == "not_configured"
    assert "activated_strict_oos_taf_artifact" in result["missing"]
    assert not (tmp_path / "resident-artifact.json").exists()


@pytest.mark.parametrize(
    ("artifact_changes", "error"),
    [
        (
            {"valid_until_utc": "2026-07-28T03:00:00Z"},
            "expired_or_not_yet_valid",
        ),
        (
            {"trained_through_utc": "2026-07-26T00:00:00Z"},
            "not_strict_oos",
        ),
        (
            {"sample_n": 23, "effective_sample_n": 15.0},
            "sample_n_below_strict_oos_min",
        ),
        (
            {"correlation_discount": 0.9},
            "correlation_discount_invalid",
        ),
        # Ruling 2026-08-15: a lost out-of-sample comparison no longer
        # withholds a P0-M artifact, so this case moved to its own test
        # below -- the field is now a verdict the artifact carries, not a
        # condition of its existence.
        (
            {"authority_scope": "RESEARCH_ONLY"},
            "not_active_for_production_no_live",
        ),
    ],
)
def test_artifact_oos_activation_and_correlation_fail_closed(
    tmp_path: Path,
    artifact_changes: dict,
    error: str,
) -> None:
    kwargs = _inputs(tmp_path, [_capture()])
    write_bounded_json(kwargs["artifact_path"], _artifact(**artifact_changes))

    with pytest.raises(ContractError, match=error):
        producer.refresh_resident_taf_inputs(**kwargs)

    assert not kwargs["output_inbox_path"].exists()


def test_artifact_serves_despite_lost_oos_comparison(tmp_path: Path) -> None:
    """P0-M publishes on sample; the losing verdict rides along."""

    kwargs = _inputs(tmp_path, [_capture()])
    write_bounded_json(
        kwargs["artifact_path"],
        _artifact(joint_physical_oos_validated=False),
    )

    result = producer.refresh_resident_taf_inputs(**kwargs)

    assert result["capture_count"] == 1


def test_timezone_mismatch_and_bare_taf_fail_closed(tmp_path: Path) -> None:
    mismatch = _inputs(
        tmp_path / "timezone",
        [_capture(timezone_name="America/New_York")],
    )
    with pytest.raises(ContractError, match="settlement_timezone_mismatch"):
        producer.refresh_resident_taf_inputs(**mismatch)

    bare_capture = _capture()
    bare_identity = {
        key: value
        for key, value in bare_capture.items()
        if key not in {"content_sha256", "taf_fields"}
    }
    bare = {
        **bare_identity,
        "content_sha256": content_sha256(bare_identity),
    }
    bare_kwargs = _inputs(tmp_path / "bare", [bare])
    with pytest.raises(ContractError, match="bare_taf_forbidden"):
        producer.refresh_resident_taf_inputs(**bare_kwargs)


def test_raw_taf_and_arrival_cannot_be_forged_or_moved(tmp_path: Path) -> None:
    forged = _capture()
    forged["raw_taf"] += " FORGED"
    identity = {key: value for key, value in forged.items() if key != "content_sha256"}
    forged["content_sha256"] = content_sha256(identity)
    kwargs = _inputs(tmp_path / "raw", [forged])
    with pytest.raises(ContractError, match="raw_content_sha_mismatch"):
        producer.refresh_resident_taf_inputs(**kwargs)

    moved = _capture()
    moved["local_received_at_utc"] = "2026-07-28T02:00:01Z"
    identity = {key: value for key, value in moved.items() if key != "content_sha256"}
    moved["content_sha256"] = content_sha256(identity)
    kwargs = _inputs(tmp_path / "clock", [moved])
    with pytest.raises(ContractError, match="local_receipt_clock_mismatch"):
        producer.refresh_resident_taf_inputs(**kwargs)

    future = _inputs(
        tmp_path / "future",
        [_capture(received="2026-07-28T03:00:01Z")],
    )
    with pytest.raises(ContractError, match="future_arrival_forbidden"):
        producer.refresh_resident_taf_inputs(**future)


def test_revision_validity_must_overlap_registry_timezone_day(
    tmp_path: Path,
) -> None:
    capture = _capture()
    capture["forecast_revision"]["valid_from_utc"] = "2026-07-26T00:00:00Z"
    capture["forecast_revision"]["valid_until_utc"] = "2026-07-27T00:00:00Z"
    identity = {
        key: value for key, value in capture.items() if key != "content_sha256"
    }
    capture["content_sha256"] = content_sha256(identity)
    kwargs = _inputs(tmp_path, [capture])

    with pytest.raises(ContractError, match="settlement_window_mismatch"):
        producer.refresh_resident_taf_inputs(**kwargs)


def test_taf_amd_cor_low_high_low_chain_merges_without_rewind(
    tmp_path: Path,
) -> None:
    root = _capture(tx_c=29.0)
    amd = _capture(
        sequence=2,
        kind="AMD",
        received="2026-07-28T02:10:00Z",
        supersedes=root["forecast_revision"]["revision_id"],
        tx_c=34.0,
    )
    cor = _capture(
        sequence=3,
        kind="COR",
        received="2026-07-28T02:20:00Z",
        supersedes=amd["forecast_revision"]["revision_id"],
        tx_c=29.0,
    )
    kwargs = _inputs(tmp_path, [root])
    producer.refresh_resident_taf_inputs(**kwargs)

    write_bounded_json(kwargs["source_inbox_paths"][0], _inbox([root, amd]))
    high = producer.refresh_resident_taf_inputs(**kwargs)
    assert high["capture_count"] == 2

    write_bounded_json(
        kwargs["source_inbox_paths"][0],
        _inbox([root, amd, cor]),
    )
    low = producer.refresh_resident_taf_inputs(**kwargs)
    assert low["capture_count"] == 3
    output = load_bounded_json(kwargs["output_inbox_path"])
    revisions = validate_forecast_revision_chain(
        item["forecast_revision"] for item in output["captures"]
    )
    assert [item.revision_kind for item in revisions] == ["TAF", "AMD", "COR"]
    assert [
        item["taf_fields"]["tx_c"] for item in output["captures"]
    ] == [29.0, 34.0, 29.0]

    # A source rewind to the root cannot delete the already confirmed chain.
    write_bounded_json(kwargs["source_inbox_paths"][0], _inbox([root]))
    rewind = producer.refresh_resident_taf_inputs(**kwargs)
    assert rewind["capture_count"] == 3
    assert rewind["state"] == "unchanged_zero_write"


def test_multiple_low_high_low_turns_remain_one_linear_checkpoint_chain(
    tmp_path: Path,
) -> None:
    captures = [_capture(tx_c=29.0)]
    predecessor = captures[0]["forecast_revision"]["revision_id"]
    for sequence in range(2, 12):
        high = sequence % 2 == 0
        row = _capture(
            sequence=sequence,
            kind="AMD" if high else "COR",
            received=f"2026-07-28T02:{sequence:02d}:00Z",
            supersedes=predecessor,
            tx_c=35.0 if high else 29.0,
        )
        captures.append(row)
        predecessor = row["forecast_revision"]["revision_id"]
    kwargs = _inputs(tmp_path, captures)

    result = producer.refresh_resident_taf_inputs(**kwargs)
    output = load_bounded_json(kwargs["output_inbox_path"])

    assert result["capture_count"] == 11
    assert [
        capture["taf_fields"]["tx_c"] for capture in output["captures"]
    ] == [29.0, 35.0, 29.0, 35.0, 29.0, 35.0, 29.0, 35.0, 29.0, 35.0, 29.0]
    assert len({
        capture["forecast_revision"]["station_id"]
        for capture in output["captures"]
    }) == 1
    for _ in range(100):
        replay = producer.refresh_resident_taf_inputs(**kwargs)
        assert replay["state"] == "unchanged_zero_write"
        assert replay["inbox_content_sha256"] == result["inbox_content_sha256"]


def test_artifact_generation_replaces_old_inbox_scope_without_cross_merge(
    tmp_path: Path,
) -> None:
    old = _capture(tx_c=29.0)
    kwargs = _inputs(tmp_path, [old])
    producer.refresh_resident_taf_inputs(**kwargs)

    new_artifact = _artifact(
        artifact_id="taf-rkpk-new-generation",
        activation_id="taf-rkpk-new-generation-activation",
    )
    write_bounded_json(kwargs["artifact_path"], new_artifact)
    new_root = _capture(
        revision_id="new-generation-root",
        tx_c=33.0,
    )
    write_bounded_json(kwargs["source_inbox_paths"][0], _inbox([new_root]))

    result = producer.refresh_resident_taf_inputs(**kwargs)
    output = load_bounded_json(kwargs["output_inbox_path"])

    assert result["artifact_generation_transition"] is True
    assert result["capture_count"] == 1
    assert output["captures"][0]["forecast_revision"]["revision_id"] == (
        "new-generation-root"
    )
    assert load_bounded_json(kwargs["output_artifact_path"])[
        "artifact_id"
    ] == "taf-rkpk-new-generation"


def test_sequence_gap_and_same_id_different_content_fail_closed(
    tmp_path: Path,
) -> None:
    root = _capture()
    gap = _capture(
        sequence=3,
        kind="COR",
        received="2026-07-28T02:20:00Z",
        supersedes=root["forecast_revision"]["revision_id"],
    )
    gap_kwargs = _inputs(tmp_path / "gap", [root, gap])
    with pytest.raises(ContractError, match="sequence_gap_or_duplicate"):
        producer.refresh_resident_taf_inputs(**gap_kwargs)

    duplicate = _capture(revision_id=root["forecast_revision"]["revision_id"], tx_c=35.0)
    conflict_kwargs = _inputs(tmp_path / "conflict", [root])
    producer.refresh_resident_taf_inputs(**conflict_kwargs)
    write_bounded_json(
        conflict_kwargs["source_inbox_paths"][0],
        _inbox([duplicate]),
    )
    with pytest.raises(ContractError, match="revision_id_content_conflict"):
        producer.refresh_resident_taf_inputs(**conflict_kwargs)


def test_source_limits_and_output_path_collision_fail_closed(
    tmp_path: Path,
) -> None:
    kwargs = _inputs(tmp_path / "collision", [_capture()])
    kwargs["output_artifact_path"] = kwargs["output_inbox_path"]
    with pytest.raises(ContractError, match="output_paths_must_be_distinct"):
        producer.refresh_resident_taf_inputs(**kwargs)

    too_many = _inputs(tmp_path / "many", [_capture()])
    too_many["source_inbox_paths"] = (
        too_many["source_inbox_paths"] * (producer.MAX_SOURCE_INBOXES + 1)
    )
    with pytest.raises(ContractError, match="source_inbox_count_invalid"):
        producer.refresh_resident_taf_inputs(**too_many)


def test_oversized_source_fails_before_json_parse(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.json"
    source = tmp_path / "oversized.json"
    write_bounded_json(artifact, _artifact())
    source.write_bytes(b" " * (producer.MAX_SOURCE_INBOX_BYTES + 1))

    with pytest.raises(ContractError, match="source_inbox_size_invalid"):
        producer.refresh_resident_taf_inputs(
            source_inbox_paths=[source],
            artifact_path=artifact,
            output_inbox_path=tmp_path / "out-inbox.json",
            output_artifact_path=tmp_path / "out-artifact.json",
            as_of_utc=AS_OF,
        )

    assert not list(tmp_path.glob(".*.tmp.*"))


def test_failure_leaves_no_temporary_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = _inputs(tmp_path, [_capture()])
    real_write = producer.write_bounded_json_if_changed
    calls = 0

    def fail_second(path, value, *, limit):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected-inbox-write-failure")
        return real_write(path, value, limit=limit)

    monkeypatch.setattr(producer, "write_bounded_json_if_changed", fail_second)
    with pytest.raises(OSError, match="injected"):
        producer.refresh_resident_taf_inputs(**kwargs)

    assert not kwargs["output_inbox_path"].exists()
    assert not list(tmp_path.glob(".*.tmp.*"))


def test_cli_writes_explicit_not_configured_evidence(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    evidence = tmp_path / "evidence.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(
                root
                / "scripts"
                / "build_bayes_basket_target_resident_taf_inputs.py"
            ),
            "--artifact",
            str(tmp_path / "missing.json"),
            "--output-inbox",
            str(tmp_path / "resident-inbox.json"),
            "--output-artifact",
            str(tmp_path / "resident-artifact.json"),
            "--as-of-utc",
            AS_OF,
            "--evidence-output",
            str(evidence),
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )

    result = load_bounded_json(evidence)
    assert result["state"] == "not_configured"
    assert json.loads(completed.stdout)["evidence_sha256"] == (
        result["evidence_sha256"]
    )
    assert evidence.stat().st_size < 1024 * 1024
    assert not (tmp_path / "resident-inbox.json").exists()


def test_one_pre_root_capture_cannot_discard_its_siblings() -> None:
    """The root gate is per capture, not batch-min.

    The inbox carries every forward capture for a scope on every cycle, so a
    single revision received before the scope's first P0 root -- measured
    2026-08-20: ZGGG event 2026-08-21, a TX-less 15:06Z TAF received 16:23Z
    against a first root at 17:31Z -- dragged ``earliest_taf`` in front of
    every root forever, and the whole batch was discarded each cycle,
    including the TX-carrying 21:04Z revision that satisfied the contract on
    its own.  Seven stations sat in IGNORED_NO_P0_BEFORE_FORECAST with no
    way out, because the blocking capture never leaves the inbox.

    A capture with no root before it cannot seat itself -- dropping it alone
    is the per-capture application of the same rule.
    """

    from pathlib import Path as _Path

    source = (
        _Path(__file__).resolve().parents[1]
        / "bayes_basket_target"
        / "production_worker.py"
    ).read_text(encoding="utf-8")
    # The anchor is computed over the captures that survive the root gate.
    assert "first_root_at" in source
    assert "dropped_pre_root = len(captures) - len(seatable)" in source
    # And the receipt says how many were dropped, so a scope losing captures
    # is visible rather than silent.
    assert '"dropped_pre_root_captures": dropped_pre_root,' in source
    # The old arithmetic -- batch-min over every capture straight into the
    # root filter with no per-capture gate -- must not come back: the state
    # is emitted from exactly one receipt site.
    assert source.count('"state": "IGNORED_NO_P0_BEFORE_FORECAST"') == 1

