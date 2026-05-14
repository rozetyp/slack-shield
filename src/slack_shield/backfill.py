"""Resumable, time-windowed backfill orchestrator.

Walks a channel backwards in fixed time windows, paginating inside each
window with `limit=15` (the unlisted-app cap). State is checkpointed to a
single JSON file after every successful page so a Ctrl-C / crash / reboot
can resume without re-pulling. Messages are appended to a JSONL file.

Adaptive windowing: if a window paginates more than `shrink_threshold`
pages we cut later windows in half. Empty windows we leave alone; the
cost is one API call per window regardless of contents.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from slack_shield.client import (
    HistoryPage,
    SlackAPIError,
    SlackClient,
    SlackRateLimitError,
)
from slack_shield.limiter import RateLimiter


log = logging.getLogger("slack_shield")


@dataclass
class Window:
    start_ts: float
    end_ts: float
    cursor: Optional[str] = None
    done: bool = False
    pages: int = 0
    messages: int = 0


@dataclass
class BackfillState:
    channel: str
    since_ts: float
    until_ts: float
    windows: list[Window] = field(default_factory=list)
    total_messages: int = 0
    started_at: float = 0.0
    finished_at: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "since_ts": self.since_ts,
            "until_ts": self.until_ts,
            "windows": [asdict(w) for w in self.windows],
            "total_messages": self.total_messages,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BackfillState":
        return cls(
            channel=data["channel"],
            since_ts=data["since_ts"],
            until_ts=data["until_ts"],
            windows=[Window(**w) for w in data.get("windows", [])],
            total_messages=data.get("total_messages", 0),
            started_at=data.get("started_at", 0.0),
            finished_at=data.get("finished_at"),
        )


class Backfill:
    """Resumable Slack channel backfill.

    Output:
      - messages_path: appended JSONL, one message per line (raw Slack payload)
      - state_path: JSON checkpoint, rewritten after every successful page

    The same (state_path, channel, since_ts, until_ts) tuple can be re-run
    after a crash and it will pick up where it left off.
    """

    def __init__(
        self,
        client: SlackClient,
        limiter: RateLimiter,
        channel: str,
        since_ts: float,
        until_ts: float,
        state_path: Path,
        messages_path: Path,
        *,
        workspace_key: str = "default",
        window_days: int = 7,
        shrink_threshold: int = 3,
    ):
        self._client = client
        self._limiter = limiter
        self._channel = channel
        self._workspace = workspace_key
        self._state_path = state_path
        self._messages_path = messages_path
        self._window_days = window_days
        self._shrink_threshold = shrink_threshold
        self.state = self._load_or_init(channel, since_ts, until_ts, window_days)

    # ------------------------------------------------------------------ state

    def _load_or_init(
        self, channel: str, since_ts: float, until_ts: float, window_days: int
    ) -> BackfillState:
        if self._state_path.exists():
            data = json.loads(self._state_path.read_text())
            state = BackfillState.from_dict(data)
            if (
                state.channel == channel
                and abs(state.since_ts - since_ts) < 1
                and abs(state.until_ts - until_ts) < 1
            ):
                log.info("resuming %d/%d windows", _done(state), len(state.windows))
                return state
            log.warning("checkpoint at %s doesn't match this job, starting fresh", self._state_path)
        return _fresh_state(channel, since_ts, until_ts, window_days)

    def _save(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.state.to_dict(), indent=2))
        tmp.replace(self._state_path)

    # -------------------------------------------------------------------- run

    async def run(self) -> AsyncIterator[Window]:
        """Run the backfill, yielding each window as it finishes.

        Usage: `async for w in backfill.run(): ...`
        Callers can ignore the yielded windows; the side effect is appending
        to messages_path and rewriting state_path. Yielding lets callers
        print progress or stop early.
        """
        if self.state.started_at == 0.0:
            self.state.started_at = _now()
            self._save()

        self._messages_path.parent.mkdir(parents=True, exist_ok=True)
        self._messages_path.touch(exist_ok=True)

        idx = 0
        while idx < len(self.state.windows):
            window = self.state.windows[idx]
            if window.done:
                idx += 1
                continue
            await self._drain_window(window, idx)
            self._save()
            yield window
            idx += 1

        self.state.finished_at = _now()
        self._save()

    async def _drain_window(self, window: Window, idx: int) -> None:
        log.info(
            "window %d/%d  %s → %s",
            idx + 1,
            len(self.state.windows),
            _fmt(window.start_ts),
            _fmt(window.end_ts),
        )
        while True:
            page = await self._fetch_page(window)
            self._append_messages(page.messages)
            window.pages += 1
            window.messages += len(page.messages)
            self.state.total_messages += len(page.messages)
            window.cursor = page.next_cursor
            self._save()  # checkpoint after every successful page
            # Heavy pagination: split future windows once, the moment we cross
            # the threshold. Using == (not >=) so we only fire once per window
            # rather than re-splitting on every subsequent page.
            if window.pages == self._shrink_threshold:
                self._shrink_remaining_windows(idx)
            if not page.has_more:
                window.done = True
                window.cursor = None
                return

    async def _fetch_page(self, window: Window) -> HistoryPage:
        while True:
            await self._limiter.acquire(self._workspace, "conversations.history")
            try:
                return await self._client.fetch_history(
                    channel=self._channel,
                    oldest=str(window.start_ts),
                    latest=str(window.end_ts),
                    cursor=window.cursor,
                    limit=15,
                )
            except SlackRateLimitError as exc:
                log.warning("429: sleeping %.0fs as Slack requested", exc.retry_after)
                await self._limiter.note_retry_after(
                    self._workspace, "conversations.history", exc.retry_after
                )
                # loop and re-acquire
            except SlackAPIError as exc:
                if exc.error in {"not_in_channel", "missing_scope", "channel_not_found"}:
                    raise
                log.error("slack error %r, re-raising", exc.error)
                raise

    # --------------------------------------------------------- adaptive sizing

    def _shrink_remaining_windows(self, current_idx: int) -> None:
        """Halve the size of any not-yet-started windows after current_idx."""
        new_windows: list[Window] = []
        changed = False
        for i, w in enumerate(self.state.windows):
            if i <= current_idx or w.done or w.pages > 0:
                new_windows.append(w)
                continue
            span = w.end_ts - w.start_ts
            if span <= 86400:  # don't go below 1 day
                new_windows.append(w)
                continue
            mid = w.start_ts + span / 2
            new_windows.append(Window(start_ts=w.start_ts, end_ts=mid))
            new_windows.append(Window(start_ts=mid, end_ts=w.end_ts))
            changed = True
        if changed:
            log.info("heavy pagination: split %d future windows", len(new_windows) - len(self.state.windows))
            self.state.windows = new_windows

    # ----------------------------------------------------------------- output

    def _append_messages(self, messages: list[dict[str, Any]]) -> None:
        if not messages:
            return
        with self._messages_path.open("a", encoding="utf-8") as f:
            for m in messages:
                f.write(json.dumps(m, ensure_ascii=False))
                f.write("\n")

    # ------------------------------------------------------------------ stats

    def eta_seconds(self) -> float:
        """Crude ETA: 60s per remaining window. Pessimistic but honest."""
        remaining = sum(1 for w in self.state.windows if not w.done)
        return remaining * 60.0

    def progress(self) -> dict[str, Any]:
        total = len(self.state.windows)
        done = _done(self.state)
        return {
            "windows_total": total,
            "windows_done": done,
            "messages_collected": self.state.total_messages,
            "eta_seconds": self.eta_seconds(),
            "percent": (done / total * 100) if total else 0.0,
        }


# --------------------------------------------------------------------- helpers


def _fresh_state(channel: str, since_ts: float, until_ts: float, window_days: int) -> BackfillState:
    windows: list[Window] = []
    cur = since_ts
    step = window_days * 86400
    while cur < until_ts:
        nxt = min(cur + step, until_ts)
        windows.append(Window(start_ts=cur, end_ts=nxt))
        cur = nxt
    return BackfillState(
        channel=channel,
        since_ts=since_ts,
        until_ts=until_ts,
        windows=windows,
    )


def _done(state: BackfillState) -> int:
    return sum(1 for w in state.windows if w.done)


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
