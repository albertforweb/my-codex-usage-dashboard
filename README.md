# My Usage

A local Codex plugin with an interactive Grafana-style view of model, reasoning effort, and token usage from the current user's Codex session logs.

## Data source and scope

The scanner reads JSONL files under `~/.codex/sessions` and `~/.codex/archived_sessions`, then stores only the extracted metadata in `~/.codex/my-usage/usage.sqlite3`. It tracks file modification time and size, so later scans process only new or changed session files and remove records for deleted files. Conversation message contents are skipped and never stored. The scanner performs no network requests. The database directory is private to the current user.

Token fields come from the latest cumulative `turn_token_usage` snapshot per turn when available. For older logs without those snapshots, it uses response usage records or the latest `last_token_usage` snapshot per turn. Sessions without token counters show no token total. The dashboard displays token coverage so missing values are not mistaken for zero usage.

## Run locally

The MCP server uses Node.js built-ins and the macOS system Python SQLite library at runtime. The `usage.scan` tool updates the index; a LaunchAgent also runs it daily at 3:00 AM. Each successful scan updates a metadata-only snapshot embedded in `app/dashboard.html`, so opening that file directly displays the latest daily snapshot. The Codex dashboard reads live indexed data when opened and checks for new or changed sessions.

```sh
node app/server.js
```

To rebuild the bundled MCP Apps client after changing `app/client.js`, run `npm ci && npm run build:app`.

The plugin is installed from the local `my-usage-local` Codex marketplace and exposes the `usage.dashboard` and `usage.scan` MCP tools plus a global dashboard entrypoint. Its source stays at `~/.codex/plugins/my-usage-plugin`.
