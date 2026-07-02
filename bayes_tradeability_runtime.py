from __future__ import annotations

from datetime import datetime, timedelta
import json
import math
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple


def _to_float(value: Any) -> Optional[float]:
    try:
        num = float(value)
    except Exception:
        return None
    if not math.isfinite(num):
        return None
    return float(num)


def _to_int(value: Any) -> Optional[int]:
    try:
        num = int(value)
    except Exception:
        return None
    return int(num)


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    raw = str(value).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _env_float(key: str, default: Optional[float]) -> Optional[float]:
    val = _to_float(os.environ.get(key))
    if val is None:
        return default
    return val


def _env_int(key: str, default: Optional[int]) -> Optional[int]:
    val = _to_int(os.environ.get(key))
    if val is None:
        return default
    return val


def _bayes_enabled() -> bool:
    return _to_bool(os.environ.get("PM_BAYES_TRADEABILITY_ENABLED"), default=False)


def _load_config() -> Dict[str, Any]:
    enabled = _bayes_enabled()
    prior_alpha = _env_float("PM_BAYES_TRADEABILITY_PRIOR_ALPHA", 8.0)
    prior_beta = _env_float("PM_BAYES_TRADEABILITY_PRIOR_BETA", 8.0)
    lookback_days = _env_int("PM_BAYES_TRADEABILITY_LOOKBACK_DAYS", 180)
    scale_floor = _env_float("PM_BAYES_TRADEABILITY_SCALE_FLOOR", 0.40)
    min_soft = _env_int("PM_BAYES_TRADEABILITY_MIN_SOFT_EVIDENCE", 0)
    min_hard = _env_int("PM_BAYES_TRADEABILITY_MIN_HARD_EVIDENCE", 40)
    z = _env_float("PM_BAYES_TRADEABILITY_Z", 1.28)
    hard_enabled = _to_bool(os.environ.get("PM_BAYES_TRADEABILITY_HARD_BLOCK"), default=False)
    hard_threshold = _env_float("PM_BAYES_TRADEABILITY_HARD_MEAN_MIN", 0.40)
    lcb_threshold = _env_float("PM_BAYES_TRADEABILITY_HARD_LCB_MIN", 0.35)
    require_data = _to_bool(os.environ.get("PM_BAYES_TRADEABILITY_REQUIRE_DATA"), default=False)

    if lookback_days is None or lookback_days <= 0:
        lookback_days = 180
    if prior_alpha is None or not math.isfinite(prior_alpha) or prior_alpha <= 0:
        prior_alpha = 8.0
    if prior_beta is None or not math.isfinite(prior_beta) or prior_beta <= 0:
        prior_beta = 8.0
    if scale_floor is None or not math.isfinite(scale_floor):
        scale_floor = 0.40
    scale_floor = max(0.0, min(1.0, float(scale_floor)))
    if z is None or not math.isfinite(z) or z < 0:
        z = 1.28
    if min_soft is None or min_soft < 0:
        min_soft = 0
    if min_hard is None or min_hard < 0:
        min_hard = 40
    if hard_threshold is None or not math.isfinite(hard_threshold):
        hard_threshold = 0.40
    if lcb_threshold is None or not math.isfinite(lcb_threshold):
        lcb_threshold = 0.35
    return {
        "enabled": bool(enabled),
        "prior_alpha": float(prior_alpha),
        "prior_beta": float(prior_beta),
        "lookback_days": int(lookback_days),
        "scale_floor": float(scale_floor),
        "min_soft": int(min_soft),
        "min_hard": int(min_hard),
        "z": float(z),
        "hard_enabled": bool(hard_enabled),
        "hard_threshold": float(hard_threshold),
        "lcb_threshold": float(lcb_threshold),
        "require_data": bool(require_data),
    }


def _count_from_rows(
    rows: List[Tuple[Any, ...]],
    *,
    strategy: str,
    slug: str,
    event_date: str,
    require_event_match: bool,
) -> Tuple[int, int, str]:
    strategy = str(strategy or "").strip().lower()
    slug = str(slug or "").strip().lower()
    event_date = str(event_date or "").strip()
    matched_scope = "none"
    wins = 0
    losses = 0

    def _acc(scope: str, match_strategy: bool, match_slug: bool, match_event: bool) -> bool:
        nonlocal wins, losses, matched_scope
        s = 0
        l = 0
        for label, row_strategy, row_slug, row_event_date in rows:
            if match_strategy and row_strategy != strategy:
                continue
            if match_slug and row_slug != slug:
                continue
            if match_event and require_event_match and event_date and row_event_date != event_date:
                continue
            if label == 1:
                s += 1
            elif label == 0:
                l += 1
        if s + l <= 0:
            return False
        wins = s
        losses = l
        matched_scope = scope
        return True

    if _acc("exact_slug_event", match_strategy=True, match_slug=True, match_event=True):
        return wins, losses, matched_scope
    if _acc("exact_slug", match_strategy=True, match_slug=True, match_event=False):
        return wins, losses, matched_scope
    if _acc("strategy", match_strategy=True, match_slug=False, match_event=False):
        return wins, losses, matched_scope
    if _acc("outcome_side_global", match_strategy=False, match_slug=False, match_event=False):
        return wins, losses, matched_scope
    return 0, 0, "none"


def _query_decision_posterior(
    db_path: str,
    *,
    strategy: str,
    slug: str,
    outcome: str,
    side: str,
    event_date: str,
    lookback_days: int,
) -> Tuple[int, int, str, str]:
    if not db_path or not os.path.exists(db_path):
        return 0, 0, "none", "missing_db"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cutoff: Optional[str] = None
        if lookback_days and lookback_days > 0:
            cutoff = (datetime.utcnow() - timedelta(days=int(lookback_days))).replace(
                microsecond=0
            ).isoformat()
        sql = """
            SELECT l.label, d.slug, d.event_date, d.outcome, d.side, d.details_json
            FROM auto_trade_decisions d
            JOIN calibration_labels l
              ON l.decision_id = d.id
            WHERE d.action = 'entry'
              AND d.will_order = 1
              AND d.outcome = ?
              AND d.side = ?
              AND l.label IN (0, 1)
        """
        params: List[Any] = [str(outcome or "").strip().upper(), str(side or "").strip().upper()]
        if cutoff:
            sql += " AND d.created_at >= ?"
            params.append(cutoff)
        sql += " ORDER BY d.created_at DESC"
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
        parsed_rows: List[Tuple[int, str, str, str]] = []
        for row in rows:
            row_dict = dict(row)
            details_txt = str(row_dict.get("details_json") or "").strip()
            details_strategy = ""
            if details_txt:
                try:
                    details_obj = json.loads(details_txt)
                    if isinstance(details_obj, dict):
                        details_strategy = str(details_obj.get("strategy") or "").strip().lower()
                except Exception:
                    details_strategy = ""
            parsed_rows.append(
                (
                    int(row_dict.get("label") or 0),
                    str(details_strategy or "").strip().lower(),
                    str(row_dict.get("slug") or "").strip().lower(),
                    str(row_dict.get("event_date") or "").strip(),
                )
            )
        return _count_from_rows(
            parsed_rows,
            strategy=strategy,
            slug=slug,
            event_date=event_date,
            require_event_match=True,
        )
    except Exception:
        return 0, 0, "none", "decision_query_error"
    finally:
        conn.close()


def _query_edge_tradeable_posterior(
    db_path: str,
    *,
    slug: str,
    outcome: str,
    side: str,
    event_date: str,
) -> Tuple[int, int, str]:
    if not db_path or not os.path.exists(db_path):
        return 0, 0, "missing_db"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        slug_val = str(slug or "").strip()
        outcome_val = str(outcome or "").strip().upper()
        side_val = str(side or "").strip().upper()
        event_date_val = str(event_date or "").strip()
        sql = """
            SELECT
              SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END),
              SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END)
            FROM edge_trades
            WHERE realized_pnl IS NOT NULL
              AND COALESCE(state,'') IN ('closed','settled','exit','resolved')
        """
        params: List[Any] = []
        if slug_val:
            sql += " AND slug = ?"
            params.append(slug_val)
        if outcome_val:
            sql += " AND outcome = ?"
            params.append(outcome_val)
        if side_val:
            sql += " AND side = ?"
            params.append(side_val)
        if event_date_val:
            sql += " AND event_date = ?"
            params.append(event_date_val)
        cur.execute(sql, tuple(params))
        row = cur.fetchone()
        if not row:
            return 0, 0, "edge_trades"
        wins = int(_to_float(row[0]) or 0.0)
        losses = int(_to_float(row[1]) or 0.0)
        if wins + losses <= 0:
            return 0, 0, "edge_trades"
        return wins, losses, "edge_trades"
    except Exception:
        return 0, 0, "edge_trades_query_error"
    finally:
        conn.close()


def _compute_posterior_meta(
    wins: int,
    losses: int,
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    prior_alpha = float(cfg["prior_alpha"])
    prior_beta = float(cfg["prior_beta"])
    total = int(wins) + int(losses)
    alpha = prior_alpha + float(wins)
    beta = prior_beta + float(losses)
    evidence = max(0, int(total))
    prior_mean = prior_alpha / (prior_alpha + prior_beta)
    posterior_mean = alpha / (alpha + beta)
    if total > 0:
        posterior_std = math.sqrt(max(0.0, posterior_mean * (1.0 - posterior_mean) / (alpha + beta + 1.0)))
        posterior_lcb = max(0.0, posterior_mean - float(cfg["z"]) * posterior_std)
        scale = min(1.0, posterior_mean / max(prior_mean, 1e-12)) if posterior_mean <= prior_mean else 1.0
    else:
        posterior_std = 0.0
        posterior_lcb = posterior_mean
        scale = 1.0
    scale = max(float(cfg["scale_floor"]), float(scale))
    low_sample = evidence < int(cfg["min_soft"])
    hard_block = False
    hard_reason = ""
    if bool(cfg["hard_enabled"]) and total >= int(cfg["min_hard"]) and (
        posterior_mean + TIME_EPS < float(cfg["hard_threshold"])
        or posterior_lcb + TIME_EPS < float(cfg["lcb_threshold"])
    ):
        hard_block = True
        hard_reason = "bayes_hard_block"
    return {
        "evidence": evidence,
        "wins": int(wins),
        "losses": int(losses),
        "prior_alpha": prior_alpha,
        "prior_beta": prior_beta,
        "prior_mean": prior_mean,
        "posterior_alpha": alpha,
        "posterior_beta": beta,
        "posterior_mean": posterior_mean,
        "posterior_std": posterior_std,
        "posterior_lcb": posterior_lcb,
        "scale": float(scale),
        "low_sample": bool(low_sample),
        "hard_block": bool(hard_block),
        "hard_block_reason": str(hard_reason),
    }


# numeric epsilon used for soft/hard policy checks
TIME_EPS = 1e-9


def apply_bayes_tradeability_adjustment(
    *,
    edge_lcb: Optional[float],
    db_path: str,
    strategy: str,
    outcome: str,
    side: str,
    slug: str = "",
    event_date: str = "",
) -> Tuple[Optional[float], Dict[str, Any]]:
    cfg = _load_config()
    meta: Dict[str, Any] = {
        "edge_tradeability_bayes_enabled": bool(cfg["enabled"]),
        "edge_tradeability_bayes_scale": 1.0,
        "edge_tradeability_bayes_evidence": 0,
        "edge_tradeability_bayes_wins": 0,
        "edge_tradeability_bayes_losses": 0,
        "edge_tradeability_bayes_source": "disabled",
        "edge_tradeability_bayes_scope": "disabled",
        "edge_tradeability_bayes_prior_alpha": float(cfg["prior_alpha"]),
        "edge_tradeability_bayes_prior_beta": float(cfg["prior_beta"]),
        "edge_tradeability_bayes_hard_block": False,
    }
    edge_lcb_val = _to_float(edge_lcb)
    if edge_lcb_val is None:
        return None, meta
    if not cfg["enabled"]:
        return edge_lcb_val, meta

    strategy_norm = str(strategy or "main").strip().lower() or "main"
    outcome_norm = str(outcome or "").strip().upper()
    side_norm = str(side or "").strip().upper()
    slug_norm = str(slug or "").strip()

    wins, losses, source = _query_decision_posterior(
        db_path,
        strategy=strategy_norm,
        slug=slug_norm,
        outcome=outcome_norm,
        side=side_norm,
        event_date=str(event_date or ""),
        lookback_days=int(cfg["lookback_days"]),
    )
    scope = str(source or "")
    if wins + losses <= 0:
        wins, losses, source = _query_edge_tradeable_posterior(
            db_path,
            slug=slug_norm,
            outcome=outcome_norm,
            side=side_norm,
            event_date=str(event_date or ""),
        )
        scope = "edge_trades"

    posterior = _compute_posterior_meta(wins, losses, cfg)
    meta.update(
        {
            "edge_tradeability_bayes_source": str(source),
            "edge_tradeability_bayes_scope": str(scope),
            "edge_tradeability_bayes_evidence": int(posterior["evidence"]),
            "edge_tradeability_bayes_wins": int(posterior["wins"]),
            "edge_tradeability_bayes_losses": int(posterior["losses"]),
            "edge_tradeability_bayes_low_sample": bool(posterior["low_sample"]),
            "edge_tradeability_bayes_prior_mean": float(posterior["prior_mean"]),
            "edge_tradeability_bayes_posterior_mean": float(posterior["posterior_mean"]),
            "edge_tradeability_bayes_posterior_lcb": float(posterior["posterior_lcb"]),
            "edge_tradeability_bayes_scale": float(posterior["scale"]),
            "edge_tradeability_bayes_hard_block": bool(posterior["hard_block"]),
            "edge_tradeability_bayes_hard_block_reason": str(
                posterior["hard_block_reason"]
            ),
        }
    )

    query_source = str(source or "").strip()
    if query_source == "decision_query_error" or query_source == "edge_trades_query_error":
        meta["edge_tradeability_bayes_na_reason"] = "history_query_error"
        return edge_lcb_val, meta
    if cfg["require_data"] and int(posterior["evidence"]) <= 0:
        meta["edge_tradeability_bayes_na_reason"] = "history_missing"
        return edge_lcb_val, meta

    if bool(posterior["hard_block"]):
        return None, meta

    if bool(posterior["low_sample"]):
        meta["edge_tradeability_bayes_na_reason"] = "low_sample"

    adjusted = float(edge_lcb_val) * float(posterior["scale"])
    return adjusted, meta
