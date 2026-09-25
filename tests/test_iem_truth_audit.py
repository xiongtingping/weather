"""The daily IEM truth audit: rules, day boundaries, verdicts, history."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from bayes_basket_target import iem_truth_audit as audit_mod
from bayes_basket_target.contracts import ContractError

UTC = timezone.utc
SHANGHAI = "Asia/Shanghai"


def _report(hour_utc: datetime, temp: str, remark: str = "") -> dict:
    stamp = hour_utc.strftime("%d%H%MZ")
    raw = f"ZUUU {stamp} VRB01MPS 9999 FEW030 {temp} Q1012 NOSIG"
    if remark:
        raw += f" RMK {remark}"
    return {"valid_at_utc": hour_utc, "raw": raw}


def _full_day(local_date: str, peak: str, *, skip_hours: tuple[int, ...] = ()) -> list[dict]:
    """Hourly reports across one Shanghai local day, the peak at 15L."""

    start = datetime.fromisoformat(local_date).replace(tzinfo=UTC) - timedelta(hours=8)
    reports = []
    for hour in range(24):
        if hour in skip_hours:
            continue
        temp = peak if hour == 15 else "20/18"
        reports.append(_report(start + timedelta(hours=hour), temp))
    return reports


def test_iem_names_us_asos_stations_without_the_k() -> None:
    assert audit_mod.iem_station_id("KATL") == "ATL"
    assert audit_mod.iem_station_id("ZUUU") == "ZUUU"
    assert audit_mod.iem_station_id("CYYZ") == "CYYZ"


def test_the_csv_is_read_as_timed_raw_reports() -> None:
    text = "station,valid,metar\nZUUU,2026-09-23 00:00,ZUUU 230000Z VRB01MPS 21/21 Q1017\n"
    reports = audit_mod.parse_iem_csv(text)
    assert reports == [{
        "valid_at_utc": datetime(2026, 9, 23, tzinfo=UTC),
        "raw": "ZUUU 230000Z VRB01MPS 21/21 Q1017",
    }]
    assert audit_mod.parse_iem_csv("") == []
    with pytest.raises(ContractError, match="response_not_csv"):
        audit_mod.parse_iem_csv("<html>busy</html>")


def test_temperatures_are_read_the_way_settlement_reads_them() -> None:
    assert audit_mod.report_temperature_c("ZUUU 250600Z 22/21 Q1012") == 22.0
    assert audit_mod.report_temperature_c("ZBAA 250600Z M02/M05 Q1030") == -2.0
    # The remark's tenths win when they agree with the body group (31.7
    # rounds to the body's 32); a remark that contradicts it is not believed.
    assert audit_mod.report_temperature_c(
        "KATL 092153Z 32/22 A2990 RMK AO2 T03170222"
    ) == pytest.approx(31.7)
    assert audit_mod.report_temperature_c(
        "KATL 092153Z 30/22 A2990 RMK AO2 T03170222"
    ) == 30.0
    assert audit_mod.report_temperature_c("ZUUU 250600Z VRB01MPS 9999 Q1012") is None


def test_the_local_day_is_an_instant_window() -> None:
    day = "2026-09-25"
    start = datetime(2026, 9, 24, 16, tzinfo=UTC)   # 00:00 Shanghai
    reports = [
        _report(start, "30/20"),                     # this day's midnight: in
        _report(start + timedelta(hours=24), "35/20"),  # next midnight: out
        *(_report(start + timedelta(hours=h), "25/20") for h in range(1, 24)),
    ]
    summary = audit_mod.iem_day_summary(reports, event_date=day, settlement_timezone=SHANGHAI)
    assert summary["max_c"] == 30.0
    assert summary["report_count"] == 24
    assert summary["warm_window_gap"]["complete"] is True


def test_verdicts() -> None:
    complete = {"complete": True}
    assert audit_mod.classify(27.0, {"max_c": 27.0, "warm_window_gap": complete}) == "AGREE"
    assert audit_mod.classify(27.0, {"max_c": 28.0, "warm_window_gap": complete}) == "IEM_ABOVE"
    assert audit_mod.classify(31.7, {"max_c": 31.1, "warm_window_gap": complete}) == "IEM_BELOW"
    assert audit_mod.classify(31.7, {"max_c": 31.4, "warm_window_gap": complete}) == "AGREE"
    assert audit_mod.classify(27.0, {"max_c": 30.0, "warm_window_gap": {"complete": False}}) == "IEM_INCOMPLETE"
    assert audit_mod.classify(27.0, {"max_c": None, "warm_window_gap": None}) == "NO_IEM"


def test_the_audit_flags_the_day_the_truth_missed() -> None:
    truths = {
        ("ZUUU", "2026-09-24"): {"station_id": "ZUUU", "event_date": "2026-09-24", "truth_c": 23.0,
                                 "revision_no": 0, "source_lane": "AWC", "settlement_timezone": SHANGHAI},
        ("ZUUU", "2026-09-25"): {"station_id": "ZUUU", "event_date": "2026-09-25", "truth_c": 26.0,
                                 "revision_no": 0, "source_lane": "AWC", "settlement_timezone": SHANGHAI},
        ("ZUCK", "2026-09-25"): {"station_id": "ZUCK", "event_date": "2026-09-25", "truth_c": 30.0,
                                 "revision_no": 1, "source_lane": "AWC", "settlement_timezone": SHANGHAI},
    }
    asked = []

    def fetch(station, first, last):
        asked.append((station, first, last))
        if station == "ZUCK":
            raise OSError("proxy refused")
        return _full_day("2026-09-24", "23/20") + _full_day("2026-09-25", "28/20")

    rows = {(r["station_id"], r["event_date"]): r for r in audit_mod.audit(truths, fetch=fetch)}
    assert asked == [
        ("ZUCK", date(2026, 9, 24), date(2026, 9, 26)),
        ("ZUUU", date(2026, 9, 23), date(2026, 9, 26)),
    ]
    assert rows[("ZUUU", "2026-09-24")]["verdict"] == "AGREE"
    assert rows[("ZUUU", "2026-09-25")]["verdict"] == "IEM_ABOVE"
    assert rows[("ZUUU", "2026-09-25")]["diff_c"] == 2.0
    assert rows[("ZUCK", "2026-09-25")]["verdict"] == "IEM_FETCH_FAILED"
    assert rows[("ZUCK", "2026-09-25")]["fetch_error"].startswith("OSError")
    summary = audit_mod.summarize(list(rows.values()))
    assert summary["counts"] == {"AGREE": 1, "IEM_ABOVE": 1, "IEM_FETCH_FAILED": 1}
    assert summary["agree_share"] == 0.5
    assert [(r["station_id"], r["event_date"]) for r in summary["disagreements"]] == [("ZUUU", "2026-09-25")]
    assert list(summary["fetch_failed_stations"]) == ["ZUCK"]


def test_an_afternoon_iem_did_not_carry_is_no_judgement() -> None:
    truths = {("ZUUU", "2026-09-25"): {"station_id": "ZUUU", "event_date": "2026-09-25", "truth_c": 26.0,
                                        "revision_no": 0, "source_lane": "AWC", "settlement_timezone": SHANGHAI}}
    rows = audit_mod.audit(
        truths, fetch=lambda *a: _full_day("2026-09-25", "22/20", skip_hours=tuple(range(11, 18)))
    )
    assert rows[0]["verdict"] == "IEM_INCOMPLETE"


def test_the_standing_truth_is_the_latest_accepted_revision() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_station_daily_high_truth(station_id TEXT, local_date TEXT, "
        "official_tmax_c REAL, revision_no INTEGER, source_lane TEXT, settlement_timezone TEXT, "
        "qc_status TEXT)"
    )
    conn.executemany(
        "INSERT INTO calibration_station_daily_high_truth VALUES(?,?,?,?,?,?,?)",
        [
            ("KATL", "2026-09-09", 30.6, 0, "AWC", "America/New_York", "ACCEPTED"),
            ("KATL", "2026-09-09", 31.7, 1, "AWC", "America/New_York", "ACCEPTED"),
            ("KATL", "2026-09-10", 29.0, 0, "AWC", "America/New_York", "QUARANTINED"),
        ],
    )
    truths = audit_mod.standing_truths(conn, first_date="2026-09-01", last_date="2026-09-30")
    assert list(truths) == [("KATL", "2026-09-09")]
    assert truths[("KATL", "2026-09-09")]["truth_c"] == 31.7
    assert truths[("KATL", "2026-09-09")]["revision_no"] == 1


def test_each_disagreement_enters_the_history_once(tmp_path) -> None:
    import scripts.iem_truth_audit as script

    row = {"station_id": "ZUUU", "event_date": "2026-09-25", "verdict": "IEM_ABOVE",
           "truth_c": 26.0, "revision_no": 0, "iem_max_c": 28.0}
    path = tmp_path / "history.jsonl"
    assert script._append_history(path, [row], checked_at="2026-09-25T10:30:00Z") == 1
    assert script._append_history(path, [row], checked_at="2026-09-26T10:30:00Z") == 0
    revised = {**row, "truth_c": 28.0, "revision_no": 1, "verdict": "AGREE"}
    assert script._append_history(path, [revised], checked_at="2026-09-27T10:30:00Z") == 1
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [line["first_seen_at"] for line in lines] == ["2026-09-25T10:30:00Z", "2026-09-27T10:30:00Z"]


class _Response:
    def __init__(self, status: int, body: bytes = b"", retry_after: str = "") -> None:
        self.status_code = status
        self.headers = {"Retry-After": retry_after} if retry_after else {}
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self):
        yield self._body


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def stream(self, method, url):
        self.urls.append(url)
        return self.responses.pop(0)


def test_a_throttled_request_waits_and_asks_again() -> None:
    body = b"station,valid,metar\nZUUU,2026-09-25 06:00,ZUUU 250600Z 27/20 Q1010\n"
    waits = []
    client = _Client([_Response(429, retry_after="7"), _Response(429), _Response(200, body)])
    reports = audit_mod.fetch_iem_reports(
        client, iem_id="ZUUU", first_utc_date=date(2026, 9, 24), last_utc_date=date(2026, 9, 26),
        sleep=waits.append,
    )
    assert waits == [7.0, 40.0]
    assert len(reports) == 1 and len(client.urls) == 3
    always = _Client([_Response(429) for _ in range(audit_mod.THROTTLE_ATTEMPTS)])
    with pytest.raises(ContractError, match="throttled|http_429"):
        audit_mod.fetch_iem_reports(
            always, iem_id="ZUUU", first_utc_date=date(2026, 9, 24), last_utc_date=date(2026, 9, 26),
            sleep=waits.append,
        )
