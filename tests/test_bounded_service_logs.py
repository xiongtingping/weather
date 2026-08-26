import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/lib/bounded_service_logs.sh"


def _run(tmp_root: Path, *log_paths: Path) -> subprocess.CompletedProcess:
    """Invoke the helper for real, with ROOT pointed at a scratch tree."""

    quoted = " ".join(f'"{p}"' for p in log_paths)
    return subprocess.run(
        [
            "/bin/bash",
            "-c",
            f'ROOT="{tmp_root}"; . "{HELPER}"; '
            f"weather_prepare_bounded_service_logs {quoted}",
        ],
        capture_output=True,
        text=True,
    )


def test_live_service_launchers_share_bounded_log_policy() -> None:
    helper = (ROOT / "scripts/lib/bounded_service_logs.sh").read_text(encoding="utf-8")
    assert "WEATHER_SERVICE_LOG_MAX_BYTES=33554432" in helper
    assert "ulimit -f" in helper
    assert 'case "$log_path" in' in helper
    assert '"$ROOT/data/"*.log' in helper

    for launcher in (
        "scripts/market_ws_listener.sh",
        "scripts/clob_order_service.sh",
        "scripts/weather_basket_yes_daemon.sh",
    ):
        text = (ROOT / launcher).read_text(encoding="utf-8")
        assert 'bounded_service_logs.sh' in text
        assert 'weather_prepare_bounded_service_logs' in text


def _logdir(tmp_path: Path) -> Path:
    logs = tmp_path / "data" / "logs"
    logs.mkdir(parents=True)
    return logs


def test_previous_run_is_kept_and_the_live_log_keeps_its_inode(
    tmp_path: Path,
) -> None:
    """The crash that killed the last run has to survive the restart.

    And the live log must be truncated rather than renamed: launchd opens
    StandardOutPath before the launcher runs, so a rename would leave its
    descriptor pointing at .prev and the live log empty for the whole run.
    """

    logs = _logdir(tmp_path)
    live = logs / "service.log"
    live.write_text("Traceback (most recent call last):\n", encoding="utf-8")
    inode_before = live.stat().st_ino

    result = _run(tmp_path, live)
    assert result.returncode == 0, result.stderr

    assert live.read_text(encoding="utf-8") == ""
    assert live.stat().st_ino == inode_before
    assert (logs / "service.log.prev").read_text(encoding="utf-8") == (
        "Traceback (most recent call last):\n"
    )


def test_an_empty_run_does_not_evict_the_kept_traceback(tmp_path: Path) -> None:
    """A service that dies before writing must not erase the real evidence."""

    logs = _logdir(tmp_path)
    live = logs / "service.log"
    live.write_text("the crash that matters\n", encoding="utf-8")
    assert _run(tmp_path, live).returncode == 0
    assert (logs / "service.log.prev").read_text(encoding="utf-8") == (
        "the crash that matters\n"
    )

    # Restart number two: the run in between wrote nothing at all.
    assert _run(tmp_path, live).returncode == 0
    assert (logs / "service.log.prev").read_text(encoding="utf-8") == (
        "the crash that matters\n"
    )


def test_a_path_outside_the_repository_log_directory_fails_closed(
    tmp_path: Path,
) -> None:
    """The helper stays a service-log helper, not a general truncation tool."""

    _logdir(tmp_path)
    outsider = tmp_path / "elsewhere.log"
    outsider.write_text("keep me\n", encoding="utf-8")

    assert _run(tmp_path, outsider).returncode == 1
    assert outsider.read_text(encoding="utf-8") == "keep me\n"
    assert not (tmp_path / "elsewhere.log.prev").exists()


def test_the_two_generations_together_cost_what_one_used_to(tmp_path: Path) -> None:
    """Keeping the previous run must not double the disk the logs take."""

    result = subprocess.run(
        ["/bin/bash", "-c", f'. "{HELPER}"; echo "$WEATHER_SERVICE_LOG_MAX_BYTES"'],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    per_generation = int(result.stdout.strip())
    assert per_generation * 2 == 67108864
