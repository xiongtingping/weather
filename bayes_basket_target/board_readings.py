"""Board readings: what the calibration board shows that production's serving
path does not store, computed here, off the board.

User rulings 2026-10-02 「所有前端都改为只读」 / 「只读是读后端算出来的」 /
「如果后端没有的，就增加前端需要的数据」 / 「新开一个后端读数任务」.  The board
used to compute these itself by calling production's own functions; it now
only reads the files this job writes (``data/board_readings/<name>.json``),
and ``tests/test_board_is_read_only.py`` keeps it that way.

Six readings, each with production's functions and nothing re-derived:

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
* ``traded_calibration`` -- charter 259's second layer: on the buckets the
  lanes actually bought (and the ones they sent for), the binary log loss
  and reliability of our served probability beside the market's mid at the
  same instant, by lane, side, stage and condition.
* ``bucket_calibration`` -- every bucket of the ladder, not only the traded
  ones: our served P(T=k) beside the market reference, binary log loss, one
  sample per settled station-day per hours-to-settlement band.

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
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence

from .resource_bounds import READER_YIELD_WAL_BYTES, install_wal_yield, wait_for_wal_under

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
    "traded_calibration", "bucket_calibration", "skew_truth_side", "mode_agreement",
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


def effective_independent_models(
    correlation: Mapping[str, Any], systems: Sequence[str]
) -> tuple[float | None, float | None]:
    """(mean pairwise residual correlation, effective independent models).

    ``k / (1 + (k - 1) * r)`` for ``k`` seated systems whose errors correlate
    ``r`` on average -- how many independent opinions the blend holds.  User
    ruling 2026-10-15 (「改」): this is the fusion layer's reading, not its
    lamp; errors every model shares (station microclimate, a common seasonal
    drift) are a ceiling no weighting removes.  ``r`` is floored at 0, so the
    count never exceeds ``k``.
    """

    pairs = []
    for i, left in enumerate(systems):
        for right in systems[i + 1:]:
            try:
                pairs.append(float(correlation[left][right]))
            except (KeyError, TypeError, ValueError):
                pass
    if not pairs:
        return None, None
    mean = sum(pairs) / len(pairs)
    k = len(systems)
    return mean, k / (1.0 + (k - 1) * max(0.0, mean))


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
    The residual correlation is reported as the effective number of
    independent models (user ruling 2026-10-15), never as the lamp.
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
            "system_count": len(systems),
            **dict(zip(
                ("mean_correlation", "effective_models"),
                effective_independent_models(correlation, systems),
            )),
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
        " bucket_ordinals_json, posterior_version FROM basket_probability_envelope{hint}"
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
        for decided_at, tz, probs, ordinals, version in envelopes:
            try:
                hours = hours_to_settlement(
                    str(day), as_of_utc=str(decided_at), settlement_timezone=str(tz))
            except (ValueError, TypeError, KeyError):
                continue
            tier = _tier(float(hours))
            if tier is not None:
                bands.setdefault(tier, []).append((decided_at, probs, ordinals, version))
        for tier, candidates in bands.items():
            picked = None
            for decided_at, probs, ordinals, version in candidates[:8]:
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
                    picked = (decided_at, probs, ordinals, market["vector"], version)
                    break
            if picked is None:
                counts["band_unpaired"] += 1
                continue
            decided_at, probs, ordinals, m, version = picked
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
                           "tier": tier, "at": str(decided_at), "version": str(version or ""), **split})
    pool_from = STAGE_VERSION_EFFECTIVE_FROM.get("P0", {}).get(
        "calibrated_native_cross_ensemble_p0_v5")

    def _since(sample: list[dict[str, Any]], moment: str | None) -> list[dict[str, Any]]:
        if not moment:
            return []
        edge = _instant(moment)
        return [r for r in sample if _instant(r["at"]) >= edge]

    long = [r for r in scored if r["tier"] != "0-12"]
    pool_all = _since(scored, pool_from)
    # Charter 255 (user 2026-10-21 「EV 与中心/形状按宪章 255 分版本段报」): the
    # 12h+ sample read version by version, oldest first; the last is the P0
    # version served most recently, the only one about today's vector.
    by_version: dict[str, list[dict[str, Any]]] = {}
    for row in long:
        by_version.setdefault(row["version"], []).append(row)
    versions = []
    for version, rows in sorted(by_version.items(), key=lambda kv: min(r["at"] for r in kv[1])):
        reading = centre_shape_reading(rows, draws=draws if version == max(
            by_version, key=lambda v: max(r["at"] for r in by_version[v])) else 0, seed=seed)
        versions.append({"version": version, "short": version.rsplit("_", 1)[-1] if version else "未标版本",
                         "first_at_utc": min(r["at"] for r in rows), "last_at_utc": max(r["at"] for r in rows),
                         **{k: reading.get(k) for k in ("decisions", "station_days", "days", "gap", "centre",
                                                        "shape", "centre_share", "gap_ci", "mean_signed_distance_c")}})
    if versions:
        current = max(versions, key=lambda v: v["last_at_utc"])
        current["current"] = True
    return {
        "stage": "P0",
        "versions": versions,
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
# Charter 259: trading probability calibration -- the second layer
# (user 2026-10-09: 把「校准」明确拆成两层 -- A. Distribution Calibration,
# LogScore/CRPS/Brier/PIT on the whole P(T=k); B. Trading Probability
# Calibration, binary LogLoss + reliability on the bucket actually traded:
# 「我实际下注的桶，概率是不是可信？」)
# ---------------------------------------------------------------------------

#: The first live fills (charter 123's go-live); nothing earlier reached the venue.
TRADED_CALIBRATION_SINCE = "2026-09-06"
#: Reliability bins (user 2026-10-10 「要回答的问题是预测30%的桶是否约有30%发
#: 生」): 0–0.05, 0.05–0.1, then tenths -- the bins the single-system
#: binary-LogLoss protocol reads its REL/ECE on, so the two pages agree.
TRADED_CALIBRATION_BINS = (0.0, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0001)
#: An order event that only the exchange sends: the leg reached the venue.
_EXCHANGE_EVENTS = frozenset({"POST_ACK", "FILL", "PARTIAL_FILL", "CANCEL_ACK", "EXPIRED", "LATE_FILL"})
#: A slice is reported only from this many legs (the research cut, round 11).
TRADED_CALIBRATION_MIN_LEGS = 15
TRADED_CALIBRATION_BOOTSTRAP_DRAWS = 2000
TRADED_CALIBRATION_SEED = 20261009
#: The market mid is read from the newest committed book at most this far
#: before the intent (the lanes send on a book this fresh or fresher).
TRADED_CALIBRATION_BOOK_WINDOW_SEC = 1800
#: Scores clip every probability here (the served vector is already floored by
#: charter 224; the market mid and the paid price are not vectors).
TRADED_CALIBRATION_CLIP = 1e-3
#: The stage read for a leg: the belief standing at the instant (charter 260,
#: ``stage_authority.served_belief_row``), not the newest of any stage.
#: ``scripts/live_book.LANE_DECISION_RE``: a lane's decision id carries the lane
#: before ``_decision_``; the basket's decision ids carry no lane.
_TRADED_LANE_RE = re.compile(r"^(?P<lane>[a-z0-9_]+?)_decision_[A-Z0-9]{3,5}_\d{4}-\d{2}-\d{2}")
_TRADED_SIDE_BANDS = (("p−q < 0", -1.0, 0.0), ("p−q 0–0.05", 0.0, 0.05), ("p−q 0.05–0.10", 0.05, 0.10),
                      ("p−q 0.10–0.20", 0.10, 0.20), ("p−q ≥ 0.20", 0.20, 1.01))
_TRADED_PRICE_BANDS = (("价 < 0.20", 0.0, 0.20), ("价 0.20–0.40", 0.20, 0.40), ("价 0.40–0.60", 0.40, 0.60),
                       ("价 0.60–0.80", 0.60, 0.80), ("价 ≥ 0.80", 0.80, 1.01))
_TRADED_LEAD_BANDS = (("距结算 < 12h", -1e9, 12.0), ("距结算 12–36h", 12.0, 36.0), ("距结算 36–60h", 36.0, 60.0),
                      ("距结算 ≥ 60h", 60.0, 1e9))


def _traded_lane(decision_id: str) -> str:
    match = _TRADED_LANE_RE.match(str(decision_id or ""))
    return match.group("lane") if match else "basket"


def _binary_logloss(p: float, hit: float) -> float:
    clipped = min(1.0 - TRADED_CALIBRATION_CLIP, max(TRADED_CALIBRATION_CLIP, float(p)))
    return -(hit * math.log(clipped) + (1.0 - hit) * math.log(1.0 - clipped))


def _served_belief_vector(
    conn: sqlite3.Connection, station: str, day: str, at_utc: str, cache: dict[tuple[str, str, str], Any],
) -> tuple[str | None, dict[str, float] | None, dict[str, int] | None, float | None]:
    """The most informed committed served vector at ``at_utc`` -- P1 if one
    stands, else P0-M, else P0 (the chain's precedence, charter 229) -- floored
    as serving floors it (charter 224) and renormalised, with its ladder order
    and its rung in °C (the bounded buckets' width, from the row's own edges --
    never a unit constant, charter 53).

    Not "the newest of any stage": a P0-M decision committed after a P1 on the
    same day is the staler belief (2026-10-09: same-day P0-M past the local
    peak keeps 26% of its mass above its mode where the truth lands 1.4% of
    the time, LogScore 0.85 against P1's 0.18 on the same station-day-hours).
    """

    from .materialization import floor_probabilities  # noqa: PLC0415

    key = (station, day, at_utc)
    if key in cache:
        return cache[key]
    from .stage_authority import served_belief_row  # noqa: PLC0415

    belief = served_belief_row(conn, station_id=station, event_date=day, at_utc=at_utc)
    row = None if belief is None else (belief["history_id"], belief["stage"])
    if row is None:
        cache[key] = (None, None, None, None)
        return cache[key]
    # Consecutive instants usually stand on the same decision; read it once.
    if ("history", row[0]) in cache:
        cache[key] = cache[("history", row[0])]
        return cache[key]
    raw: dict[str, float] = {}
    order: dict[str, int] = {}
    widths: list[float] = []
    for b, v, o, lower, upper in conn.execute(
        "SELECT bucket, probability, ordinal, lower_c, upper_c FROM basket_probability_bridge_bucket_probability"
        " WHERE history_id=?",
        (row[0],),
    ):
        raw[str(b)], order[str(b)] = float(v), int(o)
        if lower is not None and upper is not None and float(upper) > float(lower):
            widths.append(float(upper) - float(lower))
    total = sum(raw.values())
    if total <= 0.0:
        cache[key] = (str(row[1]), None, None, None)
        return cache[key]
    floored, _lifted = floor_probabilities({b: v / total for b, v in raw.items()})
    norm = sum(floored.values()) or 1.0
    cache[key] = (str(row[1]), {b: v / norm for b, v in floored.items()}, order,
                  median(widths) if widths else None)
    cache[("history", row[0])] = cache[key]
    return cache[key]


def _traded_served_vector(
    conn: sqlite3.Connection, station: str, day: str, at_utc: str, cache: dict[tuple[str, str, str], Any],
) -> tuple[str | None, dict[str, float] | None]:
    stage, vector, _order, _rung = _served_belief_vector(conn, station, day, at_utc, cache)
    return stage, vector


def _traded_market_mid(
    conn: sqlite3.Connection, station: str, day: str, at_utc: str, cache: dict[tuple[str, str, str], Any],
) -> dict[str, float] | None:
    """The normalised two-sided mid of the newest committed book inside the
    window before ``at_utc``; None when the ladder is not healthy (fewer than
    three two-sided buckets, or the mids do not sum near one)."""

    key = (station, day, at_utc)
    if key in cache:
        return cache[key]
    floor = (_utc_instant(at_utc) - timedelta(seconds=TRADED_CALIBRATION_BOOK_WINDOW_SEC)).isoformat()
    floor = floor.replace("+00:00", "Z")
    row = conn.execute(
        "SELECT buckets_json FROM basket_official_ws_snapshot"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED' AND observed_at<=? AND observed_at>=?"
        " ORDER BY observed_at DESC LIMIT 1",
        (station, day, at_utc, floor),
    ).fetchone()
    mids: dict[str, float] = {}
    if row is not None:
        try:
            buckets = json.loads(row[0] or "[]")
        except (TypeError, ValueError):
            buckets = []
        for b in buckets if isinstance(buckets, list) else []:
            bid, ask = b.get("bid_price"), b.get("best_ask")
            if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and 0.0 < ask - bid < 0.2:
                mids[str(b.get("bucket"))] = (float(bid) + float(ask)) / 2.0
    total = sum(mids.values())
    if len(mids) < 3 or not 0.85 <= total <= 1.2:
        cache[key] = None
        return None
    cache[key] = {b: v / total for b, v in mids.items()}
    return cache[key]


def _utc_instant(value: str) -> datetime:
    text = str(value or "").replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def traded_calibration_legs(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Every live-authorised BUY intent since ``TRADED_CALIBRATION_SINCE`` on a
    settled station-day, with our served probability and the market's mid for
    the leg's side at the instant it was minted, what it paid, and whether
    it won.  ``filled`` marks the legs that became positions."""

    from .label_knowability import knowable_labels  # noqa: PLC0415
    from .settlement_time import hours_to_settlement_if_known  # noqa: PLC0415

    labels = knowable_labels(conn)
    counts = {"intents": 0, "no_label": 0, "no_served_p": 0, "bucket_off_vector": 0, "no_market_mid": 0, "legs": 0}
    served_cache: dict[tuple[str, str, str], Any] = {}
    mid_cache: dict[tuple[str, str, str], Any] = {}
    legs: list[dict[str, Any]] = []
    rows = conn.execute(
        "SELECT i.order_intent_id, i.decision_id, c.station_id, c.event_date, c.settlement_timezone, i.bucket,"
        " i.side, i.limit_price, i.created_at, COALESCE(r.filled_size, 0), COALESCE(r.filled_notional_usdc, 0)"
        " FROM basket_target_order_intents i"
        " JOIN basket_target_execution_cycles c ON c.cycle_id = i.cycle_id"
        " LEFT JOIN basket_target_order_reconciliations r ON r.order_intent_id = i.order_intent_id"
        " WHERE i.live_submit_authorized=1 AND i.order_action='BUY' AND i.created_at>=?"
        " ORDER BY i.created_at",
        (TRADED_CALIBRATION_SINCE,),
    ).fetchall()
    for intent_id, decision_id, station, day, tz, bucket, side, limit_price, created, filled, notional in rows:
        counts["intents"] += 1
        label = labels.get((str(station), str(day)))
        if label is None:
            counts["no_label"] += 1
            continue
        stage, vector = _traded_served_vector(conn, str(station), str(day), str(created), served_cache)
        if vector is None:
            counts["no_served_p"] += 1
            continue
        if str(bucket) not in vector:
            counts["bucket_off_vector"] += 1
            continue
        mids = _traded_market_mid(conn, str(station), str(day), str(created), mid_cache)
        if mids is None or str(bucket) not in mids:
            counts["no_market_mid"] += 1
            continue
        p_yes, q_yes = float(vector[str(bucket)]), float(mids[str(bucket)])
        win_yes = 1.0 if str(label["bucket"]) == str(bucket) else 0.0
        is_no = str(side).upper() == "NO"
        p, q, win = ((1.0 - p_yes, 1.0 - q_yes, 1.0 - win_yes) if is_no else (p_yes, q_yes, win_yes))
        filled_size = float(filled or 0.0)
        paid = (float(notional) / filled_size) if filled_size > 0 and notional else float(limit_price)
        hours = hours_to_settlement_if_known(str(day), as_of_utc=str(created), settlement_timezone=tz)
        events = conn.execute(
            "SELECT event_type, observed_at, matched_size FROM basket_target_order_events"
            " WHERE order_intent_id=? ORDER BY observed_at", (str(intent_id),),
        ).fetchall()
        legs.append({
            "order_intent_id": str(intent_id), "lane": _traded_lane(str(decision_id)), "station_id": str(station),
            "event_date": str(day), "bucket": str(bucket), "side": "NO" if is_no else "YES", "stage": stage,
            "unit": "F" if "°F" in str(bucket) else "C", "created_at": str(created), "hours": hours,
            "settled_bucket": str(label["bucket"]),
            "p": p, "q": q, "paid": paid, "win": win, "filled": filled_size > 0,
            "shares": filled_size, "usdc": float(notional or 0.0) if filled_size > 0 else 0.0,
            # Whether the venue ever saw it (a refused or never-sent order had
            # no chance to fill), its first fill, and its last event.
            "reached": filled_size > 0 or any(str(e[0]) in _EXCHANGE_EVENTS for e in events),
            "filled_at": min((str(e[1]) for e in events if float(e[2] or 0.0) > 0.0), default=None),
            "end_at": str(events[-1][1]) if events else None,
        })
        counts["legs"] += 1
    return legs, counts


def _traded_mean(values: Sequence[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


def _traded_day_bootstrap(legs: Sequence[Mapping[str, Any]], diff: Sequence[float]) -> list[float] | None:
    """95% interval of the mean paired difference, resampling event days."""

    by_day: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for leg, d in zip(legs, diff):
        acc = by_day[str(leg["event_date"])]
        acc[0] += float(d)
        acc[1] += 1.0
    blocks = list(by_day.values())
    if len(blocks) < 3:
        return None
    rng = random.Random(TRADED_CALIBRATION_SEED)
    draws: list[float] = []
    for _ in range(TRADED_CALIBRATION_BOOTSTRAP_DRAWS):
        total = count = 0.0
        for _ in blocks:
            b = blocks[rng.randrange(len(blocks))]
            total += b[0]
            count += b[1]
        if count > 0:
            draws.append(total / count)
    if not draws:
        return None
    draws.sort()
    return [draws[int(0.025 * len(draws))], draws[int(0.975 * len(draws)) - 1]]


def traded_calibration_group(legs: Sequence[Mapping[str, Any]], *, min_legs: int | None = None) -> dict[str, Any] | None:
    """One row of the second layer: our p, the market's q and the paid price
    scored on the same legs -- binary log loss, Brier, said vs landed, the
    share of legs where ours is closer to the outcome, and the paired
    ours−market log-loss difference with a day-bootstrap interval."""

    if len(legs) < max(1, TRADED_CALIBRATION_MIN_LEGS if min_legs is None else min_legs):
        return None
    ll_p = [_binary_logloss(l["p"], l["win"]) for l in legs]
    ll_q = [_binary_logloss(l["q"], l["win"]) for l in legs]
    ll_paid = [_binary_logloss(l["paid"], l["win"]) for l in legs]
    diff = [a - b for a, b in zip(ll_p, ll_q)]
    filled = [l for l in legs if l.get("filled")]
    cost = sum(float(l["paid"]) * float(l["shares"]) for l in filled)
    payout = sum(float(l["win"]) * float(l["shares"]) for l in filled)
    return {
        "legs": len(legs), "event_days": len({l["event_date"] for l in legs}),
        "filled_legs": len(filled), "usdc": round(cost, 2),
        "logloss_ours": _traded_mean(ll_p), "logloss_market": _traded_mean(ll_q), "logloss_paid": _traded_mean(ll_paid),
        "logloss_diff": _traded_mean(diff), "logloss_diff_ci": _traded_day_bootstrap(legs, diff),
        "brier_ours": _traded_mean([(l["p"] - l["win"]) ** 2 for l in legs]),
        "brier_market": _traded_mean([(l["q"] - l["win"]) ** 2 for l in legs]),
        "said_ours": _traded_mean([l["p"] for l in legs]), "said_market": _traded_mean([l["q"] for l in legs]),
        "paid": _traded_mean([l["paid"] for l in legs]), "landed": _traded_mean([l["win"] for l in legs]),
        "ours_closer_share": _traded_mean([1.0 if abs(l["p"] - l["win"]) < abs(l["q"] - l["win"]) else 0.0 for l in legs]),
        "return": ((payout / cost) - 1.0) if cost > 0 else None,
    }


def _wilson(hits: float, n: int) -> list[float] | None:
    """95% Wilson interval of a landed rate -- a thin bin reads as thin."""

    if n <= 0:
        return None
    z = 1.959963984540054
    rate = hits / n
    denom = 1.0 + z * z / n
    centre = (rate + z * z / (2.0 * n)) / denom
    half = z * math.sqrt(rate * (1.0 - rate) / n + z * z / (4.0 * n * n)) / denom
    return [max(0.0, centre - half), min(1.0, centre + half)]


def reliability_summary(bins: Sequence[Mapping[str, Any]], said: str, count: str) -> dict[str, Any]:
    """Murphy's REL (Σ n_k (p̄_k − ō_k)² / N) and ECE (Σ n_k |p̄_k − ō_k| / N)
    of one reliability diagram: how far what was said sits from what landed."""

    n = sum(int(b[count]) for b in bins)
    if not n:
        return {"n": 0, "rel": None, "ece": None}
    return {"n": n,
            "rel": sum(b[count] * (b[said] - b["landed"]) ** 2 for b in bins) / n,
            "ece": sum(b[count] * abs(b[said] - b["landed"]) for b in bins) / n}


def traded_calibration_reliability(legs: Sequence[Mapping[str, Any]], key: str) -> list[dict[str, Any]]:
    """Reliability by bin of ``key`` (``p`` ours, ``q`` market, ``paid`` the
    price): what was said by each and what landed, with the landed rate's 95%
    interval, so the diagrams are read side by side on the same legs."""

    out: list[dict[str, Any]] = []
    for lo, hi in zip(TRADED_CALIBRATION_BINS[:-1], TRADED_CALIBRATION_BINS[1:]):
        inside = [l for l in legs if lo <= float(l[key]) < hi]
        if not inside:
            continue
        hits = sum(float(l["win"]) for l in inside)
        out.append({
            "bin": [lo, min(hi, 1.0)], "legs": len(inside),
            "said_ours": _traded_mean([l["p"] for l in inside]), "said_market": _traded_mean([l["q"] for l in inside]),
            "paid": _traded_mean([l["paid"] for l in inside]), "landed": hits / len(inside),
            "landed_ci": _wilson(hits, len(inside)),
        })
    return out


def _traded_reliability_set(legs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The three reliability diagrams of a set of legs -- by our p, by the
    market's q, by the paid price -- each with its REL, ECE and binary LogLoss."""

    out: dict[str, Any] = {}
    summary: dict[str, Any] = {}
    for name, key, said in (("ours", "p", "said_ours"), ("market", "q", "said_market"), ("paid", "paid", "paid")):
        bins = traded_calibration_reliability(legs, key)
        out[f"reliability_{name}"] = bins
        summary[name] = {**reliability_summary(bins, said, "legs"),
                         "logloss": _traded_mean([_binary_logloss(l[key], l["win"]) for l in legs])}
    out["reliability_summary"] = summary
    return out


def traded_calibration_reliability_by_lane(legs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The two reliability diagrams per lane (user 2026-10-09 「可靠性图（已成交
    腿），按照车道区分」): each lane's legs binned by our p and by the market's
    q, with what that lane said and what landed overall.  No leg floor -- a
    bin carries its count, so a thin lane reads as thin."""

    out: list[dict[str, Any]] = []
    for lane in sorted({str(l["lane"]) for l in legs}):
        inside = [l for l in legs if l["lane"] == lane]
        out.append({
            "lane": lane, "legs": len(inside), "event_days": len({l["event_date"] for l in inside}),
            "said_ours": _traded_mean([l["p"] for l in inside]), "said_market": _traded_mean([l["q"] for l in inside]),
            "paid": _traded_mean([l["paid"] for l in inside]), "landed": _traded_mean([l["win"] for l in inside]),
            **_traded_reliability_set(inside),
        })
    return out


def traded_calibration_ladder_by_lane(
    conn: sqlite3.Connection, legs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Every bucket of the ladder at the instants each lane bought (user
    2026-10-09 「校准每个桶的发生概率和实际买入的桶：按照车道区分」): the
    vector the lane read, scored on the whole ladder beside the market
    reference, so a lane's bought bucket can be read against the ladder it was
    picked from.  One ladder per distinct (station-day, our vector, market
    vector) a lane bought on; the whole counts each such ladder once."""

    served_cache: dict[tuple[str, str, str], Any] = {}
    market_cache: dict[tuple[str, str, str], Any] = {}
    counts = {"legs": 0, "no_market_reference": 0, "ladder_mismatch": 0}
    by_lane: dict[str, list[dict[str, Any]]] = defaultdict(list)
    whole: list[dict[str, Any]] = []
    seen_lane: set[Any] = set()
    seen: set[Any] = set()
    for leg in legs:
        counts["legs"] += 1
        station, day, at = str(leg["station_id"]), str(leg["event_date"]), str(leg["created_at"])
        stage, p, order, _rung = _served_belief_vector(conn, station, day, at, served_cache)
        q = _ladder_market_reference(conn, station, day, at, market_cache)
        if p is None or order is None or q is None:
            counts["no_market_reference"] += 1
            continue
        ladder = _ladder_rows(station=station, day=day, at=at, stage=stage, hours=leg.get("hours"),
                              settled=str(leg["settled_bucket"]), p=p, order=order, q=q)
        if ladder is None:
            counts["ladder_mismatch"] += 1
            continue
        ident = (station, day, tuple(sorted(p.items())), tuple(sorted(q.items())))
        if (leg["lane"], ident) not in seen_lane:
            seen_lane.add((leg["lane"], ident))
            by_lane[str(leg["lane"])].extend(ladder)
        if ident not in seen:
            seen.add(ident)
            whole.extend(ladder)
    return {
        "counts": counts, "all": bucket_calibration_group(whole),
        "lanes": [{"lane": lane, **(bucket_calibration_group(rows) or {}), **bucket_reliability_set(rows)}
                  for lane, rows in sorted(by_lane.items())],
    }


def _event_week(day: str) -> str:
    """The Monday of the event day's week (the 「日期」 cut, week by week)."""

    d = date.fromisoformat(str(day))
    return (d - timedelta(days=d.weekday())).isoformat()


def _side(value: float | None, is_no: bool) -> float | None:
    return None if value is None else ((1.0 - float(value)) if is_no else float(value))


def _ladder_leg_rows(conn: sqlite3.Connection, legs: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Each leg on its bucket with the every-bucket sample's market reference
    at the order instant (``_ladder_market_reference``) and the belief standing
    then; side-oriented (a NO leg's p, q and outcome are the NO side's)."""

    served_cache: dict[tuple[str, str, str], Any] = {}
    market_cache: dict[tuple[str, str, str], Any] = {}
    counts = {"legs": 0, "no_market_reference": 0, "ladder_mismatch": 0}
    rows: list[dict[str, Any]] = []
    for leg in legs:
        counts["legs"] += 1
        station, day, at, bucket = str(leg["station_id"]), str(leg["event_date"]), str(leg["created_at"]), str(leg["bucket"])
        _stage, p, _order, _rung = _served_belief_vector(conn, station, day, at, served_cache)
        q = _ladder_market_reference(conn, station, day, at, market_cache)
        if p is None or q is None:
            counts["no_market_reference"] += 1
            continue
        if bucket not in p or bucket not in q:
            counts["ladder_mismatch"] += 1
            continue
        is_no = leg["side"] == "NO"
        row = {k: leg[k] for k in ("order_intent_id", "lane", "station_id", "event_date", "bucket", "side", "hours",
                                   "created_at", "filled_at", "end_at", "paid", "win")}
        row.update({"at": at, "filled": bool(leg["filled"]), "reached": bool(leg["reached"]),
                    "p": _side(p[bucket], is_no), "q": _side(q[bucket], is_no)})
        row["ll_p"], row["ll_q"] = _binary_logloss(row["p"], row["win"]), _binary_logloss(row["q"], row["win"])
        row["d"] = row["ll_p"] - row["ll_q"]
        rows.append(row)
    return rows, counts


def traded_calibration_funnel(rows: Sequence[Mapping[str, Any]], counts: Mapping[str, int]) -> dict[str, Any]:
    """Where the gap widens (user 2026-10-09 「下一步继续切」: 全部候选桶 vs 策略
    信号桶 vs 实际成交桶): every leg a lane sent (its signal), split into the
    ones that never reached the venue, the ones that rested and did not fill,
    and the ones that filled, each scored on its bucket with the same market
    reference as the every-bucket sample (user 2026-10-10: 控制两组的组成差异 --
    a refused order had no chance to fill)."""

    out: list[dict[str, Any]] = []
    for population, pick in (("信号桶（全部意图）", lambda r: True), ("其中没到交易所", lambda r: not r["reached"]),
                             ("其中挂上未成交", lambda r: r["reached"] and not r["filled"]),
                             ("其中成交", lambda r: r["filled"])):
        for direction, cut in (("全部", lambda r: True), ("p > q", lambda r: r["p"] > r["q"]),
                               ("p ≤ q", lambda r: r["p"] <= r["q"])):
            group = bucket_calibration_group([r for r in rows if pick(r) and cut(r)])
            if group is not None:
                out.append({"population": population, "direction": direction, **group})
    return {"counts": dict(counts), "rows": out}


def traded_calibration_paired_legs(conn: sqlite3.Connection, legs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Every bought leg paired one by one (user 2026-10-09: 把实际买入的腿与它们在
    成交前的模型概率、市场中价、最终结算结果逐笔配对，确认二元 LogLoss 的计算对象和
    概率口径完全一致): our p as the second layer scored it (the belief at the
    intent) and as the ladder reads it at the same instant, our p at the first
    fill, the two-sided mid and the ladder's market reference, the paid price,
    the settled bucket and the outcome, and each one's binary LogLoss."""

    served_cache: dict[tuple[str, str, str], Any] = {}
    market_cache: dict[tuple[str, str, str], Any] = {}
    paired: list[dict[str, Any]] = []
    for leg in sorted((l for l in legs if l["filled"]), key=lambda l: l["created_at"]):
        station, day, at, bucket = str(leg["station_id"]), str(leg["event_date"]), str(leg["created_at"]), str(leg["bucket"])
        is_no = leg["side"] == "NO"
        filled_at = leg.get("filled_at")
        _stage, p, _order, _rung = _served_belief_vector(conn, station, day, at, served_cache)
        q = _ladder_market_reference(conn, station, day, at, market_cache)
        fill_stage, p_fill = None, None
        if filled_at:
            fill_stage, fill_vector, _o, _r = _served_belief_vector(conn, station, day, filled_at, served_cache)
            p_fill = _side(fill_vector.get(bucket), is_no) if fill_vector else None
        row = {
            "created_at": at, "filled_at": filled_at, "lane": leg["lane"], "station_id": station, "event_date": day,
            "bucket": bucket, "side": leg["side"], "stage": leg["stage"], "stage_at_fill": fill_stage,
            "p": float(leg["p"]), "p_ladder": _side(p.get(bucket), is_no) if p else None, "p_at_fill": p_fill,
            "q_mid": float(leg["q"]), "q_ref": _side(q.get(bucket), is_no) if q else None, "paid": float(leg["paid"]),
            "settled_bucket": leg["settled_bucket"], "win": float(leg["win"]), "shares": float(leg["shares"]),
        }
        for name in ("p", "p_ladder", "p_at_fill", "q_mid", "q_ref", "paid"):
            row[f"ll_{name}"] = None if row[name] is None else _binary_logloss(row[name], row["win"])
        paired.append(row)

    def _mean(values: Sequence[float | None]) -> float | None:
        kept = [float(v) for v in values if v is not None]
        return (sum(kept) / len(kept)) if kept else None

    with_ref = [r for r in paired if r["q_ref"] is not None]
    with_fill = [r for r in paired if r["p_at_fill"] is not None]
    gaps = [abs(r["p"] - r["p_ladder"]) for r in paired if r["p_ladder"] is not None]
    win_check = [r for r in paired
                 if r["win"] == ((0.0 if r["settled_bucket"] == r["bucket"] else 1.0) if r["side"] == "NO"
                                 else (1.0 if r["settled_bucket"] == r["bucket"] else 0.0))]
    summary = {
        "legs": len(paired),
        "p_same_vector": sum(1 for g in gaps if g < 1e-12), "p_max_abs_gap": max(gaps) if gaps else None,
        "outcome_consistent": len(win_check),
        "q_ref_available": len(with_ref),
        "q_mean_abs_gap": _mean([abs(r["q_mid"] - r["q_ref"]) for r in with_ref]),
        "ll_q_mid_on_ref_legs": _mean([r["ll_q_mid"] for r in with_ref]),
        "ll_q_ref_on_ref_legs": _mean([r["ll_q_ref"] for r in with_ref]),
        "ll_p_on_ref_legs": _mean([r["ll_p"] for r in with_ref]),
        "fill_known": len(with_fill),
        "stage_changed_by_fill": sum(1 for r in with_fill if r["stage_at_fill"] != r["stage"]),
        "p_moved_by_fill_over_0_01": sum(1 for r in with_fill if abs(r["p_at_fill"] - r["p"]) > 0.01),
        "p_mean_abs_move_by_fill": _mean([abs(r["p_at_fill"] - r["p"]) for r in with_fill]),
        "ll_p_at_intent_on_fill_legs": _mean([r["ll_p"] for r in with_fill]),
        "ll_p_at_fill_on_fill_legs": _mean([r["ll_p_at_fill"] for r in with_fill]),
        "minutes_to_fill_median": (median([(_utc_instant(r["filled_at"]) - _utc_instant(r["created_at"])).total_seconds() / 60.0
                                           for r in with_fill]) if with_fill else None),
    }
    return {"summary": summary, "legs": paired}


# The fill step, filled against rested-unfilled on one signal set (user
# 2026-10-10: 同一信号集合比较成交与未成交、分解下单到成交期间的变化、检验成交与
# 预测误差的关系、固定规则做前瞻样本外验证) -- the rules of
# docs/research/fill_selection_forward_20261010/PROTOCOL.md.
FILL_SELECTION_PROTOCOL = "docs/research/fill_selection_forward_20261010/PROTOCOL.md"
FILL_SELECTION_FORWARD_FROM = "2026-10-11"
FILL_SELECTION_READ_ON = "2026-10-24"
FILL_SELECTION_READ_FALLBACK = "2026-11-07"
FILL_SELECTION_MIN_FILLED = 40
FILL_SELECTION_MIN_DAYS = 10
FILL_SELECTION_SEED = 20261010
FILL_SELECTION_DRAWS = 2000
#: A market move between the order and its fill smaller than this is flat.
FILL_MOVE_FLAT = 0.01


def _band_of(value: float | None, bands: Sequence[tuple[str, float, float]]) -> str:
    if value is None:
        return "—"
    for name, lo, hi in bands:
        if lo <= value < hi:
            return name
    return "—"


def _fill_stratum(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (str(row["lane"]), _band_of(row["hours"], _TRADED_LEAD_BANDS), _band_of(row["q"], _TRADED_PRICE_BANDS),
            "p>q" if row["p"] > row["q"] else "p≤q")


def _stratified_fill_gap(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Within each stratum (lane × lead × the side's market price × direction)
    the filled mean of d minus the rested-unfilled mean, pooled with weights
    n_f·n_u/(n_f+n_u) over the strata that hold both."""

    cells: dict[Any, list[list[float]]] = defaultdict(lambda: [[], []])
    for r in rows:
        cells[r["_stratum"]][0 if r["filled"] else 1].append(float(r["d"]))
    num = den = 0.0
    strata = covered_f = covered_u = 0
    for filled, unfilled in cells.values():
        if filled and unfilled:
            w = len(filled) * len(unfilled) / (len(filled) + len(unfilled))
            num += w * (sum(filled) / len(filled) - sum(unfilled) / len(unfilled))
            den += w
            strata += 1
            covered_f += len(filled)
            covered_u += len(unfilled)
    return {"estimate": (num / den) if den else None, "strata": strata,
            "filled_covered": covered_f, "unfilled_covered": covered_u}


def _matched_fill_gap(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Groups of the same lane, station-day, bucket and side holding both a
    filled and a rested-unfilled leg; the outcome is the same inside a group,
    so its gap comes from our p and the market's q at the two instants."""

    groups: dict[Any, list[list[Mapping[str, Any]]]] = defaultdict(lambda: [[], []])
    for r in rows:
        groups[(r["lane"], r["station_id"], r["event_date"], r["bucket"], r["side"])][0 if r["filled"] else 1].append(r)
    out = []
    for (lane, station, day, bucket, side), (filled, unfilled) in sorted(groups.items()):
        if filled and unfilled:
            mean = lambda xs, k: sum(float(x[k]) for x in xs) / len(xs)  # noqa: E731
            out.append({"lane": lane, "station_id": station, "event_date": day, "bucket": bucket, "side": side,
                        "filled": len(filled), "unfilled": len(unfilled),
                        "d": mean(filled, "d") - mean(unfilled, "d"),
                        "ll_p": mean(filled, "ll_p") - mean(unfilled, "ll_p"),
                        "ll_q": mean(filled, "ll_q") - mean(unfilled, "ll_q")})
    return out


def _fill_regression(rows: Sequence[Mapping[str, Any]]) -> tuple[float | None, int]:
    """d ~ filled + lane + lead band + price band + direction + |p−q|, least
    squares; the filled coefficient."""

    import numpy as np  # noqa: PLC0415

    if len(rows) < 8 or not any(r["filled"] for r in rows) or all(r["filled"] for r in rows):
        return None, 0
    levels = [sorted({r["_stratum"][i] for r in rows}) for i in range(3)]
    columns = []
    for r in rows:
        x = [1.0, 1.0 if r["filled"] else 0.0]
        for i, values in enumerate(levels):
            x.extend(1.0 if r["_stratum"][i] == v else 0.0 for v in values[1:])
        x.extend([1.0 if r["p"] > r["q"] else 0.0, abs(float(r["p"]) - float(r["q"]))])
        columns.append(x)
    beta, *_rest = np.linalg.lstsq(np.asarray(columns), np.asarray([float(r["d"]) for r in rows]), rcond=None)
    return float(beta[1]), len(columns[0])


def _day_draws(rows: Sequence[Mapping[str, Any]], statistic: Any, *, draws: int = FILL_SELECTION_DRAWS) -> list[float] | None:
    """95% interval of ``statistic`` over whole event days resampled."""

    by_day: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        by_day[str(r["event_date"])].append(r)
    days = list(by_day)
    if len(days) < 3:
        return None
    rng = random.Random(FILL_SELECTION_SEED)
    values = []
    for _ in range(draws):
        sample = [r for _d in days for r in by_day[days[rng.randrange(len(days))]]]
        got = statistic(sample)
        if got is not None:
            values.append(got)
    if len(values) < 10:
        return None
    values.sort()
    return [values[int(0.025 * len(values))], values[int(0.975 * len(values)) - 1]]


def _fill_selection_reading(rows: Sequence[Mapping[str, Any]], *, draws: int = FILL_SELECTION_DRAWS) -> dict[str, Any]:
    """The three estimates of the protocol on one sample: the stratified gap
    (primary), the matched groups and the controlled regression, each with
    its event-day interval, and the raw filled-minus-unfilled gap beside."""

    sample = [r for r in rows if r["reached"]]
    filled = [r for r in sample if r["filled"]]
    unfilled = [r for r in sample if not r["filled"]]
    raw = (lambda xs: (sum(r["d"] for r in xs if r["filled"]) / max(1, sum(1 for r in xs if r["filled"]))
                       - sum(r["d"] for r in xs if not r["filled"]) / max(1, sum(1 for r in xs if not r["filled"])))
           if any(r["filled"] for r in xs) and any(not r["filled"] for r in xs) else None)
    stratified = _stratified_fill_gap(sample)
    matched = _matched_fill_gap(sample)

    def _matched_mean(xs: Sequence[Mapping[str, Any]], key: str = "d") -> float | None:
        got = _matched_fill_gap(xs)
        return (sum(g[key] for g in got) / len(got)) if got else None

    return {
        "filled": len(filled), "unfilled": len(unfilled),
        "event_days": len({r["event_date"] for r in sample}),
        "filled_event_days": len({r["event_date"] for r in filled}),
        "mean_d_filled": _traded_mean([r["d"] for r in filled]), "mean_d_unfilled": _traded_mean([r["d"] for r in unfilled]),
        "raw_gap": raw(sample), "raw_gap_ci": _day_draws(sample, raw, draws=draws),
        "stratified": {**stratified, "ci": _day_draws(sample, lambda xs: _stratified_fill_gap(xs)["estimate"], draws=draws)},
        "matched": {"groups": len(matched), "filled": sum(g["filled"] for g in matched),
                    "unfilled": sum(g["unfilled"] for g in matched),
                    "gap": _matched_mean(sample), "gap_ci": _day_draws(sample, _matched_mean, draws=draws),
                    "gap_ll_p": _matched_mean(sample, "ll_p"), "gap_ll_q": _matched_mean(sample, "ll_q")},
        "regression": {"coefficient": _fill_regression(sample)[0], "columns": _fill_regression(sample)[1],
                       "ci": _day_draws(sample, lambda xs: _fill_regression(xs)[0], draws=max(200, draws // 4))},
    }


#: A fill this soon after its order was a take: nothing happened in between.
FILL_INSTANT_MINUTES = 1.0


def _ladder_market_reference_near(
    conn: sqlite3.Connection, station: str, day: str, at_utc: str, cache: dict[tuple[str, str, str], Any],
) -> tuple[dict[str, float] | None, str | None]:
    """The market reference at ``at_utc`` for the order-to-fill decomposition:
    the newest committed book in the window before it, else the first one in
    the window after it.  Committed books exist only where production read the
    station-day, so a maker leg that rested half an hour often has none
    before its fill (2026-10-10: 12 of 112 filled legs, all maker, 30–186 min
    of rest); the book just after the fill is what the market said then."""

    before = _ladder_market_reference(conn, station, day, at_utc, cache)
    if before is not None:
        return before, "before"
    from .basket_kernel.valuation_v1 import market_reference_v1  # noqa: PLC0415

    ceiling = (_utc_instant(at_utc) + timedelta(seconds=TRADED_CALIBRATION_BOOK_WINDOW_SEC))
    snap = conn.execute(
        "SELECT buckets_json FROM basket_official_ws_snapshot"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED' AND observed_at>=? AND observed_at<=?"
        " ORDER BY observed_at ASC, sequence ASC LIMIT 1",
        (station, day, at_utc, ceiling.isoformat().replace("+00:00", "Z")),
    ).fetchone()
    if snap is None:
        return None, None
    try:
        market = market_reference_v1(json.loads(snap[0] or "[]"))
    except (TypeError, ValueError):
        return None, None
    if market.get("state") != "complete":
        return None, None
    return {str(b): float(v) for b, v in market["vector"].items()}, "after"


def _order_to_fill(conn: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """What moved between the order and its end (user 2026-10-10: 比较下单时市场
    中价、成交时中价、成交价与最终结算；中位间隔 1.9 分钟并不能排除期间的信息变化):
    for a filled leg the instant of its first fill, for a rested-unfilled leg
    its last event (cancel or expiry); the market reference and our belief at
    that instant, against the ones at the order."""

    served_cache: dict[tuple[str, str, str], Any] = {}
    market_cache: dict[tuple[str, str, str], Any] = {}
    moved: list[dict[str, Any]] = []
    books = {"before": 0, "after": 0, "none": 0}
    for r in rows:
        if not r["reached"]:
            continue
        at = r["filled_at"] if r["filled"] else r["end_at"]
        if not at:
            continue
        is_no = r["side"] == "NO"
        _stage, p_at, _o, _r = _served_belief_vector(conn, r["station_id"], r["event_date"], str(at), served_cache)
        q_at, side_of_book = _ladder_market_reference_near(conn, r["station_id"], r["event_date"], str(at), market_cache)
        if p_at is None or q_at is None or r["bucket"] not in p_at or r["bucket"] not in q_at:
            books["none"] += 1
            continue
        books[str(side_of_book)] += 1
        moved.append({**r, "end": str(at), "p_end": _side(p_at[r["bucket"]], is_no), "q_end": _side(q_at[r["bucket"]], is_no),
                      "book": side_of_book,
                      "minutes": (_utc_instant(str(at)) - _utc_instant(r["created_at"])).total_seconds() / 60.0})

    def _summary(xs: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
        if not xs:
            return None
        move = [x["q_end"] - x["q"] for x in xs]
        out = {
            "legs": len(xs), "event_days": len({x["event_date"] for x in xs}),
            "minutes_median": median([x["minutes"] for x in xs]),
            "q_order": _traded_mean([x["q"] for x in xs]), "q_end": _traded_mean([x["q_end"] for x in xs]),
            "p_order": _traded_mean([x["p"] for x in xs]), "p_end": _traded_mean([x["p_end"] for x in xs]),
            "landed": _traded_mean([x["win"] for x in xs]),
            "move": _traded_mean(move),
            "share_down": sum(1 for m in move if m < -FILL_MOVE_FLAT) / len(xs),
            "share_up": sum(1 for m in move if m > FILL_MOVE_FLAT) / len(xs),
            "ll_q_order": _traded_mean([x["ll_q"] for x in xs]),
            "ll_q_end": _traded_mean([_binary_logloss(x["q_end"], x["win"]) for x in xs]),
            "ll_p_order": _traded_mean([x["ll_p"] for x in xs]),
            "ll_p_end": _traded_mean([_binary_logloss(x["p_end"], x["win"]) for x in xs]),
            "ll_paid": _traded_mean([_binary_logloss(x["paid"], x["win"]) for x in xs]),
            "paid": _traded_mean([x["paid"] for x in xs]),
        }
        out["d_order"] = out["ll_p_order"] - out["ll_q_order"]
        out["d_end"] = out["ll_p_end"] - out["ll_q_end"]
        return out

    filled = [x for x in moved if x["filled"]]
    instant = [x for x in filled if x["minutes"] <= FILL_INSTANT_MINUTES]
    waited = [x for x in filled if x["minutes"] > FILL_INSTANT_MINUTES]
    unfilled = [x for x in moved if not x["filled"]]
    by_move = []
    for name, pick in (("市场在成交前压低我方这一边（q 降 > 0.01）", lambda x: x["q_end"] - x["q"] < -FILL_MOVE_FLAT),
                       ("基本不动（|Δq| ≤ 0.01）", lambda x: abs(x["q_end"] - x["q"]) <= FILL_MOVE_FLAT),
                       ("市场在成交前抬高我方这一边（q 升 > 0.01）", lambda x: x["q_end"] - x["q"] > FILL_MOVE_FLAT)):
        got = _summary([x for x in waited if pick(x)])
        if got is not None:
            by_move.append({"move_class": name, **got})
    return {"filled": _summary(filled), "filled_instant": _summary(instant), "filled_waited": _summary(waited),
            "unfilled": _summary(unfilled), "waited_by_move": by_move, "books": books,
            "flat": FILL_MOVE_FLAT, "instant_minutes": FILL_INSTANT_MINUTES}


def _forward_status(reading: Mapping[str, Any], previous: Mapping[str, Any] | None) -> dict[str, Any]:
    """The protocol's one judgement, carried unchanged once made."""

    prior = dict((previous or {}).get("judgement") or {})
    if prior.get("verdict"):
        return prior
    today = _now().date().isoformat()
    enough = (reading["filled"] >= FILL_SELECTION_MIN_FILLED
              and reading["filled_event_days"] >= FILL_SELECTION_MIN_DAYS)
    if today < FILL_SELECTION_READ_ON:
        return {"state": "accumulating"}
    if not prior.get("first_check_at"):
        prior["first_check_at"] = today
        if not enough:
            return {**prior, "state": "deferred"}
    elif today < FILL_SELECTION_READ_FALLBACK:
        return {**prior, "state": "deferred"}
    if not enough:
        return {**prior, "state": "judged", "verdict": "样本不足，不判", "judged_at": today}
    ci = reading["stratified"].get("ci")
    verdict = ("前瞻不成立（跨零）" if not ci or ci[0] <= 0.0 <= ci[1]
               else ("成交这一步放大劣势：前瞻成立" if ci[0] > 0.0 else "反向成立：成交的腿更准"))
    return {**prior, "state": "judged", "verdict": verdict, "judged_at": today,
            "estimate": reading["stratified"].get("estimate"), "ci": ci,
            "filled": reading["filled"], "filled_event_days": reading["filled_event_days"]}


def traded_calibration_fill_selection(conn: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    staged = [{**r, "_stratum": _fill_stratum(r)} for r in rows]
    in_sample = [r for r in staged if r["event_date"] < FILL_SELECTION_FORWARD_FROM]
    forward = [r for r in staged if r["event_date"] >= FILL_SELECTION_FORWARD_FROM]
    forward_reading = _fill_selection_reading(forward)
    try:
        previous = ((load_reading("traded_calibration") or {}).get("value") or {}).get("fill_selection", {}).get("forward")
    except Exception:  # noqa: BLE001 -- no previous reading is a first reading
        previous = None
    return {
        "protocol": FILL_SELECTION_PROTOCOL, "forward_from": FILL_SELECTION_FORWARD_FROM,
        "read_on": FILL_SELECTION_READ_ON, "read_fallback": FILL_SELECTION_READ_FALLBACK,
        "min_filled": FILL_SELECTION_MIN_FILLED, "min_days": FILL_SELECTION_MIN_DAYS,
        "in_sample": {**_fill_selection_reading(in_sample), "order_to_fill": _order_to_fill(conn, in_sample)},
        "forward": {**forward_reading, "judgement": _forward_status(forward_reading, previous)},
    }


def _traded_slices(legs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    days = sorted({l["event_date"] for l in legs})
    split = days[len(days) // 2] if days else None
    specs: list[tuple[str, str, Any]] = []
    # Every lane gets its row whatever its count (user 2026-10-09 「为什么没有
    # proper车道的数据，增加」: Proper had 14 filled legs, one under the floor);
    # the other groups keep the floor.
    lanes = sorted({l["lane"] for l in legs})
    for lane in lanes:
        specs.append(("车道", lane, lambda l, v=lane: l["lane"] == v))
    for side in ("YES", "NO"):
        specs.append(("方向", side, lambda l, v=side: l["side"] == v))
    for stage in ("P0", "P0-M", "P1"):
        specs.append(("阶段", stage, lambda l, v=stage: l["stage"] == v))
    for unit in ("C", "F"):
        specs.append(("结算单位", unit, lambda l, v=unit: l["unit"] == v))
    for name, lo, hi in _TRADED_SIDE_BANDS:
        specs.append(("我方对市场", name, lambda l, a=lo, b=hi: a <= l["p"] - l["q"] < b))
    for name, lo, hi in _TRADED_PRICE_BANDS:
        specs.append(("实付价", name, lambda l, a=lo, b=hi: a <= l["paid"] < b))
    for name, lo, hi in _TRADED_LEAD_BANDS:
        specs.append(("提前量", name, lambda l, a=lo, b=hi: l["hours"] is not None and a <= l["hours"] < b))
    if split:
        specs.append(("时段", f"前半（< {split}）", lambda l, v=split: l["event_date"] < v))
        specs.append(("时段", f"后半（≥ {split}）", lambda l, v=split: l["event_date"] >= v))
    for week in sorted({_event_week(l["event_date"]) for l in legs}):
        specs.append(("事件周", f"{week} 起一周", lambda l, v=week: _event_week(l["event_date"]) == v))
    out: list[dict[str, Any]] = []
    for group, name, predicate in specs:
        row = traded_calibration_group([l for l in legs if predicate(l)], min_legs=1 if group == "车道" else None)
        if row is not None:
            out.append({"group": group, "slice": name, **row})
    return out


def traded_calibration(conn: sqlite3.Connection) -> dict[str, Any]:
    """Charter 259: the second layer on the legs the lanes bought (``filled``)
    and on every leg they sent for (``intended``) -- the whole, the slices,
    and the paired reliability diagrams."""

    legs, counts = traded_calibration_legs(conn)
    populations: dict[str, Any] = {}
    for name, subset in (("filled", [l for l in legs if l["filled"]]), ("intended", legs)):
        populations[name] = {
            "legs": len(subset),
            "all": traded_calibration_group(subset),
            "slices": _traded_slices(subset),
            **_traded_reliability_set(subset),
            "reliability_by_lane": traded_calibration_reliability_by_lane(subset),
            "lanes": sorted(Counter(l["lane"] for l in subset).items()),
        }
    populations["filled"]["ladder_by_lane"] = traded_calibration_ladder_by_lane(
        conn, [l for l in legs if l["filled"]])
    ladder_rows, ladder_counts = _ladder_leg_rows(conn, legs)
    funnel = traded_calibration_funnel(ladder_rows, ladder_counts)
    paired = traded_calibration_paired_legs(conn, legs)
    fill_selection = traded_calibration_fill_selection(conn, ladder_rows)
    return {
        "rule": "traded_bucket_binary_logloss_v1", "since": TRADED_CALIBRATION_SINCE, "counts": counts,
        "min_legs": TRADED_CALIBRATION_MIN_LEGS, "book_window_sec": TRADED_CALIBRATION_BOOK_WINDOW_SEC,
        "populations": populations, "funnel": funnel, "paired_legs": paired, "fill_selection": fill_selection,
        "recent": [
            {k: l[k] for k in ("created_at", "lane", "station_id", "event_date", "bucket", "side", "stage",
                               "p", "q", "paid", "win", "filled", "shares")}
            for l in sorted(legs, key=lambda l: l["created_at"], reverse=True)[:60]
        ],
    }

# ---------------------------------------------------------------------------
# bucket_calibration -- every bucket's occurrence probability, binary LogLoss
# (user 2026-10-09 「前端合适位置增加“校准每个桶的发生概率”统计，用指标binary
# logloss」, beside 「增加“评价实际买入的桶”，用买入样本binary logloss」 --
# the second is traded_calibration's filled population)
# ---------------------------------------------------------------------------

#: Hours-to-settlement bands; one sample per settled station-day per band.
BUCKET_CALIBRATION_BANDS = (("距结算 < 12h", -1e9, 12.0), ("距结算 12–24h", 12.0, 24.0),
                            ("距结算 24–36h", 24.0, 36.0), ("距结算 36–60h", 36.0, 60.0),
                            ("距结算 ≥ 60h", 60.0, 1e9))
BUCKET_CALIBRATION_BINS = TRADED_CALIBRATION_BINS
#: Decision instants tried per band before the band counts as unpaired.
BUCKET_CALIBRATION_TRIES = 8
_BUCKET_POSITIONS = (("市场众数桶", 0, 0), ("离众数 ±1", 1, 1), ("离众数 ±2", 2, 2), ("离众数 ≥ 3", 3, 10 ** 6))
_BUCKET_SIDE_BANDS = (("p−q < −0.20", -1.01, -0.20), ("p−q −0.20–−0.10", -0.20, -0.10),
                      ("p−q −0.10–−0.05", -0.10, -0.05), ("p−q −0.05–0", -0.05, 0.0)) + _TRADED_SIDE_BANDS[1:]
_BUCKET_PRICE_BANDS = (("市场 q < 0.05", 0.0, 0.05), ("市场 q 0.05–0.20", 0.05, 0.20), ("市场 q 0.20–0.40", 0.20, 0.40),
                       ("市场 q 0.40–0.60", 0.40, 0.60), ("市场 q ≥ 0.60", 0.60, 1.01))
#: The P0 envelope's own layers, upstream first: the fused pool of the
#: families' vectors (each already through its single-system correction --
#: the uncorrected member distributions are not kept), the distribution
#: layer's input (after the consensus bridge and the radiation term), and the
#: served P0 (centre/width, far-end tails, peak ceiling).  Each is floored as
#: serving floors (charter 224) before it is scored.
P0_LAYERS = ("pool", "input", "served")
_P0_LAYER_SQL = (
    "SELECT probabilities_json, json_extract(lineage_json,'$.native_system_probabilities_json'),"
    " json_extract(lineage_json,'$.system_weights_json'), json_extract(lineage_json,'$.center_width_json')"
    " FROM basket_probability_envelope{hint} WHERE station_id=? AND event_date=? AND state='COMMITTED'"
    " AND stage='P0' AND decision_at_utc<=? ORDER BY decision_at_utc DESC LIMIT 1"
)


def _ladder_market_reference(
    conn: sqlite3.Connection, station: str, day: str, at_utc: str, cache: dict[tuple[str, str, str], Any],
) -> dict[str, float] | None:
    """The market's whole-ladder vector (``market_reference_v1``, one-sided
    tails priced as the EV page prices them) from the newest committed book at
    most ``TRADED_CALIBRATION_BOOK_WINDOW_SEC`` before ``at_utc``; None when
    there is none or it is not complete."""

    from .basket_kernel.valuation_v1 import market_reference_v1  # noqa: PLC0415

    key = (station, day, at_utc)
    if key in cache:
        return cache[key]
    floor = (_utc_instant(at_utc) - timedelta(seconds=TRADED_CALIBRATION_BOOK_WINDOW_SEC))
    snap = conn.execute(
        "SELECT buckets_json FROM basket_official_ws_snapshot"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED' AND observed_at<=? AND observed_at>=?"
        " ORDER BY observed_at DESC, sequence DESC LIMIT 1",
        (station, day, at_utc, floor.isoformat().replace("+00:00", "Z")),
    ).fetchone()
    vector = None
    if snap is not None:
        try:
            market = market_reference_v1(json.loads(snap[0] or "[]"))
        except (TypeError, ValueError):
            market = {}
        if market.get("state") == "complete":
            vector = {str(b): float(v) for b, v in market["vector"].items()}
    cache[key] = vector
    return vector


def _ladder_rows(
    *, station: str, day: str, at: str, stage: str | None, hours: float | None, settled: str,
    p: Mapping[str, float], order: Mapping[str, int], q: Mapping[str, float], **extra: Any,
) -> list[dict[str, Any]] | None:
    """One row per bucket of the ladder -- our p_k, the market's q_k, whether
    k landed, and k's distance from the market's mode; None when the two
    vectors are not the same ladder or the settled bucket is not on it."""

    if set(p) != set(q) or set(p) != set(order) or settled not in p:
        return None
    # The market's mode; a tie goes to the lower rung, as charter 249 picks it.
    mode = max(sorted(q, key=lambda b: order[b]), key=lambda b: q[b])
    return [{
        "station_id": station, "event_date": day, "at": at, "stage": stage, "hours": hours,
        "unit": "F" if "°F" in bucket else "C", "bucket": bucket, "from_mode": abs(order[bucket] - order[mode]),
        "p": float(p[bucket]), "q": float(q[bucket]), "win": 1.0 if bucket == settled else 0.0, **extra,
    } for bucket in p]


def _ladder_scores(
    vectors: Mapping[str, Mapping[str, float]], order: Mapping[str, int], settled: str, rung_c: float | None,
) -> dict[str, float | None]:
    """The whole distribution's scores on one ladder for each named vector:
    LogScore (−log of the settled bucket, clipped as the binary scores are)
    and CRPS in °C (the ladder RPS times the rung, charter 53)."""

    keys = sorted(order, key=lambda b: order[b])
    out: dict[str, float | None] = {}
    for name, vector in vectors.items():
        out[f"logscore_{name}"] = -math.log(max(TRADED_CALIBRATION_CLIP, float(vector.get(settled, 0.0) or 0.0)))
        out[f"crps_{name}_c"] = (_ladder_rps(vector, keys, settled) * rung_c) if rung_c else None
    return out


def _p0_layers(conn: sqlite3.Connection, station: str, day: str, at_utc: str) -> dict[str, dict[str, float]] | None:
    """The P0 standing at ``at_utc``, layer by layer (``P0_LAYERS``); None when
    the envelope predates the per-family vectors (2026-09-22) or a layer is
    not on the served ladder."""

    from .materialization import floor_probabilities  # noqa: PLC0415

    params = (station, day, at_utc)
    try:
        row = conn.execute(_P0_LAYER_SQL.format(hint=" INDEXED BY basket_probability_envelope_asof_idx"), params).fetchone()
    except sqlite3.OperationalError:
        # A store without the index (fixtures): the same seek unhinted.
        row = conn.execute(_P0_LAYER_SQL.format(hint=""), params).fetchone()
    if row is None or not row[1] or not row[2] or not row[3]:
        return None
    try:
        served = {str(b): float(v) for b, v in json.loads(row[0]).items()}
        systems = json.loads(row[1])
        weights = {str(k): float(v) for k, v in json.loads(row[2]).items() if k in systems}
        given = json.loads(row[3]).get("input_probability")
    except (TypeError, ValueError, AttributeError):
        return None
    total = sum(weights.values())
    if total <= 0.0 or not isinstance(given, dict) or set(map(str, given)) != set(served):
        return None
    pool = {b: sum(w * float((systems[k] or {}).get(b, 0.0) or 0.0) for k, w in weights.items()) / total for b in served}
    out: dict[str, dict[str, float]] = {}
    for name, vector in (("pool", pool), ("input", {str(b): float(v or 0.0) for b, v in given.items()}), ("served", served)):
        mass = sum(vector.values())
        if mass <= 0.0:
            return None
        floored, _lifted = floor_probabilities({b: v / mass for b, v in vector.items()})
        norm = sum(floored.values()) or 1.0
        out[name] = {b: v / norm for b, v in floored.items()}
    return out


def bucket_calibration_rows(conn: sqlite3.Connection) -> dict[str, Any]:
    """Every bucket of the ladder, scored where the lanes could have traded it.

    Sample: each settled station-day since ``TRADED_CALIBRATION_SINCE``, and in
    each hours-to-settlement band the first committed decision instant with a
    complete market reference at most ``TRADED_CALIBRATION_BOOK_WINDOW_SEC``
    old (``_ladder_market_reference``).  Our vector is the belief standing at
    that instant (charter 260, floored as served); each bucket k of the ladder
    is one row with our p_k, the market's q_k and whether k landed.  Each
    sample also carries the whole distribution's LogScore and CRPS, and the
    P0 standing at the same instant layer by layer against the same q.
    """

    from .label_knowability import knowable_labels  # noqa: PLC0415
    from .settlement_time import hours_to_settlement_if_known  # noqa: PLC0415

    counts = {"station_days": 0, "samples": 0, "band_unpaired": 0, "ladder_mismatch": 0, "buckets": 0,
              "p0_layers": 0, "p0_layers_missing": 0}
    served_cache: dict[tuple[str, str, str], Any] = {}
    market_cache: dict[tuple[str, str, str], Any] = {}
    rows: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    layer_rows: list[dict[str, Any]] = []
    layer_samples: list[dict[str, Any]] = []
    labels = {k: v for k, v in knowable_labels(conn).items() if str(k[1]) >= TRADED_CALIBRATION_SINCE}
    for (station, day), label in sorted(labels.items()):
        # The instants alone, off the as-of index (about 190 a station-day;
        # pulling a non-indexed column with them cost a row read each, ~300 s
        # of the first build); the time zone once.
        instants = [str(r[0]) for r in conn.execute(
            "SELECT decision_at_utc FROM basket_probability_bridge_history"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED' ORDER BY decision_at_utc",
            (str(station), str(day)),
        )]
        if not instants:
            continue
        tz = conn.execute(
            "SELECT settlement_timezone FROM basket_probability_bridge_history"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED' LIMIT 1",
            (str(station), str(day)),
        ).fetchone()[0]
        counts["station_days"] += 1
        bands: dict[str, list[str]] = defaultdict(list)
        for decided in instants:
            hours = hours_to_settlement_if_known(str(day), as_of_utc=str(decided), settlement_timezone=tz)
            for name, lo, hi in BUCKET_CALIBRATION_BANDS:
                if hours is not None and lo <= hours < hi:
                    if not bands[name] or bands[name][-1] != str(decided):
                        bands[name].append(str(decided))
                    break
        for band, candidates in bands.items():
            picked = None
            for at in candidates[:BUCKET_CALIBRATION_TRIES]:
                q = _ladder_market_reference(conn, str(station), str(day), at, market_cache)
                if q is None:
                    continue
                stage, vector, order, rung = _served_belief_vector(conn, str(station), str(day), at, served_cache)
                if vector is None or order is None:
                    continue
                picked = (at, stage, vector, order, rung, q)
                break
            if picked is None:
                counts["band_unpaired"] += 1
                continue
            at, stage, p, order, rung, q = picked
            ladder = _ladder_rows(
                station=str(station), day=str(day), at=at, stage=stage, settled=str(label["bucket"]),
                hours=hours_to_settlement_if_known(str(day), as_of_utc=at, settlement_timezone=tz),
                p=p, order=order, q=q, band=band,
            )
            if ladder is None:
                counts["ladder_mismatch"] += 1
                continue
            counts["samples"] += 1
            counts["buckets"] += len(ladder)
            rows.extend(ladder)
            settled = str(label["bucket"])
            meta = {"station_id": str(station), "event_date": str(day), "at": at, "band": band, "stage": stage,
                    "unit": ladder[0]["unit"]}
            samples.append({**meta, **_ladder_scores({"ours": p, "market": q}, order, settled, rung)})
            layers = _p0_layers(conn, str(station), str(day), at)
            if layers is None or set(layers["served"]) != set(q):
                counts["p0_layers_missing"] += 1
                continue
            counts["p0_layers"] += 1
            for row in ladder:
                layer_rows.append({**{k: row[k] for k in ("station_id", "event_date", "at", "band", "bucket", "from_mode",
                                                          "q", "win")},
                                   **{f"p_{name}": layers[name][row["bucket"]] for name in P0_LAYERS}})
            layer_samples.append({**meta, **_ladder_scores({**layers, "market": q}, order, settled, rung)})
    return {"rows": rows, "samples": samples, "layer_rows": layer_rows, "layer_samples": layer_samples,
            "counts": counts}


def _paired_mean(items: Sequence[Mapping[str, Any]], a: Sequence[float | None],
                 b: Sequence[float | None]) -> tuple[float | None, list[float] | None]:
    """Mean of a−b over the items where both exist, with the event-day interval."""

    kept = [(item, float(x) - float(y)) for item, x, y in zip(items, a, b) if x is not None and y is not None]
    if not kept:
        return None, None
    diff = [d for _i, d in kept]
    return _traded_mean(diff), _traded_day_bootstrap([i for i, _d in kept], diff)


def distribution_group(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The whole distribution on the same instants: LogScore and CRPS (°C),
    ours beside the market's, paired with an event-day interval."""

    if not samples:
        return None
    out: dict[str, Any] = {"samples": len(samples), "event_days": len({s["event_date"] for s in samples})}
    for score in ("logscore", "crps"):
        suffix = "_c" if score == "crps" else ""
        ours = [s.get(f"{score}_ours{suffix}") for s in samples]
        market = [s.get(f"{score}_market{suffix}") for s in samples]
        out[f"{score}_ours"] = _traded_mean([v for v in ours if v is not None])
        out[f"{score}_market"] = _traded_mean([v for v in market if v is not None])
        out[f"{score}_diff"], out[f"{score}_diff_ci"] = _paired_mean(samples, ours, market)
    return out


def p0_layer_group(rows: Sequence[Mapping[str, Any]], samples: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The P0 layers against the market on the same buckets and instants:
    binary LogLoss per bucket, LogScore and CRPS per instant; each layer's
    difference to the market and each step's difference to the layer before
    it, with event-day intervals."""

    if not rows:
        return None
    out: dict[str, Any] = {"buckets": len(rows), "samples": len(samples),
                           "event_days": len({r["event_date"] for r in rows})}
    ll = {name: [_binary_logloss(r[f"p_{name}"], r["win"]) for r in rows] for name in P0_LAYERS}
    ll["market"] = [_binary_logloss(r["q"], r["win"]) for r in rows]
    for name, values in ll.items():
        out[f"logloss_{name}"] = _traded_mean(values)
    for name in P0_LAYERS:
        out[f"logloss_{name}_vs_market"], out[f"logloss_{name}_vs_market_ci"] = _paired_mean(rows, ll[name], ll["market"])
    steps = (("bridge_radiation", "input", "pool"), ("distribution_layer", "served", "input"))
    for step, after, before in steps:
        out[f"logloss_step_{step}"], out[f"logloss_step_{step}_ci"] = _paired_mean(rows, ll[after], ll[before])
    for score in ("logscore", "crps"):
        suffix = "_c" if score == "crps" else ""
        values = {name: [s.get(f"{score}_{name}{suffix}") for s in samples] for name in (*P0_LAYERS, "market")}
        for name, column in values.items():
            out[f"{score}_{name}"] = _traded_mean([v for v in column if v is not None])
        for name in P0_LAYERS:
            out[f"{score}_{name}_vs_market"], out[f"{score}_{name}_vs_market_ci"] = _paired_mean(
                samples, values[name], values["market"])
        for step, after, before in steps:
            out[f"{score}_step_{step}"], out[f"{score}_step_{step}_ci"] = _paired_mean(samples, values[after], values[before])
    return out


def bucket_calibration_group(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Binary LogLoss and Brier of ours and the market's on the same buckets,
    said vs landed, and the paired difference with an event-day interval."""

    if not rows:
        return None
    ll_p = [_binary_logloss(r["p"], r["win"]) for r in rows]
    ll_q = [_binary_logloss(r["q"], r["win"]) for r in rows]
    diff = [a - b for a, b in zip(ll_p, ll_q)]
    return {
        "buckets": len(rows), "samples": len({(r["station_id"], r["event_date"], r["at"]) for r in rows}),
        "event_days": len({r["event_date"] for r in rows}),
        "logloss_ours": _traded_mean(ll_p), "logloss_market": _traded_mean(ll_q),
        "logloss_diff": _traded_mean(diff), "logloss_diff_ci": _traded_day_bootstrap(rows, diff),
        "brier_ours": _traded_mean([(r["p"] - r["win"]) ** 2 for r in rows]),
        "brier_market": _traded_mean([(r["q"] - r["win"]) ** 2 for r in rows]),
        "said_ours": _traded_mean([r["p"] for r in rows]), "said_market": _traded_mean([r["q"] for r in rows]),
        "landed": _traded_mean([r["win"] for r in rows]),
    }


def bucket_calibration_reliability(rows: Sequence[Mapping[str, Any]], key: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for lo, hi in zip(BUCKET_CALIBRATION_BINS[:-1], BUCKET_CALIBRATION_BINS[1:]):
        inside = [r for r in rows if lo <= float(r[key]) < hi]
        if inside:
            hits = sum(float(r["win"]) for r in inside)
            out.append({"bin": [lo, min(hi, 1.0)], "buckets": len(inside),
                        "said_ours": _traded_mean([r["p"] for r in inside]),
                        "said_market": _traded_mean([r["q"] for r in inside]),
                        "landed": hits / len(inside), "landed_ci": _wilson(hits, len(inside))})
    return out


def bucket_reliability_set(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Our and the market's reliability diagrams on every bucket, each with
    its REL, ECE and binary LogLoss."""

    ours = bucket_calibration_reliability(rows, "p")
    market = bucket_calibration_reliability(rows, "q")
    return {"reliability_ours": ours, "reliability_market": market, "reliability_summary": {
        "ours": {**reliability_summary(ours, "said_ours", "buckets"),
                 "logloss": _traded_mean([_binary_logloss(r["p"], r["win"]) for r in rows])},
        "market": {**reliability_summary(market, "said_market", "buckets"),
                   "logloss": _traded_mean([_binary_logloss(r["q"], r["win"]) for r in rows])}}}


def bucket_calibration(conn: sqlite3.Connection) -> dict[str, Any]:
    """Every bucket's occurrence probability on the ladder, ours beside the
    market's, binary LogLoss: the whole, by stage, lead, unit, the bucket's
    distance from the market's mode, the direction and size of our-minus-market,
    the market's price, the halves and the weeks; the whole distribution's
    LogScore/CRPS on the same instants; and the P0 standing at each instant
    layer by layer (user 2026-10-09 「下一步继续切」)."""

    built = bucket_calibration_rows(conn)
    rows, samples, counts = built["rows"], built["samples"], built["counts"]
    days = sorted({r["event_date"] for r in rows})
    split = days[len(days) // 2] if days else None
    specs: list[tuple[str, str, Any]] = []
    for stage in ("P0", "P0-M", "P1"):
        specs.append(("阶段", stage, lambda r, v=stage: r["stage"] == v))
    for name, _lo, _hi in BUCKET_CALIBRATION_BANDS:
        specs.append(("提前量", name, lambda r, v=name: r["band"] == v))
    for unit in ("C", "F"):
        specs.append(("结算单位", unit, lambda r, v=unit: r["unit"] == v))
    for name, lo, hi in _BUCKET_POSITIONS:
        specs.append(("桶位置", name, lambda r, a=lo, b=hi: a <= r["from_mode"] <= b))
    specs.append(("分歧方向", "p > q", lambda r: r["p"] > r["q"]))
    specs.append(("分歧方向", "p ≤ q", lambda r: r["p"] <= r["q"]))
    for name, lo, hi in _BUCKET_SIDE_BANDS:
        specs.append(("我方对市场", name, lambda r, a=lo, b=hi: a <= r["p"] - r["q"] < b))
    for name, lo, hi in _BUCKET_PRICE_BANDS:
        specs.append(("市场价", name, lambda r, a=lo, b=hi: a <= r["q"] < b))
    if split:
        specs.append(("时段", f"前半（< {split}）", lambda r, v=split: r["event_date"] < v))
        specs.append(("时段", f"后半（≥ {split}）", lambda r, v=split: r["event_date"] >= v))
    weeks = sorted({_event_week(d) for d in days})
    for week in weeks:
        specs.append(("事件周", f"{week} 起一周", lambda r, v=week: _event_week(r["event_date"]) == v))
    slices = []
    for group, name, predicate in specs:
        row = bucket_calibration_group([r for r in rows if predicate(r)])
        if row is not None:
            slices.append({"group": group, "slice": name, **row})
    # The whole distribution on the same instants, and the P0 layers.
    dist_specs: list[tuple[str, str, Any]] = [("阶段", st, lambda x, v=st: x["stage"] == v) for st in ("P0", "P0-M", "P1")]
    dist_specs += [("提前量", name, lambda x, v=name: x["band"] == v) for name, _lo, _hi in BUCKET_CALIBRATION_BANDS]
    dist_specs += [("结算单位", u, lambda x, v=u: x["unit"] == v) for u in ("C", "F")]
    dist_specs += [("事件周", f"{w} 起一周", lambda x, v=w: _event_week(x["event_date"]) == v) for w in weeks]
    dist_slices = []
    for group, name, predicate in dist_specs:
        got = distribution_group([x for x in samples if predicate(x)])
        if got is not None:
            dist_slices.append({"group": group, "slice": name, **got})
    distribution = {"all": distribution_group(samples), "slices": dist_slices}
    layer_rows, layer_samples = built["layer_rows"], built["layer_samples"]
    layer_slices = []
    for name, _lo, _hi in BUCKET_CALIBRATION_BANDS:
        got = p0_layer_group([r for r in layer_rows if r["band"] == name], [x for x in layer_samples if x["band"] == name])
        if got is not None:
            layer_slices.append({"group": "提前量", "slice": name, **got})
    for name, lo, hi in _BUCKET_POSITIONS:
        got = p0_layer_group([r for r in layer_rows if lo <= r["from_mode"] <= hi], [])
        if got is not None:
            layer_slices.append({"group": "桶位置", "slice": name, **got})
    p0_layers = {"all": p0_layer_group(layer_rows, layer_samples), "slices": layer_slices}
    return {
        "rule": "every_bucket_binary_logloss_v1", "since": TRADED_CALIBRATION_SINCE, "counts": counts,
        "book_window_sec": TRADED_CALIBRATION_BOOK_WINDOW_SEC, "tries_per_band": BUCKET_CALIBRATION_TRIES,
        "all": bucket_calibration_group(rows), "slices": slices,
        "distribution": distribution, "p0_layers": p0_layers,
        **bucket_reliability_set(rows),
    }

# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# skew: which side the truth lands, and the mode / centre bucket hit rates
# ---------------------------------------------------------------------------

#: Settled event days the skew reading looks back over.
SKEW_TRUTH_DAYS = 45
#: The lean's hit-rate trend runs forward from here and keeps every settled
#: day after it (user 2026-10-16 「向前统计真值“判对率”，并用图表看趋势」); the
#: summary tables above keep the last ``SKEW_TRUTH_DAYS``.
SKEW_TREND_SINCE = "2026-09-01"
#: The market book paired with a model decision: the newest committed
#: snapshot at or before it, no older than this.
SKEW_TRUTH_MARKET_AGE_SEC = 3600.0
_SKEW_NEXT_STAGE = {"P0": "P0-M", "P0-M": "P1"}
_SKEW_WORDS = {0: "symmetric", -1: "left", 1: "right", "floor": "floor"}


def _float_or_none(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _skew_point(vector: Mapping[str, float], axis: Sequence[Mapping[str, Any]], truth_bucket: str,
                running_high_c: float | None = None) -> dict[str, Any] | None:
    """One served vector read by production's own rules: the skew sign
    (``mean_mode_skew_reading_v1``), the mode, the centre bucket
    (``mean_center_bucket_v1``), where the truth landed against the mode and
    how much mass the vector put on each side of it."""

    from .basket_kernel.support import mean_center_bucket_v1, mean_mode_skew_reading_v1  # noqa: PLC0415
    from .station_day_board import skew_floor  # noqa: PLC0415

    ordered = [(str(a["bucket"]), int(a["ordinal"])) for a in axis]
    probs = {b: float(vector.get(b, 0.0) or 0.0) for b, _o in ordered}
    ords = dict(ordered)
    total = sum(probs.values())
    if total <= 0.0 or truth_bucket not in ords:
        return None
    reading = mean_mode_skew_reading_v1(probs, ords)
    if not reading["readable"]:
        return None
    mode_ord = int(reading["mode_ordinal"])
    mode = next(b for b, o in ordered if o == mode_ord)
    truth_ord = ords[truth_bucket]
    return {
        "sign": int(reading["sign"]), "gap": reading["gap_buckets"],
        # A 下界日 (station_day_board.skew_floor): the mode holds the running
        # observed high, so the lean is right by construction; counted apart.
        "floor": bool(axis and "lo_c" in axis[0] and skew_floor(mode, axis, running_high_c)),
        "mode": mode, "mean_bucket": mean_center_bucket_v1(probs, ords),
        "truth_side": (truth_ord > mode_ord) - (truth_ord < mode_ord),
        "claim": [sum(p for b, p in probs.items() if ords[b] < mode_ord) / total,
                  probs[mode] / total,
                  sum(p for b, p in probs.items() if ords[b] > mode_ord) / total],
    }


def skew_truth_rows(conn: sqlite3.Connection, since: str | None = None) -> list[dict[str, Any]]:
    """Every settled station-day of the window, once per stage: P0 and P0-M
    at their last decision before the next stage's first (charter 260: after
    that they are no longer the day's belief), P1 at its first; each with
    the market's committed book of that instant."""

    from .basket_kernel.valuation_v1 import market_reference_v1  # noqa: PLC0415
    from .canonical_contract import FORMAL_TRUTH_LANES_SQL  # noqa: PLC0415
    from .station_day_board import bucket_of, ladder_axis  # noqa: PLC0415

    latest = conn.execute(
        "SELECT MAX(local_date) FROM calibration_station_daily_high_truth"
        f" WHERE source_lane IN {FORMAL_TRUTH_LANES_SQL} AND qc_status='ACCEPTED'"
    ).fetchone()[0]
    if not latest:
        return []
    if since is None:
        since = (date.fromisoformat(str(latest)) - timedelta(days=SKEW_TRUTH_DAYS - 1)).isoformat()
    truth: dict[tuple[str, str], float] = {}
    for station, day, value in conn.execute(
        "SELECT station_id, local_date, official_tmax_c FROM calibration_station_daily_high_truth"
        f" WHERE source_lane IN {FORMAL_TRUTH_LANES_SQL} AND qc_status='ACCEPTED' AND local_date BETWEEN ? AND ?"
        " ORDER BY revision_no", (since, str(latest)),
    ):
        truth[(str(station), str(day))] = float(value)
    spans = {
        (str(s), str(d), str(stage)): (str(first), str(last))
        for s, d, stage, first, last in conn.execute(
            "SELECT station_id, event_date, stage, MIN(decision_at_utc), MAX(decision_at_utc)"
            " FROM basket_probability_envelope INDEXED BY basket_probability_envelope_publisher_scope_idx"
            " WHERE state='COMMITTED' AND event_date BETWEEN ? AND ? GROUP BY station_id, event_date, stage",
            (since, str(latest)),
        )
    }
    rows: list[dict[str, Any]] = []
    for (station, day, stage), (first, last) in sorted(spans.items()):
        if (station, day) not in truth:
            continue
        if stage == "P1":
            at = first
        else:
            handoff = spans.get((station, day, _SKEW_NEXT_STAGE[stage]))
            at = last
            if handoff:
                at = conn.execute(
                    "SELECT MAX(decision_at_utc) FROM basket_probability_envelope WHERE station_id=? AND event_date=?"
                    " AND state='COMMITTED' AND decision_at_utc < ? AND stage=?",
                    (station, day, handoff[0], stage),
                ).fetchone()[0]
        if not at:
            continue
        env = conn.execute(
            "SELECT probabilities_json, bucket_ordinals_json, json_extract(lineage_json,'$.bucket_intervals_c_json'),"
            " json_extract(lineage_json,'$.observed_running_high_c'), json_extract(lineage_json,'$.reported_high_c')"
            " FROM basket_probability_envelope WHERE station_id=? AND event_date=? AND state='COMMITTED'"
            " AND decision_at_utc=? AND stage=? LIMIT 1", (station, day, at, stage),
        ).fetchone()
        if env is None or not env[2]:
            continue
        axis = ladder_axis(json.loads(env[1]), json.loads(env[2]))
        truth_bucket = bucket_of(axis, truth[(station, day)])
        if truth_bucket is None:
            continue
        high = _float_or_none(env[3] if stage == "P0-M" else env[4] if stage == "P1" else None)
        model = _skew_point(json.loads(env[0]), axis, truth_bucket, high)
        if model is None:
            continue
        market = None
        book = conn.execute(
            "SELECT observed_at, buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=?"
            " AND state='COMMITTED' AND observed_at<=? ORDER BY observed_at DESC LIMIT 1", (station, day, at),
        ).fetchone()
        if book is not None:
            try:
                age = (datetime.fromisoformat(str(at).replace("Z", "+00:00"))
                       - datetime.fromisoformat(str(book[0]).replace("Z", "+00:00"))).total_seconds()
            except ValueError:
                age = None
            ref = market_reference_v1(json.loads(book[1])) if age is not None and age <= SKEW_TRUTH_MARKET_AGE_SEC else {}
            if ref.get("state") == "complete":
                market = _skew_point({str(k): float(v) for k, v in ref["vector"].items()}, axis, truth_bucket, high)
        rows.append({"station_id": station, "event_date": day, "stage": stage, "decided_at_utc": at,
                     "truth_bucket": truth_bucket, "model": model, "market": market})
    return rows


def _skew_side_group(points: Sequence[Mapping[str, Any]], truth_buckets: Sequence[str]) -> dict[str, Any]:
    n = len(points)
    return {
        "n": n,
        "truth": [sum(p["truth_side"] < 0 for p in points) / n, sum(p["truth_side"] == 0 for p in points) / n,
                  sum(p["truth_side"] > 0 for p in points) / n],
        "claim": [sum(p["claim"][i] for p in points) / n for i in range(3)],
        "mode_hit": sum(p["mode"] == t for p, t in zip(points, truth_buckets)) / n,
        "mean_hit": sum(p["mean_bucket"] == t for p, t in zip(points, truth_buckets)) / n,
    }


def _skew_call_stats(rows: Sequence[Mapping[str, Any]], source: str) -> dict[str, Any] | None:
    """User 2026-10-16 「前端统计显式判对率、判出偏斜的天数、「判对」带来的信息量」.

    Over the station-days whose truth left the mode (the only ones a lean
    can be right or wrong on): how many the reading called left or right,
    how often the call named the side the truth landed on (with an
    event-day interval), and the mutual information in bits between the
    three-state reading and that side -- the score the threshold was chosen
    on (docs/research/p0_skew_threshold_20261016)."""

    every = [r for r in rows if r[source] and r[source]["truth_side"] != 0]
    floor = [r for r in every if r[source].get("floor")]
    off = [r for r in every if not r[source].get("floor")]
    if not off:
        return None
    called = [r for r in off if r[source]["sign"] != 0]
    right = [float(r[source]["sign"] == r[source]["truth_side"]) for r in called]
    n = len(off)
    mi = 0.0
    for sign in (-1, 0, 1):
        p_sign = sum(r[source]["sign"] == sign for r in off) / n
        for side in (-1, 1):
            p_side = sum(r[source]["truth_side"] == side for r in off) / n
            joint = sum(r[source]["sign"] == sign and r[source]["truth_side"] == side for r in off) / n
            if joint > 0:
                mi += joint * math.log2(joint / (p_sign * p_side))
    return {
        "source": source, "off_mode": n, "called": len(called), "called_share": len(called) / n,
        "hit_rate": (sum(right) / len(right)) if right else None,
        "hit_ci": _traded_day_bootstrap(called, right) if called else None,
        "mi_bits": mi, "event_days": len({r["event_date"] for r in off}),
        # 下界日 are left out of everything above and counted here.
        "floor_days": len(floor), "floor_share": len(floor) / len(every),
    }


def _skew_call_trend(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per event day, per source: the off-mode station-days, how many the
    reading called and how many of those it called right -- counts, so the
    board can pool any window it draws."""

    out = []
    for day in sorted({r["event_date"] for r in rows}):
        item: dict[str, Any] = {"event_date": day}
        for source in ("model", "market"):
            every = [r[source] for r in rows if r["event_date"] == day and r[source] and r[source]["truth_side"] != 0]
            off = [p for p in every if not p.get("floor")]
            called = [p for p in off if p["sign"] != 0]
            item[source] = {"off": len(off), "called": len(called),
                            "right": sum(p["sign"] == p["truth_side"] for p in called),
                            "floor": len(every) - len(off)}
        out.append(item)
    return out


def skew_truth_side(conn: sqlite3.Connection) -> dict[str, Any]:
    """User 2026-10-11 「前端显示增加众数和均值桶的命中率：模型对市场统计显示和偏斜
    站日，真值落在哪一侧显示（按照生产现有口径）」.

    ``sides``: per stage, for the model and the market each read on its own
    skew, how often the truth lands left of / on / right of the mode against
    what the vector put there, with each one's mode and centre-bucket hit
    rate.  ``hits``: the same station-day instants paired, grouped by the
    model's skew -- mode and centre-bucket hit rates, model minus market,
    with an event-day interval.
    """

    from .basket_kernel.support import MEAN_MODE_SKEW_THRESHOLD_SIGMA  # noqa: PLC0415

    trend_rows = skew_truth_rows(conn, since=SKEW_TREND_SINCE)
    window_from = None
    if trend_rows:
        latest_day = max(r["event_date"] for r in trend_rows)
        window_from = (date.fromisoformat(latest_day) - timedelta(days=SKEW_TRUTH_DAYS - 1)).isoformat()
    rows = [r for r in trend_rows if window_from is None or r["event_date"] >= window_from]
    stages = []
    for stage in ("P0", "P0-M", "P1"):
        mine = [r for r in rows if r["stage"] == stage]
        if not mine:
            continue
        sides = []
        for source in ("model", "market"):
            have = [r for r in mine if r[source]]
            for sign in (-1, 0, 1, "floor"):
                kept = [r for r in have if (r[source].get("floor") if sign == "floor"
                                            else not r[source].get("floor") and r[source]["sign"] == sign)]
                if kept:
                    sides.append({"source": source, "skew": _SKEW_WORDS[sign], "share": len(kept) / len(have),
                                  **_skew_side_group([r[source] for r in kept], [r["truth_bucket"] for r in kept])})
        hits = []
        paired = [r for r in mine if r["market"]]
        for label, sign in (("all", None), ("symmetric", 0), ("left", -1), ("right", 1), ("floor", "floor")):
            kept = [r for r in paired if sign is None or (r["model"].get("floor") if sign == "floor"
                                                         else not r["model"].get("floor") and r["model"]["sign"] == sign)]
            if not kept:
                continue
            item = {"group": label, "n": len(kept), "event_days": len({r["event_date"] for r in kept})}
            for key, field in (("mode", "mode"), ("mean", "mean_bucket")):
                a = [float(r["model"][field] == r["truth_bucket"]) for r in kept]
                b = [float(r["market"][field] == r["truth_bucket"]) for r in kept]
                diff, ci = _paired_mean(kept, a, b)
                item[key] = {"model": sum(a) / len(a), "market": sum(b) / len(b), "diff": diff, "ci": ci}
            hits.append(item)
        calls = [c for c in (_skew_call_stats(mine, "model"), _skew_call_stats(paired, "market")) if c]
        trend = _skew_call_trend([r for r in trend_rows if r["stage"] == stage])
        stages.append({"stage": stage, "station_days": len(mine), "paired": len(paired),
                       "pick": "first" if stage == "P1" else "last_before_next_stage",
                       "sides": sides, "hits": hits, "calls": calls, "trend": trend})
    days = sorted({r["event_date"] for r in rows})
    return {"schema": "skew_truth_side_v1", "since": days[0] if days else None, "until": days[-1] if days else None,
            "event_days": len(days), "market_age_sec": SKEW_TRUTH_MARKET_AGE_SEC,
            "rule": f"mean_mode_skew_reading_v1 ({MEAN_MODE_SKEW_THRESHOLD_SIGMA:g} sigma) / mean_center_bucket_v1",
            "threshold_sigma": MEAN_MODE_SKEW_THRESHOLD_SIGMA, "trend_since": SKEW_TREND_SINCE, "stages": stages}


# ---------------------------------------------------------------------------
# mode_agreement -- when our mode and the market's fall on the same bucket
# (user 2026-10-15 「首次：模型的众数桶和市场的众数桶为同一桶的时候的真值的命中率
# 有多少」「还有时间轴维度、首次、第二次、最后一次」)
# ---------------------------------------------------------------------------

#: The agreement episodes the board reads: the first, the second, the last.
# 「最后一次」 left on 2026-10-21 (user 「全部改：众数同桶去掉「最后一次」改为按当时可知的
# 全部同桶段」): which episode is the last is known only after settlement, and an
# agreement on a wrong bucket is usually followed by another one, so the pick
# selected the right buckets by hindsight -- 98.5% landed against an 80% ask,
# while every episode taken as it began landed below its ask in every stage
# and hours band.  ``every`` (all episodes, as they started) replaces it.
MODE_AGREEMENT_PICKS = (("first", 0), ("second", 1))
#: Station-days this recent are walked again every run (a truth revision or
#: a late book still moves them); older ones reuse the stored record.
MODE_AGREEMENT_REWALK_DAYS = 4
#: Entry-price bands of the per-trade ledger (one share bought at each
#: episode's own price, user 2026-10-15 「你不要用平均数」「加」).
MODE_AGREEMENT_PRICE_BANDS = ((0.0, 0.3), (0.3, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.8), (0.8, 1.01))
#: Who moved onto the common bucket (see mode_agreement_days).
MODE_AGREEMENT_MOVERS = ("model", "market", "both", "start")


def _ladder_mode(vector: Mapping[str, float], order: Mapping[str, int]) -> str:
    # A tie goes to the lower rung, as _ladder_rows (charter 249) picks it.
    return max(sorted(vector, key=lambda b: order[b]), key=lambda b: vector[b])


def _ladder_quotes(
    conn: sqlite3.Connection, station: str, day: str, at_utc: str,
) -> tuple[dict[str, float], dict[str, float]]:
    """Each bucket's best YES ask -- what a buy would have paid -- and its raw
    two-sided mid (not rescaled to the ladder, as the market reference is), in
    the same book ``_ladder_market_reference`` reads at ``at_utc``; a bucket
    without the side(s) is absent."""

    floor = (_utc_instant(at_utc) - timedelta(seconds=TRADED_CALIBRATION_BOOK_WINDOW_SEC))
    snap = conn.execute(
        "SELECT buckets_json FROM basket_official_ws_snapshot"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED' AND observed_at<=? AND observed_at>=?"
        " ORDER BY observed_at DESC, sequence DESC LIMIT 1",
        (station, day, at_utc, floor.isoformat().replace("+00:00", "Z")),
    ).fetchone()
    try:
        buckets = json.loads(snap[0] or "[]") if snap is not None else []
    except (TypeError, ValueError):
        buckets = []
    asks: dict[str, float] = {}
    mids: dict[str, float] = {}
    for b in buckets if isinstance(buckets, list) else []:
        if not isinstance(b, dict):
            continue
        ask = b.get("best_ask") if b.get("ask_available") else None
        if not isinstance(ask, (int, float)) or not 0.0 < ask < 1.0:
            continue
        asks[str(b.get("bucket"))] = float(ask)
        bids = [float(x["price"]) for x in b.get("bid_levels") or []
                if isinstance(x, dict) and isinstance(x.get("price"), (int, float)) and float(x.get("shares") or 0) > 0]
        bid = max(bids) if bids else (float(b["bid_price"]) if b.get("bid_available")
                                       and isinstance(b.get("bid_price"), (int, float)) and b["bid_price"] > 0 else None)
        if bid is not None and 0.0 < bid <= ask:
            mids[str(b.get("bucket"))] = 0.5 * (bid + float(ask))
    return asks, mids


def mode_agreement_days(conn: sqlite3.Connection) -> dict[str, Any]:
    """Each settled station-day since ``TRADED_CALIBRATION_SINCE`` walked along
    its committed decision instants.  At each instant our belief standing then
    (charter 260, floored as served) is paired with the market's whole-ladder
    reference at most ``TRADED_CALIBRATION_BOOK_WINDOW_SEC`` old; an instant
    without one is skipped and breaks nothing.  An episode is a run of paired
    instants on which both modes are the same bucket; it ends at the first
    paired instant where they part or move to another bucket."""

    from .label_knowability import knowable_labels  # noqa: PLC0415
    from .settlement_time import hours_to_settlement_if_known  # noqa: PLC0415

    counts = {"station_days": 0, "paired_days": 0, "reused_days": 0, "instants": 0, "paired_instants": 0,
              "agree_instants": 0}
    served_cache: dict[Any, Any] = {}
    market_cache: dict[tuple[str, str, str], Any] = {}
    days: list[dict[str, Any]] = []
    # A settled station-day's walk does not change once its books and beliefs
    # are in; the previous run's record is reused while the code and the
    # settled bucket are the same (a full walk is ~0.27 s a station-day).
    code = _code_sha(*_READINGS["mode_agreement"][1])
    stored = load_reading("mode_agreement") or {}
    reuse: dict[str, dict[str, Any]] = {}
    if stored.get("code_sha256") == code:
        reuse = {f"{d['station_id']}|{d['event_date']}": d for d in (stored.get("value") or {}).get("days") or []}
    reuse_before = (_now() - timedelta(days=MODE_AGREEMENT_REWALK_DAYS)).date().isoformat()
    labels = {k: v for k, v in knowable_labels(conn).items() if str(k[1]) >= TRADED_CALIBRATION_SINCE}
    for (station, day), label in sorted(labels.items()):
        old = reuse.get(f"{station}|{day}")
        if old is not None and old["settled"] == str(label["bucket"]) and str(day) < reuse_before:
            counts["station_days"] += 1
            counts["paired_days"] += 1
            counts["reused_days"] += 1
            days.append(old)
            continue
        instants = [str(r[0]) for r in conn.execute(
            "SELECT DISTINCT decision_at_utc FROM basket_probability_bridge_history"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED' ORDER BY decision_at_utc",
            (str(station), str(day)),
        )]
        if not instants:
            continue
        tz = conn.execute(
            "SELECT settlement_timezone FROM basket_probability_bridge_history"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED' LIMIT 1",
            (str(station), str(day)),
        ).fetchone()[0]
        counts["station_days"] += 1
        settled = str(label["bucket"])
        first_paired: dict[str, Any] | None = None
        episodes: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        previous: tuple[str, str] | None = None
        paired = 0
        for at in instants:
            counts["instants"] += 1
            q = _ladder_market_reference(conn, str(station), str(day), at, market_cache)
            if q is None:
                continue
            stage, p, order, _rung = _served_belief_vector(conn, str(station), str(day), at, served_cache)
            if p is None or order is None or set(p) != set(q) or set(p) != set(order) or settled not in p:
                continue
            paired += 1
            ours, theirs = _ladder_mode(p, order), _ladder_mode(q, order)
            hours = hours_to_settlement_if_known(str(day), as_of_utc=at, settlement_timezone=tz)
            if first_paired is None:
                first_paired = {"at": at, "stage": stage, "hours": hours,
                                "ours_hit": ours == settled, "market_hit": theirs == settled}
            before, previous = previous, (ours, theirs)
            if ours != theirs:
                current = None
                continue
            counts["agree_instants"] += 1
            if current is not None and current["bucket"] == ours:
                current["end"], current["instants"] = at, current["instants"] + 1
                continue
            # Who moved onto the common bucket, against the previous paired
            # instant: the model onto the market's mode, the market onto
            # ours, both at once, or agreed from the first paired instant.
            mover = ("start" if before is None else "model" if before[1] == ours != before[0]
                     else "market" if before[0] == ours != before[1] else "both")
            # The mode and its neighbours, three buckets (two at a ladder edge).
            window = [b for b in p if abs(order[b] - order[ours]) <= 1]
            asks, mids = _ladder_quotes(conn, str(station), str(day), at)
            current = {"bucket": ours, "start": at, "end": at, "instants": 1, "stage": stage, "hours": hours,
                       "p": p[ours], "q": q[ours], "hit": ours == settled, "mover": mover,
                       "p3": sum(p[b] for b in window), "q3": sum(q[b] for b in window), "hit3": settled in window,
                       "ask": asks.get(ours),
                       "ask3": sum(asks[b] for b in window) if all(b in asks for b in window) else None,
                       "mid": mids.get(ours),
                       "mid3": sum(mids[b] for b in window) if all(b in mids for b in window) else None}
            episodes.append(current)
        counts["paired_instants"] += paired
        if not paired:
            continue
        counts["paired_days"] += 1
        # Only what the reading reads is kept: the picked episodes, and every
        # episode's start as [mover, hours, stage, hit, p, q, hit3, p3, q3, ask, ask3, mid, mid3].
        picked = {name: episodes[index] for name, index in MODE_AGREEMENT_PICKS
                  if len(episodes) > (index if index >= 0 else -index - 1)}
        starts = [[e["mover"], None if e["hours"] is None else round(e["hours"], 2), e["stage"], int(e["hit"]),
                   round(e["p"], 4), round(e["q"], 4), int(e["hit3"]), round(e["p3"], 4), round(e["q3"], 4),
                   e["ask"], None if e["ask3"] is None else round(e["ask3"], 4),
                   e["mid"], None if e["mid3"] is None else round(e["mid3"], 4)]
                  for e in episodes]
        days.append({"station_id": str(station), "event_date": str(day), "settled": settled,
                     "unit": "F" if "°F" in settled else "C", "paired": paired,
                     "first_paired": first_paired, "episodes": len(episodes), "picked": picked, "starts": starts})
    return {"days": days, "counts": counts}


def _mode_agreement_group(picked: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    n = len(picked)
    if not n:
        return None
    hits = sum(1 for e in picked if e["hit"])
    hours = [e["hours"] for e in picked if e["hours"] is not None]
    return {"n": n, "event_days": len({e["event_date"] for e in picked}),
            "station_days": len({(e.get("station_id"), e["event_date"]) for e in picked}), "hits": hits, "hit": hits / n,
            "hit_ci": _wilson(hits, n), "said_ours": sum(e["p"] for e in picked) / n,
            "said_market": sum(e["q"] for e in picked) / n,
            # Landed minus what the market said on the bucket, event days resampled.
            "over_market_ci": _traded_day_bootstrap(picked, [float(e["hit"]) - e["q"] for e in picked]),
            # The same on the mode and its two neighbours.
            "hit3": sum(1 for e in picked if e["hit3"]) / n, "said_ours3": sum(e["p3"] for e in picked) / n,
            "said_market3": sum(e["q3"] for e in picked) / n,
            "over_market3_ci": _traded_day_bootstrap(picked, [float(e["hit3"]) - e["q3"] for e in picked]),
            # Against what a buy would have paid: the best ask (one bucket, and
            # the three summed), on the episodes the book quoted.
            **_mode_agreement_ask(picked, "ask", "hit", ""), **_mode_agreement_ask(picked, "ask3", "hit3", "3"),
            # And against the raw two-sided mid of the same book.
            **_mode_agreement_ask(picked, "mid", "hit", ""), **_mode_agreement_ask(picked, "mid3", "hit3", "3"),
            "median_hours": median(hours) if hours else None}


def _mode_agreement_ask(picked: Sequence[Mapping[str, Any]], price: str, hit: str, suffix: str) -> dict[str, Any]:
    """Landed against a quoted price (``ask``/``mid``, ``suffix`` "3" for the
    mode ±1) on the episodes that had it: n_<price>, hit_<price>, <price>,
    over_<price>_ci -- keys named by the price, the suffix after it."""

    quoted = [e for e in picked if e.get(price) is not None]
    base = price[:-1] if suffix else price
    if not quoted:
        return {f"n_{base}{suffix}": 0}
    return {f"n_{base}{suffix}": len(quoted), f"hit_{base}{suffix}": sum(1 for e in quoted if e[hit]) / len(quoted),
            f"{base}{suffix}": sum(e[price] for e in quoted) / len(quoted),
            f"over_{base}{suffix}_ci": _traded_day_bootstrap(quoted, [float(e[hit]) - e[price] for e in quoted])}


def _mode_agreement_ledger(episodes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """One share of the agreed bucket bought at each episode's own price --
    the mid and the best ask, each on the episodes that had it -- summed by
    entry-price band: trades, hits, paid, returned, profit.  No averages."""

    out: dict[str, Any] = {}
    for price in ("mid", "ask"):
        quoted = [e for e in episodes if e.get(price) is not None]
        rows = []
        for lo, hi in MODE_AGREEMENT_PRICE_BANDS:
            xs = [e for e in quoted if lo <= e[price] < hi]
            if xs:
                hits = sum(1 for e in xs if e["hit"])
                paid = sum(e[price] for e in xs)
                rows.append({"lo": lo, "hi": min(hi, 1.0), "trades": len(xs), "hits": hits,
                             "paid": round(paid, 4), "profit": round(hits - paid, 4)})
        hits = sum(1 for e in quoted if e["hit"])
        paid = sum(e[price] for e in quoted)
        out[price] = {"bands": rows, "trades": len(quoted), "hits": hits, "paid": round(paid, 4),
                      "profit": round(hits - paid, 4)}
    return out


def mode_agreement(conn: sqlite3.Connection) -> dict[str, Any]:
    """How often the truth lands in the bucket where our mode and the
    market's first agree, agree a second time, and agree at all (every
    episode, as it began -- knowable then):
    the whole, by stage and hours to settlement at the episode's start, by
    settlement unit, and day by day.  Beside it, the first paired instant
    whatever the two modes say, as the baseline each mode hits on its own."""

    built = mode_agreement_days(conn)
    days, counts = built["days"], built["counts"]
    picks: dict[str, list[dict[str, Any]]] = {}
    for name, _index in MODE_AGREEMENT_PICKS:
        picks[name] = [
            {**d["picked"][name], "station_id": d["station_id"], "event_date": d["event_date"], "unit": d["unit"],
             "episodes": d["episodes"]}
            for d in days if name in d["picked"]
        ]
    picks["every"] = [
        {"mover": m, "hours": h, "stage": st, "hit": bool(hit), "p": pp, "q": qq, "hit3": bool(hit3),
         "p3": p3, "q3": q3, "ask": ask, "ask3": ask3, "mid": mid, "mid3": mid3,
         "station_id": d["station_id"], "event_date": d["event_date"], "unit": d["unit"], "episodes": d["episodes"]}
        for d in days for m, h, st, hit, pp, qq, hit3, p3, q3, ask, ask3, mid, mid3 in d["starts"]
    ]
    out_picks = []
    for name in [name for name, _index in MODE_AGREEMENT_PICKS] + ["every"]:
        rows = picks[name]
        slices = []
        for stage in ("P0", "P0-M", "P1"):
            got = _mode_agreement_group([e for e in rows if e["stage"] == stage])
            if got:
                slices.append({"group": "阶段", "slice": stage, **got})
        for band, lo, hi in BUCKET_CALIBRATION_BANDS:
            got = _mode_agreement_group([e for e in rows if e["hours"] is not None and lo <= e["hours"] < hi])
            if got:
                slices.append({"group": "提前量", "slice": band, **got})
        for unit in ("C", "F"):
            got = _mode_agreement_group([e for e in rows if e["unit"] == unit])
            if got:
                slices.append({"group": "结算单位", "slice": unit, **got})
        daily = []
        for day in sorted({e["event_date"] for e in rows}):
            got = _mode_agreement_group([e for e in rows if e["event_date"] == day])
            daily.append({"event_date": day, "n": got["n"], "hit": got["hit"]})
        out_picks.append({"pick": name,
                          "station_days": len({(e.get("station_id"), e["event_date"]) for e in rows}),
                          "episodes": len(rows), "ledger": _mode_agreement_ledger(rows),
                          "share_of_days": len({(e.get("station_id"), e["event_date"]) for e in rows}) / len(days)
                          if days else None,
                          "all": _mode_agreement_group(rows), "slices": slices, "daily": daily})
    # Who moved onto the common bucket: every episode, and the first one alone.
    converge = []
    for mover in MODE_AGREEMENT_MOVERS:
        every = [{"station_id": d["station_id"], "event_date": d["event_date"], "unit": d["unit"], "mover": m, "hours": h,
                  "stage": st, "hit": bool(hit), "p": pp, "q": qq, "hit3": bool(hit3), "p3": p3, "q3": q3,
                  "ask": ask, "ask3": ask3, "mid": mid, "mid3": mid3}
                 for d in days for m, h, st, hit, pp, qq, hit3, p3, q3, ask, ask3, mid, mid3 in d["starts"] if m == mover]
        first = [{**d["picked"]["first"], "station_id": d["station_id"], "event_date": d["event_date"]}
                 for d in days if "first" in d["picked"] and d["picked"]["first"]["mover"] == mover]
        slices = []
        for band, lo, hi in BUCKET_CALIBRATION_BANDS:
            for scope, rows in (("every", every), ("first", first)):
                got = _mode_agreement_group([e for e in rows if e["hours"] is not None and lo <= e["hours"] < hi])
                if got:
                    slices.append({"group": "提前量", "slice": band, "scope": scope, **got})
        for stage in ("P0", "P0-M", "P1"):
            for scope, rows in (("every", every), ("first", first)):
                got = _mode_agreement_group([e for e in rows if e["stage"] == stage])
                if got:
                    slices.append({"group": "阶段", "slice": stage, "scope": scope, **got})
        daily = []
        for day in sorted({e["event_date"] for e in every}):
            got = _mode_agreement_group([e for e in every if e["event_date"] == day])
            daily.append({"event_date": day, "n": got["n"], "hit": got["hit"]})
        converge.append({"mover": mover, "every": _mode_agreement_group(every), "ledger_first": _mode_agreement_ledger(first),
                         "first": _mode_agreement_group(first), "slices": slices, "daily": daily})
    base = [d["first_paired"] for d in days if d["first_paired"]]
    baseline = {
        "n": len(base),
        "ours_hit": (sum(1 for b in base if b["ours_hit"]) / len(base)) if base else None,
        "market_hit": (sum(1 for b in base if b["market_hit"]) / len(base)) if base else None,
    }
    spread = Counter(min(d["episodes"], 5) for d in days)
    event_days = sorted({d["event_date"] for d in days})
    return {
        "rule": "mode_agreement_episodes_v1", "since": TRADED_CALIBRATION_SINCE,
        "through": event_days[-1] if event_days else None, "event_days": len(event_days),
        "book_window_sec": TRADED_CALIBRATION_BOOK_WINDOW_SEC, "counts": counts,
        "episodes_per_day": [{"episodes": k, "station_days": spread.get(k, 0)} for k in range(6)],
        "first_paired": baseline, "picks": out_picks, "converge": converge, "days": days,
    }


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
    "traded_calibration": (
        traded_calibration,
        (traded_calibration, traded_calibration_legs, traded_calibration_group,
         traded_calibration_reliability, traded_calibration_reliability_by_lane, _traded_slices,
         _served_belief_vector, _traded_served_vector, _traded_market_mid,
         traded_calibration_ladder_by_lane, _ladder_market_reference, _ladder_rows, bucket_calibration_group,
         bucket_calibration_reliability, bucket_reliability_set,
         traded_calibration_funnel, traded_calibration_paired_legs, _event_week, _side,
         _wilson, reliability_summary, _traded_reliability_set, _ladder_leg_rows, _band_of, _fill_stratum,
         _stratified_fill_gap, _matched_fill_gap, _fill_regression, _day_draws, _fill_selection_reading,
         _order_to_fill, _forward_status, traded_calibration_fill_selection, _ladder_market_reference_near),
    ),
    "bucket_calibration": (
        bucket_calibration,
        (bucket_calibration, bucket_calibration_rows, bucket_calibration_group, bucket_calibration_reliability,
         _served_belief_vector, _ladder_market_reference, _ladder_rows, _binary_logloss, _traded_day_bootstrap,
         _ladder_scores, _p0_layers, _paired_mean, distribution_group, p0_layer_group, _event_week, _ladder_rps,
         _wilson, reliability_summary, bucket_reliability_set),
    ),
    "basket_shadow": (
        basket_shadow,
        (basket_shadow, _basket_shadow_walk, _basket_window, _basket_targets,
         _basket_shadow_labels, _basket_shadow_score, _day_bootstrap),
    ),
    "skew_truth_side": (
        skew_truth_side,
        (skew_truth_side, skew_truth_rows, _skew_point, _skew_side_group, _skew_call_stats, _skew_call_trend,
         _paired_mean,
         _traded_day_bootstrap),
    ),
    "mode_agreement": (
        mode_agreement,
        (mode_agreement, mode_agreement_days, _mode_agreement_group, _ladder_mode, _served_belief_vector,
         _ladder_market_reference, _wilson, _traded_day_bootstrap, _ladder_quotes, _mode_agreement_ask, _mode_agreement_ledger),
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
    # Charter 259: settled legs change once a day; the as-of reads behind it
    # are a few thousand point look-ups.
    "traded_calibration": 3 * 3600.0,
    "bucket_calibration": 3 * 3600.0,
    # Settled days move once a day; ~14k point look-ups a run.
    "skew_truth_side": 6 * 3600.0,
    "mode_agreement": 6 * 3600.0,
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


#: A reading yields the SSOT once its WAL passes this line (2026-10-16): the
#: job's connections had no progress handler, and l4_lane's 874 s and
#: 1,335 s readings held their snapshot while the WAL passed 512 MiB -- the
#: production storms of 10-15 20:02-20:09Z and 10-16 03:42-03:51Z, each
#: ending when l4_lane did.  See resource_bounds.install_wal_yield.  An
#: interrupted reading keeps its previous file and is asked again next run.
YIELD_WAL_BYTES = int(
    os.environ.get("PM_BOARD_READINGS_YIELD_WAL_BYTES", str(READER_YIELD_WAL_BYTES))
)


#: The WAL size a reading may start under (the resident's maintenance line),
#: and how long the run waits for it after a yield.
START_WAL_BYTES = int(os.environ.get("PM_DASHBOARD_RENDER_MAX_WAL_BYTES", str(256 * 1024 * 1024)))
WAL_WAIT_SEC = float(os.environ.get("PM_BOARD_READINGS_WAL_WAIT_SEC", "300"))
PAGE_RENDER_PATTERN = os.environ.get("PM_DASHBOARD_PAGE_RENDER_PATTERN", "calibration_dashboard.py --compute-pages")


def _research_render_waiting() -> bool:
    """The research render's sweep is up but its reader has not started:
    it is queued behind this job for the shared SSOT reader turn."""

    import subprocess  # noqa: PLC0415

    def up(pattern: str) -> bool:
        try:
            return subprocess.run(["pgrep", "-f", pattern], capture_output=True,
                                  timeout=10, check=False).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    return up("dashboard_research_sweep.sh") and not up("calibration_dashboard.py --refresh-research")


def _page_render_running() -> bool:
    import subprocess  # noqa: PLC0415

    try:
        return subprocess.run(["pgrep", "-f", PAGE_RENDER_PATTERN], capture_output=True,
                              timeout=10, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def run(
    db: Path = DEFAULT_DB,
    names: Sequence[str] = READING_NAMES,
    *,
    force: bool = False,
    background: bool = False,
) -> list[str]:
    """Compute and write each named reading; one line of receipt per reading.
    A reading that fails keeps its previous file and says why; one still
    inside its ``READING_MAX_AGE_SEC`` is kept unless ``force``.  ``background``
    (the hourly job, via ``main``) also steps aside when the page render
    starts and waits out the WAL after a yield."""

    receipts: list[str] = []
    for name in names:
        # The page render reads the SSOT too; it does not wait for this run,
        # so this run steps aside between readings when it starts.
        if background and _page_render_running():
            receipts.append("stopped: the page render started")
            break
        # The research render queued for the shared reader turn: hand it
        # over (2026-10-17: the research render waited out its whole hour
        # behind realisation refits and readings, and the pages it alone
        # refreshes went days stale).  This job comes back next hour.
        if background and _research_render_waiting():
            receipts.append("stopped: the research render is waiting for the reader turn")
            break
        compute, sources = _READINGS[name]
        if not force and _reading_is_current(name, _code_sha(*sources)):
            receipts.append(f"{name} kept")
            continue
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60.0)
        conn.execute("PRAGMA query_only=ON")
        # A reading that catches sqlite3.Error itself (l4_lane does, per
        # stage) still returns after a yield: the flag, not the exception,
        # says it must not be written.
        yield_state = install_wal_yield(conn, db, line_bytes=YIELD_WAL_BYTES)
        started = time.perf_counter()
        try:
            value = compute(conn)
        except Exception as exc:  # noqa: BLE001 -- one reading must not stop the rest
            if not yield_state["yielded"]:
                receipts.append(f"{name} failed: {type(exc).__name__}:{str(exc)[:200]}")
                continue
        finally:
            conn.close()
        if yield_state["yielded"]:
            receipts.append(f"{name} yielded to the writer (WAL past the line); previous kept")
            # Wait for the resident to truncate the WAL before the next
            # reading, and stop if it stays up (2026-10-17: every reading
            # after the first yield yielded too, the WAL never given the
            # minute it needed with no reader on it).
            if background and not wait_for_wal_under(db, START_WAL_BYTES, WAL_WAIT_SEC):
                receipts.append("stopped: the WAL stayed past the line")
                break
            continue
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
        run(args.db, args.only or READING_NAMES, force=bool(args.only), background=not args.only)
    ), flush=True)
    return 0


__all__ = [
    "READINGS_DIR", "READING_NAMES", "SCHEMA", "load_reading", "main", "run",
    "served_p0_seats", "write_reading",
]


if __name__ == "__main__":
    raise SystemExit(main())
