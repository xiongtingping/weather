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
from datetime import datetime, timezone
from typing import Callable, Any, Mapping, Sequence

SCHEMA = "serving_arithmetic_fingerprint_v1"

# The functions that decide what each stage serves.  Naming functions rather
# than modules keeps one stage's edits from resetting another's evidence:
# materialization.py holds all three stages, and hashing the file would make
# any P1 change cost P0 its accumulated days.
STAGE_SOURCES: Mapping[str, Sequence[tuple[str, str]]] = {
    "P0": (
        ("bayes_basket_target.native_ensemble_p0", "compile_native_ensemble_p0"),
        ("bayes_basket_target.center_width_calibration",
         "apply_center_width_calibration"),
    ),
    "P0-M": (
        ("bayes_basket_target.materialization", "materialize_p0m"),
        ("bayes_basket_target.materialization", "materialize_p0m_observed"),
        ("bayes_basket_target.center_width_calibration",
         "apply_center_width_calibration"),
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


def fingerprint(stage: str) -> str | None:
    """One hash over the arithmetic this stage serves, or None if unreadable."""

    entries = STAGE_SOURCES.get(str(stage))
    if not entries:
        return None
    digest = hashlib.sha256()
    for module_name, function_name in entries:
        try:
            module = importlib.import_module(module_name)
            target = getattr(module, function_name)
            source = inspect.getsource(target)
            tree = _strip_docstrings(ast.parse(ast.unparse(ast.parse(source))))
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
    }
    if current is None:
        return out
    since = anchor(conn, stage=stage)
    out["fingerprint_since_utc"] = since
    if not since:
        return out
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
