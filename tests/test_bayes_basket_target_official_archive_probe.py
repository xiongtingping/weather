from __future__ import annotations

from datetime import datetime, timezone
import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.official_archive_probe import probe_official_archives


class _Response:
    def __init__(self, *, status: int = 200, body: bytes = b"", content_length: int | None = None):
        self.status = status
        self.headers = {
            "Content-Type": "application/octet-stream",
            "Last-Modified": "Thu, 23 Jul 2026 05:30:00 GMT",
        }
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        return self._body[:size] if size >= 0 else self._body


def test_probe_records_gfs_idx_and_never_retains_payload() -> None:
    def opener(url, *, method, timeout_sec):
        if url.endswith("/00/"):
            return _Response(body=b'<a href="gfs.t00z.pgrb2.0p25.f018">x</a>')
        return _Response(content_length=552728368 if not url.endswith(".idx") else 41261)

    result = probe_official_archives(
        ["2025-01-01"],
        opener=opener,
        probed_at_utc="2026-07-24T00:00:00Z",
    )

    assert result["sources"]["gfs_0p25_official"]["historical_rows_with_native_metadata"] == 1
    assert result["date_probes"][0]["gfs_range_extraction_possible"] is True
    assert result["sources"]["icon_global_official"]["exact_file_probe_count"] == 5
    assert result["sources"]["icon_global_official"]["exact_file_probe_available_count"] == 5
    assert result["icon_exact_file_probes"][0]["url"].endswith(
        "2025010100_000_T_2M.grib2.bz2"
    )
    assert result["boundary"]["raw_payload_downloaded"] is False
    assert result["boundary"]["raw_payload_bytes_retained"] == 0
    assert result["receipt_sha256"] == content_sha256({k: v for k, v in result.items() if k != "receipt_sha256"})


def test_probe_rejects_empty_dates() -> None:
    with pytest.raises(ContractError, match="archive_probe_dates_empty"):
        probe_official_archives([], opener=lambda *_args, **_kwargs: None)


def test_probe_can_cover_multiple_cycles_without_changing_default_shape() -> None:
    calls = []

    def opener(url, *, method, timeout_sec):
        calls.append(url)
        return _Response(content_length=1)

    result = probe_official_archives(
        ["2025-01-01"],
        run_cycles=("00", "12"),
        opener=opener,
    )

    assert result["requested_run_cycles_utc"] == ["00", "12"]
    assert [row["run_cycle_utc"] for row in result["date_probes"]] == ["00", "12"]
    assert set(result["icon_current_listings"]) == {"00", "12"}
    assert any("/12z/" in url for url in calls)
    assert any("/12/t_2m/" in url for url in calls)


def test_probe_separates_date_directory_absence_from_exact_file_retention() -> None:
    def opener(url, *, method, timeout_sec):
        if "/t_2m/" in url and "2025010100_000" in url:
            raise RuntimeError("old_file_not_retained")
        return _Response(content_length=1)

    result = probe_official_archives(["2025-01-01"], opener=opener)
    icon = result["sources"]["icon_global_official"]
    assert icon["exact_file_probe_count"] == 5
    assert icon["exact_file_probe_available_count"] == 4
    first = result["icon_exact_file_probes"][0]
    assert first["status"] == "ERROR"
    assert first["url"].endswith("2025010100_000_T_2M.grib2.bz2")


def test_probe_rejects_unknown_cycle() -> None:
    with pytest.raises(ContractError, match="archive_probe_cycle_invalid"):
        probe_official_archives(["2025-01-01"], run_cycles=("03",), opener=lambda *_args, **_kwargs: None)


def test_probe_uses_bounded_listing_and_marks_oversized_index() -> None:
    def opener(url, *, method, timeout_sec):
        if url.endswith("/00/"):
            return _Response(body=b"x" * (128 * 1024 + 1))
        return _Response(content_length=1)

    result = probe_official_archives(["2025-01-01"], opener=opener)
    assert result["icon_current_listing"]["status"] == "BLOCKED"


def test_probe_converts_http_error_to_unavailable() -> None:
    class _NotFound(Exception):
        code = 404

    def opener(url, *, method, timeout_sec):
        if "ecmwf" in url:
            raise _NotFound("missing")
        return _Response(content_length=1)

    result = probe_official_archives(["2025-01-01"], opener=opener)
    assert result["date_probes"][0]["ecmwf"]["status"] == "UNAVAILABLE"
