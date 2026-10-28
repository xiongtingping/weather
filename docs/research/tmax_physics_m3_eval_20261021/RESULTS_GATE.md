# Weekly refit admission — calibration reading (2026-10-28)

User 2026-10-28 「加准入门」. Gate: `refit_gate.py`, called by `fit_board.py --runtime`.

Dry run on the store as of 2026-10-28 (rows through 2026-10-24; held out 2026-10-11..10-24,
629 station-days, 15,096 paired station-hours; the card in use 20261026T201634Z, window to 10-23):

| check | value | limit |
|---|---|---|
| probe MAE at 9/11/13/15/17 h vs ΔT = 0 | 0.594 vs 1.183 C | must be lower |
| probe − card in use, mean Student-t log score | +0.0087 nat | ≤ 0.02 |
| full fit / probe fleet residual SD, worst hour (23 h) | 1.004 | ≤ 1.2 |

Optimism of having seen the held-out days: the full fit (saw all 14) scores them 0.0092 nat
better than the probe.  The card in use saw 13 of the 14, so its +0.0087 lead over the probe
is that optimism, not a better fit.  The tolerance 0.02 is about twice it: a probe is refused
only when it is worse beyond what seeing the days explains.  Verdict on 2026-10-28: admitted.
