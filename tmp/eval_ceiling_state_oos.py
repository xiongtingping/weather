#!/usr/bin/env python3
"""Out-of-sample: does conditioning the ceiling on path state pay?

Each event date is scored with a curve fitted only on strictly earlier dates
-- the same discipline the replay table enforces -- and the two forms are
compared on the same rows: the bin's marginal rate against the conditioned
one.  Log loss and Brier on the layer's own target ("did the day end above
the bucket that held its running high"), overall and per hours bin.

Both forms come from the production fit and the production serving function;
this file only splits the days and adds up the losses.
"""
from __future__ import annotations

import importlib.util
import math
import pickle
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.metar_ceiling import probability_still_rising

SPEC = importlib.util.spec_from_file_location(
    "ceiling_fit", ROOT / "scripts" / "fit_metar_ceiling_artifact.py"
)
FIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIT)

CACHE = Path(
    "/private/tmp/claude-501/-Users-xiong-weather/"
    "75cac755-ae4e-4198-ab16-0f8da9badbc5/scratchpad/ceiling_samples.pkl"
)
MIN_TRAIN = 2000


def load_samples() -> list[dict]:
    if CACHE.exists():
        return pickle.loads(CACHE.read_bytes())
    samples = FIT.collect(
        ROOT / "data" / "bayes_basket_target_ssot.db",
        ROOT / "data" / "archive.db",
    )
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_bytes(pickle.dumps(samples))
    return samples


def main() -> None:
    samples = load_samples()
    print(f"样本 {len(samples)} 条,"
          f" 带路径状态 {sum(1 for s in samples if s.get('state'))} 条")
    dates = sorted({s["event_date"] for s in samples})

    losses: dict[str, list[float]] = defaultdict(list)
    brier: dict[str, list[float]] = defaultdict(list)
    per_bin: dict[int, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    scored_dates = []
    declined = {"no_curve": 0, "bin_backed_off": 0, "no_state": 0}

    for date in dates:
        earlier = [s for s in samples if s["event_date"] < date]
        if len(earlier) < MIN_TRAIN:
            continue
        artifact = FIT.fit(earlier)
        # The hours-only counterfactual is the same artifact with its slopes
        # taken away, so nothing but the conditioning differs.
        bare = {
            key: value for key, value in artifact.items()
            if key not in {"state_coefficients", "state_covariate_means"}
        }
        today = [s for s in samples if s["event_date"] == date]
        used = 0
        for sample in today:
            hours = sample["hours_left"]
            state = sample.get("state")
            plain = probability_still_rising(bare, hours)
            if plain is None:
                declined["bin_backed_off"] += 1
                continue
            if state is None:
                declined["no_state"] += 1
                continue
            conditioned = probability_still_rising(artifact, hours, state)
            if conditioned is None:
                declined["bin_backed_off"] += 1
                continue
            outcome = float(sample["still_rose"])
            index = FIT.hours_bin(hours)
            for label, probability in (
                ("hours_only", plain), ("hours_and_state", conditioned)
            ):
                bounded = min(max(probability, 1e-6), 1.0 - 1e-6)
                loss = -(
                    outcome * math.log(bounded)
                    + (1.0 - outcome) * math.log(1.0 - bounded)
                )
                score = (probability - outcome) ** 2
                losses[label].append(loss)
                brier[label].append(score)
                per_bin[index][label + "_loss"].append(loss)
                per_bin[index][label + "_brier"].append(score)
            used += 1
        if used:
            scored_dates.append((date, used))

    if not losses["hours_only"]:
        print("无可评样本")
        return
    print(f"评了 {len(scored_dates)} 个事件日,"
          f" 共 {len(losses['hours_only'])} 行"
          f" (放弃: {dict(declined)})")
    print(f"日期覆盖 {scored_dates[0][0]}..{scored_dates[-1][0]}")
    print()

    def mean(values: list[float]) -> float:
        return sum(values) / len(values) if values else float("nan")

    base_loss = mean(losses["hours_only"])
    new_loss = mean(losses["hours_and_state"])
    base_brier = mean(brier["hours_only"])
    new_brier = mean(brier["hours_and_state"])
    print("总体 (越低越好):")
    print(f"  对数损失  仅小时 {base_loss:.4f} → 加状态 {new_loss:.4f}"
          f"  ({100 * (new_loss - base_loss) / base_loss:+.1f}%)")
    print(f"  Brier    仅小时 {base_brier:.4f} → 加状态 {new_brier:.4f}"
          f"  ({100 * (new_brier - base_brier) / base_brier:+.1f}%)")
    print()
    print("按小时档 (n / 对数损失 仅小时→加状态 / Brier 仅小时→加状态):")
    for index in sorted(per_bin):
        entry = per_bin[index]
        n = len(entry["hours_only_loss"])
        low = FIT.HOURS_EDGES[index]
        high = FIT.HOURS_EDGES[index + 1]
        label = f"{low:.0f}-{high:.0f}h" if high < 1e8 else f"{low:.0f}h+"
        lo_base = mean(entry["hours_only_loss"])
        lo_new = mean(entry["hours_and_state_loss"])
        br_base = mean(entry["hours_only_brier"])
        br_new = mean(entry["hours_and_state_brier"])
        flag = "✓" if lo_new < lo_base else "✗"
        print(f"  {label:>8s} n={n:5d}  "
              f"{lo_base:.4f}→{lo_new:.4f} ({100*(lo_new-lo_base)/max(lo_base,1e-9):+6.1f}%)  "
              f"{br_base:.4f}→{br_new:.4f}  {flag}")


if __name__ == "__main__":
    main()
