# Onboarding LLBG / LTFM / UUWW (fleet 45 → 48)

Tel Aviv (LLBG), Istanbul (LTFM) and Moscow Vnukovo (UUWW) have had listed
markets, settlement timezones and 23-24 METARs a day since 2026-08-22.  The
model has never priced them, and the reason is not the stations: it is that
the fleet size is a literal in about twenty places rather than a derived
quantity.

## What is already done (idempotent, safe to re-verify)

* `calibration_station_registry` and
  `calibration_station_market_timezone_binding` carry all 48 rows.  The
  timezone comes from the market, not the seed column -- 33 of the seed's 45
  are wrong and the table is append-only.
* Nine years of ERA5 climatology for the three stations.
* Season anchors measured: LLBG July-August peak (amplitude 14.9C), LTFM
  July (18.7C), UUWW July (27.5C).

## What this patch holds

`llbg_ltfm_uuww_48.patch` flips the fleet:

* `station_registry.py` -- coordinates in two provenance blocks.  The 45
  founding coordinates keep their original AWC snapshot; the three new ones
  carry their own 614-byte snapshot (sha `b5b5b437…`), fetched for exactly
  those ids.  Re-taking one fleet-wide snapshot would silently move the 45
  coordinates already in production, because AWC revises values.
  `_SNAPSHOT_BY_STATION` lets every station report the snapshot it actually
  came from, and the manifest lists both.
* `station_season.py` -- 48 anchors, regenerated.  Verified: the 45 existing
  anchors do not move, and WMKK/WSSS stay anchorless (they are equatorial).
* Three forward capture configs -- `model_arrival_capture_v1`,
  `open_meteo_aux_nwp_capture_v1`, `icon_forward_capture_scheduler_v1`.

Deliberately NOT in the patch: `republisher_no_live_fallback_binding_v1.json`
also says 45, but that number sits beside a `report_content_sha256` and
records how many stations a report that already exists actually covered.  It
is a historical fact, and nothing compares it to the fleet.

## What remains

1. **~20 hard-coded 45s in production code and scripts.**  Known:
   `native_ensemble_aifs` (`len(stations) > 45`), `production_worker:2778`,
   `settlement_version_scorecard:58`, `settled_attribution:562`, and a dozen
   scripts including three whose filenames contain `45station`.
2. **~65 test failures across 18 files**, measured by diffing the failure set
   at 45 against 48 over the 46 fleet-referencing test files.  All are
   fixtures building 45-station data against guards that now expect 48.
3. **Production content-addressed manifests** (`config/artifacts/pr12_*`).
   Six writers put the station count into a content-addressed manifest, so
   changing the fleet unbinds every artifact pinned to it.  This is the last
   step and wants a quiet window.

Not a blocker, but worth knowing: the AIFS and CMA capture paths read
`data/bayes_basket_target_market_registry_snapshot.json`, a frozen 45-station
export from 2026-07-31 with no generator in the tree.  Neither provider is
currently among the ten the fleet captures, so it is inert -- but a future
AIFS run at 48 stations would need that export refreshed.

## Measuring honestly

Diff the failure set, never count one side.  Roughly 55-65% of the tree's
red is red before any of this.  Two traps, both paid for already: a pipeline
ending in `head` dies of SIGPIPE and leaves `git stash pop` unrun, so the
next round measures the wrong tree -- check `git stash list` after every
batch; and a shell variable holding a newline-joined file list can expand to
nothing, which pytest reports as "no tests ran" and a `grep FAILED` reads as
a clean pass.
