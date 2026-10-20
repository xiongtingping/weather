"""A capture that lost the network says so and is retried in minutes.

2026-10-20: ``capture_deterministic_forward`` ran 53 s at 07:11Z with the
AIFS Single and HRES 00Z runs published, stored neither -- the transport
helpers had folded the dead connection into "not published" -- and was next
due at 10:11Z.  User 「所有采集车道都应该增加"网络断了"重试」.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import network_retry  # noqa: E402
from bayes_basket_target.network_retry import (  # noqa: E402
    NetworkUnavailable, is_network_error, network_retry_delay,
    output_names_network_failure,
)


@pytest.fixture(scope="module")
def daemon():
    spec = importlib.util.spec_from_file_location(
        "weather_basket_yes_daemon_network_retry",
        ROOT / "scripts" / "weather_basket_yes_daemon.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script(tmp_path: Path, body: str) -> str:
    path = tmp_path / "sweep.sh"
    path.write_text("#!/bin/bash\n" + body + "\n", encoding="utf-8")
    return str(path)


def test_transport_errors_are_network_and_others_are_not() -> None:
    assert is_network_error(httpx.ConnectError("eof"))
    assert is_network_error(httpx.ReadTimeout("slow"))
    assert is_network_error(NetworkUnavailable("x"))
    wrapped = RuntimeError("outer")
    wrapped.__cause__ = httpx.ConnectError("eof")
    assert is_network_error(wrapped)
    assert not is_network_error(ValueError("bad grib"))
    assert not is_network_error(FileNotFoundError("no such file"))
    assert not is_network_error(KeyError("k"))


def test_a_dead_sweeps_tail_is_read_for_transport_errors() -> None:
    assert output_names_network_failure(
        "httpx.ConnectError: [SSL: UNEXPECTED_EOF_WHILE_READING]"
    )
    assert output_names_network_failure("socket.gaierror: [Errno 8]")
    assert not output_names_network_failure("KeyError: 'station_id'")


def test_the_delay_doubles_from_five_minutes_and_stops_at_the_interval() -> None:
    assert network_retry_delay(10800.0, 1) == 300.0
    assert network_retry_delay(10800.0, 2) == 600.0
    assert network_retry_delay(10800.0, 4) == 2400.0
    assert network_retry_delay(10800.0, 9) == 10800.0
    assert network_retry_delay(1200.0, 1) == 300.0
    assert network_retry_delay(120.0, 1) == 120.0


@pytest.mark.parametrize("module_name", [
    "aifs_single_point_archive", "native_deterministic_point_archive",
    "noaa_conus_archive", "dwd_icon_eu_point_archive",
])
def test_a_helper_whose_retries_all_fail_on_the_wire_raises(
    monkeypatch, module_name
) -> None:
    module = importlib.import_module(f"bayes_basket_target.{module_name}")

    class _Dead:
        def get(self, *_a, **_k):
            raise httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING]")

    monkeypatch.setattr(module, "_client", lambda: _Dead())
    kwargs = {"cap": 1 << 20} if module_name == "native_deterministic_point_archive" else {}
    with pytest.raises(NetworkUnavailable):
        module._get("https://example.invalid/x", **kwargs)


@pytest.mark.parametrize("module_name", [
    "aifs_single_point_archive", "native_deterministic_point_archive",
    "noaa_conus_archive", "dwd_icon_eu_point_archive",
])
def test_only_the_sources_own_404_still_means_not_published(
    monkeypatch, module_name
) -> None:
    module = importlib.import_module(f"bayes_basket_target.{module_name}")

    class _Missing:
        def get(self, *_a, **_k):
            return httpx.Response(404)

    monkeypatch.setattr(module, "_client", lambda: _Missing())
    kwargs = {"cap": 1 << 20} if module_name == "native_deterministic_point_archive" else {}
    assert module._get("https://example.invalid/x", **kwargs) is None


def test_a_noted_failure_prints_the_marker_once_at_exit() -> None:
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r)\n"
         "from bayes_basket_target.network_retry import note_network_failure\n"
         "note_network_failure('AIFS 00Z ConnectError')\n"
         "note_network_failure('HRES 00Z ConnectError')\n"
         "print('pass done')" % str(ROOT)],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    assert out.count(network_retry.HOSTED_SWEEP_NETWORK_RETRY_MARKER) == 1
    assert out[-1] == network_retry.HOSTED_SWEEP_NETWORK_RETRY_MARKER


def test_a_clean_run_that_lost_the_network_retries_in_minutes(
    daemon, tmp_path
) -> None:
    result = daemon.run_hosted_sweep(
        _script(
            tmp_path,
            'echo "AIFS: 计划 2 run，失败 2"\n'
            f'echo "{network_retry.HOSTED_SWEEP_NETWORK_RETRY_MARKER}"',
        ),
        30.0,
    )
    assert result["state"] == "network_retry"
    assert result["continue"] is False
    assert daemon.hosted_sweep_interval_after(result, 10800.0) == 300.0


def test_a_sweep_that_died_of_a_transport_error_retries_too(
    daemon, tmp_path
) -> None:
    result = daemon.run_hosted_sweep(
        _script(tmp_path, 'echo "httpx.ConnectError: [Errno 61]" >&2\nexit 1'),
        30.0,
    )
    assert result["state"] == "network_retry"
    other = daemon.run_hosted_sweep(
        _script(tmp_path, 'echo "KeyError: station_id" >&2\nexit 1'),
        30.0,
    )
    assert other["state"] == "failed"
    assert daemon.hosted_sweep_interval_after(other, 10800.0) == 10800.0


def test_the_streak_doubles_the_delay_and_any_other_outcome_resets_it(
    daemon,
) -> None:
    entry = None
    for expected in (1, 2, 3):
        entry = daemon.hosted_sweep_clock_entry(
            entry, {"state": "network_retry", "duration_sec": 3.0},
            name="capture_deterministic_forward", now_utc="2026-10-20T08:00:00Z",
        )
        assert entry["network_retry_streak"] == expected
        assert "completed_at_utc" not in entry
    outcome = daemon.hosted_sweep_boot_outcome(entry)
    assert daemon.hosted_sweep_interval_after(outcome, 10800.0) == 1200.0
    entry = daemon.hosted_sweep_clock_entry(
        entry, {"state": "ok", "duration_sec": 50.0},
        name="capture_deterministic_forward", now_utc="2026-10-20T08:20:00Z",
    )
    assert "network_retry_streak" not in entry
    assert entry["completed_at_utc"] == "2026-10-20T08:20:00Z"
