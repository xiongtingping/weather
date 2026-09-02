"""唯一本机 clob_order_service 客户端。

该客户端只负责把原始证据请求转发给本机唯一 gateway，并不决定是否
下单、不重试有歧义的 POST，也不读取天气、概率或策略数据。
"""

from __future__ import annotations

import ipaddress
import json
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


SCHEMA_VERSION = "clob_order_service_client_v1"


class ClobOrderServiceClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8765", *, timeout_seconds: float = 1.5) -> None:
        parsed = urlparse(base_url)
        host = parsed.hostname or ""
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host == "localhost"
        if (
            parsed.scheme != "http"
            or not loopback
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("clob_order_service_gateway_must_be_local_http")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = max(0.05, float(timeout_seconds))

    def _post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = Request(
            self.base_url + path,
            data=json.dumps(dict(payload), ensure_ascii=False, sort_keys=True).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                value = json.loads(raw.decode("utf-8")) if raw else {}
                return dict(value) if isinstance(value, Mapping) else {"ok": False, "error": "gateway_payload_invalid"}
        except HTTPError as exc:
            # A 5xx is the gateway failing after it had the request, which it
            # may already have acted on; a 4xx is the gateway declining to.
            # Only the second is safe to read as "nothing was placed".
            return {
                "ok": False,
                "http_status": int(exc.code),
                "error": "gateway_http_error",
                "transport_ambiguous": int(exc.code) >= 500,
            }
        except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            # The request left this process and no answer came back, so
            # whether the gateway forwarded it is unknown here.  Saying
            # "refused" would be a claim this layer cannot support: the
            # client waits 15s while the gateway's own hop to the exchange
            # can take longer, and on 2026-08-31 an order placed under
            # exactly that silence rested on the account for eighteen
            # minutes while every local table said nothing was sent.
            # Flagged so the caller records the ambiguity instead of a
            # refusal -- an unknown that reconciliation can settle beats a
            # false negative that nothing will ever revisit.
            return {
                "ok": False,
                "error": f"gateway_transport:{type(exc).__name__}",
                "transport_ambiguous": True,
            }

    def freeze_execution_guard_snapshot(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self._post("/execution-guard-snapshot", request)

    def place_order(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = dict(payload)
        if request.get("direct_target_order_context") is True:
            request["async_ack"] = True
        return self._post("/place-order", request)

    def lookup_order(self, lookup: Mapping[str, Any]) -> dict[str, Any]:
        return self._post("/reconcile-order", {"idempotency_key": lookup.get("idempotency_key")})

    def cancel_order(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self._post(
            "/cancel-order",
            {
                "order_id": request.get("order_id") or request.get("orderID"),
                "idempotency_key": request.get("idempotency_key"),
            },
        )

    def lookup_exchange_order(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self._post(
            "/lookup-exchange-order",
            {"order_id": request.get("order_id") or request.get("orderID") or request.get("exchange_order_id")},
        )


__all__ = ["ClobOrderServiceClient", "SCHEMA_VERSION"]
