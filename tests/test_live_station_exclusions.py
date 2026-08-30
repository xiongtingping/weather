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


def test_the_nine_named_stations_are_on_the_list() -> None:
    """The operator's 2026-08-30 ruling, station by station."""

    named = {
        "RPLL", "ZUCK", "RCSS", "EPWA", "EHAM",
        "SBGR", "KLAX", "KMIA", "LTAC",
    }
    assert control.excluded_stations() == named


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
    note = str(parsed.get("measured_note") or "")
    assert "permutation" in note
    assert "0.88,1.18" in note or "0.88, 1.18" in note
    assert parsed.get("authority")
    for row in parsed["excluded_stations"]:
        assert row["station"] and row["city"]
        assert "full_sample_roi" in row
    # The watchlist is not the list, and must not quietly become it.
    watch = set(parsed["watchlist_not_excluded"]["stations"])
    assert not (watch & control.excluded_stations())
