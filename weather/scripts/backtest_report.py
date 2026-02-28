#!/usr/bin/env python3
import argparse
import csv
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple


def read_rows(path: Path) -> List[dict]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def market_key(row: dict) -> str:
    slug = (row.get("slug") or "").strip()
    title = (row.get("title") or "").strip()
    event_date = (row.get("event_date") or "").strip()
    if slug:
        return slug
    if title:
        return f"{title}|{event_date}"
    return event_date or "unknown"


def agg_stats(rows: List[dict], keys: Tuple[str, ...]) -> Dict[Tuple, dict]:
    stats: Dict[Tuple, dict] = {}
    for row in rows:
        key = tuple((row.get(k) or "").strip() for k in keys)
        entry = stats.setdefault(key, {"count": 0, "hit": 0})
        entry["count"] += 1
        try:
            hit = int(float(row.get("hit") or 0))
        except Exception:
            hit = 0
        entry["hit"] += hit
    return stats


def write_csv(path: Path, headers: List[str], rows: List[List]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)


def build_overall(rows: List[dict]) -> Dict[str, dict]:
    overall = agg_stats(rows, ("system",))
    out: Dict[str, dict] = {}
    for (system,), entry in overall.items():
        count = entry["count"]
        hit = entry["hit"]
        rate = (hit / count) if count else 0.0
        out[system] = {"count": count, "hit": hit, "rate": rate}
    return out


def build_market_rows(rows: List[dict]) -> List[List]:
    enriched = []
    for row in rows:
        row = dict(row)
        row["market_key"] = market_key(row)
        enriched.append(row)
    stats = agg_stats(enriched, ("system", "market_key", "event_date"))
    out = []
    for (system, mkey, event_date), entry in sorted(stats.items()):
        count = entry["count"]
        hit = entry["hit"]
        rate = (hit / count) if count else 0.0
        out.append([system, mkey, event_date, count, hit, f"{rate:.1%}"])
    return out


def build_date_rows(rows: List[dict], date_field: str) -> List[List]:
    stats = agg_stats(rows, ("system", date_field))
    out = []
    for (system, date_value), entry in sorted(stats.items()):
        count = entry["count"]
        hit = entry["hit"]
        rate = (hit / count) if count else 0.0
        out.append([system, date_value, count, hit, f"{rate:.1%}"])
    return out


def build_html(
    overall: Dict[str, dict],
    market_rows: List[List],
    run_date_rows: List[List],
    event_date_rows: List[List],
) -> str:
    def row_html(row: List[str]) -> str:
        cells = "".join(f"<td>{val}</td>" for val in row)
        return f"<tr>{cells}</tr>"

    overall_rows = []
    for system, entry in sorted(overall.items()):
        overall_rows.append(
            [
                system,
                entry["count"],
                entry["hit"],
                f"{entry['rate']:.1%}",
            ]
        )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Backtest Report</title>
  <style>
    body {{
      font-family: Menlo, Consolas, monospace;
      margin: 24px;
      color: #111;
      background: #f8f7f3;
    }}
    h1, h2 {{
      margin: 0 0 12px;
    }}
    .section {{
      margin: 24px 0 36px;
    }}
    table {{
      border-collapse: collapse;
      width: 100%;
      background: #fff;
    }}
    th, td {{
      border: 1px solid #ddd;
      padding: 6px 10px;
      text-align: left;
      font-size: 13px;
    }}
    th {{
      background: #f0efe9;
    }}
  </style>
</head>
<body>
  <h1>Backtest Report</h1>

  <div class="section">
    <h2>Overall</h2>
    <table>
      <thead>
        <tr><th>System</th><th>Count</th><th>Hit</th><th>Hit Rate</th></tr>
      </thead>
      <tbody>
        {"".join(row_html(r) for r in overall_rows)}
      </tbody>
    </table>
  </div>

  <div class="section">
    <h2>By Market</h2>
    <table>
      <thead>
        <tr><th>System</th><th>Market</th><th>Event Date</th><th>Count</th><th>Hit</th><th>Hit Rate</th></tr>
      </thead>
      <tbody>
        {"".join(row_html(r) for r in market_rows)}
      </tbody>
    </table>
  </div>

  <div class="section">
    <h2>By Run Date</h2>
    <table>
      <thead>
        <tr><th>System</th><th>Run Date</th><th>Count</th><th>Hit</th><th>Hit Rate</th></tr>
      </thead>
      <tbody>
        {"".join(row_html(r) for r in run_date_rows)}
      </tbody>
    </table>
  </div>

  <div class="section">
    <h2>By Event Date</h2>
    <table>
      <thead>
        <tr><th>System</th><th>Event Date</th><th>Count</th><th>Hit</th><th>Hit Rate</th></tr>
      </thead>
      <tbody>
        {"".join(row_html(r) for r in event_date_rows)}
      </tbody>
    </table>
  </div>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=str, default="data/backtest_trades.csv")
    ap.add_argument("--out-dir", type=str, default="data")
    args = ap.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        raise SystemExit(f"Missing input CSV: {input_path}")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = read_rows(input_path)
    overall = build_overall(rows)
    market_rows = build_market_rows(rows)
    run_date_rows = build_date_rows(rows, "run_date")
    event_date_rows = build_date_rows(rows, "event_date")

    write_csv(
        out_dir / "backtest_report_by_market.csv",
        ["system", "market", "event_date", "count", "hit", "hit_rate"],
        market_rows,
    )
    write_csv(
        out_dir / "backtest_report_by_run_date.csv",
        ["system", "run_date", "count", "hit", "hit_rate"],
        run_date_rows,
    )
    write_csv(
        out_dir / "backtest_report_by_event_date.csv",
        ["system", "event_date", "count", "hit", "hit_rate"],
        event_date_rows,
    )

    html = build_html(overall, market_rows, run_date_rows, event_date_rows)
    (out_dir / "backtest_report.html").write_text(html, encoding="utf-8")

    print("Wrote reports:")
    print(f"- {out_dir / 'backtest_report_by_market.csv'}")
    print(f"- {out_dir / 'backtest_report_by_run_date.csv'}")
    print(f"- {out_dir / 'backtest_report_by_event_date.csv'}")
    print(f"- {out_dir / 'backtest_report.html'}")


if __name__ == "__main__":
    main()
