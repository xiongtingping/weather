from __future__ import annotations

from scripts.task_queue_worker import _compact_task_failure_detail


def test_task_worker_preserves_materializer_json_blocker_when_stderr_empty() -> None:
    detail = _compact_task_failure_detail(
        '{"ok": false, "blocked": true, "block_reason": "official_nwp_context_policy_missing"}',
        "",
    )

    assert detail == "task_result:official_nwp_context_policy_missing"


def test_task_worker_prefers_stderr_when_subprocess_has_one() -> None:
    assert _compact_task_failure_detail('{"block_reason":"ignored"}', "Traceback: test") == "Traceback: test"
