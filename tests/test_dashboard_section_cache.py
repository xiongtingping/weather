"""The dashboard's section cache: what it may reuse, and what it must not.

A render rebuilds every section from scratch and the hosted sweep starts one
every thirty minutes; measured 2026-09-02 a full build took about fourteen
minutes, so the page a reader opened was routinely hours old.  Slow-moving
sections now keep their last value on disk.

The risk that buys is a section that silently stops refreshing, which this
codebase has been bitten by before -- a light that reads green because
nothing recomputed it.  So the rules are pinned here rather than left to the
comment above the table: the health banner's inputs and everything about the
live book are never cached, a hit and a miss hand back the same structure,
and a cache that cannot be read is rebuilt rather than raised.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "SECTION_CACHE_DIR", tmp_path / "sections")
    monkeypatch.setattr(C, "SECTION_CACHE_ENABLED", True)
    monkeypatch.setattr(C, "SECTION_CACHE_TTL_SEC", {"probe": 3600})
    C._SECTION_CACHE_LOG.clear()
    return tmp_path / "sections"


def _probe(calls):
    def collector(conn, tag):
        calls.append(tag)
        return {"pair": ("a", "b"), "n": len(calls)}
    return collector


def test_a_second_render_reuses_the_first_ones_value(cache_dir) -> None:
    calls: list[str] = []
    collector = _probe(calls)
    first = C._cached_section("probe", collector, None, "first")
    second = C._cached_section("probe", collector, None, "second")
    assert calls == ["first"], "the collector ran twice"
    assert first == second
    assert C._SECTION_CACHE_LOG["probe"]["reused"] is True


def test_a_hit_and_a_miss_hand_back_the_same_structure(cache_dir) -> None:
    """The miss round-trips through JSON on purpose.

    Without it a tuple would reach the page as a tuple on the one render
    that computed it and as a list on the five that reused it, and the
    difference would surface as a bug in whichever downstream reader cared.
    """

    calls: list[str] = []
    collector = _probe(calls)
    miss = C._cached_section("probe", collector, None, "first")
    hit = C._cached_section("probe", collector, None, "second")
    assert miss["pair"] == ["a", "b"] == hit["pair"]
    assert type(miss["pair"]) is type(hit["pair"])


def test_editing_the_collector_discards_its_cached_value(cache_dir) -> None:
    calls: list[str] = []
    C._cached_section("probe", _probe(calls), None, "first")

    def edited(conn, tag):          # a different body, so a different sha
        calls.append(tag)
        return {"pair": ("c", "d"), "n": 99}

    value = C._cached_section("probe", edited, None, "second")
    assert calls == ["first", "second"]
    assert value["n"] == 99


def test_an_expired_value_is_not_served(cache_dir, monkeypatch) -> None:
    calls: list[str] = []
    collector = _probe(calls)
    C._cached_section("probe", collector, None, "first")
    monkeypatch.setattr(C, "SECTION_CACHE_TTL_SEC", {"probe": 0.0})
    C._cached_section("probe", collector, None, "second")
    assert calls == ["first", "second"]


def test_an_unreadable_cache_is_rebuilt_rather_than_raised(cache_dir) -> None:
    calls: list[str] = []
    collector = _probe(calls)
    C._cached_section("probe", collector, None, "first")
    (cache_dir / "probe.json").write_text("{ not json", encoding="utf-8")
    C._cached_section("probe", collector, None, "second")
    assert calls == ["first", "second"]


def test_the_switch_off_recomputes_everything(cache_dir, monkeypatch) -> None:
    calls: list[str] = []
    collector = _probe(calls)
    C._cached_section("probe", collector, None, "first")
    monkeypatch.setattr(C, "SECTION_CACHE_ENABLED", False)
    C._cached_section("probe", collector, None, "second")
    assert calls == ["first", "second"]


def test_a_section_with_no_lifetime_is_never_cached(cache_dir) -> None:
    """No lifetime means recomputed every time -- but still MEASURED.

    This used to assert the section was absent from ``_SECTION_CACHE_LOG``,
    using "no record" to stand for "no cache".  That conflated the two, and
    the cost was paid on 2026-09-09: only the nine sections with a lifetime
    were ever timed, so a 24-minute render had a breakdown for the parts
    already known to be slow and silence for the rest.  A section without a
    lifetime is not a section without a price.  The rule it was really
    pinning -- recompute every call, write nothing to disk -- is asserted
    directly below.
    """

    calls: list[str] = []
    collector = _probe(calls)
    C._cached_section("not_listed", collector, None, "first")
    C._cached_section("not_listed", collector, None, "second")
    assert calls == ["first", "second"]
    entry = C._SECTION_CACHE_LOG["not_listed"]
    assert entry["cached"] is False
    assert entry["reused"] is False
    assert entry["build_sec"] >= 0.0
    assert not (cache_dir / "not_listed.json").exists()


def test_every_timed_section_is_comparable_to_every_other(cache_dir) -> None:
    """One field answers "what did this build pay for this section?".

    A breakdown is only sortable if the cached and uncached rows carry the
    same key, and a reused row must carry no cost at all -- charging a build
    for a section it did not compute would send the next fix at the wrong
    one.
    """

    calls: list[str] = []
    collector = _probe(calls)
    C._cached_section("not_listed", collector, None, "a")
    C._cached_section("probe", collector, None, "b")     # has a TTL: miss
    C._cached_section("probe", collector, None, "c")     # ... then a hit
    uncached = C._SECTION_CACHE_LOG["not_listed"]
    cached = C._SECTION_CACHE_LOG["probe"]
    assert uncached["build_sec"] >= 0.0 and cached["reused"] is True
    # The hit replaced the miss's record and carries no build_sec, so a sum
    # over the log is what THIS build spent, not what a cold one would.
    assert "build_sec" not in cached
    assert cached["cached"] is True


# --------------------------------------------------------------------------
# The two safety rules.  These are the point of the file: the speed-up is
# only allowed to touch sections where a stale answer is not a wrong answer.
# --------------------------------------------------------------------------

# Everything the banner's light is computed from.  ``_build_payload`` derives
# ``alerts`` from exactly these, so a stale one is a light that says green
# about a state that has already gone red.
HEALTH_BANNER_SOURCES = {
    "members", "chain", "stages", "truth", "worker", "delta_v2_authority",
}
# The live book, which is what the page is opened for during a session.
LIVE_SECTIONS = {
    "live_readiness", "live_selection", "live_days", "live_money",
    "window_divergence",
}


def test_the_health_banners_inputs_are_never_cached() -> None:
    overlap = HEALTH_BANNER_SOURCES & set(C.SECTION_CACHE_TTL_SEC)
    assert not overlap, (
        f"{sorted(overlap)} feeds the banner light and must be rebuilt every"
        " render: a cached red that has not been recomputed reads as green"
    )


def test_the_live_book_is_never_cached() -> None:
    overlap = LIVE_SECTIONS & set(C.SECTION_CACHE_TTL_SEC)
    assert not overlap, (
        f"{sorted(overlap)} is what the page is read for mid-session and"
        " must be computed against the moment the page was built"
    )


def test_every_cached_section_is_a_key_the_payload_actually_ships() -> None:
    """A lifetime for a name no section carries caches nothing and hides it.

    Read off ``_build_payload``'s returned dict rather than a second list
    here, so a section that gets renamed fails this instead of silently
    losing its cache.
    """

    import ast

    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    fn = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_build_payload"
    )
    returned = next(
        node for node in reversed(fn.body) if isinstance(node, ast.Return)
    )
    assert isinstance(returned.value, ast.Dict)
    shipped = {
        key.value for key in returned.value.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    missing = set(C.SECTION_CACHE_TTL_SEC) - shipped
    assert not missing, f"{sorted(missing)} is cached but never shipped"


def test_the_lifetimes_are_staggered() -> None:
    """Equal lifetimes expire the whole set on one render, which puts the
    fourteen minutes back in a single go instead of spreading it."""

    lifetimes = list(C.SECTION_CACHE_TTL_SEC.values())
    assert len(set(lifetimes)) == len(lifetimes)


def test_the_cache_file_says_when_and_from_what_it_was_built(
    cache_dir,
) -> None:
    """The footer prints this; without it a cache that stops refreshing is
    indistinguishable from a number that stopped moving."""

    calls: list[str] = []
    C._cached_section("probe", _probe(calls), None, "first")
    blob = json.loads((cache_dir / "probe.json").read_text(encoding="utf-8"))
    assert blob["schema"] == "dashboard_section_cache_v1"
    assert blob["section"] == "probe"
    assert blob["computed_at_utc"].endswith("Z")
    assert len(blob["fingerprint"]) == 64


def test_a_changed_salt_discards_the_cached_value(cache_dir) -> None:
    """What a collector READS is part of what its cache was computed from.

    The fingerprint can only find the collector's own source, so a band
    grid, threshold or window that lives at module level is invisible to
    it: edit one and the file on disk still matches its fingerprint, and
    the page keeps serving the retired shape until the TTL runs out.  The
    truth-landing grid went from twelve bands to eleven on 2026-09-11 with
    a 150-minute lifetime behind it; callers declare what they read so the
    edit expires the file instead of waiting it out.
    """

    calls: list[str] = []
    collector = _probe(calls)
    C._cached_section("probe", collector, None, "first", _salt="twelve-bands")
    C._cached_section("probe", collector, None, "second", _salt="twelve-bands")
    assert calls == ["first"], "an unchanged salt should still be a hit"
    C._cached_section("probe", collector, None, "third", _salt="eleven-bands")
    assert calls == ["first", "third"]


def test_the_salt_does_not_reach_the_collector(cache_dir) -> None:
    """It is the cache's business, not the section's.

    ``_cached_section`` forwards **kwargs straight through, so a salt that
    leaked into that forwarding would raise on every collector that does
    not happen to take one -- silently turning the section uncacheable at
    best and the render red at worst.
    """

    seen: list[tuple] = []

    def strict(conn, tag):          # no **kwargs on purpose
        seen.append((conn, tag))
        return {"n": len(seen)}

    C._cached_section("probe", strict, None, "only", _salt="anything")
    assert seen == [(None, "only")]
