"""slack-shield command-line interface."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from slack_shield.backfill import Backfill, _fresh_state
from slack_shield.client import SlackAPIError, SlackClient
from slack_shield.limiter import RateLimiter


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    return args.func(args)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="slack-shield", description=__doc__)
    p.add_argument("-q", "--quiet", action="store_true", help="only warnings/errors")
    sub = p.add_subparsers(dest="cmd", required=True)

    # backfill
    b = sub.add_parser("backfill", help="Pull Slack channel history under 1 req/min")
    b.add_argument("--token", default=os.environ.get("SLACK_TOKEN"),
                   help="Slack bot/user token (or set SLACK_TOKEN)")
    b.add_argument("--channel", required=True, help="Channel ID, e.g. C01234ABCD")
    b.add_argument("--since", required=True, help="ISO date or unix ts, e.g. 2024-01-01")
    b.add_argument("--until", default=None, help="ISO date or unix ts, default = now")
    b.add_argument("--state", default=None,
                   help="Checkpoint file (default: .checkpoints/<channel>.json)")
    b.add_argument("--out", default=None,
                   help="Output JSONL file (default: <channel>.jsonl)")
    b.add_argument("--window-days", type=int, default=7, help="Initial window size in days")
    b.add_argument("--workspace-key", default="default",
                   help="Logical name for rate-limit bucketing (used if you run >1 job)")
    b.add_argument("--plan", action="store_true", help="Print the plan and exit (no API calls)")
    b.set_defaults(func=_cmd_backfill)

    # to-csv
    c = sub.add_parser("to-csv", help="Flatten a backfill JSONL into a CSV")
    c.add_argument("input", help="Path to JSONL produced by `backfill`")
    c.add_argument("output", help="Path to write CSV to")
    c.set_defaults(func=_cmd_to_csv)

    # auth-test
    a = sub.add_parser("auth-test", help="Verify a Slack token and print workspace info")
    a.add_argument("--token", default=os.environ.get("SLACK_TOKEN"))
    a.set_defaults(func=_cmd_auth)

    return p


# ------------------------------------------------------------------ commands


def _cmd_backfill(args: argparse.Namespace) -> int:
    if not args.token and not args.plan:
        print("error: --token or SLACK_TOKEN required (or use --plan)", file=sys.stderr)
        return 2
    since_ts = _parse_when(args.since)
    until_ts = _parse_when(args.until) if args.until else datetime.now(timezone.utc).timestamp()
    if since_ts >= until_ts:
        print("error: --since must be earlier than --until", file=sys.stderr)
        return 2

    state_path = Path(args.state) if args.state else Path(".checkpoints") / f"{args.channel}.json"
    out_path = Path(args.out) if args.out else Path(f"{args.channel}.jsonl")

    if args.plan:
        state = _fresh_state(args.channel, since_ts, until_ts, args.window_days)
        n = len(state.windows)
        print(f"channel:    {args.channel}")
        print(f"range:      {_fmt(since_ts)} → {_fmt(until_ts)}")
        print(f"windows:    {n} × {args.window_days}d")
        print(f"floor ETA:  {n} min  ({n / 60:.1f} h)  at the 1 req/min limit")
        print(f"state:      {state_path}")
        print(f"output:     {out_path}")
        return 0

    return asyncio.run(_run_backfill(args, since_ts, until_ts, state_path, out_path))


async def _run_backfill(
    args: argparse.Namespace,
    since_ts: float,
    until_ts: float,
    state_path: Path,
    out_path: Path,
) -> int:
    limiter = RateLimiter(interval_seconds=60.0)
    async with SlackClient(args.token) as client:
        backfill = Backfill(
            client=client,
            limiter=limiter,
            channel=args.channel,
            since_ts=since_ts,
            until_ts=until_ts,
            state_path=state_path,
            messages_path=out_path,
            workspace_key=args.workspace_key,
            window_days=args.window_days,
        )
        total = len(backfill.state.windows)
        try:
            async for w in backfill.run():
                prog = backfill.progress()
                logging.info(
                    "done %d/%d  msgs=%d  eta≈%dm",
                    prog["windows_done"],
                    total,
                    prog["messages_collected"],
                    int(prog["eta_seconds"] / 60),
                )
        except SlackAPIError as exc:
            print(f"\nslack error: {exc.error}", file=sys.stderr)
            _hint_for_error(exc.error)
            return 1

    p = backfill.progress()
    print(f"\nfinished. {p['messages_collected']} messages → {out_path}")
    print(f"checkpoint: {state_path}")
    return 0


def _cmd_to_csv(args: argparse.Namespace) -> int:
    src = Path(args.input)
    dst = Path(args.output)
    if not src.exists():
        print(f"error: {src} not found", file=sys.stderr)
        return 2
    fields = ["ts", "datetime_utc", "user", "type", "subtype", "thread_ts", "reply_count", "text"]
    n = 0
    with src.open(encoding="utf-8") as fin, dst.open("w", encoding="utf-8", newline="") as fout:
        writer = csv.DictWriter(fout, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for line in fin:
            line = line.strip()
            if not line:
                continue
            m = json.loads(line)
            ts = m.get("ts")
            row = {
                "ts": ts,
                "datetime_utc": _ts_to_iso(ts),
                "user": m.get("user") or m.get("bot_id") or "",
                "type": m.get("type", ""),
                "subtype": m.get("subtype", ""),
                "thread_ts": m.get("thread_ts", ""),
                "reply_count": m.get("reply_count", ""),
                "text": (m.get("text") or "").replace("\r\n", "\n"),
            }
            writer.writerow(row)
            n += 1
    print(f"wrote {n} rows → {dst}")
    return 0


def _cmd_auth(args: argparse.Namespace) -> int:
    if not args.token:
        print("error: --token or SLACK_TOKEN required", file=sys.stderr)
        return 2
    return asyncio.run(_run_auth(args.token))


async def _run_auth(token: str) -> int:
    async with SlackClient(token) as client:
        try:
            data = await client.auth_test()
        except SlackAPIError as exc:
            print(f"auth failed: {exc.error}", file=sys.stderr)
            return 1
    print(f"team:     {data.get('team')}  ({data.get('team_id')})")
    print(f"user:     {data.get('user')}  ({data.get('user_id')})")
    print(f"is_bot:   {data.get('is_bot', False)}")
    print(f"url:      {data.get('url')}")
    return 0


# ------------------------------------------------------------------- helpers


def _parse_when(s: str) -> float:
    s = s.strip()
    # bare unix ts (possibly with .ms)
    try:
        return float(s)
    except ValueError:
        pass
    # ISO-ish
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    # fromisoformat handles the rest in 3.11+
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _ts_to_iso(ts: Optional[str]) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()
    except Exception:
        return ""


def _hint_for_error(err: str) -> None:
    hints = {
        "not_in_channel": (
            "The bot/app isn't a member of this channel. Invite it with "
            "/invite @your-bot in Slack, or call conversations.join."
        ),
        "missing_scope": (
            "Token is missing a scope. For public channels you need channels:history; "
            "for private you also need groups:history (im:history, mpim:history for DMs)."
        ),
        "channel_not_found": (
            "Wrong channel ID, wrong workspace, or the channel is archived/inaccessible."
        ),
        "invalid_auth": "Token is invalid or revoked.",
        "token_revoked": "Token was revoked. Reinstall the app to get a new one.",
        "account_inactive": "The user who owns this token is deactivated.",
    }
    hint = hints.get(err)
    if hint:
        print(f"hint: {hint}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
