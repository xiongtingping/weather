"""Who may trade live -- one derivation, read by the panel and by the send.

Three states per station and four ways to be in one.  A station is BARRED
(judged and found wanting, or ruled out by hand), on PROBATION (nothing can
be said about it yet, or the operator is holding it back), or LIVE.  The
states are derived on every read rather than enumerated, for the reason the
2026-08-30 ruling gave and that has not changed: the fleet only grows, any
frozen list is wrong by the time it is read, and the eight-station batch went
stale in two days.

What this module adds over the derivation it replaces is the operator's hand
on each of the three, as a switch rather than a file edit:

* ``excluded_stations``            -- 禁入, the bar.  Wins over everything.
* ``station_probation_waivers``    -- 豁免, trade before the evidence is in.
* ``station_probation_holds``      -- 待观察, hold back a station that has
  qualified, without saying the station is bad.

The three are mutually exclusive by construction at the write boundary
(scripts/live_control_server.py), and they are all operator rulings, which
means the automatic layer never touches a station that carries one.

It lives in the package rather than beside the panel because the panel is not
the only reader any more.  Until 2026-09-02 the bar bound in exactly three
places -- the panel's own allowlist writer, the board, and the go-live
migration's precondition -- and in none of them was an order stopped: EDDM,
KSEA and LTAC all carry live-authorised intents minted while barred.  A gate
that is drawn on a screen and consulted by nobody at the send is a label.
``bayes_basket_target.order_submission`` reads this now, and it has to read
the same rule the screen draws or the two will disagree the first time a
switch moves.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
INTENT_PATH = ROOT / "config/trading/live_intent_v1.json"
EXCLUSION_PATH = ROOT / "config/trading/live_station_exclusions_v1.json"
SSOT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"

SCOPE_QUALIFIED = "qualified_and_not_barred"
SCOPE_ALL_EXCEPT_BARRED = "all_except_barred"
SCOPE_EXPLICIT = "explicit"

# The same threshold the automatic layer needs before it may judge a station.
# Kept in one place so admission and judgement cannot drift apart: a station
# admitted on less evidence than the layer needs would be one nothing could
# ever bar for cause.
MIN_SETTLEMENTS_TO_QUALIFY = 5


def _read(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _stations(values: Iterable[Any] | None) -> set[str]:
    return {
        str(value).strip().upper()
        for value in (values or ())
        if str(value or "").strip()
    }


def barred_stations(exclusion_path: Path | str | None = None) -> set[str]:
    """The union of the two bar lists -- hand rulings and the automatic layer.

    Fails OPEN: an unreadable file bars nothing.  This gate refuses
    admissions and never grants one, so failing open cannot by itself put
    money anywhere a positive list had not already named -- while failing
    closed would let a deleted config empty the live book, which is the
    bigger surprise.  (``live_intent_armed`` fails the other way for the
    opposite reason: it authorises spending, and a flag nobody can read is
    not permission.)

    The two lists are kept apart in the file so neither can quietly overwrite
    the other; a station barred by either is barred.
    """

    try:
        doc = _read(Path(exclusion_path or EXCLUSION_PATH))
    except (OSError, ValueError):
        return set()
    out: set[str] = set()
    for key in ("excluded_stations", "auto_excluded_stations"):
        for row in doc.get(key) or []:
            if isinstance(row, dict) and row.get("station"):
                out.add(str(row["station"]).strip().upper())
    return out


def operator_bar_sources(
    exclusion_path: Path | str | None = None,
) -> dict[str, list[str]]:
    """The bar, split by who ruled it -- the operator's rows and the machine's.

    The board draws the two differently and the switch behaves differently on
    them: turning off an operator bar retracts a ruling, turning off an
    automatic one has to *also* record a ruling, or the next sweep puts it
    straight back.
    """

    try:
        doc = _read(Path(exclusion_path or EXCLUSION_PATH))
    except (OSError, ValueError):
        return {"operator": [], "auto": []}
    def _rows(key: str) -> list[str]:
        return sorted({
            str(row["station"]).strip().upper()
            for row in (doc.get(key) or [])
            if isinstance(row, dict) and row.get("station")
        })
    return {"operator": _rows("excluded_stations"), "auto": _rows("auto_excluded_stations")}


def operator_overrides(
    intent_path: Path | str | None = None,
) -> dict[str, set[str]]:
    """The two switches that live in the intent file: 豁免 and 待观察.

    Both are rulings about a station nobody has barred, and both are read
    here rather than in two places for the reason the board learned on
    2026-08-31: the panel had LIMC live on a waiver, the page re-derived
    membership without knowing waivers existed and drew it as probation, and
    a reader comparing the two had no way to tell which one was the system.
    """

    try:
        intent = _read(Path(intent_path or INTENT_PATH))
    except (OSError, ValueError):
        return {"waived": set(), "held": set()}
    return {
        "waived": _stations(intent.get("station_probation_waivers")),
        "held": _stations(intent.get("station_probation_holds")),
    }


def _connect(conn: sqlite3.Connection | None) -> tuple[sqlite3.Connection | None, bool]:
    if conn is not None:
        return conn, False
    try:
        opened = sqlite3.connect(f"file:{SSOT_DB}?mode=ro", uri=True, timeout=30.0)
    except sqlite3.Error:
        return None, False
    opened.execute("PRAGMA busy_timeout=30000")
    return opened, True


def tradeable_stations(conn: sqlite3.Connection | None = None) -> set[str]:
    """Every station the model actually produces for, read from the book.

    Derived rather than enumerated because the fleet grows: the roster went
    45 to 48 while this was being written.  Book history is the right test
    rather than the coordinate roster -- LLBG, LTFM and UUWW are on the
    roster and have never produced a position, and a live batch cannot
    contain a station the model has never computed.
    """

    handle, owned = _connect(conn)
    if handle is None:
        return set()
    try:
        return {
            str(row[0]).strip().upper()
            for row in handle.execute(
                "SELECT DISTINCT station_id FROM paper_trade_position"
            )
            if row[0]
        }
    except sqlite3.Error:
        return set()
    finally:
        if owned:
            handle.close()


def settlement_counts(conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """How much evidence each station has actually produced."""

    handle, owned = _connect(conn)
    if handle is None:
        return {}
    try:
        return {
            str(row[0]).strip().upper(): int(row[1])
            for row in handle.execute(
                "SELECT station_id, COUNT(*) FROM paper_trade_settlement"
                " GROUP BY station_id"
            )
            if row[0]
        }
    except sqlite3.Error:
        return {}
    finally:
        if owned:
            handle.close()


def live_stations(
    *,
    intent_path: Path | str | None = None,
    exclusion_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """The three states, derived, with the operator's three switches applied.

    Order of precedence, and each layer is a different kind of statement:

    1. 禁入 wins.  A barred station does not trade, whatever else says.
    2. 待观察 (hold) keeps a qualified station out without calling it bad.
    3. 豁免 (waiver) admits a station the settlement count has not graduated
       yet -- the operator graduating it by name.
    4. Otherwise the evidence decides: ``MIN_SETTLEMENTS_TO_QUALIFY``
       settlements and the station is live; fewer and it waits.

    Probation is not a punishment and costs nothing to sit in: the paper
    ledger keeps seating and settling these stations, which is precisely how
    they accumulate the evidence that graduates them.  禁的是下单，不是记账.
    """

    try:
        intent = _read(Path(intent_path or INTENT_PATH))
    except (OSError, ValueError):
        intent = {}
    scope = str(intent.get("station_scope") or SCOPE_QUALIFIED)
    barred = barred_stations(exclusion_path)
    overrides = operator_overrides(intent_path)
    waived, held = overrides["waived"], overrides["held"]
    minimum = int(
        intent.get("minimum_settlements_to_qualify")
        or MIN_SETTLEMENTS_TO_QUALIFY
    )

    def _pack(live: set[str], probation: set[str]) -> dict[str, Any]:
        return {
            "scope": scope,
            "stations": sorted(live),
            "probation": sorted(probation),
            "probation_waivers": sorted(waived),
            # Held stations are reported whether or not they are currently in
            # the batch: a switch the operator turned on has to be visible
            # even on a station the evidence would have parked anyway.
            "probation_holds": sorted(held),
            "barred": sorted(barred),
            "minimum_settlements": minimum,
        }

    if scope == SCOPE_EXPLICIT:
        pinned = _stations(intent.get("station_allowlist")) - barred
        return _pack(pinned - held, pinned & held)
    unbarred = tradeable_stations(conn) - barred
    if scope == SCOPE_ALL_EXCEPT_BARRED:
        return _pack(unbarred - held, unbarred & held)
    counts = settlement_counts(conn)
    qualified = {
        station for station in unbarred
        if counts.get(station, 0) >= minimum or station in waived
    }
    return _pack(qualified - held, unbarred - (qualified - held))


def admission(
    station: str,
    *,
    intent_path: Path | str | None = None,
    exclusion_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """May this station's order go out?  With the reason, always.

    The reason is not decoration: ``live_station_barred`` and
    ``live_station_probation`` call for opposite follow-ups -- one is a
    ruling somebody made, the other is a station still earning its way in --
    and a receipt that flattened them into "refused" would make the two
    indistinguishable in the one record that survives the cycle.
    """

    wanted = str(station or "").strip().upper()
    scope = live_stations(
        intent_path=intent_path, exclusion_path=exclusion_path, conn=conn,
    )
    if not wanted:
        return {"admitted": False, "reason": "live_station_unknown",
                "station": wanted, "scope": scope["scope"]}
    if wanted in set(scope["barred"]):
        return {"admitted": False, "reason": "live_station_barred",
                "station": wanted, "scope": scope["scope"]}
    if wanted in set(scope["stations"]):
        return {"admitted": True, "reason": "live_station_admitted",
                "station": wanted, "scope": scope["scope"]}
    return {
        "admitted": False,
        # Held by hand and merely unproven are both probation, and the
        # distinction belongs on the receipt: one of them is somebody's
        # decision this cycle and the other is arithmetic.
        "reason": (
            "live_station_held"
            if wanted in set(scope["probation_holds"])
            else "live_station_probation"
        ),
        "station": wanted,
        "scope": scope["scope"],
    }


def station_state_map(scope: Mapping[str, Any]) -> dict[str, str]:
    """station -> live | probation | barred, for a caller that has a scope."""

    out: dict[str, str] = {}
    for station in scope.get("barred") or ():
        out[str(station)] = "barred"
    for station in scope.get("probation") or ():
        out.setdefault(str(station), "probation")
    for station in scope.get("stations") or ():
        out.setdefault(str(station), "live")
    return out


__all__ = [
    "MIN_SETTLEMENTS_TO_QUALIFY",
    "SCOPE_ALL_EXCEPT_BARRED",
    "SCOPE_EXPLICIT",
    "SCOPE_QUALIFIED",
    "admission",
    "barred_stations",
    "live_stations",
    "operator_bar_sources",
    "operator_overrides",
    "settlement_counts",
    "station_state_map",
    "tradeable_stations",
]
