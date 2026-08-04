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
        selected, cursor, catalog_sha = select_scopes(
            catalog,
            previous=previous,
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


def test_taf_rotation_resets_cursor_when_registry_catalog_changes() -> None:
    original = [
        {**_row(f"RK0{index}", "2026-08-01"), "day_state": "not_started"}
        for index in range(5)
    ]
    selected, cursor, catalog_sha = select_scopes(
        original,
        previous=None,
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
    )

    assert [scope["station_id"] for scope in next_selected] == [
        "NEW0",
        "RK00",
        "RK01",
        "RK02",
    ]
    assert next_cursor == 4
