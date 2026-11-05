#!/usr/bin/env python3
"""The station board alone, as a publishable read-only site (user 2026-11-02
「将前端发布site，只保留"站点看板"板块」).

The board reads its station-days live from the local control service
(``127.0.0.1:8788/api/station-day``), which a published page cannot reach.
So this builds every station's read the way that service does
(``station_day_board.build`` plus the three fleet readings, read-only, a few
worker processes at once since a build is mostly waiting on the disk) into its
own file beside the page, and the page answers the board's own calls from them:

    index.html          the rendered dashboard (``publish_dashboard_artifact``'s
                        conversion), every section but the station board
                        hidden, the embedded payload cut to what the board reads
    stations/<ID>.json  one station's ``days`` and per-station fields
    stations/_shared.json  the fleet-wide fields every read repeats

Holdings and manual orders are refused (a snapshot has no account), and the
board's refresh and update checks re-read the snapshot.  Nothing is written
but the output folder.

    python3 scripts/publish_station_board_site.py [--out data/station_board_site]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
# Worker processes start fresh and import the station board from the repo.
sys.path.insert(0, str(ROOT))

import publish_dashboard_artifact as artifact  # noqa: E402

DEFAULT_IN = ROOT / "tmp" / "cal_dashboard.html"
DEFAULT_OUT = ROOT / "data" / "station_board_site"
WORKERS = 6
#: The payload keys the station board reads; everything else (the live,
#: strategy, money and model-area sections) stays out of the published page.
KEEP_PAYLOAD_KEYS = ("schema", "generated_at_utc", "p0_ngr", "system_labels")
#: Repeated identically in every station's read.
SHARED_KEYS = ("fleet", "fleet_shape", "skew_history", "mode_agreement", "taker_fee_rate")

SITE_CSS = """<style id="station-board-site">
/* Only the station board (user 2026-11-02 「只保留"站点看板"板块」). */
#app > *:not(#topnav):not(.toparea:has(#sec-station-day)):not(footer){display:none!important}
.toparea:has(#sec-station-day) > *:not(#sec-station-day){display:none!important}
#jump .jump-area:not(:has(.jump-row)){display:none!important}
#jump .jump-area > *:not(.jump-row):not(.jump-sub){display:none!important}
#jump .jump-area > .jump-l, #jump .jump-area > b{display:none!important}
[data-sd-order],[data-sd-sync],[data-sd-hold]{display:none!important}
.station-board-site-note{font-size:12px;color:var(--muted);margin:6px 0 0}
</style>"""

SITE_SHIM = """<script id="station-board-site-shim">
// Read-only snapshot: the board's calls to the local control service are
// answered from the files published beside this page.
(function(){
  const BUILT = %(built)s;
  const realFetch = window.fetch.bind(window);
  let shared = null;
  const json = (body, status) => new Response(JSON.stringify(body),
    {status: status || 200, headers: {"Content-Type": "application/json"}});
  window.fetch = async function(input, init){
    const url = typeof input === "string" ? input : (input && input.url) || "";
    if(!url.startsWith("http://127.0.0.1:8788/")) return realFetch(input, init);
    const u = new URL(url);
    if(u.pathname === "/api/station-day"){
      const station = (u.searchParams.get("station") || "").toUpperCase();
      try{
        if(!shared) shared = await (await realFetch("stations/_shared.json")).json();
        const own = await (await realFetch("stations/" + encodeURIComponent(station) + ".json")).json();
        return json({...shared, ...own});
      }catch(e){
        return json({error: "快照里没有 " + station + " 的数据"}, 404);
      }
    }
    return json({error: "这是只读快照（" + BUILT + " 生成），不连账户、不能下单"}, 403);
  };
})();
</script>"""


def build_station(station: str) -> dict:
    """One station's read, as ``live_control_server._station_day_build`` makes it."""

    from bayes_basket_target import station_day_board  # noqa: PLC0415

    return station_day_board.build(station)


def build_shared() -> dict:
    from bayes_basket_target import station_day_board  # noqa: PLC0415

    return {"fleet_shape": station_day_board.fleet_shape(),
            "skew_history": station_day_board.skew_history(),
            "mode_agreement": station_day_board.mode_agreement_history()}


def cut_payload(page: str) -> str:
    marker = "const D = "
    start = page.index(marker) + len(marker)
    payload, end = json.JSONDecoder().raw_decode(page, start)
    kept = {key: payload[key] for key in KEEP_PAYLOAD_KEYS if key in payload}
    return page[:start] + json.dumps(kept, ensure_ascii=False) + page[end:]


def board_page(source_html: str, shim: str) -> str:
    """The rendered dashboard cut to the station board, with ``shim`` answering
    its calls to the local control service.  The snapshot site and the live
    gateway (``station_board_web.py``) differ only in that shim."""

    page = artifact.convert(source_html)
    page = cut_payload(page)
    page = page.replace("<title>系统健康看板</title>", "<title>站点看板</title>", 1)
    title_end = page.index("</title>") + len("</title>")
    return page[:title_end] + "\n" + SITE_CSS + shim + page[title_end:]


def build(source: Path, out: Path, stations: list[str] | None, page_only: bool = False) -> dict:
    built = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    page = board_page(source.read_text(encoding="utf-8"), SITE_SHIM % {"built": json.dumps(built)})
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(page, encoding="utf-8")
    page_mb = round(len(page.encode("utf-8")) / 1_048_576, 2)
    if page_only:
        return {"built": built, "page_mb": page_mb}
    first = {**build_station("ZUUU"), **build_shared()}
    roster = stations or [row["station_id"] for row in first.get("fleet") or []]
    target = out / "stations"
    target.mkdir(parents=True, exist_ok=True)
    (target / "_shared.json").write_text(
        json.dumps({k: first[k] for k in SHARED_KEYS if k in first}, ensure_ascii=False), encoding="utf-8")
    written, failed = [], {}

    def write(station: str, data: dict) -> None:
        own = {k: v for k, v in data.items() if k not in SHARED_KEYS}
        own["cache"] = "snapshot"
        (target / f"{station}.json").write_text(json.dumps(own, ensure_ascii=False), encoding="utf-8")
        written.append(station)

    if "ZUUU" in roster:
        write("ZUUU", first)
    pool = ProcessPoolExecutor(max_workers=WORKERS)
    try:
        futures = {pool.submit(build_station, s): s for s in roster if s != "ZUUU"}
        for future in as_completed(futures):
            station = futures[future]
            try:
                write(station, future.result())
            except Exception as exc:  # noqa: BLE001 -- reported, the rest still ship
                failed[station] = f"{type(exc).__name__}:{exc}"[:200]
    finally:
        # A worker that has built its stations still holds the board's reader
        # threads and never exits on its own; the results are in, so end them.
        for process in list(getattr(pool, "_processes", {}).values()):
            process.terminate()
        pool.shutdown(wait=False, cancel_futures=True)
    return {"built": built, "stations": sorted(written), "failed": failed,
            "page_mb": page_mb,
            "data_mb": round(sum(p.stat().st_size for p in target.glob("*.json")) / 1_048_576, 1)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="source", type=Path, default=DEFAULT_IN)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--stations", default="", help="comma-separated; default the whole fleet")
    parser.add_argument("--page-only", action="store_true", help="rewrite index.html, keep the station files")
    args = parser.parse_args()
    stations = [s.strip().upper() for s in args.stations.split(",") if s.strip()] or None
    print(json.dumps(build(args.source, args.out, stations, page_only=args.page_only), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
