from __future__ import annotations

import importlib.util
from pathlib import Path
import plistlib
import stat
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "production_reported_x_runtime_readiness.py"
SPEC = importlib.util.spec_from_file_location("production_reported_x_runtime_readiness", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _write_plist(path: Path, environment: dict[str, str]) -> None:
    with path.open("wb") as handle:
        plistlib.dump(
            {
                "Label": "com.weather.monitor.daemon",
                "EnvironmentVariables": environment,
            },
            handle,
        )


def test_installed_monitor_environment_requires_all_reported_x_ingress_lanes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "monitor.plist"
    _write_plist(path, dict(MODULE.REQUIRED_MONITOR_ENVIRONMENT))

    result = MODULE.audit_installed_monitor_environment(path)

    assert result["ok"] is True
    assert all(result["checks"].values())
    assert result["read_only"] is True


def test_installed_monitor_environment_rejects_disabled_speci_probability_ingress(
    tmp_path: Path,
) -> None:
    path = tmp_path / "monitor.plist"
    environment = dict(MODULE.REQUIRED_MONITOR_ENVIRONMENT)
    environment["PM_AMSC_ZUUU_SPECI_PRODUCTION_PROBABILITY_INGRESS_ENABLED"] = "0"
    _write_plist(path, environment)

    result = MODULE.audit_installed_monitor_environment(path)

    assert result["ok"] is False
    assert result["checks"]["PM_AMSC_ZUUU_SPECI_PRODUCTION_PROBABILITY_INGRESS_ENABLED"] is False


def test_installed_monitor_environment_rejects_missing_plist(tmp_path: Path) -> None:
    result = MODULE.audit_installed_monitor_environment(tmp_path / "missing.plist")

    assert result["ok"] is False
    assert result["load_error"] == "FileNotFoundError"
    assert not any(result["checks"].values())


def test_monitor_runtime_entrypoints_require_immutable_wrapper_and_installed_plist(
    tmp_path: Path,
) -> None:
    (tmp_path / "launchd").mkdir()
    (tmp_path / "scripts").mkdir()
    repository_plist = tmp_path / "launchd/com.weather.monitor.daemon.plist"
    installed_plist = tmp_path / "installed.plist"
    repository_plist.write_text("same-plist", encoding="utf-8")
    # The audit grew two requirements the fixture never learned: the installed
    # plist has to name the LaunchAgent entrypoint and its launch domain, and
    # the entrypoint itself has to exist.
    installed_plist.write_text(
        "com.weather.monitor.daemon\n"
        "scripts/weather_monitor_launchagent_entrypoint.sh\n"
        "PM_MONITOR_LAUNCH_DOMAIN\n",
        encoding="utf-8",
    )
    (tmp_path / "scripts/weather_monitor_launchagent_entrypoint.sh").write_text(
        "#!/bin/bash\n", encoding="utf-8"
    )
    (tmp_path / "scripts/polymarket_monitor_daemon.sh").write_text(
        '"$ROOT/scripts/run_amsc_zuuu_metar_taf_poll_immutable.sh" \\\n',
        encoding="utf-8",
    )
    wrapper = tmp_path / "scripts/run_amsc_zuuu_metar_taf_poll_immutable.sh"
    wrapper.write_text(
        'source_sha_before="$(shasum -a 256 "$SOURCE" | awk x)"\n'
        '[ "$source_sha_before" = "$snapshot_sha256" ]\n'
        '[ "$snapshot_sha256" = "$source_sha_after" ]\n'
        '/bin/bash -n "$SNAPSHOT"\n/bin/bash "$SNAPSHOT" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "scripts/amsc_zuuu_metar_taf_poll_once.sh").write_text(
        'ROOT="${AMSC_POLL_PROJECT_ROOT:-fallback}"\n', encoding="utf-8"
    )

    result = MODULE.audit_monitor_runtime_entrypoints(
        root=tmp_path, installed_monitor_plist=installed_plist
    )

    assert result["ok"] is True
    assert all(result["checks"].values())


def test_monitor_runtime_entrypoints_reject_direct_mutable_poller(tmp_path: Path) -> None:
    (tmp_path / "launchd").mkdir()
    (tmp_path / "scripts").mkdir()
    repository_plist = tmp_path / "launchd/com.weather.monitor.daemon.plist"
    installed_plist = tmp_path / "installed.plist"
    repository_plist.write_text("repo", encoding="utf-8")
    installed_plist.write_text("stale", encoding="utf-8")
    (tmp_path / "scripts/polymarket_monitor_daemon.sh").write_text(
        '"$ROOT/scripts/amsc_zuuu_metar_taf_poll_once.sh" \\\n', encoding="utf-8"
    )
    (tmp_path / "scripts/run_amsc_zuuu_metar_taf_poll_immutable.sh").write_text(
        "", encoding="utf-8"
    )
    (tmp_path / "scripts/amsc_zuuu_metar_taf_poll_once.sh").write_text(
        "", encoding="utf-8"
    )

    result = MODULE.audit_monitor_runtime_entrypoints(
        root=tmp_path, installed_monitor_plist=installed_plist
    )

    assert result["ok"] is False
    assert result["checks"]["installed_plist_matches_repository"] is False
    assert result["checks"]["monitor_calls_immutable_wrapper"] is False
    assert result["checks"]["monitor_does_not_execute_mutable_poller_directly"] is False


def test_running_monitor_runtime_forces_locale_independent_ps_output(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime.sh"
    runtime.write_text("#!/bin/bash\n", encoding="utf-8")
    runtime.touch()
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs.get("env")
        return SimpleNamespace(
            returncode=0,
            stdout="Wed Jul 15 17:18:33 2026\n",
            stderr="",
        )

    monkeypatch.setattr(MODULE.subprocess, "run", fake_run)

    result = MODULE.audit_running_monitor_runtime(
        launchctl_stdout="state = running\npid = 67031\n",
        launchctl_returncode=0,
        runtime_paths=[runtime],
    )

    assert captured["command"] == ["ps", "-p", "67031", "-o", "lstart="]
    assert captured["env"]["LC_ALL"] == "C"
    assert captured["env"]["LANG"] == "C"
    assert result["started_at"] == "Wed Jul 15 17:18:33 2026"
    assert result["started_epoch"] is not None
    assert result["error"] == ""
