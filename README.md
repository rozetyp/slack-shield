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

**Project page:** https://rozetyp.github.io/slack-shield/  
[See if your Slack app is affected →](https://rozetyp.github.io/slack-shield/affected.html)

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

Real captured output from a 3-window backfill against a test workspace.
Note the exactly-60-second gaps between HTTP requests, which is the
limiter doing its job.

```
$ slack-shield backfill --channel C0B3A580679 --since 2026-05-06 --until 2026-05-15 --window-days 3
09:53:31  window 1/3  2026-05-06 to 2026-05-09
09:53:31  HTTP Request: GET https://slack.com/api/conversations.history... "200 OK"
09:53:31  done 1/3  msgs=0  eta=2m
09:53:31  window 2/3  2026-05-09 to 2026-05-12
09:54:31  HTTP Request: GET https://slack.com/api/conversations.history... "200 OK"
09:54:31  done 2/3  msgs=0  eta=1m
09:54:31  window 3/3  2026-05-12 to 2026-05-15
09:55:31  HTTP Request: GET https://slack.com/api/conversations.history... "200 OK"
09:55:31  done 3/3  msgs=7  eta=0m

finished. 7 messages -> social.jsonl
```

When Slack does return a 429, you'll see:

```
14:04:11  429: sleeping 47s as Slack requested
```

The next request fires exactly when `Retry-After` says it can.

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

## Why slack-shield (vs the alternatives)

The Slack-history-out market is fragmented. Use the right tool for the
job; this one is for a specific niche.

| Your situation | Use this |
| -------------- | -------- |
| Building a Slack app, just want sane retries on 429 | The official [`slack_sdk`](https://github.com/slackapi/python-slack-sdk) (its `RetryHandler` covers Tier-3 cases) |
| Personal/local archive of your own workspace, you have admin access | [`slackdump`](https://github.com/rusq/slackdump). It uses a user token and isn't rate-limited the same way. Faster. |
| Full Slack-to-Teams/Mattermost migration with files, channel mapping, threading | Managed services: CloudFuze, Cloudiway, 21b. Project-priced, $5k+ |
| Want Slack data in a data warehouse for analytics | An ETL platform: Airbyte (OSS), Fivetran (paid). They hit the same rate limit but solve "into BigQuery" for you. |
| Compliance archive / eDiscovery / legal hold | Pagefreezer, Onna, Archive360, ViewExport |
| Enterprise AI search over Slack | Glean (Tier-1 only, ~$50/user/month) |
| **You ship a product with a bot/app token, an unlisted Slack app, and need to back-fill a customer's history under the new 1 rpm limit, with state and resume, and zero infrastructure** | **slack-shield** |

The last row is the niche. The official SDKs retry but don't slice
windows or checkpoint. Slackdump needs a user token, which you can't
ask paying customers to paste. Migration vendors charge five figures
for a turnkey project. ETL platforms cost monthly and aren't designed
for one-shot per-customer back-fills. slack-shield is one `pip
install`, runs on a laptop, and does the boring middle: walk the API
slowly, save your place, resume on crash.

If your situation matches one of the other rows, save yourself the
trouble and use that tool. If it matches the last row, keep reading.

### What if I could just list on the Marketplace?

Do it. That restores the older Tier-3 limits and removes the whole
problem. slack-shield is for the period before that happens (the
review can take weeks), or for cases where Marketplace listing isn't
realistic.

## Status

`v0.1.0`. Verified against a live Slack workspace on 2026-05-14:
authentication, the 1 req/min pacing between requests, cursor
pagination across pages, multi-window walks, state checkpoints, and
resume from an interrupted run all behaved as documented. 28 pytest
tests cover the components in isolation, including the 429 +
`Retry-After` path and the adaptive-shrink path.

Public API and CLI flags may change between 0.x releases. Pin if you
depend on it.

## License

MIT
