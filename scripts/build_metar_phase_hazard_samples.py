#!/usr/bin/env python3
"""Build the fleet-pooled sample set the four-layer temperature path is fitted on.

One sample per official report, not one per local-time bucket per day.  The
layer this replaces was fitted on a single anchor report per six-hour bucket
per station -- about twenty-five samples per scope -- and then served for the
whole bucket, so at 17:00 it still believed the noon histogram of remaining
warming.  Measured 2026-09-02 against an exact counterfactual, that cost 1 to 6
points of argmax accuracy at every fixed local hour.  Here the clock is a
feature and every report is a training row, which is what makes a fleet-pooled
fit possible at all: roughly 34 official reports per station-day across 45
stations is order 80,000 rows rather than 25.

What each row records: the state of the day at one instant, and how much
warming was still to come.  ``delta_c`` is the target -- the settled maximum
minus the running high at that instant, quantised to whole degrees -- and it
is the same quantity the ceiling artifact models as a single binary
(``delta_c >= 1``), which is why the phase layer's first hazard rung has to
reproduce the ceiling's own bins as a nested check.

Strict as-of, three ways.  The prefix carries only reports whose canonical
receipt was already visible at the sample instant; the sample instant itself is
the later of the report's valid time and its receipt (a report may be
transmitted seconds before the minute it names, and four other places in this
codebase carry the same predicate); and the root the sample sees is the newest
P0/P0-M committed at or before that instant, not the day's final root.

The physical state comes from production's own
``compile_conditional_metar_path_state`` rather than a local re-parse.  The
serving path will call the same function on the same prefix, so a feature that
disagrees between training and serving cannot arise -- and the METAR quirks
that function knows about (the ``32///`` dewpoint form that once took a whole
station's training packet down, cloud groups like ``FEW050CB``, soft-carrier
rows that hold no METAR body) stay known in one place.

Timezones come from the market registry binding, never from
``calibration_station_registry.timezone_name``: that column says
America/New_York for 33 of 45 stations, and a Helsinki "noon" read off it is
19:00 local, which is how an earlier version of this lane produced forecasts
that had already seen the day's peak and looked wonderful.

    python3 scripts/build_metar_phase_hazard_samples.py \
        --from 2026-08-10 --through 2026-09-01 \
        --out data/runtime/bayes_basket_target_nwp/metar_phase_hazard_samples.db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import (  # noqa: E402
    ContractError,
    ProbabilityEnvelope,
)
from bayes_basket_target.future_temperature_curve import (  # noqa: E402
    RootFutureTemperatureCurve,
)
from bayes_basket_target.materialization import (  # noqa: E402
    rehydrate_lineage_documents,
)
from bayes_basket_target.metar_four_layer import (  # noqa: E402
    curve_points,
    curve_shape_realized,
    interpolate_curve,
    recent_curve_bias,
)
from bayes_basket_target.metar_path_state import (  # noqa: E402
    compile_conditional_metar_path_state,
)
from bayes_basket_target.p1_compiler import _bucket_geometry_c  # noqa: E402
from bayes_basket_target.metar_trajectory import (  # noqa: E402
    MetarTrajectoryLikelihoodArtifact,
    SOFT_LIKELIHOOD_POLICY_ACTIVE,
    compile_metar_trajectory_update_proof,
)
from bayes_basket_target.probability_evidence_store import (  # noqa: E402
    local_time_bucket_for_hour,
    resolve_metar_trajectory_artifact_row,
)
from bayes_basket_target.regional_path_prior import (  # noqa: E402
    load_runs_for_station_day as load_regional_runs,
    regional_features_at,
)
from bayes_basket_target.settlement_time import (  # noqa: E402
    settlement_day_window,
)

SCHEMA = "bayes_basket_target_metar_phase_hazard_samples_v1"
# The target's support, the same twelve degrees the retired layer's baseline
# distribution carried.  It has to reach that far because the local day starts
# at midnight: a sample taken from the first report of the night sits a whole
# diurnal range below the afternoon maximum, and those rows are exactly where
# the phase layer learns that a night report says almost nothing.  Beyond
# twelve the row is a data problem rather than a hot afternoon, and it is
# recorded as excluded rather than clipped so the exclusion is countable.
MAX_DELTA_C = 12
# A report may be transmitted before the minute it names.  Measured at up to
# 15.2 seconds (SBGR 25 reports, MPMG 5); the same allowance is made in
# resident_canonical, p1_compiler, settlement_truth and event_contracts.
MAX_REPORT_TRANSMISSION_LEAD_SECONDS = 90.0

DDL = """
CREATE TABLE IF NOT EXISTS metar_phase_hazard_sample (
  sample_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  as_of_utc TEXT NOT NULL,
  prefix_report_count INTEGER NOT NULL,
  local_hour REAL NOT NULL,
  remaining_hours REAL NOT NULL,
  hours_since_high_refresh REAL NOT NULL,
  drawdown_from_high_c REAL NOT NULL,
  momentum_c_per_hour REAL NOT NULL,
  acceleration_c_per_hour2 REAL NOT NULL,
  cloud_cover_fraction REAL NOT NULL,
  precipitation_present INTEGER NOT NULL,
  dewpoint_depression_c REAL NOT NULL,
  wind_speed_mps REAL NOT NULL,
  warming_so_far_c REAL NOT NULL,
  curve_shape_realized REAL,
  curve_complete INTEGER NOT NULL,
  regional_headroom_c REAL,
  regional_live_gap_c REAL,
  regional_system_id TEXT,
  soft_lr_json TEXT,
  soft_lr_state TEXT NOT NULL DEFAULT '',
  root_bias_recent_c REAL,
  root_bias_report_count INTEGER NOT NULL,
  root_center_c REAL,
  root_checkpoint_id TEXT NOT NULL,
  running_high_c REAL NOT NULL,
  truth_tmax_c REAL NOT NULL,
  delta_c INTEGER NOT NULL,
  station_day_weight REAL NOT NULL,
  UNIQUE(station_id,event_date,as_of_utc)
);
CREATE INDEX IF NOT EXISTS metar_phase_hazard_sample_day_idx
  ON metar_phase_hazard_sample(event_date,station_id);
CREATE TABLE IF NOT EXISTS metar_phase_hazard_exclusion (
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  reason TEXT NOT NULL,
  detail TEXT NOT NULL,
  count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS metar_phase_hazard_build (
  schema TEXT NOT NULL,
  built_at_utc TEXT NOT NULL,
  window_from TEXT NOT NULL,
  window_through TEXT NOT NULL,
  station_day_count INTEGER NOT NULL,
  sample_count INTEGER NOT NULL,
  source_lane TEXT NOT NULL
);
"""


def _parse_utc(stamp: str) -> datetime:
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def load_timezones(target: sqlite3.Connection) -> dict[str, str]:
    """Settlement timezone per station, from the market registry binding.

    Not ``calibration_station_registry.timezone_name`` -- see the module
    docstring.  A station with no binding is skipped rather than defaulted:
    the wrong local day is worse than no sample.
    """

    return {
        str(row[0]): str(row[1])
        for row in target.execute(
            "SELECT station_id, settlement_timezone"
            " FROM calibration_station_market_timezone_binding"
        )
        if str(row[1] or "").strip()
    }


def load_truth(
    target: sqlite3.Connection, *, window_from: str, window_through: str
) -> dict[tuple[str, str], float]:
    """Latest accepted revision of each settled station-day in the window."""

    truth: dict[tuple[str, str], float] = {}
    for row in target.execute(
        "SELECT station_id, local_date, official_tmax_c"
        " FROM calibration_station_daily_high_truth"
        " WHERE qc_status='ACCEPTED' AND local_date>=? AND local_date<=?"
        " ORDER BY revision_no",
        (window_from, window_through),
    ):
        truth[(str(row[0]), str(row[1]))] = float(row[2])
    return truth


def load_day_observations(
    archive: sqlite3.Connection,
    *,
    station_id: str,
    window_start_utc: str,
    window_end_utc: str,
    source_lane: str,
) -> list[dict[str, Any]]:
    """Official reports of one local day, ordered, with their receipts.

    Only the AWC lane.  The soft carriers (asos_1min, madis_hfmetar) hold no
    METAR body and no hard rights over the running high; including them here
    would put rows in the sample set that the serving prefix treats
    differently.
    """

    rows = [
        {
            "observation_id": str(row[0]),
            "valid_at": str(row[1]),
            "canonical_committed_at": str(row[2]),
            "temperature_c": float(row[3]),
            "raw_text": str(row[4] or ""),
            "report_type": str(row[5] or ""),
            # For the suspended soft layer's counterfactual only.  The proof
            # compiler never reads it -- measured: raw_sha256, qc_state,
            # report_type, received_at_utc and supersedes_observation_id are
            # touched zero times in its body -- but the canonical observation
            # contract requires the field, so it is carried rather than
            # invented.
            "raw_sha256": str(row[6] or ""),
        }
        for row in archive.execute(
            "SELECT observation_id, valid_at, canonical_committed_at,"
            " reported_temperature_c, raw_text, report_type, raw_sha256"
            " FROM reported_trajectory_observations"
            " WHERE station_id=? AND valid_at>=? AND valid_at<?"
            " AND source_lane=? AND correction_suspected=0"
            " AND reported_temperature_c IS NOT NULL"
            " ORDER BY valid_at, observation_id",
            (station_id, window_start_utc, window_end_utc, source_lane),
        )
        if str(row[2] or "").strip()
    ]
    # One reading may arrive twice (a revision of the same minute).  The last
    # row of a shared minute speaks, matching the path compiler's own
    # collapse, so the prefix never carries two temperatures for one instant.
    collapsed: dict[str, dict[str, Any]] = {}
    for row in rows:
        collapsed[row["valid_at"]] = row
    return [collapsed[key] for key in sorted(collapsed)]


def sample_instant(row: Mapping[str, Any]) -> datetime:
    """When this report became usable: the later of its valid time and receipt.

    Bounded by the transmission lead so a backfilled receipt -- written days
    after the report existed -- cannot push the instant into the future and
    take the whole day's prefix with it.
    """

    valid = _parse_utc(row["valid_at"])
    committed = _parse_utc(row["canonical_committed_at"])
    return max(
        valid,
        min(
            committed,
            valid + timedelta(seconds=MAX_REPORT_TRANSMISSION_LEAD_SECONDS),
        ),
    )


def load_root_index(
    target: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
) -> list[tuple[datetime, dict[str, Any]]]:
    """Every P0/P0-M root of one station-day, oldest first.

    The sample takes the newest root at or before its own instant, which is
    the root production was serving then.  Taking the day's final root would
    let a sample from 09:00 see a rebase that happened at 15:00.
    """

    return [
        (_parse_utc(str(row[0])), dict(zip(
            ("stage", "checkpoint_id", "station_id", "event_date",
             "settlement_timezone", "decision_at_utc", "fresh_until_utc",
             "bucket_ordinals_json", "probabilities_json",
             "posterior_version", "posterior_sha256", "bridge_sha256",
             "lineage_json"),
            row[1:],
        )))
        for row in target.execute(
            "SELECT decision_at_utc, stage, checkpoint_id, station_id,"
            " event_date, settlement_timezone, decision_at_utc,"
            " fresh_until_utc, bucket_ordinals_json, probabilities_json,"
            " posterior_version, posterior_sha256, bridge_sha256, lineage_json"
            " FROM basket_probability_envelope"
            " WHERE station_id=? AND event_date=? AND stage IN ('P0','P0-M')"
            " ORDER BY decision_at_utc",
            (station_id, event_date),
        )
    ]


def root_at(
    roots: Sequence[tuple[datetime, dict[str, Any]]], instant: datetime
) -> tuple[datetime, dict[str, Any]] | None:
    chosen = None
    for row in roots:
        if row[0] <= instant:
            chosen = row
        else:
            break
    return chosen


class RootView:
    """One root's geometry and curve, parsed once and reused by every sample."""

    __slots__ = (
        "checkpoint_id", "probabilities", "centers", "curve", "curve_points",
        "center_c", "curve_root_checkpoint_id", "curve_root_probability_sha",
        # The soft-likelihood counterfactual compiles a real proof against
        # this root, so it needs the envelope, the upper bounds it quantises
        # with, and the lineage its artifact scope key is built from.
        "envelope", "uppers",
    )

    def __init__(
        self, target: sqlite3.Connection, row: Mapping[str, Any]
    ) -> None:
        # The stored envelope, rehydrated and rebuilt by its own class, not a
        # synthetic stand-in: the geometry helper below is production's, and
        # handing it an envelope this file invented would be an audit that
        # reimplements the rule it audits.
        lineage = rehydrate_lineage_documents(
            target, json.loads(row["lineage_json"] or "{}")
        )
        envelope = ProbabilityEnvelope.from_mapping({
            "stage": str(row["stage"]),
            "checkpoint_id": str(row["checkpoint_id"]),
            "station_id": str(row["station_id"]),
            "event_date": str(row["event_date"]),
            "settlement_timezone": str(row["settlement_timezone"]),
            "decision_at_utc": str(row["decision_at_utc"]),
            "fresh_until_utc": str(row["fresh_until_utc"]),
            "bucket_ordinals": json.loads(row["bucket_ordinals_json"]),
            "probabilities": json.loads(row["probabilities_json"]),
            "posterior_version": str(row["posterior_version"]),
            "posterior_sha256": str(row["posterior_sha256"]),
            "bridge_sha256": str(row["bridge_sha256"]),
            "lineage": lineage,
        })
        probabilities = dict(envelope.probabilities)
        self.checkpoint_id = envelope.checkpoint_id
        self.probabilities = probabilities
        self.envelope = envelope
        self.centers, self.uppers = _bucket_geometry_c(
            envelope, unit="C", upper_by_bucket={}
        )
        self.center_c = sum(
            float(probabilities[bucket]) * float(self.centers[bucket])
            for bucket in probabilities
        )
        raw_curve = lineage.get("conditional_future_temperature_curve_json")
        # The curve is bound to the root it was combined against, which is
        # not always the root serving now: a rebase keeps the curve and moves
        # the checkpoint.  The path compiler checks that binding, so the
        # binding it is handed has to be the curve's own, exactly as
        # p1_compiler passes it.
        self.curve_root_checkpoint_id = str(
            lineage.get("conditional_future_temperature_curve_root_checkpoint_id")
            or envelope.checkpoint_id
        )
        self.curve_root_probability_sha = str(
            lineage.get(
                "conditional_future_temperature_curve_root_probability_sha256"
            )
            or envelope.posterior_sha256
        )
        self.curve = None
        self.curve_points: list[tuple[datetime, float]] = []
        if raw_curve:
            self.curve_points = curve_points(raw_curve)
            try:
                self.curve = RootFutureTemperatureCurve.from_mapping(
                    json.loads(str(raw_curve))
                )
            except (ContractError, TypeError, ValueError, KeyError):
                self.curve = None

    def interpolate(self, instant: datetime) -> float | None:
        return interpolate_curve(self.curve_points, instant)

    def shape_realized(self, instant: datetime) -> float | None:
        return curve_shape_realized(self.curve_points, instant)


def _canonical_observation(
    row: Mapping[str, Any], *, sequence: int, station_id: str,
    event_date: str, settlement_timezone: str, source_lane: str,
) -> dict[str, Any]:
    """One archive row in the shape the trajectory contract requires.

    The contract asks for five fields the proof compiler never reads --
    measured on its body: ``qc_state``, ``supersedes_observation_id``,
    ``received_at_utc``, ``raw_sha256`` and ``report_type`` are touched zero
    times, and ``sequence`` and ``observation_id`` only reach a receipt hash
    and an id list.  What the physics reads is the temperature and two clocks:
    ``observed_at_utc`` and ``canonical_committed_at_utc``, which decide the
    evidence clock, the artifact fold and the leakage check.  All of those come
    from the archive unchanged.

    So the counterfactual reproduces the ratios exactly and does not reproduce
    the proof's identity hash, because ``sequence`` here is the row's position
    in the day rather than the canonical event's own.  That is the honest
    boundary of this reconstruction: the vectors are production's, the identity
    is not, and only the vectors are being measured.
    """

    return {
        "event": {
            "observation_id": str(row["observation_id"]),
            "station_id": station_id,
            "event_date": event_date,
            "settlement_timezone": settlement_timezone,
            "source_lane": source_lane,
            "report_type": (str(row.get("report_type") or "METAR")).upper(),
            "sequence": int(sequence),
            "observed_at_utc": str(row["valid_at"]),
            "received_at_utc": str(row["valid_at"]),
            "raw_sha256": str(row.get("raw_sha256") or ""),
            "qc_state": "ACCEPTED",
            "supersedes_observation_id": None,
        },
        "canonical_committed_at_utc": str(row["canonical_committed_at"]),
        "temperature_c": float(row["temperature_c"]),
        "raw_text": str(row.get("raw_text") or ""),
    }


def _soft_likelihood_ratio(
    *,
    target: sqlite3.Connection,
    view: "RootView",
    prefix: Sequence[Mapping[str, Any]],
    instant: datetime,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    source_lane: str,
    cache: dict[str, Any],
) -> tuple[dict[str, float] | None, str]:
    """What the suspended soft layer would have multiplied in, at this instant.

    The counterfactual is the production proof compiler with
    ``soft_likelihood_policy`` moved and nothing else: same artifact resolver
    (promoted, else the candidate the 2026-08-18 ruling admits), same prefix,
    same root.  Under suspension the compiler sets the strength to zero and
    takes the identity branch, so the ratios are never computed and a contract
    gate forbids a suspended proof from carrying them -- which is why this has
    to be a second compile rather than a field lifted out of the served proof.

    ``None`` with a state saying why whenever production could not have served
    it either.
    """

    zone = ZoneInfo(settlement_timezone)
    bucket = local_time_bucket_for_hour(instant.astimezone(zone).hour)
    if bucket not in cache:
        lineage = view.envelope.lineage
        try:
            row = resolve_metar_trajectory_artifact_row(
                target,
                station_id=station_id,
                source_lane=source_lane,
                season_bin=str(lineage.get("season_bin") or ""),
                local_time_bucket=bucket,
                maturity_bin="D0",
                settlement_definition=str(
                    lineage.get("settlement_definition") or ""
                ),
                as_of_utc=_iso(instant),
            )
        except (ContractError, sqlite3.Error):
            row = None
        cache[bucket] = (
            None if row is None
            else MetarTrajectoryLikelihoodArtifact.from_mapping(row["artifact"])
        )
    artifact = cache[bucket]
    if artifact is None:
        return None, "NO_ARTIFACT"
    observations = [
        _canonical_observation(
            row, sequence=index + 1, station_id=station_id,
            event_date=event_date, settlement_timezone=settlement_timezone,
            source_lane=source_lane,
        )
        for index, row in enumerate(prefix)
    ]
    try:
        proof = compile_metar_trajectory_update_proof(
            view.envelope,
            observations=observations,
            artifact=artifact,
            bucket_centers_c=view.centers,
            bucket_upper_bounds_c=view.uppers,
            maturity_bin="D0",
            as_of_utc=_iso(instant),
            soft_likelihood_policy=SOFT_LIKELIHOOD_POLICY_ACTIVE,
        )
    except (ContractError, KeyError, TypeError, ValueError) as exc:
        return None, f"REFUSED:{type(exc).__name__}:{exc}"[:80]
    ratios = {
        str(k): float(v) for k, v in proof.likelihood_ratio_by_bucket.items()
    }
    if not ratios or float(proof.applied_strength) <= 0.0:
        return None, "IDENTITY"
    return ratios, f"ACTIVE:{float(proof.applied_strength):.4f}"


def build_station_day(
    *,
    target: sqlite3.Connection,
    archive: sqlite3.Connection,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    truth_tmax_c: float,
    source_lane: str,
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """Every sample of one settled station-day, plus its exclusions."""

    window = settlement_day_window(event_date, settlement_timezone)
    # Charter 95 step two.  Read once per station-day and resolved per
    # instant, the way the serving side does it: one query, and every sample
    # still picks the freshest run its own clock could have seen.  Empty for
    # the fleet's majority, which has no regional lane at all.
    soft_artifacts: dict[str, Any] = {}
    regional_runs = load_regional_runs(
        target, station_id=station_id, event_date=event_date,
        as_of_utc="2100-01-01T00:00:00Z",
    )
    observations = load_day_observations(
        archive,
        station_id=station_id,
        window_start_utc=window["target_day_start_utc"],
        window_end_utc=window["target_day_end_utc"],
        source_lane=source_lane,
    )
    exclusions: list[tuple[str, str]] = []
    if len(observations) < 2:
        return [], [("prefix_too_short", f"{len(observations)} reports")]
    roots = load_root_index(
        target, station_id=station_id, event_date=event_date
    )
    if not roots:
        return [], [("no_root", "no P0/P0-M envelope for the day")]
    day_end = _parse_utc(window["target_day_end_utc"])
    root_views: dict[str, RootView] = {}
    samples: list[dict[str, Any]] = []

    for index in range(len(observations)):
        instant = sample_instant(observations[index])
        prefix = [
            row
            for row in observations[: index + 1]
            if sample_instant(row) <= instant
        ]
        if not prefix:
            continue
        running_high = max(row["temperature_c"] for row in prefix)
        delta_raw = truth_tmax_c - running_high
        # A running high above the settled maximum is a truth/observation
        # disagreement, not a cool day; it is the correction lane's business,
        # and a clipped negative target would teach this model that the day
        # was over when the ledger says it was not.
        if delta_raw < -0.05:
            exclusions.append(
                ("running_high_above_truth", f"{delta_raw:.2f}C")
            )
            continue
        delta = int(round(max(0.0, delta_raw)))
        if delta > MAX_DELTA_C:
            exclusions.append(("delta_out_of_support", f"{delta_raw:.2f}C"))
            continue
        chosen_root = root_at(roots, instant)
        if chosen_root is None:
            exclusions.append(("no_root_as_of", _iso(instant)))
            continue
        checkpoint_id = str(chosen_root[1]["checkpoint_id"])
        if checkpoint_id not in root_views:
            try:
                root_views[checkpoint_id] = RootView(target, chosen_root[1])
            except (ContractError, TypeError, ValueError, KeyError) as exc:
                exclusions.append(("root_geometry_invalid", str(exc)[:80]))
                continue
        view = root_views[checkpoint_id]
        prefix_rows = [
            {
                "observation_id": row["observation_id"],
                "observed_at_utc": row["valid_at"],
                "temperature_c": row["temperature_c"],
                "raw_text": row["raw_text"],
            }
            for row in prefix
        ]

        def _state(with_curve: bool):
            return compile_conditional_metar_path_state(
                prefix_rows,
                as_of_utc=_iso(instant),
                settlement_timezone=settlement_timezone,
                root_probabilities=view.probabilities,
                bucket_centers_c=view.centers,
                future_curve=view.curve if with_curve else None,
                root_checkpoint_id=(
                    view.curve_root_checkpoint_id if with_curve else None
                ),
                root_probability_sha256=(
                    view.curve_root_probability_sha if with_curve else None
                ),
                station_id=station_id,
                event_date=event_date,
            )

        try:
            state = _state(view.curve is not None)
        except ContractError as exc:
            # The curve runs out of future points in the local evening -- a
            # third of end-of-day station-days by the 2026-08-21 count -- and
            # the retired layer treated that as the whole state being
            # unusable.  It is not: the phase layer reads no model quantity
            # at all, and the remaining-warming layer degrades to its
            # no-shape form.  Losing the evening rows would have removed
            # exactly the hours where the day is decided.
            try:
                state = _state(False)
                exclusions.append(("curve_dropped", str(exc)[:60]))
            except ContractError as bare:
                exclusions.append(("path_state_refused", str(bare)[:80]))
                continue
        if not state.complete:
            exclusions.append(("path_state_incomplete", "complete=false"))
            continue

        # How long since the day last touched its own ceiling.  Ties go to the
        # latest report, not the first: while the temperature is still sitting
        # at the high the day has not turned over, and a counter that ran
        # anyway would call a flat afternoon a cooling one.
        high_index = max(
            range(len(prefix)),
            key=lambda position: (prefix[position]["temperature_c"], position),
        )
        hours_since_high = (
            instant - sample_instant(prefix[high_index])
        ).total_seconds() / 3600.0
        # The recent disagreement between the reports and the curve the root
        # was built on.  Three hours because the layer that consumes it
        # models a persistent same-day bias, and a single report's difference
        # is mostly the report's own rounding.
        bias_recent_c, bias_report_count = recent_curve_bias(
            view.curve_points,
            [
                (sample_instant(row), float(row["temperature_c"]))
                for row in prefix
            ],
            as_of=instant,
        )
        soft_lr, soft_state = _soft_likelihood_ratio(
            target=target, view=view, prefix=prefix, instant=instant,
            station_id=station_id, event_date=event_date,
            settlement_timezone=settlement_timezone, source_lane=source_lane,
            cache=soft_artifacts,
        )
        samples.append({
            "soft_lr_json": None if soft_lr is None else json.dumps(
                soft_lr, sort_keys=True
            ),
            "soft_lr_state": soft_state,
            "station_id": station_id,
            "event_date": event_date,
            "settlement_timezone": settlement_timezone,
            "as_of_utc": _iso(instant),
            "prefix_report_count": len(prefix),
            "local_hour": (
                instant.astimezone(ZoneInfo(settlement_timezone)).hour
                + instant.astimezone(ZoneInfo(settlement_timezone)).minute
                / 60.0
            ),
            "remaining_hours": max(
                0.0, (day_end - instant).total_seconds() / 3600.0
            ),
            "hours_since_high_refresh": max(0.0, hours_since_high),
            "drawdown_from_high_c": state.drawdown_from_high_c,
            "momentum_c_per_hour": state.momentum_c_per_hour,
            "acceleration_c_per_hour2": state.acceleration_c_per_hour2,
            "cloud_cover_fraction": state.cloud_cover_fraction,
            "precipitation_present": int(bool(state.precipitation_present)),
            "dewpoint_depression_c": state.dewpoint_depression_c,
            "wind_speed_mps": state.wind_speed_mps,
            "warming_so_far_c": running_high - prefix[0]["temperature_c"],
            "curve_shape_realized": view.shape_realized(instant),
            "curve_complete": int(bool(state.future_curve_complete)),
            **regional_features_at(
                regional_runs, as_of_utc=_iso(instant),
                running_high_c=running_high,
                latest_observed_c=prefix[-1]["temperature_c"],
            ),
            "root_bias_recent_c": bias_recent_c,
            "root_bias_report_count": bias_report_count,
            "root_center_c": view.center_c,
            "root_checkpoint_id": view.checkpoint_id,
            "running_high_c": running_high,
            "truth_tmax_c": truth_tmax_c,
            "delta_c": delta,
        })
    # Station-days are equal, reports within a day are not: a station that
    # files twice as often would otherwise carry twice the vote on what a
    # fleet-wide hour means.
    weight = 1.0 / len(samples) if samples else 0.0
    for sample in samples:
        sample["station_day_weight"] = weight
        sample["sample_id"] = (
            f"{station_id}-{event_date}-{sample['as_of_utc']}"
        )
    return samples, exclusions


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-db", default=str(
        ROOT / "data" / "bayes_basket_target_ssot.db"
    ))
    parser.add_argument("--archive-db", default=str(
        ROOT / "data" / "archive.db"
    ))
    parser.add_argument("--from", dest="window_from", required=True)
    parser.add_argument("--through", dest="window_through", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--source-lane", default="awc")
    parser.add_argument("--station", action="append", default=None)
    parser.add_argument("--built-at-utc", default=None)
    args = parser.parse_args()

    target = sqlite3.connect(f"file:{args.target_db}?mode=ro", uri=True)
    archive = sqlite3.connect(f"file:{args.archive_db}?mode=ro", uri=True)
    timezones = load_timezones(target)
    truth = load_truth(
        target,
        window_from=args.window_from,
        window_through=args.window_through,
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    out = sqlite3.connect(out_path)
    out.executescript(DDL)

    station_days = sorted(
        key for key in truth
        if key[0] in timezones
        and (args.station is None or key[0] in set(args.station))
    )
    total_samples = 0
    built_days = 0
    for station_id, event_date in station_days:
        try:
            samples, exclusions = build_station_day(
                target=target,
                archive=archive,
                station_id=station_id,
                event_date=event_date,
                settlement_timezone=timezones[station_id],
                truth_tmax_c=truth[(station_id, event_date)],
                source_lane=args.source_lane,
            )
        except Exception as exc:  # noqa: BLE001 - recorded, never silent
            out.execute(
                "INSERT INTO metar_phase_hazard_exclusion VALUES(?,?,?,?,?)",
                (station_id, event_date, "station_day_failed",
                 f"{type(exc).__name__}: {exc}"[:200], 1),
            )
            continue
        counted: dict[tuple[str, str], int] = {}
        for reason, detail in exclusions:
            counted[(reason, detail)] = counted.get((reason, detail), 0) + 1
        for (reason, detail), count in counted.items():
            out.execute(
                "INSERT INTO metar_phase_hazard_exclusion VALUES(?,?,?,?,?)",
                (station_id, event_date, reason, detail, count),
            )
        for sample in samples:
            out.execute(
                "INSERT OR IGNORE INTO metar_phase_hazard_sample("
                "sample_id,station_id,event_date,settlement_timezone,"
                "as_of_utc,prefix_report_count,local_hour,remaining_hours,"
                "hours_since_high_refresh,drawdown_from_high_c,"
                "momentum_c_per_hour,acceleration_c_per_hour2,"
                "cloud_cover_fraction,precipitation_present,"
                "dewpoint_depression_c,wind_speed_mps,warming_so_far_c,"
                "curve_shape_realized,curve_complete,"
                "regional_headroom_c,regional_live_gap_c,regional_system_id,"
                "soft_lr_json,soft_lr_state,"
                "root_bias_recent_c,"
                "root_bias_report_count,root_center_c,root_checkpoint_id,"
                "running_high_c,truth_tmax_c,delta_c,station_day_weight)"
                " VALUES(:sample_id,:station_id,:event_date,"
                ":settlement_timezone,:as_of_utc,:prefix_report_count,"
                ":local_hour,:remaining_hours,:hours_since_high_refresh,"
                ":drawdown_from_high_c,:momentum_c_per_hour,"
                ":acceleration_c_per_hour2,:cloud_cover_fraction,"
                ":precipitation_present,:dewpoint_depression_c,"
                ":wind_speed_mps,:warming_so_far_c,:curve_shape_realized,"
                ":curve_complete,:regional_headroom_c,:regional_live_gap_c,"
                ":regional_system_id,:soft_lr_json,:soft_lr_state,"
                ":root_bias_recent_c,"
                ":root_bias_report_count,:root_center_c,"
                ":root_checkpoint_id,:running_high_c,:truth_tmax_c,"
                ":delta_c,:station_day_weight)",
                sample,
            )
        if samples:
            built_days += 1
            total_samples += len(samples)
        out.commit()
    built_at = args.built_at_utc or _iso(datetime.now(timezone.utc))
    out.execute(
        "INSERT INTO metar_phase_hazard_build VALUES(?,?,?,?,?,?,?)",
        (SCHEMA, built_at, args.window_from, args.window_through,
         built_days, total_samples, args.source_lane),
    )
    out.commit()
    print(json.dumps({
        "schema": SCHEMA,
        "station_day_count": built_days,
        "sample_count": total_samples,
        "out": str(out_path),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
