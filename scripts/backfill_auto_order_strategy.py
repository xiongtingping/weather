import json
import math
import os
import sqlite3
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import scan_weather_market as s
from auto_order_ledger_writer import update_auto_order_strategy_by_id

DB_PATH = 'data/archive.db'
CONFIG_PATH = 'data/auto_trade_config.json'


def load_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, 'r') as f:
        return json.load(f)


def cfg_float(cfg: Dict[str, Any], key: str, default: Optional[float]) -> Optional[float]:
    val = cfg.get(key, default)
    if val is None:
        return None
    try:
        return float(val)
    except Exception:
        return default


def cfg_bool(cfg: Dict[str, Any], key: str, default: bool) -> bool:
    val = cfg.get(key, default)
    return bool(val) if val is not None else default


@dataclass
class EvalResult:
    ok: bool
    reasons: List[str]


def _top_model_stats(rows: List[Dict[str, Any]]) -> Tuple[str, Optional[float], Optional[float]]:
    return s._top_model_bucket(rows)


def _top_spread(rows: List[Dict[str, Any]], top_label: str) -> Optional[float]:
    if not top_label:
        return None
    for row in rows:
        if str(row.get('outcome') or '') != top_label:
            continue
        spread_val = s.to_float(row.get('q_round_model_spread'))
        if spread_val is None or not math.isfinite(spread_val):
            q_min = s.to_float(row.get('q_min'))
            q_max = s.to_float(row.get('q_max'))
            if q_min is not None and q_max is not None:
                spread_val = q_max - q_min
        if spread_val is not None and math.isfinite(spread_val):
            return max(spread_val, 0.0)
    return None


def _weight_top_bonus(payload: Dict[str, Any], rows: List[Dict[str, Any]], run_date: str, event_date: str) -> bool:
    models = payload.get('models') or []
    if not models:
        return False
    rounding_rule = payload.get('rounding_rule')
    market_unit = payload.get('market_unit')
    bucket_labels = [r.get('outcome') for r in rows if r.get('outcome')]
    scenario_override = s.scenario_weights(models, run_date, event_date, rounding_rule, market_unit, bucket_labels)
    weights_equal = s.resolve_weights(models, 'equal', weight_date=run_date)
    weights_auto = s.resolve_weights(models, 'auto', weight_date=run_date)
    weights_scenario = s.resolve_weights(models, 'scenario', weights_override=scenario_override, weight_date=run_date)
    weight_top_equal = s._top_label_for_weights(rows, models, weights_equal)
    weight_top_auto = s._top_label_for_weights(rows, models, weights_auto)
    weight_top_scenario = s._top_label_for_weights(rows, models, weights_scenario)
    weight_label_counts: Dict[str, int] = {}
    for label in (weight_top_equal, weight_top_auto, weight_top_scenario):
        if label:
            weight_label_counts[label] = weight_label_counts.get(label, 0) + 1
    if not weight_label_counts:
        return False
    weight_top_label, weight_top_hits = max(weight_label_counts.items(), key=lambda item: (item[1], item[0]))
    weight_top_consistent = weight_top_hits >= 2
    top_model_label, _, _ = _top_model_stats(rows)
    weight_top_match = bool(top_model_label) and bool(weight_top_label) and top_model_label == weight_top_label
    return weight_top_consistent and weight_top_match


def _model4_consistent(rows: List[Dict[str, Any]], models: List[str]) -> bool:
    top_model_label, _, _ = _top_model_stats(rows)
    model_top_label, model_top_ratio, model_top_total, model_top_all = s._model_top_label_consensus(rows, models)
    return bool(
        model_top_all
        and model_top_total >= 4
        and top_model_label
        and model_top_label
        and model_top_label == top_model_label
    )


def eval_yes_top(cfg: Dict[str, Any], payload: Dict[str, Any], rows: List[Dict[str, Any]], run_date: str, event_date: str, outcome: str) -> EvalResult:
    reasons: List[str] = []
    row_map = {str(r.get('outcome') or ''): r for r in rows}
    row = row_map.get(outcome)
    if not row:
        return EvalResult(False, ['missing_row'])

    top_label, top_q, second_q = _top_model_stats(rows)
    if not top_label or outcome != top_label:
        return EvalResult(False, ['not_top_label'])

    q_prob_val = s.to_float(row.get('q_entry'))
    if q_prob_val is None or not math.isfinite(q_prob_val):
        return EvalResult(False, ['q_entry_missing'])

    entry_yes_top_min_q = cfg_float(cfg, 'entry_yes_top_min_q', s.MINUTE_EDGE_ENTRY_YES_TOP_Q_MIN)
    if entry_yes_top_min_q is not None and q_prob_val + s.TIME_EDGE_Q_EPS < entry_yes_top_min_q:
        reasons.append('min_q')

    top_gap = None
    if top_q is not None and math.isfinite(top_q):
        if second_q is None or not math.isfinite(second_q):
            top_gap = top_q
        else:
            top_gap = top_q - second_q

    q_round_val = s.to_float(row.get('q_round'))
    q_cal_val = s.to_float(row.get('q_calibrated'))
    ask_val = s.to_float(row.get('best_ask'))

    entry_yes_top_q_consist_eps = cfg_float(cfg, 'entry_yes_top_q_consist_eps', 0.02)
    entry_yes_top_market_consist_eps = cfg_float(cfg, 'entry_yes_top_market_consist_eps', entry_yes_top_q_consist_eps)

    q_consistent = (
        q_round_val is not None and q_cal_val is not None
        and math.isfinite(q_round_val) and math.isfinite(q_cal_val)
        and abs(q_round_val - q_cal_val) <= entry_yes_top_q_consist_eps
    )
    market_consistent = (
        q_consistent
        and ask_val is not None
        and math.isfinite(ask_val)
        and abs(ask_val - q_round_val) <= entry_yes_top_market_consist_eps
        and abs(ask_val - q_cal_val) <= entry_yes_top_market_consist_eps
    )
    direct_entry = bool(market_consistent)

    models = payload.get('models') or []
    strong_consistency = bool(q_consistent or _model4_consistent(rows, models))

    entry_yes_top_gap_min = cfg_float(cfg, 'entry_yes_top_gap_min', s.MINUTE_EDGE_ENTRY_YES_TOP_GAP_MIN)
    if entry_yes_top_gap_min and not strong_consistency:
        if top_gap is None or not math.isfinite(top_gap) or top_gap + s.TIME_EDGE_Q_EPS < entry_yes_top_gap_min:
            reasons.append('gap')

    model_ratio_full = False
    if models:
        model_top_label, model_top_ratio, model_top_total, model_top_all = s._model_top_label_consensus(rows, models)
        model_ratio_full = (
            model_top_ratio is not None
            and math.isfinite(model_top_ratio)
            and model_top_ratio + s.TIME_EDGE_Q_EPS >= 1.0
        )
    entry_yes_top_model_spread_max = cfg_float(cfg, 'entry_yes_top_model_spread_max', s.MINUTE_EDGE_ENTRY_YES_TOP_MODEL_SPREAD_MAX)
    if entry_yes_top_model_spread_max and not model_ratio_full and not strong_consistency:
        top_spread = _top_spread(rows, top_label)
        if top_spread is None or not math.isfinite(top_spread) or top_spread > entry_yes_top_model_spread_max + s.TIME_EDGE_Q_EPS:
            reasons.append('model_spread')

    # signal edge check (unless strong consistency or weight top bonus)
    entry_require_signal_edge = cfg_bool(cfg, 'entry_require_signal_edge', True)
    signal_edge_val = None
    if ask_val is not None and math.isfinite(ask_val):
        signal_edge_val = q_prob_val - ask_val
    if entry_require_signal_edge and not strong_consistency and not _weight_top_bonus(payload, rows, run_date, event_date):
        entry_yes_top_signal_edge_min = cfg_float(cfg, 'entry_yes_top_signal_edge_min', s.MINUTE_EDGE_ENTRY_YES_TOP_EDGE_MIN)
        if signal_edge_val is None or not math.isfinite(signal_edge_val) or signal_edge_val <= entry_yes_top_signal_edge_min + s.TIME_EDGE_Q_EPS:
            reasons.append('signal_edge')

    # ask checks
    if ask_val is None or not math.isfinite(ask_val) or ask_val <= 0:
        reasons.append('ask_missing')
    else:
        if not direct_entry:
            entry_yes_top_require_ask_min = cfg_bool(cfg, 'entry_yes_top_require_ask_min', s.MINUTE_EDGE_ENTRY_YES_TOP_REQUIRE_ASK_MIN)
            entry_yes_top_ask_min = cfg_float(cfg, 'entry_yes_top_ask_min', s.MINUTE_EDGE_ENTRY_YES_TOP_ASK_MIN)
            if entry_yes_top_require_ask_min and entry_yes_top_ask_min is not None and ask_val < entry_yes_top_ask_min - s.TIME_EDGE_Q_EPS:
                reasons.append('ask_min')
            if s._is_tail_locked_quote(ask_val, q_prob_val):
                reasons.append('tail_lock')
        entry_yes_top_require_ask_max = cfg_bool(cfg, 'entry_yes_top_require_ask_max', True)
        entry_yes_top_ask_max = cfg_float(cfg, 'entry_yes_top_ask_max', s.MINUTE_EDGE_ENTRY_YES_TOP_ASK_MAX)
        if entry_yes_top_require_ask_max and not direct_entry:
            if entry_yes_top_ask_max is not None and ask_val > entry_yes_top_ask_max + s.TIME_EDGE_Q_EPS:
                reasons.append('ask_max')

    # spread & lag checks
    spread_val_f = s.to_float(row.get('spread'))
    entry_require_nonneg_spread = cfg_bool(cfg, 'entry_require_nonneg_spread', True)
    if entry_require_nonneg_spread and not direct_entry:
        if spread_val_f is None or not math.isfinite(spread_val_f) or spread_val_f < 0:
            reasons.append('spread_nonneg')
    entry_yes_top_require_spread_max = cfg_bool(cfg, 'entry_yes_top_require_spread_max', True)
    entry_yes_top_spread_max = cfg_float(cfg, 'entry_yes_top_spread_max', s.MINUTE_EDGE_ENTRY_YES_TOP_SPREAD_MAX)
    if entry_yes_top_require_spread_max and not direct_entry:
        if spread_val_f is None or not math.isfinite(spread_val_f) or spread_val_f > entry_yes_top_spread_max + s.TIME_EDGE_Q_EPS:
            reasons.append('spread_max')
    entry_lag_enabled = cfg_bool(cfg, 'entry_lag_enabled', True)
    if entry_lag_enabled and not direct_entry:
        lag_val = s.to_float(row.get('lag_yes'))
        if lag_val is None or not math.isfinite(lag_val) or lag_val > (s.MINUTE_EDGE_L_MAX + s.MINUTE_EDGE_L_EPS):
            reasons.append('lag')

    return EvalResult(not reasons, reasons)


def eval_main(cfg: Dict[str, Any], row: Dict[str, Any], side: str) -> EvalResult:
    reasons: List[str] = []
    side = side.upper()
    q_prob = s.to_float(row.get('q_entry') if side == 'YES' else row.get('q_entry_no'))
    ask = s.to_float(row.get('best_ask') if side == 'YES' else row.get('best_ask_no'))
    spread = s.to_float(row.get('spread') if side == 'YES' else row.get('spread_no'))
    edge_score = s.to_float(row.get('edge_score_yes') if side == 'YES' else row.get('edge_score_no'))

    if q_prob is None or not math.isfinite(q_prob):
        reasons.append('q_missing')
        return EvalResult(False, reasons)
    if ask is None or not math.isfinite(ask):
        reasons.append('ask_missing')
        return EvalResult(False, reasons)

    if cfg_bool(cfg, 'entry_require_q_range', True):
        qmin = cfg_float(cfg, 'entry_q_min', 0)
        qmax = cfg_float(cfg, 'entry_q_max', 1)
        if qmin is not None and q_prob < qmin - s.TIME_EDGE_Q_EPS:
            reasons.append('q_range')
        if qmax is not None and q_prob > qmax + s.TIME_EDGE_Q_EPS:
            reasons.append('q_range')

    if cfg_bool(cfg, 'entry_require_q_band', False):
        bands = []
        for i in (1, 2, 3):
            mn = cfg.get(f'entry_q_band_{i}_min')
            mx = cfg.get(f'entry_q_band_{i}_max')
            if mn is None or mx is None:
                continue
            bands.append((float(mn), float(mx)))
        if not bands or not any(mn <= q_prob <= mx for mn, mx in bands):
            reasons.append('q_band')

    if side == 'YES' and cfg_bool(cfg, 'entry_require_ask_range', True):
        amin = cfg_float(cfg, 'entry_ask_min', 0)
        amax = cfg_float(cfg, 'entry_ask_max', 1)
        if amin is not None and ask < amin - s.TIME_EDGE_Q_EPS:
            reasons.append('ask_range')
        if amax is not None and ask > amax + s.TIME_EDGE_Q_EPS:
            reasons.append('ask_range')
    if side == 'NO' and cfg_bool(cfg, 'entry_require_no_ask_range', True):
        amin = cfg_float(cfg, 'no_ask_min', 0)
        amax = cfg_float(cfg, 'no_ask_max', 1)
        if amin is not None and ask < amin - s.TIME_EDGE_Q_EPS:
            reasons.append('ask_range')
        if amax is not None and ask > amax + s.TIME_EDGE_Q_EPS:
            reasons.append('ask_range')

    if cfg_bool(cfg, 'entry_require_auto_max_ask', True):
        max_ask = cfg_float(cfg, 'auto_max_ask', 1)
        if max_ask is not None and ask > max_ask + s.TIME_EDGE_Q_EPS:
            reasons.append('auto_max_ask')

    if cfg_bool(cfg, 'entry_require_nonneg_spread', True):
        if spread is None or not math.isfinite(spread) or spread < 0:
            reasons.append('spread_nonneg')
    if cfg_bool(cfg, 'entry_require_spread_max', True):
        max_spread = cfg_float(cfg, 'entry_spread_max', 1)
        if spread is None or not math.isfinite(spread) or (max_spread is not None and spread > max_spread + s.TIME_EDGE_Q_EPS):
            reasons.append('spread_max')

    if cfg_bool(cfg, 'entry_require_signal_edge', True):
        edge_min = cfg_float(cfg, 'entry_signal_edge_min', 0)
        if (q_prob - ask) <= (edge_min or 0):
            reasons.append('signal_edge_min')

    if cfg_bool(cfg, 'entry_require_edge_band', False):
        bands = []
        for i in (1, 2, 3):
            mn = cfg.get(f'entry_edge_band_{i}_min')
            mx = cfg.get(f'entry_edge_band_{i}_max')
            if mn is None or mx is None:
                continue
            bands.append((float(mn), float(mx)))
        if not bands or not any(mn <= (q_prob - ask) <= mx for mn, mx in bands):
            reasons.append('edge_band')

    if edge_score is None or not math.isfinite(edge_score):
        reasons.append('edge_score_missing')
    else:
        if cfg_bool(cfg, 'entry_require_edge_score_range', False):
            mn = cfg_float(cfg, 'edge_score_never_min', 0)
            mx = cfg_float(cfg, 'edge_score_never_max', 1e9)
            if (mn is not None and edge_score < mn) or (mx is not None and edge_score > mx):
                reasons.append('edge_score_never')
        if cfg_bool(cfg, 'entry_require_edge_score_buy_range', True):
            mn = cfg_float(cfg, 'edge_score_buy_min', 0)
            mx = cfg_float(cfg, 'edge_score_buy_max', 1e9)
            if (mn is not None and edge_score < mn) or (mx is not None and edge_score > mx):
                reasons.append('edge_score_buy')

    # depth check is intentionally omitted here (need size calc); assume ok if other filters pass.
    return EvalResult(not reasons, reasons)


def backfill():
    cfg = load_config()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute(
        """
        select id, order_id, slug, event_date, outcome, side, action, created_at, strategy
        from auto_orders
        where (strategy is null or strategy = '')
          and action in ('entry', 'exit')
        order by created_at asc
        """
    )
    rows = cur.fetchall()
    updated = 0
    unknown = 0
    for r in rows:
        action = (r['action'] or '').lower()
        if action == 'exit':
            update_auto_order_strategy_by_id(cur, row_id=r['id'], strategy='exit')
            updated += 1
            continue
        slug = r['slug']
        event_date = r['event_date']
        created_at = r['created_at']
        outcome = r['outcome']
        side = (r['side'] or 'YES').upper()

        cur.execute(
            """
            select run_date, event_date, payload_json
            from event_runs
            where slug=? and event_date=? and created_at <= ?
            order by created_at desc limit 1
            """,
            (slug, event_date, created_at),
        )
        ev = cur.fetchone()
        if not ev:
            update_auto_order_strategy_by_id(cur, row_id=r['id'], strategy='unknown')
            unknown += 1
            continue
        payload = json.loads(ev['payload_json'])
        rows_payload = payload.get('rows') or []
        row_map = {str(x.get('outcome') or ''): x for x in rows_payload}
        row_payload = row_map.get(outcome)
        if not row_payload:
            update_auto_order_strategy_by_id(cur, row_id=r['id'], strategy='unknown')
            unknown += 1
            continue
        run_date = ev['run_date']
        yes_top_ok = eval_yes_top(cfg, payload, rows_payload, run_date, event_date, outcome).ok
        main_ok = eval_main(cfg, row_payload, side).ok
        if yes_top_ok and not main_ok:
            strategy = 'yes_top'
        elif main_ok and not yes_top_ok:
            strategy = 'main'
        elif yes_top_ok and main_ok:
            # yes_top has higher priority in candidate sorting
            strategy = 'yes_top'
        else:
            strategy = 'unknown'
            unknown += 1
        update_auto_order_strategy_by_id(cur, row_id=r['id'], strategy=strategy)
        updated += 1

    conn.commit()
    conn.close()
    print(f"backfilled {updated} rows, unknown={unknown}")


if __name__ == '__main__':
    backfill()
