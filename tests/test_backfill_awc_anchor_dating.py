"""An anchored backfill must date its reports from the anchor, not the clock.

A raw-format METAR header carries only ``DDHHMM``, so the ingress infers the
month from when the bytes were read.  That is right for the live lane and
wrong for every anchored read.  Measured 2026-09-02: fetching 2026-08-04 made
"04" the nearest 04, which was 2026-09-04, and 171 observations for LEMD, NZWN
and ZSQD landed three to five days in the future -- reaching the station-day
bounds and the canonical bounds before the mistake was visible anywhere.
"""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "backfill_awc_hourly_election_gap",
    ROOT / "scripts" / "backfill_awc_hourly_election_gap.py",
)
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)


def _report(raw_text: str) -> dict[str, str]:
    return {
        "station_id": "LEMD",
        "raw_text": raw_text,
        "source_report_time": "",
    }


def test_the_anchor_decides_the_month_a_bare_header_does_not_state() -> None:
    anchor = backfill._anchor_instant("20260804_2200")

    dated = backfill._date_reports_against_anchor(
        [
            _report("METAR LEMD 042200Z 35004KT CAVOK 28/13 Q1018"),
            _report("METAR LEMD 032200Z 24012KT CAVOK 28/12 Q1015"),
        ],
        anchor,
    )

    # Both inside the 2026-08-04 settlement window, which is what the request
    # asked for.  Read against "now" on 2026-09-02 these become 09-04 and
    # 09-03: reports for days that have not happened.
    assert [row["source_report_time"] for row in dated] == [
        "2026-08-04T22:00:00.000Z",
        "2026-08-03T22:00:00.000Z",
    ]


def test_an_anchor_across_a_month_boundary_reaches_the_previous_month() -> None:
    # The anchor is the only thing that can say which "31" this is.
    dated = backfill._date_reports_against_anchor(
        [_report("METAR LEMD 312200Z 35004KT CAVOK 28/13 Q1018")],
        backfill._anchor_instant("20260801_0200"),
    )

    assert dated[0]["source_report_time"] == "2026-07-31T22:00:00.000Z"


def test_a_report_that_cannot_be_parsed_is_passed_through_untouched() -> None:
    # The ingress rejects it on its own terms; guessing here would put a
    # fabricated instant on a report nobody could read.
    dated = backfill._date_reports_against_anchor(
        [_report("not a metar at all")],
        backfill._anchor_instant("20260804_2200"),
    )

    assert dated[0]["source_report_time"] == ""


def test_a_report_that_already_states_its_time_is_left_alone() -> None:
    # The JSON endpoint supplies ``reportTime``; an inference must never
    # overwrite what the source itself stated.
    stated = _report("METAR LEMD 042200Z 35004KT CAVOK 28/13 Q1018")
    stated["source_report_time"] = "2026-08-04T22:00:00.000Z"

    dated = backfill._date_reports_against_anchor(
        [stated], backfill._anchor_instant("20260901_0000")
    )

    assert dated[0]["source_report_time"] == "2026-08-04T22:00:00.000Z"


def test_the_anchor_is_read_as_utc() -> None:
    assert backfill._anchor_instant("20260804_2200") == datetime(
        2026, 8, 4, 22, 0, tzinfo=timezone.utc
    )
