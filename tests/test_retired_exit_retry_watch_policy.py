from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_exit_retry_watch_defaults_to_dry_run_and_requires_bayes_apply():
    text = (ROOT / "scripts" / "exit_retry_watch.py").read_text()

    assert "Defaults to dry-run" in text
    assert "--apply" in text
    assert "--exit-reason-code" in text
    assert "s._exit_order_policy_block_reason(reason_codes)" in text
    assert "if args.dry_run or not args.apply:" in text
    assert "real SELL requires --apply and --exit-reason-code target_realization_*" in text


def test_exit_retry_watch_launchd_is_retired_and_not_in_service_lists():
    plist = (ROOT / "launchd" / "com.weather.exit_retry_watch.plist").read_text()
    weatherctl = (ROOT / "scripts" / "weatherctl.sh").read_text()
    ui_server = (ROOT / "ui" / "server.py").read_text()

    assert "<key>Disabled</key>" in plist
    assert "<true/>" in plist
    assert "--dry-run" in plist
    assert "<key>KeepAlive</key>" in plist
    assert "<false/>" in plist

    weatherctl_labels = weatherctl.split("labels=(", 1)[1].split(")", 1)[0]
    assert "com.weather.exit_retry_watch" not in weatherctl_labels

    service_block = ui_server.split("def _launchd_service_labels()", 1)[1].split("]", 1)[0]
    assert "com.weather.exit_retry_watch" not in service_block


def test_weatherctl_stop_start_covers_task_queue_worker():
    weatherctl = (ROOT / "scripts" / "weatherctl.sh").read_text()

    weatherctl_labels = weatherctl.split("labels=(", 1)[1].split(")", 1)[0]
    legacy_labels = weatherctl.split("legacy_trade_labels=(", 1)[1].split(")", 1)[0]
    stop_block = weatherctl.split("stop)", 1)[1].split(";;", 1)[0]

    assert "com.weather.task_queue_worker" in weatherctl_labels
    assert "com.weather.task_queue_worker" not in legacy_labels
    assert 'for label in "${labels[@]}"' in stop_block
    assert '_bootout_label "$label"' in stop_block
