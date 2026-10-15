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
# The table lanes are roots for the reason ``_fit_parameters`` is one:
# what a stage serves is decided as much where the number is fitted as where
# it is applied, and both reach serving baked into a row rather than through a
# call.  They were left out when the training entry became a root, and the gap
# is not theoretical -- two commits on 2026-09-10 changed what both lanes will
# store (the width lane was composing its answer into the wrong space; the
# band lane was pooling evidence across a retired shape) and moved no
# stage's fingerprint at all, because only the LOADERS of those tables were
# reachable from the serving roots.  The band lane left both lists with its
# layer (charter 232, 2026-09-26).
STAGE_SOURCES: Mapping[str, Sequence[tuple[str, str]]] = {
    "P0": (
        ("bayes_basket_target.native_ensemble_p0", "compile_native_ensemble_p0"),
        ("bayes_basket_target.native_ensemble_oos", "build_native_ensemble_oos"),
        ("bayes_basket_target.native_ensemble_oos", "_fit_parameters"),
        ("bayes_basket_target.center_width_calibration",
         "apply_center_width_calibration"),
        ("bayes_basket_target.center_width_store", "fit_center_width"),
        ("bayes_basket_target.center_width_store", "fit_station_width"),
    ),
    "P0-M": (
        # ``materialize_p0m`` (the calibrated TAF operator) left this list
        # 2026-09-13 when that branch went offline (charter 133): P0-M
        # serves only the observed operator.  Since charter 150 that operator
        # is P0 times the TAF likelihood, whose arithmetic the closure
        # reaches through the call; the entries below it are P0's root,
        # which this stage moves with -- P0-M applies no table of its own.
        ("bayes_basket_target.materialization", "materialize_p0m_observed"),
        ("bayes_basket_target.native_ensemble_oos", "build_native_ensemble_oos"),
        ("bayes_basket_target.native_ensemble_oos", "_fit_parameters"),
        ("bayes_basket_target.center_width_calibration",
         "apply_center_width_calibration"),
        ("bayes_basket_target.center_width_store", "fit_center_width"),
        ("bayes_basket_target.center_width_store", "fit_station_width"),
    ),
    # Charter 150: the root under the hard bound times one joint observation
    # likelihood.  ``materialize_p1`` reaches the likelihood's arithmetic
    # through its call; the step's features are computed in the compiler and
    # named here because they reach serving through the packet, not a call.
    # The ceiling, the centre/width scalars and the band table left with the
    # layers they served.
    "P1": (
        ("bayes_basket_target.materialization", "materialize_p1"),
        ("bayes_basket_target.metar_trajectory",
         "compile_metar_trajectory_update_proof"),
        ("bayes_basket_target.p1_compiler", "_observation_features_for_step"),
        ("bayes_basket_target.p1_compiler", "_bucket_geometry_c"),
        ("bayes_basket_target.p1_compiler", "_hard_exclusion_threshold_c"),
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
#
# v5 (P0, P0-M) and P1 v10: charter 134's stable pool.  First declared at
# 05:10:36Z, when the fleet's last pool seat was minted, and moved the same
# morning to 05:52:13Z: two serving changes finished the rollout after that
# moment -- seats that keep the P0 fresh rank first (restart 05:31:22Z) and
# pools mint their pair views (restart 05:52:12Z) -- and the watchdog rightly
# lit for both.  Nothing had been fitted on the 42 minutes in between, so the
# move cost no lane anything.  P1 declares its root's moment.
#
# v6 (P0, P0-M) and P1 v11: charter 149, a member is calibrated under its own
# run's hour lead instead of the packet's.  Declared at the restart that
# deployed it; nothing serves during a restart, so no v5 cell falls after it.
# First typed 09:17:10Z, moved to 09:18:16Z because the fingerprint's first
# sighting is the resident's start (09:18:15.552Z) and the watchdog rightly
# lit for the minute between; no envelope of any version fell in it, and the
# first v6 cell is 09:18:21.8Z.
#
# P1 v14: charter 157, each station's clock shifted by its own peak hour;
# declared at the restart that deploys it, like v13.
#
# P1 v13: charter 156, alpha as a curve and the rise clock read between bin
# midpoints.  P1's own change, declared at the restart that deploys it (see
# the entry) rather than inferred, for the same 62s the v12 note explains.
#
# P0-M v7 and P1 v12: charter 150, each stage's own change, so the moment is
# not a root's but the version's own first decision -- declared, verbatim, as
# the value the inference returns, so no reader's bound moves by a row.  It
# is declared only because inferring it costs: the MIN walks every older row
# of the stage, 62.2s for P1 on 2026-09-19, paid by each of the six refit
# lanes that read the anchor and by the board, and it grows ~5k rows a day.
# The 2026-09-18T20:15Z calibration_refit ran 744s against a 900s timeout,
# every P1 step 60-80s slower than the day before.  Verbatim matters: the
# first v12 decision is stamped 19:10:09.000001Z, and as a string that sorts
# BELOW 19:10:09Z.  Both precede the resident's start (19:12:48.636Z)
# because the decision clock is the data's, not the process's.  The watchdog
# reads the same either way: P1's fingerprint is acknowledged and P0-M's is
# pending its refits, which a declaration does not change.
STAGE_VERSION_EFFECTIVE_FROM: Mapping[str, Mapping[str, str]] = {
    "P0": {
        "calibrated_native_cross_ensemble_p0_v3": "2026-09-06T04:18:00Z",
        "calibrated_native_cross_ensemble_p0_v4": "2026-09-10T15:27:09Z",
        "calibrated_native_cross_ensemble_p0_v5": "2026-09-14T05:52:13Z",
        "calibrated_native_cross_ensemble_p0_v6": "2026-09-18T09:18:16Z",
        # Charter 160: the radiation shift on the clearness index, every lead
        # at 0.15.  Declared past the commit and ahead of the restart that
        # deploys it, as P1 v13 was; the v7 envelopes between the resident's
        # start and this instant are not evidence yet.
        "calibrated_native_cross_ensemble_p0_v7": "2026-09-19T15:02:00Z",
        # Charter 190: the width layer carries open edge buckets as tails and
        # the width fit reads serving's axis.  Declared past the commit and
        # before the restart that deploys it (the coordinator's ruling): no
        # v8 envelope exists before the restart, so the declared moment only
        # bounds the evidence from below.  The fingerprint's first sighting
        # is that restart, which falls after this instant, so the watchdog
        # lamp for all three stages lights until the new fingerprints are
        # acknowledged (or this moment is moved to the restart, as v6 was).
        # Moved to the restart: declared 15:00:00Z, first sighting of all
        # three fingerprints 15:02:20.673Z (the resident's start), so the
        # declaration now sits one second past it, as v6 was moved.
        "calibrated_native_cross_ensemble_p0_v8": "2026-09-21T15:02:21Z",
        # Charter 198: the rung read once (10/9 C on Fahrenheit ladders) and
        # the activation read without its 256-row cut.  Declared past the
        # push and past the batch restart that deploys it (the coordinator's
        # instruction after v8's early declaration lit the watchdog): the
        # fingerprints' first sighting is that restart, which falls before
        # this instant, and the v9 envelopes between the two are simply not
        # evidence yet.  Moved from 10:30:00Z to 12:00:00Z at the
        # coordinator's request: the batch restart waits for two lanes still
        # finishing in the working tree.
        "calibrated_native_cross_ensemble_p0_v9": "2026-09-22T12:00:00Z",
        # Charter 204: AIFS ENS seated.  Same-as-of A/B builds (2026-09-23
        # 13:45Z, KLGA/EGLC/RJTT) keep every four-system view that survives
        # byte-identical, but where AIFS qualifies the pool becomes the
        # five-system one and a packet without its run serves its four-system
        # missing-member view (certified with its own weights; charter 236
        # corrects "renormalised") -- the four-family arithmetic moved.
        # Declared past the push and past the coordinator's batch restart
        # that deploys it (the watchdog reads a fingerprint first seen after
        # its declaration as a discrepancy, so the restart must come first:
        # moved from 14:30:00Z to 16:00:00Z and then 17:00:00Z at the coordinator's
        # request, as v9 was, when the 15:40-15:55Z batch did not land);
        # the v10 envelopes between the restart and this instant are not
        # evidence yet.
        "calibrated_native_cross_ensemble_p0_v10": "2026-09-23T17:00:00Z",
        # Charter 210: every artifact served at the fleet radiation
        # coefficient 0.07 (pins recorded, not served).  Declared past the
        # push and past the batch restart that deploys it; the v11 envelopes
        # between that restart and this instant are not evidence yet.
        "calibrated_native_cross_ensemble_p0_v11": "2026-09-23T18:00:00Z",
        # Charter 212: NOAA AIGEFS seated, the fifth independent family.
        # Where it qualifies the pool gains a family and a packet without its
        # run serves the pool's missing-member view.  Declared past the push
        # and past the restart that deploys it; the v12 envelopes between that
        # restart and this instant are not evidence yet.
        "calibrated_native_cross_ensemble_p0_v12": "2026-09-24T02:00:00Z",
        # Charter 199: the fusion blend serves in place of the seat weights.
        # Declared past the push and past the restart that deploys it; the
        # v13 envelopes between that restart and this instant are not
        # evidence yet.
        "calibrated_native_cross_ensemble_p0_v13": "2026-09-24T16:30:00Z",
        # Charter 216: each native ensemble member's centre and spread are
        # refitted by bucket log loss and shrunk half-way to the moments.
        # Declared past the push and past the restart that deploys it.  That
        # restart is 01:06:15Z, requested for the store ceiling (77d1c841):
        # it loaded this code from the tree two minutes before the commit
        # (01:08:39Z), and the first 216 certificates are 01:06:18Z.
        "calibrated_native_cross_ensemble_p0_v14": "2026-09-25T02:00:00Z",
        # Charter 217: AIFS ENS is its own independence family (user ruling
        # 2026-09-25).  Declared past the push and past the restart that
        # deploys it; artifacts minted before the split keep serving under
        # their certified map until the OOS rotation re-mints them, and those
        # envelopes are not evidence for this version.
        "calibrated_native_cross_ensemble_p0_v15": "2026-09-25T06:00:00Z",
        # Charter 222: the radiation coefficient splits into a warm side
        # (0.12) and a cold side (0.04), centred on each station's own
        # history.  Written in a cloud session that cannot restart the
        # resident: declared past the push, ahead of the restart that
        # deploys it on the production machine.  If that restart lands
        # later, move this instant to it (as v9 and v10 were moved) -- the
        # watchdog reads a fingerprint first seen after its declaration as a
        # discrepancy.
        "calibrated_native_cross_ensemble_p0_v16": "2026-09-25T12:00:00Z",
        # Charter 224: the served vector ends at the stages' one model-zero
        # floor (1e-3), as P0-M and P1 have since 2026-08-26.  Declared past
        # the push and past the restart that deploys it, and after charter
        # 222's v16 so the versions stay in order.
        "calibrated_native_cross_ensemble_p0_v17": "2026-09-25T13:00:00Z",
        # Charter 222 (2026-09-25 ruling): the radiation pair moves to warm
        # 0.18 / cold 0.06.  Declared past the push and past the restart that
        # deploys it, and after charter 224's v17 so the versions stay in
        # order; the v18 envelopes between that restart and this instant are
        # not evidence yet.
        "calibrated_native_cross_ensemble_p0_v18": "2026-09-25T13:30:00Z",
        # Charter 199 (2026-09-25): the eleven US ASOS stations blended like
        # every other station.  Declared past the push and past the restart
        # that deploys it, and after v18 so the versions stay in order.
        "calibrated_native_cross_ensemble_p0_v19": "2026-09-25T14:00:00Z",
        # Charter 236: pool seats rank breadth, then the pool weight they
        # keep, ahead of the instant they were minted.  Written in a cloud
        # session that cannot restart the resident, as charter 222 was:
        # declared past the push, and the restart that deploys it on the
        # production machine has to land before this instant (Beijing
        # midnight, 10-01/02; first declared 09-28T01:00Z, then 09-28T16:00Z,
        # moved each time the deploy could not be made by then).  If that
        # restart lands later, move this instant (and P0-M v22's, P1 v31's)
        # past it -- the watchdog reads a fingerprint first seen after its
        # declaration as a discrepancy.
        "calibrated_native_cross_ensemble_p0_v20": "2026-10-01T16:00:00Z",
        # Charter 242: the fusion layer's station weights become the station
        # x model log-score mixture, shrunk per system by its station-days.
        # Declared past the push and past the restart that deploys it, and
        # after v20 and P0-M v23 so the versions stay in order.
        "calibrated_native_cross_ensemble_p0_v21": "2026-10-01T19:30:00Z",
        # Charter 243: pool seats rank breadth before the rider count.
        # Declared past the push and the restart that deploys it.
        "calibrated_native_cross_ensemble_p0_v22": "2026-10-02T03:00:00Z",
        # Charter 244: stable-pool seat kappa 0.25.  Declared past the push
        # and restart; pool certificates re-mint over the next daily OOS
        # rotation, so v23 envelopes in that day mix kappa 0.5 and 0.25 seats.
        "calibrated_native_cross_ensemble_p0_v23": "2026-10-02T04:30:00Z",
        # Charter 246: the consensus bridge ramps from 1.0C to 1.5C.
        # Declared past the push and the restart that deploys it.
        "calibrated_native_cross_ensemble_p0_v24": "2026-10-02T06:30:00Z",
        # Charter 247: single-system width laid on by widening the kernel
        # (dress_v1), not by stretching the members.  Declared past the
        # push and past the restart that deploys it.
        "calibrated_native_cross_ensemble_p0_v25": "2026-10-02T14:30:00Z",
        # Charter 248: the thin-far slice is a tail mix on the stage pair
        # (kappa 1.5, the weight fitted nightly), not a lambda replacing it.
        # Declared past the push and past the restart that deploys it.
        "calibrated_native_cross_ensemble_p0_v26": "2026-10-02T17:30:00Z",
        # Charter 253: past 36 hours no bucket is served above 0.45 (the
        # far-peak ceiling), on artifacts whose holdout scored it.  Declared
        # past the push and past the restart that deploys it.
        "calibrated_native_cross_ensemble_p0_v27": "2026-10-04T15:45:00Z",
        # Charter 261: past 36 hours an F-station vector is served the fixed
        # tail mix (weight 0.10, scale 1.5), on artifacts whose holdout
        # scored it.  Declared past the push and past the restart that
        # deploys it.
        "calibrated_native_cross_ensemble_p0_v28": "2026-10-09T06:20:00Z",
        # Charter 271: the width layer's mode-to-shoulder share (identity
        # until the nightly lane admits one).  Declared past the push and
        # past the restart that deploys it.
        "calibrated_native_cross_ensemble_p0_v29": "2026-10-15T17:10:00Z",
    },
    "P0-M": {
        "p0m_taf_observed_v3": "2026-09-06T04:18:00Z",
        "p0m_taf_observed_v4": "2026-09-10T15:27:11Z",
        "p0m_taf_observed_v5": "2026-09-14T05:52:13Z",
        "p0m_taf_observed_v6": "2026-09-18T09:18:16Z",
        "p0m_taf_likelihood_v7": "2026-09-18T19:10:09Z",
        # Charter 158: declared ahead of the restart that deploys it and
        # after the tempered artifact is written, as P1 v14 was -- between
        # the restart and the write the stage serves v7's numbers under the
        # v8 label, and those envelopes are not evidence.
        "p0m_taf_likelihood_v8": "2026-09-19T10:15:00Z",
        # Charter 160: a root bump -- P0-M is P0 times the TAF likelihood, and
        # P0 moved -- at the instant P0 v7 declares.
        "p0m_taf_likelihood_v9": "2026-09-19T15:02:00Z",
        # Charter 190: a root bump at the instant P0 v8 declares.
        "p0m_taf_likelihood_v10": "2026-09-21T15:02:21Z",
        # Charter 198: a root bump at the instant P0 v9 declares.
        "p0m_taf_likelihood_v11": "2026-09-22T12:00:00Z",
        # Charter 204: root bump at the instant P0 v10 declares.
        "p0m_taf_likelihood_v12": "2026-09-23T17:00:00Z",
        # Charter 210: root bump at the instant P0 v11 declares.
        "p0m_taf_likelihood_v13": "2026-09-23T18:00:00Z",
        # Charter 212: root bump at the instant P0 v12 declares.
        "p0m_taf_likelihood_v14": "2026-09-24T02:00:00Z",
        # Charter 199: root bump at the instant P0 v13 declares.
        "p0m_taf_likelihood_v15": "2026-09-24T16:30:00Z",
        # Charter 216: root bump at the instant P0 v14 declares.
        "p0m_taf_likelihood_v16": "2026-09-25T02:00:00Z",
        # Charter 217: root bump at the instant P0 v15 declares.
        "p0m_taf_likelihood_v17": "2026-09-25T06:00:00Z",
        # Charter 222: root bump at the instant P0 v16 declares.
        "p0m_taf_likelihood_v18": "2026-09-25T12:00:00Z",
        # Charter 224: root bump at the instant P0 v17 declares.
        "p0m_taf_likelihood_v19": "2026-09-25T13:00:00Z",
        # Charter 222 ruling: root bump at the instant P0 v18 declares.
        "p0m_taf_likelihood_v20": "2026-09-25T13:30:00Z",
        # Charter 199 (ASOS blended): root bump at the instant P0 v19 declares.
        "p0m_taf_likelihood_v21": "2026-09-25T14:00:00Z",
        # Charter 236: root bump at the instant P0 v20 declares.
        "p0m_taf_likelihood_v22": "2026-10-01T16:00:00Z",
        # Charter 241: P0-M's own change -- the observed hard bound censors
        # (the ruled-out mass lands on the running high's bucket).  Deployed
        # by the 2026-10-01 restart that follows its commit; declared after
        # v22 so the versions stay in order, and past that restart, so the
        # v23 envelopes before this instant are simply not evidence yet.  P1
        # is not bumped: it roots on the proportional shape the P0-M carries
        # (``observed_floor_censoring.p1_root_probabilities``), which is the
        # vector v22 served, so P1's served numbers do not move.
        "p0m_taf_likelihood_v23": "2026-10-01T16:30:00Z",
        # Charter 242: root bump at the instant P0 v21 declares.
        "p0m_taf_likelihood_v24": "2026-10-01T19:30:00Z",
        # Charter 243: root bump at the instant P0 v22 declares.
        "p0m_taf_likelihood_v25": "2026-10-02T03:00:00Z",
        # Charter 244: root bump at the instant P0 v23 declares.
        "p0m_taf_likelihood_v26": "2026-10-02T04:30:00Z",
        # Charter 246: root bump at the instant P0 v24 declares.
        "p0m_taf_likelihood_v27": "2026-10-02T06:30:00Z",
        # Charter 247: root bump at the instant P0 v25 declares.
        "p0m_taf_likelihood_v28": "2026-10-02T14:30:00Z",
        # Charter 248: root bump at the instant P0 v26 declares.
        "p0m_taf_likelihood_v29": "2026-10-02T17:30:00Z",
        # Charter 253: root bump at the instant P0 v27 declares.
        "p0m_taf_likelihood_v30": "2026-10-04T15:45:00Z",
        # Charter 261: root bump at the instant P0 v28 declares.
        "p0m_taf_likelihood_v31": "2026-10-09T06:20:00Z",
        # Charter 271: root bump at the instant P0 v29 declares.
        "p0m_taf_likelihood_v32": "2026-10-15T17:10:00Z",
    },
    # P1 bumps on time for its own changes.  v10 is the first bump caused
    # upstream (charter 134 changed the P0 root it composes on), so it
    # declares the root's moment rather than its own first envelope; v11
    # (charter 149) is the second.  v12 (charter 150) is its own change and
    # declares its own first decision -- the inferred value, written down so
    # it is not re-derived by a scan (see above).
    "P1": {
        "p1_metar_trajectory_target_v10": "2026-09-14T05:52:13Z",
        "p1_metar_trajectory_target_v11": "2026-09-18T09:18:16Z",
        "p1_joint_likelihood_v12": "2026-09-18T19:10:09.000001Z",
        # Charter 156: declared a few minutes past the commit and ahead of the
        # restart that deploys it, so the fingerprint's first sighting (the
        # resident's start) is never later than the version's start, and no
        # v12 envelope falls inside v13's window.  The v13 envelopes between
        # the start and this instant are simply not evidence yet.
        "p1_joint_likelihood_v13": "2026-09-19T07:27:00Z",
        # Charter 157: declared the same way as v13, ahead of the restart.
        "p1_joint_likelihood_v14": "2026-09-19T09:05:00Z",
        # Charter 158: the third root bump (every P1 composes on a P0-M) --
        # the instant P0-M v8 declares.
        "p1_joint_likelihood_v15": "2026-09-19T10:15:00Z",
        # Charter 160: the fourth root bump, at the instant P0 v7 declares.
        "p1_joint_likelihood_v16": "2026-09-19T15:02:00Z",
        # Charter 161: P1's own change (the directly fitted update), declared
        # ahead of the restart that deploys it and after the artifact carrying
        # the block is written, as v14 was.
        "p1_joint_likelihood_v17": "2026-09-19T17:20:00Z",
        # Charter 190: the fifth root bump, at the instant P0 v8 declares.
        "p1_joint_likelihood_v18": "2026-09-21T15:02:21Z",
        # Charter 198: the sixth root bump, at the instant P0 v9 declares.
        "p1_joint_likelihood_v19": "2026-09-22T12:00:00Z",
        # Charter 200: P1's own change (the regional features' HRRR cell is
        # the nearest land cell; KSFO's water-cell rows are retired from every
        # reader, the pooled likelihood's samples with them).  Declared past
        # the push -- rounded up to the quarter hour, plus 45 minutes -- and
        # past the coordinator's batch restart that deploys it.
        "p1_joint_likelihood_v20": "2026-09-23T04:15:00Z",
        # Charter 204: the seventh root bump, at the instant P0 v10 declares.
        "p1_joint_likelihood_v21": "2026-09-23T17:00:00Z",
        # Charter 210: the eighth root bump, at the instant P0 v11 declares.
        "p1_joint_likelihood_v22": "2026-09-23T18:00:00Z",
        # Charter 212: the ninth root bump, at the instant P0 v12 declares.
        "p1_joint_likelihood_v23": "2026-09-24T02:00:00Z",
        # Charter 199: the tenth root bump, at the instant P0 v13 declares.
        "p1_joint_likelihood_v24": "2026-09-24T16:30:00Z",
        # Charter 216: the eleventh root bump, at the instant P0 v14 declares.
        "p1_joint_likelihood_v25": "2026-09-25T02:00:00Z",
        # Charter 217: the twelfth root bump, at the instant P0 v15 declares.
        "p1_joint_likelihood_v26": "2026-09-25T06:00:00Z",
        # Charter 222: the thirteenth root bump, at the instant P0 v16 declares.
        "p1_joint_likelihood_v27": "2026-09-25T12:00:00Z",
        # Charter 224: root bump at the instant P0 v17 declares.
        "p1_joint_likelihood_v28": "2026-09-25T13:00:00Z",
        # Charter 222 ruling: root bump at the instant P0 v18 declares.
        "p1_joint_likelihood_v29": "2026-09-25T13:30:00Z",
        # Charter 199 (ASOS blended): root bump at the instant P0 v19 declares.
        "p1_joint_likelihood_v30": "2026-09-25T14:00:00Z",
        # Charter 236: root bump at the instant P0 v20 declares.
        "p1_joint_likelihood_v31": "2026-10-01T16:00:00Z",
        # Charter 242: root bump at the instant P0 v21 declares.
        "p1_joint_likelihood_v32": "2026-10-01T19:30:00Z",
        # Charter 243: root bump at the instant P0 v22 declares.
        "p1_joint_likelihood_v33": "2026-10-02T03:00:00Z",
        # Charter 244: root bump at the instant P0 v23 declares.
        "p1_joint_likelihood_v34": "2026-10-02T04:30:00Z",
        # Charter 246: root bump at the instant P0 v24 declares.
        "p1_joint_likelihood_v35": "2026-10-02T06:30:00Z",
        # Charter 247: root bump at the instant P0 v25 declares.
        "p1_joint_likelihood_v36": "2026-10-02T14:30:00Z",
        # Charter 248: root bump at the instant P0 v26 declares.
        "p1_joint_likelihood_v37": "2026-10-02T17:30:00Z",
        # Charter 253: root bump at the instant P0 v27 declares.
        "p1_joint_likelihood_v38": "2026-10-04T15:45:00Z",
        # Charter 261: root bump at the instant P0 v28 declares.
        "p1_joint_likelihood_v39": "2026-10-09T06:20:00Z",
        # Charter 271: root bump at the instant P0 v29 declares.
        "p1_joint_likelihood_v40": "2026-10-15T17:10:00Z",
    },
}


def version_effective_from(stage: str, version: str) -> str | None:
    """When ``version``'s arithmetic began, if the bump said so."""

    declared = STAGE_VERSION_EFFECTIVE_FROM.get(str(stage)) or {}
    value = declared.get(str(version))
    return str(value) if value else None


# Charter 202 (user ruling 2026-09-23, 「证据时钟不要清零，用旧的样本」): a
# version bump no longer restarts the calibration lanes' evidence.  Each bump
# above stays the version's own start -- the watchdog, the fingerprint stamp
# and the board's version comparison read that -- but the lanes (width, band,
# band shadow, off-centre claim, effective-edge lambda, beta, valuation) and
# the scorecard read their evidence from the stage's epoch below, across
# every bump declared after it.
#
# Why the reset had to go: P0 served v6, v7, v8 and v9 between 2026-09-18
# and 09-22 and none of them lived the eight settled days the width and band
# lanes need, so neither refitted and the band layer served nothing
# (NO_BAND_IN_SUPPORT) while P0's Reliability sat 87% in the claims above
# one half -- said 64%, happened 42%.
#
# Why the epochs are where they are: each is the start of the arithmetic the
# stage still serves in kind.  P0 from charter 134's stable pool (v5): the
# membership rule changed there, and before it lie the contest seats and the
# retired station width table v1 (09-06..09-10), whose rows read +0.57 nat
# against identity and would teach the width lane to widen back what that
# table narrowed.  P0-M and P1 from charter 150 (P0-M v7, P1 v12): each
# became one likelihood update on its root there; the operators before it are
# retired.  Moving an epoch is a ruling, like a bump; nothing moves it by
# itself, and a bump that should restart evidence says so here.
STAGE_EVIDENCE_EPOCH: Mapping[str, str] = {
    "P0": "2026-09-14T05:52:13Z",
    "P0-M": "2026-09-18T19:10:09Z",
    "P1": "2026-09-18T19:10:09.000001Z",
}


def evidence_effective_from(stage: str, version: str) -> str | None:
    """Where the lanes' evidence for ``version`` begins (charter 202).

    The stage's evidence epoch when ``version`` began at or after it;
    otherwise the version's own start, so a version older than the epoch
    keeps exactly the bound it always had.  None when the version declares
    no start, which leaves the caller to infer one as before.
    """

    declared = version_effective_from(stage, version)
    epoch = STAGE_EVIDENCE_EPOCH.get(str(stage))
    if not declared or not epoch:
        return declared

    def _at(value: str) -> datetime:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    # Compared as instants: the P1 epoch carries microseconds and a string
    # comparison would order "...09Z" after "...09.000001Z".
    return epoch if _at(epoch) <= _at(declared) else declared


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
