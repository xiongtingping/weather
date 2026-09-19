"""A stage the refit retires has no fit, and the summary must not read one.

Charter 150 (2026-09-19) fits the band and centre/width tables for P0 only and
retires P0-M's and P1's: their entries come back as ``{stage, served_stage:
False, commit}``.  Both CLIs' ``--summary`` still read ``fitted_factors`` /
``serving_day_count`` off every entry, and on 2026-09-18T20:16Z each died on a
KeyError after its tables had committed -- exit 1, so the calibration_refit
sweep read ``failed`` and its completion clock stayed a day old.

The retired entries here come from the real store code, with the served set
emptied so every stage takes the retirement branch: a hand-written fixture is
how the summary drifted from the store in the first place.
"""
from __future__ import annotations

import io
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "scripts"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from bayes_basket_target import band_reliability_store, center_width_store  # noqa: E402

import refit_band_reliability  # noqa: E402
import refit_center_width  # noqa: E402

STAGES = ("P0", "P0-M", "P1")


def _run(cli, argv, monkeypatch) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", argv)
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main()
    return code, out.getvalue()


def test_band_summary_names_the_retired_stages(tmp_path, monkeypatch) -> None:
    db = tmp_path / "ssot.db"
    conn = sqlite3.connect(str(db))
    band_reliability_store.ensure_band_table_schema(conn)
    conn.commit()
    conn.close()
    monkeypatch.setattr(band_reliability_store, "BAND_SERVED_STAGES", ())
    monkeypatch.setattr(refit_band_reliability, "BAND_SERVED_STAGES", ())

    code, text = _run(
        refit_band_reliability,
        ["refit_band_reliability.py", "--db", str(db), "--commit", "--summary"],
        monkeypatch,
    )

    assert code == 0, text
    for stage in STAGES:
        assert f"SUMMARY {stage}: 宪章150起不服务，不拟合 [UNCHANGED]" in text


def test_centre_width_summary_names_the_retired_stages(tmp_path, monkeypatch) -> None:
    db = tmp_path / "ssot.db"
    conn = sqlite3.connect(str(db))
    center_width_store.ensure_center_width_schema(conn)
    conn.commit()
    conn.close()
    monkeypatch.setattr(center_width_store, "CENTER_WIDTH_SERVED_STAGES", ())
    monkeypatch.setattr(refit_center_width, "CENTER_WIDTH_SERVED_STAGES", ())

    # The arguments the nightly sweep passes, per-stage clock included.
    code, text = _run(
        refit_center_width,
        ["refit_center_width.py", "--db", str(db), "--commit", "--summary",
         "--since", "2026-08-27T09:20:00Z",
         "--since-stage", "P0=2026-09-18T09:18:16Z"],
        monkeypatch,
    )

    assert code == 0, text
    for stage in STAGES:
        assert f"SUMMARY {stage}: 宪章150起不服务，不拟合 [UNCHANGED]" in text
