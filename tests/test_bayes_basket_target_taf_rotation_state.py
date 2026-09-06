from bayes_basket_target.taf_rotation_state import (
    build_catalog,
    build_rotation_state,
    load_rotation_state,
    persist_rotation_state,
    scope_key,
    select_scopes,
)


def _row(station_id: str, event_date: str) -> dict[str, str]:
    return {
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": "Asia/Seoul",
    }


def test_taf_catalog_deduplicates_buckets_and_excludes_closed_days() -> None:
    rows = [
        _row("RKPK", "2026-07-30"),
        _row("RKPK", "2026-07-31"),
        _row("RKPK", "2026-07-31"),
        _row("RKSI", "2026-08-01"),
        _row("UNLISTED", "2026-08-01"),
    ]

    catalog = build_catalog(
        rows,
        as_of_utc="2026-07-31T00:30:00Z",
        station_ids=("RKPK", "RKSI"),
    )

    assert [scope_key(scope) for scope in catalog] == [
        "RKSI|2026-08-01|Asia/Seoul",
        "RKPK|2026-07-31|Asia/Seoul",
    ]
    assert [scope["day_state"] for scope in catalog] == [
        "not_started",
        "open",
    ]


def test_taf_catalog_excludes_open_scope_after_its_physical_peak_window() -> None:
    catalog = build_catalog(
        [
            _row("RKPK", "2026-07-31"),
            _row("RKSI", "2026-08-01"),
        ],
        # 16:00 Seoul on July 31: RKPK's 06:00-15:00 forward physical
        # window has elapsed, while the next local date remains actionable.
        as_of_utc="2026-07-31T07:00:00Z",
        station_ids=("RKPK", "RKSI"),
    )

    assert [scope_key(scope) for scope in catalog] == [
        "RKSI|2026-08-01|Asia/Seoul",
    ]


def test_taf_rotation_crosses_end_and_wraps_repeatedly(tmp_path) -> None:
    catalog = [
        {
            **_row(f"RK0{index}", "2026-08-01"),
            "day_state": "not_started",
        }
        for index in range(6)
    ]
    path = tmp_path / "taf_rotation_state.json"
    previous = None
    selected_history: list[list[str]] = []

    for generation in range(3):
        # Pinned to four rather than the production batch: this asserts the
        # resume arithmetic, which must not change when the batch is retuned.
        selected, cursor, catalog_sha = select_scopes(
            catalog,
            previous=previous,
            limit=4,
        )
        outcomes = [
            {
                "scope_key": scope_key(scope),
                "state": "captured",
                "reason": "",
            }
            for scope in selected
        ]
        state = build_rotation_state(
            catalog=catalog,
            selected=selected,
            next_cursor=cursor,
            catalog_sha256=catalog_sha,
            outcomes=outcomes,
            previous=previous,
            updated_at_utc=f"2026-07-31T00:0{generation}:00Z",
        )
        assert persist_rotation_state(path, state) is True
        previous = load_rotation_state(path)
        selected_history.append(
            [scope["station_id"] for scope in selected]
        )

    assert selected_history == [
        ["RK00", "RK01", "RK02", "RK03"],
        ["RK04", "RK05", "RK00", "RK01"],
        ["RK02", "RK03", "RK04", "RK05"],
    ]
    assert previous is not None
    assert previous["generation"] == 3
    assert previous["cursor"] == 0


def test_taf_rotation_does_not_replay_head_when_catalog_changes() -> None:
    """A changed catalogue resumes the lap; it does not restart it.

    The rotation used to key its resume on the catalogue hash, so any change
    -- and a fleet spanning twenty timezones changes it about a hundred times
    a day -- sent the cursor back to index 0.  The scopes just polled were
    polled again while the unpolled tail stayed unpolled.
    """

    original = [
        {**_row(f"RK0{index}", "2026-08-01"), "day_state": "not_started"}
        for index in range(5)
    ]
    selected, cursor, catalog_sha = select_scopes(
        original,
        previous=None,
        limit=4,
    )
    state = build_rotation_state(
        catalog=original,
        selected=selected,
        next_cursor=cursor,
        catalog_sha256=catalog_sha,
        outcomes=[
            {
                "scope_key": scope_key(scope),
                "state": "captured",
                "reason": "",
            }
            for scope in selected
        ],
        previous=None,
        updated_at_utc="2026-07-31T00:00:00Z",
    )
    changed = [
        {**_row("NEW0", "2026-08-01"), "day_state": "not_started"},
        *original,
    ]

    next_selected, next_cursor, _ = select_scopes(
        changed,
        previous=state,
        limit=4,
    )

    # RK00..RK03 were served a moment ago; NEW0 and RK04 never were.  The two
    # unserved scopes go first, and only then does the lap resume at the
    # longest-waiting of the rest.
    assert [scope["station_id"] for scope in next_selected] == [
        "NEW0",
        "RK04",
        "RK00",
        "RK01",
    ]
    assert next_cursor == 3


def test_taf_rotation_reaches_catalog_tail_under_continuous_churn() -> None:
    """The last scope in the catalogue is served within one lap.

    This is the ZUUU case.  ``ZUUU|<today>`` is alphabetically last inside the
    lower-priority ``open`` block, so it sits at the very end of a ninety-scope
    catalogue every single day.  Under the hash-keyed cursor it went unserved
    from 2026-09-02 to 2026-09-06 while AWC was publishing a TX group for it
    the whole time, because something in the fleet reshuffled the catalogue
    before any lap could cross twenty-three rounds.
    """

    tail = {**_row("ZUUU", "2026-08-01"), "day_state": "open"}
    head = [
        {**_row(f"RK{index:02d}", "2026-08-02"), "day_state": "not_started"}
        for index in range(20)
    ]
    previous = None
    served_tail_at_round = None

    for generation in range(6):
        # A different scope leaves and rejoins every round, so the catalogue
        # hashes differently every round -- exactly what the old cursor reset
        # on, and what the fleet does in production.
        churned = [
            scope
            for index, scope in enumerate(head)
            if index != generation % len(head)
        ]
        catalog = [*churned, tail]
        selected, cursor, catalog_sha = select_scopes(
            catalog, previous=previous, limit=4
        )
        if any(scope["station_id"] == "ZUUU" for scope in selected):
            served_tail_at_round = generation
            break
        previous = build_rotation_state(
            catalog=catalog,
            selected=selected,
            next_cursor=cursor,
            catalog_sha256=catalog_sha,
            outcomes=[
                {"scope_key": scope_key(scope), "state": "captured", "reason": ""}
                for scope in selected
            ],
            previous=previous,
            updated_at_utc=f"2026-07-31T00:0{generation}:00Z",
        )

    # Twenty scopes at four a round is a five-round lap, and the tail is inside
    # it however much the head churns.
    assert served_tail_at_round is not None
    assert served_tail_at_round <= 5


def test_taf_rotation_stamps_failed_scopes_as_served() -> None:
    """A scope that failed its fetch does not get the next slot too.

    EGLC answers ``awc_taf_history_row_missing`` on every attempt.  If only
    captures advanced the rotation it would hold the front of the queue
    forever, which is the same starvation from the other end.
    """

    catalog = [
        {**_row(f"RK0{index}", "2026-08-01"), "day_state": "not_started"}
        for index in range(4)
    ]
    selected, cursor, catalog_sha = select_scopes(
        catalog, previous=None, limit=2
    )
    state = build_rotation_state(
        catalog=catalog,
        selected=selected,
        next_cursor=cursor,
        catalog_sha256=catalog_sha,
        outcomes=[
            {
                "scope_key": scope_key(scope),
                "state": "failed_isolated",
                "reason": "ContractError:awc_taf_history_row_missing",
            }
            for scope in selected
        ],
        previous=None,
        updated_at_utc="2026-07-31T00:00:00Z",
    )

    next_selected, _, _ = select_scopes(catalog, previous=state, limit=2)

    assert [scope["station_id"] for scope in selected] == ["RK00", "RK01"]
    assert [scope["station_id"] for scope in next_selected] == ["RK02", "RK03"]


def test_taf_rotation_defers_rather_than_skips_an_unattempted_scope() -> None:
    """A round cut short by its budget leaves its remainder at the front.

    The round is widened to twelve scopes but bounded by wall clock, so a slow
    round attempts fewer than it selected.  Only the attempted ones are handed
    to ``build_rotation_state``; stamping the whole selection would skip the
    unattempted tail every round, which is the starvation this lane was just
    dug out of, wearing a bigger batch size.
    """

    catalog = [
        {**_row(f"RK{index:02d}", "2026-08-01"), "day_state": "not_started"}
        for index in range(12)
    ]
    selected, cursor, catalog_sha = select_scopes(catalog, previous=None)
    assert len(selected) == 12

    # The round ran out of time after five.
    attempted = selected[:5]
    state = build_rotation_state(
        catalog=catalog,
        selected=attempted,
        next_cursor=cursor,
        catalog_sha256=catalog_sha,
        outcomes=[
            {"scope_key": scope_key(scope), "state": "captured", "reason": ""}
            for scope in attempted
        ],
        previous=None,
        updated_at_utc="2026-07-31T00:00:00Z",
    )

    assert len(state["served_sequence_by_scope_key"]) == 5
    next_selected, _, _ = select_scopes(catalog, previous=state)
    # The seven it never reached lead, and only then does it lap back.
    assert [scope["station_id"] for scope in next_selected][:7] == [
        f"RK{index:02d}" for index in range(5, 12)
    ]
