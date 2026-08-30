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


def test_an_auto_bar_leaves_the_live_batch_in_the_same_move() -> None:
    """Otherwise the two lists overlap and the migration refuses to run."""

    parsed = json.loads(EXCLUSIONS.read_text(encoding="utf-8"))
    intent = json.loads(control.INTENT_PATH.read_text(encoding="utf-8"))
    auto_barred = {
        r["station"] for r in parsed.get("auto_excluded_stations") or []
    }
    assert not (auto_barred & set(intent.get("station_allowlist") or []))


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


def test_a_clean_list_still_passes() -> None:
    """The gate must not be a blanket refusal."""

    before = json.loads(control.INTENT_PATH.read_text(encoding="utf-8"))
    original = list(before.get("station_allowlist") or [])
    try:
        state = control.set_allowlist(original)
        assert set(state["live"]["station_allowlist"]) == set(original)
        assert state["live"]["station_exclusions"] == sorted(
            control.excluded_stations()
        )
    finally:
        control.set_allowlist(original)


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
