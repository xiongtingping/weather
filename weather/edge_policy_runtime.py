from __future__ import annotations

from typing import Any, Dict, List, Optional
import hashlib
import math
import os


def _to_float(val: Any) -> Optional[float]:
    try:
        num = float(val)
    except Exception:
        return None
    if not math.isfinite(num):
        return None
    return float(num)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _stable_ratio(key: str) -> float:
    text = str(key or "").encode("utf-8", errors="ignore")
    digest = hashlib.sha1(text).hexdigest()
    try:
        value = int(digest[:8], 16)
    except Exception:
        value = 0
    return float(value) / float(0xFFFFFFFF)


def load_edge_policy_from_env() -> Dict[str, Any]:
    mode = str(os.environ.get("PM_EDGE_POLICY_MODE") or "shadow").strip().lower()
    if mode not in {"off", "shadow", "canary", "enforced"}:
        mode = "shadow"
    canary_ratio = _to_float(os.environ.get("PM_EDGE_POLICY_CANARY_RATIO"))
    if canary_ratio is None:
        canary_ratio = 0.20
    canary_ratio = max(0.0, min(1.0, canary_ratio))
    signal_lcb_min = _to_float(os.environ.get("PM_EDGE_POLICY_SIGNAL_LCB_MIN"))
    if signal_lcb_min is None:
        signal_lcb_min = 0.0
    path_lcb_min = _to_float(os.environ.get("PM_EDGE_POLICY_PATH_LCB_MIN"))
    if path_lcb_min is None:
        path_lcb_min = 0.10
    edge_lcb_min = _to_float(os.environ.get("PM_EDGE_POLICY_EDGE_LCB_MIN"))
    if edge_lcb_min is None:
        edge_lcb_min = 0.0
    require_exec_audit = _env_bool("PM_EDGE_POLICY_REQUIRE_EXEC_AUDIT", True)
    require_v2 = _env_bool("PM_EDGE_POLICY_REQUIRE_V2", True)
    return {
        "enabled": _env_bool("PM_EDGE_POLICY_ENABLED", True),
        "mode": mode,
        "canary_ratio": float(canary_ratio),
        "signal_lcb_min": float(signal_lcb_min),
        "path_lcb_min": float(path_lcb_min),
        "edge_lcb_min": float(edge_lcb_min),
        "require_exec_audit": bool(require_exec_audit),
        "require_v2": bool(require_v2),
    }


def evaluate_tradeability(
    *,
    policy: Dict[str, Any],
    key: str,
    signal_lcb: Any,
    path_lcb: Any,
    edge_lcb: Any,
    v2_value: Any = None,
    exec_audit_gate_pass: Optional[bool] = None,
) -> Dict[str, Any]:
    cfg = policy if isinstance(policy, dict) else {}
    if not bool(cfg.get("enabled", True)):
        return {
            "mode": "off",
            "enforce": False,
            "pass": True,
            "blocked": False,
            "shadow_block": False,
            "reasons": [],
        }
    mode = str(cfg.get("mode") or "shadow").strip().lower()
    if mode not in {"off", "shadow", "canary", "enforced"}:
        mode = "shadow"
    signal_lcb_f = _to_float(signal_lcb)
    path_lcb_f = _to_float(path_lcb)
    edge_lcb_f = _to_float(edge_lcb)
    v2_f = _to_float(v2_value)
    signal_lcb_min = _to_float(cfg.get("signal_lcb_min"))
    if signal_lcb_min is None:
        signal_lcb_min = 0.0
    path_lcb_min = _to_float(cfg.get("path_lcb_min"))
    if path_lcb_min is None:
        path_lcb_min = 0.10
    edge_lcb_min = _to_float(cfg.get("edge_lcb_min"))
    if edge_lcb_min is None:
        edge_lcb_min = 0.0
    reasons: List[str] = []
    if signal_lcb_f is None:
        reasons.append("signal_lcb_missing")
    elif signal_lcb_f < signal_lcb_min:
        reasons.append("signal_lcb_below_min")
    if path_lcb_f is None:
        reasons.append("path_lcb_missing")
    elif path_lcb_f < path_lcb_min:
        reasons.append("path_lcb_below_min")
    if edge_lcb_f is None:
        reasons.append("edge_lcb_missing")
    elif edge_lcb_f < edge_lcb_min:
        reasons.append("edge_lcb_below_min")
    if bool(cfg.get("require_v2", True)):
        if v2_f is None:
            reasons.append("v2_missing")
    if bool(cfg.get("require_exec_audit", True)):
        if exec_audit_gate_pass is not True:
            reasons.append("exec_audit_not_pass")

    enforce = False
    sampled = False
    if mode == "enforced":
        enforce = True
    elif mode == "canary":
        sampled = _stable_ratio(str(key or "")) < float(cfg.get("canary_ratio", 0.20) or 0.0)
        enforce = bool(sampled)
    blocked = bool(enforce and reasons)
    shadow_block = bool((not enforce) and reasons and mode in {"shadow", "canary"})
    return {
        "mode": mode,
        "enforce": bool(enforce),
        "canary_sampled": bool(sampled),
        "pass": len(reasons) == 0,
        "blocked": blocked,
        "shadow_block": shadow_block,
        "reasons": reasons,
        "reasons_csv": ",".join(reasons),
        "signal_lcb_min": float(signal_lcb_min),
        "path_lcb_min": float(path_lcb_min),
        "edge_lcb_min": float(edge_lcb_min),
    }

