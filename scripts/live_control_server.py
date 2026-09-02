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
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
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
            import sys  # noqa: PLC0415

            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))
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
            "station_exclusions": _scope["barred"],
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
    re-admit a station somebody just barred.  A missing or unreadable file
    excludes nothing -- this gate refuses admissions, it does not grant
    them, so failing open here cannot by itself put money anywhere the
    allowlist had not already named.
    """

    try:
        parsed = json.loads(EXCLUSION_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    # Two lists, one bar.  The operator's rulings and the automatic layer are
    # kept apart in the file so neither can quietly overwrite the other, and
    # the union is what binds here -- a station barred by either is barred.
    out: set[str] = set()
    for key in ("excluded_stations", "auto_excluded_stations"):
        for row in parsed.get(key) or []:
            if isinstance(row, dict) and row.get("station"):
                out.add(str(row["station"]).strip().upper())
    return out


SCOPE_QUALIFIED = "qualified_and_not_barred"
SCOPE_ALL_EXCEPT_BARRED = "all_except_barred"
SCOPE_EXPLICIT = "explicit"
SSOT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
# The same threshold the automatic layer needs before it may judge a station.
# Kept in one place so admission and judgement cannot drift apart: a station
# admitted on less evidence than the layer needs would be one nothing could
# ever bar for cause.
MIN_SETTLEMENTS_TO_QUALIFY = 5


def tradeable_stations() -> set:
    """Every station the model actually produces for, read from the book.

    Derived rather than enumerated because the fleet grows: the roster went
    from 45 to 48 while this was being written, and any list of station ids
    frozen into a config is a list that will be wrong by the time somebody
    reads it.  Book history is the right test rather than the coordinate
    roster -- LLBG, LTFM and UUWW are on the roster and have never produced a
    position, and a live batch cannot contain a station the model has never
    computed.
    """

    try:
        conn = sqlite3.connect(f"file:{SSOT_DB}?mode=ro", uri=True, timeout=30.0)
    except sqlite3.Error:
        return set()
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        return {
            str(row[0]).strip().upper()
            for row in conn.execute(
                "SELECT DISTINCT station_id FROM paper_trade_position"
            )
            if row[0]
        }
    except sqlite3.Error:
        return set()
    finally:
        conn.close()


def settlement_counts() -> dict:
    """How much evidence each station has actually produced."""

    try:
        conn = sqlite3.connect(f"file:{SSOT_DB}?mode=ro", uri=True, timeout=30.0)
    except sqlite3.Error:
        return {}
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        return {
            str(row[0]).strip().upper(): int(row[1])
            for row in conn.execute(
                "SELECT station_id, COUNT(*) FROM paper_trade_settlement"
                " GROUP BY station_id"
            )
            if row[0]
        }
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def live_stations() -> dict:
    """What actually trades: judged, survived, and not barred.

    Operator ruling 2026-08-31 reverses the default -- a new station is not
    live until it has earned it.  That needs three states rather than two,
    because "not proven yet" and "judged and found wanting" are different
    facts and collapsing them does real damage: a station with two
    settlements is not bad, and putting it on the bar list would also spend
    one of the fifteen places the bar is capped at.

    So membership is still derived, never enumerated -- the ruling of
    2026-08-30 stands and the reasons for it did not change, the fleet only
    grows and any frozen list is wrong by the time it is read.  What changed
    is one more term in the derivation: a station must have produced at
    least ``MIN_SETTLEMENTS_TO_QUALIFY`` settlements, which is the same
    threshold the automatic layer needs before it may judge a station at
    all.  Sharing that number is the point: below it nothing can be said
    about a station, so below it nothing is traded on it.

    Probation is not a punishment and costs nothing to sit in -- the paper
    ledger keeps seating and settling these stations, which is precisely how
    they accumulate the evidence that graduates them.
    """

    intent = _read(INTENT_PATH)
    scope = str(intent.get("station_scope") or SCOPE_QUALIFIED)
    barred = excluded_stations()
    minimum = int(
        intent.get("minimum_settlements_to_qualify")
        or MIN_SETTLEMENTS_TO_QUALIFY
    )
    if scope == SCOPE_EXPLICIT:
        pinned = sorted({
            str(s).strip().upper()
            for s in (intent.get("station_allowlist") or [])
        })
        return {
            "scope": SCOPE_EXPLICIT,
            "stations": [s for s in pinned if s not in barred],
            "probation": [],
            "barred": sorted(barred),
            "minimum_settlements": minimum,
        }
    unbarred = tradeable_stations() - barred
    counts = settlement_counts()
    if scope == SCOPE_ALL_EXCEPT_BARRED:
        return {
            "scope": SCOPE_ALL_EXCEPT_BARRED,
            "stations": sorted(unbarred),
            "probation": [],
            "barred": sorted(barred),
            "minimum_settlements": 0,
        }
    # An operator may graduate a station by name before the settlement count
    # does it (ruling 2026-08-31: LIMC and LEMD trade now).  This is a waiver
    # of the qualifying threshold, not an enumeration of the live list -- the
    # membership stays derived, and a waiver for a barred station is inert
    # because the bar is checked first.  Recorded in the intent file so the
    # ruling survives restarts and shows up in /api/state.
    waived = {
        str(s).strip().upper()
        for s in (intent.get("station_probation_waivers") or [])
    }
    return {
        "scope": SCOPE_QUALIFIED,
        "stations": sorted(
            s for s in unbarred
            if counts.get(s, 0) >= minimum or s in waived
        ),
        "probation": sorted(
            s for s in unbarred
            if counts.get(s, 0) < minimum and s not in waived
        ),
        "probation_waivers": sorted(waived),
        "barred": sorted(barred),
        "minimum_settlements": minimum,
    }


def set_probation_waivers(stations: list) -> dict:
    """Grant or revoke the waivers that let a station trade before it qualifies.

    The mechanism arrived on 2026-08-31 as a config field with no writer:
    readable everywhere, settable only by editing the JSON by hand, which
    left two stations trading below the qualifying threshold with nothing in
    ``live_control_audit.jsonl`` to say who decided that -- in a control
    plane where all ninety-three other dial turns are recorded.  A waiver is
    the one dial that puts an unproven station on live money, so it is the
    last one that should be the unaudited one.

    Passing an empty list revokes every waiver, which is how a waiver granted
    elsewhere gets taken back through the panel rather than by editing a file
    the panel also reads.
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
