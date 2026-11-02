"""Shared scoring/statistics helpers: ladder RPS, zero-bucket-free NLL,
event-day cluster bootstrap."""
import math, random, statistics as S
from collections import defaultdict


def order(intervals):
    return sorted(intervals, key=lambda b: -1e9 if intervals[b][0] is None else float(intervals[b][0]))


def rps(prob, ordk, tb):
    F = O = s = 0.0
    for b in ordk[:-1]:
        F += float(prob.get(b, 0.0))
        O += 1.0 if b == tb else 0.0
        s += (F - O) ** 2
    return s


def nll(p):
    return -math.log(max(float(p), 1e-6))


def boot_day(diffs_by_day, B=4000, seed=7):
    """diffs_by_day: {day: [unit diffs]} -> (mean, lo, hi); resamples whole days."""
    days = sorted(diffs_by_day)
    flat = [x for d in days for x in diffs_by_day[d]]
    mean = sum(flat) / len(flat)
    rng = random.Random(seed)
    means = []
    for _ in range(B):
        pick = [days[rng.randrange(len(days))] for _ in days]
        v = [x for d in pick for x in diffs_by_day[d]]
        means.append(sum(v) / len(v))
    means.sort()
    return mean, means[int(0.025 * B)], means[int(0.975 * B) - 1]


def boot_station(diffs_by_station, B=4000, seed=11):
    return boot_day(diffs_by_station, B, seed)


def fmt(m, lo, hi, nd=4):
    star = " *" if (lo > 0 or hi < 0) else ""
    return f"{m:+.{nd}f} [{lo:+.{nd}f},{hi:+.{nd}f}]{star}"
