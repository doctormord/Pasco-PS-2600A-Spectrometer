"""Thin HTTP client for the PS-2600A REST API (/api/v1).

Talks to the API ONLY over HTTP (httpx). It imports neither the engine nor the
API implementation, so the MCP server can run on another machine than the app.

Environment
    SPECTRO_API_URL     base URL of the app (default http://127.0.0.1:8000)
    SPECTRO_API_TOKEN   bearer token (shown in the app's "Remote access" panel)
    SPECTRO_CLIENT_ID   caller name for the control lease (default "mcp")
"""
import os
from typing import Any

import httpx

DEFAULT_URL = "http://127.0.0.1:8000"
DEFAULT_CLIENT_ID = "mcp"
API_PREFIX = "/api/v1"


def env(name: str, default: str = "") -> str:
    """Environment value; empty / whitespace-only counts as 'not set'."""
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else default


class ApiError(Exception):
    """An error answer of the API (or a transport problem), as one clear message."""

    def __init__(self, message: str, *, status: int | None = None,
                 code: str | None = None, details: Any = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.details = details

    def __str__(self) -> str:
        head = f"{self.code}: " if self.code else ""
        s = head + self.message
        if self.status:
            s += f" (HTTP {self.status})"
        return s


_HINTS = {
    "unauthorized": "Check SPECTRO_API_TOKEN (copy the token from the app's Remote access panel).",
    "not_connected": "No device connected - call connect() first.",
    "control_busy": "Another client or the local GUI holds control of the device.",
}


def _parse_error(resp: httpx.Response) -> ApiError:
    code = message = None
    details = None
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            code, message, details = err.get("code"), err.get("message"), err.get("details")
        elif isinstance(body, dict) and "detail" in body:
            message = str(body["detail"])
    except Exception:
        pass
    if not message:
        message = (resp.text or resp.reason_phrase or "error")[:300]
    hint = _HINTS.get(code or "")
    if hint:
        message = f"{message} {hint}"
    return ApiError(message, status=resp.status_code, code=code, details=details)


class SpectroClient:
    """Async client; one short-lived HTTP connection per request (loop safe)."""

    def __init__(self, base_url: str | None = None, token: str | None = None,
                 client_id: str | None = None, *, timeout_s: float = 30.0):
        self.base_url = (base_url or env("SPECTRO_API_URL", DEFAULT_URL)).rstrip("/")
        self.token = token if token is not None else env("SPECTRO_API_TOKEN")
        self.client_id = client_id or env("SPECTRO_CLIENT_ID", DEFAULT_CLIENT_ID)
        self.timeout_s = timeout_s

    def headers(self) -> dict[str, str]:
        h = {"X-Client-Id": self.client_id, "Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def _send(self, method: str, path: str, *, json: Any = None,
                    params: dict | None = None, timeout_s: float | None = None
                    ) -> httpx.Response:
        url = self.base_url + (path if path.startswith("/api/") else API_PREFIX + path)
        params = {k: v for k, v in (params or {}).items() if v is not None}
        t = httpx.Timeout(timeout_s or self.timeout_s, connect=5.0)
        try:
            async with httpx.AsyncClient(timeout=t) as c:
                resp = await c.request(method, url, json=json, params=params,
                                       headers=self.headers())
        except httpx.ConnectError:
            raise ApiError(f"Cannot connect to the spectrometer API at {self.base_url}. "
                           "Start the app with Remote access enabled (or "
                           "'PS-2600A_Pro.py --headless') and check SPECTRO_API_URL.",
                           code="unreachable")
        except httpx.TimeoutException:
            raise ApiError(f"The API at {self.base_url} did not answer in time.",
                           code="timeout")
        except httpx.HTTPError as e:
            raise ApiError(f"HTTP error talking to {self.base_url}: {e}", code="http_error")
        if resp.status_code >= 400:
            raise _parse_error(resp)
        return resp

    async def request(self, method: str, path: str, *, json: Any = None,
                      params: dict | None = None, timeout_s: float | None = None) -> Any:
        """JSON answer (2xx incl. 202)."""
        resp = await self._send(method, path, json=json, params=params, timeout_s=timeout_s)
        try:
            return resp.json()
        except ValueError:
            raise ApiError("The API answered with something that is not JSON.",
                           status=resp.status_code, code="bad_response")

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, body: Any = None, *, timeout_s: float | None = None) -> Any:
        return await self.request("POST", path, json=body if body is not None else {},
                                  timeout_s=timeout_s)

    async def put(self, path: str, body: Any) -> Any:
        return await self.request("PUT", path, json=body)

    async def download(self, method: str, path: str, *, json: Any = None,
                       params: dict | None = None, timeout_s: float | None = None) -> bytes:
        """Raw bytes of a file answer (export / report)."""
        resp = await self._send(method, path, json=json, params=params, timeout_s=timeout_s)
        return resp.content
