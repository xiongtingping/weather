from __future__ import annotations

import hashlib
from pathlib import Path

from bayes_basket_target import ecmwf_ifs_archive
from bayes_basket_target.ecmwf_pf50_raw_reuse import (
    RetainedMemberField,
    find_content_addressed_message,
    store_content_addressed_message,
)


def _field(body: bytes) -> RetainedMemberField:
    digest = hashlib.sha256(body).hexdigest()
    receipt = {
        "member_id": "perturbed_01",
        "member_number": 1,
        "forecast_step": 36,
        "object_key": "20260706/18z/ifs/0p25/enfo/x.grib2",
        "index_sha256": "a" * 64,
        "index_row_sha256": "b" * 64,
        "range_start": 10,
        "range_end": 10 + len(body) - 1,
        "message_sha256": digest,
        "last_modified": "Tue, 07 Jul 2026 01:06:17 GMT",
    }
    return RetainedMemberField(
        ensemble_system_id="ECMWF_IFS_ENS_OPEN_PF50",
        run_init_utc="2026-07-06T18:00:00Z",
        member_id="perturbed_01",
        forecast_step=36,
        message_sha256=digest,
        receipt=receipt,
        retained_at_utc="2026-07-07T01:07:00.000Z",
        source_packet_content_sha256="c" * 64,
        source_packet_ref="/retained/ecmwf.json",
    )


def test_content_addressed_raw_is_reused_without_transport(
    tmp_path: Path,
    monkeypatch,
) -> None:
    body = b"GRIB retained member bytes"
    field = _field(body)
    stored = store_content_addressed_message(
        body, root=tmp_path, expected_sha256=field.message_sha256
    )
    assert find_content_addressed_message(field.message_sha256, (tmp_path,)) == stored

    def forbidden(*_args, **_kwargs):
        raise AssertionError("network transport must not be called")

    monkeypatch.setattr(ecmwf_ifs_archive, "_get_bytes", forbidden)
    replayed, receipt, from_local_store = ecmwf_ifs_archive._download_message(
        grib_url="https://storage.googleapis.com/ecmwf-open-data/object",
        index_sha256="c" * 64,
        object_key="mirror-object",
        row={"_offset": 0, "_length": len(body), "number": 1, "step": 36},
        timeout_seconds=1.0,
        retained=field,
        raw_roots=(tmp_path,),
        raw_store=tmp_path,
    )
    assert replayed == body
    assert receipt == field.receipt
    assert from_local_store is True


def test_a_repeat_range_is_served_from_the_store_without_transport(
    tmp_path: Path,
    monkeypatch,
) -> None:
    # Content addressing cannot answer "have I fetched this before?" -- the
    # digest is only known after the download.  Two event dates served by the
    # same run overlap by roughly half their forecast steps, and each shared
    # step is the same immutable object range, so the second capture re-fetched
    # every one of them.  Recording the source coordinate on the way in is what
    # lets the second capture ask.
    body = b"GRIB message for a shared forecast step" * 8
    row = {"_offset": 512, "_length": len(body), "number": 7, "step": 24}

    def transport(*_args, **_kwargs):
        return body, {"last-modified": "Wed, 13 Aug 2026 00:00:00 GMT"}

    monkeypatch.setattr(ecmwf_ifs_archive, "_get_bytes", transport)
    first, first_receipt, first_from_store = ecmwf_ifs_archive._download_message(
        grib_url="https://storage.googleapis.com/ecmwf-open-data/object",
        index_sha256="d" * 64,
        object_key="shared-object",
        row=dict(row),
        timeout_seconds=1.0,
        raw_roots=(tmp_path,),
        raw_store=tmp_path,
    )
    assert first == body
    assert first_from_store is False

    def forbidden(*_args, **_kwargs):
        raise AssertionError("network transport must not be called")

    monkeypatch.setattr(ecmwf_ifs_archive, "_get_bytes", forbidden)
    second, second_receipt, second_from_store = ecmwf_ifs_archive._download_message(
        grib_url="https://storage.googleapis.com/ecmwf-open-data/object",
        index_sha256="d" * 64,
        object_key="shared-object",
        row=dict(row),
        timeout_seconds=1.0,
        raw_roots=(tmp_path,),
        raw_store=tmp_path,
    )
    assert second == body
    assert second_from_store is True
    # The receipt must describe the same object, including the header recorded
    # when these bytes were first fetched -- inventing a fresh value would
    # claim a response that was never made.
    assert second_receipt == first_receipt


def test_a_different_range_of_the_same_object_is_not_served_from_the_store(
    tmp_path: Path,
    monkeypatch,
) -> None:
    body = b"first range" * 4
    monkeypatch.setattr(
        ecmwf_ifs_archive,
        "_get_bytes",
        lambda *_a, **_k: (body, {"last-modified": ""}),
    )
    ecmwf_ifs_archive._download_message(
        grib_url="https://storage.googleapis.com/ecmwf-open-data/object",
        index_sha256="e" * 64,
        object_key="shared-object",
        row={"_offset": 0, "_length": len(body), "number": 1, "step": 3},
        timeout_seconds=1.0,
        raw_roots=(tmp_path,),
        raw_store=tmp_path,
    )

    other = b"second range!" * 4
    calls: list[int] = []

    def transport(*_args, **_kwargs):
        calls.append(1)
        return other, {"last-modified": ""}

    monkeypatch.setattr(ecmwf_ifs_archive, "_get_bytes", transport)
    fetched, _receipt, from_store = ecmwf_ifs_archive._download_message(
        grib_url="https://storage.googleapis.com/ecmwf-open-data/object",
        index_sha256="e" * 64,
        object_key="shared-object",
        row={"_offset": 4096, "_length": len(other), "number": 1, "step": 6},
        timeout_seconds=1.0,
        raw_roots=(tmp_path,),
        raw_store=tmp_path,
    )
    assert fetched == other
    assert from_store is False
    assert calls == [1]
