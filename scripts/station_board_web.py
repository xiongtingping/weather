#!/usr/bin/env python3
"""The station board as a live public website (user 2026-11-03 「我想要的不是快照，
是部署上线成网站」; ngrok tunnel, public, no login).

A read-only gateway on 127.0.0.1, published by ngrok (launchd
``com.weather.station_board_tunnel``).  It serves exactly two things:

    GET /                    the station board page, rebuilt whenever the
                             dashboard render rewrites tmp/cal_dashboard.html
                             (``publish_station_board_site.board_page``: every
                             other section hidden, the embedded payload cut to
                             what the board reads)
    GET /api/station-day     the board's read, passed to the local control
                             service with only ``station`` and ``token_only``

Everything else is 404 or 405.  The control service itself -- the live switch,
the lane switches, manual orders, holdings -- is never reachable from here, and
``force`` is dropped so a visitor cannot make it rebuild on demand.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import publish_station_board_site as site  # noqa: E402

HOST, PORT = "127.0.0.1", 8794
SOURCE = ROOT / "tmp" / "cal_dashboard.html"
UPSTREAM = "http://127.0.0.1:8788/api/station-day"
UPSTREAM_TIMEOUT_SEC = 60.0
# No proxies, and no proxy lookup: the upstream is this machine, and urllib's
# default opener asks macOS for the system proxy (``_scproxy``), which killed
# the process outright under launchd -- an empty reply, no traceback.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

LIVE_SHIM = """<script id="station-board-web-shim">
// The public site: the board's reads go to this server, which passes them to
// the local control service; nothing else of that service is reachable.
(function(){
  const realFetch = window.fetch.bind(window);
  window.fetch = function(input, init){
    const url = typeof input === "string" ? input : (input && input.url) || "";
    if(!url.startsWith("http://127.0.0.1:8788/")) return realFetch(input, init);
    const u = new URL(url);
    if(u.pathname === "/api/station-day"){
      u.searchParams.delete("force");
      return realFetch("/api/station-day?" + u.searchParams.toString());
    }
    return Promise.resolve(new Response(JSON.stringify({error: "公开网站只读：不连账户、不能下单"}),
      {status: 403, headers: {"Content-Type": "application/json"}}));
  };
})();
</script>"""

_PAGE: dict[str, object] = {"mtime": None, "body": b""}
_PAGE_LOCK = threading.Lock()


def page_bytes() -> bytes:
    """The page, rebuilt when the render has rewritten its source."""

    mtime = SOURCE.stat().st_mtime
    with _PAGE_LOCK:
        if _PAGE["mtime"] != mtime:
            _PAGE["body"] = site.board_page(SOURCE.read_text(encoding="utf-8"), LIVE_SHIM).encode("utf-8")
            _PAGE["mtime"] = mtime
        return _PAGE["body"]  # type: ignore[return-value]


def upstream_query(query: str) -> str | None:
    """Only a four-letter station and ``token_only``; anything else is refused."""

    params = urllib.parse.parse_qs(query or "")
    station = str((params.get("station") or [""])[0]).strip().upper()
    if not re.fullmatch(r"[A-Z]{4}", station):
        return None
    kept = {"station": station}
    if str((params.get("token_only") or ["0"])[0]) == "1":
        kept["token_only"] = "1"
    return urllib.parse.urlencode(kept)


class Handler(BaseHTTPRequestHandler):
    server_version = "station-board"

    def _send(self, status: int, body: bytes, content_type: str, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 -- http.server's name
        path, _sep, query = self.path.partition("?")
        try:
            if path in ("/", "/index.html"):
                self._send(200, page_bytes(), "text/html; charset=utf-8")
            elif path == "/api/station-day":
                kept = upstream_query(query)
                if kept is None:
                    self._json(400, {"error": "station 必须是四个字母的机场代码"})
                    return
                try:
                    with _OPENER.open(f"{UPSTREAM}?{kept}", timeout=UPSTREAM_TIMEOUT_SEC) as response:
                        self._send(response.status, response.read(), "application/json; charset=utf-8")
                except urllib.error.HTTPError as exc:
                    self._send(exc.code, exc.read(), "application/json; charset=utf-8")
            elif path == "/healthz":
                self._send(200, b"ok", "text/plain")
            else:
                self._json(404, {"error": "not found"})
        except (urllib.error.URLError, TimeoutError) as exc:
            self._json(502, {"error": f"看板数据服务暂不可用：{type(exc).__name__}"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001 -- one request, never the server
            self._json(500, {"error": f"{type(exc).__name__}"})

    do_HEAD = do_GET

    def _refuse(self) -> None:
        self._json(405, {"error": "只读网站"})

    do_POST = do_PUT = do_DELETE = do_PATCH = _refuse

    def log_message(self, fmt: str, *args) -> None:  # one line per request, to stderr
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))


def main() -> int:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"station board: http://{HOST}:{PORT}/", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
