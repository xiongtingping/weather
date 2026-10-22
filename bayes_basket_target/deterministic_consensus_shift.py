"""Charter 279: the deterministic global consensus F as a P0 centre feature -- shadow.

User ruling 2026-10-22 「改，先影子，尝试测试逐站偏差！放在p0卡片里对比」 on research
补记十一 (``docs/research/p0_mode_hit_20261021``): the median of the deterministic
point forecasts knowable at a decision (charter 267's readers), with its own cold
bias corrected and a fleet-level kappa, moved the P0 centre toward the truth on
every walk-forward cut -- P0>36h LogScore -0.043 [-0.069, -0.024] over 18 event
days, P0-M -0.022, P1 nothing.  Charter 201's ``deterministic_centre_correction_v1``
computes the same feature per station x single source and admits one station in
48; this is its fleet-level consensus form.

    F      median of the roster's local-day maxima knowable at the decision
           (``deterministic_forecast_family.feature_at``), >= MIN_SYSTEMS of them
    F_cal  F + b, b the bias of F against the settled truth fitted on earlier
           event days -- the fleet's one number (``fleet``) or the station's own,
           shrunk toward the fleet's (``station``; the user's 「逐站偏差」 arm)
    delta  kappa * (F_cal - c), c the served mean of the vector being shadowed,
           kappa fitted through the origin on earlier event days, capped +/- CAP_C
    shadow floor(shift(served, delta)) -- the width layer's pure shift (charter
           190) on the served vector, charter 224's floor last; what the nightly
           fit scores and what this receipt records

One roster (user ruling 2026-10-22 「统一用十家」, charter 279 补记二): the four
systems 补记十一 read (charter 201's default readers) and the six native global
runs charter 267 captures for F.  补记一 read the four alone against the ten:
equal at P0, the ten better at P0-M (a steadier median, not coverage).  Two
variants = bias form; the fit scores each one walk-forward and the receipt
carries both, so the P0 card compares them.  The roster key stays ``wide`` so
the artifact and the receipts written before the ruling read unchanged.

This step records, it does not serve: every P0, P0-M and P1 compile carries
``deterministic_consensus_json`` and the served vector is untouched
(``served_delta_c`` 0).  Nothing in the shadow can stop a compile.  The daily
fit (``scripts/fit_deterministic_consensus_shift.py``) writes one JSON artifact
the P0 and P0-M compiles read; P1, whose envelope is rebuilt from its packet,
carries the read and its family's parameters on the packet (``packet_bundle``).
Serving is a later ruling, after >= 30 forward event days.
"""

from __future__ import annotations

import json
import math
import os
import random
import statistics
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .regional_point_lane import SYSTEM_IDS

RULE_VERSION = "deterministic_consensus_shift_v1"
LINEAGE_KEY = "deterministic_consensus_json"
CAP_C = 1.5
MIN_SYSTEMS = 3
#: Event days of settled record a stage family needs before a bias/kappa is fitted.
MIN_FIT_EVENT_DAYS = 8
#: The station bias is shrunk toward the fleet's: (n * mean_station + m * fleet) / (n + m).
STATION_BIAS_PRIOR_DAYS = 10.0
#: Charter 240 on the walk-forward: at least this many evaluable event days for a verdict.
VERDICT_MIN_EVENT_DAYS = 20
#: Charter 279 补记四 (user 2026-10-22 「把脚注改成只报 10-01 以后的同期口径」): the
#: walk-forward the board and the verdict read scores only decisions from here
#: on -- the serving chain the shadow would join (P0 v20+); August-September's
#: older versions served a P0 the consensus had far more to correct (补记一).
#: Parameters are still fitted on every earlier event day.
SCORE_FROM_DECISION_UTC = "2026-10-01T00:00:00Z"
BOOTSTRAP_DRAWS = 300
BOOTSTRAP_SEED = 279
ROOT = Path(__file__).resolve().parents[1]
FIT_ARTIFACT_PATH = Path(
    os.environ.get("PM_DETERMINISTIC_CONSENSUS_FIT")
    or ROOT / "data" / "runtime" / "bayes_basket_target_nwp" / "deterministic_consensus_fit.json"
)
FIT_SCHEMA = "bayes_basket_target_deterministic_consensus_fit_v1"
# Shadow: every compile records, none serves.
MODE = os.environ.get("PM_DETERMINISTIC_CONSENSUS_MODE", "shadow").strip().lower()

# The ten (user ruling 2026-10-22 「统一用十家」): 补记十一's four -- charter 201's
# default readers, the AIFS single member and the IFS 9 km / UKMO relays and CMA
# -- and the six native global deterministic runs charter 267 captures for F.
PRODUCTS: tuple[str, ...] = (
    "AIFS", "IFS9KM", "UKMO10", "CMA",
    "IFS_HRES", "GFS", "AIGFS", "GDPS", "ICON_GLOBAL", "UKMO_GLOBAL",
)
ROSTERS: Mapping[str, frozenset[str]] = {
    "wide": frozenset(SYSTEM_IDS[p] for p in PRODUCTS),
}
CONSENSUS_SYSTEM_IDS: frozenset[str] = ROSTERS["wide"]
PRODUCT_OF_SYSTEM = {SYSTEM_IDS[p]: p for p in PRODUCTS}
BIAS_FORMS: tuple[str, ...] = ("fleet", "station")
VARIANTS: tuple[str, ...] = tuple(f"{form}_wide" for form in BIAS_FORMS)
PRIMARY_VARIANT = "fleet_wide"
STAGE_FAMILIES: tuple[str, ...] = ("P0_far", "P0_near", "P0M", "P1")
FAR_HOURS = 36.0
NEAR_FLOOR_HOURS = 12.0
#: How each family's shadow treats the buckets the day's observed high has
#: already ruled out: P0-M censors them onto the lowest live bucket (charter
#: 241), P1 renormalises proportionally over the live ones (its own rule).
CENSOR_BY_FAMILY: Mapping[str, bool] = {"P0_far": False, "P0_near": False, "P0M": True, "P1": False}

_FIT_CACHE: dict[str, Any] = {}


def variant_parts(variant: str) -> tuple[str, str]:
    form, roster = str(variant).split("_", 1)
    if form not in BIAS_FORMS or roster not in ROSTERS:
        raise ValueError(f"unknown consensus variant: {variant}")
    return form, roster


def stage_family(stage: str, hours_to_settlement: float | None) -> str | None:
    """Which fitted family a decision belongs to; None where the feature is not read
    (P0 within 12 h of settlement, where P1 serves).  P1 joined on the user's ruling
    2026-10-22 「影子也要进p1」 (charter 279 补记三)."""

    if stage == "P0-M":
        return "P0M"
    if stage == "P1":
        return "P1"
    if stage != "P0" or hours_to_settlement is None:
        return None
    try:
        hours = float(hours_to_settlement)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(hours):
        return None
    if hours > FAR_HOURS:
        return "P0_far"
    if hours >= NEAR_FLOOR_HOURS:
        return "P0_near"
    return None


# ---------------------------------------------------------------------------
# The read: F per roster at a decision.


def consensus_from_features(features: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """F per roster from a ``feature_at`` result; the per-system values kept compactly."""

    systems: dict[str, dict[str, Any]] = {}
    for system_id, feature in sorted(features.items()):
        product = PRODUCT_OF_SYSTEM.get(str(system_id))
        if product is None:
            continue
        systems[product] = {
            "system_id": str(system_id),
            "day_max_c": round(float(feature["day_max_c"]), 3),
            "run_init_utc": str(feature.get("run_init_utc") or ""),
        }
    f_c: dict[str, float | None] = {}
    n: dict[str, int] = {}
    for roster, members in ROSTERS.items():
        values = [
            float(entry["day_max_c"]) for entry in systems.values() if entry["system_id"] in members
        ]
        n[roster] = len(values)
        f_c[roster] = round(statistics.median(values), 4) if len(values) >= MIN_SYSTEMS else None
    return {"systems": systems, "f_c": f_c, "n": n}


def consensus_read(
    conn: Any,
    *,
    station_id: str,
    event_date: str,
    as_of_utc: str,
    settlement_timezone: str,
    rows: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """The deterministic runs knowable at ``as_of_utc`` and F per roster.  Never raises."""

    from .deterministic_forecast_family import feature_at  # noqa: PLC0415

    try:
        features = feature_at(
            conn, station_id=station_id, event_date=event_date, as_of_utc=as_of_utc,
            settlement_timezone=settlement_timezone, systems=sorted(CONSENSUS_SYSTEM_IDS), rows=rows,
        )
        read = consensus_from_features(features)
        read.update(state="ok", as_of_utc=str(as_of_utc))
        return read
    except Exception as exc:  # noqa: BLE001 -- a shadow never stops a compile
        return {"state": "ERROR", "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                "systems": {}, "f_c": {r: None for r in ROSTERS}, "n": {r: 0 for r in ROSTERS}}


# ---------------------------------------------------------------------------
# The artifact.


def load_fit_artifact(path: Path | None = None) -> dict[str, Any] | None:
    """The daily fit, cached on its modification time."""

    target = Path(path or FIT_ARTIFACT_PATH)
    try:
        stamp = target.stat().st_mtime_ns
    except OSError:
        return None
    cached = _FIT_CACHE.get(str(target))
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != FIT_SCHEMA:
        return None
    _FIT_CACHE.clear()
    _FIT_CACHE[str(target)] = (stamp, payload)
    return payload


def family_parameters(fit: Mapping[str, Any] | None, family: str) -> dict[str, dict[str, Any]]:
    """Per variant the serving parameters the artifact fitted for ``family`` ({} when none)."""

    families = (fit or {}).get("families") or {}
    entry = families.get(family) or {}
    out: dict[str, dict[str, Any]] = {}
    for variant, params in (entry.get("variants") or {}).items():
        if isinstance(params, Mapping) and params.get("kappa") is not None and params.get("bias_c") is not None:
            out[str(variant)] = dict(params)
    return out


def bias_for(params: Mapping[str, Any], *, form: str, station_id: str) -> tuple[float, str]:
    """The bias this variant applies at the station, and where it came from."""

    fleet = float(params["bias_c"])
    if form == "fleet":
        return fleet, "fleet"
    per_station = params.get("station_bias_c") or {}
    value = per_station.get(str(station_id).upper())
    if value is None:
        return fleet, "fleet_fallback"
    return float(value), "station"


# ---------------------------------------------------------------------------
# The shadow.


def served_mean_c(
    probability: Mapping[str, float], ordered: Sequence[str],
    intervals: Mapping[str, Sequence[float | None]],
) -> float:
    from .deterministic_feature_correction import served_mean_c as _mean  # noqa: PLC0415

    return _mean(probability, ordered, intervals)


def shift_served(
    probability: Mapping[str, float], ordered: Sequence[str],
    intervals: Mapping[str, Sequence[float | None]], delta_c: float,
    *, excluded: Sequence[str] = (), censor: bool = False,
) -> dict[str, float]:
    """The shadow vector: the width layer's pure shift (capped), the day's observed
    bound re-applied, then charter 224's floor on the live buckets.

    A cold shift moves mass onto buckets the observed high has already ruled
    out; ``censor`` puts it on the lowest live bucket (P0-M, charter 241),
    otherwise it is dropped and the live buckets renormalised (P1).
    """

    from .deterministic_feature_correction import shift_distribution  # noqa: PLC0415
    from .materialization import floor_probabilities  # noqa: PLC0415

    shifted = shift_distribution(probability, ordered, intervals, delta_c)
    dead = {str(b) for b in excluded} & set(ordered)
    live = [key for key in ordered if key not in dead]
    if dead and live:
        lost = sum(float(shifted[key]) for key in dead)
        out = {key: (0.0 if key in dead else float(shifted[key])) for key in ordered}
        if censor:
            out[live[0]] += lost
        else:
            mass = sum(out[key] for key in live)
            if mass > 0.0:
                out = {key: (out[key] / mass if key in live else 0.0) for key in ordered}
        shifted = out
    floored, _buckets = floor_probabilities(shifted, dead)
    return {key: float(floored[key]) for key in ordered}


def mode_bucket(probability: Mapping[str, float], ordered: Sequence[str]) -> str | None:
    best = None
    for key in ordered:
        value = float(probability.get(key, 0.0))
        if best is None or value > best[1]:
            best = (key, value)
    return None if best is None else best[0]


def shadow_receipt(
    *,
    read: Mapping[str, Any],
    fit: Mapping[str, Any] | None,
    family: str | None,
    station_id: str,
    served_probability: Mapping[str, float],
    ordered: Sequence[str],
    intervals: Mapping[str, Sequence[float | None]],
    with_vectors: bool = True,
    excluded: Sequence[str] = (),
) -> dict[str, Any]:
    """What the consensus would move this vector by under each variant, and the vector it would serve.

    Pure: the read and the fit come in.  ``excluded`` is the stage's observed
    hard bound, re-applied to each shadow by the family's rule.  Never raises.
    """

    receipt: dict[str, Any] = {
        "rule": RULE_VERSION, "mode": MODE, "state": "SHADOW", "cap_c": CAP_C,
        "family": family, "primary": PRIMARY_VARIANT, "served_delta_c": 0.0,
        "read_state": read.get("state"), "systems": read.get("systems") or {},
        "f_c": read.get("f_c") or {}, "n": read.get("n") or {}, "variants": {},
        "fit_fitted_at_utc": (fit or {}).get("fitted_at_utc"),
    }
    try:
        if read.get("state") != "ok":
            receipt["state"] = "NO_READ"
            receipt["error"] = read.get("error")
            return receipt
        centre = served_mean_c(served_probability, ordered, intervals)
        receipt["centre_c"] = round(centre, 4)
        if family is None:
            receipt["state"] = "NOT_READ_AT_STAGE"
            return receipt
        params_by_variant = family_parameters(fit, family)
        for variant in VARIANTS:
            form, roster = variant_parts(variant)
            entry: dict[str, Any] = {"form": form, "roster": roster}
            f_value = (read.get("f_c") or {}).get(roster)
            params = params_by_variant.get(variant)
            if f_value is None:
                entry["state"] = "TOO_FEW_SYSTEMS"
            elif params is None:
                entry["state"] = "NO_FIT"
                entry["f_c"] = float(f_value)
            else:
                bias, bias_source = bias_for(params, form=form, station_id=station_id)
                kappa = float(params["kappa"])
                f_cal = float(f_value) + bias
                gap = f_cal - centre
                raw = kappa * gap
                delta = max(-CAP_C, min(CAP_C, raw))
                shadow = shift_served(served_probability, ordered, intervals, delta, excluded=excluded,
                                      censor=CENSOR_BY_FAMILY.get(family, False))
                entry.update(
                    state="READ", f_c=float(f_value), bias_c=round(bias, 4), bias_source=bias_source,
                    kappa=round(kappa, 4), f_cal_c=round(f_cal, 4), gap_c=round(gap, 4),
                    delta_c=round(delta, 4), clipped=abs(raw) > CAP_C,
                    shadow_centre_c=round(served_mean_c(shadow, ordered, intervals), 4),
                    shadow_mode=mode_bucket(shadow, ordered),
                )
                if with_vectors:
                    entry["shadow_probabilities"] = {key: round(float(shadow[key]), 8) for key in ordered}
            receipt["variants"][variant] = entry
        if excluded:
            receipt["excluded"] = sorted({str(b) for b in excluded} & set(ordered), key=list(ordered).index)
        if not params_by_variant:
            receipt["state"] = "NO_FIT"
    except Exception as exc:  # noqa: BLE001 -- a shadow never stops a compile
        receipt["state"] = "ERROR"
        receipt["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    return receipt


def shadow_receipt_from_conn(
    conn: Any,
    *,
    station_id: str,
    event_date: str,
    decision_at_utc: str,
    settlement_timezone: str,
    stage: str,
    hours_to_settlement: float | None,
    served_probability: Mapping[str, float],
    ordered: Sequence[str],
    intervals: Mapping[str, Sequence[float | None]],
    rows: Sequence[Mapping[str, Any]] | None = None,
    fit: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The P0 compile's one call: read, then the receipt."""

    read = consensus_read(
        conn, station_id=station_id, event_date=event_date, as_of_utc=decision_at_utc,
        settlement_timezone=settlement_timezone, rows=rows,
    )
    return shadow_receipt(
        read=read, fit=fit if fit is not None else load_fit_artifact(),
        family=stage_family(stage, hours_to_settlement), station_id=station_id,
        served_probability=served_probability, ordered=ordered, intervals=intervals,
    )


def packet_bundle(
    read: Mapping[str, Any], *, fit: Mapping[str, Any] | None, family: str, station_id: str,
) -> dict[str, Any]:
    """What a stage whose envelope is rebuilt from its packet (P1) carries: the read
    and only the fitted parameters this station-day's family reads -- the station's
    own bias, not the fleet's 48 -- so the rebuild never reads the artifact again."""

    variants: dict[str, Any] = {}
    for variant, params in family_parameters(fit, family).items():
        entry = {"bias_c": params["bias_c"], "kappa": params["kappa"]}
        own = (params.get("station_bias_c") or {}).get(str(station_id).upper())
        if own is not None:
            entry["station_bias_c"] = {str(station_id).upper(): own}
        variants[variant] = entry
    return {
        "read": dict(read),
        "fit": {"schema": FIT_SCHEMA, "fitted_at_utc": (fit or {}).get("fitted_at_utc"),
                "families": {family: {"variants": variants}}},
        "family": family,
    }


# ---------------------------------------------------------------------------
# The fit: bias and kappa per family and variant, walk-forward scored.
#
# A record is one first decision of a settled station-day:
#   {"station", "day", "family", "centre_c", "f_c": {"wide"}, "y_c",
#    "p": {bucket: prob}, "ordered": [...], "intervals": {bucket: [lo, hi]}, "winner": bucket,
#    "excluded": [buckets the observed high had ruled out at the decision]}


def _fit_bias(records: Sequence[Mapping[str, Any]], *, roster: str, form: str) -> tuple[float, dict[str, float]]:
    pairs = [(str(r["station"]).upper(), float(r["y_c"]) - float(r["f_c"][roster])) for r in records]
    fleet = statistics.mean(v for _s, v in pairs)
    if form == "fleet":
        return fleet, {}
    by_station: dict[str, list[float]] = {}
    for station, value in pairs:
        by_station.setdefault(station, []).append(value)
    m = STATION_BIAS_PRIOR_DAYS
    shrunk = {
        station: (sum(values) + m * fleet) / (len(values) + m) for station, values in by_station.items()
    }
    return fleet, shrunk


def fit_parameters(
    records: Sequence[Mapping[str, Any]], *, family: str, before_day: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Per variant: the fleet bias, the station biases (station forms) and kappa, on
    the family's records before ``before_day`` (all of them when None)."""

    out: dict[str, dict[str, Any]] = {}
    for variant in VARIANTS:
        form, roster = variant_parts(variant)
        prior = [
            r for r in records
            if r["family"] == family and r["f_c"].get(roster) is not None
            and (before_day is None or str(r["day"]) < str(before_day))
        ]
        days = {str(r["day"]) for r in prior}
        if len(days) < MIN_FIT_EVENT_DAYS:
            continue
        fleet, shrunk = _fit_bias(prior, roster=roster, form=form)
        num = den = 0.0
        for r in prior:
            bias = shrunk.get(str(r["station"]).upper(), fleet) if form == "station" else fleet
            gap = float(r["f_c"][roster]) + bias - float(r["centre_c"])
            num += gap * (float(r["y_c"]) - float(r["centre_c"]))
            den += gap * gap
        kappa = max(0.0, min(1.0, num / den)) if den > 0 else 0.0
        entry: dict[str, Any] = {
            "bias_c": round(fleet, 4), "kappa": round(kappa, 4),
            "n_decisions": len(prior), "n_days": len(days),
            "fit_through_day": max(days),
        }
        if form == "station":
            entry["station_bias_c"] = {s: round(v, 4) for s, v in sorted(shrunk.items())}
            entry["station_bias_prior_days"] = STATION_BIAS_PRIOR_DAYS
        out[variant] = entry
    return out


def _logscore(p: Mapping[str, float], winner: str) -> float:
    return -math.log(max(float(p.get(winner, 0.0)), 1e-12))


def _brier(p: Mapping[str, float], ordered: Sequence[str], winner: str) -> float:
    return sum((float(p.get(k, 0.0)) - (1.0 if k == winner else 0.0)) ** 2 for k in ordered)


def _crps_rungs(p: Mapping[str, float], ordered: Sequence[str], winner: str) -> float:
    """The CRPS on the rung axis: squared error of the cumulative, summed over rungs."""

    target = list(ordered).index(winner)
    cumulative = 0.0
    total = 0.0
    for i, key in enumerate(ordered):
        cumulative += float(p.get(key, 0.0))
        total += (cumulative - (1.0 if i >= target else 0.0)) ** 2
    return total


def score_record(record: Mapping[str, Any], shadow: Mapping[str, float]) -> dict[str, float]:
    ordered = list(record["ordered"])
    winner = str(record["winner"])
    served = record["p"]
    return {
        "logscore_before": _logscore(served, winner), "logscore_after": _logscore(shadow, winner),
        "brier_before": _brier(served, ordered, winner), "brier_after": _brier(shadow, ordered, winner),
        "crps_before": _crps_rungs(served, ordered, winner), "crps_after": _crps_rungs(shadow, ordered, winner),
    }


def _bootstrap_ci(by_day: Mapping[str, Sequence[float]], *, draws: int, seed: int) -> tuple[float, float]:
    days = sorted(by_day)
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(draws):
        sample = [v for d in (rng.choice(days) for _ in days) for v in by_day[d]]
        means.append(statistics.mean(sample))
    means.sort()
    lo = means[int(0.025 * (len(means) - 1))]
    hi = means[int(0.975 * (len(means) - 1))]
    return lo, hi


def verdict_of(summary: Mapping[str, Any]) -> str:
    """Charter 240 on the walk-forward: the LogScore change's upper bound below zero, the
    CRPS and the Brier not significantly worse, >= VERDICT_MIN_EVENT_DAYS evaluable days."""

    if int(summary.get("n_days") or 0) < VERDICT_MIN_EVENT_DAYS:
        return "INSUFFICIENT"
    ls = summary["logscore"]
    if not (ls["ci"][1] < 0.0):
        return "REFUSED"
    for score in ("crps", "brier"):
        if summary[score]["ci"][0] > 0.0:
            return "REFUSED"
    return "PASS"


def walk_forward(
    records: Sequence[Mapping[str, Any]], *, family: str,
    shift: Callable[..., Mapping[str, float]] | None = None,
    score_from_utc: str | None = None,
) -> dict[str, Any]:
    """Each event day scored under the parameters fitted on the days before it, per variant.

    ``score_from_utc`` scores only decisions at or after that instant; the
    parameters are still fitted on every earlier event day, scored or not.
    """

    shift_fn = shift or shift_served
    family_records = [r for r in records if r["family"] == family]
    days = sorted({
        str(r["day"]) for r in family_records
        if score_from_utc is None or str(r.get("decision_at_utc") or "") >= score_from_utc
    })
    per_variant: dict[str, dict[str, Any]] = {
        v: {"by_day": {}, "by_station": {}, "sums": {k: 0.0 for k in (
            "logscore_before", "logscore_after", "brier_before", "brier_after", "crps_before", "crps_after")},
            "n": 0}
        for v in VARIANTS
    }
    for day in days:
        params = fit_parameters(family_records, family=family, before_day=day)
        if not params:
            continue
        for record in (r for r in family_records if str(r["day"]) == day):
            if score_from_utc is not None and str(record.get("decision_at_utc") or "") < score_from_utc:
                continue
            centre = float(record["centre_c"])
            for variant, p in params.items():
                form, roster = variant_parts(variant)
                f_value = record["f_c"].get(roster)
                if f_value is None:
                    continue
                bias, _src = bias_for(p, form=form, station_id=str(record["station"]))
                delta = max(-CAP_C, min(CAP_C, float(p["kappa"]) * (float(f_value) + bias - centre)))
                shadow = shift_fn(record["p"], record["ordered"], record["intervals"], delta,
                                  excluded=record.get("excluded") or (),
                                  censor=CENSOR_BY_FAMILY.get(family, False))
                s = score_record(record, shadow)
                slot = per_variant[variant]
                for k, v in s.items():
                    slot["sums"][k] += v
                slot["n"] += 1
                d_ls = s["logscore_after"] - s["logscore_before"]
                slot["by_day"].setdefault(day, []).append(d_ls)
                slot["by_station"].setdefault(str(record["station"]).upper(), []).append(d_ls)
                slot.setdefault("crps_by_day", {}).setdefault(day, []).append(s["crps_after"] - s["crps_before"])
                slot.setdefault("brier_by_day", {}).setdefault(day, []).append(s["brier_after"] - s["brier_before"])
    out: dict[str, Any] = {}
    for variant, slot in per_variant.items():
        n = slot["n"]
        if n == 0:
            out[variant] = {"n": 0, "n_days": 0, "verdict": "INSUFFICIENT"}
            continue
        sums = slot["sums"]
        summary: dict[str, Any] = {"n": n, "n_days": len(slot["by_day"])}
        for score, by_day_key in (("logscore", "by_day"), ("crps", "crps_by_day"), ("brier", "brier_by_day")):
            before = sums[f"{score}_before"] / n
            after = sums[f"{score}_after"] / n
            lo, hi = _bootstrap_ci(slot[by_day_key], draws=BOOTSTRAP_DRAWS, seed=BOOTSTRAP_SEED)
            summary[score] = {"before": round(before, 5), "after": round(after, 5),
                              "delta": round(after - before, 5), "ci": [round(lo, 5), round(hi, 5)]}
        summary["days_better"] = sum(1 for v in slot["by_day"].values() if statistics.mean(v) < 0)
        summary["stations_better"] = sum(1 for v in slot["by_station"].values() if statistics.mean(v) < 0)
        summary["stations"] = {
            s: {"n": len(v), "delta_logscore": round(statistics.mean(v), 4)}
            for s, v in sorted(slot["by_station"].items())
        }
        summary["verdict"] = verdict_of(summary)
        out[variant] = summary
    return out


def finite_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


__all__ = [
    "BIAS_FORMS", "CAP_C", "CENSOR_BY_FAMILY", "CONSENSUS_SYSTEM_IDS", "FIT_ARTIFACT_PATH", "FIT_SCHEMA",
    "LINEAGE_KEY", "MIN_FIT_EVENT_DAYS", "MIN_SYSTEMS", "MODE", "PRIMARY_VARIANT", "PRODUCTS",
    "PRODUCT_OF_SYSTEM", "ROSTERS", "RULE_VERSION", "SCORE_FROM_DECISION_UTC", "STAGE_FAMILIES",
    "STATION_BIAS_PRIOR_DAYS", "VARIANTS",
    "VERDICT_MIN_EVENT_DAYS", "bias_for", "consensus_from_features", "consensus_read",
    "family_parameters", "fit_parameters", "load_fit_artifact", "mode_bucket", "packet_bundle", "score_record",
    "served_mean_c", "shadow_receipt", "shadow_receipt_from_conn", "shift_served", "stage_family",
    "variant_parts", "verdict_of", "walk_forward",
]
