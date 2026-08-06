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
    replayed, receipt = ecmwf_ifs_archive._download_message(
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
