"""Bounded registry-scoped scheduler for official ECMWF/GFS/ICON prefetch.

The scheduler runs in an isolated future inside the existing resident system
service; it is not a service and is not part of the decision critical path.
Provider HTTP details remain thin boundary adapters, while this module owns
the shared run key, settlement-timezone scope, mature-run fallback, resource
caps, and the exact capture set handed to the resident P0 producer.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Callable, Mapping, Sequence

from .calibration import validate_formal_ensemble_artifact
from .contracts import (
    CALIBRATION_CANDIDATE_MODEL_IDS,
    GLOBAL_FORMAL_MODEL_IDS,
    REGIONAL_CALIBRATION_MODEL_IDS,
    CalibrationActivation,
    ContractError,
    ModelEnsembleArtifact,
    content_sha256,
    model_set_hash,
    timestamp_in_window,
    timestamp_lte,
)
from .et_history import event_day_window
from .market_registry import load_market_registry_snapshot
from .model_run_contract import build_model_run_contract
from .native_ensemble_contract import MIN_CROSS_ENSEMBLE_FAMILIES
from .native_ensemble_oos import OFFICIAL_ARCHIVE_SCOPE_CONTRACTS
from .resource_bounds import (
    MAX_INPUT_FILES_PER_BATCH,
    assert_process_memory_budget,
    assert_target_raw_retention_budget,
    load_bounded_json,
    write_bounded_json_if_changed,
)
from .settlement_time import settlement_day_window
from .station_registry import station_coordinates
from .strict_asof_replay import (
    lead_bucket_for_maturity,
    maturity_key_for_model_run,
    season_bin_for_event_date,
)


SCHEMA = "bayes_basket_target_official_nwp_capture_scheduler_v1"
PROVIDERS = (
    "ecmwf_ifs025_official",
    "gfs_0p25_official",
    "icon_global_official",
    "gem_gdps_official",
    "cma_grapes_global_open_meteo",
    "ncep_hrrr_conus_open_meteo",
    "ncep_nbm_conus_open_meteo",
    "ncep_nam_conus_open_meteo",
    "icon_eu_open_meteo",
    "icon_d2_open_meteo",
    "meteofrance_arome_france0025_open_meteo",
    "ukmo_uk_deterministic_2km_open_meteo",
    "knmi_harmonie_arome_netherlands_open_meteo",
    "knmi_harmonie_arome_europe_open_meteo",
    "jma_msm_open_meteo",
)
# One calibrated ECMWF role has two official-native transport products.  This
# is capture redundancy for the same independence family, not a third model
# role, a second ensemble weight, or permission to use Open-Meteo AIFS.
ECMWF_OFFICIAL_CAPTURE_FALLBACKS = (
    "ecmwf_ifs025_official",
    "ecmwf_aifs025_official",
)
AIFS_PROVIDER_OOS_BINDING_SCHEMA = (
    "ecmwf_aifs_official_provider_strict_oos_binding_v1"
)
MODEL_PROVIDER_IDS = {
    "ECMWF": "ecmwf_ifs025_official",
    "GFS": "gfs_0p25_official",
    "ICON": "icon_global_official",
    "GEM": "gem_gdps_official",
    "CMA": "cma_grapes_global_open_meteo",
    "HRRR": "ncep_hrrr_conus_open_meteo",
    "NBM": "ncep_nbm_conus_open_meteo",
    "NAM": "ncep_nam_conus_open_meteo",
    "ICON-EU": "icon_eu_open_meteo",
    "ICON-D2": "icon_d2_open_meteo",
    "AROME-FR": "meteofrance_arome_france0025_open_meteo",
    "UKMO-2KM": "ukmo_uk_deterministic_2km_open_meteo",
    "HARMONIE-NL": "knmi_harmonie_arome_netherlands_open_meteo",
    "HARMONIE-EU": "knmi_harmonie_arome_europe_open_meteo",
    "JMA-MSM": "jma_msm_open_meteo",
}
MODEL_RUN_HOURS = {
    "ECMWF": (0, 12),
    "GFS": (0, 6, 12, 18),
    "ICON": (0, 6, 12, 18),
    "GEM": (0, 12),
    "CMA": (0, 12),
    "HRRR": (12,),
    "NBM": (12,),
    "NAM": (12,),
    "ICON-EU": (12,),
    "ICON-D2": (12,),
    "AROME-FR": (12,),
    "UKMO-2KM": (12,),
    "HARMONIE-NL": (12,),
    "HARMONIE-EU": (12,),
    "JMA-MSM": (12,),
}
AGGREGATOR_MODEL_BY_PROVIDER = {
    provider: model
    for model, provider in MODEL_PROVIDER_IDS.items()
    if provider.endswith("_open_meteo")
}
MAX_RUN_FALLBACKS = 4
MAX_CANDIDATE_MODELS = 5
REGIONAL_MODEL_POOL_PATH = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "bayes_basket_target"
    / "station_regional_model_pool_v1.json"
)
REGIONAL_MODEL_POOL_SCHEMA = (
    "bayes_basket_target_station_regional_model_pool_v1"
)
# Runs older than this relative to the event date are not worth capturing: the
# forecast is superseded several times over before the day arrives, and the
# archives are not kept indefinitely either.
MAX_RUN_AGE_DAYS = 2
# Leads a station-day is captured at, freshest first.  P0 is recomputed as the
# models update rather than decided once, so an open station-day is a series of
# captures: d2 two days out, then d1, then d0 on the day itself, each reading a
# run that has assimilated observations the previous had not.
SUPPORTED_LEAD_DAYS = tuple(range(0, MAX_RUN_AGE_DAYS + 1))
# Discovery and network execution are separate bounds.  The resident worker
# independently admits at most MAX_SCOPES_PER_CYCLE scopes to network capture,
# so this only bounds what discovery may return.
#
# The old figure of 135 was "45 stations x 3 event days", from when a
# station-day was captured once.  It is now captured once per lead, so the
# ceiling is stations x open event days x leads: 45 stations, at most four
# event days open at once (discovery admits a day from 72h before its local
# start until it closes), and one scope per supported lead.  Truncating below
# that drops the longest-lead scopes, which are the only ones ready early
# enough to give a not-yet-started day any P0 at all.
MAX_DISCOVERED_SCOPES = 45 * 4 * len(SUPPORTED_LEAD_DAYS)
MAX_SCOPES_PER_CYCLE = 4
SETTLEMENT_DEFINITION = "official_daily_tmax_c"
CANDIDATE_PREFETCH_SCHEMA = (
    "bayes_basket_target_candidate_raw_training_prefetch_v1"
)


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError("official_nwp_scheduler_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("official_nwp_scheduler_timestamp_timezone_required")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def candidate_run_initializations(
    *,
    as_of_utc: str,
    event_date: str,
    settlement_timezone: str,
    limit: int = MAX_RUN_FALLBACKS,
) -> list[str]:
    """Return newest permissible 6-hour runs, then deterministic fallbacks."""

    now = _parse_utc(as_of_utc)
    window = settlement_day_window(event_date, settlement_timezone)
    target_start = _parse_utc(str(window["target_day_start_utc"]))
    ceiling = min(now, target_start).replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    ceiling = ceiling.replace(hour=(ceiling.hour // 6) * 6)
    count = max(1, min(int(limit), MAX_RUN_FALLBACKS))
    return [_stamp(ceiling - timedelta(hours=6 * offset)) for offset in range(count)]


# Hours after a d2 run's 18Z init before its members are reliably complete on
# the object store.  Sized to the slower provider (ECMWF trails GEFS), and
# deliberately conservative: a late fetch costs one rotation step, an early
# one wastes an entire capture.
D2_PUBLICATION_MARGIN_HOURS = 8


def contracted_run_inits(
    event_date: str,
    *,
    lead_days: int,
) -> dict[str, datetime]:
    """The run each official archive reads for this event date at this lead.

    Derived from ``OFFICIAL_ARCHIVE_SCOPE_CONTRACTS`` rather than restated, so
    the schedule cannot advertise a run the collectors will not fetch.  The
    cycles differ by system -- ECMWF and GEFS are read at 18Z, ECCC at 00Z,
    ICON at 12Z, each being the cycle where that provider publishes the fields
    at the resolution the extraction needs -- so a lead is a set of runs, not
    one run.
    """

    target = date.fromisoformat(event_date)
    lead_key = f"d{int(lead_days)}"
    runs: dict[str, datetime] = {}
    for system_id, contract in OFFICIAL_ARCHIVE_SCOPE_CONTRACTS.items():
        lead = (contract.get("leads") or {}).get(lead_key)
        if not isinstance(lead, Mapping):
            continue
        cycle = str(lead.get("run_cycle") or "").strip().upper()
        if not cycle.endswith("Z") or not cycle[:-1].isdigit():
            raise ContractError("official_nwp_scheduler_run_cycle_invalid")
        runs[str(system_id)] = datetime.combine(
            target - timedelta(days=int(lead_days)),
            time(hour=int(cycle[:-1])),
            tzinfo=timezone.utc,
        )
    if not runs:
        raise ContractError("official_nwp_scheduler_lead_not_contracted")
    return runs


def run_ready_at_utc(run_init: datetime) -> datetime:
    """When a run's members are reliably complete on the object store.

    A run existing is not a run being complete: its members land over the
    following hours.  Fetching before then burns a capture slot on a scope
    whose fields cannot all resolve yet.

    The margin is a property of the providers, not of the lead -- every cycle
    publishes on the same schedule relative to its own initialisation -- so
    this takes the run itself rather than an event date and a lead.  It is
    sized to the slower of the two archives (ECMWF trails GEFS): fetching a
    little late costs one rotation step, fetching early costs a whole capture.
    """

    return (
        run_init.astimezone(timezone.utc)
        + timedelta(hours=D2_PUBLICATION_MARGIN_HOURS)
    )


def scope_ready_at_utc(event_date: str, *, lead_days: int) -> datetime:
    """When a lead has enough families on the object store to be worth taking.

    Not the last system, and not the first.  A capture that yields a single
    family cannot produce a cross-family P0 at all, so waiting for the second
    is not conservatism; waiting for the fourth would hold d0 back until after
    the event day has ended for most stations, since the 18Z archives only land
    the following morning.
    """

    ready = sorted(
        run_ready_at_utc(run)
        for run in contracted_run_inits(
            event_date, lead_days=lead_days
        ).values()
    )
    return ready[min(MIN_CROSS_ENSEMBLE_FAMILIES, len(ready)) - 1]


def registry_capture_scopes(
    registry: Mapping[str, Any],
    *,
    as_of_utc: str,
    max_scopes: int = MAX_DISCOVERED_SCOPES,
) -> list[dict[str, Any]]:
    """Discover the bounded station-date-lead catalog for the open window.

    An open station-day appears once per supported lead, because P0 is
    recomputed as the models update rather than decided once: the same day is
    captured at d2, again at d1, and again at d0, each from a run that has
    assimilated more than the last.

    This function performs no network work.  Its bound therefore covers the
    full canonical catalog; callers apply their smaller per-cycle acquisition
    limit only after deterministic priority and round-robin selection.
    """

    rows = registry.get("rows")
    if not isinstance(rows, list):
        raise ContractError("official_nwp_scheduler_registry_rows_missing")
    grouped: dict[tuple[str, str, str], set[str]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ContractError("official_nwp_scheduler_registry_row_invalid")
        if (
            int(row.get("source_eligible") or 0) != 1
            or int(row.get("market_active") or 0) != 1
            or int(row.get("market_accepting_orders") or 0) != 1
        ):
            continue
        station = str(row.get("station_id") or "").strip().upper()
        event_date = str(row.get("event_date") or "").strip()
        timezone_name = str(row.get("settlement_timezone") or "").strip()
        outcome = str(row.get("outcome") or "").strip()
        if not station or not event_date or not timezone_name or not outcome:
            raise ContractError("official_nwp_scheduler_registry_scope_invalid")
        grouped.setdefault((event_date, timezone_name, station), set()).add(outcome)
    # Scopes carry their sort key alongside them rather than recomputing it in
    # the comparator: a station-day now expands to one scope per lead, so a key
    # derived per comparison would redo the timezone arithmetic several times
    # more often than there are scopes.
    ordered: list[tuple[tuple[bool, datetime, str, int], dict[str, Any]]] = []
    now = _parse_utc(as_of_utc)
    for event_date, timezone_name, station in sorted(grouped):
        state = event_day_window(
            event_date,
            as_of_utc=as_of_utc,
            target_timezone=timezone_name,
        )
        if state["state"] == "closed":
            continue
        start = _parse_utc(str(state["target_day_start_utc"]))
        if start - now > timedelta(hours=72):
            continue
        # One scope per lead, freshest lead first.  A station-day is not one
        # unit of work but a series: the d1 capture reads a run that has
        # assimilated a day of observations the d2 run had not, and the d0 one
        # another day beyond that.  Emitting only the freshest lead would leave
        # the earlier ones permanently uncaptured, and those are what the
        # archive trains on; emitting only d2 is what used to freeze P0 at the
        # forecast made two days out.
        #
        # The lead is the finest unit the collectors distinguish: each archive
        # module reads its own contracted cycle and varies only the run day, so
        # a finer schedule would emit scopes that resolve to the same fetch.
        for lead_days in SUPPORTED_LEAD_DAYS:
            runs = contracted_run_inits(event_date, lead_days=lead_days)
            ready_at = scope_ready_at_utc(event_date, lead_days=lead_days)
            # Model readiness outranks the timezone order.  Leads differ by a
            # day in when their runs publish, so a purely chronological sweep
            # spends capture slots on scopes whose runs are still incomplete
            # while ready ones wait behind them.  Within a readiness group the
            # east-to-west day order still holds, and within one station-day
            # the shortest lead -- the freshest run -- leads.
            ordered.append(((
                not (ready_at <= now),
                start,
                station,
                lead_days,
            ), {
                "event_date": event_date,
                "settlement_timezone": timezone_name,
                "station_ids": [station],
                "outcome_count": len(
                    grouped[(event_date, timezone_name, station)]
                ),
                "event_day_state": state["state"],
                "lead_days": lead_days,
                # Per system, because the archives are contracted at different
                # cycles.  Publishing one run time for the scope would name a
                # run three of the four collectors do not read.
                "run_init_utc_by_system": {
                    system_id: run.isoformat().replace("+00:00", "Z")
                    for system_id, run in sorted(runs.items())
                },
                # A run is publishable before its fields have all landed.
                "model_ready_at_utc": ready_at.isoformat().replace(
                    "+00:00", "Z"
                ),
                "model_ready": ready_at <= now,
            }))
    ordered.sort(key=lambda pair: pair[0])
    scopes = [scope for _, scope in ordered]
    return scopes[: max(0, min(int(max_scopes), MAX_DISCOVERED_SCOPES))]


def _capture_paths(result: Mapping[str, Any]) -> list[Path]:
    rows: list[Any] = []
    fanout = result.get("fanout")
    if isinstance(fanout, Mapping):
        rows.extend(fanout.get("outputs") or [])
    rows.extend(result.get("records") or [])
    paths: list[Path] = []
    for row in rows:
        if not isinstance(row, Mapping) or row.get("status") not in {
            "CAPTURED",
            "REUSED",
        }:
            continue
        raw = str(row.get("path") or "").strip()
        if raw:
            path = Path(raw).expanduser().resolve()
            if path.is_file():
                paths.append(path)
    return sorted(set(paths))


def _provider_complete(result: Mapping[str, Any], paths: Sequence[Path]) -> bool:
    fanout = result.get("fanout")
    if isinstance(fanout, Mapping):
        outputs = fanout.get("outputs")
        captured_count = int(fanout.get("captured_count") or 0)
        expected_count = int(fanout.get("expected_arrival_count") or 0)
        return (
            bool(paths)
            and isinstance(outputs, list)
            and expected_count > 0
            and captured_count == expected_count
            and len(outputs) == expected_count
            and all(
                isinstance(row, Mapping)
                and row.get("status") in {"CAPTURED", "REUSED"}
                for row in outputs
            )
            and int(fanout.get("blocked_count") or 0) == 0
            and not list(fanout.get("blockers") or [])
        )
    return (
        bool(paths)
        and int(result.get("blocked_count") or 0) == 0
        and result.get("early_stopped") is not True
    )


def _normalize_candidate_models(values: Sequence[Any]) -> list[str]:
    """Normalize model names/provider ids without widening formal capture."""

    if isinstance(values, (str, bytes)):
        raise ContractError("official_nwp_candidate_models_invalid")
    provider_to_model = {
        str(provider).strip().lower(): model
        for model, provider in MODEL_PROVIDER_IDS.items()
    }
    models: list[str] = []
    seen_providers: set[str] = set()
    for value in values:
        token = str(value or "").strip()
        if not token:
            continue
        model = token.upper()
        if model not in MODEL_PROVIDER_IDS:
            model = provider_to_model.get(token.lower(), "")
        if not model or model not in MODEL_PROVIDER_IDS:
            raise ContractError("official_nwp_candidate_model_invalid")
        provider = MODEL_PROVIDER_IDS[model]
        if provider in seen_providers:
            continue
        seen_providers.add(provider)
        models.append(model)
    if len(models) < 2:
        raise ContractError("official_nwp_candidate_model_count_below_minimum")
    if len(models) > MAX_CANDIDATE_MODELS:
        raise ContractError("official_nwp_candidate_model_count_exceeded")
    return models


def candidate_model_batch_for_station(
    *,
    station_id: str,
    run_initialization_utc: str,
    regional_pool_path: Path = REGIONAL_MODEL_POOL_PATH,
) -> list[str]:
    """Select one bounded, rotating calibration-only acquisition batch."""

    station = str(station_id or "").strip().upper()
    if not station:
        raise ContractError("official_nwp_candidate_station_missing")
    raw = load_bounded_json(
        regional_pool_path.expanduser().resolve(),
        limit=1024 * 1024,
    )
    if (
        not isinstance(raw, Mapping)
        or raw.get("schema") != REGIONAL_MODEL_POOL_SCHEMA
    ):
        raise ContractError("official_nwp_regional_pool_invalid")
    station_pool = raw.get("station_regional_pool")
    if not isinstance(station_pool, Mapping):
        raise ContractError("official_nwp_regional_pool_invalid")
    regional_raw = station_pool.get(station, [])
    if (
        not isinstance(regional_raw, list)
        or len(regional_raw) != len(set(regional_raw))
    ):
        raise ContractError("official_nwp_station_regional_pool_invalid")
    regional = [
        str(model or "").strip().upper()
        for model in regional_raw
        if str(model or "").strip()
    ]
    if any(
        model not in REGIONAL_CALIBRATION_MODEL_IDS for model in regional
    ):
        raise ContractError("official_nwp_station_regional_model_invalid")
    run = _parse_utc(run_initialization_utc)
    pool = [
        model
        for model in list(GLOBAL_FORMAL_MODEL_IDS) + regional
        if run.hour in MODEL_RUN_HOURS[model]
    ]
    if (
        len(pool) != len(set(pool))
        or any(model not in CALIBRATION_CANDIDATE_MODEL_IDS for model in pool)
    ):
        raise ContractError("official_nwp_station_candidate_pool_invalid")
    if len(pool) <= MAX_CANDIDATE_MODELS:
        return _normalize_candidate_models(pool)
    # Regional candidates are currently bound to the 12Z station pool.  Use
    # the UTC run date rather than the absolute six-hour cycle number:
    # multiplying four daily cycles by a five-model batch would otherwise
    # keep a ten-model pool forever pinned to the same half.
    cycle_number = run.date().toordinal()
    offset = (cycle_number * MAX_CANDIDATE_MODELS) % len(pool)
    batch = [
        pool[(offset + index) % len(pool)]
        for index in range(min(MAX_CANDIDATE_MODELS, len(pool)))
    ]
    return _normalize_candidate_models(batch)


def _candidate_sha(name: str, value: Any) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(
        char not in "0123456789abcdef" for char in result
    ):
        raise ContractError(f"official_nwp_candidate_{name}_invalid")
    return result


def _candidate_arrival_bindings(
    paths: Sequence[Path],
    *,
    provider_id: str,
    model_id: str,
    source_class: str,
    station_id: str,
    event_date: str,
    run_initialization_utc: str,
) -> list[dict[str, Any]]:
    """Extract content-bound publication/arrival facts from provider output."""

    bindings: dict[str, dict[str, Any]] = {}
    for path in paths:
        if path.suffix.lower() != ".json":
            continue
        payload = load_bounded_json(path)
        if not isinstance(payload, Mapping):
            continue
        supplied_payload_sha = payload.get("receipt_sha256")
        if supplied_payload_sha is not None:
            payload_sha = _candidate_sha(
                "provider_payload_receipt_sha256",
                supplied_payload_sha,
            )
            if content_sha256({
                key: value
                for key, value in payload.items()
                if key != "receipt_sha256"
            }) != payload_sha:
                raise ContractError(
                    "official_nwp_candidate_provider_payload_hash_mismatch"
                )
        candidates: list[Mapping[str, Any]] = []
        if isinstance(payload.get("arrival"), Mapping):
            arrival = payload["arrival"]
            supplied = _candidate_sha(
                "arrival_content_sha256",
                arrival.get("content_sha256"),
            )
            if content_sha256({
                key: value
                for key, value in arrival.items()
                if key != "content_sha256"
            }) != supplied:
                raise ContractError(
                    "official_nwp_candidate_arrival_hash_mismatch"
                )
            candidates.append(arrival)
        for field in ("input_artifacts", "decoded_points"):
            rows = payload.get(field)
            if isinstance(rows, list):
                candidates.extend(
                    row for row in rows if isinstance(row, Mapping)
                )
        for raw in candidates:
            raw_provider = str(
                raw.get("provider_id") or ""
            ).strip().lower()
            raw_station = str(
                raw.get("station_id") or ""
            ).strip().upper()
            raw_event_date = str(
                raw.get("event_date")
                or raw.get("target_local_date")
                or ""
            ).strip()
            raw_run = str(
                raw.get("run_initialization_utc")
                or raw.get("model_run_initialization_utc")
                or ""
            ).strip().replace("+00:00", "Z")
            if (
                raw_provider != provider_id
                or raw_station != station_id
                or raw_event_date != event_date
                or raw_run != run_initialization_utc
            ):
                continue
            received = _stamp(_parse_utc(str(
                raw.get("received_at_utc") or ""
            )))
            receipt_sha = _candidate_sha(
                "provider_receipt_sha256",
                raw.get("receipt_sha256"),
            )
            raw_values = raw.get("raw_payload_sha256s")
            if isinstance(raw_values, list):
                raw_shas = sorted({
                    _candidate_sha("raw_sha256", value)
                    for value in raw_values
                })
            else:
                raw_value = (
                    raw.get("raw_sha256")
                    or raw.get("raw_message_sha256")
                )
                raw_shas = [
                    _candidate_sha("raw_sha256", raw_value)
                ]
            published_raw = (
                raw.get("published_at_utc")
                or raw.get("native_publication_at_utc")
            )
            if source_class == "aggregator_republisher":
                if published_raw not in (None, ""):
                    raise ContractError(
                        "official_nwp_candidate_aggregator_native_publication_forbidden"
                    )
                published = None
            else:
                published = _stamp(_parse_utc(str(published_raw or "")))
                if _parse_utc(published) > _parse_utc(received):
                    raise ContractError(
                        "official_nwp_candidate_publication_after_arrival"
                    )
            identity = {
                "model_id": model_id,
                "provider_id": provider_id,
                "source_class": source_class,
                "station_id": station_id,
                "event_date": event_date,
                "run_initialization_utc": run_initialization_utc,
                "published_at_utc": published,
                "received_at_utc": received,
                "raw_sha256s": raw_shas,
                "provider_receipt_sha256": receipt_sha,
                "evidence_payload_sha256": content_sha256(payload),
                "evidence_path": str(path),
            }
            bindings[content_sha256(identity)] = {
                **identity,
                "binding_sha256": content_sha256(identity),
            }
    if not bindings:
        raise ContractError(
            "official_nwp_candidate_publication_arrival_evidence_missing"
        )
    return [bindings[key] for key in sorted(bindings)]


def _remove_candidate_fragment(
    path: Path,
    *,
    expected_parent: Path,
) -> None:
    """Delete only an unpromoted provider fragment in its exact run scope."""

    parent = expected_parent.expanduser().resolve()
    candidate = path.expanduser()
    if (
        candidate.is_symlink()
        or candidate.resolve().parent != parent
        or not candidate.name
    ):
        raise ContractError("official_nwp_candidate_fragment_path_invalid")
    if candidate.is_dir():
        shutil.rmtree(candidate)
    elif candidate.exists():
        candidate.unlink()


def _load_candidate_receipt(
    path: Path,
    *,
    fingerprint_sha256: str,
) -> dict[str, Any] | None:
    if not path.is_file() or path.is_symlink():
        return None
    payload = load_bounded_json(path)
    if not isinstance(payload, Mapping):
        raise ContractError("official_nwp_candidate_receipt_invalid")
    supplied = _candidate_sha(
        "receipt_content_sha256",
        payload.get("content_sha256"),
    )
    if content_sha256({
        key: value
        for key, value in payload.items()
        if key != "content_sha256"
    }) != supplied:
        raise ContractError("official_nwp_candidate_receipt_hash_mismatch")
    if payload.get("fingerprint_sha256") != fingerprint_sha256:
        raise ContractError("official_nwp_candidate_receipt_scope_mismatch")
    return dict(payload)


def prefetch_candidate_raw_training(
    *,
    registry_snapshot_path: Path,
    scope: Mapping[str, Any],
    as_of_utc: str,
    run_initialization_utc: str,
    candidate_models: Sequence[Any],
    output_root: Path,
    provider_runner: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Capture one bounded station/date/run for future strict-as-of training.

    This sidecar deliberately has no activation input and never calls
    :func:`capture_scope`.  It preserves complete raw/decode paths returned by
    each provider, isolates failures, and emits only a candidate receipt.  A
    later M3-01 builder must independently prove publication/arrival timing;
    this function never creates a native-arrival index or a P0 packet.
    """

    registry = load_market_registry_snapshot(registry_snapshot_path)
    runner = provider_runner or _default_provider_runner
    event_date = str(scope.get("event_date") or "").strip()
    timezone_name = str(scope.get("settlement_timezone") or "").strip()
    station_ids = [
        str(value).strip().upper()
        for value in scope.get("station_ids") or []
        if str(value).strip()
    ]
    if not event_date or not timezone_name or len(station_ids) != 1:
        raise ContractError("official_nwp_candidate_scope_must_be_one_station")
    models = _normalize_candidate_models(candidate_models)
    run = _parse_utc(run_initialization_utc)
    as_of = _parse_utc(as_of_utc)
    if run > as_of:
        raise ContractError("official_nwp_candidate_run_after_as_of")
    normalized_run = _stamp(run)
    normalized_as_of = _stamp(as_of)
    station_id = station_ids[0]
    candidate_root = (
        output_root.expanduser().resolve() / "candidate_raw"
    )
    destination = (
        candidate_root
        / event_date
        / timezone_name.replace("/", "_")
        / station_id
        / normalized_run.replace("-", "").replace(":", "")
    )
    fingerprint_identity = {
        "event_date": event_date,
        "settlement_timezone": timezone_name,
        "station_id": station_id,
        "run_initialization_utc": normalized_run,
        "candidate_models": models,
        "market_registry_snapshot_sha256": str(
            registry["snapshot_sha256"]
        ),
    }
    fingerprint_sha256 = content_sha256(fingerprint_identity)
    assert_target_raw_retention_budget(candidate_root)
    contract = build_model_run_contract(
        target_local_date=event_date,
        run_initialization_utc=normalized_run,
        station_ids=[station_id],
        target_timezone=timezone_name,
        market_registry_snapshot_sha256=str(registry["snapshot_sha256"]),
    )
    contract_path = destination / "model_run_contract.json"
    write_bounded_json_if_changed(contract_path, contract)
    receipt_path = destination / "candidate_prefetch_receipt.json"
    previous = _load_candidate_receipt(
        receipt_path,
        fingerprint_sha256=fingerprint_sha256,
    )
    previous_by_provider = {
        str(row.get("provider_id") or ""): dict(row)
        for row in (previous or {}).get("providers") or []
        if isinstance(row, Mapping)
    }

    provider_rows: list[dict[str, Any]] = []
    all_paths: list[Path] = []
    complete_provider_count = 0
    for model in models:
        provider = MODEL_PROVIDER_IDS[model]
        source_class = (
            "aggregator_republisher"
            if provider in AGGREGATOR_MODEL_BY_PROVIDER
            else "native_or_official"
        )
        previous_row = previous_by_provider.get(provider)
        if previous_row is not None and previous_row.get("state") == "COMPLETE":
            previous_paths = [
                Path(str(value)).expanduser().resolve()
                for value in previous_row.get("capture_paths") or []
            ]
            if (
                previous_paths
                and all(path.is_file() for path in previous_paths)
                and content_sha256(
                    _candidate_arrival_bindings(
                        previous_paths,
                        provider_id=provider,
                        model_id=model,
                        source_class=source_class,
                        station_id=station_id,
                        event_date=event_date,
                        run_initialization_utc=normalized_run,
                    )
                )
                == previous_row.get("evidence_bindings_sha256")
            ):
                provider_rows.append(previous_row)
                all_paths.extend(previous_paths)
                complete_provider_count += 1
                continue
        final_provider_dir = destination / provider
        try:
            assert_process_memory_budget()
            if final_provider_dir.exists() or final_provider_dir.is_symlink():
                _remove_candidate_fragment(
                    final_provider_dir,
                    expected_parent=destination,
                )
            destination.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix=f".candidate-{provider}-",
                dir=destination,
            ) as temporary:
                temporary_provider_dir = (
                    Path(temporary).resolve() / provider
                )
                result = runner(
                    provider_id=provider,
                    event_date=event_date,
                    run_initialization_utc=normalized_run,
                    as_of_utc=normalized_as_of,
                    settlement_timezone=timezone_name,
                    station_ids=[station_id],
                    registry_snapshot_sha256=str(
                        registry["snapshot_sha256"]
                    ),
                    contract_path=contract_path,
                    output_dir=temporary_provider_dir,
                )
                if not isinstance(result, Mapping):
                    raise ContractError(
                        "official_nwp_scheduler_provider_result_invalid"
                    )
                paths = _capture_paths(result)
                if not _provider_complete(result, paths):
                    raise ContractError(
                        "official_nwp_candidate_provider_incomplete"
                    )
                relative_paths = []
                for path in paths:
                    try:
                        relative_paths.append(
                            path.resolve().relative_to(
                                temporary_provider_dir
                            )
                        )
                    except ValueError as exc:
                        raise ContractError(
                            "official_nwp_candidate_provider_path_escape"
                        ) from exc
                bindings = _candidate_arrival_bindings(
                    paths,
                    provider_id=provider,
                    model_id=model,
                    source_class=source_class,
                    station_id=station_id,
                    event_date=event_date,
                    run_initialization_utc=normalized_run,
                )
                projected_bytes = sum(
                    int(path.stat().st_size)
                    for path in temporary_provider_dir.rglob("*")
                    if path.is_file() and not path.is_symlink()
                )
                assert_target_raw_retention_budget(
                    candidate_root,
                    bytes_needed=projected_bytes,
                )
                os.replace(
                    temporary_provider_dir,
                    final_provider_dir,
                )
                final_paths = [
                    final_provider_dir / relative
                    for relative in relative_paths
                ]
                remapped_bindings = []
                for binding in bindings:
                    evidence_path = Path(
                        str(binding["evidence_path"])
                    ).resolve()
                    relative = evidence_path.relative_to(
                        temporary_provider_dir
                    )
                    identity = {
                        key: value
                        for key, value in binding.items()
                        if key not in {
                            "binding_sha256",
                            "evidence_path",
                        }
                    }
                    identity["evidence_path"] = str(
                        final_provider_dir / relative
                    )
                    remapped_bindings.append({
                        **identity,
                        "binding_sha256": content_sha256(identity),
                    })
                assert_target_raw_retention_budget(candidate_root)
            complete_provider_count += 1
            all_paths.extend(final_paths)
            provider_rows.append({
                "model_id": model,
                "provider_id": provider,
                "source_class": source_class,
                "aggregator_or_republisher":
                    source_class == "aggregator_republisher",
                "native_arrival_indexed": False,
                "state": "COMPLETE",
                "capture_paths": [
                    str(path) for path in final_paths
                ],
                "capture_path_count": len(final_paths),
                "blocked_count": 0,
                "evidence_bindings": remapped_bindings,
                "evidence_bindings_sha256": content_sha256(
                    remapped_bindings
                ),
            })
        except Exception as exc:
            if final_provider_dir.exists() or final_provider_dir.is_symlink():
                _remove_candidate_fragment(
                    final_provider_dir,
                    expected_parent=destination,
                )
            provider_rows.append({
                "model_id": model,
                "provider_id": provider,
                "source_class": source_class,
                "aggregator_or_republisher": source_class == "aggregator_republisher",
                "native_arrival_indexed": False,
                "state": "BLOCKED_FAIL_CLOSED",
                "capture_paths": [],
                "capture_path_count": 0,
                "blocked_count": 1,
                "reason": f"{type(exc).__name__}:{exc}",
            })
        finally:
            assert_process_memory_budget()

    unique_paths = sorted(set(all_paths))
    if len(unique_paths) > MAX_INPUT_FILES_PER_BATCH:
        raise ContractError("official_nwp_scheduler_capture_batch_exceeded")
    successful_models = [
        row["model_id"]
        for row in provider_rows
        if row["state"] == "COMPLETE"
    ]
    receipt_identity = {
        "schema": CANDIDATE_PREFETCH_SCHEMA,
        "state": (
            "CAPTURED_CANDIDATE"
            if len(successful_models) >= 2
            else "PARTIAL_CANDIDATE"
            if successful_models
            else "BLOCKED_FAIL_CLOSED"
        ),
        "candidate_only": True,
        "p0_materialization_authorized": False,
        "production_no_live": True,
        "execution_mode": "production_no_live",
        "event_date": event_date,
        "settlement_timezone": timezone_name,
        "station_id": station_id,
        "run_initialization_utc": normalized_run,
        "as_of_utc": str(
            (previous or {}).get("as_of_utc") or normalized_as_of
        ),
        "fingerprint_sha256": fingerprint_sha256,
        "fingerprint": fingerprint_identity,
        "candidate_models": models,
        "candidate_provider_ids": [MODEL_PROVIDER_IDS[model] for model in models],
        "successful_models": successful_models,
        "complete_provider_count": complete_provider_count,
        "candidate_pool_complete":
            complete_provider_count == len(models),
        "providers": provider_rows,
        "capture_paths": [str(path) for path in unique_paths],
        "capture_path_count": len(unique_paths),
        "model_run_contract_path": str(contract_path),
        "native_arrival_indexed": False,
        "creates_activation": False,
        "creates_p0": False,
        "writes_production_db": False,
        "writes_target_database": False,
        "calls_order_gateway": False,
        "places_orders": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }
    receipt = {
        **receipt_identity,
        "content_sha256": content_sha256(receipt_identity),
    }
    write_bounded_json_if_changed(receipt_path, receipt)
    assert_target_raw_retention_budget(candidate_root)
    assert_process_memory_budget()
    return {**receipt, "receipt_path": str(receipt_path)}


def _default_provider_runner(
    *,
    provider_id: str,
    event_date: str,
    run_initialization_utc: str,
    as_of_utc: str,
    settlement_timezone: str,
    station_ids: Sequence[str],
    registry_snapshot_sha256: str,
    contract_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Invoke one bounded provider adapter without creating another service."""

    if provider_id == "ecmwf_ifs025_official":
        from scripts.capture_bayes_basket_target_ecmwf_same_run import (
            build_plan,
            execute_plan,
        )

        plan = build_plan(
            target_local_date=event_date,
            run_initialization_utc=run_initialization_utc,
            as_of_utc=as_of_utc,
            contract_path=contract_path,
        )
        return execute_plan(plan, output_root=output_dir)
    if provider_id == "ecmwf_aifs025_official":
        try:
            from scripts.run_bayes_basket_target_aifs_forward_capture import (
                build_plan,
                execute_plan,
            )
        except ImportError as exc:  # explicit; never silently skip AIFS
            raise ContractError(
                "official_nwp_scheduler_aifs_adapter_unavailable"
            ) from exc

        plan = build_plan(
            event_date=event_date,
            run_initialization_utc=run_initialization_utc,
            as_of_utc=as_of_utc,
            station_ids=list(station_ids),
            source_station_id=str(station_ids[0]),
            target_timezone=settlement_timezone,
            market_registry_snapshot_sha256=registry_snapshot_sha256,
        )
        # The AIFS adapter fans out its own official-native capture receipt.
        # Keep source and fanout output under this exact run/provider scope.
        return execute_plan(
            plan,
            source_root=output_dir,
            output_root=output_dir,
        )
    if provider_id == "gfs_0p25_official":
        from scripts.capture_bayes_basket_target_gfs_same_run import capture_run

        return capture_run(
            target_local_date=event_date,
            run_initialization_utc=run_initialization_utc,
            decision_at_utc=as_of_utc,
            contract_path=contract_path,
            output_dir=output_dir,
        )
    if provider_id == "icon_global_official":
        from scripts.run_bayes_basket_target_icon_forward_capture import (
            build_plan,
            execute_plan,
        )

        plan = build_plan(
            event_date=event_date,
            run_initialization_utc=run_initialization_utc,
            as_of_utc=as_of_utc,
            station_ids=list(station_ids),
            target_timezone=settlement_timezone,
            market_registry_snapshot_sha256=registry_snapshot_sha256,
            registry_bound=True,
            source_station_id=str(station_ids[0]),
            due_only=False,
        )
        # Keep both ICON raw retention accounting and the retained objects
        # inside this scheduler-owned, station/run-bounded scope.  Falling
        # back to the adapter's historical DEFAULT_ARRIVAL_ROOT makes a new
        # target capture scan unrelated legacy research objects and can
        # falsely trip the provider budget before writing a single byte.
        return execute_plan(
            plan,
            source_root=output_dir,
            output_root=output_dir,
        )
    if provider_id == "gem_gdps_official":
        from scripts.capture_bayes_basket_target_gem_exact_run import capture

        output = output_dir / "gem_exact_run_decode.json"
        capture(
            stations=station_ids,
            event_date=event_date,
            run_initialization_utc=run_initialization_utc,
            as_of_utc=as_of_utc,
            registry_path=(
                Path(__file__).resolve().parents[1]
                / "data/bayes_basket_target_market_registry_snapshot.json"
            ),
            raw_root=output_dir / "raw",
            output_path=output,
            max_workers=2,
        )
        return {
            "records": [{"status": "CAPTURED", "path": str(output)}],
            "blocked_count": 0,
        }
    if provider_id in AGGREGATOR_MODEL_BY_PROVIDER:
        from scripts.capture_bayes_basket_target_cma_exact_run import capture

        model_id = AGGREGATOR_MODEL_BY_PROVIDER[provider_id]
        output = output_dir / (
            model_id.lower().replace("-", "_") + "_exact_run_decode.json"
        )
        capture(
            stations=station_ids,
            event_date=event_date,
            run_initialization_utc=run_initialization_utc,
            as_of_utc=as_of_utc,
            registry_path=(
                Path(__file__).resolve().parents[1]
                / "data/bayes_basket_target_market_registry_snapshot.json"
            ),
            raw_root=output_dir / "raw",
            output_path=output,
            model_id=model_id,
        )
        return {
            "records": [{"status": "CAPTURED", "path": str(output)}],
            "blocked_count": 0,
        }
    raise ContractError("official_nwp_scheduler_provider_invalid")


def _capture_model_role(
    *,
    role_provider_id: str,
    model_id: str,
    provider_runner: Callable[..., dict[str, Any]],
    event_date: str,
    run_initialization_utc: str,
    as_of_utc: str,
    settlement_timezone: str,
    station_ids: Sequence[str],
    registry_snapshot_sha256: str,
    contract_path: Path,
    run_dir: Path,
) -> dict[str, Any]:
    """Capture exactly the provider signed by the activation binding.

    AIFS must never be selected because an IFS request happened to fail.  A
    binding may name AIFS explicitly after its own strict OOS/activation; a
    legacy binding without provider selection remains pinned to IFS.
    """

    providers = (role_provider_id,)
    attempts: list[dict[str, Any]] = []
    for provider_id in providers:
        try:
            result = provider_runner(
                provider_id=provider_id,
                event_date=event_date,
                run_initialization_utc=run_initialization_utc,
                as_of_utc=as_of_utc,
                settlement_timezone=settlement_timezone,
                station_ids=station_ids,
                registry_snapshot_sha256=registry_snapshot_sha256,
                contract_path=contract_path,
                output_dir=run_dir / provider_id,
            )
            if not isinstance(result, Mapping):
                raise ContractError("official_nwp_scheduler_provider_result_invalid")
            paths = _capture_paths(result)
            complete = _provider_complete(result, paths)
            attempts.append({
                "provider_id": provider_id,
                "state": "COMPLETE" if complete else "BLOCKED_OR_PARTIAL",
                "capture_path_count": len(paths),
                "blocked_count": int(result.get("blocked_count") or 0),
            })
            if complete:
                return {
                    "model_id": model_id,
                    "role_provider_id": role_provider_id,
                    "selected_provider_id": provider_id,
                    "complete": True,
                    "paths": paths,
                    "attempts": attempts,
                }
        except Exception as exc:
            attempts.append({
                "provider_id": provider_id,
                "state": "BLOCKED_FAIL_CLOSED",
                "capture_path_count": 0,
                "blocked_count": 1,
                "reason": f"{type(exc).__name__}:{exc}",
            })
        finally:
            assert_process_memory_budget()
    return {
        "model_id": model_id,
        "role_provider_id": role_provider_id,
        "selected_provider_id": "",
        "complete": False,
        "paths": [],
        "attempts": attempts,
    }


def _require_aifs_provider_oos_binding(
    binding: Mapping[str, Any],
    *,
    activation: CalibrationActivation,
    ensemble: ModelEnsembleArtifact,
) -> None:
    """Require AIFS's own signed strict-OOS/provider-lineage authority.

    AIFS and IFS share the ECMWF independence family.  Choosing the former
    for the one ECMWF role is therefore permitted only when its own native
    strict-OOS result is bound to this exact ensemble activation.  A generic
    ECMWF ensemble activation, or a legacy binding that only names AIFS, is
    not sufficient evidence and must never enable a capture substitution.
    """

    raw_by_model = binding.get("provider_strict_oos_binding_by_model")
    if not isinstance(raw_by_model, Mapping):
        raise ContractError("official_nwp_scheduler_aifs_oos_binding_missing")
    raw = raw_by_model.get("ECMWF")
    if not isinstance(raw, Mapping):
        raise ContractError("official_nwp_scheduler_aifs_oos_binding_missing")
    identity = {
        str(key): value
        for key, value in raw.items()
        if key != "content_sha256"
    }
    if str(raw.get("content_sha256") or "") != content_sha256(identity):
        raise ContractError("official_nwp_scheduler_aifs_oos_binding_sha_mismatch")
    lineage = raw.get("provider_lineage")
    expected_lineage = {
        "publication_authority": "official_native",
        "independence_family_id": "ECMWF",
        "same_independence_family_as_ifs": True,
        "counts_as_distinct_independence_family": False,
    }
    if (
        raw.get("schema") != AIFS_PROVIDER_OOS_BINDING_SCHEMA
        or raw.get("provider_id") != "ecmwf_aifs025_official"
        or raw.get("model_id") != "ECMWF"
        or raw.get("independence_family_id") != "ECMWF"
        or raw.get("strict_oos_state") != "FORMAL_P0_READY"
        or raw.get("formal_p0_eligible") is not True
        or raw.get("native_arrival_index_eligible") is not True
        or not isinstance(raw.get("strict_oos_artifact_sha256"), str)
        or len(str(raw["strict_oos_artifact_sha256"])) != 64
        or not isinstance(lineage, Mapping)
        or dict(lineage) != expected_lineage
        or raw.get("activation_id") != activation.activation_id
        or raw.get("activation_sha256") != activation.content_sha256
        or raw.get("ensemble_artifact_id") != ensemble.ensemble_artifact_id
        or raw.get("ensemble_artifact_sha256") != ensemble.content_sha256
    ):
        raise ContractError("official_nwp_scheduler_aifs_oos_binding_invalid")


def capture_scope(
    *,
    registry_snapshot_path: Path,
    scope: Mapping[str, Any],
    as_of_utc: str,
    output_root: Path,
    activation_bindings_by_run: Mapping[str, Mapping[str, Any]],
    run_initializations: Sequence[str] | None = None,
    provider_runner: Callable[..., dict[str, Any]] = _default_provider_runner,
) -> dict[str, Any]:
    """Prefetch the exact OOS-selected provider mask for one mature run.

    Provider arrival order never chooses a P0 combination.  Each run carries
    a content-addressed activation/artifact binding; an incomplete selected
    mask falls back as a whole to the next mature run.
    """

    registry = load_market_registry_snapshot(registry_snapshot_path)
    event_date = str(scope.get("event_date") or "")
    timezone_name = str(scope.get("settlement_timezone") or "")
    station_ids = [
        str(value).strip().upper()
        for value in scope.get("station_ids") or []
        if str(value).strip()
    ]
    if not event_date or not timezone_name or not station_ids:
        raise ContractError("official_nwp_scheduler_scope_invalid")
    destination = (
        output_root.expanduser().resolve()
        / event_date
        / timezone_name.replace("/", "_")
        / "-".join(station_ids)
    )
    attempts: list[dict[str, Any]] = []
    all_paths: list[Path] = []
    selected_run = ""
    runs = list(run_initializations or candidate_run_initializations(
        as_of_utc=as_of_utc,
        event_date=event_date,
        settlement_timezone=timezone_name,
    ))
    if not runs or len(runs) > MAX_RUN_FALLBACKS:
        raise ContractError("official_nwp_scheduler_run_candidate_count_invalid")
    for run in runs:
        assert_process_memory_budget()
        raw_binding = activation_bindings_by_run.get(run)
        if not isinstance(raw_binding, Mapping):
            raise ContractError(
                "official_nwp_scheduler_activation_binding_missing"
            )
        binding = raw_binding.get("activation_binding")
        raw_activation = raw_binding.get("ensemble_activation")
        raw_ensemble = raw_binding.get("ensemble_artifact")
        if (
            not isinstance(binding, Mapping)
            or not isinstance(raw_activation, Mapping)
            or not isinstance(raw_ensemble, Mapping)
        ):
            raise ContractError(
                "official_nwp_scheduler_activation_binding_invalid"
            )
        binding_identity = {
            str(key): value
            for key, value in binding.items()
            if key != "binding_sha256"
        }
        if (
            str(binding.get("binding_sha256") or "")
            != content_sha256(binding_identity)
        ):
            raise ContractError(
                "official_nwp_scheduler_activation_binding_sha_mismatch"
            )
        activation_identity = {
            str(key): value
            for key, value in raw_activation.items()
            if key != "content_sha256"
        }
        if (
            str(raw_activation.get("content_sha256") or "")
            != content_sha256(activation_identity)
        ):
            raise ContractError(
                "official_nwp_scheduler_activation_sha_mismatch"
            )
        activation = CalibrationActivation.from_mapping(raw_activation)
        ensemble = ModelEnsembleArtifact.from_mapping(raw_ensemble)
        validate_formal_ensemble_artifact(ensemble)
        parsed_run = _parse_utc(run)
        expected_run_cycle = f"{parsed_run.hour:02d}Z"
        expected_lead_bucket = lead_bucket_for_maturity(
            maturity_key_for_model_run(
                run,
                event_date,
                target_timezone=timezone_name,
            )
        )
        expected_season_bin = season_bin_for_event_date(
            event_date,
            latitude=float(
                station_coordinates(station_ids[0])["latitude"]
            ),
        )
        models = tuple(sorted({
            str(model).strip().upper()
            for model in binding.get("selected_models") or []
            if str(model).strip()
        }))
        if (
            models != tuple(ensemble.candidate_models)
            or len(models) < 2
            or any(model not in MODEL_PROVIDER_IDS for model in models)
            or activation.artifact_kind != "MODEL_ENSEMBLE"
            or activation.artifact_id != ensemble.ensemble_artifact_id
            or activation.available_model_set_hash_or_star
            != model_set_hash(models)
            or activation.authority_scope != "PRODUCTION_NO_LIVE_ONLY"
            or (
                activation.station_id,
                activation.season_bin,
                activation.lead_bucket,
                activation.run_cycle,
                activation.settlement_definition,
            )
            != (
                station_ids[0],
                expected_season_bin,
                expected_lead_bucket,
                expected_run_cycle,
                SETTLEMENT_DEFINITION,
            )
            or (
                ensemble.station_id,
                ensemble.season_bin,
                ensemble.lead_bucket,
                ensemble.run_cycle,
                ensemble.settlement_definition,
            )
            != (
                station_ids[0],
                expected_season_bin,
                expected_lead_bucket,
                expected_run_cycle,
                SETTLEMENT_DEFINITION,
            )
            or not timestamp_lte(activation.activated_at_utc, as_of_utc)
            or not timestamp_in_window(
                as_of_utc,
                activation.valid_from_utc,
                activation.valid_until_utc,
            )
            or binding.get("ensemble_artifact_id")
            != ensemble.ensemble_artifact_id
            or binding.get("ensemble_artifact_sha256")
            != ensemble.content_sha256
            or binding.get("selection_evidence_sha256")
            != ensemble.selection_evidence_sha256
            or binding.get("activation_id") != activation.activation_id
            or binding.get("activation_generation") != activation.generation
            or binding.get("activation_sha256") != activation.content_sha256
            or binding.get("authority_scope") != activation.authority_scope
        ):
            raise ContractError(
                "official_nwp_scheduler_activation_scope_mismatch"
            )
        raw_selected_provider_by_model = binding.get(
            "selected_provider_by_model"
        )
        if raw_selected_provider_by_model is None:
            selected_provider_by_model = {
                model: MODEL_PROVIDER_IDS[model]
                for model in models
            }
        elif isinstance(raw_selected_provider_by_model, Mapping):
            selected_provider_by_model = {
                str(model).strip().upper(): str(provider).strip().lower()
                for model, provider in raw_selected_provider_by_model.items()
            }
            if set(selected_provider_by_model) != set(models):
                raise ContractError(
                    "official_nwp_scheduler_selected_provider_scope_mismatch"
                )
        else:
            raise ContractError(
                "official_nwp_scheduler_selected_provider_scope_mismatch"
            )
        for model, provider in selected_provider_by_model.items():
            allowed = (
                set(ECMWF_OFFICIAL_CAPTURE_FALLBACKS)
                if model == "ECMWF"
                else {MODEL_PROVIDER_IDS[model]}
            )
            if provider not in allowed:
                raise ContractError(
                    "official_nwp_scheduler_selected_provider_not_authorized"
                )
        if (
            selected_provider_by_model.get("ECMWF")
            == "ecmwf_aifs025_official"
        ):
            _require_aifs_provider_oos_binding(
                binding,
                activation=activation,
                ensemble=ensemble,
            )
        selected_providers = tuple(
            selected_provider_by_model[model] for model in models
        )
        run_dir = destination / run.replace("-", "").replace(":", "")
        contract = build_model_run_contract(
            target_local_date=event_date,
            run_initialization_utc=run,
            station_ids=station_ids,
            target_timezone=timezone_name,
            market_registry_snapshot_sha256=str(registry["snapshot_sha256"]),
        )
        contract_path = run_dir / "model_run_contract.json"
        write_bounded_json_if_changed(contract_path, contract)
        provider_rows: list[dict[str, Any]] = []
        complete_count = 0
        complete_providers: set[str] = set()
        for model in models:
            provider = selected_provider_by_model[model]
            role_capture = _capture_model_role(
                role_provider_id=provider,
                model_id=model,
                provider_runner=provider_runner,
                event_date=event_date,
                run_initialization_utc=run,
                as_of_utc=as_of_utc,
                settlement_timezone=timezone_name,
                station_ids=station_ids,
                registry_snapshot_sha256=str(registry["snapshot_sha256"]),
                contract_path=contract_path,
                run_dir=run_dir,
            )
            complete = bool(role_capture["complete"])
            complete_count += int(complete)
            if complete:
                complete_providers.add(provider)
                # Exactly one binding-selected provider owns each model role.
                all_paths.extend(role_capture["paths"])
            provider_rows.append({
                "model_id": model,
                "provider_id": provider,
                "state": "COMPLETE" if complete else "BLOCKED_FAIL_CLOSED",
                "capture_path_count": len(role_capture["paths"]),
                "blocked_count": 0 if complete else 1,
                "selected_provider_id": role_capture["selected_provider_id"],
                "attempts": role_capture["attempts"],
            })
        selected_ready = set(selected_providers) <= complete_providers
        attempts.append({
            "run_initialization_utc": run,
            "complete_provider_count": complete_count,
            "selected_models": list(models),
            "activation_generation": activation.generation,
            "ensemble_artifact_sha256": ensemble.content_sha256,
            "selection_evidence_sha256": (
                ensemble.selection_evidence_sha256
            ),
            "selected_mask_ready": selected_ready,
            "selected_provider_by_model": {
                row["model_id"]: row["selected_provider_id"]
                for row in provider_rows
                if row["selected_provider_id"]
            },
            "providers": provider_rows,
        })
        if selected_ready:
            selected_run = run
            break
    unique_paths = sorted(set(all_paths))
    if len(unique_paths) > MAX_INPUT_FILES_PER_BATCH:
        raise ContractError("official_nwp_scheduler_capture_batch_exceeded")
    return {
        "schema": SCHEMA,
        "state": "CAPTURED" if selected_run else "BLOCKED_FAIL_CLOSED",
        "event_date": event_date,
        "settlement_timezone": timezone_name,
        "station_ids": station_ids,
        "selected_run_initialization_utc": selected_run,
        "capture_paths": [str(path) for path in unique_paths],
        "capture_path_count": len(unique_paths),
        "attempts": attempts,
        "writes_target_database": False,
        "calls_order_gateway": False,
        "places_orders": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }


__all__ = [
    "CANDIDATE_PREFETCH_SCHEMA",
    "MAX_CANDIDATE_MODELS",
    "MAX_RUN_FALLBACKS",
    "MAX_DISCOVERED_SCOPES",
    "MAX_SCOPES_PER_CYCLE",
    "MODEL_RUN_HOURS",
    "MODEL_PROVIDER_IDS",
    "PROVIDERS",
    "SCHEMA",
    "candidate_model_batch_for_station",
    "candidate_run_initializations",
    "capture_scope",
    "prefetch_candidate_raw_training",
    "registry_capture_scopes",
]
