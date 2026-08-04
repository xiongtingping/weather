"""Read-only adapter from existing weather/market snapshots to a strategy plan.

The adapter consumes already-materialized snapshots only.  It performs no
network access, database writes, market refresh, or order submission.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from weather_basket_yes_runtime import parse_bucket_interval

from .runtime import build_basket_yes_plan


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _json_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _book_asks(book: Any) -> list[dict[str, float]]:
    book = _mapping(book)
    result: list[dict[str, float]] = []
    for raw in _list(book.get("asks")):
        raw = _mapping(raw)
        try:
            price, size = float(raw.get("price")), float(raw.get("size"))
        except (TypeError, ValueError):
            continue
        if 0.0 < price < 1.0 and size > 0.0:
            result.append({"price": price, "size": size})
    return sorted(result, key=lambda row: row["price"])


def _quote_by_token(snapshot: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    values = (
        snapshot.get("quote_snapshots")
        or snapshot.get("market_quotes")
        or snapshot.get("orderbook_snapshots")
        or []
    )
    result: dict[str, Mapping[str, Any]] = {}
    if isinstance(values, Mapping):
        values = [dict(value, token=token) if isinstance(value, Mapping) else {"token": token} for token, value in values.items()]
    for raw in _list(values):
        row = _mapping(raw)
        token = str(row.get("token") or row.get("token_id") or row.get("asset") or "").strip()
        if token:
            result[token] = row
    return result


def _bucket_c(row: Mapping[str, Any]) -> int | None:
    for key in ("bucket_c", "temperature_c", "bucket_value_c"):
        try:
            value = float(row.get(key))
        except (TypeError, ValueError):
            continue
        if value.is_integer():
            return int(value)
    outcome = str(row.get("outcome") or row.get("label") or row.get("bucket") or "").strip()
    try:
        interval = parse_bucket_interval(outcome)
    except ValueError:
        return None
    if interval.get("kind") == "single" and interval.get("lower") is not None:
        return int(round(float(interval["lower"])))
    return None


def _market_rows(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_rows = snapshot.get("market_registry") or snapshot.get("registry_rows") or snapshot.get("buckets") or []
    quote_map = _quote_by_token(snapshot)
    output: list[dict[str, Any]] = []
    for raw in _list(raw_rows):
        raw_mapping = _mapping(raw)
        # Some existing registry snapshots store the canonical row in a
        # JSON-encoded ``registry_json`` field.  Preserve explicit fields on
        # the outer row (for example token IDs) while allowing the canonical
        # registry payload to supply outcome/kind/bucket metadata.
        nested_registry = _json_mapping(raw_mapping.get("registry_json"))
        row = dict(nested_registry or raw_mapping)
        if nested_registry:
            row.update({key: value for key, value in raw_mapping.items() if key != "registry_json"})
        if not row:
            continue
        outcome = str(row.get("outcome") or row.get("label") or row.get("bucket") or row.get("groupItemTitle") or "").strip()
        row["outcome"] = outcome
        bucket_c = _bucket_c(row)
        if bucket_c is not None:
            row["bucket_c"] = bucket_c
        if not row.get("kind") and outcome:
            try:
                row["kind"] = parse_bucket_interval(outcome).get("kind")
            except ValueError:
                pass
        yes_token = str(row.get("yes_token") or "").strip()
        no_token = str(row.get("no_token") or "").strip()
        yes_quote = quote_map.get(yes_token, {})
        no_quote = quote_map.get(no_token, {})
        yes_asks = _book_asks(row.get("yes_order_book") or row.get("yes_book") or yes_quote.get("order_book") or yes_quote.get("book"))
        no_asks = _book_asks(row.get("no_order_book") or row.get("no_book") or no_quote.get("order_book") or no_quote.get("book"))
        if yes_asks:
            row["yes_ask"] = yes_asks[0]["price"]
            row["yes_ask_size"] = yes_asks[0]["size"]
        if no_asks:
            row["no_asks"] = no_asks
        output.append(row)
    return output


def _regime(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    direct = snapshot.get("regime")
    if isinstance(direct, Mapping):
        return dict(direct)
    result: dict[str, Any] = {}
    feature_surface = snapshot.get("features")
    if not isinstance(feature_surface, Mapping):
        feature_surface = snapshot.get("weather_likelihood_features")
    for surface in (snapshot, feature_surface if isinstance(feature_surface, Mapping) else {}):
        for key in (
            "official_nwp_physical_regime",
            "physical_regime",
            "climo_regime",
            "basin_heat_mechanism_model",
            "forecast_background_context",
        ):
            value = surface.get(key)
            if isinstance(value, Mapping):
                result[key] = dict(value)
        for key in (
            "forecast_weather_regime",
            "target_day_regime",
            "regime_direction",
            "direction_state",
            "regime_probability_vector",
            "forecast_regime_probabilities",
            "basin_regime",
            "basin_regime_confidence",
            "cloud_regime",
            "precipitation_regime",
            "convection_regime",
            "primary_regime_label",
            "subtropical_high_regime",
            "monsoon_humidity_regime",
            "basin_ventilation_regime",
            "basin_heat_mechanism_state",
            "basin_heat_mechanism_status",
        ):
            if surface.get(key) is not None:
                result[key] = surface.get(key)
    return result


def _probability_projection(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only existing P0/P1/posterior projection surfaces."""

    result: dict[str, Any] = {}
    for key in (
        "p0",
        "P0",
        "p1",
        "P1",
        "latest_p0",
        "latest_p1",
        "initial_p0",
        "posterior_p0",
        "posterior_p1",
        "strong_posterior",
        "posterior_checkpoint",
        "checkpoint",
        "realization_state",
        "probability_stage",
        "posterior_stage",
        "market_bucket_probabilities",
        "posterior_market_bucket_probabilities",
        "resolution_bridge",
        "market_projection",
    ):
        if snapshot.get(key) is not None:
            result[key] = snapshot.get(key)
    for container_key in ("probability_projection", "posterior_projection", "posterior"):
        value = snapshot.get(container_key)
        if isinstance(value, Mapping):
            result.update(dict(value))
    return result


def build_plan_from_weather_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Transform existing snapshot surfaces into pure-YES plan input."""

    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    event = dict(_mapping(snapshot.get("event")))
    for key in (
        "event_id",
        "slug",
        "event_date",
        "target_event_date",
        "market_unit",
        "market_anchor_c",
        "market_implied_mean_c",
        "station_id",
        "settlement_timezone",
    ):
        if event.get(key) is None and snapshot.get(key) is not None:
            event[key] = snapshot.get(key)
    if event.get("market_anchor_c") is None:
        event["market_anchor_c"] = snapshot.get("market_implied_mean_c") or snapshot.get("market_anchor_bucket_c")
    features = snapshot.get("features")
    if not isinstance(features, Mapping):
        features = snapshot.get("weather_likelihood_features")
    if not isinstance(features, Mapping):
        features = {}
    source_observations = snapshot.get("source_observations") or snapshot.get("forecast_source_observations") or []
    report = snapshot.get("report") or snapshot.get("reported_trajectory_observation")
    return build_basket_yes_plan(
        {
            "as_of": snapshot.get("as_of") or snapshot.get("snapshot_at") or event.get("as_of"),
            "event": event,
            "buckets": _market_rows(snapshot),
            "features": dict(features),
            "source_observations": source_observations if isinstance(source_observations, Sequence) else [],
            "probability_projection": _probability_projection(snapshot),
            "regime": _regime(snapshot),
            "report": report if isinstance(report, Mapping) else None,
            "config": snapshot.get("basket_strategy_config") or snapshot.get("config") or {},
        }
    )


__all__ = ["build_plan_from_weather_snapshot"]
