"""Async Slack Web API client for conversations.history / replies.

Surfaces the two errors that backfill code actually has to branch on:
- SlackRateLimitError carries Retry-After (seconds) from the 429 header
- SlackAPIError covers everything else with the raw `error` string
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import httpx


class SlackAPIError(Exception):
    def __init__(self, error: str, payload: Optional[dict] = None):
        super().__init__(error)
        self.error = error
        self.payload = payload


class SlackRateLimitError(SlackAPIError):
    def __init__(self, retry_after: float, payload: Optional[dict] = None):
        super().__init__("rate_limited", payload)
        self.retry_after = retry_after


@dataclass
class HistoryPage:
    messages: list[dict[str, Any]]
    has_more: bool
    next_cursor: Optional[str]


class SlackClient:
    BASE_URL = "https://slack.com/api"

    def __init__(self, token: str, http: Optional[httpx.AsyncClient] = None):
        self._token = token
        self._http = http or httpx.AsyncClient(timeout=30.0)
        self._owns_http = http is None

    async def __aenter__(self) -> "SlackClient":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def auth_test(self) -> dict[str, Any]:
        return await self._call("auth.test", method="POST", data={})

    async def fetch_history(
        self,
        channel: str,
        oldest: Optional[str] = None,
        latest: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = 15,
    ) -> HistoryPage:
        params: dict[str, Any] = {"channel": channel, "limit": min(limit, 15)}
        if oldest is not None:
            params["oldest"] = oldest
        if latest is not None:
            params["latest"] = latest
        if cursor:
            params["cursor"] = cursor
        data = await self._call("conversations.history", method="GET", params=params)
        return HistoryPage(
            messages=data.get("messages", []),
            has_more=bool(data.get("has_more", False)),
            next_cursor=(data.get("response_metadata") or {}).get("next_cursor"),
        )

    async def fetch_replies(
        self,
        channel: str,
        thread_ts: str,
        cursor: Optional[str] = None,
        limit: int = 15,
    ) -> HistoryPage:
        params: dict[str, Any] = {"channel": channel, "ts": thread_ts, "limit": min(limit, 15)}
        if cursor:
            params["cursor"] = cursor
        data = await self._call("conversations.replies", method="GET", params=params)
        return HistoryPage(
            messages=data.get("messages", []),
            has_more=bool(data.get("has_more", False)),
            next_cursor=(data.get("response_metadata") or {}).get("next_cursor"),
        )

    async def _call(
        self,
        method_name: str,
        *,
        method: str,
        params: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        url = f"{self.BASE_URL}/{method_name}"
        headers = {"Authorization": f"Bearer {self._token}"}
        resp = await self._http.request(method, url, params=params, data=data, headers=headers)
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "60"))
            raise SlackRateLimitError(retry_after, _safe_json(resp))
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("ok", False):
            raise SlackAPIError(payload.get("error", "unknown_error"), payload)
        return payload


def _safe_json(resp: httpx.Response) -> Optional[dict[str, Any]]:
    try:
        return resp.json()
    except Exception:
        return None
