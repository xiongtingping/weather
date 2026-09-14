"""When each stage's served arithmetic last changed, without anyone saying so.

The calibration scalars measure how wrong the *served* distribution is, so a
fit must not reach back past the moment that distribution changed shape.  That
boundary used to be a date kept by hand in the daily script, which is a second
place to remember and was wrong the day it mattered: it still pointed at the
2026-08-18 width fix on the day the ceiling landed.  A version string on the
envelope is no better -- the 2026-08-18 fix left P0's string untouched, so a
bound derived from it alone reached back three weeks too far.

So the code is asked directly.  Each stage names the functions that actually
compute what it serves, and their ASTs are hashed.  An AST carries no comments,
no blank lines and no formatting, so the heavy commentary this codebase runs on
can be rewritten freely without costing the layer its evidence; a change to the
arithmetic itself does move the hash.  Docstrings are stripped for the same
reason.

The first time a fingerprint is seen it is stamped, and that stamp is the
boundary from then on.  Nobody edits a date, and nobody has to notice that a
change was a serving change.
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import sqlite3
import textwrap
from datetime import datetime, timezone
from typing import Callable, Any, Mapping, Sequence

SCHEMA = "serving_arithmetic_fingerprint_v1"

# The functions that decide what each stage serves.  Naming functions rather
# than modules keeps one stage's edits from resetting another's evidence:
# materialization.py holds all three stages, and hashing the file would make
# any P1 change cost P0 its accumulated days.
# Each root is expanded to its call closure inside this package before it is
# hashed (``_closure``).  Measured 2026-09-08: charter 86 halved P0's served
# width by changing ``native_ensemble_oos._fit_parameters`` -- a function no
# root here named and none of them calls at serving time, because its output
# reaches serving baked into fitted artifacts -- and the fingerprint did not
# move.  Two consequences, both fixed here.  A serving root's callees are
# part of its arithmetic (``compile_native_ensemble_p0`` imports
# ``_resolved_bias_c`` from the OOS module and that function decides the
# served centre), so the closure is hashed, not the root's own body.  And
# the training entry whose artifacts a stage serves is a root in its own
# right: what P0 serves is decided as much when the artifact is fitted as
# when it is compiled.  P0-M is built on P0's root and moves with it.
# The three table lanes are roots for the reason ``_fit_parameters`` is one:
# what a stage serves is decided as much where the number is fitted as where
# it is applied, and both reach serving baked into a row rather than through a
# call.  They were left out when the training entry became a root, and the gap
# is not theoretical -- two commits on 2026-09-10 changed what both lanes will
# store (the width lane was composing its answer into the wrong space; the
# band lane was pooling evidence across a retired shape) and moved no
# stage's fingerprint at all, because only the LOADERS of those tables were
# reachable from the serving roots.
STAGE_SOURCES: Mapping[str, Sequence[tuple[str, str]]] = {
    "P0": (
        ("bayes_basket_target.native_ensemble_p0", "compile_native_ensemble_p0"),
        ("bayes_basket_target.native_ensemble_oos", "build_native_ensemble_oos"),
        ("bayes_basket_target.native_ensemble_oos", "_fit_parameters"),
        ("bayes_basket_target.center_width_calibration",
         "apply_center_width_calibration"),
        ("bayes_basket_target.center_width_store", "fit_center_width"),
        ("bayes_basket_target.center_width_store", "fit_station_width"),
        ("bayes_basket_target.band_reliability_store",
         "fit_band_reliability_table"),
    ),
    "P0-M": (
        # ``materialize_p0m`` (the calibrated TAF operator) left this list
        # 2026-09-13 when that branch went offline (charter 133): P0-M
        # serves only the observed operator.
        ("bayes_basket_target.materialization", "materialize_p0m_observed"),
        ("bayes_basket_target.native_ensemble_oos", "build_native_ensemble_oos"),
        ("bayes_basket_target.native_ensemble_oos", "_fit_parameters"),
        ("bayes_basket_target.center_width_calibration",
         "apply_center_width_calibration"),
        ("bayes_basket_target.center_width_store", "fit_center_width"),
        ("bayes_basket_target.center_width_store", "fit_station_width"),
        ("bayes_basket_target.band_reliability_store",
         "fit_band_reliability_table"),
    ),
    "P1": (
        ("bayes_basket_target.materialization", "materialize_p1"),
        ("bayes_basket_target.metar_trajectory",
         "compile_metar_trajectory_update_proof"),
        ("bayes_basket_target.metar_ceiling", "apply_ceiling"),
        ("bayes_basket_target.p1_compiler", "_bucket_geometry_c"),
        ("bayes_basket_target.p1_compiler", "_hard_exclusion_threshold_c"),
        ("bayes_basket_target.center_width_calibration",
         "apply_center_width_calibration"),
        ("bayes_basket_target.center_width_store", "fit_center_width"),
        ("bayes_basket_target.center_width_store", "fit_station_width"),
        ("bayes_basket_target.band_reliability_store",
         "fit_band_reliability_table"),
    ),
}


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    """Prose is not arithmetic; a rewritten docstring must cost nothing."""

    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                   ast.Module)
        ):
            continue
        body = getattr(node, "body", None)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return tree


PACKAGE = "bayes_basket_target"
# This module is the instrument, not the arithmetic.  The fitting lanes below
# call ``served_arithmetic_anchor``, which calls back in here, so without the
# carve-out every edit to the watchdog lights the watchdog -- and a lamp that
# reports its own maintenance is a lamp nobody reads.  Roots are explicit and
# are never excluded; this only stops the expansion walking back into here.
EXCLUDED_MODULES = frozenset({f"{PACKAGE}.serving_arithmetic"})


def _function_source_tree(target: Any) -> ast.AST:
    source = textwrap.dedent(inspect.getsource(target))
    return _strip_docstrings(ast.parse(ast.unparse(ast.parse(source))))


def _direct_callees(module: Any, target: Any) -> list[tuple[str, str]]:
    """Module-level functions of this package that ``target`` calls by name.

    ``f(...)`` resolves through the module's own globals, which is where an
    imported helper lands (``from .native_ensemble_oos import _resolved_bias_c``
    puts it in ``native_ensemble_p0``'s namespace with ``__module__`` still
    naming its home).  ``mod.f(...)`` resolves when ``mod`` is a package
    module.  Methods, closures and dynamic dispatch are not followed: the
    hash is a watchdog, and a watchdog that could not be computed is worse
    than one with a named blind spot.
    """

    tree = _function_source_tree(target)
    names: set[str] = set()
    attrs: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if isinstance(callee, ast.Name):
            names.add(callee.id)
        elif isinstance(callee, ast.Attribute) and isinstance(
            callee.value, ast.Name
        ):
            attrs.add((callee.value.id, callee.attr))
    found: list[tuple[str, str]] = []
    for name in sorted(names):
        obj = getattr(module, name, None)
        if inspect.isfunction(obj) and str(obj.__module__ or "").startswith(
            PACKAGE
        ):
            found.append((str(obj.__module__), str(obj.__name__)))
    for holder_name, attr in sorted(attrs):
        holder = getattr(module, holder_name, None)
        if inspect.ismodule(holder) and str(holder.__name__).startswith(PACKAGE):
            obj = getattr(holder, attr, None)
            if inspect.isfunction(obj):
                found.append((str(obj.__module__), str(obj.__name__)))
    return found


def _closure(entries: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """Every package function reachable by name from ``entries``, sorted.

    Sorted so the hash does not depend on traversal order; a function
    reached from two roots is hashed once.  A root that cannot be resolved
    raises, and the caller turns that into "no fingerprint" -- the same
    contract the single-function hash had.
    """

    seen: set[tuple[str, str]] = set()
    queue = list(entries)
    while queue:
        module_name, function_name = queue.pop()
        key = (module_name, function_name)
        if key in seen:
            continue
        module = importlib.import_module(module_name)
        target = getattr(module, function_name)
        if not inspect.isfunction(target):
            raise TypeError(f"not a function: {module_name}.{function_name}")
        seen.add(key)
        home = importlib.import_module(str(target.__module__))
        for callee in _direct_callees(home, target):
            if callee[0] in EXCLUDED_MODULES:
                continue
            if callee not in seen:
                queue.append(callee)
    return sorted(seen)


# When a served version's arithmetic actually started, for versions whose
# bump was typed later than the change it names.
#
# The anchor a calibration lane keys its evidence off is otherwise inferred:
# the earliest decision still carrying the version being served now.  That
# inference makes a bump cost a lane every day it has accumulated, because a
# version bumped today first appears today -- so the bump never gets made, the
# watchdog's lamp stays on, and the boundary is carried by a hand-set flag in
# the sweep instead.  Charter 122 wrote the trap down on 2026-09-08 and chose
# the flag: "A version bump today would anchor at today and throw away the
# de-blended days already served."
#
# A version may therefore say when its arithmetic began.  The claim has the
# same standing as the version string beside it -- both are somebody's
# assertion about what is served -- and the fingerprint watchdog still checks
# the code side independently.  An entry for a version nobody serves is inert;
# a version with no entry behaves exactly as it did before this existed.
#
# 2026-09-06T04:18:00Z is the moment the last of three width changes reached
# serving -- the centre/width layer wired at lambda 0.75 (09-03), charter 86
# retiring the climate blend, and the station width table at 0.5 on 32 of 45
# stations -- which together took P0's served sigma from 1.6 to 0.70 bucket
# steps and left both versions behind.  It is the same moment the refit sweep
# has been passing by hand as ``--since-stage`` since 2026-09-08; the flag is
# now a fallback for the version's first hours rather than the only carrier.
#
# v4 (both stages): the per-station width tables were retired by the lane's
# incumbent veto -- P0 at 2026-09-10T15:27:09Z, P0-M at 15:27:11Z.  Cells
# served under the 0.5 scale would read to the next width fit as "widen",
# which the retirement already did; the clock starts at the retirement.
STAGE_VERSION_EFFECTIVE_FROM: Mapping[str, Mapping[str, str]] = {
    "P0": {
        "calibrated_native_cross_ensemble_p0_v3": "2026-09-06T04:18:00Z",
        "calibrated_native_cross_ensemble_p0_v4": "2026-09-10T15:27:09Z",
        "calibrated_native_cross_ensemble_p0_v5": "2026-09-14T04:09:49Z",
    },
    "P0-M": {
        "p0m_taf_observed_v3": "2026-09-06T04:18:00Z",
        "p0m_taf_observed_v4": "2026-09-10T15:27:11Z",
        "p0m_taf_observed_v5": "2026-09-14T04:09:49Z",
    },
}


def version_effective_from(stage: str, version: str) -> str | None:
    """When ``version``'s arithmetic began, if the bump said so."""

    declared = STAGE_VERSION_EFFECTIVE_FROM.get(str(stage)) or {}
    value = declared.get(str(version))
    return str(value) if value else None


def closure(stage: str) -> list[tuple[str, str]] | None:
    """The functions a stage's fingerprint covers, for an audit to read."""

    entries = STAGE_SOURCES.get(str(stage))
    if not entries:
        return None
    try:
        return _closure(entries)
    except (ImportError, AttributeError, OSError, SyntaxError, TypeError):
        return None


def fingerprint(stage: str) -> str | None:
    """One hash over the arithmetic this stage serves, or None if unreadable."""

    members = closure(stage)
    if members is None:
        return None
    digest = hashlib.sha256()
    for module_name, function_name in members:
        try:
            module = importlib.import_module(module_name)
            tree = _function_source_tree(getattr(module, function_name))
        except (ImportError, AttributeError, OSError, SyntaxError, TypeError):
            # A source we cannot read yields no fingerprint at all rather than
            # a partial one: a partial hash would move on unrelated packaging
            # changes and reset evidence for no reason.
            return None
        digest.update(module_name.encode())
        digest.update(function_name.encode())
        digest.update(ast.dump(tree).encode())
    return digest.hexdigest()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS calibration_serving_arithmetic("
        " stage TEXT NOT NULL,"
        " fingerprint_sha256 TEXT NOT NULL,"
        " first_seen_utc TEXT NOT NULL,"
        " PRIMARY KEY(stage, fingerprint_sha256))"
    )
    # The other half of the watchdog's sentence.  A fingerprint later than
    # the served version "means one of two things, and only whoever wrote
    # the commit can say which" -- and until 2026-09-08 there was no place to
    # say it, so the discrepancy had been true for every stage on every day
    # since the mechanism landed (P0: seventeen fingerprints, one version).
    # A lamp that cannot go out is not read.  An acknowledgement says the
    # shape served under this fingerprint is the version's shape; a real
    # change is answered by a version bump, never by a row here.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS calibration_serving_arithmetic_ack("
        " stage TEXT NOT NULL,"
        " fingerprint_sha256 TEXT NOT NULL,"
        " acknowledged_at_utc TEXT NOT NULL,"
        " reason TEXT NOT NULL,"
        " PRIMARY KEY(stage, fingerprint_sha256))"
    )


def acknowledge(
    conn: sqlite3.Connection,
    *,
    stage: str,
    reason: str,
    fingerprint_sha256: str | None = None,
    now_utc: str | None = None,
) -> dict[str, Any]:
    """Record that the current (or named) fingerprint is inert.

    Inert means: the arithmetic moved, the served shape did not, and the
    version it carries still describes it.  The reason is required and is
    the audit's only record of who judged that and on what grounds.
    """

    if not str(reason or "").strip():
        raise ValueError("serving_arithmetic_ack_reason_required")
    target = fingerprint_sha256 or fingerprint(stage)
    if not target:
        raise ValueError("serving_arithmetic_ack_fingerprint_unreadable")
    ensure_schema(conn)
    stamp = now_utc or datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    conn.execute(
        "INSERT OR REPLACE INTO calibration_serving_arithmetic_ack"
        "(stage, fingerprint_sha256, acknowledged_at_utc, reason)"
        " VALUES(?,?,?,?)",
        (str(stage), str(target), stamp, str(reason).strip()),
    )
    return {
        "stage": str(stage),
        "fingerprint": str(target),
        "acknowledged_at_utc": stamp,
        "reason": str(reason).strip(),
    }


def withdraw(
    conn: sqlite3.Connection,
    *,
    stage: str,
    fingerprint_sha256: str | None = None,
) -> dict[str, Any]:
    """Take back an acknowledgement, relighting the lamp for that fingerprint.

    An acknowledgement is a judgement, and a judgement made on a reading that
    could not see everything has to be retractable -- otherwise the only way
    back is editing the table by hand, which leaves no trace of who decided
    what.  The first row this table ever held, 2026-09-10T09:22Z, was exactly
    that case: the judge measured the served shape across the moment the code
    landed, and the change it was judging was a change to two FITTING lanes
    whose numbers do not reach serving until those lanes next activate.  It
    read inert because the step had not been taken yet.
    """

    target = fingerprint_sha256 or fingerprint(stage)
    if not target:
        raise ValueError("serving_arithmetic_ack_fingerprint_unreadable")
    ensure_schema(conn)
    cursor = conn.execute(
        "DELETE FROM calibration_serving_arithmetic_ack"
        " WHERE stage=? AND fingerprint_sha256=?",
        (str(stage), str(target)),
    )
    return {
        "stage": str(stage),
        "fingerprint": str(target),
        "withdrawn": int(cursor.rowcount or 0),
    }


def acknowledged(
    conn: sqlite3.Connection, *, stage: str, fingerprint_sha256: str
) -> dict[str, Any] | None:
    try:
        row = conn.execute(
            "SELECT acknowledged_at_utc, reason"
            " FROM calibration_serving_arithmetic_ack"
            " WHERE stage=? AND fingerprint_sha256=?",
            (str(stage), str(fingerprint_sha256)),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    return {"acknowledged_at_utc": str(row[0]), "reason": str(row[1])}


def record(
    conn: sqlite3.Connection,
    *,
    stage: str,
    now_utc: str | None = None,
    bootstrap_utc: "str | Callable[[], str | None] | None" = None,
) -> str | None:
    """Stamp this stage's current arithmetic the first time it is seen.

    Later sightings keep the earliest stamp: the boundary is when the shape
    started serving, not when something last looked.

    The very first stamp for a stage is different in kind.  A fingerprint
    appearing while the table already holds one for that stage means the
    arithmetic changed just now, and "now" is the honest boundary.  A
    fingerprint appearing into an empty table says only that nobody had looked
    before -- the shape may have been serving for weeks -- so ``bootstrap_utc``
    is used instead, and stamping "now" there would throw away every day of
    evidence the stage had legitimately earned.

    ``bootstrap_utc`` may be a callable, and callers whose bootstrap is
    expensive should pass one.  It is consulted only on that first-ever
    stamp, which is the rarest path there is -- every stage in production has
    been stamped for weeks -- but Python evaluates an argument whether or not
    the callee reads it.  The refit paid 202 seconds a round computing a
    boundary that ``seen_before`` then threw away, three times, because the
    call site could not know the predicate.  Keeping the predicate here is
    the point: only this function knows when the value is wanted.
    """

    current = fingerprint(stage)
    if current is None:
        return None
    ensure_schema(conn)
    seen_before = conn.execute(
        "SELECT 1 FROM calibration_serving_arithmetic WHERE stage=? LIMIT 1",
        (str(stage),),
    ).fetchone() is not None
    stamp = now_utc or datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    if not seen_before and bootstrap_utc:
        resolved = bootstrap_utc() if callable(bootstrap_utc) else bootstrap_utc
        if resolved:
            stamp = str(resolved)
    conn.execute(
        "INSERT OR IGNORE INTO calibration_serving_arithmetic"
        "(stage, fingerprint_sha256, first_seen_utc) VALUES(?,?,?)",
        (str(stage), current, stamp),
    )
    return current


def anchor(conn: sqlite3.Connection, *, stage: str) -> str | None:
    """When the arithmetic being served now was first seen serving."""

    current = fingerprint(stage)
    if current is None:
        return None
    try:
        row = conn.execute(
            "SELECT first_seen_utc FROM calibration_serving_arithmetic"
            " WHERE stage=? AND fingerprint_sha256=?",
            (str(stage), current),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return str(row[0]) if row and row[0] else None


def watchdog(conn: sqlite3.Connection, *, stage: str) -> dict[str, Any]:
    """Did the serving code move after the version that describes it did?

    The fingerprint stopped being an evidence boundary on 2026-08-27 (it was
    resetting eight-day clocks for commits that only wrote a receipt) and
    became this: a discrepancy that names what happened and asks for the
    version bump that was skipped, instead of quietly spending the evidence.

    A fingerprint stamped after the served version started serving means one
    of two things, and only whoever wrote the commit can say which: the
    change was inert -- a receipt, a replay door, a refactor -- and nothing
    is owed; or it changed what is served, the version owes a bump, and
    until it gets one the fit is pooling two shapes.  Reported, never
    enforced: a watchdog that failed the fit closed would be the old bound
    again under a new name.

    Answered with a descending seek rather than the version anchor's own
    minimum, which walks every older row of the stage -- 147,870 of them for
    P1.  A committed decision that predates the code change while carrying
    the version being served now proves the version is the older of the two,
    which is the whole question.
    """

    current = fingerprint(stage)
    out: dict[str, Any] = {
        "stage": str(stage),
        "fingerprint": current,
        "fingerprint_since_utc": None,
        "served_version": None,
        "discrepancy": False,
        "acknowledged": None,
    }
    if current is None:
        return out
    since = anchor(conn, stage=stage)
    out["fingerprint_since_utc"] = since
    if not since:
        return out
    ack = acknowledged(conn, stage=stage, fingerprint_sha256=current)
    out["acknowledged"] = ack
    try:
        latest = conn.execute(
            "SELECT e.posterior_version"
            " FROM basket_probability_bridge_history h"
            " JOIN basket_probability_envelope e"
            "   ON e.envelope_id = h.checkpoint_id"
            " WHERE h.stage=? AND h.state='COMMITTED'"
            " ORDER BY h.decision_at_utc DESC LIMIT 1",
            (str(stage),),
        ).fetchone()
        if not latest or not latest[0]:
            return out
        out["served_version"] = str(latest[0])
        if ack is not None:
            # Judged inert by whoever could judge it.  The lamp goes out
            # for this fingerprint only; the next arithmetic change lights
            # it again.  The served version is still reported: it is the
            # evidence boundary every reader of this stage is waiting on,
            # and returning before naming it printed "边界取服务版本 —" on
            # a page whose next line is a countdown to that boundary
            # (user report, 2026-09-12).
            return out
        declared = version_effective_from(str(stage), out["served_version"])
        if declared:
            # A version that says when it began is compared against that,
            # never against when its string first appeared.  Otherwise every
            # bump clears this lamp on its own -- a brand new string has no
            # decision older than anything -- and making bumps cheap, which
            # the declaration does, would have made clearing it cheap too.
            out["version_effective_from_utc"] = declared
            out["discrepancy"] = str(since) > str(declared)
            return out
        older = conn.execute(
            "SELECT 1"
            " FROM basket_probability_bridge_history h"
            " JOIN basket_probability_envelope e"
            "   ON e.envelope_id = h.checkpoint_id"
            " WHERE h.stage=? AND h.state='COMMITTED'"
            " AND h.decision_at_utc<? AND e.posterior_version=?"
            " ORDER BY h.decision_at_utc DESC LIMIT 1",
            (str(stage), str(since), out["served_version"]),
        ).fetchone()
    except (sqlite3.OperationalError, TypeError, ValueError):
        return out
    out["discrepancy"] = older is not None
    return out
