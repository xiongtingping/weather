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


def test_exit_retry_watch_launchd_job_is_gone_not_merely_disabled():
    """The retirement went past Disabled=true: the plist itself was deleted.

    This used to read ``launchd/com.weather.exit_retry_watch.plist`` and
    assert it carried Disabled/true and KeepAlive/false.  The job was removed
    outright in the 2026-07-30 refactor, so there is no plist to read and the
    assertion could only raise.  A job that does not exist is retired more
    thoroughly than one that is disabled, and that is what is checked here --
    together with the two lists a retired label must stay out of.
    """

    assert not (ROOT / "launchd" / "com.weather.exit_retry_watch.plist").exists()
    plists = sorted((ROOT / "launchd").glob("*.plist"))
    assert plists, "launchd/ should still hold the jobs that are not retired"
    for path in plists:
        assert "com.weather.exit_retry_watch" not in path.read_text(encoding="utf-8")

    ui_server = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
    service_block = ui_server.split("def _launchd_service_labels()", 1)[1].split("]", 1)[0]
    assert "com.weather.exit_retry_watch" not in service_block


def test_task_queue_worker_is_a_retired_label_with_no_launchd_job():
    """``scripts/weatherctl.sh`` is gone, and so is the label it started.

    This used to assert weatherctl's ``labels=(...)`` array contained
    com.weather.task_queue_worker and its stop branch booted every label out.
    That script was deleted on 2026-08-04 and nothing in the tree carries a
    ``labels=(`` array any more.  The label itself is now classified as a
    legacy probability-writer token, so the thing worth holding is that it
    stays unwired: no plist, and no service list.
    """

    import production_probability_runtime_manifest as manifest

    assert "com.weather.task_queue_worker" in manifest.LEGACY_PROBABILITY_WRITER_TOKENS
    assert not (ROOT / "scripts" / "weatherctl.sh").exists()
    for path in sorted((ROOT / "launchd").glob("*.plist")):
        assert "com.weather.task_queue_worker" not in path.read_text(encoding="utf-8")

    ui_server = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
    service_block = ui_server.split("def _launchd_service_labels()", 1)[1].split("]", 1)[0]
    assert "com.weather.task_queue_worker" not in service_block
