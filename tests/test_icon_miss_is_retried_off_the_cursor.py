"""An ICON fetch that fails on a pass is named, and asked for again off the cursor.

2026-09-14: the d0 pass at 08:43Z banked ECMWF, GEFS and ECCC and lost ICON.
The subprocess wrote the error into ``icon_capture`` only, which the worker
never copies, so no receipt anywhere said why.  And the retry waited for the
rotation cursor to lap back to one of the date's scopes -- 2.09h achieved --
while DWD, which keeps only the newest run, still had the objects: a
foreground capture of the same run at 11:05Z succeeded in 184s.
"""

from __future__ import annotations

import ast
import inspect
import subprocess
import textwrap
import time
from concurrent.futures import Future
from pathlib import Path

import pytest

import bayes_basket_target.production_worker as pw
from scripts import capture_official_native_ensemble_forward as capture
from tests.test_capture_official_native_ensemble_forward import (
    _install_station_registry,
    _registry,
)


def test_a_failed_icon_fetch_is_named_among_the_held_families(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_station_registry(monkeypatch)
    monkeypatch.setattr(
        capture, "load_market_registry_snapshot", lambda _path: _registry()
    )
    for name, schema in (
        ("capture_gefs_station_days",
         "noaa_gefs_official_historical_multistation_members_v2"),
        ("capture_ecmwf_station_days",
         "ecmwf_ifs_open_pf50_historical_multistation_members_v1"),
    ):
        monkeypatch.setattr(
            capture,
            name,
            lambda _schema=schema, **_kw: {
                "schema": _schema,
                "run_init_utc": "2026-07-30T00:00:00Z",
                "captures": [],
            },
        )

    def icon_read_timeout(**_kw):
        raise RuntimeError("ReadTimeout: opendata.dwd.de")

    monkeypatch.setattr(
        capture, "capture_icon_eps_station_days", icon_read_timeout
    )
    monkeypatch.setattr(
        capture.subprocess,
        "run",
        lambda command, **_kw: subprocess.CompletedProcess(
            command, 0, stdout="built", stderr=""
        ),
    )
    monkeypatch.setattr(
        capture,
        "_verify_official_inbox",
        lambda *_a, **_kw: {"content_sha256": "0" * 64, "packets": []},
    )
    registry_path = tmp_path / "registry.json"
    registry_path.write_text("{}", encoding="utf-8")

    receipt = capture.capture_official_native_forward(
        registry_path=registry_path,
        event_date="2026-08-01",
        gefs_output=tmp_path / "gefs.json",
        ecmwf_output=tmp_path / "ecmwf.json",
        icon_output=tmp_path / "icon.json",
        inbox_output=tmp_path / "inbox.json",
        timeout_seconds=30.0,
        lead_days=2,
        run_cycle_hour=0,
    )

    assert receipt["state"] == "COMMITTED_NO_LIVE"
    held = receipt["family_held_on_earlier_run"]
    assert set(held) == {pw.ICON_TRANSPORT_PROVIDER_ID}
    assert "ReadTimeout" in held[pw.ICON_TRANSPORT_PROVIDER_ID]
    assert receipt["icon_capture"]["error"] == held[
        pw.ICON_TRANSPORT_PROVIDER_ID
    ]


class _Pool:
    def __init__(self) -> None:
        self.submitted = 0

    def submit(self, _fn):
        self.submitted += 1
        return Future()


def _finished(outcome: dict) -> Future:
    future: Future = Future()
    future.set_result(outcome)
    return future


def _worker(tmp_path: Path) -> tuple[pw.TargetProductionWorker, _Pool]:
    worker = object.__new__(pw.TargetProductionWorker)
    worker.nwp_runtime_root = tmp_path
    (tmp_path / "market_registry_forward.json").write_text(
        "{}", encoding="utf-8"
    )
    pool = _Pool()
    worker._official_native_capture_pool = pool
    worker._official_native_capture_inflight = {}
    worker._official_native_capture_attempts = {}
    return worker, pool


def _captured(*, icon_held: bool) -> dict:
    return {
        "state": "CAPTURED",
        "captured_run_init_utc": "2026-09-14T00:00:00Z",
        "families_held_on_earlier_run": (
            {pw.ICON_TRANSPORT_PROVIDER_ID: "RuntimeError:ReadTimeout"}
            if icon_held else
            {"ECMWF_OPEN_DATA_S3": "HTTPStatusError:404"}
        ),
    }


def _cursor_lands_on(worker, *, event_date: str, as_of: str, inbox: Path):
    return worker._capture_official_native_packet(
        station_id="OEJN",
        event_date=event_date,
        as_of_utc=as_of,
        inbox_path=inbox,
    )


def test_a_date_that_lost_icon_is_asked_for_before_the_cursor_returns(
    tmp_path: Path,
) -> None:
    worker, pool = _worker(tmp_path)
    inbox = tmp_path / "inbox.json"
    as_of = "2026-09-14T10:30:00Z"
    worker._official_native_capture_inflight[("2026-09-14",)] = _finished(
        _captured(icon_held=True)
    )
    worker._official_native_capture_attempts[("2026-09-14",)] = (
        time.monotonic() - pw.OFFICIAL_NATIVE_CAPTURE_RETRY_SEC - 1.0
    )

    # The cursor is on another date; its call reaps the 09-14 outcome.
    assert _cursor_lands_on(
        worker, event_date="2026-09-15", as_of=as_of, inbox=inbox
    )["state"] == "SUBMITTED"
    assert worker._official_native_icon_retry_dates == {"2026-09-14"}

    assert worker._retry_icon_held_captures(
        as_of_utc=as_of, inbox_path=inbox
    ) == {"2026-09-14": "SUBMITTED"}
    assert pool.submitted == 2
    # In flight now: the next scope's retry does not stack a second pass.
    assert worker._retry_icon_held_captures(
        as_of_utc=as_of, inbox_path=inbox
    ) == {}
    assert pool.submitted == 2


def test_a_pass_that_brought_icon_home_clears_the_retry(tmp_path: Path) -> None:
    worker, pool = _worker(tmp_path)
    inbox = tmp_path / "inbox.json"
    as_of = "2026-09-14T10:30:00Z"
    worker._official_native_icon_retry_dates = {"2026-09-14"}
    worker._official_native_capture_inflight[("2026-09-14",)] = _finished(
        _captured(icon_held=False)
    )
    worker._official_native_capture_attempts[("2026-09-14",)] = (
        time.monotonic() - pw.OFFICIAL_NATIVE_CAPTURE_RETRY_SEC - 1.0
    )

    _cursor_lands_on(worker, event_date="2026-09-15", as_of=as_of, inbox=inbox)

    assert worker._official_native_icon_retry_dates == set()
    assert worker._retry_icon_held_captures(
        as_of_utc=as_of, inbox_path=inbox
    ) == {}
    assert pool.submitted == 1


def test_an_icon_run_not_yet_on_dwd_waits_and_a_past_date_is_dropped(
    tmp_path: Path,
) -> None:
    worker, pool = _worker(tmp_path)
    inbox = tmp_path / "inbox.json"
    # d2 reads ICON's 12Z run of 09-14, which is not out at 10:30Z; 09-13 is
    # behind the clock and its run is gone.
    worker._official_native_icon_retry_dates = {"2026-09-13", "2026-09-16"}

    assert worker._retry_icon_held_captures(
        as_of_utc="2026-09-14T10:30:00Z", inbox_path=inbox
    ) == {}
    assert pool.submitted == 0
    assert worker._official_native_icon_retry_dates == {"2026-09-16"}

    assert worker._retry_icon_held_captures(
        as_of_utc="2026-09-14T16:30:00Z", inbox_path=inbox
    ) == {"2026-09-16": "SUBMITTED"}
    assert pool.submitted == 1


@pytest.mark.parametrize(
    ("event_date", "as_of", "published"),
    [
        ("2026-09-14", "2026-09-14T03:59:00Z", False),
        ("2026-09-14", "2026-09-14T04:00:00Z", True),
        ("2026-09-15", "2026-09-14T04:00:00Z", True),
        ("2026-09-16", "2026-09-14T15:59:00Z", False),
        ("2026-09-16", "2026-09-14T16:00:00Z", True),
        ("2026-09-17", "2026-09-14T23:00:00Z", False),
        ("2026-09-13", "2026-09-14T10:00:00Z", False),
    ],
)
def test_icon_publication_follows_its_own_lead_contract(
    event_date: str, as_of: str, published: bool
) -> None:
    assert pw._icon_run_published_for(event_date, as_of_utc=as_of) is published


def test_every_rotation_step_offers_the_retry() -> None:
    source = textwrap.dedent(
        inspect.getsource(pw.TargetProductionWorker.refresh_resident_nwp)
    )
    calls = [
        node.func.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    ]
    assert "_retry_icon_held_captures" in calls
