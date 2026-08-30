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

    barred = {
        "RPLL", "ZUCK", "RCSS", "EHAM", "SBGR", "KLAX", "KMIA",
        "RKSI", "KAUS", "FACT",
        "CYYZ", "EDDM", "NZWN",
    }
    assert control.excluded_stations() == barred
    # Released stations must be out of the bar, not merely annotated.
    assert not ({"EPWA", "LTAC"} & control.excluded_stations())


def test_a_release_keeps_its_record() -> None:
    """A released station leaves a row saying what it was released against.

    Without it the next review sees a clean station and cannot tell that
    the call was made over a contrary reading -- which is the one thing
    that would let the same argument be had twice.
    """

    parsed = json.loads(EXCLUSIONS.read_text(encoding="utf-8"))
    released = {row["station"]: row for row in parsed["released_stations"]}
    assert {"EPWA", "LTAC"} <= set(released)
    for row in released.values():
        assert row["released_utc"] and row["note"]


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
