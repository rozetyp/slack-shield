"""End-to-end tests for the Backfill orchestrator.

These never call real Slack. respx swaps the HTTP layer so we can drive the
orchestrator through every branch (empty channel, paginated channel, 429,
heavy-pagination shrink, resume from checkpoint).
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from slack_shield import Backfill, BackfillState, RateLimiter, SlackClient
from slack_shield.backfill import Window, _fresh_state


SLACK_HIST = "https://slack.com/api/conversations.history"


def _ts(date: str) -> float:
    return datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


async def _run(bf: Backfill) -> list[Window]:
    out = []
    async for w in bf.run():
        out.append(w)
    return out


def _make_backfill(
    tmp_path: Path,
    *,
    since="2024-01-01",
    until="2024-01-22",
    window_days=7,
    shrink_threshold=3,
):
    """Helper: build a Backfill with a fast limiter and tmp paths."""
    client = SlackClient("xoxb-test")
    limiter = RateLimiter(interval_seconds=0.0)
    return Backfill(
        client=client,
        limiter=limiter,
        channel="C1",
        since_ts=_ts(since),
        until_ts=_ts(until),
        state_path=tmp_path / "state.json",
        messages_path=tmp_path / "out.jsonl",
        window_days=window_days,
        shrink_threshold=shrink_threshold,
    )


# ----------------------------------------------------- window planning


def test_fresh_state_generates_correct_window_count():
    s = _fresh_state("C1", _ts("2024-01-01"), _ts("2024-01-22"), 7)
    assert len(s.windows) == 3
    assert s.windows[0].start_ts == _ts("2024-01-01")
    assert s.windows[0].end_ts == _ts("2024-01-08")
    assert s.windows[-1].end_ts == _ts("2024-01-22")


def test_fresh_state_short_range_makes_one_window():
    s = _fresh_state("C1", _ts("2024-01-01"), _ts("2024-01-03"), 7)
    assert len(s.windows) == 1
    assert s.windows[0].end_ts == _ts("2024-01-03")


# ----------------------------------------------------- happy paths


@respx.mock
async def test_empty_channel_completes_with_zero_messages(tmp_path):
    respx.get(SLACK_HIST).mock(
        return_value=httpx.Response(200, json={"ok": True, "messages": [], "has_more": False})
    )
    bf = _make_backfill(tmp_path)
    windows = await _run(bf)
    assert len(windows) == 3
    assert all(w.done for w in bf.state.windows)
    assert bf.state.total_messages == 0
    assert bf.state.finished_at is not None
    assert (tmp_path / "out.jsonl").read_text() == ""


@respx.mock
async def test_single_page_per_window_writes_jsonl(tmp_path):
    respx.get(SLACK_HIST).mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [{"ts": "1.0", "text": "hi"}],
                "has_more": False,
            },
        )
    )
    bf = _make_backfill(tmp_path)
    await _run(bf)
    lines = (tmp_path / "out.jsonl").read_text().splitlines()
    assert len(lines) == 3  # one message per window, 3 windows
    parsed = [json.loads(l) for l in lines]
    assert all(m["text"] == "hi" for m in parsed)
    assert bf.state.total_messages == 3


@respx.mock
async def test_pagination_within_window_walks_cursor(tmp_path):
    """Window 1 returns has_more+cursor; second call comes back with that cursor."""
    respx.get(SLACK_HIST).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "ok": True,
                    "messages": [{"ts": "1.0"}],
                    "has_more": True,
                    "response_metadata": {"next_cursor": "CUR_A"},
                },
            ),
            httpx.Response(
                200,
                json={"ok": True, "messages": [{"ts": "2.0"}, {"ts": "3.0"}], "has_more": False},
            ),
            # windows 2 and 3 are empty
            httpx.Response(200, json={"ok": True, "messages": [], "has_more": False}),
            httpx.Response(200, json={"ok": True, "messages": [], "has_more": False}),
        ]
    )
    bf = _make_backfill(tmp_path)
    await _run(bf)
    calls = respx.calls
    # second call must carry the cursor from the first
    assert calls[1].request.url.params["cursor"] == "CUR_A"
    assert bf.state.windows[0].pages == 2
    assert bf.state.windows[0].messages == 3
    assert bf.state.total_messages == 3


# ----------------------------------------------------- checkpointing & resume


@respx.mock
async def test_state_is_checkpointed_after_each_window(tmp_path):
    respx.get(SLACK_HIST).mock(
        return_value=httpx.Response(
            200, json={"ok": True, "messages": [{"ts": "1.0"}], "has_more": False}
        )
    )
    bf = _make_backfill(tmp_path)
    saved_states = []
    async for _ in bf.run():
        # snapshot the on-disk state after each yielded window
        saved_states.append(json.loads((tmp_path / "state.json").read_text()))
    assert len(saved_states) == 3
    # each snapshot has progressively more windows marked done
    done_counts = [sum(1 for w in s["windows"] if w["done"]) for s in saved_states]
    assert done_counts == [1, 2, 3]


@respx.mock
async def test_resume_skips_completed_windows(tmp_path):
    # pre-write a state where windows 0 and 1 are already done
    state_path = tmp_path / "state.json"
    out_path = tmp_path / "out.jsonl"
    state = _fresh_state("C1", _ts("2024-01-01"), _ts("2024-01-22"), 7)
    state.windows[0].done = True
    state.windows[0].messages = 10
    state.windows[1].done = True
    state.windows[1].messages = 5
    state.total_messages = 15
    state.started_at = 1.0  # already started
    state_path.write_text(json.dumps(state.to_dict()))

    respx.get(SLACK_HIST).mock(
        return_value=httpx.Response(
            200, json={"ok": True, "messages": [{"ts": "9.0"}], "has_more": False}
        )
    )

    bf = Backfill(
        client=SlackClient("xoxb-test"),
        limiter=RateLimiter(interval_seconds=0.0),
        channel="C1",
        since_ts=_ts("2024-01-01"),
        until_ts=_ts("2024-01-22"),
        state_path=state_path,
        messages_path=out_path,
    )
    yielded = await _run(bf)

    # only window 2 was pending; should make exactly one API call
    assert len(respx.calls) == 1
    assert len(yielded) == 1
    assert all(w.done for w in bf.state.windows)
    # JSONL contains only the new fetch
    lines = out_path.read_text().splitlines()
    assert len(lines) == 1


@respx.mock
async def test_mismatched_checkpoint_starts_fresh(tmp_path):
    """If state file is for a different range, we ignore it and start over."""
    state_path = tmp_path / "state.json"
    # checkpoint says channel C1 from 2023 to 2024
    stale = _fresh_state("C1", _ts("2023-01-01"), _ts("2024-01-01"), 7)
    stale.windows[0].done = True
    state_path.write_text(json.dumps(stale.to_dict()))

    respx.get(SLACK_HIST).mock(
        return_value=httpx.Response(200, json={"ok": True, "messages": [], "has_more": False})
    )
    # call with a different range
    bf = _make_backfill(tmp_path, since="2025-01-01", until="2025-01-15")
    await _run(bf)
    # state should reflect the *new* range, not the stale one
    assert bf.state.since_ts == _ts("2025-01-01")
    assert all(w.done for w in bf.state.windows)


# ----------------------------------------------------- 429 handling


@respx.mock
async def test_429_retries_same_page_after_retry_after(tmp_path):
    respx.get(SLACK_HIST).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0.05"}, json={"ok": False}),
            httpx.Response(
                200, json={"ok": True, "messages": [{"ts": "1.0"}], "has_more": False}
            ),
            # remaining 2 windows
            httpx.Response(200, json={"ok": True, "messages": [], "has_more": False}),
            httpx.Response(200, json={"ok": True, "messages": [], "has_more": False}),
        ]
    )
    bf = _make_backfill(tmp_path)
    await _run(bf)
    assert len(respx.calls) == 4  # one retry + 3 windows
    assert bf.state.total_messages == 1


# ----------------------------------------------------- adaptive shrinking


@respx.mock
async def test_heavy_pagination_shrinks_future_windows(tmp_path):
    """A window that paginates >= shrink_threshold times should halve later windows."""
    # Build a long range with two 7-day windows.
    # Window 0 returns 3 paginated pages (hits shrink_threshold=3), then stops.
    # We expect window 1 (originally 7 days) to be split into 2 windows.
    respx.get(SLACK_HIST).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "ok": True,
                    "messages": [{"ts": f"{i}.0"}],
                    "has_more": True,
                    "response_metadata": {"next_cursor": f"C{i}"},
                },
            )
            for i in range(1, 3)
        ]
        + [
            httpx.Response(
                200, json={"ok": True, "messages": [{"ts": "3.0"}], "has_more": False}
            ),
            # plenty of empty responses for the split-up future windows
            httpx.Response(200, json={"ok": True, "messages": [], "has_more": False}),
            httpx.Response(200, json={"ok": True, "messages": [], "has_more": False}),
            httpx.Response(200, json={"ok": True, "messages": [], "has_more": False}),
        ]
    )
    bf = _make_backfill(tmp_path, since="2024-01-01", until="2024-01-15", window_days=7,
                       shrink_threshold=3)
    initial_count = len(bf.state.windows)
    assert initial_count == 2
    await _run(bf)
    # window 1 (the future one) should have been split
    assert len(bf.state.windows) > initial_count, (
        f"expected split; still have {len(bf.state.windows)} windows"
    )


# ----------------------------------------------------- progress reporting


@respx.mock
async def test_progress_reports_sensible_numbers(tmp_path):
    respx.get(SLACK_HIST).mock(
        return_value=httpx.Response(
            200, json={"ok": True, "messages": [{"ts": "1.0"}], "has_more": False}
        )
    )
    bf = _make_backfill(tmp_path)
    p0 = bf.progress()
    assert p0["windows_total"] == 3
    assert p0["windows_done"] == 0
    assert p0["eta_seconds"] == 180.0  # 3 * 60s
    await _run(bf)
    p1 = bf.progress()
    assert p1["windows_done"] == 3
    assert p1["messages_collected"] == 3
    assert p1["eta_seconds"] == 0.0
    assert p1["percent"] == 100.0
