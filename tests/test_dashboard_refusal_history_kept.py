"""The refusal history reads only the days it has not summarised yet.

2026-10-17 (user 「拒单历史 6470 分钟前（超期）」): the window's end moves
daily, so the whole history since 08-31 was re-read every day (460 s by
10-13) and lost every read to the writer from then on.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402


def _fake_reads(monkeypatch):
    reads: list[tuple[str, str]] = []

    def closed(conn, first_day, through):
        reads.append((first_day, through))
        day = first_day
        days = {}
        while day <= through:
            days[f"KLGA|{day}"] = [False, "[]"]
            day = C._next_iso_day(day)
        return {"first_day": first_day, "through": through, "days": days}

    monkeypatch.setattr(C.live_selection, "closed_plan_summaries", closed)
    return reads


def test_only_new_days_are_read(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "SECTION_CACHE_DIR", tmp_path)
    reads = _fake_reads(monkeypatch)
    first = C._collect_refusal_history(None, "2026-10-01", "2026-10-05")
    assert reads == [("2026-10-01", "2026-10-05")]
    assert len(first["days"]) == 5
    second = C._collect_refusal_history(None, "2026-10-01", "2026-10-07")
    assert reads[-1] == ("2026-10-06", "2026-10-07")
    assert sorted(second["days"]) == [f"KLGA|2026-10-0{d}" for d in range(1, 8)]
    # Same window again: nothing to read.
    C._collect_refusal_history(None, "2026-10-01", "2026-10-07")
    assert len(reads) == 2


def test_an_earlier_first_day_reads_whole(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "SECTION_CACHE_DIR", tmp_path)
    reads = _fake_reads(monkeypatch)
    C._collect_refusal_history(None, "2026-10-03", "2026-10-05")
    C._collect_refusal_history(None, "2026-10-01", "2026-10-05")
    assert reads[-1] == ("2026-10-01", "2026-10-05")


def test_a_later_first_day_drops_the_days_before_it(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "SECTION_CACHE_DIR", tmp_path)
    _fake_reads(monkeypatch)
    C._collect_refusal_history(None, "2026-10-01", "2026-10-05")
    out = C._collect_refusal_history(None, "2026-10-03", "2026-10-05")
    assert sorted(out["days"]) == [f"KLGA|2026-10-0{d}" for d in (3, 4, 5)]
