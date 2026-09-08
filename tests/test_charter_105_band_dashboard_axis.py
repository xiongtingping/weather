"""The board reads a band table through the table's own axis.

Charter 105 moved P0's cells from the claim to the bucket's distance from
the peak, and every *serving* lane was taught to switch lookups with the
table (``test_charter_105_band_fit_axis``).  The board was not.  It kept
mapping an eleven-row 5% display grid onto whatever cells it found, and a
distance table's five cells are ``[0,1) [1,2) [2,3) [3,4) [4,99)`` -- every
claim midpoint from 0.025 to 0.475 falls inside the first one.  So on
2026-09-08 the P0 card printed one factor, 1.014, on all eleven rows, with
"1.09× 档 0-100%" beside it, directly under a header that said "4 档生效".

Nothing was served wrong: production applied the distance lookup correctly
the whole time.  The page was describing a table it could not read, which
is the failure mode a shared axis column exists to prevent -- both lookups
take ``[lo, hi)`` pairs and both "work".

The renderer is JavaScript inside ``HTML_TEMPLATE``, so these tests extract
``stages()`` and run it under node against a payload, rather than asserting
on the source text: the bug was in what the function produced, and a source
grep for "centre_distance" would have passed on code that still printed the
first cell eleven times.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402

# The live P0 table on 2026-09-07 and the ratios it was fitted from, so the
# numbers this test reads are the numbers the screenshot showed.
DISTANCE_FACTORS = [
    [[0.0, 1.0], 1.0144718713084007],
    [[1.0, 2.0], 1.0565076398594613],
    [[2.0, 3.0], 1.0],
    [[3.0, 4.0], 0.982131282548169],
    [[4.0, 99.0], 0.6725719452649614],
]
DISTANCE_RATIOS = {
    "[0.0, 1.0]": 1.0895759093523634,
    "[1.0, 2.0]": 1.1298326841057522,
    "[2.0, 3.0]": 1.0426819939152618,
    "[3.0, 4.0]": 0.7918517042255736,
    "[4.0, 99.0]": 0.5698550859374643,
}
CLAIM_FACTORS = [
    [[round(i * 0.05, 2), round(i * 0.05 + 0.05, 2)], 1.0 + i / 1000]
    for i in range(10)
] + [[[0.5, 1.01], 0.947]]


def _evidence(axis: str, ratios: dict[str, float]) -> str:
    return json.dumps(
        {
            "axis": axis,
            "accumulating": False,
            "serving_day_count": 39,
            "serving_station_day_count": 1386,
            "serving_report": {
                key: {"ratio": ratio, "qualified": True, "blocked_by": None}
                for key, ratio in ratios.items()
            },
            "basis": {key: "serving_lane" for key in ratios},
        }
    )


def _legacy_schema(conn: sqlite3.Connection) -> None:
    """The table as it stood before the axis column, where every row meant
    the claim partition and nothing recorded that it did."""

    conn.execute(
        "CREATE TABLE calibration_band_reliability_table ("
        " band_table_sha256 TEXT PRIMARY KEY, factors_json TEXT,"
        " evidence_json TEXT, fitted_at_utc TEXT, serving_day_count INTEGER,"
        " activated INTEGER, activated_at_utc TEXT, stage TEXT)"
    )


def _insert(
    conn: sqlite3.Connection,
    stage: str,
    factors: list,
    axis: str | None,
    ratios: dict[str, float],
) -> None:
    columns = (
        "band_table_sha256, factors_json, evidence_json, fitted_at_utc,"
        " serving_day_count, activated, activated_at_utc, stage"
    )
    values: list = [
        f"{stage}{axis or 'legacy'}".ljust(64, "0")[:64],
        json.dumps(factors),
        _evidence(axis or "claim", ratios),
        "2026-09-07T16:19:45Z",
        39,
        1,
        "2026-09-07T16:19:45Z",
        stage,
    ]
    if axis is not None:
        columns += ", axis"
        values.append(axis)
    conn.execute(
        f"INSERT INTO calibration_band_reliability_table ({columns})"
        f" VALUES ({','.join('?' for _ in values)})",
        values,
    )


@pytest.fixture()
def conn() -> sqlite3.Connection:
    from bayes_basket_target.band_reliability_store import (
        ensure_band_table_schema,
    )

    connection = sqlite3.connect(":memory:")
    ensure_band_table_schema(connection)
    _insert(connection, "P0", DISTANCE_FACTORS, "centre_distance",
            DISTANCE_RATIOS)
    _insert(connection, "P1", CLAIM_FACTORS, "claim",
            {json.dumps(cell): 0.9 for cell, _f in CLAIM_FACTORS})
    return connection


def _stage(stages: list[dict], name: str) -> dict:
    return next(s for s in stages if s["stage"] == name)


def test_the_payload_carries_each_stages_axis(conn) -> None:
    """Without it the renderer has to guess, and both guesses parse."""

    stages = C.collect_band_stages(conn)
    assert _stage(stages, "P0")["axis"] == "centre_distance"
    assert _stage(stages, "P1")["axis"] == "claim"
    # The cells ride out unchanged; the axis says how to read them.
    assert [b["lower"] for b in _stage(stages, "P0")["bands"]] == [
        0.0, 1.0, 2.0, 3.0, 4.0
    ]


def test_a_database_without_the_column_reads_as_claim() -> None:
    """Every table written before 2026-09-07 was fitted on the claim, so an
    absent column is an answer, not a blank -- the board must render those
    on the 5% grid rather than refusing them."""

    connection = sqlite3.connect(":memory:")
    _legacy_schema(connection)
    _insert(connection, "P0", CLAIM_FACTORS, None,
            {json.dumps(cell): 0.9 for cell, _f in CLAIM_FACTORS})
    stages = C.collect_band_stages(connection)
    assert _stage(stages, "P0")["axis"] == "claim"


def _render(stages: list[dict]) -> str:
    """Run the board's own ``stages()`` over a payload.

    Extracted from ``HTML_TEMPLATE`` by brace matching rather than copied,
    so this exercises the function that ships.
    """

    node = shutil.which("node")
    if node is None:  # pragma: no cover - CI images all carry node
        pytest.skip("node is required to run the board's renderer")
    source = C.HTML_TEMPLATE
    start = source.index("function stages(){")
    depth = 0
    end = None
    for index in range(source.index("{", start), len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    assert end is not None, "stages() has unbalanced braces"
    script = (
        "const D = "
        + json.dumps({"stages": stages}, ensure_ascii=False)
        + ";\n"
        "const h = (s) => String(s == null ? '' : s);\n"
        "const hours = (v) => v == null ? '-' : String(v);\n"
        "function reliabilityAudit(){ return ''; }\n"
        + source[start:end]
        + "\nconsole.log(stages());\n"
    )
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _factor_cells(card: str) -> list[str]:
    import re

    return re.findall(r'<td class="factor [a-z]+">([\d.]+)', card)


def _row_labels(card: str) -> list[str]:
    import re

    return re.findall(r'<td class="mono">([^<]+)</td>', card)


def _axis_header(html: str) -> str:
    """The first column's header on the card's own table.

    Scoped to the table because the section's prose explains BOTH axes now
    -- an assertion on the whole page would pass on the explanation while
    the table above it still said the wrong thing.
    """

    import re

    table = html[html.index('<table style="margin-top:12px">'):]
    return re.search(r"<th>([^<]+)</th>", table).group(1).strip()


def test_a_distance_table_renders_one_row_per_rung(conn) -> None:
    """The regression itself.  Under the claim grid this produced eleven
    rows carrying 1.014 -- the ``[0,1)`` factor -- because every claim
    midpoint under 50% lands in the first distance cell."""

    stages = C.collect_band_stages(conn)
    html = _render([_stage(stages, "P0")])
    factors = _factor_cells(html)
    assert factors == ["1.014", "1.057", "1.000", "0.982", "0.673"]
    # The card's pill says "4 档生效"; four rows must actually move.  The
    # broken page said the same thing over eleven rows that all read 1.014.
    assert sum(1 for f in factors if f != "1.000") == 4
    labels = _row_labels(html)
    assert labels == ["峰值桶", "离峰 1 格", "离峰 2 格", "离峰 3 格", "离峰 ≥4 格"]
    # And the column is named for what the rows actually are.
    assert _axis_header(html) == "桶离峰距离"
    # And the measured column keeps each rung's own ratio, not the first's
    # repeated: 1.09x on the peak, 0.57x four or more steps out.
    assert "1.09×" in html and "0.57×" in html


def test_a_claim_table_still_renders_the_five_percent_grid(conn) -> None:
    """The fix must not cost the claim stages their display grid: the cells
    are wider than the rows, and a row says which cell owns it."""

    stages = C.collect_band_stages(conn)
    html = _render([_stage(stages, "P1")])
    labels = _row_labels(html)
    assert labels[0] == "0–5%"
    assert labels[-1] == "≥50%"
    assert len(labels) == 11
    assert _axis_header(html) == "概率档"
    assert "离峰" not in html[html.index('<table style="margin-top:12px">'):]


def test_the_section_names_the_axis_each_stage_is_on(conn) -> None:
    """Three cards that no longer share a partition have to say so, or the
    reader compares a distance rung with a probability band by eye."""

    stages = C.collect_band_stages(conn)
    html = _render(stages)
    assert "P0＝离峰距离" in html
    assert "P1＝声称概率" in html
    # The residual audit below is always on the claim ruler, so the claim
    # of a shared 5% ruler cannot stand once any stage has moved.
    assert "不能逐行对照" in html
