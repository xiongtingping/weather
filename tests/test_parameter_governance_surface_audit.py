import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "parameter_governance_surface_audit.py"
spec = importlib.util.spec_from_file_location("parameter_governance_surface_audit", SCRIPT_PATH)
audit_mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(audit_mod)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _plist(label: str, *, env: dict[str, str] | None = None) -> str:
    env = env or {}
    env_xml = ""
    if env:
        env_xml = "<key>EnvironmentVariables</key><dict>"
        for key, value in env.items():
            env_xml += f"<key>{key}</key><string>{value}</string>"
        env_xml += "</dict>"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array><string>/bin/zsh</string><string>-lc</string><string>echo ok</string></array>
  <key>RunAtLoad</key><true/>
  {env_xml}
</dict>
</plist>
"""


def test_parameter_governance_surface_audit_blocks_legacy_optimizer_apply(tmp_path: Path) -> None:
    _write(
        tmp_path / "launchd" / "com.weather.feedback_loop_hourly.plist",
        _plist("com.weather.feedback_loop_hourly", env={"PM_OPTIMIZER_AUTO_APPLY": "1"}),
    )
    _write(
        tmp_path / "scripts" / "feedback_loop.py",
        """
auto_apply = os.environ.get("PM_OPTIMIZER_AUTO_APPLY") == "1"
if auto_apply:
    payload["auto_apply"] = apply_optimizer_suggestions()
        """,
    )
    _write(
        tmp_path / "ui" / "server.py",
        """
if path == "/api/optimizer-apply":
    self._send_json(apply_edge_bias_gate_suggestion(days=30, restart=True))
def load_auto_trade_config():
    save_auto_trade_config(data)
        """,
    )

    report = audit_mod.build_audit(tmp_path)

    assert report["read_only"] is True
    assert report["guards"]["writes_archive_db"] is False
    assert report["ok"] is False
    assert "legacy_optimizer_auto_apply_enabled_in_launchd" in report["blockers"]
    assert "legacy_optimizer_apply_surface_not_bound_to_bayes_parameter_governance" in report["blockers"]
    assert "unconfirmed_ui_optimizer_apply_endpoint_present" in report["blockers"]
    assert "auto_trade_config_load_path_can_write_defaults" in report["warnings"]
    assert any(surface["id"] == "legacy_feedback_optimizer_auto_apply" for surface in report["parameter_write_surfaces"])
    assert report["config_surface"]["high_risk_keys"][0]["key"] == "PM_OPTIMIZER_AUTO_APPLY"


def test_parameter_governance_surface_audit_allows_no_apply_governance(tmp_path: Path) -> None:
    _write(
        tmp_path / "launchd" / "com.weather.monitor.daemon.plist",
        _plist("com.weather.monitor.daemon", env={"PM_HARD_SOURCE_AWC_RELEASE_WATCH_ENABLED": "1"}),
    )
    _write(
        tmp_path / "scripts" / "install_monitor_launchdaemon.sh",
        'MONITOR_LABEL="com.weather.monitor.daemon"\nlaunchctl kickstart -k "system/${MONITOR_LABEL}"\n',
    )
    _write(
        tmp_path / "scripts" / "weatherctl.sh",
        "restart)\n  launchctl kickstart -k gui/${UID}/com.xiong.weather.ui\n  ;;\n",
    )
    _write(
        tmp_path / "daily_review_persistence_writer.py",
        "PARAMETER_CHANGE_EVENT_INSERT_SQL = 'INSERT INTO parameter_change_events'\nallow_parameter_event_apply = False\n",
    )
    _write(
        tmp_path / "scripts" / "daily_review_persistence_apply.py",
        'CONFIRMATION_PHRASE = "APPLY_DAILY_REVIEW_PERSISTENCE"\n'
        'PARAMETER_EVENT_CONFIRMATION_PHRASE = "APPLY_DAILY_REVIEW_PARAMETER_EVENTS"\n',
    )
    _write(
        tmp_path / "scripts" / "parameter_change_event_transition_apply.py",
        'CONFIRMATION_PHRASE = "APPLY_PARAMETER_CHANGE_EVENT_TRANSITIONS"\n'
        "STATE_TRANSITIONS = {}\n"
        '"changes_parameters": False\n',
    )

    report = audit_mod.build_audit(tmp_path)

    assert report["ok"] is True
    assert report["blockers"] == []
    assert report["parameter_change_governance_contract"]["state_machine"]["apply_allowed_in_state"] == "approved"
    assert any(surface["id"] == "daily_review_parameter_event_ledger" for surface in report["parameter_write_surfaces"])
    transition_surface = [
        surface
        for surface in report["parameter_write_surfaces"]
        if surface["id"] == "parameter_change_event_transition_apply_cli"
    ][0]
    assert transition_surface["status"] == "controlled_transition_contract"
    assert transition_surface["changes_parameters"] is False
    assert any(surface["id"] == "monitor_launchdaemon_install_and_kickstart" for surface in report["launchd_restart_surfaces"])
    assert report["boundary"]["daily_review_candidates"] == "proposed only until reviewed/approved"


def test_parameter_governance_surface_audit_accepts_retired_optimizer_endpoint(tmp_path: Path) -> None:
    _write(
        tmp_path / "launchd" / "com.weather.feedback_loop_hourly.plist",
        _plist("com.weather.feedback_loop_hourly", env={"PM_OPTIMIZER_AUTO_APPLY": "0"}),
    )
    _write(
        tmp_path / "ui" / "server.py",
        """
def apply_edge_bias_gate_suggestion():
    return _legacy_optimizer_apply_retired_payload(source="direct")
if path == "/api/optimizer-apply":
    self._send_json(_legacy_optimizer_apply_retired_payload(source="api_optimizer_apply"))
        """,
    )

    report = audit_mod.build_audit(tmp_path)

    assert report["ok"] is True
    assert report["blockers"] == []
    endpoint = [surface for surface in report["parameter_write_surfaces"] if surface["id"] == "ui_optimizer_apply_endpoint"][0]
    assert endpoint["status"] == "retired_no_write"
    assert endpoint["writes_config_if_called"] is False
    assert endpoint["restarts_launchd_default"] is False


def test_parameter_governance_surface_audit_summary_cli(tmp_path: Path, capsys) -> None:
    _write(tmp_path / "launchd" / "com.weather.monitor.daemon.plist", _plist("com.weather.monitor.daemon"))

    code = audit_mod.main(["--root", str(tmp_path), "--summary"])
    out = capsys.readouterr().out

    assert code == 0
    assert "parameter_governance_surface_audit_v1" in out
    assert '"read_only": true' in out
    assert "parameter_surface_count" in out
