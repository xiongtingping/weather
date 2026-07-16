from __future__ import annotations

import math
import re
import sqlite3
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "temperature_prior_runtime_v1"
EPS = 1e-12
CONTINUOUS_PRIOR_SCHEMA_VERSION = "continuous_temperature_climate_prior_v1"
CONTINUOUS_PRIOR_GRID_STEP_C = 0.1


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_facts_sqlite": False,
        "writes_auto_trade_decisions": False,
        "places_orders": False,
        "restarts_launchd": False,
        "historical_backfill": False,
        "changes_parameters": False,
        "reads_market_prices": False,
        "reads_forecast_sources": False,
        "reads_current_observations": False,
    }


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _date_key(value: Any) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except Exception:
        return ""


def _parse_date(value: Any) -> date | None:
    key = _date_key(value)
    if not key:
        return None
    try:
        return date.fromisoformat(key)
    except Exception:
        return None


def _parse_dt(value: Any) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    if text:
        try:
            dt = datetime.fromisoformat(text)
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _normalize_unit(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in {"C", "°C", "CELSIUS"}:
        return "C"
    if text in {"F", "°F", "FAHRENHEIT"}:
        return "F"
    return text


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _as_bias_c(value: Any) -> float:
    out = _to_float(value)
    return float(out) if out is not None else 0.0


def _bucket_bounds(label: str, rounding_rule: str = "rounded_degrees") -> tuple[float, float] | None:
    raw = str(label or "").strip().replace("–", "-")
    if not raw:
        return None
    num = r"(-?\d+(?:\.\d+)?)"
    rounded = str(rounding_rule or "").strip().lower() in {"", "rounded_degrees", "whole_degrees"}
    match = re.match(rf"^{num}\s*-\s*{num}", raw)
    if not match:
        match = re.match(rf"^{num}\s+to\s+{num}", raw, flags=re.I)
    if match:
        low = min(float(match.group(1)), float(match.group(2)))
        high = max(float(match.group(1)), float(match.group(2)))
        return (low - 0.5, high + 0.5) if rounded else (low, high)
    match = re.match(rf"^{num}\s*(?:°?[CF])?\s+or\s+below$", raw, flags=re.I)
    if match:
        high = float(match.group(1))
        return float("-inf"), high + 0.5 if rounded else high
    match = re.match(rf"^{num}\s*(?:°?[CF])?\s+or\s+(?:above|higher|more)$", raw, flags=re.I)
    if match:
        low = float(match.group(1))
        return low - 0.5 if rounded else low, float("inf")
    match = re.match(rf"^{num}\s*(?:°?[CF])?$", raw, flags=re.I)
    if match:
        center = float(match.group(1))
        return (center - 0.5, center + 0.5) if rounded else (center, center)
    return None


def _bucket_hit(actual_c: float, label: str, rounding_rule: str) -> bool | None:
    bounds = _bucket_bounds(label, rounding_rule=rounding_rule)
    if bounds is None:
        return None
    low, high = bounds
    return bool(float(low) <= float(actual_c) < float(high))


def _normalize(values: Sequence[float]) -> list[float]:
    cleaned = [max(0.0, float(value)) for value in values]
    total = sum(cleaned)
    if total <= EPS:
        return [1.0 / len(cleaned) for _ in cleaned] if cleaned else []
    return [value / total for value in cleaned]


def _round_float(value: float | None, ndigits: int = 12) -> float | None:
    if value is None:
        return None
    return round(float(value), ndigits)


def _day_distance(a: date, b: date) -> int:
    a_day = int(a.strftime("%j"))
    b_day = int(b.strftime("%j"))
    raw = abs(a_day - b_day)
    return min(raw, 366 - raw)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
            (table,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1] or "") for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def _optional_select(columns: set[str], column: str) -> str:
    return column if column in columns else f"NULL AS {column}"


def _query_settled_rows(
    conn: sqlite3.Connection,
    *,
    target_event_date: str,
    as_of: str,
    city: str,
    station_id: str,
    market_var: str,
    market_unit: str,
    history_days: int,
) -> list[dict[str, Any]]:
    target_day = _parse_date(target_event_date)
    if target_day is None:
        return []
    start_day = target_day - timedelta(days=max(1, int(history_days)))
    as_of_dt = _parse_dt(as_of)
    city_norm = _normalize_text(city)
    station_norm = str(station_id or "").strip().upper()
    unit_norm = _normalize_unit(market_unit)
    market_var_text = str(market_var or "").strip()
    seen: set[str] = set()
    out: list[dict[str, Any]] = []

    def include_row(row: Mapping[str, Any], *, source_table: str) -> None:
        event_key = _date_key(row.get("event_date"))
        if not event_key or event_key in seen or event_key >= target_event_date:
            return
        created_at = str(row.get("created_at") or "")
        if created_at:
            created_dt = _parse_dt(created_at)
            if created_dt >= as_of_dt:
                return
        actual = _to_float(row.get("settlement_actual"))
        if actual is None:
            return
        row_unit = _normalize_unit(row.get("market_unit"))
        if unit_norm and row_unit and row_unit != unit_norm:
            return
        row_var = str(row.get("market_var") or "").strip()
        if market_var_text and row_var and row_var != market_var_text:
            return
        row_station = str(row.get("station_id") or "").strip().upper()
        row_city = _normalize_text(row.get("city"))
        identity_text = _normalize_text(
            f"{row.get('city') or ''} {row.get('station_id') or ''} {row.get('slug') or ''} {row.get('title') or ''}"
        )
        station_match = bool(station_norm and row_station and row_station == station_norm)
        city_match = bool(city_norm and city_norm in identity_text)
        if (station_norm or city_norm) and not (station_match or city_match):
            return
        seen.add(event_key)
        out.append(
            {
                "event_date": event_key,
                "actual_c": float(actual),
                "created_at": created_at,
                "source_table": source_table,
                "station_id": row_station,
                "city": row_city,
                "rounding_rule": str(row.get("rounding_rule") or ""),
                "weather_type": _normalize_text(row.get("weather_type")),
                "terrain_class": _normalize_text(row.get("terrain_class")),
                "site_class": _normalize_text(row.get("site_class")),
                "urbanization_class": _normalize_text(row.get("urbanization_class")),
            }
        )

    if _table_exists(conn, "event_daily_temps"):
        daily_columns = _table_columns(conn, "event_daily_temps")
        try:
            rows = conn.execute(
                f"""
                SELECT event_date, settlement_actual, created_at, station_id, city,
                       market_var, market_unit,
                       {_optional_select(daily_columns, "rounding_rule")},
                       {_optional_select(daily_columns, "weather_type")},
                       {_optional_select(daily_columns, "terrain_class")},
                       {_optional_select(daily_columns, "site_class")},
                       {_optional_select(daily_columns, "urbanization_class")},
                       NULL AS slug, NULL AS title
                FROM event_daily_temps
                WHERE event_date >= ?
                  AND event_date < ?
                  AND settlement_actual IS NOT NULL
                ORDER BY event_date DESC, datetime(created_at) DESC, id DESC
                LIMIT 2000
                """,
                (start_day.isoformat(), target_event_date),
            ).fetchall()
        except sqlite3.Error:
            rows = []
        for row in rows:
            include_row(dict(row), source_table="event_daily_temps")

    if _table_exists(conn, "event_runs"):
        run_columns = _table_columns(conn, "event_runs")
        slug_select = "slug" if "slug" in run_columns else "NULL AS slug"
        title_select = "title" if "title" in run_columns else "NULL AS title"
        try:
            rows = conn.execute(
                f"""
                SELECT event_date, settlement_actual, created_at, station_id, city,
                       market_var, market_unit, rounding_rule,
                       {_optional_select(run_columns, "weather_type")},
                       {_optional_select(run_columns, "terrain_class")},
                       {_optional_select(run_columns, "site_class")},
                       {_optional_select(run_columns, "urbanization_class")},
                       {slug_select}, {title_select}
                FROM event_runs
                WHERE event_date >= ?
                  AND event_date < ?
                  AND settlement_actual IS NOT NULL
                ORDER BY event_date DESC, datetime(created_at) DESC, id DESC
                LIMIT 2000
                """,
                (start_day.isoformat(), target_event_date),
            ).fetchall()
        except sqlite3.Error:
            rows = []
        for row in rows:
            include_row(dict(row), source_table="event_runs")
    return out


def _open_readonly(db_path: Any) -> sqlite3.Connection | None:
    if not db_path:
        return None
    path = Path(str(db_path))
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _bucket_stats(
    rows: Sequence[Mapping[str, Any]],
    *,
    labels: Sequence[str],
    rounding_rule: str,
    smoothing: float,
    temperature_bias_c: float,
) -> dict[str, Any]:
    counts = [0.0 for _ in labels]
    matched_samples = 0
    sample_rows: list[dict[str, Any]] = []
    for row in rows:
        actual = _to_float(row.get("actual_c"))
        if actual is None:
            continue
        adjusted_actual = float(actual) + float(temperature_bias_c)
        matched = False
        for idx, label in enumerate(labels):
            hit = _bucket_hit(adjusted_actual, label, rounding_rule or row.get("rounding_rule") or "")
            if hit is None:
                continue
            if hit:
                counts[idx] += 1.0
                matched = True
        if matched:
            matched_samples += 1
            if len(sample_rows) < 20:
                sample = {
                    "event_date": row.get("event_date"),
                    "actual_c": _round_float(actual),
                    "source_table": row.get("source_table"),
                }
                if abs(float(temperature_bias_c)) > EPS:
                    sample["adjusted_actual_c"] = _round_float(adjusted_actual)
                    sample["temperature_bias_c"] = _round_float(float(temperature_bias_c))
                sample_rows.append(sample)
    probs = _normalize([count + max(0.0, float(smoothing)) for count in counts]) if labels else []
    return {
        "counts": counts,
        "matched_samples": matched_samples,
        "probabilities": probs,
        "sample_rows": sample_rows,
    }


def _blend_probabilities(parent: Sequence[float], child: Sequence[float], child_weight: float) -> list[float]:
    if not parent:
        return list(child)
    if not child:
        return list(parent)
    weight = min(1.0, max(0.0, float(child_weight)))
    return _normalize([
        (1.0 - weight) * float(parent_value) + weight * float(child_value)
        for parent_value, child_value in zip(parent, child)
    ])


def _continuous_prior_grid(
    rows: Sequence[Mapping[str, Any]],
    *,
    labels: Sequence[str],
    rounding_rule: str,
    temperature_bias_c: float,
) -> list[float]:
    values = [
        float(actual) + float(temperature_bias_c)
        for actual in (_to_float(row.get("actual_c")) for row in rows)
        if actual is not None
    ]
    lows: list[float] = []
    highs: list[float] = []
    for label in labels:
        bounds = _bucket_bounds(label, rounding_rule=rounding_rule)
        if bounds is None:
            continue
        low, high = bounds
        if math.isfinite(low):
            lows.append(float(low))
        if math.isfinite(high):
            highs.append(float(high))
    low = min([*(values or [20.0]), *(lows or values or [20.0])]) - 5.0
    high = max([*(values or [45.0]), *(highs or values or [45.0])]) + 5.0
    low = math.floor(low / CONTINUOUS_PRIOR_GRID_STEP_C) * CONTINUOUS_PRIOR_GRID_STEP_C
    high = math.ceil(high / CONTINUOUS_PRIOR_GRID_STEP_C) * CONTINUOUS_PRIOR_GRID_STEP_C
    count = min(1000, max(2, int(round((high - low) / CONTINUOUS_PRIOR_GRID_STEP_C)) + 1))
    return [round(low + index * CONTINUOUS_PRIOR_GRID_STEP_C, 6) for index in range(count)]


def _kde_bandwidth(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 1.25
    std = statistics.pstdev(values)
    ordered = sorted(float(value) for value in values)
    q1 = ordered[max(0, int(0.25 * (len(ordered) - 1)))]
    q3 = ordered[min(len(ordered) - 1, int(0.75 * (len(ordered) - 1)))]
    robust = (q3 - q1) / 1.349 if q3 > q1 else std
    scale = min(value for value in (std, robust) if value > EPS) if max(std, robust) > EPS else 1.0
    silverman = 0.9 * scale * (len(values) ** -0.2)
    return _clamp(silverman, 0.45, 2.25)


def _clamp(value: float, low: float, high: float) -> float:
    return max(float(low), min(float(high), float(value)))


def _kde_mass(
    rows: Sequence[Mapping[str, Any]],
    *,
    grid: Sequence[float],
    temperature_bias_c: float,
) -> tuple[list[float], float, int]:
    values = [
        float(actual) + float(temperature_bias_c)
        for actual in (_to_float(row.get("actual_c")) for row in rows)
        if actual is not None
    ]
    if not values:
        return ([1.0 / len(grid) for _ in grid] if grid else []), 1.25, 0
    bandwidth = _kde_bandwidth(values)
    raw = []
    for temp in grid:
        raw.append(
            sum(
                math.exp(-0.5 * ((float(temp) - value) / bandwidth) ** 2)
                for value in values
            )
        )
    return _normalize(raw), bandwidth, len(values)


def _continuous_prior_distribution(
    *,
    rows: Sequence[Mapping[str, Any]],
    window_rows: Sequence[Mapping[str, Any]],
    condition_blends: Sequence[tuple[Sequence[Mapping[str, Any]], float, str]],
    labels: Sequence[str],
    rounding_rule: str,
    temperature_bias_c: float,
    date_window_weight: float,
) -> dict[str, Any]:
    grid = _continuous_prior_grid(
        rows,
        labels=labels,
        rounding_rule=rounding_rule,
        temperature_bias_c=temperature_bias_c,
    )
    broad, broad_bandwidth, broad_count = _kde_mass(
        rows,
        grid=grid,
        temperature_bias_c=temperature_bias_c,
    )
    seasonal, seasonal_bandwidth, seasonal_count = _kde_mass(
        window_rows,
        grid=grid,
        temperature_bias_c=temperature_bias_c,
    )
    mass = _blend_probabilities(broad, seasonal, date_window_weight if seasonal_count else 0.0)
    condition_trace: list[dict[str, Any]] = []
    for condition_rows, weight, name in condition_blends:
        conditional, bandwidth, count = _kde_mass(
            condition_rows,
            grid=grid,
            temperature_bias_c=temperature_bias_c,
        )
        mass = _blend_probabilities(mass, conditional, weight)
        condition_trace.append(
            {
                "condition": name,
                "weight": _round_float(weight),
                "sample_count": count,
                "bandwidth_c": _round_float(bandwidth),
            }
        )
    running = 0.0
    grid_rows: list[dict[str, Any]] = []
    for temp, value in zip(grid, mass):
        running = min(1.0, running + max(0.0, value))
        grid_rows.append(
            {
                "temp_c": _round_float(temp),
                "mass": _round_float(value),
                "pdf": _round_float(value / CONTINUOUS_PRIOR_GRID_STEP_C),
                "cdf": _round_float(running),
            }
        )
    return {
        "schema": CONTINUOUS_PRIOR_SCHEMA_VERSION,
        "target_variable": "settlement_daily_max_c",
        "source": "settled_history_date_station_kde",
        "grid_step_c": CONTINUOUS_PRIOR_GRID_STEP_C,
        "grid": grid_rows,
        "broad_sample_count": broad_count,
        "seasonal_sample_count": seasonal_count,
        "broad_bandwidth_c": _round_float(broad_bandwidth),
        "seasonal_bandwidth_c": _round_float(seasonal_bandwidth),
        "date_window_weight": _round_float(date_window_weight if seasonal_count else 0.0),
        "conditioning_trace": condition_trace,
        "parameters_fitted_from_future_or_current_observations": False,
        "reads_market_prices": False,
    }


def _condition_rows(rows: Sequence[Mapping[str, Any]], key: str, value: Any) -> tuple[list[Mapping[str, Any]], bool]:
    target = _normalize_text(value)
    if not target:
        return [], False
    has_any_value = any(bool(_normalize_text(row.get(key))) for row in rows)
    return [row for row in rows if _normalize_text(row.get(key)) == target], has_any_value


def build_temperature_prior_distribution(
    *,
    buckets: Sequence[Any],
    event_date: str,
    as_of: str = "",
    conn: sqlite3.Connection | None = None,
    db_path: Any = None,
    city: str = "",
    station_id: str = "",
    market_var: str = "temperature_2m_max",
    market_unit: str = "C",
    rounding_rule: str = "rounded_degrees",
    history_days: int = 730,
    date_window_days: int = 15,
    min_samples: int = 3,
    smoothing: float = 0.5,
    weather_type: str = "",
    terrain_class: str = "",
    site_class: str = "",
    urbanization_class: str = "",
    site_temperature_bias_c: Any = 0.0,
    terrain_temperature_bias_c: Any = 0.0,
    urban_heat_island_c: Any = 0.0,
) -> dict[str, Any]:
    labels = [
        str(item.get("bucket") if isinstance(item, Mapping) else item).strip()
        for item in buckets or []
    ]
    labels = [label for label in labels if label]
    na_reasons: list[str] = []
    if not labels:
        na_reasons.append("bucket_labels_missing")
    target_day = _parse_date(event_date)
    if target_day is None:
        na_reasons.append("event_date_invalid")

    own_conn = False
    active_conn = conn
    if active_conn is None:
        try:
            active_conn = _open_readonly(db_path)
            own_conn = active_conn is not None
        except sqlite3.Error as exc:
            return {
                "schema": SCHEMA_VERSION,
                "state": "unavailable",
                "ok": False,
                "read_only": True,
                "guards": _guards(),
                "prior_distribution": {},
                "prior_meta": {"state_reason": "history_db_open_error", "error": str(exc)[:160]},
                "na_reasons": ["history_db_open_error"],
            }
    if active_conn is None:
        na_reasons.append("history_db_missing")
        rows: list[dict[str, Any]] = []
    else:
        rows = _query_settled_rows(
            active_conn,
            target_event_date=str(event_date or ""),
            as_of=str(as_of or datetime.now(timezone.utc).isoformat()),
            city=city,
            station_id=station_id,
            market_var=market_var,
            market_unit=market_unit,
            history_days=history_days,
        )
    if own_conn and active_conn is not None:
        active_conn.close()

    date_window = max(0, int(date_window_days))
    if target_day is not None:
        window_rows = [
            row
            for row in rows
            if (row_day := _parse_date(row.get("event_date"))) is not None
            and _day_distance(row_day, target_day) <= date_window
        ]
    else:
        window_rows = []
    min_sample_count = max(1, int(min_samples))
    bias_components = {
        "site_temperature_bias_c": _as_bias_c(site_temperature_bias_c),
        "terrain_temperature_bias_c": _as_bias_c(terrain_temperature_bias_c),
        "urban_heat_island_c": _as_bias_c(urban_heat_island_c),
    }
    total_temperature_bias_c = sum(bias_components.values())
    stat_kwargs = {
        "labels": labels,
        "rounding_rule": rounding_rule,
        "smoothing": max(0.0, float(smoothing)),
        "temperature_bias_c": total_temperature_bias_c,
    }
    broad_stats = _bucket_stats(rows, **stat_kwargs)
    window_stats = _bucket_stats(window_rows, **stat_kwargs)
    broad_matched = int(broad_stats["matched_samples"])
    window_matched = int(window_stats["matched_samples"])
    date_window_weight = 0.0
    if window_matched > 0:
        date_window_weight = min(1.0, float(window_matched) / float(min_sample_count))
    if broad_matched <= 0 and window_matched > 0:
        date_window_weight = 1.0
    prior_values = _blend_probabilities(
        broad_stats["probabilities"],
        window_stats["probabilities"],
        date_window_weight,
    )
    if window_matched <= 0 and rows:
        na_reasons.append("date_window_history_missing_using_broader_history")
    elif window_matched < min_sample_count and broad_matched > window_matched:
        na_reasons.append("date_window_below_min_samples_shrink_to_broader_history")

    conditioning_trace: list[dict[str, Any]] = []
    continuous_condition_blends: list[tuple[Sequence[Mapping[str, Any]], float, str]] = []

    def apply_condition(key: str, value: Any) -> None:
        nonlocal prior_values
        normalized_value = _normalize_text(value)
        if not normalized_value:
            return
        condition_rows, has_any_value = _condition_rows(rows, key, normalized_value)
        if not has_any_value:
            na_reasons.append(f"{key}_history_unavailable")
            conditioning_trace.append(
                {
                    "condition": key,
                    "value": normalized_value,
                    "state": "unavailable",
                    "reason": f"{key}_history_unavailable",
                    "weight": 0.0,
                    "sample_count": 0,
                }
            )
            return
        condition_stats = _bucket_stats(condition_rows, **stat_kwargs)
        condition_matched = int(condition_stats["matched_samples"])
        if condition_matched <= 0:
            na_reasons.append(f"{key}_history_missing")
            conditioning_trace.append(
                {
                    "condition": key,
                    "value": normalized_value,
                    "state": "missing",
                    "reason": f"{key}_history_missing",
                    "weight": 0.0,
                    "sample_count": 0,
                }
            )
            return
        condition_weight = min(1.0, float(condition_matched) / float(min_sample_count))
        if condition_weight < 1.0:
            na_reasons.append(f"{key}_below_min_samples_shrink_to_parent_prior")
        prior_values = _blend_probabilities(
            prior_values,
            condition_stats["probabilities"],
            condition_weight,
        )
        continuous_condition_blends.append((condition_rows, condition_weight, key))
        conditioning_trace.append(
            {
                "condition": key,
                "value": normalized_value,
                "state": "applied",
                "weight": _round_float(condition_weight),
                "sample_count": condition_matched,
                "bucket_counts": {
                    label: _round_float(count)
                    for label, count in zip(labels, condition_stats["counts"])
                },
            }
        )

    apply_condition("weather_type", weather_type)
    apply_condition("terrain_class", terrain_class)
    apply_condition("site_class", site_class)
    apply_condition("urbanization_class", urbanization_class)

    availability_sample_count = max(broad_matched, window_matched)
    state = "available" if labels and availability_sample_count >= min_sample_count else "insufficient_history"
    if availability_sample_count <= 0:
        na_reasons.append("settled_history_missing")
    elif availability_sample_count < min_sample_count:
        na_reasons.append("settled_history_below_min_samples")

    prior_distribution = {
        label: float(_round_float(value) or 0.0)
        for label, value in zip(labels, prior_values)
    }
    prior_continuous_distribution = _continuous_prior_distribution(
        rows=rows,
        window_rows=window_rows,
        condition_blends=continuous_condition_blends,
        labels=labels,
        rounding_rule=rounding_rule,
        temperature_bias_c=total_temperature_bias_c,
        date_window_weight=date_window_weight,
    )
    sample_scale = min(1.0, float(availability_sample_count) / float(min_sample_count))
    prior_na_reasons = sorted(set(reason for reason in na_reasons if reason))
    prior_na_reason = "+".join(prior_na_reasons)
    return {
        "schema": SCHEMA_VERSION,
        "state": state,
        "ok": state == "available",
        "read_only": True,
        "production_entry_enabled": False,
        "guards": _guards(),
        "event_date": str(event_date or ""),
        "as_of": str(as_of or ""),
        "buckets": list(labels),
        "prior_distribution": prior_distribution,
        "prior_continuous_distribution": prior_continuous_distribution,
        "prior_na_reason": prior_na_reason,
        "prior_meta": {
            "schema": "temperature_prior_meta_v1",
            "state": state,
            "source": "settled_history_date_site_terrain_prior",
            "boundary": "historical_climate_date_site_terrain_weather_type_only_no_forecast_no_metar_no_market",
            "sample_count": int(availability_sample_count),
            "raw_candidate_count": len(rows),
            "effective_sample_count": len(window_rows) if date_window_weight >= 1.0 else len(rows),
            "date_window_sample_count": len(window_rows),
            "date_window_matched_sample_count": int(window_matched),
            "broad_matched_sample_count": int(broad_matched),
            "date_window_weight": _round_float(date_window_weight),
            "min_samples": int(min_sample_count),
            "sample_scale": _round_float(sample_scale),
            "shrinkage_target": "broader_settled_history",
            "history_days": int(history_days),
            "date_window_days": int(date_window_days),
            "smoothing": _round_float(max(0.0, float(smoothing))),
            "city": str(city or ""),
            "station_id": str(station_id or ""),
            "weather_type": _normalize_text(weather_type),
            "terrain_class": _normalize_text(terrain_class),
            "site_class": _normalize_text(site_class),
            "urbanization_class": _normalize_text(urbanization_class),
            "temperature_bias_c": _round_float(total_temperature_bias_c),
            "temperature_bias_components": {
                key: _round_float(value)
                for key, value in bias_components.items()
            },
            "market_var": str(market_var or ""),
            "market_unit": str(market_unit or ""),
            "rounding_rule": str(rounding_rule or ""),
            "bucket_counts": {
                label: _round_float(count)
                for label, count in zip(labels, window_stats["counts"])
            },
            "broad_bucket_counts": {
                label: _round_float(count)
                for label, count in zip(labels, broad_stats["counts"])
            },
            "conditioning_trace": conditioning_trace,
            "na_reasons": prior_na_reasons,
            "prior_na_reason": prior_na_reason,
            "sample_rows": window_stats["sample_rows"] or broad_stats["sample_rows"],
        },
        "na_reasons": prior_na_reasons,
        "next_boundary": "hierarchical_posterior_pipeline_runtime_no_production_entry",
    }


__all__ = ["SCHEMA_VERSION", "build_temperature_prior_distribution"]
