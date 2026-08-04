from __future__ import annotations

from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
import bz2
import hashlib
import io

import pytest

import scripts.run_bayes_basket_target_icon_eps_forward_capture as capture
from bayes_basket_target.contracts import ContractError


def test_plan_is_one_official_global_eps_lead_and_fail_closed() -> None:
    plan = capture.build_plan(
        run_initialization_utc="2026-08-01T00:00:00Z",
        forecast_hour=24,
        as_of_utc="2026-08-01T06:00:00Z",
    )
    assert plan["product_id"] == "DWD_ICON_GLOBAL_EPS"
    assert plan["expected_member_count"] == 40
    assert plan["source_url"].endswith("_024_t_2m.grib2.bz2")
    assert plan["authority"]["native_arrival_index_eligible_before_decode"] is False
    assert plan["boundary"]["single_lead_only"] is True


@pytest.mark.parametrize("lead", [49, 50, 73, 77, 121, 131])
def test_plan_rejects_unpublished_sparse_leads(lead: int) -> None:
    with pytest.raises(ContractError, match="forecast_hour_not_published"):
        capture.build_plan(
            run_initialization_utc="2026-08-01T00:00:00Z",
            forecast_hour=lead,
            as_of_utc="2026-08-01T06:00:00Z",
        )


def test_execute_streams_content_addressed_raw_but_does_not_authorize_p0(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = bz2.compress(b"GRIB" + b"native-eps-member-container")

    class Response(io.BytesIO):
        status = 200

        def __init__(self) -> None:
            super().__init__(payload)
            headers = Message()
            headers["Content-Length"] = str(len(payload))
            headers["Last-Modified"] = "Sat, 01 Aug 2026 02:37:21 GMT"
            headers["ETag"] = '"native"'
            self.headers = headers

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    monkeypatch.setattr(capture, "open_official_https", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(capture, "assert_target_raw_retention_budget", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(capture, "assert_process_memory_budget", lambda: None)
    monkeypatch.setattr(
        capture.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": capture.MIN_FREE_BYTES + capture.MAX_RESPONSE_BYTES + 1})(),
    )
    plan = capture.build_plan(
        run_initialization_utc="2026-08-01T00:00:00Z",
        forecast_hour=0,
        as_of_utc="2026-08-01T06:00:00Z",
    )
    receipt = tmp_path / "receipt.json"
    result = capture.execute_plan(plan, output_root=tmp_path / "raw-root", receipt_path=receipt)
    assert result["state"] == "OFFICIAL_RAW_CAPTURED_MEMBER_DECODE_PENDING"
    assert result["raw_sha256"] == hashlib.sha256(payload).hexdigest()
    assert result["native_arrival_index_eligible"] is False
    assert result["formal_p0_eligible"] is False
    assert result["boundary"]["orders_submitted"] == 0
    assert Path(result["raw_object_ref"]).read_bytes() == payload


def test_safe_url_rejects_non_dwd_or_query() -> None:
    with pytest.raises(ContractError, match="source_url_invalid"):
        capture._safe_url("https://open-meteo.com/icon-eps.grib2.bz2")
    with pytest.raises(ContractError, match="source_url_invalid"):
        capture._safe_url(
            "https://opendata.dwd.de/weather/nwp/icon-eps/grib/00/t_2m/"
            "icon-eps_global_icosahedral_single-level_2026080100_000_t_2m.grib2.bz2?x=1"
        )
