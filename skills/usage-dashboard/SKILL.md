---
name: usage-dashboard
description: Open the local Codex usage dashboard and summarize model, reasoning effort, and recorded token usage across sessions.
---

# Codex usage dashboard

When the user asks to see or analyze their Codex model usage, call the `usage.dashboard` tool. It reads local Codex session metadata and returns an interactive dashboard with model and effort counts, recorded token usage, filters, and session details.

The plugin keeps an incremental SQLite index at `~/.codex/my-usage/usage.sqlite3`. The daily local scan and dashboard update tool are named `usage.scan`; call it when the user asks to refresh or rescan. Opening the dashboard checks for new or changed files and updates the index incrementally.

The scanner stores only session IDs, timestamps, project paths, source, model and effort counts, and token counters. It never stores conversation text. Token totals reflect counters present in the local logs; show the coverage count when discussing them.
