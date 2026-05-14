# Changelog

All notable changes are documented here.

## [0.1.0] - 2026-05-14

### Added
- Async rate limiter keyed by `(workspace, route)` that enforces a minimum
  interval and honors `Retry-After` from Slack 429s.
- Slack client with `auth.test`, `conversations.history`, `conversations.replies`.
- Resumable time-windowed backfill with adaptive shrinking on heavy pagination.
- JSON checkpoint after every successful page; JSONL append for messages.
- CLI: `slack-shield backfill`, `slack-shield to-csv`, `slack-shield auth-test`,
  with `--plan` for a dry-run estimate.
