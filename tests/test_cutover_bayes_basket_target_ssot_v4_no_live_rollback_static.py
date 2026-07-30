from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/cutover_bayes_basket_target_ssot_v4_no_live.sh"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _rollback_region(text: str) -> str:
    """Return the function region from which EXIT cleanup can restore state."""
    start = text.index("restart_target_writer() {")
    end = text.index("\ntrap cleanup EXIT", start)
    return text[start:end]


def _assert_stage_flag(
    text: str,
    *,
    initial: str,
    committed: str,
    rollback_region: str,
) -> None:
    assert initial in text, f"missing pre-mutation stage state: {initial}"
    assert committed in text, f"missing post-commit stage state: {committed}"
    assert (
        initial.split("=", 1)[0] in rollback_region
    ), f"EXIT rollback does not inspect stage state: {initial}"


def test_exit_trap_tracks_every_independently_committed_mutation_stage() -> None:
    """A single mutation_started bit cannot identify which images to restore."""
    text = _text()
    rollback = _rollback_region(text)

    for initial, committed in (
        ("forward_store_initialized=0", "forward_store_initialized=1"),
        ("hard_no_timezone_migrated=0", "hard_no_timezone_migrated=1"),
        ("ssot_schema_migrated=0", "ssot_schema_migrated=1"),
        ("no_live_activation_applied=0", "no_live_activation_applied=1"),
    ):
        _assert_stage_flag(
            text,
            initial=initial,
            committed=committed,
            rollback_region=rollback,
        )

    # A stage is committed only after its mutator returns successfully.
    assert text.index("hard_no_timezone_migrated=1") > text.index(
        '"$ROOT/scripts/migrate_reported_trajectory_hard_no_timezone.py"'
    )
    assert text.index("ssot_schema_migrated=1") > text.index(
        '"$ROOT/scripts/bayes_basket_target_production_ssot_apply.py"'
    )
    assert text.index("no_live_activation_applied=1") > text.index(
        '"$ROOT/scripts/bayes_basket_target_no_live_activation_apply.py"'
    )


def test_exit_trap_restores_all_database_mutations_in_reverse_order() -> None:
    """Later-stage failure must restore the original pre-cutover state."""
    text = _text()
    rollback = _rollback_region(text)

    # The activation backup contains the already-migrated schema, whereas
    # SCHEMA_BACKUP is the original target DB.  A direct restore from the
    # original image is sufficient and avoids an unnecessary transient restore
    # of the activation image.
    for recovery_image in (
        "$SCHEMA_BACKUP",
        "$HARD_NO_BACKUP",
    ):
        assert (
            recovery_image in rollback
        ), f"EXIT rollback never consumes recovery image {recovery_image}"

    schema_restore = rollback.index("$SCHEMA_BACKUP")
    hard_no_restore = rollback.index("$HARD_NO_BACKUP")
    assert schema_restore < hard_no_restore

    # Restored SQLite objects must be checked before the writer is eligible
    # for restart.  Merely copying a backup is not recovery proof.
    assert rollback.count("quick_check") >= 2
    assert rollback.index("quick_check") < rollback.rindex(
        "restart_target_writer"
    )


def test_forward_store_and_pause_file_restore_their_exact_pre_cutover_state() -> None:
    """The initializer and pause write are mutations even before DB migration."""
    text = _text()
    rollback = _rollback_region(text)

    # The cutover must distinguish an absent forward store from a pre-existing
    # one and retain a bounded recovery image before initialize_forward_store.
    assert "FORWARD_STORE_PREEXISTED=" in text
    assert "FORWARD_STORE_BACKUP=" in text
    assert text.index("FORWARD_STORE_PREEXISTED=") < text.index(
        "\ninitialize_forward_store\n"
    )
    assert text.index("FORWARD_STORE_BACKUP=") < text.index(
        "\ninitialize_forward_store\n"
    )
    assert "$FORWARD_STORE_BACKUP" in rollback
    assert "FORWARD_STORE_PREEXISTED" in rollback

    # TARGET_PAUSE may already exist.  Success and failure paths may not
    # unconditionally overwrite/remove another maintenance authority.
    assert "TARGET_PAUSE_PREEXISTED=" in text
    assert "TARGET_PAUSE_BACKUP=" in text
    assert text.index("TARGET_PAUSE_PREEXISTED=") < text.index(
        '> "$TARGET_PAUSE"'
    )
    assert "$TARGET_PAUSE_BACKUP" in rollback
    assert "TARGET_PAUSE_PREEXISTED" in rollback


def test_rollback_attempts_every_restore_and_fails_closed_if_recovery_is_partial() -> None:
    """set -e inside EXIT must not skip later restores after one restore error."""
    text = _text()
    rollback = _rollback_region(text)

    assert "rollback_failed=0" in rollback
    assert (
        "set +e" in rollback or rollback.count("rollback_failed=1") >= 3
    ), "first restore failure can abort cleanup before remaining restores"
    assert "rollback_verified=1" in rollback

    restart = rollback.rindex("restart_target_writer")
    verified = rollback.index("rollback_verified=1")
    assert verified < restart
    assert re.search(
        r'rollback_verified[^;\n]*-eq 1|'
        r'\[\s*"\$rollback_verified"\s*-eq\s*1\s*\]',
        rollback,
    ), "target restart is not gated on verified full recovery"
    assert "/usr/bin/touch \"$LIVE_PAUSE\"" in rollback


def test_cutover_never_mutates_market_ws_clob_or_invokes_order_submission() -> None:
    """All launchd mutations must remain scoped to the no-live target writer."""
    text = _text()
    launchd_mutations = [
        line.strip()
        for line in text.splitlines()
        if "/bin/launchctl" in line
        and any(
            verb in line
            for verb in (" bootout ", " bootstrap ", " kickstart ", " enable ")
        )
    ]
    assert launchd_mutations
    assert all(
        "$TARGET_LABEL" in line or "$TARGET_PLIST" in line
        for line in launchd_mutations
    )
    assert all("$MARKET_WS_LABEL" not in line for line in launchd_mutations)
    assert all("$CLOB_LABEL" not in line for line in launchd_mutations)

    for forbidden in (
        "curl -X POST",
        "curl --request POST",
        "curl --data",
        "live_submit_authorized=true",
        "authorizes_order=true",
        "confirm-live",
        "clob_order_gateway.py",
        "submit_order",
        "place_order",
    ):
        assert forbidden not in text

    # The only curl is the read-only health probe.
    curl_lines = [line.strip() for line in text.splitlines() if "/usr/bin/curl" in line]
    assert curl_lines == [
        'health_payload="$(/usr/bin/curl -fsS --max-time 3 http://127.0.0.1:8765/health)"'
    ]


def test_power_loss_journal_precedes_mutation_and_gates_new_cutover() -> None:
    text = _text()
    assert (
        'CUTOVER_JOURNAL="$ROOT/data/'
        'bayes_basket_target_ssot_v4_cutover_journal.json"'
    ) in text
    recovery = text.index("\nrecover_stale_cutover_journal\n")
    trap = text.index("\ntrap cleanup EXIT", recovery)
    preflight = text.index(
        'probe_forward_store 0 0 "$FORWARD_PRECHECK"',
        trap,
    )
    assert recovery < trap < preflight

    prepared = text.index(
        'write_cutover_journal '
        '"prepared_before_forward_store_initializer"'
    )
    initializer = text.index("\ninitialize_forward_store\n")
    assert prepared < initializer
    for state, mutator in (
        (
            "hard_no_timezone_stage_complete",
            "migrate_reported_trajectory_hard_no_timezone.py",
        ),
        (
            "ssot_schema_stage_complete",
            "bayes_basket_target_production_ssot_apply.py",
        ),
        (
            "no_live_activation_stage_complete",
            "bayes_basket_target_no_live_activation_apply.py",
        ),
    ):
        assert text.index(f'write_cutover_journal "{state}"') > text.index(
            mutator
        )


def test_incomplete_rollback_retains_journal_and_recovery_images() -> None:
    text = _text()
    rollback = _rollback_region(text)
    conditional_cleanup = re.search(
        r'if \[ "\$rollback_failed" -eq 0 \]; then\s+'
        r'/bin/rm -f\s+\\?\s*'
        r'"\$FORWARD_STORE_BACKUP" "\$TARGET_PAUSE_BACKUP" '
        r'"\$CUTOVER_JOURNAL"',
        rollback,
    )
    assert conditional_cleanup is not None
    incomplete = rollback.index(
        "target_ssot_v4_cutover_rollback_incomplete"
    )
    assert rollback.index("$CUTOVER_JOURNAL") < incomplete
