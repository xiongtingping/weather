"""The dashboard's write path for the three live-preparation knobs.

Serves 127.0.0.1 only.  GET /api/state returns the three control files and
the contract facts the page's checklist renders; the POST endpoints below
move exactly one knob each, atomically (tmp + rename), inside hard bounds,
and append every change to an audit trail.  The page works without this
server -- controls simply render disabled with the command to start it.

What the live toggle does, stated here because it changed and the old
sentence is quoted all over this repo: until 2026-08-30 the worker was
contract-locked to production_no_live three times over, so arming placed no
order and could not.  The go-live migration removed that lock.  Arming now
starts live trading.

So the switch owns both operator gates rather than one of them.  Arming
writes the intent flag and lifts data/live_paused.flag; disarming places the
pause flag back before it records the intent.  One switch, both directions
(operator ruling, 2026-09-02): a go that leaves a second file to remember is
not a go, and the reader of a switch labelled ARMED is entitled to believe
money can move.

POST /api/kelly-fraction  {"value": 0.25}   -> kelly_fraction (凯利系数, charter 141)
POST /api/exposure        {"value": 0.80}   -> portfolio_risk_fraction (总敞口上限:
                  the share of the real account the basket's own open
                  exposure -- filled cost on unsettled station-days plus
                  resting buys, other lanes not counted -- may be; charter 223)
POST /api/station-sizing  {"value": 0.065}  -> station_day_sizing_fraction (单站日定注)
POST /api/station-ceiling {"value": 0.10}   -> station_day_ceiling_fraction (单站日上限)
POST /api/kelly  is retired: it wrote the exposure bound under Kelly's name, and a
                  board still open from before 2026-09-17 must fail loudly
                  rather than move the wrong dial.
POST /api/budget  {"value": 515}           -> target_remaining_budget_usdc
POST /api/lane-stake {"lane": "tail_short", "value": 10} -> one lane's per-trade stake_usdc
                  (秤砣线 charter 146; 远尾做空 H1 and Proper 下注线 charter 252)
POST /api/tiebreak-stake is retired (charter 252): /api/lane-stake took it over, and a
                  board still open from before must fail loudly rather than move nothing.
POST /api/lane    {"lane": "basket", "enabled": false} -> one strategy lane's switch (charter 146);
                  the board draws four of them -- 篮子, Proper 下注线, 秤砣线, 同心线 (charters 229, 231)
POST /api/live    {"enabled": true, "confirm": "LIVE", "by": "..."}
POST /api/allowlist {"stations": ["ZBAA", ...]}
POST /api/station-bar     {"station": "KSEA", "barred": true}
POST /api/station-waiver  {"station": "LIMC", "waived": true}
POST /api/station-hold    {"station": "KDAL", "held": true}
POST /api/station-bulk    {"action": "waive_all" | "bar_all"}
POST /api/manual-basket-order {"confirm":"BUY","station":"ZUUU","event_date":...,"batch_id":...,
                  "mode":"taker|maker|manual","legs":[{"bucket","shares","limit_price"}]}
                  -> the operator's own basket through the local gateway (board origin only)
GET  /api/station-day?station=ZUUU[&force=1|&token_only=1] -> one station's today and day-ahead
                  distributions, market, TAF/METAR (read-only; the board's 单站日 panel)

The three per-station ones are the station gate, one click each: 禁入 (the bar), 豁免
(trade before the evidence is in) and 待观察 (hold a qualified station back).
The bulk one moves every station at once (user, 2026-09-16): all open on a
waiver, or all barred.
All three are operator rulings and mutually exclusive, and the automatic
layer never touches a station carrying one.  Until 2026-09-02 the bar was
editable only by hand in the JSON, which is why it moved four times in one
day through a text editor and why one of those days is recorded in the file
as a same-day round trip.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# The derivation this panel writes for, owned by the package because the
# send reads it too (bayes_basket_target.order_submission).  A copy here
# would be a second answer to "who may trade", and the board proved on
# 2026-08-31 how quickly two copies drift.
from bayes_basket_target import live_station_scope as scope_rules  # noqa: E402
POLICY_PATH = ROOT / "config/trading/portfolio_capital_policy_v1.json"
RUNTIME_PATH = ROOT / "config/bayes_basket_target/no_live_runtime_v1.json"
# The live twin.  Production in production_live mode reads this file, not the
# no-live one, so a budget knob that wrote only the no-live file would be a
# dial connected to nothing -- the operator turns it, the number changes, and
# the running system never sees it.  Both are written whenever both exist.
LIVE_RUNTIME_PATH = ROOT / "config/bayes_basket_target/live_runtime_v1.json"
INTENT_PATH = ROOT / "config/trading/live_intent_v1.json"
EXCLUSION_PATH = ROOT / "config/trading/live_station_exclusions_v1.json"
AUDIT_PATH = ROOT / "data" / "live_control_audit.jsonl"
# Charter 141: read-only, for the station-day numbers the board shows.
SSOT_PATH = ROOT / "data" / "bayes_basket_target_ssot.db"
# Charter 141: the three capital dials, the file the resident re-reads before
# every compile (``basket_kernel.delta_v2.refresh_capital_layer``).
CAPITAL_LAYER_PATH = ROOT / "config/trading/capital_layer_v1.json"
# Charter 146: one switch per strategy lane, and the tiebreak lane's stake
# (user ruling 2026-09-18: 前端增加可自行调整每笔预算).  Read and written
# through ``bayes_basket_target.strategy_lanes`` so the board, the daemon and
# the submitter cannot hold different opinions about the file.
STRATEGY_LANES_PATH = ROOT / "config/trading/strategy_lanes_v1.json"
# The gateway's admission gate.  clob_order_service.mjs checks this file at
# every place-order, before any idempotency write, signing or exchange POST,
# and it stops every lane without exception.  The switch below owns it; the
# file stays the mechanism so a manual `touch` is still a working e-stop and
# so the install and cutover scripts that place it keep working unchanged.
PAUSE_FLAG_PATH = ROOT / "data" / "live_paused.flag"
PORT = 8788

# Charter 141: this knob is the whole-book exposure bound on the real
# account (the non-cash share: positions plus resting buys), so it may go to
# the whole account.
KELLY_BOUNDS = (0.05, 1.00)
KELLY_STEP_MAX = 1.00
EXPOSURE_BOUNDS = (0.05, 1.00)
BUDGET_BOUNDS = (0.0, 2000.0)
# The budget moves in whole multiples of 5 USDC (operator ruling,
# 2026-09-16).  Held here rather than only on the board's stepper, so a
# value that did not come from the board cannot put an odd cent back.
BUDGET_MULTIPLE = 5.0
STATION_RE = re.compile(r"^[A-Z0-9]{3,6}$")
_EXECUTION_MODE: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_atomic(path: Path, payload: dict) -> None:
    handle, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name, suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _pause_present() -> bool:
    return PAUSE_FLAG_PATH.is_file()


def _place_pause() -> bool:
    """Stop the gateway.  Returns whether this call is what stopped it."""

    if _pause_present():
        return False
    PAUSE_FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
    PAUSE_FLAG_PATH.touch()
    return True


def _lift_pause() -> bool:
    """Let the gateway admit orders.  Returns whether a flag was removed."""

    if not _pause_present():
        return False
    PAUSE_FLAG_PATH.unlink()
    return True


def _audit(entry: dict) -> None:
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(
            {"at_utc": _now(), **entry}, ensure_ascii=False,
        ) + "\n")


def _execution_mode() -> str:
    """The mode the production lane is compiled for, cached for the process.

    Read from the compiled constant rather than from a config file: the
    files describe two contracts that both exist on disk, and which one is
    in force is a property of the code.  Import failure answers "unknown"
    and the caller falls back to the no-live file -- the conservative
    direction, and the payload names the file it read so a reader can tell
    the fallback from the answer.
    """

    global _EXECUTION_MODE
    if _EXECUTION_MODE is None:
        try:
            from bayes_basket_target.production import (  # noqa: PLC0415
                PRODUCTION_EXECUTION_MODE,
            )
            _EXECUTION_MODE = str(PRODUCTION_EXECUTION_MODE)
        except Exception:  # noqa: BLE001
            _EXECUTION_MODE = "unknown"
    return _EXECUTION_MODE


def state() -> dict:
    policy = _read(POLICY_PATH)
    runtime = _read(RUNTIME_PATH)
    intent = _read(INTENT_PATH)
    paused = _pause_present()
    _scope = live_stations()
    # The contract the running mode selects.  This block read the no-live
    # file unconditionally, so after the go-live migration minted the live
    # twin it reported live_submit_authorized=false about a system whose
    # contract said true -- and the arming hover text on the board is
    # computed from exactly these three fields.  The two files carry the
    # same budget, which is why the same mistake went unnoticed elsewhere
    # for a week; naming the file read is the part that stops it repeating.
    mode = _execution_mode()
    contract_path = (
        LIVE_RUNTIME_PATH
        if mode == "production_live" and LIVE_RUNTIME_PATH.is_file()
        else RUNTIME_PATH
    )
    contract = _read(contract_path)
    contracts = {
        "execution_mode": mode,
        "runtime_file": contract_path.name,
        "live_submit_authorized": contract.get("live_submit_authorized"),
        "places_orders": contract.get("places_orders"),
        "calls_order_gateway": contract.get("calls_order_gateway"),
        "policy_authority_scope": policy.get("authority_scope"),
    }
    capital = _read_capital_layer()
    return {
        "schema": "live_control_state_v1",
        "at_utc": _now(),
        # Charter 141: the kernel's Kelly fraction, a real dial now.
        "kelly": {
            "kelly_fraction": capital["kelly_fraction"],
            "bounds": KELLY_BOUNDS,
            "step": 0.05,
        },
        # The whole-book exposure bound (this key was "kelly" until
        # 2026-09-17, which is what it was never).
        "exposure": {
            "portfolio_risk_fraction": policy["portfolio_risk_fraction"],
            "bounds": EXPOSURE_BOUNDS,
            "step": 0.05,
        },
        "station_sizing": {
            "station_day_sizing_fraction": capital["station_day_sizing_fraction"],
            "bounds": CAPITAL_BOUNDS["station_day_sizing_fraction"],
            "step": 0.005,
        },
        "station_ceiling": {
            "station_day_ceiling_fraction": capital["station_day_ceiling_fraction"],
            "bounds": CAPITAL_BOUNDS["station_day_ceiling_fraction"],
            "step": 0.01,
        },
        "budget": {
            # From the same mode-selected file as the contract facts above.
            # set_budget writes both twins, so they normally agree; when a
            # mirror write has failed, the number production will actually
            # spend is the one to show.
            "target_remaining_budget_usdc": contract.get(
                "target_remaining_budget_usdc",
                runtime["target_remaining_budget_usdc"],
            ),
            "bounds": BUDGET_BOUNDS,
            "step": BUDGET_MULTIPLE,
            "multiple": BUDGET_MULTIPLE,
        },
        # Charter 141: the station-day's two numbers, read-only.  The
        # per-station dollar dial they replace never bounded spend.
        "capital_layer": _capital_layer_state(),
        # Charter 146/252: every dialled lane's per-trade stake, and the lane
        # switches beside them.
        "lane_stakes": _lane_stakes_state(),
        "strategy_lanes": _strategy_lanes_state(),
        "live": {
            "enabled": intent["live_enabled"],
            "armed_by": intent.get("armed_by"),
            "armed_at_utc": intent.get("armed_at_utc"),
            # Both halves of the one switch.  They can still disagree --
            # `touch data/live_paused.flag` is a working e-stop and the
            # gateway installer places one -- so the board gets the pair and
            # the derived answer, never one of the two on its own.  A reader
            # asking "is it sending" gets that question answered here.
            "submission_paused": paused,
            "submitting": bool(
                intent["live_enabled"]
                and not paused
                and contracts["live_submit_authorized"] is True
            ),
            # The derived set, not the stored one: a reader asking what
            # trades has to get the answer the system would act on, and in
            # all_except_barred mode the stored list is not that answer.
            "station_scope": _scope["scope"],
            "station_allowlist": _scope["stations"],
            "station_probation": _scope["probation"],
            # A waived station is live without having qualified, so the
            # reason has to travel with the answer -- otherwise the only way
            # to explain why a three-settlement station trades is to open the
            # config and know the mechanism exists.
            "station_probation_waivers": _scope.get("probation_waivers") or [],
            # The third switch.  A held station is out of the batch by
            # somebody's decision rather than by its settlement count, and a
            # board that could not tell the two apart would draw a hand
            # ruling as arithmetic.
            "station_probation_holds": _scope.get("probation_holds") or [],
            "station_exclusions": _scope["barred"],
            # Which of the two bar lists a station sits on.  The switch
            # behaves differently on them -- releasing an automatic bar has
            # to record a ruling as well, or the sweep undoes the click --
            # so the board has to be able to draw the difference.
            "station_bar_sources": scope_rules.operator_bar_sources(
                EXCLUSION_PATH
            ),
            "minimum_settlements_to_qualify": _scope["minimum_settlements"],
            "pinned_allowlist": intent.get("station_allowlist") or [],
        },
        "contracts": contracts,
    }


def set_exposure(value: float) -> dict:
    """The whole-book exposure bound (charter 141): positions plus resting buys
    may be at most this share of the real account."""

    if not EXPOSURE_BOUNDS[0] <= value <= EXPOSURE_BOUNDS[1]:
        raise ValueError(
            f"exposure out of bounds {EXPOSURE_BOUNDS}: {value}"
        )
    policy = _read(POLICY_PATH)
    before = float(policy["portfolio_risk_fraction"])
    policy["portfolio_risk_fraction"] = round(float(value), 4)
    _write_atomic(POLICY_PATH, policy)
    _audit({
        "knob": "portfolio_risk_fraction",
        "before": before, "after": policy["portfolio_risk_fraction"],
    })
    return state()


def _capital_kernel():
    from bayes_basket_target.basket_kernel import delta_v2  # noqa: PLC0415

    return delta_v2


def _read_capital_layer() -> dict:
    """The dials as the file says them, else the kernel's defaults."""

    kernel = _capital_kernel()
    try:
        raw = _read(CAPITAL_LAYER_PATH)
        if raw.get("schema") != kernel.CAPITAL_LAYER_SCHEMA:
            raise ValueError("capital_layer_schema_invalid")
        return {**kernel.validate_capital_layer(raw), "source": "config"}
    except Exception:  # noqa: BLE001 -- a missing or bad file reads as defaults
        return {**kernel.CAPITAL_LAYER_DEFAULTS, "source": "defaults"}


def _set_capital_dial(key: str, value: float) -> dict:
    kernel = _capital_kernel()
    current = _read_capital_layer()
    before = current[key]
    proposed = {
        k: current[k] for k in kernel.CAPITAL_LAYER_DEFAULTS
    }
    proposed[key] = round(float(value), 4)
    try:
        values = kernel.validate_capital_layer(proposed)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(str(exc)) from exc
    payload = {
        "schema": kernel.CAPITAL_LAYER_SCHEMA,
        **values,
        "updated_at_utc": _now(),
    }
    if CAPITAL_LAYER_PATH.is_file():
        existing = _read(CAPITAL_LAYER_PATH)
        if existing.get("rationale"):
            payload["rationale"] = existing["rationale"]
    _write_atomic(CAPITAL_LAYER_PATH, payload)
    _audit({"knob": key, "before": before, "after": values[key]})
    return state()


def _lanes_module():
    from bayes_basket_target import strategy_lanes  # noqa: PLC0415

    return strategy_lanes


def _read_strategy_lanes() -> dict:
    return _lanes_module().load_strategy_lanes(STRATEGY_LANES_PATH)


def _strategy_lanes_state() -> dict:
    doc = _read_strategy_lanes()
    lanes = {
        name: {"enabled": bool(lane.get("enabled") is True)}
        for name, lane in doc["lanes"].items()
    }
    for name, stages in _lane_opening_stages(doc).items():
        lanes[name]["opening_stages"] = stages
    return {
        "source": doc.get("source"),
        "updated_at_utc": doc.get("updated_at_utc"),
        "lanes": lanes,
    }


def _lane_opening_stages(doc: dict) -> dict:
    """Charter 229: the stages the basket and the tiebreak lane may open on,
    read where each rule lives -- the basket's in the compile, the tiebreak
    lane's in its lane file -- so the board's switches say what they reach
    without restating a rule.  A lane whose answer cannot be read is left
    out rather than guessed."""

    lanes = _lanes_module()
    order = lanes.KNOWN_STAGES
    out: dict = {}
    try:
        from bayes_basket_target.delta_v2_runtime import (  # noqa: PLC0415
            BASKET_BUYING_STAGES,
        )
        out[lanes.LANE_BASKET] = [s for s in order if s in BASKET_BUYING_STAGES]
    except Exception:  # noqa: BLE001
        pass
    try:
        tiebreak = lanes.opening_stages(doc["lanes"][lanes.LANE_TIEBREAK])
        out[lanes.LANE_TIEBREAK] = [s for s in order if s in tiebreak]
    except Exception:  # noqa: BLE001
        pass
    # The lanes whose stages live in their own lane file (charters 231, 234).
    for name in (lanes.LANE_PROPER_BET, lanes.LANE_TAIL_SHORT):
        try:
            stages = lanes.opening_stages(doc["lanes"][name])
            out[name] = [s for s in order if s in stages]
        except Exception:  # noqa: BLE001
            pass
    return out


def _lane_stakes_state() -> dict:
    """Each dialled lane's stake as the lane will read it, with its bounds
    (``strategy_lanes.STAKE_DIALS``)."""

    lanes = _lanes_module()
    doc = _read_strategy_lanes()
    return {
        name: {
            "stake_usdc": doc["lanes"][name].get("stake_usdc"),
            "bounds": list(dial["bounds"]),
            "step": dial["step"],
            "enabled": doc["lanes"][name].get("enabled") is True,
        }
        for name, dial in lanes.STAKE_DIALS.items()
    }


def _write_strategy_lanes(mutate) -> None:
    """Rewrite the lane file through ``mutate(document)``, keeping every
    field the operator or a charter wrote there.  An unreadable file is not
    edited into shape: the operator sees it refuse and repairs it."""

    lanes = _lanes_module()
    current = _read_strategy_lanes()
    if current.get("source") == "unreadable":
        raise ValueError(f"strategy_lanes_unreadable:{current.get('reason')}")
    try:
        document = _read(STRATEGY_LANES_PATH)
    except Exception:  # noqa: BLE001 -- no file yet: start from the defaults
        document = {}
    if not isinstance(document.get("lanes"), dict):
        document["lanes"] = {}
    for name in lanes.KNOWN_LANES:
        document["lanes"].setdefault(name, {})
        for key, value in current["lanes"][name].items():
            document["lanes"][name].setdefault(key, value)
    document.setdefault("schema", lanes.SCHEMA)
    mutate(document)
    document["updated_at_utc"] = _now()
    _write_atomic(STRATEGY_LANES_PATH, document)


def set_lane_stake(lane: str, value: float) -> dict:
    """POST /api/lane-stake {"lane": "tail_short", "value": 10}: one dialled
    lane's per-trade stake."""

    lanes = _lanes_module()
    name = str(lane or "").strip()
    stake = lanes.validate_lane_stake(name, value)
    before = _lane_stakes_state()[name]["stake_usdc"]

    def mutate(document: dict) -> None:
        document["lanes"][name]["stake_usdc"] = stake

    _write_strategy_lanes(mutate)
    _audit({"knob": f"{name}_stake_usdc", "before": before, "after": stake})
    return state()


def set_lane_enabled(lane: str, enabled: bool, by: str = "") -> dict:
    """POST /api/lane {"lane": "basket", "enabled": false}: one lane's switch."""

    lanes = _lanes_module()
    name = str(lane or "").strip()
    if name not in lanes.KNOWN_LANES:
        raise ValueError(f"strategy_lane_unknown:{name}")
    before = _strategy_lanes_state()["lanes"][name]["enabled"]

    def mutate(document: dict) -> None:
        document["lanes"][name]["enabled"] = bool(enabled)
        document["lanes"][name][
            "paused_at_utc" if not enabled else "enabled_at_utc"
        ] = _now()

    _write_strategy_lanes(mutate)
    _audit({"knob": f"strategy_lane:{name}", "before": before, "after": bool(enabled), "by": str(by or "")})
    return state()


def set_kelly_fraction(value: float) -> dict:
    return _set_capital_dial("kelly_fraction", value)


def set_station_sizing(value: float) -> dict:
    return _set_capital_dial("station_day_sizing_fraction", value)


def set_station_ceiling(value: float) -> dict:
    return _set_capital_dial("station_day_ceiling_fraction", value)


def set_budget(value: float) -> dict:
    if not BUDGET_BOUNDS[0] <= value <= BUDGET_BOUNDS[1]:
        raise ValueError(
            f"budget out of bounds {BUDGET_BOUNDS}: {value}"
        )
    units = float(value) / BUDGET_MULTIPLE
    if abs(units - round(units)) > 1e-9:
        raise ValueError(
            f"budget must be a whole multiple of {BUDGET_MULTIPLE:g}: {value}"
        )
    runtime = _read(RUNTIME_PATH)
    before = float(runtime["target_remaining_budget_usdc"])
    runtime["target_remaining_budget_usdc"] = round(float(value), 2)
    _write_atomic(RUNTIME_PATH, runtime)
    if LIVE_RUNTIME_PATH.is_file():
        live_runtime = _read(LIVE_RUNTIME_PATH)
        live_runtime["target_remaining_budget_usdc"] = runtime[
            "target_remaining_budget_usdc"
        ]
        _write_atomic(LIVE_RUNTIME_PATH, live_runtime)
    _audit({
        "knob": "target_remaining_budget_usdc",
        "before": before, "after": runtime["target_remaining_budget_usdc"],
        "wrote_live_twin": LIVE_RUNTIME_PATH.is_file(),
    })
    return state()


CAPITAL_BOUNDS = {
    "station_day_sizing_fraction": (0.005, 0.25),
    "station_day_ceiling_fraction": (0.01, 0.50),
}


def _capital_layer_state() -> dict:
    """Charter 141's station-day fractions and the latest real account read."""

    out: dict = {}
    try:
        capital = _read_capital_layer()
        out.update({
            "rule": _capital_kernel().CAPITAL_LAYER_RULE_VERSION,
            "kelly_fraction": capital["kelly_fraction"],
            "sizing_fraction": capital["station_day_sizing_fraction"],
            "ceiling_fraction": capital["station_day_ceiling_fraction"],
            "source": capital["source"],
        })
    except Exception as exc:  # noqa: BLE001 -- a state read never fails the page
        out["error"] = f"{type(exc).__name__}:{exc}"[:160]
    if not Path(SSOT_PATH).is_file():
        return out
    try:
        import sqlite3  # noqa: PLC0415

        conn = sqlite3.connect(
            f"file:{SSOT_PATH}?mode=ro",
            uri=True, timeout=5.0,
        )
        try:
            row = conn.execute(
                "SELECT observed_at_utc,cash_usdc,positions_value_usdc,"
                "equity_usdc,resting_buy_usdc FROM basket_target_account_equity"
                " ORDER BY observed_at_utc DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        if row is not None:
            out["account"] = {
                "observed_at_utc": row[0], "cash_usdc": row[1],
                "positions_value_usdc": row[2], "equity_usdc": row[3],
                "resting_buy_usdc": row[4],
            }
    except Exception as exc:  # noqa: BLE001
        out["account_error"] = f"{type(exc).__name__}:{exc}"[:160]
    return out


def set_live(enabled: bool, confirm: str, by: str) -> dict:
    """Move both operator gates at once, in the order that cannot leak.

    Two gates stop a live order and both are the operator's: the intent flag
    this file writes, and data/live_paused.flag, which the gateway reads at
    every place-order.  Until 2026-09-02 the switch moved only the first, so
    ARMED meant "armed, and now go delete a file" -- and the reverse asymmetry
    was worse, because a disarm left the gateway open and relied on the worker
    honouring the flag.  Operator ruling: the switch is the whole control.

    The order is the safety property, not an implementation detail.

    * Arming writes the intent first, then lifts the pause.  Between the two
      the system is armed-and-paused, which is stopped.
    * Disarming places the pause first, then writes the intent.  Between the
      two the system is paused-and-armed, which is also stopped.

    Either way the intermediate state is a stopped one, so a crash between
    the two writes cannot leave a gateway that sends without an operator who
    said so.  Lifting the pause is the last thing that happens on the way up
    and the first on the way down.

    The pause flag stays a real file, and the e-stop it has always been: a
    hand `touch` still stops every lane instantly.  What changes is that
    nobody has to remember it to go live -- and if the two ever disagree,
    state() reports both and the board draws the disagreement rather than
    picking one to believe.
    """

    if enabled and confirm != "LIVE":
        raise ValueError("arming live intent requires confirm='LIVE'")
    intent = _read(INTENT_PATH)
    before = bool(intent["live_enabled"])
    paused_before = _pause_present()

    moved = False
    flag_error = ""

    if not enabled:
        # Stop first, record second.  A failure here does not abandon the
        # disarm: the intent flag still has to go down, because the worker
        # reads it and a half-done stop must end on the stopping side.
        try:
            moved = _place_pause()
        except OSError as exc:
            flag_error = f"{type(exc).__name__}: {exc}"

    intent["live_enabled"] = bool(enabled)
    intent["armed_by"] = str(by or "dashboard") if enabled else None
    intent["armed_at_utc"] = _now() if enabled else None
    intent["updated_at_utc"] = _now()
    _write_atomic(INTENT_PATH, intent)

    if enabled:
        try:
            moved = _lift_pause()
        except OSError as exc:
            # Armed but still paused: the old two-key state, which is
            # stopped.  Reported rather than swallowed -- a switch reading
            # ARMED while a flag it could not remove still stops every
            # order is the exact lie this change exists to end, so it must
            # not return a success.
            flag_error = f"{type(exc).__name__}: {exc}"

    _audit({
        "knob": "live_enabled", "before": before,
        "after": intent["live_enabled"], "by": intent["armed_by"],
        "pause_flag_before": paused_before,
        "pause_flag_after": _pause_present(),
        "pause_flag_moved": bool(moved),
        **({"pause_flag_error": flag_error} if flag_error else {}),
    })
    if flag_error:
        raise RuntimeError((
            "intent armed but the gateway pause flag could not be lifted, "
            "so nothing will be sent: "
            if enabled else
            "intent lowered but the gateway pause flag could not be placed, "
            "so only the worker-side stop is in force: "
        ) + flag_error)
    return state()


def excluded_stations() -> set:
    """Stations barred from live trading, read fresh on every call.

    Read rather than cached because the file is the authority and an
    operator may edit it between requests; a stale copy would silently
    re-admit a station somebody just barred.  The rule itself -- two lists,
    one bar, fail open -- lives in ``bayes_basket_target.live_station_scope``
    now, because the send reads it too and a second copy here is how the
    screen and the gateway come to disagree about who is barred.
    """

    return scope_rules.barred_stations(EXCLUSION_PATH)


# Re-exported rather than restated: callers (the board, the tests, the
# migration) import these names from this module, and a second definition is
# a second thing to keep in step.
SCOPE_QUALIFIED = scope_rules.SCOPE_QUALIFIED
SCOPE_ALL_EXCEPT_BARRED = scope_rules.SCOPE_ALL_EXCEPT_BARRED
SCOPE_EXPLICIT = scope_rules.SCOPE_EXPLICIT
SSOT_DB = scope_rules.SSOT_DB
MIN_SETTLEMENTS_TO_QUALIFY = scope_rules.MIN_SETTLEMENTS_TO_QUALIFY


def tradeable_stations() -> set:
    """Every station the model actually produces for, read from the book."""

    return scope_rules.tradeable_stations()


def settlement_counts() -> dict:
    """How much evidence each station has actually produced."""

    return scope_rules.settlement_counts()


def live_stations() -> dict:
    """The three states, derived, with the operator's three switches applied.

    Paths are passed rather than defaulted so a test can point this whole
    control plane at a copy: monkeypatching ``INTENT_PATH`` has to move the
    derivation as well as the writers, or ``state()`` answers about the live
    configuration a test believed it had replaced.
    """

    return scope_rules.live_stations(
        intent_path=INTENT_PATH, exclusion_path=EXCLUSION_PATH,
    )


# --- the three operator switches ---------------------------------------
#
# 禁入 / 豁免 / 待观察, one click each, and all three are operator rulings:
# the automatic layer never touches a station that carries one.  They are
# mutually exclusive at this boundary rather than by a check somewhere
# downstream -- a station that is both barred and waived is a file that
# states two things, and whichever reader wins is an accident.
#
# The bar was hand-editable only until 2026-09-02, which is why it moved
# four times in a day through a text editor and why two of those moves are
# recorded in the file as a same-day round trip.  A ruling that has to be
# typed into JSON is a ruling that gets postponed.


def _station(value: str) -> str:
    station = str(value or "").strip().upper()
    if not STATION_RE.match(station):
        raise ValueError(f"bad station id: {value!r}")
    return station


def _book_rows(stations) -> dict:
    """Each station's own numbers, so a hand-ruled row carries its evidence.

    Same definition as the board's, and the board's is the live book since
    2026-09-16 (user ruling: the paper ledger is a rule comparator, so what
    a station is ruled on is its real money): the settled live baskets'
    stake and P&L summed, and ``settlements`` counts the settled live
    station-days.  A bar written without them would be a name in a list,
    and the next reader would have to re-derive what the operator was
    looking at -- which is exactly the re-litigation the file's
    ``why_not_automatic`` note exists to prevent.  One build for any number
    of stations, so the bulk switch reads the book once.
    """

    empty = {"full_sample_roi": None, "settlements": 0}
    wanted = {str(s) for s in stations}
    out = {station: dict(empty) for station in wanted}
    try:
        conn = sqlite3.connect(f"file:{SSOT_DB}?mode=ro", uri=True, timeout=30.0)
    except sqlite3.Error:
        return out
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        import live_book  # noqa: PLC0415
        rows = live_book.build(conn)["settlements"]
    except (sqlite3.Error, ImportError):
        return out
    finally:
        conn.close()
    totals: dict = {}
    for row in rows:
        station = str(row["station_id"])
        if station not in wanted:
            continue
        slot = totals.setdefault(station, {"stake": 0.0, "pnl": 0.0, "days": set()})
        slot["stake"] += float(row["stake_usdc"] or 0.0)
        slot["pnl"] += float(row["pnl_usdc"] or 0.0)
        slot["days"].add(str(row["event_date"]))
    for station, slot in totals.items():
        out[station] = {
            "full_sample_roi": (
                round(slot["pnl"] / slot["stake"], 3) if slot["stake"] else None
            ),
            "settlements": len(slot["days"]),
        }
    return out


def _book_row(station: str) -> dict:
    return _book_rows([station])[station]


def _city(station: str) -> str:
    """The readable name the file's rows carry.  Falls back to the code.

    The registry's city_label is empty for every station, so the labels live
    beside the other board (selection_market_dashboard).  A station the map
    has not caught up with -- the fleet went 45 to 48 -- gets its own code
    rather than an empty string, because every row in this file is asserted
    to carry a city and a switch must not be able to write one that fails.
    """

    try:
        from selection_market_dashboard import STATION_LABELS
    except ImportError:
        return station
    return str(STATION_LABELS.get(station) or station)


def _clear_intent_switches(station: str) -> dict:
    """A barred station is neither waived nor held -- take both back.

    Silently leaving them would make the file state two things about one
    station, and the reader that wins would be an accident of ordering.
    """

    intent = _read(INTENT_PATH)
    before = {
        "waivers": list(intent.get("station_probation_waivers") or []),
        "holds": list(intent.get("station_probation_holds") or []),
    }
    waivers = [s for s in before["waivers"] if str(s).upper() != station]
    holds = [s for s in before["holds"] if str(s).upper() != station]
    if waivers == before["waivers"] and holds == before["holds"]:
        return {}
    intent["station_probation_waivers"] = waivers
    intent["station_probation_holds"] = holds
    intent["updated_at_utc"] = _now()
    _write_atomic(INTENT_PATH, intent)
    return {
        "cleared_waiver": before["waivers"] != waivers,
        "cleared_hold": before["holds"] != holds,
    }


def set_station_bar(station: str, barred: bool, by: str = "") -> dict:
    """禁入, on or off, as an operator ruling with its evidence attached.

    Turning it OFF on a station the automatic layer barred does two things,
    and the second is the one that makes the switch mean anything: the
    automatic row is removed *and* a release is recorded in
    ``released_stations``, which is what the sweep reads as "the operator
    has ruled on this one".  Without it the next sweep -- minutes away --
    would put the station straight back and the switch would look broken to
    the person who just used it.

    There is no ceiling to make room under any more (operator ruling
    2026-09-02).  A hand ruling used to evict the automatic layer's weakest
    row once the union hit fifteen; barring a station now costs nothing but
    the market it skips, and nothing is released to pay for it.
    """

    station = _station(station)
    doc = _read(EXCLUSION_PATH)
    before = sorted(excluded_stations())
    was_auto = _bar_in(
        doc, station, barred, by, _book_row(station), _now()[:10]
    )
    doc["updated_at_utc"] = _now()
    _write_atomic(EXCLUSION_PATH, doc)
    cleared = _clear_intent_switches(station) if barred else {}
    # A barred station cannot stay pinned: the two lists are asserted
    # disjoint and the migration refuses to run on an overlap.
    intent = _read(INTENT_PATH)
    pinned = [
        s for s in (intent.get("station_allowlist") or [])
        if str(s).strip().upper() != station
    ]
    if barred and pinned != list(intent.get("station_allowlist") or []):
        intent["station_allowlist"] = pinned
        intent["updated_at_utc"] = _now()
        _write_atomic(INTENT_PATH, intent)
    _audit({
        "knob": "station_bar",
        "station": station,
        "before": before,
        "after": sorted(excluded_stations()),
        "barred": bool(barred),
        "was_auto_barred": was_auto,
        "by": by or "dashboard",
        **cleared,
    })
    return state()


def _bar_in(
    doc: dict, station: str, barred: bool, by: str, book: dict, stamp: str,
) -> bool:
    """Move one station's bar rows inside an exclusion doc already read.

    The row arithmetic of the 禁入 switch, apart from the file it lives in,
    so the single switch and the bulk one write exactly the same rows.
    Returns whether the station was on the automatic list.
    """

    operator_rows = list(doc.get("excluded_stations") or [])
    auto_rows = list(doc.get("auto_excluded_stations") or [])
    released_rows = list(doc.get("released_stations") or [])
    was_auto = any(
        str(row.get("station") or "").upper() == station for row in auto_rows
    )
    prior_release = next(
        (row for row in released_rows
         if str(row.get("station") or "").upper() == station),
        None,
    )
    if barred:
        existing = next(
            (row for row in operator_rows
             if str(row.get("station") or "").upper() == station),
            None,
        )
        if existing is None:
            operator_rows.append({
                "station": station,
                "city": _city(station),
                "full_sample_roi": book["full_sample_roi"],
                "settlements": book["settlements"],
                "added_utc": stamp,
                "by": by or "dashboard",
                "note": (
                    "Barred from the board's station switch."
                    + (" Took over a standing automatic bar." if was_auto else "")
                    + (" Re-barred after an earlier release." if prior_release
                       else "")
                ),
                # A station that went out and came back says so on its own
                # row.  EPWA and LTAC made that round trip inside a single
                # day on 2026-08-30, and a list that smooths its own
                # reversals away cannot be scored later: the interesting
                # question about a station is not only whether it is barred
                # but how many times the call has changed.
                **({
                    "released_utc": prior_release.get("released_utc"),
                    "rebarred_utc": stamp,
                } if prior_release else {}),
            })
        else:
            # A station that goes out, comes back and goes out again says so
            # on its own row.  EPWA and LTAC did exactly this inside one day
            # on 2026-08-30, and smoothing it away would leave the list
            # unable to be scored later: the interesting question about a
            # station is not only whether it is barred but how often the call
            # has changed.
            existing["rebarred_utc"] = stamp
            existing.setdefault("city", _city(station))
            existing.setdefault("full_sample_roi", book["full_sample_roi"])
        # One station, one bar list.  An operator ruling takes the row over
        # from the automatic layer rather than sitting beside it -- the two
        # lists are asserted disjoint, and a station in both would let the
        # machine appear to own a person's ruling.
        auto_rows = [
            row for row in auto_rows
            if str(row.get("station") or "").upper() != station
        ]
        released_rows = [
            row for row in released_rows
            if str(row.get("station") or "").upper() != station
        ]
        # Barring is a verdict, and the watchlist is for stations without
        # one, so the station leaves it in the same move.
        watch = doc.get("watchlist_not_excluded")
        if isinstance(watch, dict):
            watch["stations"] = [
                s for s in (watch.get("stations") or [])
                if str(s).strip().upper() != station
            ]
    else:
        operator_rows = [
            row for row in operator_rows
            if str(row.get("station") or "").upper() != station
        ]
        auto_rows = [
            row for row in auto_rows
            if str(row.get("station") or "").upper() != station
        ]
        released_rows = [
            row for row in released_rows
            if str(row.get("station") or "").upper() != station
        ]
        released_rows.append({
            "station": station,
            "city": _city(station),
            "full_sample_roi": book["full_sample_roi"],
            "settlements": book["settlements"],
            "released_utc": stamp,
            "by": by or "dashboard",
            "note": (
                "Released from the board's station switch."
                + (" Lifts a standing automatic bar, which is also what"
                   " keeps the sweep off it." if was_auto else "")
            ),
        })
    doc["excluded_stations"] = operator_rows
    doc["auto_excluded_stations"] = auto_rows
    doc["released_stations"] = released_rows
    return was_auto


def set_all_stations(action: str, by: str = "") -> dict:
    """一键全部开（豁免） / 一键全部禁入: every station at once (user, 2026-09-16).

    ``waive_all`` releases every bar -- operator and automatic rows alike,
    each release recorded as a ruling so the sweep stays off -- lifts every
    hold, and waives every station.  ``bar_all`` writes an operator bar for
    every station not already carrying one and takes back every waiver, hold
    and pinned allowlist entry, the same three things the single switch
    clears.  "Every station" is the production fleet
    (``tradeable_stations``) together with anything a switch already names,
    so no barred, waived or held station is left behind.

    The rows are the single switch's own (``_bar_in``), the book is read
    once, and each file is written once: the exclusions first, then the
    intent, so an interruption between the two leaves the safe half --
    released-but-not-waived trades nothing, and barred-but-still-waived is
    refused at the bar.  One audit line carries every list before and after.
    """

    if action not in ("waive_all", "bar_all"):
        raise ValueError(f"unknown bulk action: {action!r}")
    doc = _read(EXCLUSION_PATH)
    intent = _read(INTENT_PATH)

    def _upper(values) -> list:
        return sorted({
            str(s).strip().upper() for s in (values or []) if str(s).strip()
        })

    barred_before = sorted(excluded_stations())
    operator_before = set(scope_rules.operator_bar_sources(EXCLUSION_PATH)["operator"])
    waivers_before = _upper(intent.get("station_probation_waivers"))
    holds_before = _upper(intent.get("station_probation_holds"))
    pinned_before = _upper(intent.get("station_allowlist"))
    fleet = sorted(
        {_station(s) for s in tradeable_stations()}
        | set(barred_before) | set(waivers_before) | set(holds_before)
    )
    if not fleet:
        raise ValueError("no stations to switch: the fleet read came back empty")
    stamp = _now()[:10]
    if action == "bar_all":
        moving = [s for s in fleet if s not in operator_before]
        books = _book_rows(moving)
        for station in moving:
            _bar_in(doc, station, True, by, books[station], stamp)
        waivers_after: list = []
        holds_after: list = []
        pinned_after: list = []
    else:
        moving = list(barred_before)
        books = _book_rows(moving)
        for station in moving:
            _bar_in(doc, station, False, by, books[station], stamp)
        waivers_after = list(fleet)
        holds_after = []
        pinned_after = pinned_before
    if moving:
        doc["updated_at_utc"] = _now()
        _write_atomic(EXCLUSION_PATH, doc)
    intent["station_probation_waivers"] = waivers_after
    intent["station_probation_holds"] = holds_after
    intent["station_allowlist"] = pinned_after
    intent["updated_at_utc"] = _now()
    _write_atomic(INTENT_PATH, intent)
    _audit({
        "knob": "station_bulk",
        "action": action,
        "stations": fleet,
        "moved": moving,
        "barred_before": barred_before,
        "barred_after": sorted(excluded_stations()),
        "waivers_before": waivers_before,
        "waivers_after": waivers_after,
        "holds_before": holds_before,
        "holds_after": holds_after,
        "pinned_before": pinned_before,
        "pinned_after": pinned_after,
        "by": by or "dashboard",
    })
    return state()


def set_station_waiver(station: str, waived: bool, by: str = "") -> dict:
    """豁免: trade this station before its settlement count graduates it.

    The one dial that puts an unproven station on live money, so it is
    audited by name and refused on a barred station rather than accepted and
    left inert -- the bar is checked first in the derivation, so a waiver
    there would read like a decision and do nothing.
    """

    station = _station(station)
    if waived and station in excluded_stations():
        raise ValueError(
            "a waiver cannot admit a barred station: " + station
        )
    intent = _read(INTENT_PATH)
    before = sorted({
        str(s).strip().upper()
        for s in (intent.get("station_probation_waivers") or [])
    })
    after = sorted(set(before) | {station}) if waived else sorted(
        set(before) - {station}
    )
    holds = sorted({
        str(s).strip().upper()
        for s in (intent.get("station_probation_holds") or [])
    })
    # Waiving and holding are opposite instructions about the same station.
    holds_after = sorted(set(holds) - {station}) if waived else holds
    intent["station_probation_waivers"] = after
    intent["station_probation_holds"] = holds_after
    intent["updated_at_utc"] = _now()
    _write_atomic(INTENT_PATH, intent)
    _audit({
        "knob": "station_probation_waiver",
        "station": station, "before": before, "after": after,
        "cleared_hold": holds != holds_after,
        "by": by or "dashboard",
    })
    return state()


def set_station_hold(station: str, held: bool, by: str = "") -> dict:
    """待观察: keep a qualified station out of the live batch, by hand.

    The state that did not exist as a control: probation was arithmetic
    only, so an operator who wanted a station recorded but not traded had
    the bar as the only tool -- and the bar is a verdict that spends one of
    fifteen capped places and reads, correctly, as "found wanting".  A hold
    says the opposite thing: nothing is wrong with the station, it is not
    trading yet.  The paper ledger keeps seating and settling it, which is
    how a held station keeps earning the evidence that would end the hold.
    """

    station = _station(station)
    if held and station in excluded_stations():
        raise ValueError(
            "a barred station is already out; hold is for stations that"
            " could trade: " + station
        )
    intent = _read(INTENT_PATH)
    before = sorted({
        str(s).strip().upper()
        for s in (intent.get("station_probation_holds") or [])
    })
    after = sorted(set(before) | {station}) if held else sorted(
        set(before) - {station}
    )
    waivers = sorted({
        str(s).strip().upper()
        for s in (intent.get("station_probation_waivers") or [])
    })
    waivers_after = sorted(set(waivers) - {station}) if held else waivers
    intent["station_probation_holds"] = after
    intent["station_probation_waivers"] = waivers_after
    intent["updated_at_utc"] = _now()
    _write_atomic(INTENT_PATH, intent)
    _audit({
        "knob": "station_probation_hold",
        "station": station, "before": before, "after": after,
        "cleared_waiver": waivers != waivers_after,
        "by": by or "dashboard",
    })
    return state()


def set_probation_waivers(stations: list) -> dict:
    """Grant or revoke the waivers as a whole list.

    The per-station switch above is what the board uses; this stays because
    it is the way to revoke every waiver at once, and because the mechanism
    arrived on 2026-08-31 as a config field with no writer at all --
    readable everywhere, settable only by editing the JSON by hand, which
    left two stations trading below the qualifying threshold with nothing in
    ``live_control_audit.jsonl`` to say who decided that.
    """

    cleaned = sorted({str(s).strip().upper() for s in stations if s})
    for station in cleaned:
        if not STATION_RE.match(station):
            raise ValueError(f"bad station id: {station}")
    # A waiver cannot smuggle a barred station in -- the bar is checked first
    # in live_stations, so such a waiver would be inert -- but accepting one
    # silently would leave a list that reads like a decision and does
    # nothing.  Refuse, and say which.
    barred = sorted(set(cleaned) & excluded_stations())
    if barred:
        raise ValueError(
            "a waiver cannot admit a barred station: " + ",".join(barred)
        )
    intent = _read(INTENT_PATH)
    before = intent.get("station_probation_waivers") or []
    intent["station_probation_waivers"] = cleaned
    intent["updated_at_utc"] = _now()
    _write_atomic(INTENT_PATH, intent)
    _audit({
        "knob": "station_probation_waivers",
        "before": before, "after": cleaned,
    })
    return state()


def set_scope(scope: str) -> dict:
    if scope not in (SCOPE_QUALIFIED, SCOPE_ALL_EXCEPT_BARRED, SCOPE_EXPLICIT):
        raise ValueError(f"unknown station scope: {scope}")
    intent = _read(INTENT_PATH)
    before = intent.get("station_scope")
    intent["station_scope"] = scope
    intent["updated_at_utc"] = _now()
    _write_atomic(INTENT_PATH, intent)
    _audit({"knob": "station_scope", "before": before, "after": scope})
    return state()


def set_allowlist(stations: list) -> dict:
    cleaned = sorted({str(s).strip().upper() for s in stations if s})
    for station in cleaned:
        if not STATION_RE.match(station):
            raise ValueError(f"bad station id: {station}")
    if not cleaned:
        raise ValueError("allowlist cannot be empty")
    # The allowlist is the positive list and the exclusions are the negative
    # one; when they disagree the exclusion wins.  Refusing the whole request
    # rather than quietly dropping the barred entries is deliberate: a caller
    # that asked for a barred station has a different list in mind than the
    # one it would have got back, and silently returning a shorter list is how
    # an operator ends up believing a station is live when it is not.
    barred = sorted(set(cleaned) & excluded_stations())
    if barred:
        raise ValueError(
            "stations barred by live_station_exclusions_v1: "
            + ",".join(barred)
        )
    intent = _read(INTENT_PATH)
    before = intent.get("station_allowlist") or []
    intent["station_allowlist"] = cleaned
    # Pinning a list is what explicit mode means; saying so here keeps a
    # posted list from being silently ignored by a derived scope.
    intent["station_scope"] = SCOPE_EXPLICIT
    intent["updated_at_utc"] = _now()
    _write_atomic(INTENT_PATH, intent)
    _audit({
        "knob": "station_allowlist", "before": before, "after": cleaned,
    })
    return state()


# The board's one-click basket (user 2026-10-05「点击确认一键下单」): the
# pages allowed to send it -- the board as the static server serves it.
MANUAL_ORDER_ORIGINS = frozenset({
    "http://127.0.0.1:8791", "http://localhost:8791",
})

NATIVE_CONFIRM_TIMEOUT_SEC = 120


def native_order_confirmation(body: dict) -> tuple[bool, str]:
    """Ask the operator on this Mac, in a system dialog, to confirm the basket."""

    import subprocess  # noqa: PLC0415

    legs = "\n".join(
        f"  {leg.get('bucket')}：{leg.get('shares')} 份 × 限价 {leg.get('limit_price')}"
        for leg in (body.get("legs") or [])[:12]
    )
    mode = {"taker": "吃卖一（限价 FOK）", "maker": "挂买一+1（post-only GTC）",
            "manual": "手填价格（GTC）"}.get(str(body.get("mode")), str(body.get("mode")))
    text = (f"看板请求用实盘钱包买入 YES\n\n{body.get('station')} {body.get('event_date')}\n"
            f"{legs}\n\n方式：{mode}\n\n只有点「下单」才会发出。")
    script = [
        "on run argv",
        "activate",
        'set r to display dialog (item 1 of argv) with title "一键下单确认" '
        'buttons {"取消", "下单"} default button "取消" cancel button "取消" '
        f"with icon caution giving up after {NATIVE_CONFIRM_TIMEOUT_SEC}",
        "if gave up of r then return \"timeout\"",
        "return button returned of r",
        "end run",
    ]
    args = ["osascript"]
    for line in script:
        args += ["-e", line]
    try:
        out = subprocess.run(args + [text], capture_output=True, text=True,
                             timeout=NATIVE_CONFIRM_TIMEOUT_SEC + 15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"确认框无法弹出：{type(exc).__name__}"
    answer = (out.stdout or "").strip()
    if out.returncode == 0 and answer == "下单":
        return True, "confirmed"
    if answer == "timeout":
        return False, "超时未点"
    if "-128" in (out.stderr or ""):
        return False, "点了取消"
    return False, (out.stderr or answer or "未知").strip()[:120]


# The board's one-station panel (user 2026-10-05: ZUUU 单站日分布，自动刷新与一键
# 刷新).  A reader of what production stored, built on demand
# (bayes_basket_target.station_day_board, ~0.5 s of read-only reads); answers
# within STATION_DAY_CACHE_SEC are shared so several open tabs poll one read,
# and ``force=1`` (the panel's refresh button) skips the cache.
STATION_DAY_CACHE_SEC = 20.0
_STATION_DAY_CACHE: dict[str, tuple[float, dict]] = {}
_STATION_DAY_LOCK = threading.Lock()


def station_day(query: str) -> dict:
    from urllib.parse import parse_qs  # noqa: PLC0415

    from bayes_basket_target import station_day_board  # noqa: PLC0415

    params = parse_qs(query or "")
    station = str((params.get("station") or ["ZUUU"])[0]).strip().upper()
    if not re.fullmatch(r"[A-Z]{4}", station):
        raise ValueError(f"station must be four letters: {station!r}")
    force = str((params.get("force") or ["0"])[0]) == "1"
    if str((params.get("token_only") or ["0"])[0]) == "1":
        # The panel's poll: one index seek a day, no build.
        return station_day_board.probability_token(station)
    with _STATION_DAY_LOCK:
        cached = _STATION_DAY_CACHE.get(station)
        if (cached and not force and time.monotonic() - cached[0] < STATION_DAY_CACHE_SEC
                and cached[1].get("token") == station_day_board.probability_token(station)["token"]):
            return {**cached[1], "cache": "hit"}
        payload = station_day_board.build(station)
        _STATION_DAY_CACHE[station] = (time.monotonic(), payload)
        return {**payload, "cache": "miss"}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(200, {"ok": True})

    def do_GET(self) -> None:  # noqa: N802
        path, _, query = self.path.partition("?")
        if path.rstrip("/") == "/api/state":
            self._send(200, state())
        elif path.rstrip("/") == "/api/station-day":
            try:
                self._send(200, station_day(query))
            except Exception as exc:  # noqa: BLE001 -- the board shows the reason
                self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
        else:
            self._send(404, {"error": "unknown endpoint"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path.rstrip("/") == "/api/kelly":
                raise ValueError(
                    "/api/kelly is retired (charter 141): reload the board; "
                    "use /api/exposure or /api/kelly-fraction"
                )
            elif self.path.rstrip("/") == "/api/exposure":
                out = set_exposure(float(body["value"]))
            elif self.path.rstrip("/") == "/api/kelly-fraction":
                out = set_kelly_fraction(float(body["value"]))
            elif self.path.rstrip("/") == "/api/station-sizing":
                out = set_station_sizing(float(body["value"]))
            elif self.path.rstrip("/") == "/api/station-ceiling":
                out = set_station_ceiling(float(body["value"]))
            elif self.path.rstrip("/") == "/api/budget":
                out = set_budget(float(body["value"]))
            elif self.path.rstrip("/") == "/api/tiebreak-stake":
                raise ValueError(
                    "/api/tiebreak-stake is retired (charter 252): reload the board; "
                    "use /api/lane-stake"
                )
            elif self.path.rstrip("/") == "/api/lane-stake":
                out = set_lane_stake(str(body.get("lane") or ""), body["value"])
            elif self.path.rstrip("/") == "/api/lane":
                out = set_lane_enabled(
                    str(body.get("lane") or ""),
                    bool(body.get("enabled")),
                    str(body.get("by") or ""),
                )
            elif self.path.rstrip("/") == "/api/live":
                out = set_live(
                    bool(body.get("enabled")),
                    str(body.get("confirm") or ""),
                    str(body.get("by") or ""),
                )
            elif self.path.rstrip("/") == "/api/allowlist":
                out = set_allowlist(list(body.get("stations") or []))
            elif self.path.rstrip("/") == "/api/station-bar":
                out = set_station_bar(
                    str(body.get("station") or ""),
                    bool(body.get("barred")),
                    str(body.get("by") or ""),
                )
            elif self.path.rstrip("/") == "/api/station-waiver":
                out = set_station_waiver(
                    str(body.get("station") or ""),
                    bool(body.get("waived")),
                    str(body.get("by") or ""),
                )
            elif self.path.rstrip("/") == "/api/station-hold":
                out = set_station_hold(
                    str(body.get("station") or ""),
                    bool(body.get("held")),
                    str(body.get("by") or ""),
                )
            elif self.path.rstrip("/") == "/api/manual-basket-order":
                # Money leaves the wallet: only the board's own page may ask.
                # The CORS answer above lets any page's script reach this
                # loopback port, so the Origin is checked here, by name.
                origin = str(self.headers.get("Origin") or "")
                if origin == "null":
                    # The board opened as a file:// page sends Origin: null --
                    # and so can any site's sandboxed frame.  Such a request
                    # goes ahead only after the operator says yes in a dialog
                    # on this Mac, which no web page can click.
                    confirmed, why = native_order_confirmation(body)
                    if not confirmed:
                        self._send(403, {"error": f"本机确认框未确认（{why}）"})
                        return
                elif origin not in MANUAL_ORDER_ORIGINS:
                    self._send(403, {"error": f"来源不被允许下单：{origin or '无 Origin'}"})
                    return
                from bayes_basket_target.manual_basket_order import (  # noqa: PLC0415
                    place_manual_basket,
                )
                out = place_manual_basket(body)
            elif self.path.rstrip("/") == "/api/station-bulk":
                out = set_all_stations(
                    str(body.get("action") or ""),
                    str(body.get("by") or ""),
                )
            elif self.path.rstrip("/") == "/api/probation_waivers":
                # Empty list revokes; that is the intended way to take back a
                # waiver rather than editing the file the panel reads.
                out = set_probation_waivers(
                    list(body.get("stations") or [])
                )
            else:
                self._send(404, {"error": "unknown endpoint"})
                return
            self._send(200, out)
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._send(400, {"error": str(exc)})
        except RuntimeError as exc:
            # A gate that half moved.  500, not 400: the caller's request was
            # well formed and part of it was carried out, and the message
            # says which part, so the operator is not left believing the
            # switch is where the label says it is.
            self._send(500, {"error": str(exc), "state": state()})

    def log_message(self, *_args) -> None:
        return


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"live control: http://127.0.0.1:{PORT}/api/state  (Ctrl-C 停止)")
    server.serve_forever()


if __name__ == "__main__":
    main()
