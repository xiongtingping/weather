# TAF and METAR weather at their own stages (2026-09-23)

User ruling, 2026-09-23: 「TAF 的云、雨、风也应该像p0一样，不应该作为p0的替补，metar也是同理」, then
「不是删，是应该起作用」「在各自阶段起作用」「而且随着时间的推移，权重应该越大，而且可以二次确认预报里的云雨是否兑现」
「应该像p0一样使用」, and, on the summary "the forecaster's weather speaks only at the TAF step, the observed
weather only at the METAR step, and neither stands in for P0": 「这句话对的」.

The principle landed as charter 43's 2026-09-23 amendment (dc925e56): P0 no longer reads the TAF fields
as a stand-in for its radiation shift. This report measures the two placements the ruling asks for.
Nothing here changes served arithmetic.

## Data

* 8,243 forward TAFs (1,393 settled station-days, 45 stations, 2026-08-20..09-23), each on the P0 root
  current at its arrival. Cloud / precipitation / wind are re-derived from the raw archive
  (`data/runtime/taf_raw_archive`) with the shipped `_forecast_features` (61988984 + c725cd8f semantics),
  because the stored capture rows carry the old reading and no method marker. `load_cases.py`.
* P1: the production sample table (61,266 samples, 1,910 station-days); 44,861 samples get the TAF
  standing at their `as_of_utc` (`p1_augment_samples.py`, scratch copy only).

## 1. P0-M: a centre shift like P0's radiation shift, weighted by TAF lead

Shift = -k(lead) x index x sigma(root), index = equal-weight standardised cloud / precipitation / wind,
lead buckets by hours from arrival to the local 06:00 heating-window start (h00_12 incl. negative,
h12_24, h24_48). Walked forward one UTC day at a time on station-days settled before the day began
(`score.py`).

| arm | by-day mean vs P0 (nat) | 95% CI |
|---|---|---|
| monotone k (h00_12 >= h12_24 >= h24_48) | -0.0011 | [-0.0042, +0.0025] |
| free k per lead | -0.0091 | [-0.0167, -0.0031] |
| fixed 0.15 (P0's coefficient) | +0.0009 | [-0.0085, +0.0114] |

Why (`dissect.py`): the TAF fields correlate with P0's standardised residual at +0.027 / +0.009 / -0.006;
net of the radiation reading P0 already applied (corr with TAF cloud -0.358) the forecaster's increment
is -0.006 / -0.017 / -0.005, zero at every lead. The model's own radiation forecast already carries the
cloud the TAF describes.

## 2. P1: confirming whether the forecast cloud / rain materialised

On served P1 rows (13,484 rows, 208 station-days, `p1_confirm.py`) the pattern looked like a missing
confirmation: P1 already absorbs the observed cloud (corr +0.001), but the observed-minus-forecast
mismatch keeps a partial correlation of -0.105 (cloud) and -0.085 (precipitation); forecast rain that
came left P1 +0.33 sigma too cold, unforecast rain -0.48 sigma too warm.

The production fitter (`scripts/fit_p1_observation_likelihood.py`, charter 161 direct update, same
held-out rows, days 2026-09-13..09-22) decides it (`p1_run_fit.py`, `p1_compare.py`):

| state added to the direct update | paired mean vs incumbent state (nat/sample) | 95% CI | days better |
|---|---|---|---|
| TAF cloud, precipitation, wind | -0.0025 | [-0.0055, +0.0006] | 3/10 |
| surprise: observed - forecast cloud, precipitation | -0.0017 | [-0.0029, -0.0005] | 4/10 |

The direct update already reads the root's own belief (`root_rise_mass`, `root_mean_minus_high_c`)
and weights the observed cloud / precipitation / wind separately in the last six hours and in six to
ten hours, so "weight grows as the day goes on" is already how P1 reads the METAR weather.

## 3. What the P1 pattern actually is: P0's radiation shift is over-applied

The served radiation shift correlates negatively with the residual it leaves: -0.072 per station-day on
the absolute form (1,292 station-days, about 2.6 SE), -0.159 on the clearness form (150 station-days).
Keeping part of the served absolute shift on the same roots, by-day mean vs served:

| kept | gain (nat) | 95% CI |
|---|---|---|
| 0.75 | +0.019 | [+0.006, +0.037] |
| 0.50 | +0.024 | [+0.005, +0.048] |
| 0.25 | +0.023 | [-0.001, +0.052] |
| 0 | +0.017 | [-0.011, +0.051] |

The clearness form has 5 days only and points the same way (keep 0: +0.017 [-0.006, +0.042]). The
cooling a cloudy or wet forecast implies is applied about twice over at P0, which is why forecast rain
that came leaves P1 too cold. That is a P0 (charter 160) coefficient question and is not changed here.
