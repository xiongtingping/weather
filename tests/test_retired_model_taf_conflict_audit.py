from scripts.retired_model_taf_conflict_audit import build_retired_model_taf_conflict_audit


def test_retired_model_taf_conflict_special_case_is_absent_from_new_runtime_fact_paths() -> None:
    report = build_retired_model_taf_conflict_audit()

    assert report["ok"] is True
    assert report["violations"] == []
    assert report["guards"]["writes_archive_db"] is False
