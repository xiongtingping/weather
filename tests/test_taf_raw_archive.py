"""「存原文」(2026-09-19): every TAF the resident sees is kept whole, once.

The forward capture keeps four numbers and a hash; the text lived only in a
six-day inbox, so the test of the TAF's unused physics ran out of days.  The
archive appends AWC's whole row (raw text and decoded groups) to a daily
file outside the SSOT, once per bulletin, and can never fail a capture.
"""
from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import taf_raw_archive as A  # noqa: E402

WHEN = datetime(2026, 9, 19, 11, 30, tzinfo=timezone.utc)


def _row(station: str = "ZUUU", issued: str = "2026-09-19T09:02:00.000Z", raw: str | None = None) -> dict:
    return {
        "icaoId": station,
        "issueTime": issued,
        "dbPopTime": "2026-09-19T09:05:11.000Z",
        "rawTAF": raw or f"TAF {station} 190902Z 1912/2012 02003MPS 6000 SCT040 TX25/2007Z TN20/2022Z",
        "fcsts": [{"timeFrom": 1789819200, "timeTo": 1789905600, "wdir": 20, "wspd": 6,
                   "visib": "6+", "clouds": [{"cover": "SCT", "base": 4000, "type": None}]}],
    }


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setenv(A.ENV, str(tmp_path))
    monkeypatch.setattr(A, "_SEEN", set())
    monkeypatch.setattr(A, "_SEEDED", set())
    return tmp_path


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_each_bulletin_is_written_once_with_the_whole_awc_row(archive) -> None:
    rows = [_row("ZUUU"), _row("RKPK"), _row("ZUUU")]
    assert A.archive_rows(rows, received_at=WHEN, source_url="u", response_sha256="s") == 2
    # The next round returns the same bulletins: nothing new is written.
    assert A.archive_rows(rows, received_at=WHEN, source_url="u") == 0
    lines = _lines(archive / "2026-09-19.jsonl")
    assert [l["station_id"] for l in lines] == ["ZUUU", "RKPK"]
    assert lines[0]["awc_row"]["fcsts"][0]["wdir"] == 20
    assert lines[0]["raw_taf"].startswith("TAF ZUUU")
    assert lines[0]["schema"] == A.SCHEMA and lines[0]["response_sha256"] == "s"
    # An amendment is a new bulletin.
    amd = _row("ZUUU", "2026-09-19T11:10:00.000Z", "TAF AMD ZUUU 191110Z 1912/2012 36004MPS 3000 -SHRA BKN020 TX23/2007Z")
    assert A.archive_rows([amd], received_at=WHEN, source_url="u") == 1


def test_a_restart_reads_back_what_is_already_on_disk(archive, monkeypatch) -> None:
    A.archive_rows([_row()], received_at=WHEN, source_url="u")
    monkeypatch.setattr(A, "_SEEN", set())
    monkeypatch.setattr(A, "_SEEDED", set())
    assert A.archive_rows([_row()], received_at=WHEN, source_url="u") == 0
    # Yesterday's file seeds too: a bulletin current across midnight is not re-written.
    monkeypatch.setattr(A, "_SEEN", set())
    monkeypatch.setattr(A, "_SEEDED", set())
    tomorrow = datetime(2026, 9, 20, 0, 10, tzinfo=timezone.utc)
    assert A.archive_rows([_row()], received_at=tomorrow, source_url="u") == 0
    assert not (archive / "2026-09-20.jsonl").exists()


def test_archiving_never_raises_and_retries_a_failed_write(archive, monkeypatch) -> None:
    target = archive / "2026-09-19.jsonl"
    target.mkdir()                              # a directory where the file should be
    before = A.STATS["errors"]
    assert A.archive_rows([_row()], received_at=WHEN, source_url="u") == 0
    assert A.STATS["errors"] == before + 1
    target.rmdir()
    monkeypatch.setattr(A, "_SEEDED", set())
    assert A.archive_rows([_row()], received_at=WHEN, source_url="u") == 1
    assert A.archive_rows([None, {"icaoId": "X"}, "junk"], received_at=WHEN, source_url="u") == 0


def test_off_disables_it(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(A.ENV, "off")
    assert A.archive_dir() is None
    assert A.archive_rows([_row()], received_at=WHEN, source_url="u") == 0
    monkeypatch.delenv(A.ENV)
    assert A.archive_dir() == A.DEFAULT_DIR


def test_the_suite_never_writes_the_real_archive() -> None:
    assert A.archive_dir() is None


def test_the_bulk_probe_and_the_station_capture_both_archive(archive, monkeypatch) -> None:
    rev = importlib.import_module("scripts.capture_bayes_basket_target_awc_taf_revision")
    rows = [_row("LTAC", "2026-09-19T10:40:00.000Z"), _row("ZUUU")]
    newer, receipt = rev.awc_newer_taf_stations(
        ["LTAC", "ZUUU"], held_issued_at={}, fetch=lambda url, timeout_sec: (rows, "sha", 10),
    )
    assert receipt["archived"] == 2 and set(newer) == {"LTAC", "ZUUU"}
    # The per-station capture archives what it fetched, even a row its parse refuses.
    seen = []
    monkeypatch.setattr(rev, "_fetch_json", lambda url, timeout_sec: (seen.append(url) or [_row("RKPK")], "sha2", 10))
    try:
        rev.capture_forward_awc_taf(station_id="RKPK", event_date="2026-09-25", settlement_timezone="Asia/Seoul")
    except Exception:  # noqa: BLE001 -- a bulletin for another day; the archive keeps it anyway
        pass
    stations = [l["station_id"] for l in _lines(archive / f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl")]
    assert "RKPK" in stations and seen
