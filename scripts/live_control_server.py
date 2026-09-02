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

POST /api/kelly   {"value": 0.35}          -> portfolio_risk_fraction
POST /api/budget  {"value": 514.29}        -> target_remaining_budget_usdc
POST /api/live    {"enabled": true, "confirm": "LIVE", "by": "..."}
POST /api/allowlist {"stations": ["ZBAA", ...]}
POST /api/station-bar     {"station": "KSEA", "barred": true}
POST /api/station-waiver  {"station": "LIMC", "waived": true}
POST /api/station-hold    {"station": "KDAL", "held": true}

The last three are the station gate, one click each: 禁入 (the bar), 豁免
(trade before the evidence is in) and 待观察 (hold a qualified station back).
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
STATION_CAP_PATH = ROOT / "config/trading/station_cap_v1.json"
INTENT_PATH = ROOT / "config/trading/live_intent_v1.json"
EXCLUSION_PATH = ROOT / "config/trading/live_station_exclusions_v1.json"
AUDIT_PATH = ROOT / "data" / "live_control_audit.jsonl"
# The gateway's admission gate.  clob_order_service.mjs checks this file at
# every place-order, before any idempotency write, signing or exchange POST,
# and it stops every lane without exception.  The switch below owns it; the
# file stays the mechanism so a manual `touch` is still a working e-stop and
# so the install and cutover scripts that place it keep working unchanged.
PAUSE_FLAG_PATH = ROOT / "data" / "live_paused.flag"
PORT = 8788

KELLY_BOUNDS = (0.05, 0.50)
KELLY_STEP_MAX = 0.50
BUDGET_BOUNDS = (0.0, 2000.0)
STATION_CAP_BOUNDS = (1.0, 30.0)
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
    return {
        "schema": "live_control_state_v1",
        "at_utc": _now(),
        "kelly": {
            "portfolio_risk_fraction": policy["portfolio_risk_fraction"],
            "bounds": KELLY_BOUNDS,
            "step": 0.05,
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
            "step": 50.0,
        },
        "station_cap": {
            "station_cap_usdc": (
                _read(STATION_CAP_PATH).get("station_cap_usdc")
                if STATION_CAP_PATH.is_file() else None
            ),
            "default_usdc": 15.0,
            "bounds": STATION_CAP_BOUNDS,
            "step": 1.0,
        },
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


def set_kelly(value: float) -> dict:
    if not KELLY_BOUNDS[0] <= value <= KELLY_BOUNDS[1]:
        raise ValueError(
            f"kelly out of bounds {KELLY_BOUNDS}: {value}"
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


def set_budget(value: float) -> dict:
    if not BUDGET_BOUNDS[0] <= value <= BUDGET_BOUNDS[1]:
        raise ValueError(
            f"budget out of bounds {BUDGET_BOUNDS}: {value}"
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


def set_station_cap(value: float) -> dict:
    """The per-station bankroll, moved live without a deploy.

    Until 2026-08-31 this number was a code constant
    (runtime._DEFAULT_POLICY["total_budget_usdc"]) and changing it took an
    edit and a restart; the operator asked for a dial.  The file this writes
    is read fresh at every plan compile, and the three bankroll-shaped
    bounds (protected floor, outside loss, minimum expected pnl) are derived
    from it there, not stored here -- one number, one owner.
    """

    if not STATION_CAP_BOUNDS[0] <= value <= STATION_CAP_BOUNDS[1]:
        raise ValueError(
            f"station cap out of bounds {STATION_CAP_BOUNDS}: {value}"
        )
    current = (
        _read(STATION_CAP_PATH) if STATION_CAP_PATH.is_file() else {}
    )
    before = current.get("station_cap_usdc")
    payload = {
        "schema": "basket_station_cap_v1",
        "station_cap_usdc": round(float(value), 2),
        "updated_at_utc": _now(),
    }
    _write_atomic(STATION_CAP_PATH, payload)
    _audit({
        "knob": "station_cap_usdc",
        "before": before, "after": payload["station_cap_usdc"],
    })
    return state()


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
# The bar's ceiling.  Read from the automatic layer so one number governs
# both the sweep that fills the list and the switch that adds to it.
try:  # pragma: no cover - exercised by whichever import path is live
    from auto_station_exclusions import MAX_BARRED_STATIONS
except ImportError:  # pragma: no cover
    MAX_BARRED_STATIONS = 15


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


def _book_row(station: str) -> dict:
    """The station's own numbers, so a hand-ruled row carries its evidence.

    Same definition as the board's: every settled row, stake and P&L summed.
    A bar written without them would be a name in a list, and the next
    reader would have to re-derive what the operator was looking at -- which
    is exactly the re-litigation the file's ``why_not_automatic`` note
    exists to prevent.
    """

    try:
        conn = sqlite3.connect(f"file:{SSOT_DB}?mode=ro", uri=True, timeout=30.0)
    except sqlite3.Error:
        return {"full_sample_roi": None, "settlements": 0}
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(stake_usdc),0),"
            " COALESCE(SUM(pnl_usdc),0) FROM paper_trade_settlement"
            " WHERE station_id=?",
            (station,),
        ).fetchone()
    except sqlite3.Error:
        return {"full_sample_roi": None, "settlements": 0}
    finally:
        conn.close()
    count, stake, pnl = int(row[0]), float(row[1]), float(row[2])
    return {
        "full_sample_roi": round(pnl / stake, 3) if stake else None,
        "settlements": count,
    }


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


def _trim_auto_rows_to_cap(doc: dict) -> list:
    """Make room for a hand ruling by releasing the machine's weakest bars.

    The cap is a budget for the automatic layer's judgement, not a licence
    to overrule a person's -- so when a new hand ruling would push the union
    past fifteen, the automatic rows give way, least severe first.  This is
    the same arithmetic the sweep does on its next pass (``room = cap -
    operator rows``), applied now so the file is never briefly over the cap
    that the migration and the tests both check.

    Ordering copies the sweep's own, which is the point: it keeps the most
    severe rows and drops the rest, and a row this releases would be
    released there too on the next pass.  The sweep sorts by fulfilment
    ratio ascending with an unreadable ratio pushed to the end, so a row
    barred on the money gap gives way first, and among ratio rows the
    least-negative one goes.  The two keys are not comparable directly -- a
    ratio of -0.5 and a gap of -18 USDC are different units -- so they are
    ordered as the sweep orders them rather than mixed.
    """

    operator = {
        str(row.get("station") or "").strip().upper()
        for row in (doc.get("excluded_stations") or [])
    }
    auto = list(doc.get("auto_excluded_stations") or [])
    room = max(0, MAX_BARRED_STATIONS - len(operator))
    if len(auto) <= room:
        return []

    def _weakest_first(row: dict) -> tuple:
        value = row.get("value")
        readable = (
            str(row.get("barred_on") or "") == "fulfilment_ratio"
            and isinstance(value, (int, float))
        )
        return (
            # A gap-barred row has no readable ratio, which is where the
            # sweep parks it too: last to keep, first to give way.
            1 if readable else 0,
            -float(value) if readable else 0.0,
        )

    ordered = sorted(auto, key=_weakest_first)
    dropped = ordered[:len(auto) - room]
    keep = {id(row) for row in ordered[len(auto) - room:]}
    doc["auto_excluded_stations"] = [row for row in auto if id(row) in keep]
    return sorted(
        str(row.get("station") or "").strip().upper() for row in dropped
    )


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
    """

    station = _station(station)
    doc = _read(EXCLUSION_PATH)
    before = sorted(excluded_stations())
    stamp = _now()[:10]
    operator_rows = list(doc.get("excluded_stations") or [])
    auto_rows = list(doc.get("auto_excluded_stations") or [])
    released_rows = list(doc.get("released_stations") or [])
    was_auto = any(
        str(row.get("station") or "").upper() == station for row in auto_rows
    )
    book = _book_row(station)
    prior_release = next(
        (row for row in released_rows
         if str(row.get("station") or "").upper() == station),
        None,
    )
    trimmed: list = []
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
    doc["updated_at_utc"] = _now()
    if barred:
        trimmed = _trim_auto_rows_to_cap(doc)
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
        "auto_rows_released_for_room": trimmed,
        "by": by or "dashboard",
        **cleared,
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
        if self.path.rstrip("/") == "/api/state":
            self._send(200, state())
        else:
            self._send(404, {"error": "unknown endpoint"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path.rstrip("/") == "/api/kelly":
                out = set_kelly(float(body["value"]))
            elif self.path.rstrip("/") == "/api/station-cap":
                out = set_station_cap(float(body["value"]))
            elif self.path.rstrip("/") == "/api/budget":
                out = set_budget(float(body["value"]))
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
