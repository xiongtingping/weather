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


def _distinct_stations(
    handle: sqlite3.Connection, statement: str
) -> set[str]:
    try:
        return {
            str(row[0]).strip().upper()
            for row in handle.execute(statement)
            if row[0]
        }
    except sqlite3.Error:
        return set()


def tradeable_stations(conn: sqlite3.Connection | None = None) -> set[str]:
    """Every station the model actually produces for, read from the book.

    Derived rather than enumerated because the fleet grows: the roster went
    45 to 48 while this was being written.  Book history is the right test
    rather than the coordinate roster -- LLBG, LTFM and UUWW are on the
    roster and have never produced a position, and a live batch cannot
    contain a station the model has never computed.

    The roster is production's own, and it is deliberately NOT the ledger's
    (user ruling, 2026-09-08: 清单和账本解耦).  This read used to be
    ``paper_trade_position`` -- a RESEARCH table the paper ledger drops and
    re-derives whenever its selection fingerprint moves
    (``_drop_superseded_positions``, which commits the delete before it
    seats anything).  A fingerprint change at 09:27:27Z emptied it, and
    because every station's membership was computed from that one set, the
    empty table read as "no station has earned its way in": all 45 went to
    probation, the send gate refused every BUY, and the fleet placed no
    order for 65 minutes while eighteen station-days sat holding legs whose
    partners had been cancelled.  Nothing was broken and nothing alarmed --
    an empty research table is a perfectly well-formed answer to the
    question this function used to ask.

    Which is the whole point of the decoupling: a research artifact must not
    be able to answer a production question at all, not even correctly.  The
    union with the ledger was the first fix and it was the wrong one -- it
    left the ledger able to put a station INTO the live roster, so the
    coupling survived in the direction that adds risk rather than the one
    that removes it.

    ``basket_policy_training_fact`` is the production record of the
    decisions that actually minted intents.  It carries no_update and
    no_delete triggers, so no rebuild can empty it; it holds the same 45
    stations; and it excludes LLBG, LTFM and UUWW, which are on the
    coordinate roster and have never produced a seat.  It answers "which
    stations does the model produce for" strictly better than a replay of
    that production does.
    """

    handle, owned = _connect(conn)
    if handle is None:
        return set()
    try:
        # No predicate on ``intent_count``: a training fact exists only
        # because production made a decision for that station-day, so every
        # row already answers the question, and the two readings are equal
        # over the whole table (checked 2026-09-08, 45 == 45).  It is not a
        # cosmetic simplification -- ``intent_count`` is not in the scope
        # index, so asking for it turns an index-only walk into 20,534 row
        # fetches on a read that runs on every submission pass and every
        # board render.  With it: SCAN USING INDEX.  Without: SCAN USING
        # COVERING INDEX.
        return _distinct_stations(
            handle,
            "SELECT DISTINCT station_id FROM basket_policy_training_fact",
        )
    finally:
        if owned:
            handle.close()


def settlement_counts(conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """How much evidence each station has actually produced."""

    handle, owned = _connect(conn)
    if handle is None:
        return {}
    try:
        # The paper book left the SSOT for its own file (2026-09-19).
        from .paper_ledger_store import attach_paper_ledger  # noqa: PLC0415
        attach_paper_ledger(handle)
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
    2. 待观察 (hold) keeps a station out without calling it bad.
    3. 豁免 (waiver) is what puts a station in.  Nothing else does.

    Point 3 used to read "admits a station the settlement count has not
    graduated yet", with a fourth clause under it where the count admitted
    on its own.  Both the count and the roster it was intersected with are
    reads of a data table, and on 2026-09-08 one of those tables was
    rebuilt: every switch on the board still said what the operator had set
    it to, and the fleet placed no order for 65 minutes anyway.  Membership
    is now the three hand-written lists and nothing else (user ruling:
    只看首批站点闸门表即可), which is what the board has told its reader since
    2026-09-02 -- 表里的五闸…不再自己决定谁上线.

    The count has not gone anywhere: it is still derived, still reported,
    and still what the automatic bar layer judges on.  It just does not
    admit, because admitting is an act that should leave an audit row, and
    a query does not leave one.

    Probation is not a punishment and costs nothing to sit in: the paper
    ledger keeps seating and settling these stations, which is precisely how
    they accumulate the evidence an operator reads before flipping 豁免.
    禁的是下单，不是记账.
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

    # The first-batch station gate decides membership and nothing else does
    # (user ruling 2026-09-08: 只看首批站点闸门表即可).  The board has said
    # this in its own words since 2026-09-02 -- 三个开关全是人工的…下一轮生产
    # 立刻照它执行；表里的五闸是给你看的评分，不再自己决定谁上线 -- and the
    # send path had not caught up: it still intersected the operator's three
    # switches with a universe derived from a data table, so a table that
    # went away could overrule every switch on the board.  That is what
    # happened on 2026-09-08, and no amount of choosing a better table fixes
    # the shape: a switch that a query can silently veto is not a switch.
    #
    # So membership is now read from the two config files alone and the
    # verdict is arithmetic on three hand-written lists.  The roster below
    # is DISPLAY -- the probation column the board draws -- and it can no
    # longer subtract from the gate.
    roster = tradeable_stations(conn)
    roster_available = bool(roster)

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
            # Display only, and named for what it is now that the gate
            # decides alone: the board's probation column has a book behind
            # it, or it does not.  Neither state can change who trades.
            "roster_available": roster_available,
            "roster_source": "book" if roster_available else "unavailable",
        }

    if scope == SCOPE_EXPLICIT:
        pinned = _stations(intent.get("station_allowlist")) - barred
        return _pack(pinned - held, pinned & held)
    unbarred = roster - barred
    if scope == SCOPE_ALL_EXCEPT_BARRED:
        return _pack(unbarred - held, unbarred & held)
    # 豁免 minus 禁入 minus 待观察.  The settlement count is still derived and
    # still reported -- the automatic layer judges on it and the board draws
    # it -- but it no longer admits anybody: graduating a station is the
    # operator flipping 豁免, which is the one act that leaves an audit row.
    # Reading a count as an admission is how a research table came to hold a
    # veto over the live book.
    live = (waived - barred) - held
    # Everything the board knows about and is not trading: the stations
    # production produces for, plus anyone the operator has named, less the
    # ones actually live.  Display only.
    known = ((roster | waived | held) - barred)
    return _pack(live, known - live)


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
    # Carried on every verdict, admitted or not: a cycle that traded on the
    # fallback roster has to be tellable afterwards from one that traded on
    # the book, and the receipt is the only record that survives the cycle.
    # Reported on every verdict so a receipt can still say whether the
    # board's display roster was readable when this cycle ran.  It does not
    # enter the verdict: the gate does.
    source = str(scope.get("roster_source") or "book")
    if not wanted:
        return {"admitted": False, "reason": "live_station_unknown",
                "station": wanted, "scope": scope["scope"],
                "roster_source": source}
    if wanted in set(scope["barred"]):
        return {"admitted": False, "reason": "live_station_barred",
                "station": wanted, "scope": scope["scope"],
                "roster_source": source}
    if wanted in set(scope["stations"]):
        return {"admitted": True, "reason": "live_station_admitted",
                "station": wanted, "scope": scope["scope"],
                "roster_source": source}
    return {
        "admitted": False,
        # Held by hand and not on the gate are both probation, and the
        # distinction belongs on the receipt: one of them is somebody's
        # decision this cycle and the other is a station nobody has waived
        # in yet.  There is no third reason any more -- the outage one this
        # incident briefly needed died with the query that caused it, since
        # a missing table can no longer refuse anybody.
        "reason": (
            "live_station_held"
            if wanted in set(scope["probation_holds"])
            else "live_station_probation"
        ),
        "station": wanted,
        "scope": scope["scope"],
        "roster_source": source,
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
