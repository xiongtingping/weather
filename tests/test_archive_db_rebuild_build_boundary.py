import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "archive_db_rebuild_build.py"
spec = importlib.util.spec_from_file_location("archive_db_rebuild_build", SCRIPT_PATH)
archive_build = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(archive_build)


def test_archive_rebuild_candidate_copy_boundary_is_not_live_fact_writer(tmp_path: Path) -> None:
    source = tmp_path / "archive.db"
    output = tmp_path / "candidate.db"

    boundary = archive_build.build_archive_rebuild_candidate_copy_boundary(
        source_db=source,
        output_db=output,
    )

    assert boundary["schema"] == "archive_db_rebuild_candidate_copy_boundary_v1"
    assert boundary["execution_mode"] == "candidate_db_table_copy_not_fact_snapshot_mutation"
    assert boundary["source_db_mode"] == "attach_read_only"
    assert boundary["output_scope"] == "candidate_archive_db_file"
    assert boundary["guards"]["writes_archive_db"] is False
    assert boundary["guards"]["writes_candidate_archive_db"] is True
    assert boundary["guards"]["replaces_source_db"] is False
    assert boundary["guards"]["places_orders"] is False
    assert boundary["guards"]["historical_backfill"] is False
    assert boundary["fact_writer_integration"]["status"] == "not_applicable_for_candidate_copy"
