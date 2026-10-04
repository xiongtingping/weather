"""After a refresh renews nothing, the next one waits for the publisher's next books.

2026-10-04 15:51:41-45Z: four refreshes in four seconds against one unchanged
publisher manifest spent the per-day cap of six, and the 19 scopes behind
them died together on production_quote_expired (15 more the same way at
15:43:47Z).  A refresh can only renew with books newer than the ones it
holds, and those arrive with the publisher's next full resubscription.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from bayes_basket_target import production_worker as pw

ROOT = Path(__file__).resolve().parents[1]


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _manifest(*stamps: datetime) -> dict:
    return {
        "quotes_by_token": {
            f"t{index}": {"book_observed_at": _iso(stamp)}
            for index, stamp in enumerate(stamps)
        }
    }


def test_the_floor_is_the_oldest_book_and_none_without_a_clock() -> None:
    now = datetime.now(timezone.utc)
    old = now - timedelta(seconds=40)
    assert pw.publisher_book_floor_v1(_manifest(now, old, now)) == old
    assert pw.publisher_book_floor_v1({"quotes_by_token": {}}) is None
    assert pw.publisher_book_floor_v1({}) is None
    assert pw.publisher_book_floor_v1(
        {"quotes_by_token": {"a": {"book_observed_at": _iso(now)}, "b": {}}}
    ) is None


class _Publisher:
    """A manifest file plus the catalog the worker would read from it."""

    def __init__(self, tmp_path: Path, manifest: dict | None) -> None:
        self.path = tmp_path / "manifest.json"
        self.manifest = manifest
        self.reads = 0
        self._write()

    def _write(self) -> None:
        self.path.write_text(str(self.reads), encoding="utf-8")
        stat = self.path.stat()
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    def publish(self, manifest: dict) -> None:
        self.manifest = manifest
        self.reads += 1
        self._write()

    def catalog(self) -> list[dict]:
        if self.manifest is None:
            return [{"station_id": "EGLC", "event_date": "2026-10-04", "manifest": {}}]
        return [{"station_id": "KLGA", "event_date": "2026-10-04", "manifest": self.manifest}]


def _worker(publisher: _Publisher) -> SimpleNamespace:
    worker = SimpleNamespace(
        market_manifest_path=publisher.path,
        stop_requested=False,
        _resident_ws_scope_catalog=publisher.catalog,
    )
    worker._await = pw.TargetProductionWorker._await_publisher_books.__get__(worker)
    return worker


def test_fresh_books_end_the_wait_at_once(tmp_path: Path) -> None:
    publisher = _Publisher(tmp_path, _manifest(datetime.now(timezone.utc)))
    waited, arrived = _worker(publisher)._await(
        station_id="KLGA", event_date="2026-10-04", lease_seconds=60.0, budget_seconds=30.0,
    )
    assert arrived is True and waited < 1.0


def test_the_wait_ends_when_the_next_generation_lands(tmp_path: Path, monkeypatch) -> None:
    stale = datetime.now(timezone.utc) - timedelta(seconds=55)
    publisher = _Publisher(tmp_path, _manifest(stale))
    sleeps: list[float] = []

    def _sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            publisher.publish(_manifest(datetime.now(timezone.utc)))

    monkeypatch.setattr(pw.time, "sleep", _sleep)
    waited, arrived = _worker(publisher)._await(
        station_id="KLGA", event_date="2026-10-04", lease_seconds=60.0, budget_seconds=30.0,
    )
    assert arrived is True
    assert len(sleeps) == 3


def test_a_silent_publisher_is_waited_on_only_up_to_the_budget(tmp_path: Path, monkeypatch) -> None:
    publisher = _Publisher(tmp_path, _manifest(datetime.now(timezone.utc) - timedelta(seconds=55)))
    clock = [1000.0]
    monkeypatch.setattr(pw.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(pw.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    waited, arrived = _worker(publisher)._await(
        station_id="KLGA", event_date="2026-10-04", lease_seconds=60.0, budget_seconds=5.0,
    )
    assert arrived is False and waited == 5.0


def test_a_scope_the_publisher_does_not_carry_is_not_waited_on(tmp_path: Path, monkeypatch) -> None:
    publisher = _Publisher(tmp_path, None)
    monkeypatch.setattr(pw.time, "sleep", lambda seconds: (_ for _ in ()).throw(AssertionError("slept")))
    waited, arrived = _worker(publisher)._await(
        station_id="KLGA", event_date="2026-10-04", lease_seconds=60.0, budget_seconds=30.0,
    )
    assert arrived is False and waited < 1.0


def test_the_loop_waits_only_after_a_refresh_that_renewed_nothing() -> None:
    source = (ROOT / "bayes_basket_target" / "production_worker.py").read_text(encoding="utf-8")
    loop = source[source.index("for position, (station_id, event_date) in enumerate(rows):"):]
    loop = loop[:loop.index('self._mark("decision_loop")')]
    wait = loop.index("self._await_publisher_books(")
    assert loop.index("and day in days_awaiting_publisher") < wait
    assert wait < loop.index("self._refresh_scope_day_evidence(")
    assert "days_awaiting_publisher.add(day)" in loop
    assert "days_awaiting_publisher.discard(day)" in loop
    receipt = source.split('"quote_evidence_refresh": {', 1)[1].split('"receipts":', 1)[0]
    for field in ('"publisher_waits"', '"publisher_wait_seconds"', '"await_publisher"'):
        assert field in receipt
    assert pw.QUOTE_REFRESH_AWAIT_PUBLISHER is True
    assert pw.QUOTE_PUBLISHER_WAIT_BUDGET_SECONDS == 60.0
