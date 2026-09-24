"""A keyword the callee does not accept is a fleet outage, not a typo.

``compile_production_plan()`` takes a long chain of pass-through keywords,
and twice in one week a caller grew one before the callee did.  Each time
every station's compile raised ``TypeError`` and the whole fleet stopped
deciding, and each time it was found afterwards by a person reading blocked
rows:

* 2026-09-03T02Z, ``last_acted_probability_identity``, 28 scopes.
* 2026-09-09T09:03-10:40Z, ``off_centre_claim_bands``, all 45 stations on
  the live event day for an hour and thirty-eight minutes -- 65 decision
  rows in that window, every one blocked, not one prepared.

Both are the same shape and both are visible without running anything: the
call site names a keyword, the def does not have it.  This reads every
module in the package and in ``scripts/``, resolves each call back to the
function it actually reaches, and refuses the mismatch.

Resolution is deliberately narrow, because a check with false positives
gets an allowlist and then gets ignored.  A call counts only when the name
was imported from a module in this repository (``from .production import
compile_production_plan``), defined in the same module, or reached through
an imported module alias.  A bare method call on an object is not resolved
at all -- ``value.isoformat(timespec=...)`` collides with a module-level
``def isoformat`` in this very package, and that collision alone produced
673 false hits on the first attempt.  With the narrow rule the whole
repository yields three, and all three are real.
"""

from __future__ import annotations

import ast
import functools
import pathlib

import pytest

from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]

# Already broken when this check was written (2026-09-09), and left that
# way on purpose: both are orphaned PR10 one-offs -- no launchd job, no
# other script, no document references either of them -- and their calls
# have drifted from ``model_oos_selection`` far enough that fixing them is
# domain work, not a rename (the scripts pass cohort *hashes* where the
# contract now wants cohort *references*).  They are the evidence that this
# check was needed, so they are named rather than deleted, and the check
# exists to stop the next one reaching the resident.
# Empty since 2026-09-17: both orphans that used to be listed here
# (evaluate_pr10_reserved_p0.py, build_pr10_reserved_masks.py) were deleted
# with the July-August legacy tree, so the tree carries no known-broken call.
KNOWN_BROKEN_CALL_SITES: set[tuple[str, str]] = set()


def _python_files(root: pathlib.Path) -> list[pathlib.Path]:
    paths = [
        *sorted((root / "bayes_basket_target").rglob("*.py")),
        *sorted((root / "scripts").glob("*.py")),
        *sorted(root.glob("*.py")),
    ]
    return [p for p in paths if "node_modules" not in p.parts]


def _resolve_module(
    root: pathlib.Path, path: pathlib.Path, module: str, level: int
) -> str | None:
    """The file a ``from ... import`` reaches, when it is one of ours."""

    if level:
        base = path.parent
        for _ in range(level - 1):
            base = base.parent
        target = base / (module.replace(".", "/") + ".py") if module else (
            base / "__init__.py"
        )
        return str(target.relative_to(root)) if target.exists() else None
    if module and module.startswith("bayes_basket_target"):
        target = root / (module.replace(".", "/") + ".py")
        return str(target.relative_to(root)) if target.exists() else None
    return None


def _signatures(trees):
    """Module-level defs only: a nested def is not what a caller imports."""

    out = {}
    for rel, tree in trees.items():
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                accepted = (
                    {a.arg for a in args.args}
                    | {a.arg for a in args.posonlyargs}
                    | {a.arg for a in args.kwonlyargs}
                )
                out[(rel, node.name)] = (accepted, args.kwarg is not None)
    return out


def _scan(root: pathlib.Path) -> list[dict[str, Any]]:
    """Every resolvable call that hands a keyword its callee cannot take."""

    trees = {}
    for path in _python_files(root):
        try:
            trees[str(path.relative_to(root))] = ast.parse(
                path.read_text(encoding="utf-8")
            )
        except (SyntaxError, UnicodeDecodeError):
            # A module mid-edit by another session is not this check's
            # business; the suite that imports it will say so.
            continue
    signatures = _signatures(trees)
    # Indexed once.  Scanning every signature per file is 1,554 x 11,878
    # comparisons and was most of this check's runtime.
    local_names: dict[str, set[str]] = {}
    for file, name in signatures:
        local_names.setdefault(file, set()).add(name)
    found = []
    for rel, tree in trees.items():
        path = root / rel
        by_name: dict[str, tuple[str, str]] = {}
        by_alias: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                target = _resolve_module(
                    root, path, node.module or "", node.level or 0
                )
                for alias in node.names:
                    if target:
                        by_name[alias.asname or alias.name] = (target, alias.name)
                    if node.level and not node.module:
                        module = _resolve_module(
                            root, path, alias.name, node.level
                        )
                        if module:
                            by_alias[alias.asname or alias.name] = module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname and alias.name.startswith("bayes_basket_target"):
                        module = root / (alias.name.replace(".", "/") + ".py")
                        if module.exists():
                            by_alias[alias.asname] = str(
                                module.relative_to(root)
                            )
        local = local_names.get(rel, frozenset())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            key = None
            func = node.func
            if isinstance(func, ast.Name):
                if func.id in by_name:
                    key = by_name[func.id]
                elif func.id in local:
                    key = (rel, func.id)
            elif isinstance(func, ast.Attribute) and isinstance(
                func.value, ast.Name
            ):
                if func.value.id in by_alias:
                    key = (by_alias[func.value.id], func.attr)
            if key is None or key not in signatures:
                continue
            accepted, takes_kwargs = signatures[key]
            if takes_kwargs:
                continue
            for keyword in node.keywords:
                if keyword.arg and keyword.arg not in accepted:
                    found.append({
                        "caller": rel,
                        "line": node.lineno,
                        "callee": key[1],
                        "callee_module": key[0],
                        "keyword": keyword.arg,
                    })
    return found


@functools.lru_cache(maxsize=1)
def _mismatches() -> tuple[tuple[tuple[str, Any], ...], ...]:
    # Scanned once for the whole module: three tests ask for it and the
    # parse is most of this check's runtime.
    return tuple(
        tuple(sorted(row.items())) for row in _scan(ROOT)
    )


def _rows() -> list[dict[str, Any]]:
    return [dict(row) for row in _mismatches()]


@functools.lru_cache(maxsize=1)
def _referrer_corpus() -> dict[str, str]:
    """Everything that could plausibly invoke a script, read once."""

    corpus = {}
    for path in [
        *sorted(ROOT.glob("launchd/*.plist")),
        *sorted((ROOT / "scripts").glob("*.sh")),
        *sorted((ROOT / "scripts").glob("*.py")),
        *sorted((ROOT / "bayes_basket_target").rglob("*.py")),
    ]:
        if "node_modules" in path.parts:
            continue
        try:
            corpus[str(path.relative_to(ROOT))] = path.read_text(
                encoding="utf-8"
            )
        except (OSError, UnicodeDecodeError):
            continue
    return corpus


def test_no_call_passes_a_keyword_its_callee_cannot_accept() -> None:
    unexpected = [
        row for row in _rows()
        if (row["caller"], row["callee"]) not in KNOWN_BROKEN_CALL_SITES
    ]

    assert not unexpected, "\n".join(
        "{caller}:{line} passes {keyword!r} to {callee}() "
        "in {callee_module}, which does not accept it".format(**row)
        for row in unexpected
    )


def test_the_two_episodes_this_check_was_written_for_would_be_caught() -> None:
    """The check has to fail on the real shape, not just pass on today's tree.

    Both outages were a keyword on a resolvable call, so the detector is
    exercised against exactly that: a module in the package importing a
    function from another and handing it a name the def does not carry.
    """

    from bayes_basket_target import production, production_io, p0_preselection

    production_source = pathlib.Path(production.__file__).read_text(
        encoding="utf-8"
    )
    accepted = {
        arg.arg
        for node in ast.parse(production_source).body
        if isinstance(node, ast.FunctionDef)
        and node.name == "compile_production_plan"
        for arg in [*node.args.args, *node.args.kwonlyargs]
    }
    # The two keywords that took the fleet are accepted now.  If either is
    # ever removed while a caller still passes it, the check above fires.
    assert "off_centre_claim_bands" in accepted
    assert "last_acted_probability_identity" in accepted

    # And the callers really do pass them, so the pairing is live rather
    # than a pair of names nobody uses.  Since 2026-09-24 both entries pass
    # the chain's history as one splat of ``chain_compile_kwargs`` -- which
    # the static detector above cannot see into, so its keys are checked
    # against the def here, by calling it.
    for module in (production_io, p0_preselection):
        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        assert "**chain_compile_kwargs(" in source

    class _Chain:
        tables_as_of_utc = None

        def chain_tilt(self, **_):
            return None

        def last_acted_probability_identity(self, **_):
            return None

        def last_decision_signature(self, **_):
            return None

    import sqlite3

    splat = production_io.chain_compile_kwargs(
        sqlite3.connect(":memory:"), _Chain(),
        station_id="ZUUU", event_date="2026-09-18", stage="P0",
    )
    assert {"off_centre_claim_bands", "last_acted_probability_identity"} <= set(splat)
    assert set(splat) <= accepted, sorted(set(splat) - accepted)


def test_the_known_broken_pair_is_still_exactly_two_and_still_broken() -> None:
    """An allowlist that never shrinks is a rug, so it is pinned.

    If someone fixes one of these, this fails and the entry comes out.  If
    a third joins them, the check above fails instead.
    """

    broken = {
        (row["caller"], row["callee"]) for row in _rows()
    }

    assert broken == KNOWN_BROKEN_CALL_SITES


@pytest.mark.parametrize("relative", sorted(
    caller for caller, _callee in KNOWN_BROKEN_CALL_SITES
))
def test_the_allowlisted_scripts_are_still_orphans(relative: str) -> None:
    """They are excused only because nothing runs them.

    The moment one is wired into a launchd job, a shell script or another
    module, "already broken and unreferenced" stops being true and it has
    to be fixed rather than listed.
    """

    name = pathlib.Path(relative).name
    referrers = [
        rel for rel, text in _referrer_corpus().items()
        if pathlib.Path(rel).name != name and name in text
    ]

    assert not referrers, f"{relative} is referenced by {referrers}"


# ------------------------------------------------- the detector, on a fixture


def _write(root: pathlib.Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


CALLEE = '''
def compile_production_plan(*, station_id, market_snapshot):
    return station_id, market_snapshot
'''


def test_the_detector_catches_the_shape_that_took_the_fleet(tmp_path) -> None:
    """A passing check proves nothing unless it fails on the real bug.

    This is 2026-09-09 in miniature: the caller grew a keyword, the callee
    did not, and nothing but a run would have said so.
    """

    _write(tmp_path, "bayes_basket_target/production.py", CALLEE)
    _write(tmp_path, "bayes_basket_target/production_io.py", '''
from .production import compile_production_plan


def compile_production_cycle_from_ssot(conn):
    return compile_production_plan(
        station_id="ZUUU",
        market_snapshot={},
        off_centre_claim_bands={},
    )
''')

    found = _scan(tmp_path)

    assert [
        (row["caller"], row["callee"], row["keyword"]) for row in found
    ] == [(
        "bayes_basket_target/production_io.py",
        "compile_production_plan",
        "off_centre_claim_bands",
    )]


def test_the_detector_is_quiet_once_the_callee_accepts_it(tmp_path) -> None:
    _write(tmp_path, "bayes_basket_target/production.py", '''
def compile_production_plan(*, station_id, market_snapshot,
                            off_centre_claim_bands=None):
    return station_id, market_snapshot, off_centre_claim_bands
''')
    _write(tmp_path, "bayes_basket_target/production_io.py", '''
from .production import compile_production_plan


def go():
    return compile_production_plan(
        station_id="ZUUU", market_snapshot={}, off_centre_claim_bands={},
    )
''')

    assert _scan(tmp_path) == []


def test_a_callee_that_takes_kwargs_is_never_a_mismatch(tmp_path) -> None:
    _write(tmp_path, "bayes_basket_target/production.py", '''
def compile_production_plan(*, station_id, **rest):
    return station_id, rest
''')
    _write(tmp_path, "bayes_basket_target/production_io.py", '''
from .production import compile_production_plan


def go():
    return compile_production_plan(station_id="ZUUU", anything_at_all=1)
''')

    assert _scan(tmp_path) == []


def test_a_method_call_on_a_value_is_not_resolved(tmp_path) -> None:
    """The rule that keeps this check usable.

    A module-level ``def isoformat`` in the package collides with the
    datetime method of that name, and resolving attribute calls by name
    alone produced 673 false hits -- an allowlist that size is a check
    nobody reads.
    """

    _write(tmp_path, "bayes_basket_target/execution_deadline.py", '''
def isoformat(value):
    return value
''')
    _write(tmp_path, "bayes_basket_target/other.py", '''
from datetime import datetime

from .execution_deadline import isoformat


def go(moment: datetime):
    return moment.isoformat(timespec="seconds"), isoformat(moment)
''')

    assert _scan(tmp_path) == []


def test_a_module_alias_call_is_resolved(tmp_path) -> None:
    _write(tmp_path, "bayes_basket_target/production.py", CALLEE)
    _write(tmp_path, "bayes_basket_target/caller.py", '''
from . import production


def go():
    return production.compile_production_plan(
        station_id="ZUUU", market_snapshot={}, nope=1,
    )
''')

    assert [row["keyword"] for row in _scan(tmp_path)] == ["nope"]
