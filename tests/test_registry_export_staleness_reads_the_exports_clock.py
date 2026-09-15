"""The rotation re-exports the registry when its snapshot is old, not every call.

2026-09-15: ``_registry_export_is_stale`` read a top-level ``as_of_utc`` the
export never writes -- its clock is ``filters.as_of_utc``, beside
``exported_at_utc`` -- so it answered "stale" on every call and every rotation
call re-exported the registry from the live database: 152s of a 239s poll,
2.45s a call, while production waited behind the forecast turn.  An export
whose scope has not moved writes nothing, so the file's clock alone would go
stale for good thirty minutes in; the attempt is remembered too.

Built on a real export, so the check reads the shape the export writes.
"""

from __future__ import annotations

from pathlib import Path

import bayes_basket_target.production_worker as pw
from bayes_basket_target.market_registry import export_shared_live_registry
from tests.test_bayes_basket_target_market_registry import _source_db


def _exported(tmp_path: Path, as_of: str) -> Path:
    source = tmp_path / "archive.db"
    snapshot = tmp_path / "registry.json"
    _source_db(source)
    result = export_shared_live_registry(
        source_db=source,
        output_path=snapshot,
        as_of_utc=as_of,
        include_not_started_for_p0=True,
    )
    assert result["state"] == "EXPORTED"
    return snapshot


def _worker() -> pw.TargetProductionWorker:
    return object.__new__(pw.TargetProductionWorker)


def test_a_snapshot_inside_its_age_is_not_stale(tmp_path) -> None:
    snapshot = _exported(tmp_path, "2026-07-23T12:00:00Z")
    assert _worker()._registry_export_is_stale(
        snapshot, as_of_utc="2026-07-23T12:10:00Z"
    ) is False


def test_a_snapshot_past_its_age_is_stale(tmp_path) -> None:
    snapshot = _exported(tmp_path, "2026-07-23T12:00:00Z")
    assert _worker()._registry_export_is_stale(
        snapshot, as_of_utc="2026-07-23T12:31:00Z"
    ) is True


def test_an_export_that_found_nothing_new_still_counts_as_asked(tmp_path) -> None:
    snapshot = _exported(tmp_path, "2026-07-23T12:00:00Z")
    worker = _worker()
    # Forty minutes on, the scope has not moved: the export writes nothing and
    # the file keeps its 12:00 clock.
    again = export_shared_live_registry(
        source_db=tmp_path / "archive.db",
        output_path=snapshot,
        as_of_utc="2026-07-23T12:40:00Z",
        include_not_started_for_p0=True,
    )
    assert again["state"] == "UNCHANGED_NO_WRITE"
    worker._registry_export_attempted_at_utc = "2026-07-23T12:40:00Z"

    assert worker._registry_export_is_stale(
        snapshot, as_of_utc="2026-07-23T12:45:00Z"
    ) is False
    assert worker._registry_export_is_stale(
        snapshot, as_of_utc="2026-07-23T13:11:00Z"
    ) is True


def test_an_unreadable_snapshot_is_stale(tmp_path) -> None:
    broken = tmp_path / "registry.json"
    broken.write_text("{}", encoding="utf-8")
    assert _worker()._registry_export_is_stale(
        broken, as_of_utc="2026-07-23T12:00:00Z"
    ) is True


def test_the_rotation_remembers_every_export_it_asks_for() -> None:
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(pw.TargetProductionWorker.refresh_resident_nwp)
    ))
    export_branches = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and any(
            isinstance(call, ast.Call)
            and getattr(call.func, "id", "") == "export_shared_live_registry"
            for stmt in node.body for call in ast.walk(stmt)
        )
    ]
    assert export_branches
    for branch in export_branches:
        assert any(
            isinstance(stmt, ast.Assign)
            and ast.unparse(stmt.targets[0])
            == "self._registry_export_attempted_at_utc"
            for stmt in branch.body
        )
