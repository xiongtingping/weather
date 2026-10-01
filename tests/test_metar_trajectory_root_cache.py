"""P1's METAR trajectory packet reads frozen station-days from its root cache.

User ruling 2026-10-01 「做」: the packet, run by production for one due
station a cycle, read every P0/P0-M the station ever had -- which kept every
envelope in the SSOT.  A station-day ROOT_FREEZE_LAG_DAYS past is copied
once: every column, and of the lineage the keys the packet reads.  Verified
the same day on six live stations: every row equal on every column and every
read lineage key, either way.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target import ssot
from bayes_basket_target.materialization import LINEAGE_DOCUMENT_DDL
from tests.test_taf_likelihood_review_20260919 import _ddl

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_bayes_basket_target_metar_trajectory_training_packet.py"
NOW = "2026-10-01T03:00:00Z"


def _packet():
    spec = importlib.util.spec_from_file_location("_traj_packet_cache", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CURVE_BODY = json.dumps({"points": []})
CURVE_SHA = hashlib.sha256(CURVE_BODY.encode("utf-8")).hexdigest()


def _store(tmp_path: Path) -> Path:
    path = tmp_path / "ssot.db"
    conn = sqlite3.connect(path)
    conn.execute(_ddl(ssot.PRODUCTION_TARGET_DDL, "basket_probability_envelope"))
    # The curve is stored once and referenced (926dfa29); the packet puts it back.
    conn.execute(LINEAGE_DOCUMENT_DDL)
    conn.execute(
        "INSERT INTO basket_probability_lineage_document VALUES(?,?,?)",
        (CURVE_SHA, CURVE_BODY, "2026-09-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()
    return path


def _root(path: Path, n: int, day: str, stage: str = "P0", *, extra: dict | None = None) -> None:
    lineage = {
        "bucket_intervals_c_json": json.dumps({"20": [19.5, 20.5]}),
        "conditional_future_temperature_curve_json": "@ref/" + CURVE_SHA,
        "p0_root_checkpoint_id": f"cp-{n}",
        "unread_key": "x" * 500,
        **(extra or {}),
    }
    conn = sqlite3.connect(path)
    columns = [r[1] for r in conn.execute("PRAGMA table_info(basket_probability_envelope)")]
    values = {c: (f"{n:x}" * 64)[:64] if c.endswith("sha256") else f"{c}-{n}" for c in columns}
    values.update({
        "envelope_id": f"env-{n}", "stage": stage, "checkpoint_id": f"cp-{n}", "station_id": "KLGA",
        "event_date": day, "settlement_timezone": "America/New_York",
        "decision_at_utc": f"{day}T0{n % 10}:00:00Z", "state": "COMMITTED",
        "bucket_ordinals_json": json.dumps({"20": 0}), "probabilities_json": json.dumps({"20": 1.0}),
        "lineage_json": json.dumps(lineage),
    })
    conn.execute(
        f"INSERT INTO basket_probability_envelope({','.join(values)}) VALUES({','.join('?' * len(values))})",
        tuple(values.values()),
    )
    conn.commit()
    conn.close()


def _seen(module, rows):
    out = []
    for row in rows:
        lineage = json.loads(str(row["lineage_json"] or "{}"))
        out.append(
            tuple(row[c] for c in module.ROOT_COLUMNS if c != "lineage_json")
            + tuple(lineage.get(k) for k in module.CACHED_LINEAGE_KEYS)
        )
    return out


def test_frozen_days_read_the_same_from_the_cache(tmp_path: Path) -> None:
    packet = _packet()
    db = _store(tmp_path)
    for n, day in enumerate(["2026-09-20", "2026-09-20", "2026-09-21", "2026-09-30"]):
        _root(db, n, day, stage="P0-M" if n == 1 else "P0")
    cache = tmp_path / "roots.db"
    plain = packet._read_probability_roots(db, station_id="KLGA")
    first = packet._read_probability_roots(db, station_id="KLGA", cache_path=cache, now_utc=NOW)
    again = packet._read_probability_roots(db, station_id="KLGA", cache_path=cache, now_utc=NOW)
    assert _seen(packet, plain) == _seen(packet, first) == _seen(packet, again)
    frozen = {r[0] for r in sqlite3.connect(cache).execute("SELECT event_date FROM frozen_day")}
    assert frozen == {"2026-09-20", "2026-09-21"}           # 09-30 is inside the lag


def test_a_frozen_day_survives_its_envelopes_leaving_the_ssot(tmp_path: Path) -> None:
    packet = _packet()
    db = _store(tmp_path)
    _root(db, 0, "2026-09-20")
    cache = tmp_path / "roots.db"
    before = packet._read_probability_roots(db, station_id="KLGA", cache_path=cache, now_utc=NOW)
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM basket_probability_envelope")
    conn.commit()
    conn.close()
    after = packet._read_probability_roots(db, station_id="KLGA", cache_path=cache, now_utc=NOW)
    assert _seen(packet, after) == _seen(packet, before) and len(after) == 1


def test_a_day_the_ssot_added_to_is_frozen_again(tmp_path: Path) -> None:
    packet = _packet()
    db = _store(tmp_path)
    _root(db, 0, "2026-09-20")
    cache = tmp_path / "roots.db"
    packet._read_probability_roots(db, station_id="KLGA", cache_path=cache, now_utc=NOW)
    _root(db, 1, "2026-09-20")
    rows = packet._read_probability_roots(db, station_id="KLGA", cache_path=cache, now_utc=NOW)
    assert len(rows) == 2
    count = sqlite3.connect(cache).execute("SELECT row_count FROM frozen_day").fetchone()[0]
    assert count == 2


def test_the_cache_keeps_only_the_lineage_keys_the_packet_reads(tmp_path: Path) -> None:
    packet = _packet()
    db = _store(tmp_path)
    _root(db, 0, "2026-09-20")
    cache = tmp_path / "roots.db"
    packet._read_probability_roots(db, station_id="KLGA", cache_path=cache, now_utc=NOW)
    (row,) = packet._read_probability_roots(db, station_id="KLGA", cache_path=cache, now_utc=NOW)
    assert "unread_key" not in json.loads(row["lineage_json"])
    # The cache keeps the reference; the read hands the packet the body.
    stored = sqlite3.connect(cache).execute("SELECT body FROM blob WHERE body LIKE '%@ref/%'").fetchone()
    assert stored is not None
    assert json.loads(row["lineage_json"])["conditional_future_temperature_curve_json"] == CURVE_BODY


def test_every_lineage_key_the_packet_reads_is_cached() -> None:
    """A key read off ``lineage`` that the cache drops would read as None, silently."""

    packet = _packet()
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    read = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
        and isinstance(node.func.value, ast.Name) and node.func.value.id == "lineage"
        and node.args and isinstance(node.args[0], ast.Constant)
    }
    assert read and read <= set(packet.CACHED_LINEAGE_KEYS)
    # ... and nothing reads the parsed lineage by subscript, outside the
    # projection that builds the cached copy.
    projection = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_project_lineage"
    )
    inside = {id(node) for node in ast.walk(projection)}
    subscripts = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
        and node.value.id == "lineage" and id(node) not in inside
    ]
    assert subscripts == []


def test_production_runs_the_packet_with_its_cache() -> None:
    worker = (ROOT / "bayes_basket_target" / "production_worker.py").read_text(encoding="utf-8")
    assert "build_bayes_basket_target_metar_trajectory_training_packet.py" in worker
    assert "--root-cache" not in worker   # the default path: the cache is on


@pytest.mark.parametrize("raw", ["not json", None, "[]"])
def test_an_unparseable_lineage_is_kept_whole(raw) -> None:
    packet = _packet()
    assert packet._project_lineage(raw) == raw or (raw is None and packet._project_lineage(raw) == "{}")
