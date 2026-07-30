import ast
from pathlib import Path

from scripts.install_v6_market_transaction_branch_runtime import (
    transform_drain,
    transform_ingress,
)


def test_installer_is_idempotent_for_evolved_live_runtime() -> None:
    root = Path(__file__).resolve().parents[1]
    drain = (root / "production_probability_v6_canonical_event_drain_runtime.py").read_text()
    ingress = (root / "production_probability_v6_amsc_archive_ingress.py").read_text()
    assert transform_drain(drain) == drain
    assert transform_ingress(ingress) == ingress


ROOT = Path(__file__).resolve().parents[1]


def test_market_branch_transform_is_idempotent_and_keeps_inputs_outside_writer_lock():
    drain_source = (ROOT / "production_probability_v6_canonical_event_drain_runtime.py").read_text()
    ingress_source = (ROOT / "production_probability_v6_amsc_archive_ingress.py").read_text()
    drain = transform_drain(drain_source)
    ingress = transform_ingress(ingress_source)

    assert transform_drain(drain) == drain
    assert transform_ingress(ingress) == ingress
    ast.parse(drain)
    ast.parse(ingress)
    assert "commit_v6_candidate_transaction" in drain
    assert "commit_v6_public_physical_transaction" not in drain
    assert "market_inputs: Mapping[str, Any] | None = None" in drain
    prewarm = ingress.index("market_inputs = prewarm_v6_market_inputs")
    writer_lock = ingress.index("with archive_db_write_gate")
    assert prewarm < writer_lock
    assert "market_inputs=market_inputs" in ingress
    assert '"places_orders": True' not in drain


def test_market_branch_preview_does_not_modify_active_runtime():
    drain_path = ROOT / "production_probability_v6_canonical_event_drain_runtime.py"
    ingress_path = ROOT / "production_probability_v6_amsc_archive_ingress.py"
    drain_before = drain_path.read_bytes()
    ingress_before = ingress_path.read_bytes()

    transform_drain(drain_before.decode())
    transform_ingress(ingress_before.decode())

    assert drain_path.read_bytes() == drain_before
    assert ingress_path.read_bytes() == ingress_before
