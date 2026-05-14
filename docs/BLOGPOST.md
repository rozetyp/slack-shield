# How to back-fill Slack history under 1 request per minute

*A practical write-up for anyone whose unlisted Slack app is now stuck
behind Slack's new rate limit.*

---

On **May 29, 2025**, Slack cut `conversations.history` and
`conversations.replies` to **1 request per minute** with a maximum of
**15 messages per request** for "commercially-distributed" apps that
aren't listed on the Slack Marketplace. New installs were affected
immediately. Existing installs follow on **March 3, 2026**.

If you do the math, that's **21,600 messages per channel per day** at
peak. A busy customer channel with half a million messages becomes a
**23-day backfill** at full theoretical throughput, and the moment any
window paginates you're adding extra minutes. There's no clever way
around that ceiling; Slack means it.

So what do you actually *do* on the day a customer signs up and you
need their year of history?

This is the playbook I ended up with, plus a small Python tool to do
the boring parts.

## 1. Figure out if you're actually affected

Three things determine whether the 1 rpm limit applies to *you*:

| You are…                                  | Limit                |
| ----------------------------------------- | -------------------- |
| Listed on the Slack Marketplace           | Tier-3 (older limits) |
| An internal customer-built app            | Tier-3                |
| An unlisted, commercially-distributed app | **1 rpm, 15 msgs**   |
| Using a user token (`xoxp-…`)             | User rate-limit class (different) |

The change is targeted at the third bucket: products that ship to
other workspaces but haven't been through Marketplace review. If you
build internal tooling for your own team, nothing changed for you. If
your customers paste their own personal token to run a script locally,
same thing.

If you're in the third bucket, the only real fix is to get your app
listed on the Marketplace. Everything below is for the time between
"customer signed up" and "Marketplace review finished," which can be
weeks.

## 2. Stop trying to fight the limit

Every retry pattern that "works" against normal Slack limits breaks
here:

- **Exponential backoff** is useless when the next request is also
  going to be 429. You're not bursty-overloading the server; you're
  just over the rate.
- **Bigger `limit=` values** are clamped to 15. The parameter is
  silently capped server-side.
- **Multiple bot tokens** don't help; the limit is per-workspace,
  per-method, not per-token.
- **Parallel calls** make it strictly worse. You burn the per-minute
  budget on a race instead of progress.

The only thing that works: one request, then wait, then one more
request. Forever. With state.

## 3. Build the backfill the way Slack tells you to

The behaviors you actually need:

**(a) Time-slice the channel.** Pull `oldest=W_start&latest=W_end`
for a window (default 7 days), then move to the next window. Each
window is independent state, so a Ctrl-C in the middle doesn't lose
your place. Adjust window size adaptively: if a window paginates
more than ~3 pages, halve all future windows.

**(b) Honor `Retry-After` exactly.** When Slack returns 429, the
header tells you how long to wait. Take that value, not a fixed
60s. The penalty is sometimes longer than 60s and you don't want
to spin against it.

**(c) Checkpoint after every successful page.** Not every window,
not every minute. Every page. Write to a temp file, rename. If your
process gets OOM-killed at message 1,247,832 of 1,500,000, you should
lose at most one page of work.

**(d) Don't fan out across channels.** Per-method, per-workspace
budgets are shared. Round-robin between channels inside the budget
instead of running them in parallel.

The whole thing fits in about 400 lines of Python.

## 4. Be honest about ETA

Customers ask "when will my data be ready" and the temptation is to
quote `total_messages / 900 messages-per-hour`. Don't. Instead:

```
floor_eta = remaining_windows × 60s
realistic_eta = floor_eta × (1 + observed_paginations_per_window)
```

Pad by 20% for `Retry-After` variance. Quote a range, not a number.

Most users accept "this will take 6 to 10 hours, you'll get an email"
because the alternative they were comparing against was "it doesn't
work at all."

## 5. The boring lessons

A few things I had to learn the hard way:

- **Bot membership.** `not_in_channel` is the most common failure on
  customer onboarding. You can't pull history from a private channel
  the bot isn't in. The fix is `conversations.join` for public, or
  asking the user to invite the bot.
- **Scope errors.** `channels:history` covers public; `groups:history`
  covers private. They're different scopes. Surfacing `missing_scope`
  with a specific scope name in the error message saves an hour of
  support per customer.
- **`since_ts` precision matters.** Slack timestamps are seconds with
  6-decimal-place subseconds. If you store them as floats and stringify
  them, you can lose the last digit. Keep them as strings end-to-end.
- **Output as JSONL, not JSON.** A 1M-message backfill in one JSON
  array is hard to stream, hard to resume into, and hard to inspect.
  One message per line; convert to CSV/Parquet when you're done.

## 6. The tool

I wrote [**slack-shield**](https://github.com/rozetyp/slack-shield)
to bundle these behaviors. It's a single `pip install`, zero infra,
runs on your machine. The CLI is the obvious shape:

```
$ slack-shield backfill --channel C0123456 --since 2024-01-01
14:02:11  window 1/22  2024-01-01 → 2024-01-08
14:02:11  done 1/22  msgs=14  eta≈21m
14:03:11  window 2/22  2024-01-08 → 2024-01-15
14:04:11  429: sleeping 47s as Slack requested
...
```

Output is a JSONL of raw Slack message payloads. Convert to CSV with
`slack-shield to-csv` afterward. State is a single JSON file you can
delete to start over, or keep around to resume.

It is not magic. There is no way to make the backfill faster than the
rate limit allows. It just removes the burden of writing the same
windowing + checkpointing + retry-after loop yourself.

## 7. The longer arc

The Slack change is the kind of API decision that quietly remakes
a small developer market. Tools that assumed cheap programmatic
access to message history have to either get on the Marketplace, get
on user tokens, or eat the new ceiling. Most will pick the last one
and then quietly migrate to the first.

If you're shipping a product against the new limits, two pragmatic
suggestions:

1. **Frame the wait as part of the UX.** "We're indexing your Slack,
   this takes a few hours, we'll email you" is a fine flow. "Connect
   and search" is a lie.
2. **Set a calendar reminder for your Marketplace application.** The
   wait between submit and approval is itself a backfill problem.
   The earlier you start, the less time you spend living with 1 rpm.

That's it. Source is on GitHub, MIT-licensed. If you find a corner I
missed, the issues page is open.

---

*If you found this useful or want to argue with any of it, the repo
is at [github.com/rozetyp/slack-shield](https://github.com/rozetyp/slack-shield).*
