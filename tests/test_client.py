"""Tests for SlackClient: request shape, response parsing, error mapping."""

import httpx
import pytest
import respx

from slack_shield import SlackAPIError, SlackClient, SlackRateLimitError


SLACK = "https://slack.com/api"


@respx.mock
async def test_auth_test_happy_path():
    route = respx.post(f"{SLACK}/auth.test").mock(
        return_value=httpx.Response(
            200,
            json={"ok": True, "team": "Acme", "team_id": "T1", "user": "bot", "user_id": "U1"},
        )
    )
    async with SlackClient("xoxb-test") as c:
        data = await c.auth_test()
    assert route.called
    assert data["team"] == "Acme"
    assert route.calls.last.request.headers["Authorization"] == "Bearer xoxb-test"


@respx.mock
async def test_fetch_history_returns_messages_and_paging():
    route = respx.get(f"{SLACK}/conversations.history").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [{"ts": "1.0", "text": "a"}, {"ts": "2.0", "text": "b"}],
                "has_more": True,
                "response_metadata": {"next_cursor": "abc123"},
            },
        )
    )
    async with SlackClient("xoxb-test") as c:
        page = await c.fetch_history(channel="C1", oldest="100", latest="200")
    req = route.calls.last.request
    assert req.url.params["channel"] == "C1"
    assert req.url.params["oldest"] == "100"
    assert req.url.params["latest"] == "200"
    assert req.url.params["limit"] == "15"
    assert len(page.messages) == 2
    assert page.has_more is True
    assert page.next_cursor == "abc123"


@respx.mock
async def test_fetch_history_clamps_limit_to_15():
    route = respx.get(f"{SLACK}/conversations.history").mock(
        return_value=httpx.Response(200, json={"ok": True, "messages": [], "has_more": False})
    )
    async with SlackClient("xoxb-test") as c:
        await c.fetch_history(channel="C1", limit=999)
    assert route.calls.last.request.url.params["limit"] == "15"


@respx.mock
async def test_fetch_history_sends_cursor_when_provided():
    route = respx.get(f"{SLACK}/conversations.history").mock(
        return_value=httpx.Response(200, json={"ok": True, "messages": [], "has_more": False})
    )
    async with SlackClient("xoxb-test") as c:
        await c.fetch_history(channel="C1", cursor="cur_xyz")
    assert route.calls.last.request.url.params["cursor"] == "cur_xyz"


@respx.mock
async def test_fetch_history_omits_optional_params_when_unset():
    route = respx.get(f"{SLACK}/conversations.history").mock(
        return_value=httpx.Response(200, json={"ok": True, "messages": [], "has_more": False})
    )
    async with SlackClient("xoxb-test") as c:
        await c.fetch_history(channel="C1")
    params = route.calls.last.request.url.params
    assert "oldest" not in params
    assert "latest" not in params
    assert "cursor" not in params


@respx.mock
async def test_429_raises_rate_limit_error_with_retry_after():
    respx.get(f"{SLACK}/conversations.history").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "47"}, json={"ok": False})
    )
    async with SlackClient("xoxb-test") as c:
        with pytest.raises(SlackRateLimitError) as exc_info:
            await c.fetch_history(channel="C1")
    assert exc_info.value.retry_after == 47.0


@respx.mock
async def test_429_without_retry_after_header_defaults_to_60():
    respx.get(f"{SLACK}/conversations.history").mock(
        return_value=httpx.Response(429, json={"ok": False})
    )
    async with SlackClient("xoxb-test") as c:
        with pytest.raises(SlackRateLimitError) as exc_info:
            await c.fetch_history(channel="C1")
    assert exc_info.value.retry_after == 60.0


@respx.mock
async def test_ok_false_raises_api_error_with_error_string():
    respx.get(f"{SLACK}/conversations.history").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "not_in_channel"})
    )
    async with SlackClient("xoxb-test") as c:
        with pytest.raises(SlackAPIError) as exc_info:
            await c.fetch_history(channel="C1")
    assert exc_info.value.error == "not_in_channel"


@respx.mock
async def test_fetch_replies_passes_thread_ts():
    route = respx.get(f"{SLACK}/conversations.replies").mock(
        return_value=httpx.Response(
            200,
            json={"ok": True, "messages": [{"ts": "1.0"}], "has_more": False},
        )
    )
    async with SlackClient("xoxb-test") as c:
        page = await c.fetch_replies(channel="C1", thread_ts="1234567.890")
    assert route.calls.last.request.url.params["ts"] == "1234567.890"
    assert len(page.messages) == 1
