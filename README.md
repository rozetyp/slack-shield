# slack-shield

Pull Slack channel history from an unlisted/non-Marketplace app without
fighting the 1 request/minute rate limit by hand.

Since **May 29, 2025** Slack throttles `conversations.history` and
`conversations.replies` to **1 request/minute, max 15 messages per request**
for any commercially-distributed app not listed in the Slack Marketplace.
Existing installs switch over on **March 3, 2026**. This is a small Python
library + CLI that does the only sane thing under that constraint: a
time-windowed, checkpointed, resumable backfill that honors `Retry-After`
and survives reboots.

[See if your Slack app is affected →](docs/affected.html)

## What it does

- One request per minute per `(workspace, route)`, exactly what Slack asks.
- Honors `Retry-After` on every 429 (no fixed 60s sleep).
- Walks the channel in time windows (default 7 days), paginates inside each
  window with `limit=15`, halves future windows when pagination gets heavy.
- Checkpoints state to a local JSON file after **every successful page**, so
  Ctrl-C / OOM / reboot resumes where it left off.
- Writes messages as JSONL (one raw Slack message per line). Convert to CSV
  with `slack-shield to-csv` when you're done.
- Zero infrastructure. No Redis, no Postgres, no Docker. One binary, one
  state file, one output file.

## What it isn't

- A hosted service. You run it locally.
- A way around the rate limit. There is no way around the rate limit; this
  just makes the wait survivable.
- A replacement for [slackdump](https://github.com/rusq/slackdump) if your
  use case allows a **user token**. Slackdump is faster and more featureful
  for that case. `slack-shield` is for the case where you're stuck with a
  bot/app token under the new limits.

## Install

```bash
pip install slack-shield
```

Requires Python 3.10+. The only runtime dependency is `httpx`.

## Quick start

```bash
export SLACK_TOKEN=xoxb-...

# verify the token works
slack-shield auth-test

# see how long the job will take before starting (no API calls)
slack-shield backfill --channel C01234ABCD --since 2024-01-01 --plan

# run it
slack-shield backfill --channel C01234ABCD --since 2024-01-01 --until 2025-01-01

# convert to CSV when finished
slack-shield to-csv C01234ABCD.jsonl out.csv
```

If you Ctrl-C and re-run the same `backfill` command, it resumes from the
last checkpointed page. No flags needed.

## What you'll see

```
$ slack-shield backfill --channel C0123456 --since 2024-01-01 --until 2024-06-01
14:02:11  window 1/22  2024-01-01 → 2024-01-08
14:02:11  done 1/22  msgs=14  eta≈21m
14:03:11  window 2/22  2024-01-08 → 2024-01-15
14:03:11  done 2/22  msgs=29  eta≈20m
14:04:11  window 3/22  2024-01-15 → 2024-01-22
14:04:11  429: sleeping 47s as Slack requested
14:04:58  done 3/22  msgs=44  eta≈19m
...
```

## How long will it take?

Worst case: **1 window = 1 minute**. So a 2-year backfill at 7-day windows
is ~104 windows = ~1.7 hours of wall clock. Heavy channels paginate inside
a window (each extra page = +1 minute). A busy 100k-message channel can
take a day. The `--plan` flag prints a floor estimate before you start.

## As a library

```python
import asyncio
from pathlib import Path
from slack_shield import SlackClient, RateLimiter, Backfill

async def main():
    limiter = RateLimiter(interval_seconds=60.0)
    async with SlackClient(token="xoxb-...") as client:
        bf = Backfill(
            client=client,
            limiter=limiter,
            channel="C01234ABCD",
            since_ts=1704067200,             # 2024-01-01
            until_ts=1735689600,             # 2025-01-01
            state_path=Path(".state.json"),
            messages_path=Path("out.jsonl"),
        )
        async for window in bf.run():
            print(bf.progress())

asyncio.run(main())
```

## Token scopes you'll need

Bot tokens need at least one of these, depending on channel type:

| Channel kind         | Scope             |
| -------------------- | ----------------- |
| Public channels      | `channels:history`|
| Private channels     | `groups:history`  |
| Direct messages      | `im:history`      |
| Group DMs            | `mpim:history`    |

The bot also needs to be a member of the channel
(`/invite @your-bot` in Slack, or call `conversations.join`).

If you see `not_in_channel` or `missing_scope`, `slack-shield` prints a
short hint with the fix.

## "Wait, isn't there a way around this?"

For most people, no. The exceptions worth knowing about:

- **Slack Marketplace apps** keep the higher Tier-3 limits. If your app is
  productized and you can list it, do that.
- **User tokens** aren't restricted the same way. If your use case lets the
  end user paste their own personal token (e.g. local archives,
  self-service exports), use [slackdump](https://github.com/rusq/slackdump);
  it's been doing this for years.
- **Enterprise Grid Discovery API** exists if your customer is a Grid org.
  Different commercial path.
- **Workspace admin ZIP exports** work for one-off offline archives but
  not for ongoing programmatic access.

This tool exists for the case where none of those apply: you ship a
product that uses a bot/app token to pull Slack history on behalf of
other workspaces, and you can't list on Marketplace yet.

## Status

`v0.1.0`. Used in earnest. Public API and CLI flags may change between
0.x releases. Pin if you depend on it.

## License

MIT
