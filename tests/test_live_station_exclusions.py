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


def test_the_barred_list_is_the_operators_current_ruling() -> None:
    """Station by station, and the released two are really gone.

    The list moved three times on 2026-08-30 -- nine named, then
    RKSI/KAUS/FACT added, then CYYZ/EDDM/NZWN added and EPWA/LTAC
    released -- so it is asserted as a set rather than by count.
    """

    parsed = json.loads(EXCLUSIONS.read_text(encoding="utf-8"))
    operator = {r["station"] for r in parsed["excluded_stations"]}
    auto = {r["station"] for r in parsed.get("auto_excluded_stations") or []}
    # Ten stations the operator barred by hand, after four rounds of rulings.
    assert operator == {
        "EDDM", "EHAM", "EPWA", "FACT", "KMIA",
        "LTAC", "RCSS", "RPLL", "SBGR", "ZUCK",
    }
    # The union is what binds: a station barred by either list is barred.
    assert control.excluded_stations() == operator | auto
    # And the two lists must not overlap -- a station managed by both would
    # let the automatic layer appear to own an operator ruling.
    assert not (operator & auto)


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

    state = control.set_probation_waivers(["LEMD"])
    assert state["live"]["station_probation_waivers"] == ["LEMD"]
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
    would also spend one of the fifteen capped places on a station nobody
    has anything against.
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
    for station in scope["stations"]:
        assert counts.get(station, 0) >= minimum or station in waived, station
    for station in scope["probation"]:
        assert counts.get(station, 0) < minimum and station not in waived
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


def test_probation_does_not_spend_a_capped_bar_place() -> None:
    """The cap is fifteen; unproven stations must not eat into it."""

    import auto_station_exclusions as auto  # noqa: PLC0415

    scope = control.live_stations()
    assert len(scope["barred"]) <= auto.MAX_BARRED_STATIONS
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


def test_the_bar_is_capped_at_fifteen_stations() -> None:
    """Operator ruling 2026-08-30, and the cap binds the machine only.

    Trimming operator rows to make room would let a budget for the
    automatic layer's judgement overrule a person's, so the cap is spent
    by hand rulings first and the layer takes what is left.
    """

    import auto_station_exclusions as auto  # noqa: PLC0415

    parsed = json.loads(EXCLUSIONS.read_text(encoding="utf-8"))
    operator = {r["station"] for r in parsed["excluded_stations"]}
    auto_barred = {
        r["station"] for r in parsed.get("auto_excluded_stations") or []
    }
    assert len(control.excluded_stations()) <= auto.MAX_BARRED_STATIONS
    assert len(auto_barred) <= auto.MAX_BARRED_STATIONS - len(operator)
    assert parsed["auto_policy"]["max_barred_stations"] == (
        auto.MAX_BARRED_STATIONS
    )


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
        state = control.set_allowlist(["ZBAA", "ZSQD"])
        assert state["live"]["station_scope"] == control.SCOPE_EXPLICIT
        assert set(state["live"]["station_allowlist"]) == {"ZBAA", "ZSQD"}
        barred = sorted(control.excluded_stations())[0]
        with pytest.raises(ValueError, match=barred):
            control.set_allowlist(["ZBAA", barred])
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
