"""Bounded Open-Meteo ensemble candidate capture.

Open-Meteo transports upstream ensemble members but is an
``aggregator_republisher``.  Packets from this module can seed member buckets
and OOS candidates; they never claim native run-init/publication timestamps.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlencode

try:
    import httpx
except ModuleNotFoundError:  # pragma: no cover - service bootstrap runtime
    httpx = None

from .contracts import ContractError, content_sha256
from .native_ensemble_contract import native_ensemble_family_set_hash
from .p0_producer import bucket_intervals_from_registry_rows


SCHEMA = "open_meteo_native_ensemble_forward_packet_v1"
API_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
VARIABLE = "temperature_2m_max"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
# Statuses that mean "later", not "no".  429 is the one that matters here: a
# single request asks for every registered ensemble model, so it costs many
# call units and the provider's limit is reachable in normal operation.
RETRYABLE_HTTP_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
MAX_TRANSPORT_ATTEMPTS = 3
# Bounded, and deliberately longer than a socket retry: the point of waiting on
# a rate limit is to stop consuming the quota that is refusing us.  The cap
# keeps one scope from holding a rotation slot indefinitely -- a scope that
# cannot be fetched now is retried on the next sweep, not held open here.
RETRY_BACKOFF_BASE_SECONDS = 2.0
MAX_RETRY_BACKOFF_SECONDS = 8.0
# A packet produced here is a live/forward transport observation, never a
# retrospective strict-as-of sample.  Historical Open-Meteo OOS has a
# separate append-only contract bound to closed AWC truth.
OBSERVATIONAL_EVIDENCE_ROLE = "observational_candidate_not_oos"
FORWARD_OOS_TEMPORAL_SCOPE = OBSERVATIONAL_EVIDENCE_ROLE
ENSEMBLE_SYSTEMS: dict[str, dict[str, Any]] = {
    "ECMWF_IFS_ENS_GLOBAL": {
        "model_scope": "global",
        "canonical_name": "ECMWF IFS 0.25 Ensemble",
        "model_scope": "global",
        "provider_id": "ECMWF",
        "independence_family_id": "ECMWF",
        "api_model": "ecmwf_ifs025_ensemble",
        "expected_member_count": 51,
    },
    "ECMWF_IFS_ENS_EUROPE": {
        "model_scope": "regional",
        "canonical_name": "ECMWF IFS Europe Ensemble",
        "provider_id": "ECMWF",
        "independence_family_id": "ECMWF",
        "api_model": "ecmwf_ifs_europe_ensemble",
        "expected_member_count": 51,
    },
    "ECMWF_AIFS_ENS_GLOBAL": {
        "model_scope": "global",
        "canonical_name": "ECMWF AIFS 0.25 Ensemble",
        "model_scope": "global",
        "provider_id": "ECMWF",
        "independence_family_id": "ECMWF",
        "api_model": "ecmwf_aifs025_ensemble",
        "expected_member_count": 51,
        # AIFS is a distinct ECMWF product but not a second independent
        # ECMWF family.  This local integration only has the Open-Meteo
        # transport receipt; it has no official AIFS native-arrival proof.
        "candidate_only": True,
        "formal_p0_eligible": False,
        "native_arrival_index_eligible": False,
        "official_native_arrival_status": "MISSING_FAIL_CLOSED",
    },
    "ECMWF_AIFS_ENS_EUROPE": {
        "model_scope": "regional",
        "canonical_name": "ECMWF AIFS Europe Ensemble",
        "model_scope": "regional",
        "provider_id": "ECMWF",
        "independence_family_id": "ECMWF",
        "api_model": "ecmwf_aifs_europe_ensemble",
        "expected_member_count": 51,
        "candidate_only": True,
        "formal_p0_eligible": False,
        "native_arrival_index_eligible": False,
        "official_native_arrival_status": "MISSING_FAIL_CLOSED",
    },
    "NOAA_GEFS_SEAMLESS": {
        "model_scope": "global",
        "canonical_name": "NOAA GEFS Seamless",
        "provider_id": "NOAA",
        "independence_family_id": "NOAA_GEFS",
        "api_model": "ncep_gefs_seamless",
        "expected_member_count": 31,
    },
    "NOAA_GEFS_025": {
        "model_scope": "global",
        "canonical_name": "NOAA GEFS 0.25",
        "provider_id": "NOAA",
        "independence_family_id": "NOAA_GEFS",
        "api_model": "ncep_gefs025",
        "expected_member_count": 31,
    },
    "NOAA_GEFS_05": {
        "model_scope": "global",
        "canonical_name": "NOAA GEFS 0.5",
        "provider_id": "NOAA",
        "independence_family_id": "NOAA_GEFS",
        "api_model": "ncep_gefs05",
        "expected_member_count": 31,
    },
    "NOAA_AIGEFS_025": {
        "model_scope": "global",
        "canonical_name": "NOAA AIGEFS 0.25",
        "provider_id": "NOAA",
        "independence_family_id": "NOAA_AIGEFS",
        "api_model": "ncep_aigefs025",
        "expected_member_count": 31,
    },
    "DWD_ICON_EPS_SEAMLESS": {
        "model_scope": "global",
        "canonical_name": "DWD ICON EPS Seamless",
        "model_scope": "global",
        "provider_id": "DWD",
        "independence_family_id": "DWD_ICON_EPS",
        "api_model": "icon_seamless_eps",
        "expected_member_count": 40,
    },
    "DWD_ICON_GLOBAL_EPS": {
        "model_scope": "global",
        "canonical_name": "DWD ICON Global EPS",
        "model_scope": "global",
        "provider_id": "DWD",
        "independence_family_id": "DWD_ICON_EPS",
        "api_model": "icon_global_eps",
        "expected_member_count": 40,
    },
    "DWD_ICON_EU_EPS": {
        "model_scope": "regional",
        "canonical_name": "DWD ICON EU EPS",
        "model_scope": "regional",
        "provider_id": "DWD",
        "independence_family_id": "DWD_ICON_EPS",
        "api_model": "icon_eu_eps",
        "expected_member_count": 40,
    },
    "DWD_ICON_D2_EPS": {
        "model_scope": "regional",
        "canonical_name": "DWD ICON D2 EPS",
        "model_scope": "regional",
        "provider_id": "DWD",
        "independence_family_id": "DWD_ICON_EPS",
        "api_model": "icon_d2_eps",
        "expected_member_count": 20,
    },
    "ECCC_GEM_GLOBAL_ENS": {
        "model_scope": "global",
        "canonical_name": "ECCC GEM Global Ensemble",
        "provider_id": "ECCC",
        "independence_family_id": "ECCC_GEM",
        "api_model": "gem_global_ensemble",
        "expected_member_count": 21,
    },
    "UKMO_GLOBAL_ENS": {
        "model_scope": "global",
        "canonical_name": "UKMO Global Ensemble 20km",
        "provider_id": "UKMO",
        "independence_family_id": "UKMO",
        "api_model": "ukmo_global_ensemble_20km",
        "expected_member_count": 18,
    },
    "UKMO_UK_ENS": {
        "model_scope": "regional",
        "canonical_name": "UKMO UK Ensemble 2km",
        "provider_id": "UKMO",
        "independence_family_id": "UKMO",
        "api_model": "ukmo_uk_ensemble_2km",
        "expected_member_count": 3,
    },
    "METEOSWISS_ICON_CH1_ENS": {
        "model_scope": "regional",
        "canonical_name": "MeteoSwiss ICON CH1 Ensemble",
        "provider_id": "METEOSWISS",
        "independence_family_id": "METEOSWISS_ICON",
        "api_model": "meteoswiss_icon_ch1_ensemble",
        "expected_member_count": 11,
    },
    "METEOSWISS_ICON_CH2_ENS": {
        "model_scope": "regional",
        "canonical_name": "MeteoSwiss ICON CH2 Ensemble",
        "provider_id": "METEOSWISS",
        "independence_family_id": "METEOSWISS_ICON",
        "api_model": "meteoswiss_icon_ch2_ensemble",
        "expected_member_count": 21,
    },
    "GOOGLE_WEATHERNEXT2_ENS": {
        "model_scope": "global",
        "canonical_name": "Google WeatherNext 2 Ensemble",
        "provider_id": "GOOGLE",
        "independence_family_id": "GOOGLE_WEATHERNEXT",
        "api_model": "google_weathernext2_ensemble",
        "expected_member_count": 64,
    },
}


# Response headers a provider uses to describe its own limit.  Recorded rather
# than interpreted: the point of capturing them is to stop guessing which
# window is being hit.
RATE_LIMIT_HEADER_PREFIXES = ("x-ratelimit", "ratelimit", "retry-after")
MAX_RATE_LIMIT_DIAGNOSTIC_CHARS = 300


def rate_limit_diagnostics(response: Any) -> dict[str, str]:
    """What the provider said about its own limit, bounded and never raising.

    A 429 that reports only its status code cannot be acted on: reducing model
    count, slowing the sweep and raising the quota are three different fixes,
    and which one applies depends on whether the minutely, hourly or daily
    window was exceeded.  Open-Meteo says exactly that in the response body --
    "Minutely API request limit exceeded", and so on -- and that body was being
    discarded.

    Diagnostics only.  Nothing downstream branches on these; they exist so an
    operator reading a receipt can tell which limit was reached.
    """

    result: dict[str, str] = {}
    try:
        headers = getattr(response, "headers", None) or {}
        for key, value in dict(headers).items():
            name = str(key).lower()
            if name.startswith(RATE_LIMIT_HEADER_PREFIXES):
                result[name] = str(value)[:MAX_RATE_LIMIT_DIAGNOSTIC_CHARS]
    except Exception:
        pass
    try:
        # A streamed response has not been read at the point raise_for_status
        # fires, so the body has to be pulled explicitly before it can be
        # inspected.
        body = getattr(response, "content", None)
        if not body and hasattr(response, "read"):
            body = response.read()
        text = (body or b"").decode("utf-8", errors="replace")
        try:
            decoded = json.loads(text)
            reason = decoded.get("reason") if isinstance(decoded, Mapping) else None
        except (TypeError, ValueError, json.JSONDecodeError):
            reason = None
        result["reason"] = str(
            reason if reason else text.strip()
        )[:MAX_RATE_LIMIT_DIAGNOSTIC_CHARS]
    except Exception:
        pass
    return {key: value for key, value in result.items() if value}


def _retry_after_seconds(response: Any, *, attempt: int) -> float:
    """How long to wait before retrying, preferring what the server asked for.

    ``Retry-After`` is the provider telling us exactly when its window reopens;
    guessing shorter just spends another unit of quota on a refusal.  Only when
    the header is absent or unparseable does this fall back to exponential
    backoff.
    """

    raw = ""
    try:
        raw = str(response.headers.get("Retry-After") or "").strip()
    except Exception:
        raw = ""
    if raw:
        try:
            return max(0.0, min(float(raw), MAX_RETRY_BACKOFF_SECONDS))
        except ValueError:
            # An HTTP-date form is valid too, but parsing it buys nothing here:
            # the backoff below is already bounded and this path is rare.
            pass
    return min(
        RETRY_BACKOFF_BASE_SECONDS * (2 ** attempt),
        MAX_RETRY_BACKOFF_SECONDS,
    )


# Systems whose domain is the whole globe, and therefore the set worth asking
# for at an arbitrary station.
#
# The provider prices a request by (variables x models x days), so asking for
# all eighteen registered systems costs roughly eighteen units per station-day
# rather than one.  Seven of those are regional: measured across 45 stations of
# retained history, ICON-EU returned members for 9 stations, ICON-D2 for 5,
# UKMO-UK for 4, the two MeteoSwiss models for 3 each, and the two ECMWF Europe
# models for none at all.  Everywhere else they were billed and then discarded
# into ``unavailable_products``.
#
# Dropping them from the request is a 39% cut in units per capture with no
# effect on any station they do not cover.  It does narrow the candidate set
# for the handful of European stations they do cover -- these are republisher
# candidates, every one ``formal_p0_eligible: false``, so this bounds what the
# aggregator lane offers those stations as OOS candidates rather than changing
# any formal P0 authority.  Re-admitting them per station is the better answer
# and needs a station-to-domain map this module does not have; the scope
# metadata above is what such a map would key on.
GLOBAL_ENSEMBLE_SYSTEMS = {
    system_id: config
    for system_id, config in ENSEMBLE_SYSTEMS.items()
    if str(config.get("model_scope") or "global") == "global"
}


def _bounded_get_json(
    url: str,
    *,
    timeout_seconds: float,
) -> tuple[dict[str, Any], str]:
    """Fetch one bounded JSON body, retrying only what is worth retrying.

    429 used to be the one failure this loop could not survive.  It is raised
    by ``raise_for_status`` as an ``HTTPStatusError``, and only transport and
    timeout errors were caught, so a rate-limited scope failed on its first
    attempt with no wait -- while genuine transport errors retried three times
    back to back with no delay at all, which against a rate limiter spends
    quota faster rather than slower.

    Each request here asks for every registered ensemble model at once, so it
    costs the provider far more than one call unit.  Widening the rotation
    catalogue multiplied that, which is how the limit came to be reached in the
    first place; retrying without a wait is what turned reaching it into a
    failed scope.
    """

    if httpx is None:
        raise ContractError(
            "open_meteo_ensemble_httpx_dependency_missing"
        )
    last_error: Exception | None = None
    raw = b""
    for _attempt in range(MAX_TRANSPORT_ATTEMPTS):
        try:
            with httpx.Client(
                timeout=timeout_seconds,
                headers={"User-Agent": "weather-native-ensemble-resident/1"},
                follow_redirects=False,
            ) as client:
                with client.stream("GET", url) as response:
                    response.raise_for_status()
                    length = response.headers.get("Content-Length")
                    if length and int(length) > MAX_RESPONSE_BYTES:
                        raise ContractError(
                            "open_meteo_ensemble_response_too_large"
                        )
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise ContractError(
                                "open_meteo_ensemble_response_too_large"
                            )
                        chunks.append(chunk)
                    raw = b"".join(chunks)
            break
        except httpx.HTTPStatusError as exc:
            status = int(exc.response.status_code)
            if status not in RETRYABLE_HTTP_STATUS_CODES:
                # A 400 will be a 400 next time; only the statuses that mean
                # "later" are worth another unit of quota.
                raise
            last_error = exc
            # No wait after the final attempt: the scope is given up either
            # way, and sleeping first only holds a rotation slot open for a
            # retry that will not happen.  It is retried on the next sweep.
            if _attempt < MAX_TRANSPORT_ATTEMPTS - 1:
                time.sleep(
                    _retry_after_seconds(exc.response, attempt=_attempt)
                )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last_error = exc
            if _attempt < MAX_TRANSPORT_ATTEMPTS - 1:
                time.sleep(
                    min(
                        RETRY_BACKOFF_BASE_SECONDS * (2 ** _attempt),
                        MAX_RETRY_BACKOFF_SECONDS,
                    )
                )
    else:
        if isinstance(last_error, httpx.HTTPStatusError):
            diagnostics = rate_limit_diagnostics(last_error.response)
            detail = diagnostics.get("reason") or ""
            raise ContractError(
                "open_meteo_ensemble_rate_limited:"
                + str(last_error.response.status_code)
                + (f":{detail}" if detail else "")
            ) from last_error
        raise ContractError(
            "open_meteo_ensemble_transport_failed:"
            + type(last_error).__name__
        ) from last_error
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(
            "open_meteo_ensemble_response_invalid"
        ) from exc
    if not isinstance(payload, dict) or payload.get("error"):
        raise ContractError(
            "open_meteo_ensemble_api_error:"
            + str(payload.get("reason") if isinstance(payload, dict) else "")
        )
    return payload, hashlib.sha256(raw).hexdigest()


def _extract_target_members(
    payload: Mapping[str, Any],
    *,
    event_date: str,
    expected_count: int,
    ensemble_system_id: str,
    api_model: str,
) -> list[dict[str, Any]]:
    daily = payload.get("daily")
    if not isinstance(daily, Mapping):
        raise ContractError("open_meteo_ensemble_daily_missing")
    dates = daily.get("time")
    if not isinstance(dates, list) or event_date not in dates:
        raise ContractError("open_meteo_ensemble_target_date_missing")
    date_index = dates.index(event_date)
    control_key = f"{VARIABLE}_{api_model}"
    if control_key not in daily:
        control_key = VARIABLE
    keys = [control_key] + [
        (
            f"{VARIABLE}_member{index:02d}_{api_model}"
            if f"{VARIABLE}_member{index:02d}_{api_model}" in daily
            else f"{VARIABLE}_member{index:02d}"
        )
        for index in range(1, expected_count)
    ]
    members: list[dict[str, Any]] = []
    for index, key in enumerate(keys):
        values = daily.get(key)
        if (
            not isinstance(values, list)
            or date_index >= len(values)
            or values[date_index] is None
        ):
            raise ContractError(
                "open_meteo_ensemble_member_set_incomplete"
            )
        try:
            value = float(values[date_index])
        except (TypeError, ValueError) as exc:
            raise ContractError(
                "open_meteo_ensemble_member_value_invalid"
            ) from exc
        if not math.isfinite(value):
            raise ContractError(
                "open_meteo_ensemble_member_value_invalid"
            )
        members.append({
            "ensemble_system_id": ensemble_system_id,
            "member_id": (
                "control" if index == 0 else f"perturbed_{index:02d}"
            ),
            "member_role": "CONTROL" if index == 0 else "PERTURBED",
            "raw_tmax_c": value,
        })
    return members


def _member_bucket_probabilities(
    members: Sequence[Mapping[str, Any]],
    intervals: Mapping[str, tuple[float | None, float | None]],
) -> dict[str, float]:
    counts = {str(bucket): 0 for bucket in intervals}
    for member in members:
        value = float(member["raw_tmax_c"])
        matches = [
            str(bucket)
            for bucket, (lower, upper) in intervals.items()
            if (lower is None or value >= float(lower))
            and (upper is None or value < float(upper))
        ]
        if len(matches) != 1:
            raise ContractError(
                "open_meteo_ensemble_bucket_contract_not_exhaustive"
            )
        counts[matches[0]] += 1
    total = len(members)
    return {
        bucket: count / total
        for bucket, count in counts.items()
    }


def capture_scope(
    *,
    station_id: str,
    event_date: str,
    latitude: float,
    longitude: float,
    settlement_timezone: str,
    registry_rows: Sequence[Mapping[str, Any]],
    captured_at_utc: str,
    timeout_seconds: float = 45.0,
) -> dict[str, Any]:
    """Capture every registered product available for one station-day."""

    if (
        not station_id
        or not event_date
        or not settlement_timezone
        or not math.isfinite(float(latitude))
        or not math.isfinite(float(longitude))
    ):
        raise ContractError("open_meteo_ensemble_scope_invalid")
    intervals = bucket_intervals_from_registry_rows(registry_rows)
    frozen_intervals = {
        bucket: [
            None if lower is None else float(lower),
            None if upper is None else float(upper),
        ]
        for bucket, (lower, upper) in intervals.items()
    }
    bucket_contract_sha = content_sha256(frozen_intervals)
    api_models = ",".join(
        str(config["api_model"])
        for config in GLOBAL_ENSEMBLE_SYSTEMS.values()
    )
    query = urlencode({
        "latitude": float(latitude),
        "longitude": float(longitude),
        "daily": VARIABLE,
        "models": api_models,
        "timezone": settlement_timezone,
        "start_date": event_date,
        "end_date": event_date,
    })
    url = f"{API_URL}?{query}"
    payload, response_sha = _bounded_get_json(
        url,
        timeout_seconds=timeout_seconds,
    )
    systems: list[dict[str, Any]] = []
    unavailable_products: list[dict[str, Any]] = []
    # Iterate what was asked for, not everything registered: a system absent
    # from the request would otherwise be reported as an unavailable product
    # rather than as one nobody asked about.
    for system_id, config in GLOBAL_ENSEMBLE_SYSTEMS.items():
        try:
            members = _extract_target_members(
                payload,
                event_date=event_date,
                expected_count=int(config["expected_member_count"]),
                ensemble_system_id=system_id,
                api_model=str(config["api_model"]),
            )
        except ContractError as exc:
            unavailable_products.append({
                "ensemble_system_id": system_id,
                "api_model": config["api_model"],
                "reason": str(exc),
            })
            continue
        member_identity = [
            {
                "member_id": row["member_id"],
                "member_role": row["member_role"],
                "raw_tmax_c": row["raw_tmax_c"],
            }
            for row in members
        ]
        member_set_sha = content_sha256(member_identity)
        probabilities = _member_bucket_probabilities(
            members,
            intervals,
        )
        systems.append({
            "ensemble_system_id": system_id,
            "independence_family_id": config[
                "independence_family_id"
            ],
            "canonical_name": config["canonical_name"],
            "model_scope": config.get("model_scope", "global"),
            "upstream_provider_id": config["provider_id"],
            "transport_provider_id": "OPEN_METEO",
            "api_model": config["api_model"],
            "expected_member_count": config["expected_member_count"],
            "member_count": len(members),
            "members": members,
            "member_set_sha256": member_set_sha,
            "raw_bucket_probabilities": probabilities,
            # Deliberately repeat this authority beside the product, rather
            # than relying on the envelope-wide transport label.  AIFS must
            # not acquire native-arrival or formal-P0 meaning at ingress.
            "candidate_authority": {
                "source_role": "aggregator_republisher",
                "candidate_only": bool(
                    config.get("candidate_only", False)
                ),
                "formal_p0_eligible": bool(
                    config.get("formal_p0_eligible", False)
                ),
                "native_arrival_index_eligible": bool(
                    config.get("native_arrival_index_eligible", False)
                ),
                "official_native_arrival_status": str(
                    config.get(
                        "official_native_arrival_status",
                        "NOT_APPLICABLE",
                    )
                ),
            },
        })
    available_ids = [
        str(system["ensemble_system_id"]) for system in systems
    ]
    available_families = sorted({
        str(system["independence_family_id"])
        for system in systems
    })
    gate = {
        "state": "BLOCKED",
        "reason": "aggregator_republisher_not_formal_native",
        "formal_p0_eligible": False,
        "probability_pathway": (
            "CALIBRATED_NATIVE_MEMBERS_CROSS_ENSEMBLE"
        ),
        "ensemble_system_ids": available_ids,
        "ensemble_system_count": len(available_ids),
        "independence_family_ids": available_families,
        "independent_family_count": len(available_families),
        "available_ensemble_family_set_hash": (
            native_ensemble_family_set_hash(available_families)
            if available_families
            else None
        ),
        "member_count_by_system": {
            str(system["ensemble_system_id"]): int(
                system["member_count"]
            )
            for system in systems
        },
    }
    stable_identity = {
        "schema": SCHEMA,
        "station_id": station_id.strip().upper(),
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "evidence_role": OBSERVATIONAL_EVIDENCE_ROLE,
        "oos_event_temporal_scope": FORWARD_OOS_TEMPORAL_SCOPE,
        "strict_oos_sample_eligible": False,
        "can_substitute_for_formal_promotion": False,
        "latitude": float(latitude),
        "longitude": float(longitude),
        "bucket_contract_sha256": bucket_contract_sha,
        "bucket_intervals": frozen_intervals,
        "systems": [
            {
                key: value
                for key, value in system.items()
                if key not in {"request_url", "response_sha256"}
            }
            for system in systems
        ],
        "cross_model_gate": gate,
        "unavailable_products": unavailable_products,
    }
    packet = {
        **stable_identity,
        "aggregator_snapshot_at_utc": captured_at_utc,
        "systems": systems,
        "request_url": url,
        "response_sha256": response_sha,
        "source_authority": {
            "source_role": "aggregator_republisher",
            "native_run_init_known": False,
            "native_publication_at_known": False,
            "native_arrival_index_eligible": False,
            "formal_p0_eligible": False,
            "production_no_live_calibration_candidate": True,
            "evidence_role": OBSERVATIONAL_EVIDENCE_ROLE,
            "oos_event_temporal_scope": FORWARD_OOS_TEMPORAL_SCOPE,
            "counts_as_strict_oos": False,
            "historical_oos_contract_bound": False,
            "can_substitute_for_formal_promotion": False,
        },
        "snapshot_content_sha256": content_sha256(stable_identity),
    }
    return {
        **packet,
        "packet_content_sha256": content_sha256(packet),
    }


__all__ = [
    "API_URL",
    "ENSEMBLE_SYSTEMS",
    "FORWARD_OOS_TEMPORAL_SCOPE",
    "OBSERVATIONAL_EVIDENCE_ROLE",
    "SCHEMA",
    "capture_scope",
]
