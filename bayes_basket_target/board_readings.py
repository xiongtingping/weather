"""Board readings: what the calibration board shows that production's serving
path does not store, computed here, off the board.

User rulings 2026-10-02 「所有前端都改为只读」 / 「只读是读后端算出来的」 /
「如果后端没有的，就增加前端需要的数据」 / 「新开一个后端读数任务」.  The board
used to compute these itself by calling production's own functions; it now
only reads the files this job writes (``data/board_readings/<name>.json``),
and ``tests/test_board_is_read_only.py`` keeps it that way.

Five readings, each with production's functions and nothing re-derived:

* ``seat_windows`` -- charter 131's seat window for every served cell the
  board's landing tables read (``serving_cells_by_day``), not only the
  envelopes production decided on.  Incremental: a cell is keyed on (station,
  event date, decision instant) and computed once.
* ``fusion_resolve`` -- each served seat's weights re-solved from what the
  SSOT kept, by production's weighting functions, and the distance to the
  stored weights (the consistency self-check).
* ``centre_shape`` -- the P0 gap to the market split into centre and shape.
* ``l4_lane`` -- the L4 rows' forward skill and the settled-window
  realisation table.
* ``basket_shadow`` -- the basket's P0 ladder per station-day (window, legs,
  gate) from the preselection plans production compiled, and how its
  choices settled.  Incremental: a rowid cursor, chunked short reads.

Read-only against the SSOT (``mode=ro``); the job is a reader beside the
writer (charter: read-only work runs on its own executor, never in a writer
slot) and is scheduled by ``scripts/board_readings_sweep.sh``.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import random
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
READINGS_DIR = Path(
    os.environ.get("PM_BOARD_READINGS_DIR") or (ROOT / "data" / "board_readings")
)
DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
SCHEMA = "board_reading_v1"
#: How far back the seat windows reach: the scorecard's own longest window.
SEAT_LOOKBACK_DAYS = 45
#: How far before the last run's start the seat windows are read again: a
#: three-hour block's median cell can move until the block closes, plus margin.
SEAT_REREAD_HOURS = 6
READING_NAMES = (
    "seat_windows", "fusion_resolve", "centre_shape", "l4_lane", "basket_shadow",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _code_sha(*objects: Any) -> str:
    text = "".join(inspect.getsource(obj) for obj in objects)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_reading(name: str, value: Any, *, code: str, build_sec: float) -> None:
    """Atomically replace ``<name>.json``."""

    READINGS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": SCHEMA,
        "name": name,
        "computed_at_utc": _now().isoformat().replace("+00:00", "Z"),
        "build_sec": round(float(build_sec), 1),
        "code_sha256": code,
        "value": value,
    }
    tmp = READINGS_DIR / f"{name}.json.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
    tmp.replace(READINGS_DIR / f"{name}.json")


def load_reading(name: str) -> dict[str, Any] | None:
    """The stored reading, or None when this job has not written it."""

    try:
        payload = json.loads((READINGS_DIR / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        return None
    return payload


# ---------------------------------------------------------------------------
# the seats P0 actually serves
# ---------------------------------------------------------------------------

#: Event days (today and after) whose served P0 seats the board reads.
SERVED_SEAT_DAYS = 3


def served_p0_seats(
    conn: sqlite3.Connection, *, days: int = SERVED_SEAT_DAYS,
) -> list[dict[str, Any]]:
    """The seat each open station-day's newest P0 was served from, as stored.

    Read off the envelope's own lineage (``native_cross_ensemble_artifact_
    sha256`` / ``native_cross_activation_sha256``) to the artifact and the
    selection evidence, all by content sha.  Until 2026-10-02 the board and
    this job took ``MAX(generation)`` per (station, season, lead, cycle) as
    "the served seat"; since charter 198 a scope holds hundreds of valid
    pairings and P0 ranks them (usable, pool, exact, breadth ...), so the
    highest generation was mostly some two-system re-mint -- 706 of 743
    rows showed two systems while the served P0s carried six.
    """

    today = _now().date()
    seats: list[dict[str, Any]] = []
    for offset in range(days):
        day = (today + timedelta(days=offset)).isoformat()
        for station, decided in conn.execute(
            "SELECT station_id, MAX(decision_at_utc) FROM basket_probability_envelope"
            " WHERE state='COMMITTED' AND event_date=? AND stage='P0'"
            " GROUP BY station_id",
            (day,),
        ).fetchall():
            lineage = conn.execute(
                "SELECT json_extract(lineage_json,'$.native_cross_ensemble_artifact_sha256'),"
                " json_extract(lineage_json,'$.native_cross_activation_sha256')"
                " FROM basket_probability_envelope WHERE station_id=? AND event_date=?"
                " AND stage='P0' AND state='COMMITTED' AND decision_at_utc=? LIMIT 1",
                (station, day, decided),
            ).fetchone()
            if not lineage or not lineage[0]:
                continue
            artifact = conn.execute(
                "SELECT cross_ensemble_artifact_id, season_bin, lead_bucket, run_cycle,"
                " combination_method, selection_evidence_sha256"
                " FROM calibration_cross_native_ensemble_artifact WHERE content_sha256=?",
                (str(lineage[0]),),
            ).fetchone()
            if artifact is None:
                continue
            activation = conn.execute(
                "SELECT available_system_set_hash_or_star"
                " FROM calibration_native_ensemble_activation WHERE content_sha256=?",
                (str(lineage[1] or ""),),
            ).fetchone()
            evidence = conn.execute(
                "SELECT content_sha256, selected_weights_json,"
                " cross_system_residual_correlation_json, run_cycle_by_system_json,"
                " family_weight_caps_json, train_end_date, candidate_system_ids_json,"
                " selected_system_ids_json, holdout_end_date, selected_system_count"
                " FROM calibration_cross_native_ensemble_selection_evidence"
                " WHERE content_sha256=?",
                (str(artifact[5]),),
            ).fetchone()
            if evidence is None:
                continue
            seats.append({
                "station_id": str(station), "event_date": day,
                "decision_at_utc": str(decided),
                "artifact_id": str(artifact[0]),
                "season_bin": str(artifact[1]), "lead_bucket": str(artifact[2]),
                "run_cycle": str(artifact[3]), "combination_method": artifact[4],
                "set_hash": None if activation is None else activation[0],
                "evidence_sha256": evidence[0],
                "selected_weights_json": evidence[1],
                "correlation_json": evidence[2],
                "run_cycle_by_system_json": evidence[3],
                "family_weight_caps_json": evidence[4],
                "train_end_date": evidence[5],
                "candidate_system_ids_json": evidence[6],
                "selected_system_ids_json": evidence[7],
                "holdout_end_date": evidence[8],
                "selected_system_count": evidence[9],
            })
    return seats


# ---------------------------------------------------------------------------
# seat_windows
# ---------------------------------------------------------------------------

def seat_window(
    *,
    stage: str,
    keys: Sequence[str],
    probabilities: Mapping[str, float],
    excluded: Sequence[str] = (),
) -> dict[str, Any] | None:
    """The seat window charter 131 rules for one served cell.

    Read off the runtime's own functions rather than re-derived here (a
    panel that copied the seat rule would drift the day the rule moved):
    the centre is the mass-weighted mean bucket snapped to the nearest live
    rung (charter 54/75), the shape is fixed by the three-state reading at
    0.35 sigma -- symmetric ``c+-1``; skewed ``{c, tail, tail+1}`` with the
    mode bucket outside by ruling -- and a P1 day left with one live rung
    seats that rung alone.  The widest ruled shape whose rungs are all live
    is the window reported; production seats the two-rung subset only when
    the wider one fails a gate, which a page without prices cannot see.

    ``None`` when the runtime would seat nothing: no live rung, or every
    ruled shape reaches off the ladder or onto a dead rung.  Until
    2026-09-12 this panel hard-coded the mean bucket +-1 (charter 49/79,
    both retired), which on a skewed day is not the seat at all.
    """

    try:                                             # noqa: PLC0415
        from bayes_basket_target.delta_v2_runtime import (
            LADDER_CENTER_DEFINITION,
            _alternate_ladder_center,
            _skew_extension_sign,
            seat_shape_windows_v1,
        )
    except ImportError:
        return None
    ordinals = {str(key): index for index, key in enumerate(keys)}
    dead = {str(name) for name in excluded}
    admitted = [
        str(key) for key in keys
        if float(probabilities.get(key, 0.0) or 0.0) > 0.0
        and str(key) not in dead
    ]
    if not admitted:
        return None
    # The runtime's own order: heaviest live rung as the seed, the ruled
    # definition (mean, since charter 54) applied over it.
    centre = max(
        admitted,
        key=lambda bucket: (
            float(probabilities.get(bucket, 0.0) or 0.0), -ordinals[bucket]
        ),
    )
    if LADDER_CENTER_DEFINITION != "heaviest":
        centre = _alternate_ladder_center(
            LADDER_CENTER_DEFINITION,
            admitted=admitted,
            probabilities=probabilities,
            ordinals=ordinals,
        ) or centre
    sign = _skew_extension_sign(probabilities, ordinals)
    live = {ordinals[bucket] for bucket in admitted}
    _shapes, audit = seat_shape_windows_v1(
        centre_ordinal=ordinals[centre],
        sign=sign,
        stage=str(stage),
        probability_by_ordinal={
            ordinals[key]: float(probabilities.get(key, 0.0) or 0.0)
            for key in keys
        },
        live_ordinals=live,
    )
    by_ordinal = {index: key for key, index in ordinals.items()}
    for shape in audit.get("allowed_ordinals") or []:
        if all(int(ordinal) in live for ordinal in shape):
            return {
                "state": str(audit.get("state") or ""),
                "sign": int(sign),
                "centre": centre,
                "seat_keys": [by_ordinal[int(o)] for o in shape],
            }
    return None


def seat_windows(conn: sqlite3.Connection) -> dict[str, Any]:
    """``{"station|event_date|decision_at": {state, sign, seat_keys} | None}``
    for every served cell of the last ``SEAT_LOOKBACK_DAYS``, keeping what an
    earlier run computed (a cell's probabilities are immutable)."""

    from .center_width_store import _truth, serving_cells_by_day  # noqa: PLC0415

    previous = load_reading("seat_windows") or {}
    # Keyed on the rule alone: the values are seat_window's, whatever reads them.
    same_code = previous.get("code_sha256") == _code_sha(seat_window)
    kept: dict[str, Any] = (
        dict((previous.get("value") or {}).get("seats") or {}) if same_code else {}
    )
    floor = (_now() - timedelta(days=SEAT_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Only the cells that can still change are read again.  A cell is the
    # median row of a three-hour block, and a block takes new rows only while
    # it is current, so every cell decided more than SEAT_REREAD_HOURS before
    # the last run began is fixed and kept as written.  Reading all 45 days
    # of all three stages every hour was the job's whole cost, and it fell
    # inside production's cycle (2026-10-02 13:27Z: the first cycle after the
    # 13:30Z restart ran past its 720 s alarm beside it).
    reread_from = floor
    if same_code and previous.get("computed_at_utc"):
        try:
            last = datetime.fromisoformat(
                str(previous["computed_at_utc"]).replace("Z", "+00:00")
            ) - timedelta(seconds=float(previous.get("build_sec") or 0.0))
            reread_from = max(
                floor,
                (last - timedelta(hours=SEAT_REREAD_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        except ValueError:
            reread_from = floor
    seats: dict[str, Any] = {
        key: value for key, value in kept.items()
        if key.split("|")[-1] >= floor and key.split("|")[-1] < reread_from
    }
    truth = _truth(conn)
    computed = 0
    for stage in ("P0", "P0-M", "P1"):
        for cells in serving_cells_by_day(
            conn, truth, stage=stage, min_decision_at_utc=reread_from,
        ).values():
            for cell in cells:
                key = "|".join((
                    str(cell["station_id"]), str(cell["event_date"]),
                    str(cell.get("decision_at_utc") or ""),
                ))
                if key in kept:
                    seats[key] = kept[key]
                    continue
                seat = seat_window(
                    stage=stage,
                    keys=cell["ordered_keys"],
                    probabilities=cell["probabilities"],
                    excluded=cell.get("excluded_buckets") or (),
                )
                seats[key] = None if seat is None else {
                    "state": seat["state"], "sign": int(seat["sign"]),
                    "seat_keys": list(seat["seat_keys"]),
                }
                computed += 1
    return {
        "seats": seats, "computed": computed, "floor_utc": floor,
        "reread_from_utc": reread_from,
    }


# ---------------------------------------------------------------------------
# fusion_resolve
# ---------------------------------------------------------------------------

def _fusion_synthetic_residuals(
    systems: Sequence[str],
    correlation: Mapping[str, Mapping[str, float]],
    sigmas: Sequence[float],
) -> dict[str, dict[str, float]] | None:
    """Residual series whose population covariance is exactly ``D R D``.

    Production weighs a seat from per-day train residuals.  The SSOT keeps
    what those residuals summarise -- the correlation matrix on the evidence
    row, and each system's variance through the inverse-variance shadow --
    but not the days.  Re-deriving the solver from those pieces would put a
    second copy of the rule on this page, and the copy that stood here until
    2026-09-17 had drifted three ways: it refused a lone active system where
    production allows one, ignored the family caps, and scaled by train RMSE
    where production scales by the residual sigma.  So the covariance is
    turned back into series that carry it exactly, and those are handed to
    the production function unchanged.

    Column ``m`` is ``sqrt(2) cos(2 pi (m+1) t / n)``: zero mean, mean square
    one, and orthogonal to its siblings while ``n > 2k``.  With ``S = L L^T``
    the rows ``x_t = L z_t`` have population covariance ``S``, the normaliser
    production divides by.  ``None`` when the stored correlation is not
    positive definite, which a matrix built from pairwise day sets can be.
    """

    from bayes_basket_target.native_ensemble_oos import (  # noqa: PLC0415
        MINIMUM_VARIANCE_MIN_DAYS,
    )

    size = len(systems)
    if size < 2 or len(sigmas) != size:
        return None
    try:
        covariance = [
            [
                float(correlation[left][right])
                * float(sigmas[row])
                * float(sigmas[column])
                for column, right in enumerate(systems)
            ]
            for row, left in enumerate(systems)
        ]
    except (KeyError, TypeError, ValueError):
        return None
    lower = [[0.0] * size for _ in range(size)]
    for row in range(size):
        for column in range(row + 1):
            value = covariance[row][column] - sum(
                lower[row][term] * lower[column][term]
                for term in range(column)
            )
            if row == column:
                if value <= 1e-12:
                    return None
                lower[row][row] = math.sqrt(value)
            else:
                lower[row][column] = value / lower[column][column]
    days = max(MINIMUM_VARIANCE_MIN_DAYS, 2 * size + 2)
    basis = [
        [
            math.sqrt(2.0) * math.cos(2.0 * math.pi * (term + 1) * day / days)
            for term in range(size)
        ]
        for day in range(days)
    ]
    return {
        system: {
            f"synthetic-{day:02d}": sum(
                lower[row][term] * basis[day][term]
                for term in range(row + 1)
            )
            for day in range(days)
        }
        for row, system in enumerate(systems)
    }


def _fusion_reconstruction(
    *,
    served: Mapping[str, float],
    correlation: Mapping[str, Mapping[str, float]],
    inverse_variance: Mapping[str, float] | None,
    member_scale: Mapping[str, float],
    member_train_n: Mapping[str, int | None],
    family_by_system: Mapping[str, str],
    family_caps: Mapping[str, float],
    kappa: float | None,
) -> dict[str, Any]:
    """A served seat's weights, re-solved by production's own functions.

    Every input is what the SSOT kept of what production read:

    * ``inverse_variance`` is the evaluation's shadow scheme, which is
      ``_minimum_variance_weights`` at shrinkage 1.0 over the same train
      series, so its shares are ``1/var`` of exactly the diagonal the served
      solve used.  Absent means that call returned nothing: the shared train
      window was under ``MINIMUM_VARIANCE_MIN_DAYS`` and production weighed
      the seat by inverse error.
    * ``member_scale`` and ``member_train_n`` are the member artifact's
      residual sigma and fit case count, which is what that fallback reads.
      The artifact persists the serving fit while the seat was weighed on
      the gate fit, a few days shorter, so a fallback seat re-solves only
      approximately.
    * ``kappa`` is parsed from the artifact's combination method, so a pool
      seat is shrunk by the value it was minted with.

    ``basis`` says which reconstruction this was: ``exact`` when the
    diagonal came from the shadow; ``capped`` when the shadow's shares had
    hit a family cap, which hides the variance ratio, so the member sigma
    stands in; ``fallback`` when the seat was weighed by inverse error; and
    ``non_psd`` when the stored correlation admits no covariance.  Only an
    ``exact`` distance checks production -- the others measure how much of
    it this page can reconstruct.
    """

    from bayes_basket_target.contracts import ContractError  # noqa: PLC0415
    from bayes_basket_target.native_ensemble_oos import (  # noqa: PLC0415
        SEAT_WEIGHT_RULE_INVERSE_RMSE,
        _seat_weights,
    )
    from bayes_basket_target.stable_pool import (  # noqa: PLC0415
        stable_pool_weights,
    )

    systems = sorted(served)
    families = {system: family_by_system[system] for system in systems}
    residuals: dict[str, dict[str, float]] = {system: {} for system in systems}
    basis = "fallback"
    if inverse_variance:
        shares = {
            system: float(inverse_variance.get(system) or 0.0)
            for system in systems
        }
        family_share: dict[str, float] = defaultdict(float)
        for system, share in shares.items():
            family_share[families[system]] += share
        capped = any(
            abs(share - float(family_caps.get(family, 0.75))) <= 1e-9
            for family, share in family_share.items()
        )
        if capped or min(shares.values()) <= 0.0:
            basis = "capped"
            sigmas = [float(member_scale[system]) for system in systems]
        else:
            basis = "exact"
            sigmas = [1.0 / math.sqrt(shares[system]) for system in systems]
        synthetic = _fusion_synthetic_residuals(systems, correlation, sigmas)
        if synthetic is None:
            return {"basis": "non_psd", "weights": None, "skill_rule": None}
        residuals = synthetic
    try:
        weights, skill_rule = _seat_weights(
            residuals_by_system=residuals,
            train_error_scale_by_system={
                system: float(member_scale[system]) for system in systems
            },
            family_by_system=families,
            family_caps=family_caps,
            train_n_by_system={
                system: member_train_n.get(system) for system in systems
            },
        )
        if kappa is not None:
            weights = stable_pool_weights(weights, families, kappa=kappa)
    except (ContractError, KeyError, ValueError, ZeroDivisionError):
        return {"basis": basis, "weights": None, "skill_rule": None}
    if skill_rule == SEAT_WEIGHT_RULE_INVERSE_RMSE:
        basis = "fallback"
    return {"basis": basis, "weights": dict(weights), "skill_rule": skill_rule}


def _fusion_rule_label(kappa: float | None, skill_rule: str | None) -> str:
    from bayes_basket_target.native_ensemble_oos import (  # noqa: PLC0415
        SEAT_WEIGHT_RULE_COVARIANCE,
    )

    if skill_rule is None:
        return "—"
    covariance = skill_rule == SEAT_WEIGHT_RULE_COVARIANCE
    if kappa is not None:
        return f"稳定池 κ={kappa:g} · {'协方差' if covariance else '逆误差'}"
    return "最小方差" if covariance else "逆误差兜底"


def fusion_resolved_seats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Whether each served blend carries the weights production's rule gives.

    Until 2026-09-17 this compared the served weights against a minimum-
    variance optimum solved here from train RMSE, and three things had moved
    under it.  Charter 134 (2026-09-14) shrinks every pool seat halfway
    toward family-balanced weights, so a gap of about 20% was the design
    rather than a finding.  Production had weighed on the residual sigma
    since the bias correction, so a system with a large offset but a steady
    error -- GEFS at OEJN, RMSE 6.4 against sigma 0.8 -- was pointed the
    wrong way.  And the sample was the newest 400 evidence rows, of which 85
    were served.

    Now every served seat -- activation pointer, artifact, evidence, the
    path ``collect_serving_weights`` reads -- is re-solved by production's
    own functions from what the SSOT kept (``_fusion_reconstruction``).  The
    distance grades the lamp only where the diagonal is recovered exactly;
    measured at the rewrite, 686 of 826 served seats, 99th percentile 0.8%.
    The residual correlation grades it as before.
    """

    family_by_system = {
        str(system): str(family)
        for system, family in conn.execute(
            "SELECT ensemble_system_id, independence_family_id"
            " FROM calibration_native_ensemble_system"
        )
    }
    # The seats P0 actually serves (``served_p0_seats``), not the highest
    # generation per scope.
    served_rows = [
        (
            seat["station_id"], seat["season_bin"], seat["lead_bucket"],
            seat["run_cycle"], seat["set_hash"], seat["combination_method"],
            seat["evidence_sha256"], seat["selected_weights_json"],
            seat["correlation_json"], seat["run_cycle_by_system_json"],
            seat["family_weight_caps_json"], seat["train_end_date"],
        )
        for seat in served_p0_seats(conn)
        if int(seat.get("selected_system_count") or 0) > 1
    ]

    rows: list[dict[str, Any]] = []
    unread = 0
    for (
        station, season, lead, cycle, set_hash, method, evidence_sha,
        weights_json, correlation_json, cycles_json, caps_json, train_end,
    ) in served_rows:
        try:
            served = {
                str(system): float(value)
                for system, value in json.loads(weights_json).items()
            }
            correlation = json.loads(correlation_json)
            cycles = json.loads(cycles_json) if cycles_json else {}
            caps = {
                str(family): float(value)
                for family, value in json.loads(caps_json).items()
            }
        except (AttributeError, TypeError, ValueError):
            unread += 1
            continue
        systems = sorted(served)
        if (
            len(systems) < 2
            or not isinstance(correlation, dict)
            or any(system not in family_by_system for system in systems)
        ):
            unread += 1
            continue
        off_diagonal = []
        for left in systems:
            for right in systems:
                if left == right:
                    continue
                try:
                    off_diagonal.append(float(correlation[left][right]))
                except (KeyError, TypeError, ValueError):
                    pass
        kappa = None
        for part in str(method or "").split("::")[1:]:
            if part.startswith("stable_pool_kappa="):
                try:
                    kappa = float(part.split("=", 1)[1])
                except ValueError:
                    pass
        evaluation = conn.execute(
            "SELECT json_extract(oos_metrics_json, '$.equal_weight_diagnostic"
            ".shadow_schemes.inverse_variance.weights')"
            " FROM calibration_cross_native_ensemble_oos_evaluation"
            " WHERE station_id=? AND season_bin=? AND lead_bucket=?"
            " AND run_cycle=? AND available_system_set_hash=?"
            " AND selection_evidence_sha256=?"
            " ORDER BY evaluated_at_utc DESC LIMIT 1",
            (station, season, lead, cycle, set_hash, evidence_sha),
        ).fetchone()
        member_scale: dict[str, float] = {}
        member_train_n: dict[str, int | None] = {}
        for system in systems:
            member = conn.execute(
                "SELECT spread_parameters_json, reliability_parameters_json"
                " FROM calibration_native_ensemble_system_artifact"
                " WHERE station_id=? AND lead_bucket=? AND run_cycle=?"
                " AND ensemble_system_id=? AND train_end_date<=?"
                " AND season_bin=?"
                " ORDER BY rowid DESC LIMIT 1",
                (
                    station, lead, str(cycles.get(system) or cycle), system,
                    train_end, season,
                ),
            ).fetchone()
            if member is None:
                break
            try:
                spread = json.loads(member[0])
                reliability = json.loads(member[1] or "{}")
                member_scale[system] = float(
                    spread.get("train_residual_sigma_c")
                    or spread["train_rmse_c"]
                )
            except (KeyError, TypeError, ValueError):
                break
            count = reliability.get("fit_case_count")
            member_train_n[system] = int(count) if count else None
        if evaluation is None or len(member_scale) != len(systems):
            unread += 1
            continue
        try:
            inverse_variance = (
                json.loads(evaluation[0]) if evaluation[0] else None
            )
        except (TypeError, ValueError):
            inverse_variance = None
        rebuilt = _fusion_reconstruction(
            served=served,
            correlation=correlation,
            inverse_variance=inverse_variance,
            member_scale=member_scale,
            member_train_n=member_train_n,
            family_by_system=family_by_system,
            family_caps=caps,
            kappa=kappa,
        )
        resolved = rebuilt["weights"]
        basis = rebuilt["basis"]
        if resolved is None and basis != "non_psd":
            basis = "unsolved"
        rows.append({
            "station_id": str(station),
            "season_bin": str(season),
            "lead_bucket": str(lead),
            "run_cycle": str(cycle),
            "rule": _fusion_rule_label(kappa, rebuilt["skill_rule"]),
            "basis": basis,
            "max_correlation": max(off_diagonal) if off_diagonal else None,
            "distance": (
                0.5 * sum(
                    abs(served[system] - float(resolved.get(system, 0.0)))
                    for system in systems
                )
                if resolved is not None else None
            ),
            "served": served,
            "resolved": resolved,
        })

    return {"rows": rows, "unread": unread}


# ---------------------------------------------------------------------------
# centre_shape
# ---------------------------------------------------------------------------

# Hours-to-settlement bands the split is read in, the audit's own.  The
# headline is 12h and beyond: inside 12h P1 serves and the market already
# reads the day's observations.
CENTRE_SHAPE_TIERS = (
    ("0-12", 0.0, 12.0), ("12-24", 12.0, 24.0), ("24-36", 24.0, 36.0),
    ("36-48", 36.0, 48.0), ("48+", 48.0, None),
)


def _ladder_rps(
    vector: Mapping[str, float], ordered_keys: Sequence[str], label: str,
) -> float:
    """Ranked probability score over an ordered ladder: the squared gap
    between the cumulative claim and the cumulative outcome, summed over every
    rung boundary.  Lower is better; it charges a miss by how many rungs away
    the mass sat, which is what a centre error costs."""

    cumulative = score = 0.0
    hit = False
    for key in ordered_keys[:-1]:
        cumulative += float(vector.get(key, 0.0) or 0.0)
        hit = hit or str(key) == str(label)
        score += (cumulative - (1.0 if hit else 0.0)) ** 2
    return score


def centre_shape_row(
    p: Mapping[str, float],
    m: Mapping[str, float],
    label: str,
    *,
    ordered_keys: Sequence[str],
    intervals: Mapping[str, Sequence[float | None]],
) -> dict[str, Any]:
    """One decision's gap to the market, split into centre and shape.

    The 2026-09-22 P0 downstream audit's rule (finding 1): the served vector
    ``p`` and the market mid ``m`` are scored with RPS on the ladder; then
    ``p`` is moved onto the market's mean with production's own width-layer
    transform (``apply_center_width_calibration``, width lambda 1, key
    centres from ``served_bucket_axis``, point edges as the audit ran it), the
    shift bounded by that transform's own entitlement ``MAX_ABS_SHIFT_C``.
    What the move removes is the centre part of the gap; what is left is the
    shape part, which is all the distribution layers (width, band, radiation)
    can ever answer for.

    The same shift in the other direction is the control: the mean of the two
    signs is a shift of the same size in a random direction.  A centre gain
    that were only the mechanics of moving mass would show up there too.
    """

    from bayes_basket_target.center_width_calibration import (  # noqa: PLC0415
        MAX_ABS_SHIFT_C,
        apply_center_width_calibration,
        served_bucket_axis,
    )

    keys = [str(key) for key in ordered_keys]
    centres, _edges = served_bucket_axis(intervals, keys)
    model_mean = sum(float(p.get(k, 0.0) or 0.0) * c for k, c in zip(keys, centres))
    market_mean = sum(float(m.get(k, 0.0) or 0.0) * c for k, c in zip(keys, centres))
    wanted = market_mean - model_mean
    shift = max(-MAX_ABS_SHIFT_C, min(MAX_ABS_SHIFT_C, wanted))

    def _moved(delta: float) -> dict[str, float]:
        return apply_center_width_calibration(
            p, delta_c=delta, width_lambda=1.0,
            ordered_keys=keys, key_centers_c=centres,
        )[0]

    toward = _ladder_rps(_moved(shift), keys, label)
    away = _ladder_rps(_moved(-shift), keys, label) if shift else toward
    return {
        "rps_model": _ladder_rps(p, keys, label),
        "rps_market": _ladder_rps(m, keys, label),
        "rps_at_market_mean": toward,
        "rps_random_direction": 0.5 * (toward + away),
        "model_minus_market_mean_c": -wanted,
        "clamped": abs(wanted) > MAX_ABS_SHIFT_C,
    }


def centre_shape_reading(
    rows: Sequence[Mapping[str, Any]], *, draws: int, seed: int,
) -> dict[str, Any]:
    """The split over a sample, station-days weighted equally and whole event
    days resampled for the 95% intervals (a fleet shares its weather).

    ``gap`` = RPS(model) - RPS(market), positive where the model is worse;
    ``shape`` = what is left after the move onto the market's mean;
    ``centre`` = ``gap - shape``; ``control`` = the random-direction shift
    minus the served vector, positive where moving the same distance without
    the market's direction makes it worse.
    """

    per_day: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (str(row["station"]), str(row["date"]))
        per_day[key] = per_day.get(key, 0) + 1
    days: dict[str, list[float]] = {}
    for row in rows:
        w = 1.0 / per_day[(str(row["station"]), str(row["date"]))]
        acc = days.setdefault(str(row["date"]), [0.0] * 6)
        acc[0] += w
        acc[1] += w * (row["rps_model"] - row["rps_market"])
        acc[2] += w * (row["rps_at_market_mean"] - row["rps_market"])
        acc[3] += w * (row["rps_random_direction"] - row["rps_model"])
        acc[4] += w * abs(row["model_minus_market_mean_c"])
        acc[5] += w * row["model_minus_market_mean_c"]
    empty = {
        "decisions": len(rows), "station_days": len(per_day), "days": len(days),
        "gap": None, "shape": None, "centre": None, "centre_share": None,
        "control": None, "gap_ci": None, "shape_ci": None, "centre_ci": None,
        "centre_share_ci": None, "control_ci": None,
        "mean_abs_distance_c": None, "mean_signed_distance_c": None,
        "clamped_share": None,
    }
    if not days:
        return empty

    def _point(picked: Sequence[list[float]]) -> tuple[float, ...] | None:
        w = sum(day[0] for day in picked)
        if w <= 0.0:
            return None
        gap = sum(day[1] for day in picked) / w
        shape = sum(day[2] for day in picked) / w
        control = sum(day[3] for day in picked) / w
        share = (gap - shape) / gap if gap > 0.0 else None
        return gap, shape, gap - shape, share, control

    point = _point(list(days.values()))
    if point is None:
        return empty
    rng = random.Random(int(seed))
    keys = list(days)
    draws_out: list[tuple[float, ...]] = []
    if len(keys) >= 2:
        for _ in range(int(draws)):
            got = _point([days[key] for key in rng.choices(keys, k=len(keys))])
            if got is not None:
                draws_out.append(got)

    def _band(index: int) -> list[float] | None:
        values = sorted(d[index] for d in draws_out if d[index] is not None)
        if len(values) < 10:
            return None
        return [values[int(0.025 * (len(values) - 1))],
                values[int(math.ceil(0.975 * (len(values) - 1)))]]

    weight = sum(day[0] for day in days.values())
    return {
        **empty,
        "gap": point[0], "shape": point[1], "centre": point[2],
        "centre_share": point[3], "control": point[4],
        "gap_ci": _band(0), "shape_ci": _band(1), "centre_ci": _band(2),
        "centre_share_ci": _band(3), "control_ci": _band(4),
        "mean_abs_distance_c": sum(d[4] for d in days.values()) / weight,
        "mean_signed_distance_c": sum(d[5] for d in days.values()) / weight,
        "clamped_share": sum(1 for row in rows if row["clamped"]) / len(rows),
    }


def collect_centre_shape(
    conn: sqlite3.Connection,
    *,
    floor_day: str | None,
    as_of_event_date: str | None,
    served_anchor_utc: str | None,
    draws: int,
    seed: int,
) -> dict[str, Any]:
    """The P0 gap to the market, split into centre and shape, sampled the way
    the 2026-09-22 audit sampled it: every settled station-day of the charter
    EV page's window, and in each hours-to-settlement band the first committed
    P0 envelope that has a complete market snapshot at most 30 minutes old.

    Envelopes, not the valuation lane's trading decisions: P0 trades only far
    out (inside about a day and a half the trading stage is P0-M or P1), but
    P0 is served at every lead as the root both of them multiply, so its gap
    at 12-24h is read here too.  Every read is a seek by station and event
    day: the envelope list, the ladder once from the newest envelope's
    lineage, and the snapshot at or before each candidate decision.  Labels
    are the truth lane's (``knowable_labels``), as on the rest of the page.

    Three samples at 12h and beyond -- since charter 134's stable pool (P0 v5,
    the audit's own sample), the whole window, since the serving-arithmetic
    anchor -- then the stable-pool sample band by band.
    """

    from bayes_basket_target.basket_kernel.valuation_v1 import (  # noqa: PLC0415
        market_reference_v1,
    )
    from bayes_basket_target.center_width_calibration import (  # noqa: PLC0415
        MAX_ABS_SHIFT_C,
    )
    from bayes_basket_target.label_knowability import (  # noqa: PLC0415
        knowable_labels,
    )
    from bayes_basket_target.serving_arithmetic import (  # noqa: PLC0415
        STAGE_VERSION_EFFECTIVE_FROM,
    )
    from bayes_basket_target.settlement_time import (  # noqa: PLC0415
        hours_to_settlement,
    )

    def _instant(value: str) -> datetime:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    def _tier(hours: float) -> str | None:
        for name, lo, hi in CENTRE_SHAPE_TIERS:
            if hours >= lo and (hi is None or hours < hi):
                return name
        return None

    def _seek(sql: str, params: tuple[Any, ...], index: str) -> list[Any]:
        try:
            return conn.execute(
                sql.format(hint=f" INDEXED BY {index}"), params).fetchall()
        except sqlite3.OperationalError:
            # A store without the index (fixtures): the same seek unhinted.
            return conn.execute(sql.format(hint=""), params).fetchall()

    env_sql = (
        "SELECT decision_at_utc, settlement_timezone, probabilities_json,"
        " bucket_ordinals_json FROM basket_probability_envelope{hint}"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        " AND stage='P0' ORDER BY decision_at_utc"
    )
    ladder_sql = (
        "SELECT json_extract(lineage_json,'$.bucket_intervals_c_json')"
        " FROM basket_probability_envelope{hint}"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        " AND stage='P0' ORDER BY decision_at_utc DESC LIMIT 1"
    )
    snap_sql = (
        "SELECT observed_at, buckets_json FROM basket_official_ws_snapshot{hint}"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        " AND observed_at<=? ORDER BY observed_at DESC, sequence DESC LIMIT 1"
    )
    counts = {"station_days": 0, "no_envelope": 0, "no_ladder": 0,
              "band_unpaired": 0, "ladder_mismatch": 0, "failed": 0}
    known = knowable_labels(conn)
    if known and (floor_day is None or as_of_event_date is None):
        # No window handed in (the P0 population failed): the page's window
        # on the newest label, never the whole history.
        from bayes_basket_target.valuation_store import WINDOW_DAYS  # noqa: PLC0415
        as_of_event_date = as_of_event_date or max(day for _s, day in known)
        floor_day = floor_day or (
            date.fromisoformat(str(as_of_event_date)) - timedelta(days=int(WINDOW_DAYS))
        ).isoformat()
    labels = {
        key: value for key, value in known.items()
        if (floor_day is None or str(key[1]) > str(floor_day))
        and (as_of_event_date is None or str(key[1]) <= str(as_of_event_date))
    }
    scored: list[dict[str, Any]] = []
    for (station, day), label in sorted(labels.items()):
        envelopes = _seek(env_sql, (station, day),
                          "basket_probability_envelope_asof_idx")
        if not envelopes:
            counts["no_envelope"] += 1
            continue
        counts["station_days"] += 1
        got = _seek(ladder_sql, (station, day),
                    "basket_probability_envelope_asof_idx")
        try:
            intervals = json.loads(got[0][0]) if got and got[0][0] else None
        except (TypeError, ValueError):
            intervals = None
        if not intervals:
            counts["no_ladder"] += 1
            continue
        bands: dict[str, list[Any]] = {}
        for decided_at, tz, probs, ordinals in envelopes:
            try:
                hours = hours_to_settlement(
                    str(day), as_of_utc=str(decided_at), settlement_timezone=str(tz))
            except (ValueError, TypeError, KeyError):
                continue
            tier = _tier(float(hours))
            if tier is not None:
                bands.setdefault(tier, []).append((decided_at, probs, ordinals))
        for tier, candidates in bands.items():
            picked = None
            for decided_at, probs, ordinals in candidates[:8]:
                snap = _seek(snap_sql, (station, day, str(decided_at)),
                             "basket_official_ws_snapshot_asof_idx")
                if not snap or (
                    _instant(decided_at) - _instant(snap[0][0])
                ).total_seconds() > 1800:
                    continue
                try:
                    market = market_reference_v1(json.loads(snap[0][1] or "[]"))
                except (TypeError, ValueError):
                    continue
                if market.get("state") == "complete":
                    picked = (decided_at, probs, ordinals, market["vector"])
                    break
            if picked is None:
                counts["band_unpaired"] += 1
                continue
            decided_at, probs, ordinals, m = picked
            try:
                p = {str(k): float(v) for k, v in json.loads(probs).items()}
                order = json.loads(ordinals)
                keys = sorted(order, key=lambda k: order[k])
            except (TypeError, ValueError, AttributeError):
                counts["failed"] += 1
                continue
            bucket = str(label["bucket"])
            if (set(keys) != set(p) or set(keys) != set(m)
                    or set(keys) != set(intervals) or bucket not in keys):
                counts["ladder_mismatch"] += 1
                continue
            try:
                split = centre_shape_row(
                    p, m, bucket, ordered_keys=keys, intervals=intervals)
            except (ValueError, TypeError, KeyError):
                counts["failed"] += 1
                continue
            scored.append({"station": str(station), "date": str(day),
                           "tier": tier, "at": str(decided_at), **split})
    pool_from = STAGE_VERSION_EFFECTIVE_FROM.get("P0", {}).get(
        "calibrated_native_cross_ensemble_p0_v5")

    def _since(sample: list[dict[str, Any]], moment: str | None) -> list[dict[str, Any]]:
        if not moment:
            return []
        edge = _instant(moment)
        return [r for r in sample if _instant(r["at"]) >= edge]

    long = [r for r in scored if r["tier"] != "0-12"]
    pool_all = _since(scored, pool_from)
    return {
        "available": bool(scored),
        "stable_pool_from_utc": pool_from,
        "served_anchor_utc": served_anchor_utc,
        "window": {"floor_day": floor_day, "as_of_event_date": as_of_event_date},
        "samples": {
            "stable_pool": centre_shape_reading(
                _since(long, pool_from), draws=draws, seed=seed),
            "window": centre_shape_reading(long, draws=draws, seed=seed),
            "anchor": centre_shape_reading(
                _since(long, served_anchor_utc), draws=draws, seed=seed),
        },
        "tiers": {
            name: centre_shape_reading(
                [r for r in pool_all if r["tier"] == name], draws=draws, seed=seed)
            for name, _lo, _hi in CENTRE_SHAPE_TIERS
        },
        "population": counts,
        "max_abs_shift_c": float(MAX_ABS_SHIFT_C),
    }


def centre_shape(conn: sqlite3.Connection) -> dict[str, Any]:
    """The split on the charter EV page's P0 window (its newest label back
    ``WINDOW_DAYS``), with the P0 serving anchor."""

    from .center_width_store import evidence_anchor  # noqa: PLC0415
    from .valuation_store import BOOTSTRAP_DRAWS, BOOTSTRAP_SEED  # noqa: PLC0415

    try:
        anchor = evidence_anchor(conn, stage="P0")
    except Exception:  # noqa: BLE001 -- no anchor reads the whole window
        anchor = None
    return collect_centre_shape(
        conn, floor_day=None, as_of_event_date=None,
        served_anchor_utc=anchor, draws=BOOTSTRAP_DRAWS, seed=BOOTSTRAP_SEED,
    )


# ---------------------------------------------------------------------------
# l4_lane
# ---------------------------------------------------------------------------


def l4_lane(conn: sqlite3.Connection) -> dict[str, Any]:
    """Each stage's forward skill on the fits that were active, and the last
    fourteen days' settled windows re-judged with the L4 row of their
    decision."""

    from .posterior_store import forward_skill, realisation  # noqa: PLC0415

    forward: dict[str, Any] = {}
    for scope in ("P0", "P0-M", "P1"):
        try:
            forward[scope] = forward_skill(conn, stage_scope=scope)
        except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            forward[scope] = {"error": f"{type(exc).__name__}:{exc}"[:200]}
    try:
        realised = realisation(
            conn,
            since_event_date=(_now().date() - timedelta(days=14)).isoformat(),
        )
    except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        realised = {"error": f"{type(exc).__name__}:{exc}"[:200]}
    return {"forward": forward, "realisation": realised}


# ---------------------------------------------------------------------------
# the basket's shadow ladder (user 2026-10-05 「前端增加篮子策略（梯子）的显示和
# 评估：按照篮子策略选桶下单」, answered 「影子显示+评估」)
# ---------------------------------------------------------------------------

#: The basket buys in P0 only (charter 229), so only P0 preselection plans
#: say what it would have bought.
BASKET_SHADOW_STAGE = "P0"
#: Event days kept in the reading.
BASKET_SHADOW_DAYS = 45
#: Plans read per query: each query is its own short read transaction, so
#: the job never holds a snapshot across the table (WAL pinning).
BASKET_SHADOW_CHUNK_ROWS = 1500
#: Wall-clock budget of one run; the cursor carries the rest to the next.
BASKET_SHADOW_BUDGET_SEC = float(os.environ.get("PM_BASKET_SHADOW_BUDGET_SEC") or 600.0)
#: Bumped when a record's meaning changes: the stored cursor and records are
#: then dropped and the table is re-walked.
BASKET_SHADOW_STATE_VERSION = 3
BASKET_SHADOW_BOOTSTRAP_DRAWS = 2000

_BASKET_SHADOW_SQL = (
    "SELECT rowid, station_id, event_date, decision_at_utc,"
    " json_extract(plan_json,'$.compiled_plan.plan.basket_target_activated'),"
    " json_extract(plan_json,'$.compiled_plan.delta_v2_audit.window_gate_counts'),"
    " json_extract(plan_json,'$.compiled_plan.delta_v2_audit.candidate_windows'),"
    " CASE WHEN json_extract(plan_json,'$.compiled_plan.plan.basket_target_activated')"
    "  THEN json_extract(plan_json,'$.compiled_plan.plan.targets') END,"
    " json_extract(plan_json,'$.compiled_plan.plan.reason_codes')"
    " FROM basket_p0_preselection_plan"
    " WHERE rowid>? AND rowid<=? AND probability_stage=?"
    " ORDER BY rowid"
)


def _basket_window(raw: Any) -> dict[str, Any] | None:
    """The first admissible candidate window, as the board shows it."""

    try:
        windows = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except ValueError:
        return None
    for w in windows if isinstance(windows, list) else []:
        if not isinstance(w, Mapping) or w.get("admissible") is not True:
            continue
        legs = w.get("leg_claim_cost") or {}
        return {
            "buckets": [str(b) for b in (w.get("buckets") or [])],
            "all_in_price": w.get("all_in_price"),
            "win_probability": w.get("win_probability"),
            "market_mid": w.get("market_mid"),
            "hours_to_cutoff": w.get("hours_to_cutoff"),
            # bucket -> [model claim, all-in ask]
            "legs": {str(b): list(v) for b, v in legs.items()
                     if isinstance(v, (list, tuple)) and len(v) == 2},
        }
    return None


def _basket_targets(raw: Any) -> list[dict[str, Any]]:
    try:
        rows = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except ValueError:
        return []
    out = []
    for t in rows if isinstance(rows, list) else []:
        if not isinstance(t, Mapping):
            continue
        shares = float(t.get("delta_shares") or t.get("target_shares") or 0.0)
        price = t.get("limit_price") if t.get("limit_price") is not None else t.get("effective_price")
        mode = str(t.get("execution_candidate") or "").upper()
        # Plans before ~09-19 carry target shapes with no execution (mode
        # NONE, price 0.0): a shape, not an order, and free legs would read
        # as profit.  Only a priced leg with an execution family counts.
        if shares <= 0 or price is None or float(price) <= 0 or mode in ("", "NONE"):
            continue
        out.append({
            "bucket": str(t.get("bucket")),
            "shares": round(shares, 4),
            "price": float(price),
            "mode": mode,
            "role": str(t.get("role") or ""),
        })
    return out


def _basket_shadow_walk(
    conn: sqlite3.Connection,
    state: dict[str, Any],
    *,
    budget_sec: float,
) -> None:
    """Advance ``state`` over the plans written since its cursor."""

    started = time.perf_counter()
    (top,) = conn.execute("SELECT MAX(rowid) FROM basket_p0_preselection_plan").fetchone()
    top = int(top or 0)
    floor_day = (_now().date() - timedelta(days=BASKET_SHADOW_DAYS)).isoformat()
    cursor = int(state.get("cursor") or 0)
    if cursor == 0:
        row = conn.execute(
            "SELECT MIN(rowid) FROM basket_p0_preselection_plan WHERE created_at>=?",
            (floor_day,),
        ).fetchone()
        cursor = max(0, int(row[0] or 1) - 1)
    days: dict[str, Any] = state.setdefault("station_days", {})
    while cursor < top and time.perf_counter() - started < budget_sec:
        hi = min(top, cursor + BASKET_SHADOW_CHUNK_ROWS)
        for (rowid, station, day, at, activated, gates_raw, windows_raw,
             targets_raw, reasons_raw) in conn.execute(
                 _BASKET_SHADOW_SQL, (cursor, hi, BASKET_SHADOW_STAGE)):
            if str(day) < floor_day:
                continue
            key = f"{station}|{day}"
            rec = days.setdefault(key, {
                "station_id": str(station), "event_date": str(day),
                "plans": 0, "first_at": str(at),
                "windowed_plans": 0, "activated_plans": 0,
                "gates": {}, "first_window": None, "open": None,
            })
            rec["plans"] += 1
            rec["last_at"] = str(at)
            try:
                gates = json.loads(gates_raw) if gates_raw else {}
            except ValueError:
                gates = {}
            gate = str((gates or {}).get("last_refusing_gate") or "")
            window = _basket_window(windows_raw)
            if window is not None:
                rec["windowed_plans"] += 1
                if rec["first_window"] is None:
                    rec["first_window"] = {**window, "at": str(at)}
            if gate:
                rec["gates"][gate] = rec["gates"].get(gate, 0) + 1
            targets = _basket_targets(targets_raw) if activated else []
            if targets:
                rec["activated_plans"] += 1
                if rec["open"] is None:
                    # The same plan's all-in ask per bucket, when its window
                    # priced the leg: the taker end of the fill assumption.
                    asks = (window or {}).get("legs") or {}
                    rec["open"] = {
                        "at": str(at),
                        "legs": [
                            {**t, "ask": (asks.get(t["bucket"]) or [None, None])[1]}
                            for t in targets
                        ],
                        "cost_usdc": round(sum(t["shares"] * t["price"] for t in targets), 4),
                    }
            try:
                reasons = json.loads(reasons_raw) if reasons_raw else []
            except ValueError:
                reasons = []
            rec["latest"] = {
                "at": str(at),
                "window": window,
                "gate": gate or None,
                "activated": bool(targets),
                "legs": targets,
                "reasons": [str(r) for r in reasons][:6],
            }
        cursor = hi
    state["cursor"] = cursor
    state["cursor_top"] = top
    state["caught_up"] = cursor >= top
    for key in [k for k, v in days.items() if v.get("event_date", "") < floor_day]:
        del days[key]


def _basket_shadow_labels(conn: sqlite3.Connection, floor_day: str) -> dict[str, str]:
    """Production's settlement label, standing revision, per station-day."""

    out: dict[str, str] = {}
    for station, day, bucket in conn.execute(
        "SELECT station_id, event_date, settled_bucket"
        " FROM basket_station_day_settlement_label l"
        " WHERE event_date>=? AND truth_revision_no=("
        "  SELECT MAX(truth_revision_no) FROM basket_station_day_settlement_label x"
        "  WHERE x.station_id=l.station_id AND x.event_date=l.event_date)",
        (floor_day,),
    ):
        out[f"{station}|{day}"] = str(bucket)
    return out


def _day_bootstrap(rows: Sequence[Mapping[str, Any]], *, seed: int) -> list[float] | None:
    """95% interval of sum(pnl)/sum(cost), resampling event days."""

    by_day: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for r in rows:
        acc = by_day[str(r["event_date"])]
        acc[0] += float(r["pnl"])
        acc[1] += float(r["cost"])
    blocks = list(by_day.values())
    if len(blocks) < 3:
        return None
    rng = random.Random(seed)
    draws = []
    for _ in range(BASKET_SHADOW_BOOTSTRAP_DRAWS):
        pnl = cost = 0.0
        for _ in blocks:
            b = blocks[rng.randrange(len(blocks))]
            pnl += b[0]
            cost += b[1]
        if cost > 0:
            draws.append(pnl / cost)
    if not draws:
        return None
    draws.sort()
    return [draws[int(0.025 * len(draws))], draws[int(0.975 * len(draws)) - 1]]


def _basket_shadow_score(records: Sequence[Mapping[str, Any]], labels: Mapping[str, str]) -> dict[str, Any]:
    """Two readings on the settled station-days.

    * ``open`` -- the basket's own opening order (first activated P0 plan):
      its legs, shares and limit prices, assumed filled in full at the limit.
      An upper bound: a resting maker order is not certain to fill.
    * ``open_at_ask`` -- the same legs and shares bought at that plan's
      all-in ask instead (taker), where its window priced every leg.
    * ``window`` -- the basket's chosen window at the first plan that had
      one, one share per leg at the all-in ask (taker): the bucket choice
      judged without the sizing that usually zeroes it.
    """

    open_rows, open_ask_rows, window_rows = [], [], []
    for rec in records:
        key = f"{rec['station_id']}|{rec['event_date']}"
        settled = labels.get(key)
        if settled is None:
            continue
        if rec.get("open"):
            legs = rec["open"]["legs"]
            payout = sum(t["shares"] for t in legs if t["bucket"] == settled)
            cost = float(rec["open"]["cost_usdc"])
            open_rows.append({"event_date": rec["event_date"], "pnl": payout - cost,
                              "cost": cost, "hit": payout > 0})
            if all(t.get("ask") is not None for t in legs):
                ask_cost = sum(t["shares"] * float(t["ask"]) for t in legs)
                open_ask_rows.append({"event_date": rec["event_date"],
                                      "pnl": payout - ask_cost, "cost": ask_cost,
                                      "hit": payout > 0})
        w = rec.get("first_window")
        if w and w.get("all_in_price") is not None:
            hit = settled in (w.get("buckets") or [])
            cost = float(w["all_in_price"])
            window_rows.append({
                "event_date": rec["event_date"], "pnl": (1.0 if hit else 0.0) - cost,
                "cost": cost, "hit": hit,
                "claim": w.get("win_probability"), "mid": w.get("market_mid"),
            })

    def summary(rows: list[dict[str, Any]], seed: int) -> dict[str, Any]:
        cost = sum(r["cost"] for r in rows)
        pnl = sum(r["pnl"] for r in rows)
        out = {
            "n": len(rows),
            "days": len({r["event_date"] for r in rows}),
            "hits": sum(1 for r in rows if r["hit"]),
            "cost": round(cost, 4),
            "pnl": round(pnl, 4),
            "roi": (pnl / cost) if cost > 0 else None,
            "roi_ci95": _day_bootstrap(rows, seed=seed),
        }
        claims = [float(r["claim"]) for r in rows if r.get("claim") is not None]
        mids = [float(r["mid"]) for r in rows if r.get("mid") is not None]
        if claims:
            out["mean_claim"] = sum(claims) / len(claims)
        if mids:
            out["mean_mid"] = sum(mids) / len(mids)
        if rows:
            out["hit_rate"] = out["hits"] / len(rows)
            out["mean_cost"] = cost / len(rows)
        return out

    return {
        "open": summary(open_rows, 11),
        "open_at_ask": summary(open_ask_rows, 12),
        "window": summary(window_rows, 13),
    }


def basket_shadow(conn: sqlite3.Connection) -> dict[str, Any]:
    """What the basket would buy (its P0 ladder) per station-day, and how
    those choices settled -- read from the P0 preselection plans production
    already compiled; nothing is recomputed and no order is placed."""

    previous = (load_reading("basket_shadow") or {}).get("value") or {}
    state = previous.get("state") if isinstance(previous.get("state"), dict) else {}
    if state.get("version") != BASKET_SHADOW_STATE_VERSION:
        state = {"version": BASKET_SHADOW_STATE_VERSION}
    _basket_shadow_walk(conn, state, budget_sec=BASKET_SHADOW_BUDGET_SEC)
    floor_day = (_now().date() - timedelta(days=BASKET_SHADOW_DAYS)).isoformat()
    labels = _basket_shadow_labels(conn, floor_day)
    records = sorted(
        state["station_days"].values(),
        key=lambda r: (r["event_date"], r["station_id"]),
        reverse=True,
    )
    for rec in records:
        rec["settled_bucket"] = labels.get(f"{rec['station_id']}|{rec['event_date']}")
    return {
        "state": state,
        "stage": BASKET_SHADOW_STAGE,
        "days_kept": BASKET_SHADOW_DAYS,
        "score": _basket_shadow_score(records, labels),
    }


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

_READINGS: dict[str, tuple[Any, tuple[Any, ...]]] = {
    "seat_windows": (seat_windows, (seat_window,)),
    "fusion_resolve": (
        fusion_resolved_seats,
        (fusion_resolved_seats, _fusion_reconstruction,
         _fusion_synthetic_residuals, _fusion_rule_label),
    ),
    "centre_shape": (
        centre_shape,
        (centre_shape, collect_centre_shape, centre_shape_reading,
         centre_shape_row, _ladder_rps),
    ),
    "l4_lane": (l4_lane, (l4_lane,)),
    "basket_shadow": (
        basket_shadow,
        (basket_shadow, _basket_shadow_walk, _basket_window, _basket_targets,
         _basket_shadow_labels, _basket_shadow_score, _day_bootstrap),
    ),
}


#: How old a reading may be before the hourly job recomputes it.  The job is
#: a long SSOT reader, and l4_lane alone read 5-16 minutes of every hour
#: (2026-10-04, swap 7 GB of 8, production cycles at 400-1300 s); L4 is
#: offline (charter 231) and its settled windows move once a day, and the
#: centre/shape gap reads closed days.  The seat and fusion readings follow
#: what P0 serves now and stay hourly.  ``--only`` always recomputes.
READING_MAX_AGE_SEC: dict[str, float] = {
    "centre_shape": 3 * 3600.0,
    "l4_lane": 6 * 3600.0,
}


def _reading_is_current(name: str, code: str) -> bool:
    stored = load_reading(name)
    limit = READING_MAX_AGE_SEC.get(name)
    if stored is None or limit is None or stored.get("code_sha256") != code:
        return False
    try:
        computed = datetime.fromisoformat(
            str(stored.get("computed_at_utc")).replace("Z", "+00:00")
        )
    except ValueError:
        return False
    # A few minutes of slack, so a reading written late in one hourly run is
    # still recomputed on the run its age falls due, not the one after.
    return (_now() - computed).total_seconds() < limit - 600.0


def run(
    db: Path = DEFAULT_DB,
    names: Sequence[str] = READING_NAMES,
    *,
    force: bool = False,
) -> list[str]:
    """Compute and write each named reading; one line of receipt per reading.
    A reading that fails keeps its previous file and says why; one still
    inside its ``READING_MAX_AGE_SEC`` is kept unless ``force``."""

    receipts: list[str] = []
    for name in names:
        compute, sources = _READINGS[name]
        if not force and _reading_is_current(name, _code_sha(*sources)):
            receipts.append(f"{name} kept")
            continue
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60.0)
        conn.execute("PRAGMA query_only=ON")
        started = time.perf_counter()
        try:
            value = compute(conn)
        except Exception as exc:  # noqa: BLE001 -- one reading must not stop the rest
            receipts.append(f"{name} failed: {type(exc).__name__}:{str(exc)[:200]}")
            continue
        finally:
            conn.close()
        elapsed = time.perf_counter() - started
        write_reading(name, value, code=_code_sha(*sources), build_sec=elapsed)
        receipts.append(f"{name} {elapsed:.0f}s")
    return receipts


def main(argv: Sequence[str] | None = None) -> int:
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--only", action="append", choices=READING_NAMES)
    args = parser.parse_args(argv)
    print("board readings: " + "; ".join(
        run(args.db, args.only or READING_NAMES, force=bool(args.only))
    ), flush=True)
    return 0


__all__ = [
    "READINGS_DIR", "READING_NAMES", "SCHEMA", "load_reading", "main", "run",
    "served_p0_seats", "write_reading",
]


if __name__ == "__main__":
    raise SystemExit(main())
