# TAF TX: likelihood or feature?  (2026-09-24, read-only walk-forward)

Question (熊先生): 区域模型作特征优于作似然（regional_*）——那 TAF 呢？

Sample: clean P0 envelopes (no bias_refused system, >=12 h to settlement) at the 17 TX stations, 08-01..09-21;
row = P0 envelope whose decision time already had a TAF with a TX for that event day (forward capture, provider-available
time; +6 h in the lag-6 run).  10,650 rows / 490 station-days (lag 0); 7,328 / 423 (lag 6).  Every parameter as-of
(station cells whose truth was knowable at the decision), pooled line + K=15 shrink exactly as `taf_likelihood.py`,
charter-158 groups from `disagreement_group`, lambda per group fitted as-of on pooled training rows.

Arms: shift_roll (charter-201 form, k on debiased TX), shift_raw/shift_ab, shift_temp (same charter-158 tempering machinery
on the shift: delta = k_st * m_g * x), lik_pool (charter 150), lik_pool_temp (charter 158 = production form), lik_rec /
lik_rec_temp (the likelihood's centre only, width restored), lik_naive, antithetic controls.  Score: RPS on the ladder,
cells = (day, tier), event-day cluster bootstrap within station, equal station weight.

## Result (dRPS vs P0, 17 stations; negative = better)

| arm | lag 0 | lag +6 h |
|---|---|---|
| shift_roll (feature, charter 201 form) | +0.001 [-0.017, +0.021] | +0.011 [-0.010, +0.033] |
| shift_temp (feature + group tempering) | +0.006 [-0.009, +0.021] | +0.017 [+0.002, +0.033] |
| lik_pool (likelihood, charter 150) | +0.022 [-0.006, +0.052] | +0.023 [-0.006, +0.055] |
| **lik_pool_temp (likelihood + charter 158 = production form)** | **-0.014 [-0.028, -0.001]** | -0.001 [-0.017, +0.018] |
| lik_rec_temp (tempered likelihood's centre only) | +0.000 [-0.013, +0.013] | +0.009 [-0.006, +0.026] |

Head-to-head lik_pool_temp - shift_temp: -0.020 [-0.032, -0.009] (lag 0, 14/17 stations), -0.018 [-0.032, -0.004] (lag 6, 12/17).
By group (lag 0): REASONED (16% of rows) lik_pool_temp -0.068 [-0.110, -0.029], beats shift_temp by -0.045; SMALL (63%)
-0.011 vs shift_temp +0.002 (h2h -0.013 [-0.025, -0.002]); UNREASONED_COLD / OTHER: both forms lose when they follow the
TAF, tempering mutes the likelihood to ~0, the shift's multiplier does not mute enough (+0.026 / +0.055).
Tier 12-24 h carries the gain (h2h -0.029 [-0.043, -0.015]); 24-36 h n.s.; >=36 h too few rows.

## Reading

The opposite of the regional model, for a stated reason.  The regional forecast's information was in the CENTRE
(k-shrunk shift -0.054) and its likelihood was sharp (tau 0.6-1.0 on 10-34 cells) so multiplying narrowed an already
over-confident P0.  The TAF's information is NOT in the centre: the shift form gains nothing (its sign is right --
shift vs random sign -0.033 -- but P0's centre already carries it, as measured 09-13: beta 0.22, centre mix 0.02 C), and
transplanting the likelihood's own centre (lik_rec_temp) is also ~0.  What the TAF carries is CONDITIONAL SHAPE
information: when it disagrees with P0 for a physically stated reason the truth sits on the TAF's side and P0 is too
wide there; when it agrees (|x| < 1 C, 63% of rows) the agreement itself is evidence and the likelihood sharpens P0
around the agreed value, which no shift can express (a shift of ~0 does nothing).  The TAF line is flat (tau 1.2-1.5,
b 0.35: an equivalent thermometer of +-3.5-4 C) and with lambda <= 0.75 the narrowing is mild (posterior sd 1.67 -> 1.46,
mode hit 0.347 -> 0.331; the untempered line: 1.41 / 0.314).

Untempered, the likelihood is worse than both P0 and the shift.  Tempering is what makes it work (lik_pool_temp -
lik_pool -0.037 [-0.057, -0.019]); the as-of lambda table settles at REASONED 0.5 / UNREASONED_COLD 0.25 / OTHER 0.5 /
SMALL 0.75 -- lower everywhere than the production artifact's 1 / 0.5 / 1 / 1 (fitted 09-22 on NLL by day).  The
production artifact's own walk-forward agrees in shape: line gain +0.012 nat CI [-0.035, +0.058] (gate IDENTITY as
measured, SERVE by charter 151), tempering +0.009 nat [-0.002, +0.024] vs lambda = 1.

Caveats: the absolute gain over P0 is 2% of RPS and vanishes at +6 h arrival lag (the ordering does not); the
lambda grid is chosen on cell RPS, production's on NLL by day; ZSPD/KBKF lose mode-hit under the likelihood in
REASONED (0.91 -> 0.43, 0.49 -> 0.23) while SBGR/ZBAA/RKPK gain it.

Files: collect_taf_envelopes.py (sample), taf_lik_vs_feature.py (arms), results_lag{0,6}.json, out_lag{0,6}.txt;
regional_* are the regional-model run of the same question (09-23) for comparison.

## Follow-up (2026-09-24, 熊先生「测试」): should the production lambda table be lowered to the as-of table?

Two yardsticks, run side by side.

**This harness** (P0-envelope rows, cell = (day, tier), 17 stations equal weight; RPS and floored NLL):
production table {1, 0.5, 1, 1} vs P0: RPS +0.009 [-0.014, +0.035], NLL -0.023 [-0.043, -0.002].
As-of four-group table (lik_pool_temp) beats it on both: RPS -0.023 [-0.038, -0.009], NLL -0.013 [-0.024, -0.002],
15/17 stations, both lags.  The gap sits in OTHER (warm TAF without clear/dry skies, 7% of rows: lambda 1 -> 0.5 is
-0.10 RPS in-group) and UNREASONED_COLD (0.25 vs 0.5: -0.026 at lag 0, n.s. at lag 6); REASONED and SMALL n.s.
Group-specific beats any single global lambda on NLL (0.25 / 0.5 / 0.75: +0.016 / +0.009 / +0.012 vs as-of).

**Production's own machinery** (`fit_taf_likelihood.py`, read-only, `fitter_variants.py` / `fitter_fixed_tables.py`:
as-of lines, TAF-arrival x current-P0 pairing, weight 1/n per station-day, log score vs lambda = 1 pooled by event
day; 2,296 TAFs / 32 days):

| table | by-day mean gain vs lambda = 1 (nat) | note |
|---|---|---|
| production {1, .5, 1, 1} | +0.0091 [-0.0002, +0.0219] | reproduces the artifact |
| as-of low {.5, .25, .5, .75} | +0.0034 [-0.018, +0.028] | OTHER +0.043 by weight, REASONED -0.005 |
| OTHER muted {1, .5, .5, 1} | +0.0082 [-0.006, +0.024] | OTHER's +0.043 by weight is a few heavy days: by-day mean does not move |
| global 0.5 / 0.75 | +0.001 / +0.006 | |
| low - production | -0.0057 [-0.022, +0.011] | n.s. |

Letting the fitter fit all four groups (TEMPERED_GROUPS extended): it picks REASONED 1.0, OTHER 1.0, SMALL 1.25 in
sample and walks forward at -0.004 nat [-0.019, +0.013] -> IDENTITY; the 2026-09-19 finding reproduces exactly.

**Ruling material**: on production's own yardstick no table beats the current one and every gain is inside noise; the
harness's advantage for the low table comes from weighting every P0 re-decision and from RPS on the ladder, not from
the TAF-arrival log score the gate is written against.  A hand-set table would also be overwritten by the daily refit.
So: leave the production table and TEMPERED_GROUPS alone.  What survives both yardsticks is only the ordering
(tempered likelihood > untempered; likelihood > shift).
