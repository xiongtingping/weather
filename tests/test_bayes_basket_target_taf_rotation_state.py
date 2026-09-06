from bayes_basket_target.taf_rotation_state import (
    PRIORITY_D1_TX,
    PRIORITY_GENERAL,
    PRIORITY_HELD,
    build_catalog,
    build_rotation_state,
    held_scope_prefixes,
    load_rotation_state,
    persist_rotation_state,
    scope_key,
    scope_priority_class,
    select_scopes,
    tx_bearing_station_ids,
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


# --- Charter 85 (b), 2026-09-06: held station-days first, then d1 at the TX
# stations, then the general least-recently-served queue -----------------------

def _scope(station: str, day: str, tz: str = "Asia/Shanghai", state: str = "not_started"):
    return {"station_id": station, "event_date": day, "settlement_timezone": tz,
            "day_state": state}


AS_OF = "2026-09-06T06:30:00Z"  # 14:30 in Shanghai: 09-07 is tomorrow there


def test_priority_classes() -> None:
    held = {"ZGSZ|2026-09-06"}
    tx = {"ZGSZ", "ZGGG"}
    assert scope_priority_class(_scope("ZGSZ", "2026-09-06"), held_prefixes=held,
                                tx_station_ids=tx, as_of_utc=AS_OF) == PRIORITY_HELD
    assert scope_priority_class(_scope("ZGSZ", "2026-09-07"), held_prefixes=held,
                                tx_station_ids=tx, as_of_utc=AS_OF) == PRIORITY_D1_TX
    # d2 at a TX station and d1 at a non-TX station are general.
    assert scope_priority_class(_scope("ZGSZ", "2026-09-08"), held_prefixes=held,
                                tx_station_ids=tx, as_of_utc=AS_OF) == PRIORITY_GENERAL
    assert scope_priority_class(_scope("KMIA", "2026-09-07", "America/New_York"),
                                held_prefixes=held, tx_station_ids=tx,
                                as_of_utc=AS_OF) == PRIORITY_GENERAL
    # Without a clock the d1 class cannot be decided and is not claimed.
    assert scope_priority_class(_scope("ZGSZ", "2026-09-07"), tx_station_ids=tx) == PRIORITY_GENERAL


def test_held_then_d1_tx_then_general_and_the_general_queue_still_moves() -> None:
    # Eight alphabetically-early general scopes, then the priority ones at the
    # tail -- the shape that starved ZGSZ.
    catalog = [_scope(f"AA0{i}", "2026-09-07", "UTC") for i in range(8)]
    catalog += [_scope("ZGSZ", "2026-09-06", state="open"),   # held
                _scope("ZGSZ", "2026-09-07"),                  # d1 TX
                _scope("ZGGG", "2026-09-07"),                  # d1 TX
                _scope("ZGSZ", "2026-09-08")]                  # d2 TX -> general
    selected, _, _ = select_scopes(
        catalog, previous=None, limit=6, priority_slots=3,
        held_scope_prefixes={"ZGSZ|2026-09-06"}, tx_station_ids={"ZGSZ", "ZGGG"},
        as_of_utc=AS_OF,
    )
    keys = [scope_key(s) for s in selected]
    assert keys[:3] == ["ZGSZ|2026-09-06|Asia/Shanghai", "ZGSZ|2026-09-07|Asia/Shanghai",
                        "ZGGG|2026-09-07|Asia/Shanghai"], "held first, then d1 TX by catalogue order"
    assert keys[3:] == ["AA00|2026-09-07|UTC", "AA01|2026-09-07|UTC", "AA02|2026-09-07|UTC"], (
        "the remaining slots walk the general queue"
    )
    assert [s["rotation_priority_class"] for s in selected] == [0, 1, 1, 2, 2, 2]


def test_priority_scopes_rotate_among_themselves_and_never_freeze_the_tail(tmp_path) -> None:
    catalog = [_scope(f"AA0{i}", "2026-09-07", "UTC") for i in range(4)]
    held = {f"HH0{i}|2026-09-07" for i in range(4)}
    catalog += [_scope(f"HH0{i}", "2026-09-07", "UTC") for i in range(4)]
    previous = None
    laps = []
    for gen in range(4):
        selected, cursor, sha = select_scopes(
            catalog, previous=previous, limit=4, priority_slots=2,
            held_scope_prefixes=held, as_of_utc=AS_OF,
        )
        laps.append([s["station_id"] for s in selected])
        outcomes = [{"scope_key": scope_key(s), "state": "captured", "reason": ""} for s in selected]
        previous = build_rotation_state(
            catalog=catalog, selected=selected, next_cursor=cursor, catalog_sha256=sha,
            outcomes=outcomes, previous=previous, updated_at_utc=f"2026-09-06T06:0{gen}:00Z",
        )
    # Two held per lap, least-recently-served among the held; two general per
    # lap walking AA00..AA03 -- every scope is reached inside two laps.
    assert laps[0] == ["HH00", "HH01", "AA00", "AA01"]
    assert laps[1] == ["HH02", "HH03", "AA02", "AA03"]
    assert laps[2] == ["HH00", "HH01", "AA00", "AA01"]
    assert laps[3] == ["HH02", "HH03", "AA02", "AA03"]
    assert previous["latest_selected_priority_classes"] == {
        "HH02|2026-09-07|UTC": 0, "HH03|2026-09-07|UTC": 0,
        "AA02|2026-09-07|UTC": 2, "AA03|2026-09-07|UTC": 2,
    }
    assert load_rotation_state is not None


def test_without_priority_inputs_the_plain_rotation_is_unchanged() -> None:
    catalog = [_scope(f"RK0{i}", "2026-08-01", "Asia/Seoul") for i in range(6)]
    plain, _, _ = select_scopes(catalog, previous=None, limit=4)
    assert [s["station_id"] for s in plain] == ["RK00", "RK01", "RK02", "RK03"]
    assert all(s["rotation_priority_class"] == PRIORITY_GENERAL for s in plain)


def test_the_ledger_and_capture_reads_behind_the_priority() -> None:
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE basket_target_execution_cycles (cycle_id TEXT, station_id TEXT, event_date TEXT);
        CREATE TABLE basket_target_order_intents (order_intent_id TEXT, cycle_id TEXT, order_action TEXT);
        CREATE TABLE basket_target_order_reconciliations (order_intent_id TEXT, state TEXT, filled_size REAL);
        CREATE TABLE calibration_forward_taf_capture (station_id TEXT, tx_available INTEGER, received_at_utc TEXT);
        INSERT INTO basket_target_execution_cycles VALUES ('c1','ZGSZ','2026-09-06'),('c2','KMIA','2026-09-06');
        INSERT INTO basket_target_order_intents VALUES ('i1','c1','BUY'),('i2','c2','BUY'),('i3','c2','SELL');
        INSERT INTO basket_target_order_reconciliations VALUES ('i1','resolved_filled',5.0),('i2','resolved_filled',5.0),('i3','resolved_filled',5.0);
        INSERT INTO calibration_forward_taf_capture VALUES ('ZGSZ',1,'2026-09-03T02:00:00Z'),('KMIA',0,'2026-09-06T03:50:00Z'),('OLD1',1,'2026-07-01T00:00:00Z');
        """
    )
    assert held_scope_prefixes(conn) == frozenset({"ZGSZ|2026-09-06"}), "KMIA is sold flat"
    assert tx_bearing_station_ids(conn, as_of_utc=AS_OF) == frozenset({"ZGSZ"}), (
        "US TAFs carry no TX; a TX seen 60 days ago is out of the cohort"
    )
    # Missing tables read as no priority, never as a failure.
    assert held_scope_prefixes(sqlite3.connect(":memory:")) == frozenset()
    assert tx_bearing_station_ids(sqlite3.connect(":memory:"), as_of_utc=AS_OF) == frozenset()

