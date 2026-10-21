---
status: accepted
authority: L3
owner: weather-system
effective_from: 2026-08-30
---

# Onboarding LLBG / LTFM / UUWW (fleet 45 → 48)

Tel Aviv (LLBG), Istanbul (LTFM) and Moscow Vnukovo (UUWW) have had listed
markets, settlement timezones and 23-24 METARs a day since 2026-08-22.  The
model has never priced them, and the reason is not the stations: it is that
the fleet size is a literal in about twenty places rather than a derived
quantity.

## Moved 2026-10-04 (fleet 45 -> 48)

Done in one commit and one restart, in the order the earlier two attempts
(2026-08-31, 2026-09-21) showed it has to go:

1. `scripts/prepare_fleet_move.py --add LLBG LTFM UUWW --commit-authority
   --mint ...` appended `global_awc_ssot_v2` source-authority rows for the 48
   (only the projection builder reads them) and minted
   `config/artifacts/pr12_authority_scope_projection_current_20261004T120000Z.json`
   from the doctrine blobs pinned at e9d31583.
2. `--prove` ran the pr12 contract builder read-only against it: 1,400
   revision facts, the three new stations 157-161 each, 136 closed station-days
   quarantined (the old projection already closed them; their old generation
   stays and is what settlement reads).
3. The move itself: coordinates (one AWC stationinfo call over all 48, no
   drift in the 45), the sweep's PROJECTION pointer, the capture configs'
   fleet label, the lane map, the charter-205 settlement-source contract
   (all three resolve on weather.gov/wrh/timeseries, degrees C), and the
   regional blocks.  The seven tests the move turned red used the three as
   their example of a station outside the fleet; they use ZZZZ now.

4. **Missed on the day, done the same evening:** the cash-phase authority.
   `basket_cash_phase_authority_manifest` held the 45 seeded stations only, so
   every snapshot of the three was refused on
   `resident_cash_phase_authority_manifest_not_unique` (zero rows) -- three
   `basket_official_ws_snapshot_missing` scopes a cycle.  The fix is data, not
   code: `scripts/renew_cash_phase_authority_window.py --onboard --valid-from
   <now>` issues the fleet's (uniform) template to every fleet station with
   no binding, under its own name and market timezone, ending with the fleet's
   window; ingest the packet with `scripts/ingest_bayes_basket_target_artifacts.py
   --apply`.  The next refusal behind it was the same kind:
   `production_basket_policy_artifacts_missing`, the twelve
   `basket_policy_activation` rows every seeded station holds (all pointing at
   the global prior) -- `scripts/onboard_basket_policy_activation.py
   --valid-from <now>`, same ingest.  Run both as part of every future move.

The patch file below is history; the evidence the three stations still have
to earn (arrival corpus, OOS activation) accrues from here.

## Moved 2026-10-21 (fleet 48 -> 49): KDEN, a city changing station

Denver's markets settle on KBKF through event date 2026-10-22 and on KDEN
(Denver International) from 2026-10-23, so KDEN joins beside KBKF rather than
replacing it.  The charter-205 baseline keeps one entry per city and names the
station it settles on now, with `previous_station: {station_id, through}` for
the one before; `settlement_basis.settled_station(station, event_date)` is the
one reading (the registry's expected contract, the truth reconciliation and
the copy-follow lane's token map all ask it).  Same order as 10-04: SSOT rows
and ERA5 climatology with the coordinate patched in process (anchor July,
amplitude 26.1C, the 48 existing anchors unchanged), `prepare_fleet_move.py
--add KDEN --authority-version global_awc_ssot_v3 --commit-authority --mint`
(49 rows; prove: 1,291 revision facts, 153 closed days quarantined, KDEN 0 --
no route yet), then the one commit.  KDEN is not added to the HRRR/NBM
capture roster: charter 201 freezes that roster to `P1_FEATURE_PAIRS`.

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

The patch now carries seven files, not five: the station lane map and the
official source routes were briefly committed on their own and had to be
pulled back.  Both are read against the fleet, so a 48-station config under a
45-station fleet is itself a mismatch -- it turned two passing contract tests
red.  The pairing has to move together.

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

## Measured cost (2026-08-31, nine passes)

Full-suite net-new failures at 48, diffed against a 45-side baseline taken
from the same commit: **75 -> 16**.  The first estimate, before any of this,
was 305.  Most of the reduction came from deriving literals rather than from
changing behaviour; every pass was verified as a no-op at 45 by diffing the
failure sets rather than counting one side.

The remaining sixteen are single cases across eleven files.  A caution about
that number: the baseline drifts.  Parallel sessions commit while a 35-minute
suite runs, so a diff against a baseline taken hours earlier attributes their
failures to the fleet.  Re-take the baseline from the same commit as the
measurement, or the number is fiction.

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
