"""What survives of the v6 cutover: the station-day boundary it installed.

The cutover to physical_state_bayes_v6 is done -- the package is imported
by production_probability_v6_public_ssot_writer and the writer stamps
physical_state_bayes_v6_production -- and its two one-shot scripts,
cutover_physical_state_bayes_v6_production.sh and
..._runtime_hotfix.sh, were deleted with the rest of that work on
2026-08-04.

Nine tests here read those two files and asserted on their text: that the
commit boundary was irreversible, that the monitor stopped before the
writers, and so on.  Every one of them could only raise FileNotFoundError
once the scripts were gone, which is a red that says nothing about the
system the cutover produced.  They are retired with the scripts, on
2026-09-07.

What is kept is the one assertion that is about the running system rather
than about how it got here: the physical entrypoints resolve the weather
day in the station's own zone.  Its fourth script lost its v6 suffix in
the same commit and is followed here to its new name.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_v6_zuuu_physical_entrypoints_use_station_day() -> None:
    physical_entrypoints = (
        ROOT / "scripts/bootstrap_production_probability_v6.py",
        ROOT / "scripts/production_probability_v6_live_acceptance.py",
        ROOT / "scripts/prepare_next_day_production_probability_v6.py",
        # Renamed out of its v6 suffix when the cutover finished.
        ROOT / "scripts/apply_amsc_probability_fast_lane_from_plan.py",
    )
    for path in physical_entrypoints:
        text = path.read_text(encoding="utf-8")
        assert 'ZoneInfo("Asia/Shanghai")' in text
