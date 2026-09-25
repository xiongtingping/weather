"""Charter 225 (八): the daily repair pass of the settlement reconciliation.

User ruling 2026-09-25, 「加一条常规车道：每天把新结算的市场和真值对一遍，不一致
就按结算源自动修补」.  The three-hourly reconciliation stays a reader; the daily
``--repair`` pass captures each disagreeing day's settlement page outside any
lock and re-settles it through ``settlement_source_truth``, one short write a
day, bounded, failing a day closed rather than guessing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.settlement_truth_reconciliation as recon
from bayes_basket_target import settlement_source_capture as cap
from bayes_basket_target.contracts import ContractError
from tests.test_charter_225_settlement_source_truth import (
    DAY,
    STATION,
    ZONE,
    _awc_truth,
    _ssot,
    _wu_html,
)


def _markets(winner: str, labels: tuple[str, ...]) -> list[dict]:
    return [
        {"groupItemTitle": label, "umaResolutionStatus": "resolved",
         "outcomePrices": json.dumps(["1", "0"] if label == winner else ["0", "1"])}
        for label in labels
    ]


def _noaa_event(*, winner: str, unit: str = "Celsius", labels=("29°C", "30°C", "31°C")) -> dict:
    return {
        "slug": "highest-temperature-in-munich-on-august-1-2026",
        "closed": True,
        "closedTime": "2026-08-02 12:00:00+00",
        "resolutionSource": "https://www.weather.gov/wrh/timeseries?site=EDDM",
        "description": (
            "This market will resolve to the temperature range that contains the highest "
            f"temperature recorded at the Munich Airport Station in degrees {unit} on 1 Aug '26.\n\n"
            "The resolution source for this market will be information from NOAA, available here: "
            "https://www.weather.gov/wrh/timeseries?site=EDDM."
        ),
        "markets": _markets(winner, labels),
    }


def test_a_noaa_day_is_evidenced_by_the_market_resolution() -> None:
    from bayes_basket_target import settlement_source_truth as sst

    event = _noaa_event(winner="30°C")
    parsed = sst.read_market_resolution(
        {**event, "markets": event["markets"]}, slug=event["slug"], event_date=DAY,
        paid_bucket="30°C",
    )
    assert parsed["value"] is None and parsed["identity"]["winner"] == "30°C"
    # A one-degree bucket is exact; a 2°F range or an open edge is not.
    assert sst.decide(paid_bucket="30°C", unit="C", source_value=None) == (
        sst.BASIS_RESOLUTION_DEGREE, 30.0)
    with pytest.raises(ContractError, match="value_undetermined"):
        sst.decide(paid_bucket="88-89°F", unit="F", source_value=None)
    with pytest.raises(ContractError, match="winner_mismatch"):
        sst.read_market_resolution(event, slug=event["slug"], event_date=DAY, paid_bucket="31°C")
    with pytest.raises(ContractError, match="identity_mismatch"):
        sst.read_market_resolution(event, slug=event["slug"], event_date="2026-08-02",
                                   paid_bucket="30°C")


def test_no_script_reads_the_noaa_page() -> None:
    """Its series is served through a token only weather.gov's origin may use."""

    with pytest.raises(ContractError, match="origin_locked"):
        cap.capture(object(), market={"family": "NOAA_WRH_TIMESERIES", "unit": "C"},
                    station_id="ZBAA", event_date="2026-09-20", captures=Path("/nonexistent"))


class _Response:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.status_code, self._body = status, body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self):
        yield self._body


class _Client:
    def __init__(self, routes: dict[str, bytes]) -> None:
        self.routes, self.urls = routes, []

    def stream(self, method: str, url: str, headers=None):
        self.urls.append((url, dict(headers or {})))
        for prefix, body in self.routes.items():
            if url.startswith(prefix):
                return _Response(body)
        return _Response(b"", status=404)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _event(**change) -> dict:
    event = {
        "slug": "highest-temperature-in-munich-on-august-1-2026",
        "closed": True,
        "resolutionSource": "https://www.wunderground.com/history/daily/de/munich/EDDM",
        "description": (
            "This market will resolve to the temperature range that contains the highest "
            "temperature recorded at the Munich Airport Station in degrees Celsius on 1 Aug '26.\n\n"
            "The resolution source for this market will be information from Wunderground, "
            "specifically the highest temperature recorded for all times on this day for the "
            "Munich Airport Station, available here: "
            "https://www.wunderground.com/history/daily/de/munich/EDDM."
        ),
    }
    event.update(change)
    return event


def _row(**change) -> dict:
    row = {"station_id": STATION, "event_date": DAY, "slug": "highest-temperature-in-munich-on-august-1-2026",
           "verdict": "OUR_EVIDENCE_GAP", "our_bucket": "20°C", "market_bucket": "30°C",
           "market_state": "RESOLVED"}
    row.update(change)
    return row


def _db(tmp_path: Path) -> None:
    """The production schemas, one standing AWC truth of 20, and the station's
    bound timezone -- the registry has retired the day, as it has every day
    older than three."""

    from bayes_basket_target.market_registry import apply_target_market_registry_schema

    conn = _ssot(tmp_path)
    apply_target_market_registry_schema(conn)
    _awc_truth(conn, value=20.0)
    conn.execute(
        "INSERT INTO calibration_station_market_timezone_binding(station_id,settlement_timezone,"
        "market_registry_timezone_count,binding_version,bound_at_utc,content_sha256)"
        " VALUES(?,?,1,'test','2026-08-01T00:00:00Z',?)", (STATION, ZONE, "e" * 64),
    )
    conn.close()


def test_a_disagreement_is_captured_and_re_settled_once(tmp_path: Path) -> None:
    _db(tmp_path)
    client = _Client({"https://www.wunderground.com/history/daily/de/munich/EDDM/date/2026-8-1":
                      _wu_html().encode()})
    event = _event()
    results = recon.repair([_row()], {event["slug"]: event}, db=tmp_path / "ssot.db",
                           captures=tmp_path / "captures", max_repairs=5, client=client)
    assert [r["state"] for r in results] == ["repaired"]
    assert results[0]["official_tmax_c"] == 30.0 and results[0]["revision_no"] == 1
    assert client.urls[0][1]["Cookie"] == "wu_units=m"
    again = recon.repair([_row()], {event["slug"]: event}, db=tmp_path / "ssot.db",
                         captures=tmp_path / "captures", max_repairs=5, client=client)
    assert [r["state"] for r in again] == ["settlement_source_truth_stands"]


def test_a_day_the_page_cannot_settle_is_listed_not_guessed(tmp_path: Path) -> None:
    _db(tmp_path)
    event = _event()
    # The page is down: refused, nothing written.
    down = recon.repair([_row()], {event["slug"]: event}, db=tmp_path / "ssot.db",
                        captures=tmp_path / "captures", max_repairs=5, client=_Client({}))
    assert down[0]["state"] == "refused" and "http_404" in down[0]["reason"]
    # An open edge the page does not support: refused as undetermined.
    edge = recon.repair([_row(market_bucket="23°C or below", verdict="MARKET_LOWEST_BRACKET")],
                        {event["slug"]: event}, db=tmp_path / "ssot.db",
                        captures=tmp_path / "captures", max_repairs=5,
                        client=_Client({"https://www.wunderground.com/": _wu_html().encode()}))
    assert edge[0]["state"] == "refused" and "value_undetermined" in edge[0]["reason"]
    # Over the cap, the rest wait for the next pass.
    capped = recon.repair([_row(), _row(event_date="2026-08-02")], {event["slug"]: event},
                          db=tmp_path / "ssot.db", captures=tmp_path / "captures",
                          max_repairs=0, client=_Client({}))
    assert {r["state"] for r in capped} == {"deferred_repair_cap"}


def test_a_noaa_disagreement_is_re_settled_from_the_resolution(tmp_path: Path) -> None:
    _db(tmp_path)
    event = _noaa_event(winner="30°C")
    row = _row(verdict="MARKET_ABOVE")
    results = recon.repair([row], {event["slug"]: event}, db=tmp_path / "ssot.db",
                           captures=tmp_path / "captures", max_repairs=5, client=_Client({}))
    assert [r["state"] for r in results] == ["repaired"]
    assert results[0]["basis"] == "PAID_WHOLE_DEGREE_FROM_RESOLUTION"
    assert results[0]["official_tmax_c"] == 30.0
    assert (tmp_path / "captures" / STATION / DAY / "market_resolution.json").exists()


def test_a_noaa_range_bucket_is_listed_not_guessed(tmp_path: Path) -> None:
    _db(tmp_path)
    event = _noaa_event(winner="86-87°F", unit="Fahrenheit", labels=("84-85°F", "86-87°F"))
    row = _row(verdict="MARKET_ABOVE", market_bucket="86-87°F", our_bucket="68-69°F")
    results = recon.repair([row], {event["slug"]: event}, db=tmp_path / "ssot.db",
                           captures=tmp_path / "captures", max_repairs=5, client=_Client({}))
    assert results[0]["state"] == "refused" and "value_undetermined" in results[0]["reason"]


def test_the_three_hourly_pass_stays_a_reader(tmp_path: Path, monkeypatch) -> None:
    """Without --repair the pass never reaches the repair (no SSOT write)."""

    monkeypatch.setattr(recon, "_events", lambda start, end: [])
    monkeypatch.setattr(recon, "load_config",
                        lambda: {"settlement_source_contract": {"cities": {}}})
    monkeypatch.setattr(recon, "_reconcile", lambda db, start, events, cities: [])
    calls = []
    monkeypatch.setattr(recon, "repair", lambda *a, **k: calls.append(1) or [])
    paths = dict(db=tmp_path / "x.db", status_path=tmp_path / "s.json",
                 history_path=tmp_path / "h.jsonl",
                 repair_status_path=tmp_path / "rs.json",
                 repair_history_path=tmp_path / "rh.jsonl")
    status = recon.run(days=3, dry_run=False, **paths)
    assert calls == [] and status["writes_ssot"] is False and "repair" not in status
    status = recon.run(days=3, dry_run=False, repair_pass=True,
                       client_factory=lambda: _Client({}), **paths)
    assert calls == [1] and status["writes_ssot"] is True
    assert json.loads((tmp_path / "rs.json").read_text())["attempted"] == 0
