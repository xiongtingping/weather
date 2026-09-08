"""The ceiling fitter: hour bins with gap cells, thin cells backing off to the hour."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from bayes_basket_target import metar_ceiling


def _fitter():
    path = Path(__file__).resolve().parents[1] / "scripts" / "fit_metar_ceiling_artifact.py"
    spec = importlib.util.spec_from_file_location("fit_metar_ceiling_artifact", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_fit_writes_v2_cells_and_backs_thin_cells_off_to_the_hour() -> None:
    fitter = _fitter()
    samples = []
    # 10-13h bin: 200 samples at the high that mostly still rise, 200 two
    # degrees under that mostly do not, and 5 far under (too thin).
    for i in range(200):
        samples.append({"station_id": "A", "event_date": "2026-08-%02d" % (1 + i % 20),
                        "hours_left": 11.0, "still_rose": int(i % 10 < 8), "gap_c": 0.0})
        samples.append({"station_id": "A", "event_date": "2026-08-%02d" % (1 + i % 20),
                        "hours_left": 11.0, "still_rose": int(i % 10 < 2), "gap_c": 2.0})
    for i in range(5):
        samples.append({"station_id": "A", "event_date": "2026-08-01",
                        "hours_left": 11.0, "still_rose": 1, "gap_c": 5.0})
    # And one sample with no formal report yet: feeds the marginal only.
    samples.append({"station_id": "A", "event_date": "2026-08-01",
                    "hours_left": 11.0, "still_rose": 0, "gap_c": None})
    artifact = fitter.fit(samples)
    assert artifact["schema"] == metar_ceiling.SCHEMA
    assert artifact["gap_edges_c"] == list(metar_ceiling.GAP_EDGES_C)
    assert artifact["gap_dimension"] == metar_ceiling.CEILING_GAP_DIMENSION_VERSION
    assert artifact["gap_sample_n"] == 405
    bin_10_13 = artifact["bins"][str(fitter.hours_bin(11.0))]
    assert bin_10_13["sample_n"] == 406
    cells = bin_10_13["by_gap"]
    assert cells["0"]["p_still_rose"] == 0.8 and not cells["0"]["backed_off"]
    assert cells["2"]["p_still_rose"] == 0.2 and not cells["2"]["backed_off"]
    assert cells["3"]["backed_off"] and cells["3"]["p_still_rose"] == bin_10_13["p_still_rose"]
    assert cells["1"]["sample_n"] == 0 and cells["1"]["backed_off"]
    # The serving side reads the same cells the fitter wrote.
    assert metar_ceiling.resolve_rate(artifact, 11.0, 2.0)["rate"] == 0.2
    assert metar_ceiling.resolve_rate(artifact, 11.0, 5.0)["source"] == (
        "hours_marginal_gap_cell_backed_off"
    )


def test_the_latest_formal_report_is_the_newest_at_or_before_the_clock() -> None:
    fitter = _fitter()
    parse = fitter._parse
    points = sorted([
        (parse("2026-09-05T16:00:00Z"), 27.0),
        (parse("2026-09-06T03:00:00Z"), 25.0),
        (parse("2026-09-06T05:00:00Z"), 26.0),
    ])
    start = parse("2026-09-05T16:00:00Z")
    assert fitter._latest_formal_temperature(points, start, parse("2026-09-06T04:30:00Z")) == 25.0
    assert fitter._latest_formal_temperature(points, start, parse("2026-09-06T06:00:00Z")) == 26.0
    # Nothing yet today: a report from before the local day does not count.
    assert fitter._latest_formal_temperature(
        points, parse("2026-09-06T16:00:00Z"), parse("2026-09-06T17:00:00Z")
    ) is None


def test_the_daily_job_runs_on_the_interpreter_the_package_imports_under() -> None:
    """Bare ``python3`` is the wrong interpreter for this job, and the way
    it was wrong is the way this kind of failure is always wrong: silently.

    The launchd job's PATH puts ``/opt/homebrew/bin`` first, whose Python
    3.13 has never carried ``httpx`` -- which ``bayes_basket_target``
    imports at package import time.  Every scheduled run from 2026-08-26
    echoed its start line, died on ModuleNotFoundError, and left the
    artifact where it was; thirteen consecutive runs failed and the only
    refit in that stretch was one somebody ran by hand on 09-06.  Nothing
    reported it because the artifact does not expire -- it just keeps
    serving the curve it was last fitted with.

    So the script picks the interpreter the resident picks, and the
    fallback stays for a checkout with no venv.
    """

    script = (
        Path(__file__).resolve().parents[1]
        / "scripts" / "metar_ceiling_refit_daily.sh"
    ).read_text(encoding="utf-8")
    assert "resolve_project_python" in script, (
        "the resolver every other scheduled script here uses"
    )
    assert "command -v python3" not in script, (
        "that fallback is the bug: it resolves to Homebrew's under launchd"
    )
    # And the resolver's own fallback is the Framework build, not PATH.
    resolver = (
        Path(__file__).resolve().parents[1]
        / "scripts" / "lib" / "resolve_project_python.sh"
    ).read_text(encoding="utf-8")
    assert "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3" in resolver
    assert "command -v" not in resolver
