from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_exit_retry_watch_script_is_gone_not_merely_guarded():
    """The retirement went past the dry-run guard: the script itself is gone.

    This used to read ``scripts/exit_retry_watch.py`` and assert it defaulted
    to dry-run and demanded ``--apply`` with a target_realization exit reason
    code.  The script was removed with the pre-basket lane it drove, so there
    is nothing left to read and those assertions could only raise.  A script
    that does not exist cannot place a SELL, which is what the guards were
    protecting against.
    """

    assert not (ROOT / "scripts" / "exit_retry_watch.py").exists()


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

    # The service list this used to read lived in ui/server.py, which is gone
    # with the same lane; a label cannot sit in a list that does not exist.
    assert not (ROOT / "ui" / "server.py").exists()


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

    assert not (ROOT / "ui" / "server.py").exists()
