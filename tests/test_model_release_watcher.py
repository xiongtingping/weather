from datetime import date, datetime, timezone
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import model_release_watcher as watcher


class _FakeResponse:
    def __init__(self, status_code=200, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class _FakeGfsSession:
    def __init__(self, *, listed_keys, head_status=404):
        contents = "".join(
            f"<Contents><Key>{key}</Key><LastModified>2026-06-30T09:35:01.000Z</LastModified></Contents>"
            for key in listed_keys
        )
        self._list_text = f"<ListBucketResult>{contents}</ListBucketResult>"
        self._head_status = head_status

    def get(self, url, timeout):
        return _FakeResponse(status_code=200, text=self._list_text)

    def head(self, url, timeout, allow_redirects=True):
        headers = {}
        if 200 <= self._head_status < 300:
            headers["Last-Modified"] = "Tue, 30 Jun 2026 09:35:01 GMT"
        return _FakeResponse(status_code=self._head_status, headers=headers)


def test_gfs_peak_window_hours_for_next_day_from_12z_cycle():
    required = watcher._required_forecast_hours_for_peak_window(
        model="gfs_seamless",
        cycle=datetime(2026, 6, 30, 12, tzinfo=timezone.utc),
        event_date=date(2026, 7, 1),
        peak_start_hour=12,
        peak_end_hour=18,
    )

    assert required == [16, 17, 18, 19, 20, 21, 22]


def test_gfs_no_files_distinguishes_empty_official_run_prefix():
    result = watcher._watch_model(
        session=_FakeGfsSession(listed_keys=[]),
        model="gfs_seamless",
        event_date=date(2026, 7, 1),
        now=datetime(2026, 6, 30, 15, 20, tzinfo=timezone.utc),
        timeout_sec=1,
        peak_start_hour=12,
        peak_end_hour=18,
        previous={},
    )

    assert result["run_id"] == "2026063012"
    assert result["status"] == "no_files"
    assert result["status_reason"] == "official_run_prefix_empty"
    assert result["probe_metadata"]["gfs_official_prefix"] == "gfs.20260630/12/atmos/"
    assert result["probe_metadata"]["gfs_official_prefix_empty"] is True


def test_gfs_no_files_distinguishes_missing_peak_hours_from_empty_prefix():
    result = watcher._watch_model(
        session=_FakeGfsSession(
            listed_keys=[
                "gfs.20260630/12/atmos/gfs.t12z.pgrb2.0p25.f000",
                "gfs.20260630/12/atmos/gfs.t12z.pgrb2.0p25.f001",
            ]
        ),
        model="gfs_seamless",
        event_date=date(2026, 7, 1),
        now=datetime(2026, 6, 30, 15, 20, tzinfo=timezone.utc),
        timeout_sec=1,
        peak_start_hour=12,
        peak_end_hour=18,
        previous={},
    )

    assert result["status"] == "no_files"
    assert result["status_reason"] == "official_peak_window_forecast_hours_not_available"
    assert result["probe_metadata"]["gfs_official_prefix_empty"] is False
    assert result["probe_metadata"]["gfs_official_prefix_key_count"] == 2
    assert result["probe_metadata"]["gfs_required_hour_keys_present"] == {
        "16": False,
        "17": False,
        "18": False,
        "19": False,
        "20": False,
        "21": False,
        "22": False,
    }


def test_configure_release_session_uses_explicit_official_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9999")
    monkeypatch.setenv("PM_OFFICIAL_NWP_PROXY", "http://127.0.0.1:7890")
    session = watcher.requests.Session()
    watcher.configure_official_requests_session(session)
    assert session.trust_env is False
    assert session.proxies == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }
