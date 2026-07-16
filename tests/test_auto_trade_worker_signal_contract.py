from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_persistent_worker_forwards_launchd_shutdown_to_python_child() -> None:
    text = (ROOT / "scripts" / "auto_trade_worker.sh").read_text(encoding="utf-8")

    assert "forward_worker_shutdown" in text
    assert 'trap forward_worker_shutdown TERM INT' in text
    assert 'kill -TERM "$WORKER_CHILD_PID"' in text
    assert 'wait "$WORKER_CHILD_PID"' in text
    assert 'SHUTDOWN_REQUESTED=1' in text
