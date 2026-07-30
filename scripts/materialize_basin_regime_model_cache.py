#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_LOC_KEY = "30.5583,103.9460"
DEFAULT_STATION_ID = "ZUUU"
DEFAULT_MARKET_VAR = "temperature_2m_max"
DEFAULT_MARKET_UNIT = "C"
DEFAULT_TZ = "Asia/Shanghai"
DEFAULT_OFFICIAL_CURVE_ROOT = ROOT / "data" / "runtime" / "official_nwp_curve_sources_v2"
OFFICIAL_MODEL_PROVIDER = {
    "ecmwf_ifs025": "ecmwf",
    "gfs_seamless": "gfs",
    "icon_seamless": "icon",
}

import sys

sys.path.insert(0, str(ROOT))
from climo_regime_runtime import derive_basin_regime_label  # noqa: E402
from official_nwp_physical_regime_runtime import (  # noqa: E402
    aggregate_official_nwp_physical_regime,
    derive_official_nwp_physical_regime,
)


REGIME_COLUMNS = {
    "basin_regime": "TEXT",
    "basin_regime_features_json": "TEXT",
    "basin_regime_source": "TEXT",
    "basin_regime_updated_at": "TEXT",
    "basin_regime_confidence": "REAL",
}

COMPACT_FEATURE_KEYS = {
    "basin_regime",
    "primary_regime_label",
    "regime_confidence",
    "monsoon_humidity_regime",
    "subtropical_high_regime",
    "basin_ventilation_index",
    "basin_ventilation_regime",
    "basin_heat_mechanism_state",
    "basin_heat_mechanism_status",
    "basin_heat_uplift_index",
    "basin_heat_mechanism_support_score",
    "basin_heat_mechanism_suppression_score",
    "basin_heat_mechanism_temperature_adjustment_c",
    "basin_heat_mechanism_active_support_keys",
    "basin_heat_mechanism_active_suppression_keys",
    "high_temp_potential_gate",
    "climo_tmax_sample_count",
    "climo_tmax_basis",
    "climo_tmax_month",
    "source",
    "updated_at",
}


def _today_local(tz_name: str) -> str:
    return datetime.now(ZoneInfo(tz_name)).date().isoformat()


def _safe_json(raw: Any) -> Dict[str, Any]:
    try:
        payload = json.loads(str(raw or ""))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _parse_time(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _ensure_columns(conn: sqlite3.Connection) -> None:
    cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(model_member_cache)")}
    for column, coltype in REGIME_COLUMNS.items():
        if column not in cols:
            conn.execute(f"ALTER TABLE model_member_cache ADD COLUMN {column} {coltype}")
    conn.commit()


def _extract_features(fact: Mapping[str, Any]) -> Dict[str, Any]:
    features = fact.get("climo_regime_features")
    if not isinstance(features, Mapping):
        climo_context = fact.get("climo_regime")
        if isinstance(climo_context, Mapping) and isinstance(climo_context.get("features"), Mapping):
            features = climo_context.get("features")
    if not isinstance(features, Mapping):
        return {}
    out = dict(features)
    basin_regime = str(out.get("basin_regime") or "").strip()
    if not basin_regime:
        basin_regime = derive_basin_regime_label(out)
    if basin_regime and basin_regime != "unknown":
        out["basin_regime"] = basin_regime
    return out


def _compact_features(features: Mapping[str, Any]) -> str:
    compact = {key: value for key, value in dict(features).items() if key in COMPACT_FEATURE_KEYS}
    return json.dumps(compact, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _load_official_curve_regimes(curve_root: Path) -> Dict[tuple[str, str], list[Dict[str, Any]]]:
    """Index strict-as-of physical regimes from immutable official curves.

    Older revisions are enriched in memory from their exact snapshot hashes;
    immutable files are never rewritten.  Only upper-air physical fields are
    sent to the classifier, never the temperature curve.
    """

    indexed: Dict[tuple[str, str], list[Dict[str, Any]]] = {}
    root = curve_root.expanduser().resolve()
    for provider in sorted(set(OFFICIAL_MODEL_PROVIDER.values())):
        history = root / provider / "history"
        revision_dir = root / provider / "daily_peak_revisions"
        for path in sorted(revision_dir.glob("*.json")):
            try:
                revision = _safe_json(path.read_text(encoding="utf-8"))
            except OSError:
                continue
            if revision.get("ok") is not True or str(revision.get("state") or "") != "available":
                continue
            event_date = str(revision.get("event_date") or "")[:10]
            available_at = str(revision.get("available_at") or "")
            available_dt = _parse_time(available_at)
            if not event_date or available_dt is None:
                continue
            regime = revision.get("physical_regime")
            if not isinstance(regime, Mapping):
                point_regimes: list[Dict[str, Any]] = []
                for snapshot_sha in revision.get("snapshot_sha256s") or []:
                    snapshot_path = history / f"{str(snapshot_sha or '').strip()}.json"
                    if not snapshot_path.is_file():
                        point_regimes = []
                        break
                    try:
                        snapshot = _safe_json(snapshot_path.read_text(encoding="utf-8"))
                    except OSError:
                        point_regimes = []
                        break
                    features = snapshot.get("normalized_features")
                    if not isinstance(features, Mapping):
                        point_regimes = []
                        break
                    point_regimes.append(derive_official_nwp_physical_regime(features))
                regime = aggregate_official_nwp_physical_regime(point_regimes)
            regime_map = dict(regime) if isinstance(regime, Mapping) else {}
            label = str(regime_map.get("basin_regime") or "unknown").strip()
            if not label or label == "unknown":
                continue
            indexed.setdefault((provider, event_date), []).append(
                {
                    "provider_id": provider,
                    "event_date": event_date,
                    "available_at": available_at,
                    "available_dt": available_dt,
                    "source_revision_id": str(revision.get("source_revision_id") or path.stem),
                    "basin_regime": label,
                    "basin_regime_confidence": _to_float(regime_map.get("basin_regime_confidence")),
                    "physical_regime": regime_map,
                }
            )
    for rows in indexed.values():
        rows.sort(key=lambda item: (item["available_dt"], item["source_revision_id"]))
    return indexed


def _materialize_official_curve_regimes(
    conn: sqlite3.Connection,
    *,
    curve_root: Path,
    loc_key: str,
    market_var: str,
    market_unit: str,
    start_date: str,
    end_date: str,
    apply: bool,
) -> Dict[str, Any]:
    revisions = _load_official_curve_regimes(curve_root)
    params: list[Any] = [loc_key, market_var, market_unit, start_date, *OFFICIAL_MODEL_PROVIDER]
    end_clause = ""
    if end_date:
        end_clause = "AND event_date <= ?"
        params.append(end_date)
    placeholders = ",".join("?" for _ in OFFICIAL_MODEL_PROVIDER)
    cache_rows = conn.execute(
        f"""
        SELECT id,model,event_date,created_at
        FROM model_member_cache
        WHERE loc_key=? AND market_var=? AND market_unit=? AND event_date>=?
          AND model IN ({placeholders})
          {end_clause}
          AND COALESCE(official_updated_source,'')!='open_meteo_historical_forecast'
          AND COALESCE(basin_regime,'')=''
        ORDER BY datetime(created_at),id
        """,
        tuple(params),
    ).fetchall()
    candidates: list[Dict[str, Any]] = []
    changed = 0
    for cache in cache_rows:
        provider = OFFICIAL_MODEL_PROVIDER.get(str(cache["model"] or ""), "")
        created_at = _parse_time(cache["created_at"])
        eligible = [
            row
            for row in revisions.get((provider, str(cache["event_date"] or "")[:10]), [])
            if created_at is not None and row["available_dt"] <= created_at
        ]
        if not eligible:
            continue
        selected = eligible[-1]
        feature_payload = {
            "schema": "model_cache_official_nwp_physical_regime_v1",
            "basin_regime": selected["basin_regime"],
            "regime_confidence": selected["basin_regime_confidence"],
            "source": "official_nwp_complete_hourly_curve_physical_fields",
            "source_revision_id": selected["source_revision_id"],
            "available_at": selected["available_at"],
            "physical_regime": selected["physical_regime"],
            "boundary": {
                "strict_asof_revision_available_before_cache_row": True,
                "temperature_curve_weather_type_inference_forbidden": True,
                "temperature_fields_consumed": False,
                "merge_fill_only": True,
            },
        }
        if apply:
            cursor = conn.execute(
                """
                UPDATE model_member_cache
                SET basin_regime=?, basin_regime_features_json=?, basin_regime_source=?,
                    basin_regime_updated_at=?, basin_regime_confidence=?
                WHERE id=? AND COALESCE(basin_regime,'')=''
                """,
                (
                    selected["basin_regime"],
                    json.dumps(feature_payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
                    f"official_nwp_revision:{selected['source_revision_id']}",
                    selected["available_at"],
                    selected["basin_regime_confidence"],
                    int(cache["id"]),
                ),
            )
            changed += int(cursor.rowcount or 0)
        candidates.append(
            {
                "cache_row_id": int(cache["id"]),
                "model": str(cache["model"]),
                "event_date": str(cache["event_date"]),
                "created_at": str(cache["created_at"]),
                "basin_regime": selected["basin_regime"],
                "source_revision_id": selected["source_revision_id"],
            }
        )
    return {
        "revision_count": sum(len(rows) for rows in revisions.values()),
        "candidate_rows": len(candidates),
        "rows_changed": changed,
        "rows": candidates,
    }


def materialize(
    conn: sqlite3.Connection,
    *,
    start_date: str,
    end_date: str,
    loc_key: str,
    station_id: str,
    market_var: str,
    market_unit: str,
    official_curve_root: Path = DEFAULT_OFFICIAL_CURVE_ROOT,
    apply: bool,
) -> Dict[str, Any]:
    if apply:
        _ensure_columns(conn)
    cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(model_member_cache)")}
    has_regime_cols = set(REGIME_COLUMNS).issubset(cols)
    params = [start_date]
    end_clause = ""
    if end_date:
        end_clause = "AND h.event_date <= ?"
        params.append(end_date)
    params.extend([market_var, market_var, market_unit, market_unit, station_id, f"%{station_id.lower()}%"])
    rows = conn.execute(
        f"""
        SELECT h.id, h.slug, h.event_date, h.fact_as_of, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE h.event_date >= ?
          {end_clause}
          AND COALESCE(json_extract(h.fact_json, '$.market_var'), ?) = ?
          AND COALESCE(json_extract(h.fact_json, '$.market_unit'), ?) = ?
          AND (
                LOWER(COALESCE(json_extract(h.fact_json, '$.station_id'), '')) = LOWER(?)
                OR LOWER(h.slug) LIKE ?
              )
        ORDER BY h.event_date ASC, h.id ASC
        """,
        tuple(params),
    ).fetchall()
    candidates = 0
    changed = 0
    skipped: list[Dict[str, Any]] = []
    updated: list[Dict[str, Any]] = []
    for row in rows:
        fact = _safe_json(row["fact_json"])
        features = _extract_features(fact)
        basin_regime = str(features.get("basin_regime") or "").strip()
        if not basin_regime or basin_regime == "unknown":
            skipped.append(
                {
                    "event_date": row["event_date"],
                    "slug": row["slug"],
                    "reason": "climo_regime_features_missing_or_unknown",
                }
            )
            continue
        candidates += 1
        source = str(features.get("regime_source") or features.get("source") or "event_fact_latest.climo_regime_features")
        updated_at = str(features.get("regime_updated_at") or features.get("updated_at") or row["fact_as_of"] or "")
        confidence = _to_float(features.get("regime_confidence"))
        feature_json = _compact_features(features)
        if has_regime_cols:
            target_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM model_member_cache
                WHERE loc_key = ?
                  AND event_date = ?
                  AND market_var = ?
                  AND market_unit = ?
                  AND COALESCE(official_updated_source, '') != 'open_meteo_historical_forecast'
                  AND (
                        basin_regime IS NULL
                        OR basin_regime = ''
                        OR basin_regime_features_json IS NULL
                        OR basin_regime_features_json = ''
                      )
                """,
                (loc_key, row["event_date"], market_var, market_unit),
            ).fetchone()[0]
        else:
            target_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM model_member_cache
                WHERE loc_key = ?
                  AND event_date = ?
                  AND market_var = ?
                  AND market_unit = ?
                  AND COALESCE(official_updated_source, '') != 'open_meteo_historical_forecast'
                """,
                (loc_key, row["event_date"], market_var, market_unit),
            ).fetchone()[0]
        if apply and int(target_count or 0) > 0:
            cur = conn.execute(
                """
                UPDATE model_member_cache
                SET basin_regime = COALESCE(NULLIF(basin_regime, ''), ?),
                    basin_regime_features_json = COALESCE(NULLIF(basin_regime_features_json, ''), ?),
                    basin_regime_source = COALESCE(NULLIF(basin_regime_source, ''), ?),
                    basin_regime_updated_at = COALESCE(NULLIF(basin_regime_updated_at, ''), ?),
                    basin_regime_confidence = COALESCE(basin_regime_confidence, ?)
                WHERE loc_key = ?
                  AND event_date = ?
                  AND market_var = ?
                  AND market_unit = ?
                  AND COALESCE(official_updated_source, '') != 'open_meteo_historical_forecast'
                  AND (
                        basin_regime IS NULL
                        OR basin_regime = ''
                        OR basin_regime_features_json IS NULL
                        OR basin_regime_features_json = ''
                      )
                """,
                (
                    basin_regime,
                    feature_json,
                    source,
                    updated_at,
                    confidence,
                    loc_key,
                    row["event_date"],
                    market_var,
                    market_unit,
                ),
            )
            changed += int(cur.rowcount or 0)
        updated.append(
            {
                "event_date": row["event_date"],
                "slug": row["slug"],
                "basin_regime": basin_regime,
                "source": source,
                "target_rows": int(target_count or 0),
            }
        )
    official_curve = (
        _materialize_official_curve_regimes(
            conn,
            curve_root=official_curve_root,
            loc_key=loc_key,
            market_var=market_var,
            market_unit=market_unit,
            start_date=start_date,
            end_date=end_date,
            apply=apply,
        )
        if has_regime_cols
        else {"revision_count": 0, "candidate_rows": 0, "rows_changed": 0, "rows": []}
    )
    if apply:
        conn.commit()
    return {
        "ok": True,
        "apply": bool(apply),
        "start_date": start_date,
        "end_date": end_date or None,
        "candidates": candidates,
        "rows_changed": changed,
        "official_curve_regime": official_curve,
        "updated": updated,
        "skipped": skipped,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize basin_regime features into model_member_cache.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--tz", default=DEFAULT_TZ)
    parser.add_argument("--loc-key", default=DEFAULT_LOC_KEY)
    parser.add_argument("--station-id", default=DEFAULT_STATION_ID)
    parser.add_argument("--market-var", default=DEFAULT_MARKET_VAR)
    parser.add_argument("--market-unit", default=DEFAULT_MARKET_UNIT)
    parser.add_argument("--official-curve-root", type=Path, default=DEFAULT_OFFICIAL_CURVE_ROOT)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    start_date = str(args.start_date or _today_local(str(args.tz)))
    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row
    try:
        result = materialize(
            conn,
            start_date=start_date,
            end_date=str(args.end_date or ""),
            loc_key=str(args.loc_key),
            station_id=str(args.station_id),
            market_var=str(args.market_var),
            market_unit=str(args.market_unit),
            official_curve_root=args.official_curve_root,
            apply=bool(args.apply),
        )
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
