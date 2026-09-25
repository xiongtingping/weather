"""Charter 219: a scope trains on the runs live seats on it.

A same-day run issued twelve hours or more into the station's local day lands
in ``h00_12`` beside the previous day's run of the same cycle.  Live seats only
the previous day's there (``scope_trained_by_longer_lead``), but the OOS
reader kept whichever case it met first -- for backfilled AIFS ENS and AIGEFS
history, the same-day run, 58-59 of 60 days on every station checked.  The
reader now drops exactly the runs live can never seat on their own scope.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import datetime

import pytest

from bayes_basket_target import follow_run
from bayes_basket_target import native_ensemble_oos as oos

ECMWF_IFS_SYSTEM_ID = follow_run.ECMWF_SYSTEM_ID
DWD_ICON_EPS_SYSTEM_ID = follow_run.ICON_SYSTEM_ID
ECCC_GEPS_SYSTEM_ID = follow_run.ECCC_SYSTEM_ID
AIFS = follow_run.system_id(follow_run.AIFS_FAMILY)
AIGEFS = follow_run.system_id(follow_run.AIGEFS_FAMILY)


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _never(system: str, station_tz: str, event_date: str, run: str) -> bool:
    return follow_run.live_never_seats(
        system,
        event_date=event_date,
        settlement_timezone=station_tz,
        run_init=_utc(run),
    )


@pytest.mark.parametrize("system", [AIFS, AIGEFS])
def test_the_same_day_evening_run_is_never_seated_and_its_eve_is(system):
    # EGLC 2026-09-24 starts 23Z on the 23rd (BST).  The 18Z run of the 24th
    # is issued 19 hours in; the 18Z run of the 23rd, 5 hours before.
    assert _never(system, "Europe/London", "2026-09-24", "2026-09-24T18:00:00Z")
    assert not _never(system, "Europe/London", "2026-09-24", "2026-09-23T18:00:00Z")


@pytest.mark.parametrize("system", [AIFS, AIGEFS])
def test_zuuu_cycles(system):
    # ZUUU 2026-09-25 starts 16Z on the 24th.  00Z of the 25th is 08:00
    # local: its previous-day twin is 16h ahead, another bucket, so it keeps
    # its own scope.  06Z (14:00) and 12Z (20:00) of the 25th share h00_12
    # with the 24th's runs of the same cycle, which live seats instead.
    tz, day = "Asia/Shanghai", "2026-09-25"
    assert not _never(system, tz, day, "2026-09-25T00:00:00Z")
    assert _never(system, tz, day, "2026-09-25T06:00:00Z")
    assert _never(system, tz, day, "2026-09-25T12:00:00Z")
    assert not _never(system, tz, day, "2026-09-24T12:00:00Z")
    assert not _never(system, tz, day, "2026-09-24T18:00:00Z")


@pytest.mark.parametrize("system", [
    ECMWF_IFS_SYSTEM_ID, DWD_ICON_EPS_SYSTEM_ID, ECCC_GEPS_SYSTEM_ID,
    "NOAA_GEFS_05", "NOT_A_SYSTEM",
])
def test_families_with_a_ladder_rung_are_never_ruled_out(system):
    # KLGA 2026-09-24's 18Z run is 14:00 local: a ladder can still seat it
    # as its base, so training keeps it.
    assert not _never(system, "America/New_York", "2026-09-24", "2026-09-24T18:00:00Z")


class _Untouchable:
    def __getattr__(self, name):  # pragma: no cover - reached only on failure
        raise AssertionError(f"the connection was read ({name})")


@pytest.mark.parametrize("family", [follow_run.AIFS_FAMILY, follow_run.AIGEFS_FAMILY])
def test_live_turns_away_exactly_what_training_drops(family):
    run = _utc("2026-09-24T18:00:00Z")
    assert follow_run.live_never_seats(
        follow_run.system_id(family),
        event_date="2026-09-24", settlement_timezone="Europe/London", run_init=run,
    )
    # Refused before any certificate is read.
    assert follow_run.follow_serving_ready(
        _Untouchable(), family=family, station_id="EGLC",
        event_date="2026-09-24", settlement_timezone="Europe/London",
        run_init=run, as_of_utc="2026-09-25T02:00:00Z",
    ) is False


def test_the_oos_reader_drops_what_live_never_seats():
    tree = ast.parse(textwrap.dedent(inspect.getsource(oos._load_cases)))
    guards = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Call)
        and getattr(node.test.func, "id", None) == "live_never_seats"
    ]
    assert len(guards) == 1
    guard = guards[0]
    assert any(isinstance(stmt, ast.Continue) for stmt in guard.body)
    keywords = {kw.arg: ast.unparse(kw.value) for kw in guard.test.keywords}
    assert keywords == {
        "event_date": "day",
        "settlement_timezone": "timezone_name",
        "run_init": "run_init",
    }
    assert [ast.unparse(arg) for arg in guard.test.args] == ["system_id"]
    assert oos.live_never_seats is follow_run.live_never_seats
