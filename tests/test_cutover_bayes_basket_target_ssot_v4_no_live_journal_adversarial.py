from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/cutover_bayes_basket_target_ssot_v4_no_live.sh"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _function(text: str, name: str, next_name: str) -> str:
    start = text.index(f"{name}() {{")
    end = text.index(f"\n{next_name}() {{", start)
    return text[start:end]


def _cleanup(text: str) -> str:
    start = text.index("cleanup() {")
    end = text.index("\nrecover_stale_cutover_journal\n", start)
    return text[start:end]


def _success_tail(text: str) -> str:
    start = text.rindex(
        'apply_bounded_maintenance_retention "$RETENTION_RECEIPT"'
    )
    return text[start:]


def test_stale_journal_recovery_is_the_first_maintenance_gate() -> None:
    """A stale transaction must be resolved before any fresh preflight."""
    text = _text()
    recovery_call = text.index("\nrecover_stale_cutover_journal\n")
    trap = text.index("\ntrap cleanup EXIT", recovery_call)
    fresh_preflights = (
        text.index("\n/usr/bin/touch \"$LIVE_PAUSE\"", trap),
        text.index("\nassert_target_runtime_wiring\n", trap),
        text.index('probe_forward_store 0 0 "$FORWARD_PRECHECK"', trap),
        text.index('load_target_activation(Path(sys.argv[2]))', trap),
    )

    assert recovery_call < trap
    assert all(recovery_call < preflight for preflight in fresh_preflights)
    assert "return 1" in _function(
        text,
        "recover_stale_cutover_journal",
        "restart_target_writer",
    )


def test_stale_partial_recovery_retains_journal_and_both_pauses() -> None:
    recovery = _function(
        _text(),
        "recover_stale_cutover_journal",
        "restart_target_writer",
    )
    failure = recovery.index('if [ "$recover_failed" -ne 0 ]; then')
    before_failure = recovery[:failure]
    failure_tail = recovery[failure:]

    assert '/usr/bin/touch "$LIVE_PAUSE"' in recovery
    assert (
        '"bayes-basket-target-m5-migration-v1" > "$TARGET_PAUSE"'
        in before_failure
    )
    assert '"$CUTOVER_JOURNAL"' in recovery
    journal_delete = recovery.index(
        '"$stale_forward_backup" "$stale_pause_backup" "$CUTOVER_JOURNAL"'
    )
    success_guard = recovery.rfind(
        'if [ "$recover_failed" -eq 0 ]; then',
        0,
        journal_delete,
    )
    assert success_guard >= 0
    assert journal_delete < failure
    assert "stale_cutover_journal_recovery_incomplete" in failure_tail
    assert '/bin/rm -f "$CUTOVER_JOURNAL"' not in failure_tail


def test_incomplete_recovery_cannot_restart_writer() -> None:
    recovery = _function(
        _text(),
        "recover_stale_cutover_journal",
        "restart_target_writer",
    )
    restart = recovery.index("restart_target_writer")
    restart_guard = recovery.rfind(
        'if [ "$recover_failed" -eq 0 ]; then',
        0,
        restart,
    )
    failure_branch = recovery.index(
        'if [ "$recover_failed" -ne 0 ]; then',
        restart,
    )

    assert restart_guard >= 0
    assert restart_guard < restart < failure_branch
    assert recovery.count("restart_target_writer") == 1


def test_journal_deletion_has_only_three_verified_terminal_paths() -> None:
    text = _text()
    occurrences = [
        match.start()
        for match in re.finditer(r'"\$CUTOVER_JOURNAL"', text)
        if "/bin/rm -f" in text[max(0, match.start() - 120) : match.start()]
    ]

    # Stale-journal full recovery, EXIT-trap full rollback, and successful
    # cutover are the only legal deletion sites.
    assert len(occurrences) == 3

    recovery = _function(
        text,
        "recover_stale_cutover_journal",
        "restart_target_writer",
    )
    cleanup = _cleanup(text)
    success_tail = _success_tail(text)

    recovery_delete = recovery.rindex('"$CUTOVER_JOURNAL"')
    assert (
        recovery.rfind(
            'if [ "$recover_failed" -eq 0 ]; then',
            0,
            recovery_delete,
        )
        >= 0
    )

    cleanup_delete = cleanup.rindex('"$CUTOVER_JOURNAL"')
    assert (
        cleanup.rfind(
            'if [ "$rollback_failed" -eq 0 ]; then',
            0,
            cleanup_delete,
        )
        >= 0
    )
    assert success_tail.index('apply_bounded_maintenance_retention') < (
        success_tail.index('"$CUTOVER_JOURNAL"')
    )
    assert success_tail.index('"$CUTOVER_JOURNAL"') < success_tail.index(
        "success=1"
    )


def test_retention_never_runs_on_abort_before_writer_stop() -> None:
    """No mutation means neither success nor a completed rollback."""
    cleanup = _cleanup(_text())
    early_abort_start = cleanup.index('if [ "$target_stopped" -eq 0 ]; then')
    early_abort_end = cleanup.index("\n  fi", early_abort_start)
    early_abort = cleanup[early_abort_start:early_abort_end]

    assert "apply_bounded_maintenance_retention" not in early_abort


def test_rollback_retention_runs_only_after_writer_restart_succeeds() -> None:
    """Retention must not run if restoring the service later fails."""
    cleanup = _cleanup(_text())
    retention = cleanup.index(
        'apply_bounded_maintenance_retention '
        '\\\n      "$FAILURE_RETENTION_RECEIPT"',
        cleanup.index("rollback_verified=1"),
    )
    restart = cleanup.index(
        "restart_target_writer",
        cleanup.index("rollback_verified=1"),
    )
    terminal_success = cleanup.index(
        "target_ssot_v4_cutover_rolled_back_verified"
    )

    assert restart < retention < terminal_success
    restart_guard = cleanup.rfind(
        'if [ "$rollback_verified" -eq 1 ]',
        0,
        restart,
    )
    assert restart_guard >= 0


def test_success_retention_is_after_writer_and_database_verification() -> None:
    text = _text()
    retention = text.rindex(
        'apply_bounded_maintenance_retention "$RETENTION_RECEIPT"'
    )
    writer_restart = text.rindex("\nrestart_target_writer\n", 0, retention)
    receipt_verification = text.rindex(
        '"state": "database_verified_before_writer_restart"',
        0,
        retention,
    )
    final_receipt_write = text.rindex(
        "path.write_text(",
        receipt_verification,
        retention,
    )

    assert receipt_verification < writer_restart < final_receipt_write < retention
