from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from bayes_basket_target import noaa_gefs_archive
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.gefs_raw_reuse import RetainedGEFSField
from bayes_basket_target.native_raw_object_store import store_content_addressed_message


def _field(body: bytes, *, member_id: str = "control") -> RetainedGEFSField:
    digest = hashlib.sha256(body).hexdigest()
    object_member = "gec00" if member_id == "control" else "gep01"
    receipt = {
        "member_id": member_id,
        "forecast_hour": 36,
        "object_key": (
            "gefs.20260706/18/atmos/pgrb2ap5/"
            f"{object_member}.t18z.pgrb2a.0p50.f036"
        ),
        "index_sha256": "a" * 64,
        "index_line": "1:0:d=2026070618:TMP:2 m above ground:",
        "range_start": 10,
        "range_end": 10 + len(body) - 1,
        "message_sha256": digest,
        "last_modified": "Tue, 07 Jul 2026 01:06:17 GMT",
    }
    return RetainedGEFSField(
        run_init_utc="2026-07-06T18:00:00Z",
        member_id=member_id,
        forecast_hour=36,
        message_sha256=digest,
        receipt=receipt,
        retained_at_utc="2026-07-07T01:07:00.000Z",
        source_packet_content_sha256="c" * 64,
        source_packet_ref="/retained/gefs.json",
    )


def test_gefs_content_addressed_raw_is_reused_without_transport(
    tmp_path: Path, monkeypatch,
) -> None:
    body = b"GRIB retained GEFS field bytes"
    field = _field(body)
    store_content_addressed_message(
        body, root=tmp_path, expected_sha256=field.message_sha256
    )
    monkeypatch.setattr(
        noaa_gefs_archive,
        "_get_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("network transport must not be called")
        ),
    )
    message, receipt, response_bytes = noaa_gefs_archive._download_gefs_field(
        client=object(), run_day="20260706", cycle="18",
        object_member="gec00", member_id="control", forecast_hour=36,
        timeout_seconds=1.0, retained=field, raw_roots=(tmp_path,),
        raw_store=tmp_path,
    )
    assert message == body
    assert receipt == field.receipt
    assert response_bytes == 0


def test_gefs_retained_receipt_cannot_relabel_local_member(
    tmp_path: Path,
) -> None:
    body = b"GRIB control field bytes"
    field = _field(body)
    store_content_addressed_message(
        body, root=tmp_path, expected_sha256=field.message_sha256
    )
    with pytest.raises(ContractError, match="gefs_archive_retained_scope_mismatch"):
        noaa_gefs_archive._download_gefs_field(
            client=object(), run_day="20260706", cycle="18",
            object_member="gep01", member_id="perturbed_01", forecast_hour=36,
            timeout_seconds=1.0, retained=field, raw_roots=(tmp_path,),
            raw_store=tmp_path,
        )


def test_gefs_retained_inventory_accepts_both_approved_prefixes(
    tmp_path: Path, monkeypatch,
) -> None:
    for prefix, member_id in (
        ("GEFS_45station", "control"),
        ("NOAA_GEFS_45station", "perturbed_01"),
    ):
        field = _field(member_id.encode(), member_id=member_id)
        identity = {
            "schema": "noaa_gefs_official_historical_multistation_members_v2",
            "ensemble_system_id": "NOAA_GEFS_05",
            "run_init_utc": "2026-07-06T18:00:00Z",
            "archive_query_at_utc": "2026-07-07T01:07:00.000Z",
            "captures": [{"field_receipts": [dict(field.receipt)]}],
        }
        packet = {**identity, "content_sha256": content_sha256(identity)}
        (tmp_path / f"{prefix}_20260708_official_d2.json").write_text(
            json.dumps(packet), encoding="utf-8"
        )
    monkeypatch.setenv("GEFS_RETAINED_PACKET_DIR", str(tmp_path))
    noaa_gefs_archive._retained_fields.cache_clear()
    try:
        fields = noaa_gefs_archive._retained_fields()
    finally:
        noaa_gefs_archive._retained_fields.cache_clear()
    assert {key[2] for key in fields} == {"control", "perturbed_01"}
