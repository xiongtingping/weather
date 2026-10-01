"""The calibration lanes read frozen days' envelope payloads from a cache (「现在做」).

Verified 2026-10-01 on the live store: the width lane's P0 sample (12,849
cells) and the off-centre lane's three stages identical uncached, while
filling, and from the cache.
"""

from __future__ import annotations

import ast
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bayes_basket_target import serving_payload_cache as P
from bayes_basket_target import ssot
from bayes_basket_target.contracts import ContractError, content_sha256
from tests.test_taf_likelihood_review_20260919 import _ddl

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)   # frozen through 09-28


@pytest.fixture(autouse=True)
def _fresh_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(P, "_CONNECTIONS", {})


def _store(tmp_path: Path, day: str) -> tuple[sqlite3.Connection, str]:
    conn = sqlite3.connect(tmp_path / "ssot.db")
    conn.execute(_ddl(ssot.PRODUCTION_TARGET_DDL, "basket_probability_envelope"))
    lineage = {key: f"{key}-value" for key in P.PAYLOAD_LINEAGE_KEYS}
    lineage["unread"] = "x" * 300
    payload = {
        "schema": "bayes_basket_probability_envelope_v1", "stage": "P0", "checkpoint_id": "cp-1",
        "station_id": "KLGA", "event_date": day, "settlement_timezone": "America/New_York",
        "decision_at_utc": f"{day}T01:00:00Z", "fresh_until_utc": f"{day}T02:00:00Z",
        "bucket_ordinals": {"20": 0}, "probabilities": {"20": 1.0}, "posterior_version": "v",
        "posterior_sha256": "a" * 64, "bridge_sha256": "b" * 64, "lineage": lineage,
    }
    sha = content_sha256(payload)
    conn.execute(
        "INSERT INTO basket_probability_envelope(envelope_id,stage,checkpoint_id,station_id,event_date,"
        "settlement_timezone,decision_at_utc,fresh_until_utc,bucket_ordinals_json,probabilities_json,"
        "posterior_version,posterior_sha256,bridge_sha256,lineage_json,content_sha256,state,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("cp-1", "P0", "cp-1", "KLGA", day, "America/New_York", f"{day}T01:00:00Z", f"{day}T02:00:00Z",
         json.dumps({"20": 0}), json.dumps({"20": 1.0}), "v", "a" * 64, "b" * 64, json.dumps(lineage),
         sha, "COMMITTED", f"{day}T01:00:00Z"),
    )
    conn.commit()
    return conn, sha


def test_a_frozen_payload_survives_its_envelope_leaving_the_ssot(tmp_path: Path) -> None:
    conn, sha = _store(tmp_path, "2026-09-20")
    cache = tmp_path / "cache.db"
    first = P.cached_history_envelope_payload(
        conn, checkpoint_id="cp-1", envelope_content_sha256=sha, cache_path=cache, now=NOW,
    )
    conn.execute("DELETE FROM basket_probability_envelope")
    conn.commit()
    again = P.cached_history_envelope_payload(
        conn, checkpoint_id="cp-1", envelope_content_sha256=sha, cache_path=cache, now=NOW,
    )
    assert again["probabilities"] == first["probabilities"]
    assert again["bucket_ordinals"] == first["bucket_ordinals"]
    assert again["lineage"] == {k: first["lineage"][k] for k in P.PAYLOAD_LINEAGE_KEYS}
    assert "unread" not in again["lineage"]


def test_an_unfrozen_day_is_not_kept(tmp_path: Path) -> None:
    conn, sha = _store(tmp_path, "2026-09-30")
    cache = tmp_path / "cache.db"
    P.cached_history_envelope_payload(
        conn, checkpoint_id="cp-1", envelope_content_sha256=sha, cache_path=cache, now=NOW,
    )
    conn.execute("DELETE FROM basket_probability_envelope")
    conn.commit()
    with pytest.raises(ContractError, match="body_missing"):
        P.cached_history_envelope_payload(
            conn, checkpoint_id="cp-1", envelope_content_sha256=sha, cache_path=cache, now=NOW,
        )


def test_a_payload_whose_hash_fails_is_never_kept(tmp_path: Path) -> None:
    conn, _sha = _store(tmp_path, "2026-09-20")
    cache = tmp_path / "cache.db"
    with pytest.raises(ContractError, match="sha_mismatch"):
        P.cached_history_envelope_payload(
            conn, checkpoint_id="cp-1", envelope_content_sha256="f" * 64, cache_path=cache, now=NOW,
        )
    assert sqlite3.connect(cache).execute("SELECT COUNT(*) FROM payload").fetchone()[0] == 0


def test_a_pass_covers_the_lane_only_from_no_later_than_its_anchor(tmp_path: Path) -> None:
    cache = tmp_path / "cache.db"
    P.record_pass("lane", stage="P0", pre_band=True, min_decision_at_utc="2026-09-14T05:52:13Z",
                  cache_path=cache, now=NOW)
    assert P.covered_through("lane", stage="P0", anchor_utc="2026-09-14T05:52:13Z",
                             cache_path=cache, now=NOW) == "2026-09-28"
    # A pass that started later than the lane's anchor read a subset.
    assert P.covered_through("lane", stage="P0", anchor_utc="2026-09-10T00:00:00Z",
                             cache_path=cache, now=NOW) is None
    # A stale pass does not count.
    later = datetime(2026, 10, 9, tzinfo=timezone.utc)
    assert P.covered_through("lane", stage="P0", anchor_utc="2026-09-14T05:52:13Z",
                             cache_path=cache, now=later) is None


def test_a_fixture_store_never_writes_the_production_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn, sha = _store(tmp_path, "2026-09-20")
    production = tmp_path / "production_cache.db"
    monkeypatch.setattr(P, "CACHE_PATH", production)
    P.cached_history_envelope_payload(conn, checkpoint_id="cp-1", envelope_content_sha256=sha, now=NOW)
    P.record_pass("lane", stage="P0", pre_band=True, min_decision_at_utc=None, conn=conn, now=NOW)
    assert not production.exists()


def _reads(path: str, names: set[str]) -> tuple[set[str], set[str], set[str]]:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    lineage_keys: set[str] = set()
    envelope_keys: set[str] = set()
    passed_to: set[str] = set()
    for fn in tree.body:
        if not (isinstance(fn, ast.FunctionDef) and fn.name in names):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" \
                    and isinstance(node.func.value, ast.Name) and node.args and isinstance(node.args[0], ast.Constant):
                if node.func.value.id == "lineage":
                    lineage_keys.add(node.args[0].value)
                elif node.func.value.id == "envelope":
                    envelope_keys.add(node.args[0].value)
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id in {"lineage", "envelope"}:
                passed_to.add("subscript")
            if isinstance(node, ast.Call) and any(isinstance(a, ast.Name) and a.id in {"lineage", "envelope"} for a in node.args):
                passed_to.add(ast.unparse(node.func))
    return lineage_keys, envelope_keys, passed_to


def test_the_lanes_read_nothing_off_a_payload_the_cache_drops() -> None:
    """A key read off a cached payload that the cache did not keep reads as None, silently.

    It happened while this was built: ``system_weights_json`` (the seated
    system count) had joined the width lane's reads, and the first parity run
    differed on 11,188 cells until it was kept.
    """

    width = _reads("bayes_basket_target/center_width_store.py",
                   {"_serving_cell", "_receipt_of", "_pre_band_claim"})
    offcentre = _reads("bayes_basket_target/band_reliability_store.py", {"serving_lane_by_station_day"})
    for lineage_keys, envelope_keys, passed_to in (width, offcentre):
        assert lineage_keys <= set(P.PAYLOAD_LINEAGE_KEYS)
        assert envelope_keys <= {"lineage", "probabilities", "bucket_ordinals"}
        assert passed_to <= {"_receipt_of", "_pre_band_claim"}
