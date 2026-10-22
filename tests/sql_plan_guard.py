"""Plan every SQL statement in the code against the live schema snapshot.

Shared by ``test_sql_query_plans`` and anyone auditing by hand.  A statement
is a string constant (implicit concatenation folds into one) that starts with
SELECT or WITH and reads FROM a table; ``{...}`` holes in f-strings and
``%s`` lists become placeholders.  A finding is a plan step that SCANs a
table holding at least ``BIG_TABLE_ROWS`` rows without a covering index --
every row and its overflow pages read, the shape behind each slow render and
cycle fixed one at a time since August.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "tests" / "fixtures" / "ssot_schema_snapshot.json"
BIG_TABLE_ROWS = 50_000
SOURCES = (
    "bayes_basket_target/**/*.py",
    "scripts/calibration_dashboard.py",
    "scripts/live_book.py",
)


def snapshot_connection() -> tuple[sqlite3.Connection, dict[str, int]]:
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    conn = sqlite3.connect(":memory:")
    for table in snapshot["tables"].values():
        try:
            conn.execute(table["sql"])
        except sqlite3.Error:
            pass
    for sql in snapshot["indexes"]:
        try:
            conn.execute(sql)
        except sqlite3.Error:
            pass
    sizes = {name: int(t["rows"]) for name, t in snapshot["tables"].items()}
    return conn, sizes


def _text(node: ast.AST, names: dict[str, str] | None = None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and names and node.id in names:
        return names[node.id]
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("?")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _text(node.left, names), _text(node.right, names)
        if left is not None and right is not None:
            return left + right
    return None


def statements() -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    paths = sorted({p for pattern in SOURCES for p in ROOT.glob(pattern)})
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        # Module-level SQL fragments (``_LIVE_PLAN_READINGS_SQL``) are planned
        # where they are completed, ``NAME + " WHERE ..."``; a fragment that
        # is only ever a prefix is not a statement of its own.
        names: dict[str, str] = {}
        definitions: dict[str, int] = {}
        for node in tree.body:
            if (
                isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                text = _text(node.value)
                if text is not None:
                    names[node.targets[0].id] = text
                    definitions[node.targets[0].id] = id(node.value)
        prefixes = {
            node.left.id for node in ast.walk(tree)
            if isinstance(node, ast.BinOp) and isinstance(node.left, ast.Name)
            and node.left.id in names
        }
        seen: set[int] = {definitions[name] for name in prefixes}
        for node in ast.walk(tree):
            # A bare name is a reference, planned where it is completed.
            if id(node) in seen or isinstance(node, ast.Name):
                continue
            text = _text(node, names)
            if text is None:
                continue
            if isinstance(node, ast.BinOp):
                for child in ast.walk(node):
                    seen.add(id(child))
            if re.match(r"\s*(SELECT|WITH)\b", text, re.I) and re.search(r"\bFROM\b", text, re.I):
                found.append((str(path.relative_to(ROOT)), node.lineno, text))
    return found


def statement_key(path: str, sql: str) -> str:
    """Stable across line moves: the file and the statement's normalised text."""

    normal = re.sub(r"\s+", " ", sql).strip()
    return f"{path}:{hashlib.sha256(normal.encode()).hexdigest()[:12]}"


def full_scans(conn: sqlite3.Connection, sizes: dict[str, int]) -> list[dict]:
    out = []
    for path, line, sql in statements():
        # ``LIMIT 0`` reads no row: it asks for the column names.
        if re.search(r"\bLIMIT\s+0\s*$", sql.strip(), re.I):
            continue
        query = re.sub(r"%s", "?", re.sub(r"\{[^}]*\}", "?", sql))
        try:
            plan = conn.execute("EXPLAIN QUERY PLAN " + query, [None] * query.count("?")).fetchall()
        except (sqlite3.Error, ValueError):
            continue
        for row in plan:
            detail = str(row[-1])
            match = re.match(r"SCAN (\w+)", detail)
            if not match or "COVERING INDEX" in detail:
                continue
            table = match.group(1)
            if sizes.get(table, 0) >= BIG_TABLE_ROWS:
                out.append({
                    "key": statement_key(path, sql), "path": path, "line": line,
                    "table": table, "rows": sizes[table], "detail": detail,
                })
                break
    return out
