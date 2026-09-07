"""The exclusion list has to bind, and it has to bind the right way.

The list is a risk ruling rather than a measured rule, which is exactly why
it needs tests: nothing downstream re-derives it, so if the wiring breaks
there is no second signal that would notice.  What is asserted here is the
behaviour an operator relies on -- a barred station cannot be allowlisted,
the refusal names it, and a failed request leaves the file untouched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import live_control_server as control  # noqa: E402


EXCLUSIONS = ROOT / "config" / "trading" / "live_station_exclusions_v1.json"


def state_holds() -> set:
    """Whatever the operator is currently holding back, read fresh."""

    return set(control.live_stations().get("probation_holds") or [])


def test_the_barred_list_is_the_operators_current_ruling() -> None:
    """The properties, not the membership.

    This asserted the exact ten stations of the 2026-08-30 rulings, which
    was safe only while changing the list meant editing JSON.  It is a
    switch on the board now (2026-09-02) and the list moved nine times in
    four minutes the first time it was used -- a test that pins the
    membership reports the operator using the control as a regression,
    which is the same trap the pinning test below already carries a note
    about.  What has to hold is how the two lists compose.
    """

    parsed = json.loads(EXCLUSIONS.read_text(encoding="utf-8"))
    operator = {r["station"] for r in parsed["excluded_stations"]}
    auto = {r["station"] for r in parsed.get("auto_excluded_stations") or []}
    assert operator, "the hand list is not empty on this book"
    # The union is what binds: a station barred by either list is barred.
    assert control.excluded_stations() == operator | auto
    # And the two lists must not overlap -- a station managed by both would
    # let the automatic layer appear to own an operator ruling.
    assert not (operator & auto)
    # Every hand-ruled row still carries its evidence, whichever way it got
    # there: the switch writes the same shape the editor used to.
    for row in parsed["excluded_stations"]:
        assert row["station"] and row["city"] and "full_sample_roi" in row


def test_the_automatic_layer_never_touches_an_operator_ruling() -> None:
    """Three operator surfaces, and the machine may not write over any.

    The allowlist counts as a ruling too, and leaving it out was a real
    defect: on 2026-08-30 the layer moved to bar EFHK -- a 5/5 station on
    the live allowlist returning +13.5% -- because it earned 4.02 against
    an expected 11.17, so the fulfilment ratio read 0.36.  The union with
    the allowlist would then have failed the migration's own disjointness
    precondition and blocked the open.
    """

    import auto_station_exclusions as auto  # noqa: PLC0415

    parsed = json.loads(EXCLUSIONS.read_text(encoding="utf-8"))
    intent = json.loads(control.INTENT_PATH.read_text(encoding="utf-8"))
    protected = (
        {r["station"] for r in parsed["excluded_stations"]}
        | {r["station"] for r in parsed.get("released_stations") or []}
        | set(intent.get("station_allowlist") or [])
    )
    auto_barred = {
        r["station"] for r in parsed.get("auto_excluded_stations") or []
    }
    assert not (auto_barred & protected)
    # The decision function itself must skip them, not merely happen to.
    verdict = auto.decide(
        {s: {"settlements": 99, "fulfilment_ratio": -9.0,
             "realized_gap_usdc": -999.0} for s in sorted(protected)},
        currently_auto_barred=set(),
        operator_ruled=protected,
    )
    assert verdict["bar"] == [] and verdict["release"] == []


def test_a_profitable_station_is_never_auto_barred() -> None:
    """EFHK is the case, and the guard that used to save it was the wrong one.

    Both keys measure a shortfall against what was claimed and neither can
    see that the shortfall was still a profit.  EFHK earned +4.02 against
    an expected 11.17, so the fulfilment ratio read 0.36 and the layer
    moved to bar a station that makes money and sits on the live batch.
    It was covered for one day by treating the allowlist as an operator
    ruling -- but once the allowlist became every unbarred station that
    guard covered the whole fleet and the layer could never act at all.
    The floor states the rule directly instead.
    """

    import auto_station_exclusions as auto  # noqa: PLC0415

    verdict = auto.decide(
        {
            "PROFIT": {
                "settlements": 99, "fulfilment_ratio": -9.0,
                "realized_gap_usdc": -999.0, "realized_usdc": 4.02,
            },
            "LOSER": {
                "settlements": 99, "fulfilment_ratio": -9.0,
                "realized_gap_usdc": -999.0, "realized_usdc": -4.02,
            },
        },
        currently_auto_barred=set(),
        operator_ruled=set(),
    )
    assert [row["station"] for row in verdict["bar"]] == ["LOSER"]


def _an_unbarred_station() -> str:
    """A station the live config does not currently bar.

    These tests used to name LEMD, and broke the day the operator barred
    it: a test that needs an admissible station must not depend on which
    stations happen to be admissible today.
    """

    from bayes_basket_target.station_registry import (  # noqa: PLC0415
        coordinate_registry_manifest,
    )

    barred = set(control.excluded_stations())
    return sorted(
        str(station) for station in coordinate_registry_manifest()["station_ids"]
        if str(station) not in barred
    )[0]


def test_a_waiver_can_be_granted_and_revoked_through_the_panel(
    tmp_path, monkeypatch,
) -> None:
    """The one dial that puts an unproven station on live money, audited.

    It arrived as a config field with no writer -- readable everywhere,
    settable only by editing the JSON -- so two stations traded below the
    qualifying threshold with nothing in live_control_audit.jsonl to say
    who decided it, in a plane where every other dial turn is recorded.
    """

    before = json.loads(control.INTENT_PATH.read_text(encoding="utf-8"))
    work = tmp_path / "intent.json"
    work.write_text(json.dumps(before, ensure_ascii=False), encoding="utf-8")
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(control, "INTENT_PATH", work)
    monkeypatch.setattr(control, "AUDIT_PATH", audit)

    # Derived, not named.  This was "LEMD" until the operator barred it,
    # and the test then asserted a waiver on a barred station succeeds --
    # the opposite of the rule three lines down.  Which stations are barred
    # is live configuration and moves; that a waiver works on one that is
    # not barred does not.
    admissible = _an_unbarred_station()
    state = control.set_probation_waivers([admissible])
    assert state["live"]["station_probation_waivers"] == [admissible]
    # Revoking is an empty list, not a file edit.
    state = control.set_probation_waivers([])
    assert state["live"]["station_probation_waivers"] == []
    # A waiver on a barred station is refused rather than silently inert.
    barred = sorted(control.excluded_stations())[0]
    with pytest.raises(ValueError, match=barred):
        control.set_probation_waivers([barred])
    # Both accepted turns are on the record; the refusal is not a turn.
    lines = [
        json.loads(line)
        for line in audit.read_text(encoding="utf-8").splitlines() if line
    ]
    knobs = [row["knob"] for row in lines]
    assert knobs == ["station_probation_waivers"] * 2


def test_the_board_and_the_control_plane_cannot_disagree() -> None:
    """One rule, one owner.  The board had its own copy for one afternoon.

    They drifted the moment a waiver appeared: the server had LIMC live
    and the board drew it as probation, and a reader comparing the two had
    nothing to tell them which one was the system.  The page reads the
    control plane now rather than re-deriving membership from the same
    inputs.
    """

    import sqlite3  # noqa: PLC0415

    from calibration_dashboard import (  # noqa: PLC0415
        collect_band_residual_audit, collect_live_readiness,
    )

    db = ROOT / "data" / "bayes_basket_target_ssot.db"
    if not db.is_file():
        pytest.skip("no SSOT on this machine")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        board = collect_live_readiness(
            conn, (collect_band_residual_audit(conn)).get("stations") or []
        )
    finally:
        conn.close()
    plane = control.live_stations()
    assert sorted(board["station_allowlist"]) == sorted(plane["stations"])
    assert sorted(board["station_probation"]) == sorted(plane["probation"])
    assert sorted(board["station_exclusions"]) == sorted(plane["barred"])
    assert sorted(board["station_probation_waivers"]) == sorted(
        plane.get("probation_waivers") or []
    )
    assert sorted(board["station_probation_holds"]) == sorted(
        plane.get("probation_holds") or []
    )
    # And the page must not have grown a second copy of the derivation.
    page = (ROOT / "scripts" / "calibration_dashboard.py").read_text(
        encoding="utf-8"
    )
    assert "_live_scope()" in page
    assert "minimum_settlements_to_qualify\") or 5" not in page


def test_the_layer_is_actually_scheduled() -> None:
    """A policy that only runs when somebody invokes it has not been applied.

    The bar and release rule was written to be automatic on 2026-08-30 and
    then ran by hand for a day, which is the same gap the correction-review
    sweep beside it exists to close: a station that went bad stayed live
    until a person looked, and one that recovered stayed barred.
    """

    import os  # noqa: PLC0415
    import re  # noqa: PLC0415

    daemon = (ROOT / "scripts" / "weather_basket_yes_daemon.py").read_text(
        encoding="utf-8"
    )
    entry = re.search(
        r'\(\s*"auto_station_exclusions",\s*"([^"]+)",\s*([\d_.]+),\s*([\d_.]+),',
        daemon,
    )
    assert entry, "the sweep is not in HOSTED_SWEEPS"
    script = ROOT / entry.group(1)
    assert script.is_file(), script
    assert os.access(script, os.X_OK), f"{script} is not executable"
    # Its timeout has to leave room for the two aggregate scans it runs.
    assert float(entry.group(3)) >= 300.0
    body = script.read_text(encoding="utf-8")
    # Single-instance, or a slow pass overlaps the next tick and two writers
    # race on the same config.
    assert "single_instance auto_station_exclusions_sweep" in body
    # And it must write, or the schedule buys nothing.
    assert "auto_station_exclusions.py --apply" in body


def test_a_new_station_is_not_live_until_it_has_earned_it() -> None:
    """Default deny, operator ruling 2026-08-31 -- and three states, not two.

    "Not proven yet" and "judged and found wanting" call for opposite
    actions, so probation is its own state.  Collapsing it into the bar
    would also say something about a station nobody has anything against.
    """

    import auto_station_exclusions as auto  # noqa: PLC0415

    scope = control.live_stations()
    assert scope["scope"] == control.SCOPE_QUALIFIED
    counts = control.settlement_counts()
    minimum = scope["minimum_settlements"]
    # Admission and judgement share one threshold: admitting on less
    # evidence than the layer needs would admit something nothing could
    # ever bar for cause.
    assert minimum == auto.MIN_SETTLEMENTS == control.MIN_SETTLEMENTS_TO_QUALIFY
    # Live means qualified, or waived by name.  A waiver is the operator
    # graduating a station ahead of its settlement count (LEMD and LIMC on
    # 2026-08-31) -- which is fine and auditable, and the thing to assert is
    # that there is no third way in: nothing may be live silently.
    waived = set(scope.get("probation_waivers") or [])
    held = set(scope.get("probation_holds") or [])
    for station in scope["stations"]:
        assert counts.get(station, 0) >= minimum or station in waived, station
        # A held station never trades, whatever its count says.
        assert station not in held, station
    for station in scope["probation"]:
        # Two ways to be on probation and they are different facts: the
        # settlement count has not got there yet, or the operator is holding
        # the station back by hand (2026-09-02 switch).
        assert (
            (counts.get(station, 0) < minimum and station not in waived)
            or station in held
        ), station
    # Every waiver has to be written down where an operator can revoke it.
    intent = json.loads(control.INTENT_PATH.read_text(encoding="utf-8"))
    assert waived == {
        str(s).strip().upper()
        for s in (intent.get("station_probation_waivers") or [])
    }
    # A waiver cannot smuggle a barred station back in: the bar is checked
    # first, so this must hold no matter what anybody writes in the list.
    assert not (waived & set(scope["barred"]))
    # A station nobody has ever settled is not live and not barred.
    assert "LLBG" not in scope["stations"]
    assert "LLBG" not in scope["barred"]


def test_the_three_states_are_disjoint_and_cover_what_trades() -> None:
    scope = control.live_stations()
    live, probation, barred = (
        set(scope["stations"]), set(scope["probation"]), set(scope["barred"])
    )
    assert not (live & probation) and not (live & barred)
    assert not (probation & barred)
    assert live | probation == control.tradeable_stations() - barred


def test_probation_is_not_the_bar() -> None:
    """Two states, two meanings, and nothing may be in both.

    This used to assert that probation ate none of the fifteen capped bar
    places.  The cap is gone (operator ruling 2026-09-02) and the reason the
    two states are separate did not go with it: "not proven yet" and "judged
    and found wanting" call for opposite actions.
    """

    scope = control.live_stations()
    assert not (set(scope["probation"]) & set(scope["barred"]))


def test_the_live_batch_is_the_bars_complement_not_a_list() -> None:
    """Operator ruling 2026-08-30, and the reason is that lists go stale.

    The eight-station batch was wrong within two days -- KLGA and KSFO
    reached 5/5 while absent from it, KORD stayed on it with three chains
    -- and the fleet roster moved 45 to 48 in the same window.  Deriving
    the batch means a station that comes online joins by itself and a
    station that is barred leaves by itself, and the two sets cannot drift
    apart or overlap because one is computed from the other.
    """

    scope = control.live_stations()
    assert scope["scope"] in (
        control.SCOPE_QUALIFIED, control.SCOPE_ALL_EXCEPT_BARRED
    )
    barred = control.excluded_stations()
    tradeable = control.tradeable_stations()
    assert set(scope["stations"]) <= tradeable - barred
    # Disjointness by construction, asserted anyway: the migration refuses
    # to run on an overlap, and a precondition nobody checks is not one.
    assert not (set(scope["stations"]) & barred)
    # And the stored list stays empty in this mode -- a populated one is the
    # stale-list trap the ruling replaced.
    intent = json.loads(control.INTENT_PATH.read_text(encoding="utf-8"))
    assert not (intent.get("station_allowlist") or [])


def test_a_station_the_model_never_computed_is_not_in_the_batch() -> None:
    """Membership comes from book history, not the coordinate roster.

    LLBG, LTFM and UUWW are on the roster and have never produced a
    position, so a complement over the roster would put stations into a
    live batch that cannot trade.
    """

    scope = set(control.live_stations()["stations"])
    for station in ("LLBG", "LTFM", "UUWW"):
        assert station not in scope


def test_the_bar_has_no_ceiling() -> None:
    """Operator ruling 2026-09-02: the fifteen-station cap is lifted.

    What the cap did on this book was hide the layer's own verdicts rather
    than restrain them.  On the day it came off, the criterion had barred
    six more stations (KDAL, KLGA, KORD, RJTT, ZBAA, ZSQD) and every one was
    being dropped on every pass, so the stored list was not the list the
    criterion produced -- and a reader could not tell the difference.
    Barring costs one skipped market, and the board's switch retracts any
    row in one click.

    The disjointness the cap was tangled up with is asserted here because it
    still has to hold: one station, one bar list.
    """

    import auto_station_exclusions as auto  # noqa: PLC0415

    parsed = json.loads(EXCLUSIONS.read_text(encoding="utf-8"))
    operator = {r["station"] for r in parsed["excluded_stations"]}
    auto_barred = {
        r["station"] for r in parsed.get("auto_excluded_stations") or []
    }
    assert auto.MAX_BARRED_STATIONS is None
    assert parsed["auto_policy"]["max_barred_stations"] is None
    assert not (operator & auto_barred)
    # And no trim survives anywhere: a decision to bar must reach the file.
    verdict = auto.decide(
        {f"S{i:03d}": {"settlements": 99, "fulfilment_ratio": -9.0,
                       "realized_gap_usdc": -999.0, "realized_usdc": -1.0}
         for i in range(40)},
        currently_auto_barred=set(),
        operator_ruled=set(),
    )
    assert len(verdict["bar"]) == 40


def test_the_bar_never_intersects_the_live_allowlist() -> None:
    """If it ever did, the go-live migration would refuse to run."""

    intent = json.loads(control.INTENT_PATH.read_text(encoding="utf-8"))
    assert not (
        set(intent.get("station_allowlist") or []) & control.excluded_stations()
    )


def test_a_reversal_stays_visible_in_the_record() -> None:
    """A station that went out and came back must say so on its own row.

    EPWA and LTAC were released and re-barred on 2026-08-30.  Smoothing
    that into a plain bar would leave the list unable to be scored later:
    the interesting question about a station is not only whether it is
    barred but how many times the call has changed.
    """

    parsed = json.loads(EXCLUSIONS.read_text(encoding="utf-8"))
    rows = {row["station"]: row for row in parsed["excluded_stations"]}
    for station in ("EPWA", "LTAC"):
        row = rows[station]
        assert row.get("released_utc") and row.get("rebarred_utc")
        assert row.get("note")
    # Anything still sitting in released_stations must really be released.
    for row in parsed.get("released_stations") or []:
        assert row["station"] not in control.excluded_stations()


def test_the_allowlist_and_the_exclusions_do_not_overlap() -> None:
    """The live open cannot be carrying a barred station right now."""

    intent = json.loads(
        (ROOT / "config" / "trading" / "live_intent_v1.json")
        .read_text(encoding="utf-8")
    )
    allowlist = set(intent.get("station_allowlist") or [])
    assert not (allowlist & control.excluded_stations())


def test_a_barred_station_cannot_be_allowlisted() -> None:
    """And the whole request fails rather than being quietly trimmed.

    Returning a shortened list would leave the caller believing a station
    is live when it is not, which is the failure this gate exists to
    prevent -- so the refusal names the station and nothing is written.
    """

    before = json.loads(control.INTENT_PATH.read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="RPLL"):
        control.set_allowlist(["ZBAA", "RPLL"])
    after = json.loads(control.INTENT_PATH.read_text(encoding="utf-8"))
    assert before == after, "a refused request must not touch the file"


def test_pinning_a_list_switches_the_scope_and_still_refuses_barred(
    tmp_path, monkeypatch,
) -> None:
    """Explicit mode is still reachable, and the bar still wins inside it.

    Restores derived mode afterwards -- this is the live config, and a test
    that left the batch pinned would quietly freeze the thing the ruling
    was made to unfreeze.
    """

    # Against copies, not the running configuration.  The first version of
    # this drove the real control server: it wrote the live intent file and
    # left six station_allowlist entries in data/live_control_audit.jsonl,
    # which is the log an operator reads to answer "who turned this dial".
    # A test that forges entries there can mask a real one.
    before = json.loads(control.INTENT_PATH.read_text(encoding="utf-8"))
    work = tmp_path / "intent.json"
    work.write_text(json.dumps(before, ensure_ascii=False), encoding="utf-8")
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(control, "INTENT_PATH", work)
    monkeypatch.setattr(control, "AUDIT_PATH", audit)
    try:
        # Picked from what is currently admissible rather than named.  This
        # pinned ZBAA and ZSQD by hand until 2026-09-01, when the automatic
        # layer barred both and the test began failing on a config change
        # that was the layer working correctly -- the bar list is not a
        # constant and a test that treats it as one reports fleet news as a
        # regression.  Only the property is asserted: whatever is admissible
        # can be pinned, and whatever is barred cannot.
        barred = sorted(control.excluded_stations())
        assert barred, "this test needs at least one barred station"
        candidates = [
            "ZBAA", "ZSQD", "ZGGG", "ZUUU", "RJTT", "RKSI", "WSSS", "WMKK",
            "EGLL", "LFPG", "EDDF", "KSFO", "KORD", "KSEA", "KLAX",
        ]
        # Held stations are excluded here too: a hold keeps a station out of
        # the batch in every scope, so a pinned list containing one would
        # come back short and the assertion below would read that as a bug.
        held = set(state_holds())
        admissible = [
            s for s in candidates if s not in set(barred) and s not in held
        ][:2]
        assert len(admissible) == 2, "this test needs two admissible stations"
        state = control.set_allowlist(admissible)
        assert state["live"]["station_scope"] == control.SCOPE_EXPLICIT
        assert set(state["live"]["pinned_allowlist"]) == set(admissible)
        assert set(state["live"]["station_allowlist"]) == set(admissible)
        with pytest.raises(ValueError, match=barred[0]):
            control.set_allowlist([admissible[0], barred[0]])
    finally:
        pass
    # The live configuration was never opened for writing, so there is
    # nothing to restore and nothing to have got wrong -- which is the
    # property the earlier restore-by-hand version kept failing to hold.
    assert json.loads(
        Path(str(control.__dict__["ROOT"]) + "/config/trading/live_intent_v1.json")
        .read_text(encoding="utf-8")
    ) == before


def test_a_missing_file_bars_nothing_rather_than_everything() -> None:
    """This gate refuses admissions; it must never be able to grant one.

    Failing open looks wrong until you name what it opens: the allowlist,
    which is a separate positive list somebody had to write.  Failing
    closed instead would let a deleted config empty the live book, which
    is a bigger surprise than the one it would prevent.
    """

    saved = control.EXCLUSION_PATH
    try:
        control.EXCLUSION_PATH = ROOT / "config" / "trading" / "no_such.json"
        assert control.excluded_stations() == set()
    finally:
        control.EXCLUSION_PATH = saved


def test_the_file_carries_its_evidence() -> None:
    """A hand-entered list has to say why, or the next reader re-litigates it.

    Specifically it must record that the mechanical screens were tested and
    found to carry no signal -- without that, somebody will helpfully
    automate this list back into existence from recent ROI.
    """

    parsed = json.loads(EXCLUSIONS.read_text(encoding="utf-8"))
    note = str(parsed.get("why_not_automatic") or "")
    assert "walk-forward" in note.lower()
    assert "p=0.346" in note
    assert parsed.get("authority")
    # The review policy must keep saying that its own criteria do not decide.
    policy = parsed["review_policy"]
    assert policy["add_candidate_when"] and policy["release_candidate_when"]
    assert "not" in policy["candidates_are_not_decisions"].lower()
    # And a standing candidate must never already be barred/released silently.
    standing = policy["standing_candidates_2026_08_30"]
    assert not (set(standing["add"]) & control.excluded_stations())
    assert set(standing["release"]) <= control.excluded_stations()
    # Release must stay stricter than barring.  An ROI-only release rule let
    # EPWA out on a technicality while every forward seat it ever took lost,
    # so the criterion has to keep naming the structural gates.
    release = policy["release_candidate_when"]
    assert "centre bias" in release and "0.88" in release
    assert policy["why_release_is_stricter"]
    for row in parsed["excluded_stations"]:
        assert row["station"] and row["city"]
        assert "full_sample_roi" in row
    # The watchlist is not the list, and must not quietly become it.
    watch = set(parsed["watchlist_not_excluded"]["stations"])
    assert not (watch & control.excluded_stations())


def test_a_barred_station_leaves_the_watchlist_in_the_same_move() -> None:
    """Barring is a verdict, and the watchlist is for stations without one.

    The watchlist defines itself as "persistently negative in both halves
    and *not barred*", so a station the automatic layer bars stops meeting
    its own entry condition.  Until 2026-09-01 the layer did not know the
    watchlist existed: KHOU was barred on 09-01 and stayed listed as under
    review, so the file asserted two opposite things about one station.
    The same invariant the layer already maintains against the live batch,
    now maintained against the watchlist too.
    """

    parsed = json.loads(EXCLUSIONS.read_text(encoding="utf-8"))
    watch = set(parsed["watchlist_not_excluded"]["stations"])
    auto = {
        str(row["station"]).strip().upper()
        for row in (parsed.get("auto_excluded_stations") or [])
    }
    manual = {
        str(row["station"]).strip().upper()
        for row in (parsed.get("excluded_stations") or [])
    }
    assert not (watch & auto), "a barred station is not still under review"
    assert not (watch & manual)


def _config_copies(tmp_path, monkeypatch):
    """Point the whole control plane at copies of the two live files.

    Every switch test runs against these.  The first version of the waiver
    test drove the real server and left six entries in
    ``data/live_control_audit.jsonl`` -- the log an operator reads to answer
    "who turned this dial" -- and a test that forges entries there can mask
    a real one.
    """

    import bayes_basket_target.live_station_scope as scope_rules  # noqa: PLC0415

    intent = tmp_path / "intent.json"
    exclusions = tmp_path / "exclusions.json"
    intent.write_text(
        control.INTENT_PATH.read_text(encoding="utf-8"), encoding="utf-8"
    )
    exclusions.write_text(
        EXCLUSIONS.read_text(encoding="utf-8"), encoding="utf-8"
    )
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(control, "INTENT_PATH", intent)
    monkeypatch.setattr(control, "EXCLUSION_PATH", exclusions)
    monkeypatch.setattr(control, "AUDIT_PATH", audit)
    monkeypatch.setattr(scope_rules, "INTENT_PATH", intent)
    monkeypatch.setattr(scope_rules, "EXCLUSION_PATH", exclusions)
    return intent, exclusions, audit


def test_the_bar_is_a_switch_and_writes_a_ruling_with_its_evidence(
    tmp_path, monkeypatch,
) -> None:
    """禁入, one click, and the row it writes carries what it was ruled on.

    A bar written as a bare name would make the next reader re-derive what
    the operator was looking at, which is the re-litigation the file's
    ``why_not_automatic`` note exists to prevent -- and the file's own test
    asserts every hand-ruled row carries a city and a full-sample ROI, so a
    switch that could write one without them would break the list it edits.
    """

    intent, exclusions, audit = _config_copies(tmp_path, monkeypatch)
    station = "KLGA"
    assert station not in control.excluded_stations()

    control.set_station_bar(station, True, by="test")
    doc = json.loads(exclusions.read_text(encoding="utf-8"))
    row = next(
        r for r in doc["excluded_stations"] if r["station"] == station
    )
    assert row["city"] and "full_sample_roi" in row and row["added_utc"]
    assert station in control.excluded_stations()
    assert station not in set(control.live_stations()["stations"])
    # Nothing gives way to make room: the ceiling was lifted on 2026-09-02,
    # so a hand ruling adds a row and releases none.
    parsed = json.loads(exclusions.read_text(encoding="utf-8"))
    operator = {r["station"] for r in parsed["excluded_stations"]}
    auto = {r["station"] for r in parsed["auto_excluded_stations"]}
    assert not (operator & auto), "one station, one bar list"

    control.set_station_bar(station, False, by="test")
    assert station not in control.excluded_stations()
    doc = json.loads(exclusions.read_text(encoding="utf-8"))
    released = {r["station"] for r in doc["released_stations"]}
    assert station in released, "a release is a ruling, and it is recorded"

    entries = [
        json.loads(line)
        for line in audit.read_text(encoding="utf-8").splitlines() if line
    ]
    assert [e["knob"] for e in entries] == ["station_bar", "station_bar"]
    assert entries[0]["station"] == station and entries[0]["barred"] is True
    assert entries[1]["barred"] is False


def test_a_hand_bar_releases_nobody_to_make_room(tmp_path, monkeypatch) -> None:
    """The behaviour the ceiling forced, and the reason it was lifted.

    Under the cap each hand ruling past fifteen evicted the automatic
    layer's weakest row, so barring one station silently un-barred another:
    on 2026-09-02 KORD released KHOU, KHOU released SAEZ, ZBAA released
    ZUUU, RKSI released VILK and KAUS released KSEA, and two of the freed
    ones had to be barred again by hand seconds later.
    """

    intent, exclusions, audit = _config_copies(tmp_path, monkeypatch)
    doc = json.loads(exclusions.read_text(encoding="utf-8"))
    # A full hand list and a standing automatic one, the shape that used to
    # trigger the eviction.
    doc["excluded_stations"] = [
        {"station": f"X{i:03d}", "city": "x", "full_sample_roi": -0.5,
         "added_utc": "2026-09-02"} for i in range(15)
    ]
    doc["auto_excluded_stations"] = [
        {"station": "KSFO", "barred_on": "fulfilment_ratio", "value": -0.9,
         "settlements": 9, "auto_barred_utc": "2026-09-02"},
        {"station": "OEJN", "barred_on": "realized_gap_usdc", "value": -20.0,
         "settlements": 9, "auto_barred_utc": "2026-09-02"},
    ]
    doc["released_stations"] = []
    exclusions.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    control.set_station_bar("MMMX", True, by="test")
    after = json.loads(exclusions.read_text(encoding="utf-8"))
    assert {r["station"] for r in after["auto_excluded_stations"]} == {
        "KSFO", "OEJN"
    }
    assert len(after["excluded_stations"]) == 16
    assert len(control.excluded_stations()) == 18
    # And nothing was released to pay for it.
    assert after["released_stations"] == []


def test_releasing_an_automatic_bar_keeps_the_sweep_off_the_station(
    tmp_path, monkeypatch,
) -> None:
    """Otherwise the click is undone minutes later and the switch looks broken.

    The automatic layer re-decides every sweep from the same two keys that
    barred the station in the first place, so removing its row is not enough
    -- the operator's release has to be recorded as a ruling, which is the
    one thing the layer will not touch.
    """

    import auto_station_exclusions as auto  # noqa: PLC0415

    intent, exclusions, audit = _config_copies(tmp_path, monkeypatch)
    monkeypatch.setattr(auto, "CONFIG", exclusions)
    monkeypatch.setattr(auto, "INTENT", intent)
    doc = json.loads(exclusions.read_text(encoding="utf-8"))
    # Planted rather than borrowed from the live file.  The automatic list
    # is empty whenever the operator has ruled on everything the criterion
    # would reach -- which is what happened the day the switches shipped --
    # and a test that needs one has to bring its own.
    station = "KSFO"
    doc["excluded_stations"] = [
        r for r in doc["excluded_stations"] if r["station"] != station
    ]
    doc["released_stations"] = [
        r for r in (doc.get("released_stations") or [])
        if r["station"] != station
    ]
    doc["auto_excluded_stations"] = [{
        "station": station, "barred_on": "fulfilment_ratio",
        "value": -0.9, "settlements": 9, "auto_barred_utc": "2026-09-02",
    }]
    exclusions.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    control.set_station_bar(station, False, by="test")
    assert station not in control.excluded_stations()
    verdict = auto.run(apply_changes=False)
    assert station in verdict["operator_ruled"]
    assert station not in [row["station"] for row in verdict["barred_now"]]


def test_waiver_and_hold_are_switches_and_exclude_each_other(
    tmp_path, monkeypatch,
) -> None:
    """豁免 and 待观察 are opposite instructions about the same station.

    Holding one while the other stands would leave the file saying two
    things, and which one wins would be an accident of read order.  The
    exclusivity is enforced where the write happens rather than checked
    downstream.
    """

    intent, exclusions, audit = _config_copies(tmp_path, monkeypatch)
    station = _an_unbarred_station()

    state = control.set_station_waiver(station, True, by="test")
    assert station in state["live"]["station_probation_waivers"]
    assert station in state["live"]["station_allowlist"], (
        "a waiver is what puts an unqualified station in the batch"
    )

    state = control.set_station_hold(station, True, by="test")
    assert station in state["live"]["station_probation_holds"]
    assert station not in state["live"]["station_probation_waivers"]
    assert station not in state["live"]["station_allowlist"]
    assert station in state["live"]["station_probation"]

    # A hold is not a bar: it says nothing about the station being bad.
    assert station not in state["live"]["station_exclusions"]

    state = control.set_station_hold(station, False, by="test")
    # Whatever else the operator is holding stays held -- this switch moves
    # one station, and asserting an empty list would make the test depend on
    # nobody else having used the board.
    assert station not in state["live"]["station_probation_holds"]


def test_a_barred_station_takes_neither_of_the_other_two_switches(
    tmp_path, monkeypatch,
) -> None:
    """Refused, not accepted-and-inert.

    The bar is checked first in the derivation, so a waiver or a hold on a
    barred station would do nothing at all -- and a list that reads like a
    decision and does nothing is worse than a refusal that names the reason.
    """

    _config_copies(tmp_path, monkeypatch)
    barred = sorted(control.excluded_stations())[0]
    with pytest.raises(ValueError, match=barred):
        control.set_station_waiver(barred, True)
    with pytest.raises(ValueError, match=barred):
        control.set_station_hold(barred, True)
    # And barring a station takes back whatever it was carrying.
    station = _an_unbarred_station()
    control.set_station_waiver(station, True)
    control.set_station_bar(station, True)
    state = control.state()
    assert station in state["live"]["station_exclusions"]
    assert station not in state["live"]["station_probation_waivers"]


def test_a_switch_that_is_not_a_station_is_refused(
    tmp_path, monkeypatch,
) -> None:
    _config_copies(tmp_path, monkeypatch)
    for bad in ("", "no", "not-a-station", "TOOLONGID"):
        with pytest.raises(ValueError):
            control.set_station_bar(bad, True)


def _submission_conn(station: str) -> "sqlite3.Connection":  # noqa: F821
    """A book with one prepared intent, and enough history to qualify.

    The station gate reads the same two facts the board does -- which
    stations the model has ever produced for, and how many settlements each
    has -- so a test of the gate has to give it a book to read rather than
    stub the answer.
    """

    import sqlite3  # noqa: PLC0415

    from bayes_basket_target.ssot import (  # noqa: PLC0415
        apply_production_ssot_schema,
    )

    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    conn.execute(
        "CREATE TABLE paper_trade_position(position_id TEXT PRIMARY KEY,"
        " station_id TEXT, event_date TEXT)"
    )
    conn.execute(
        "CREATE TABLE paper_trade_settlement(settlement_id TEXT PRIMARY KEY,"
        " position_id TEXT, station_id TEXT, event_date TEXT,"
        " stake_usdc REAL, pnl_usdc REAL)"
    )
    for index in range(6):
        conn.execute(
            "INSERT INTO paper_trade_position VALUES(?,?,?)",
            (f"pos-{index}", station, f"2026-08-2{index}"),
        )
        conn.execute(
            "INSERT INTO paper_trade_settlement VALUES(?,?,?,?,?,?)",
            (f"set-{index}", f"pos-{index}", station,
             f"2026-08-2{index}", 10.0, 1.0),
        )
    conn.execute(
        "INSERT INTO basket_target_execution_cycles(cycle_id,plan_id,"
        "station_id,event_date,settlement_timezone,strategy_lane,plan_sha256,"
        "requested_reserve_usdc,state,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("cycle-1", "plan-1", station, "2026-08-30", "UTC", "basket",
         f"{0:064d}", 10.0, "prepared", "2026-08-30T09:00:00Z",
         "2026-08-30T09:00:00Z"),
    )
    conn.execute(
        "INSERT INTO basket_target_order_intents(order_intent_id,decision_id,"
        "cycle_id,bucket,token_id,side,order_action,limit_price,"
        "requested_size,requested_notional_usdc,state,live_submit_authorized,"
        "idempotency_key,intent_json,intent_sha256,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("intent-a", "decision-1", "cycle-1", "35", "token-35", "YES", "BUY",
         0.12, 40.0, 4.8, "prepared", 1, "idem-a", "{}", f"{1:064d}",
         "2026-08-30T09:00:00Z"),
    )
    return conn


def test_the_bar_stops_the_send_and_not_only_the_screen(
    tmp_path, monkeypatch,
) -> None:
    """The gate binds at the one place that spends money.

    Until 2026-09-02 the bar was consulted by the panel, drawn on the board
    and checked by the go-live migration's precondition, and the submit path
    did not read it at all: EDDM, KSEA and LTAC all carry live-authorised
    intents minted while they were barred.  A switch that stops nothing is
    worse than no switch, because people rely on it.
    """

    from bayes_basket_target import order_submission as submission  # noqa: PLC0415

    intent_path, exclusions, _audit = _config_copies(tmp_path, monkeypatch)
    station = "KLGA"
    conn = _submission_conn(station)

    armed = tmp_path / "armed.json"
    armed.write_text('{"live_enabled": true}', encoding="utf-8")
    monkeypatch.setattr(submission, "LIVE_INTENT_PATH", armed)

    def _never(_path, _payload):
        raise AssertionError("a barred station must not reach the exchange")

    control.set_station_bar(station, True, by="test")
    with conn:
        receipt = submission.submit_prepared_intents(
            conn, cycle_id="cycle-1", transport=_never,
            live_submit_authorized=True,
            observed_at_utc="2026-08-30T09:30:00Z",
        )
    assert receipt["orders_submitted"] == 0
    refusal = receipt["submissions"][0]
    assert refusal["reason"] == "live_station_barred"
    assert refusal["exchange_post_started"] is False
    # The whole path still ran except the send, so the payload it would have
    # sent is on the receipt -- the same shape the disarmed branch produces.
    assert refusal["payload_sha256"]
    assert refusal["station_id"] == station

    # A hold refuses too, and says something different: nobody ruled this
    # station bad, it is being kept back.
    control.set_station_bar(station, False, by="test")
    control.set_station_hold(station, True, by="test")
    with conn:
        receipt = submission.submit_prepared_intents(
            conn, cycle_id="cycle-1", transport=_never,
            live_submit_authorized=True,
            observed_at_utc="2026-08-30T09:30:00Z",
        )
    assert receipt["submissions"][0]["reason"] == "live_station_held"

    # And with both switches off the same intent goes out: the gate refuses
    # admissions, it does not invent them.
    control.set_station_hold(station, False, by="test")
    sent: list = []

    def _accept(_path, payload):
        sent.append(payload)
        return {
            "ok": True, "accepted": True, "pending": False,
            "reconcile_required": False, "exchange_post_started": True,
            "exchange_order_id": "exch-1", "source": "clob_order_service",
        }

    with conn:
        receipt = submission.submit_prepared_intents(
            conn, cycle_id="cycle-1", transport=_accept,
            live_submit_authorized=True,
            observed_at_utc="2026-08-30T09:30:00Z",
        )
    assert len(sent) == 1 and receipt["orders_submitted"] == 1


def test_a_bar_does_not_strand_an_open_position(tmp_path, monkeypatch) -> None:
    """The bar refuses admissions; it must not block the way out.

    A station barred while holding a position has to stay able to trim,
    reseat or exit it -- otherwise the one ruling made to reduce risk is the
    thing that prevents the reduction.  So the gate reads the order's
    direction, and the other submit path (certain_win_recycle) is an exit
    path by construction.
    """

    from bayes_basket_target import order_submission as submission  # noqa: PLC0415

    _config_copies(tmp_path, monkeypatch)
    station = "KLGA"
    conn = _submission_conn(station)
    conn.execute(
        "UPDATE basket_target_order_intents SET order_action='SELL'"
        " WHERE order_intent_id='intent-a'"
    )
    armed = tmp_path / "armed.json"
    armed.write_text('{"live_enabled": true}', encoding="utf-8")
    monkeypatch.setattr(submission, "LIVE_INTENT_PATH", armed)
    control.set_station_bar(station, True, by="test")

    sent: list = []

    def _accept(_path, payload):
        sent.append(payload)
        return {
            "ok": True, "accepted": True, "pending": False,
            "reconcile_required": False, "exchange_post_started": True,
            "exchange_order_id": "exch-2", "source": "clob_order_service",
        }

    with conn:
        receipt = submission.submit_prepared_intents(
            conn, cycle_id="cycle-1", transport=_accept,
            live_submit_authorized=True,
            observed_at_utc="2026-08-30T09:30:00Z",
        )
    assert receipt["orders_submitted"] == 1 and len(sent) == 1
