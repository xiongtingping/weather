from scripts.audit_resident_p1_replay_binding import _replay_state


AS_OF = "2026-08-01T17:27:46.876Z"


def _row(
    *,
    stage: str,
    checkpoint_id: str,
    decision_at_utc: str,
    fresh_until_utc: str,
    p0_root_checkpoint_id: str | None = None,
) -> dict[str, object]:
    lineage: dict[str, object] = {}
    if p0_root_checkpoint_id is not None:
        lineage["p0_root_checkpoint_id"] = p0_root_checkpoint_id
    return {
        "stage": stage,
        "checkpoint_id": checkpoint_id,
        "decision_at_utc": decision_at_utc,
        "fresh_until_utc": fresh_until_utc,
        "lineage": lineage,
    }


def test_stale_p1_on_an_old_p0_root_requires_replay_not_p0_fallback() -> None:
    latest_p0 = _row(
        stage="P0",
        checkpoint_id="p0-new",
        decision_at_utc="2026-08-01T16:44:40.423Z",
        fresh_until_utc="2026-08-01T22:44:40.423Z",
    )
    latest_p1 = _row(
        stage="P1",
        checkpoint_id="p1-old-root",
        decision_at_utc="2026-08-01T12:07:47.643Z",
        fresh_until_utc="2026-08-01T13:07:47.643Z",
        p0_root_checkpoint_id="p0-old",
    )

    result = _replay_state(
        latest_p0=latest_p0,
        p0_ambiguous=False,
        latest_p1=latest_p1,
        p1_ambiguous=False,
        as_of_utc=AS_OF,
    )

    assert result["state"] == "RESIDENT_P1_REPLAY_REQUIRED"
    assert result["replay_required"] is True
    assert result["latest_p1_fresh_at_as_of"] is False
    assert result["latest_p1_p0_root_checkpoint_id"] == "p0-old"


def test_current_p1_root_does_not_require_replay_even_when_stale() -> None:
    latest_p0 = _row(
        stage="P0",
        checkpoint_id="p0-current",
        decision_at_utc="2026-08-01T16:44:40.423Z",
        fresh_until_utc="2026-08-01T22:44:40.423Z",
    )
    latest_p1 = _row(
        stage="P1",
        checkpoint_id="p1-current-root",
        decision_at_utc="2026-08-01T12:07:47.643Z",
        fresh_until_utc="2026-08-01T13:07:47.643Z",
        p0_root_checkpoint_id="p0-current",
    )

    result = _replay_state(
        latest_p0=latest_p0,
        p0_ambiguous=False,
        latest_p1=latest_p1,
        p1_ambiguous=False,
        as_of_utc=AS_OF,
    )

    assert result == {
        "latest_p0_checkpoint_id": "p0-current",
        "latest_p1_checkpoint_id": "p1-current-root",
        "latest_p1_p0_root_checkpoint_id": "p0-current",
        "latest_p1_fresh_at_as_of": False,
        "state": "P1_ROOT_CURRENT",
        "replay_required": False,
    }


def test_no_visible_p1_is_not_misreported_as_a_replay_mismatch() -> None:
    result = _replay_state(
        latest_p0=_row(
            stage="P0",
            checkpoint_id="p0-current",
            decision_at_utc="2026-08-01T16:44:40.423Z",
            fresh_until_utc="2026-08-01T22:44:40.423Z",
        ),
        p0_ambiguous=False,
        latest_p1=None,
        p1_ambiguous=False,
        as_of_utc=AS_OF,
    )

    assert result == {"state": "NO_VISIBLE_P1", "replay_required": False}
