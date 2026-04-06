---
name: token-dashboard
description: Generate and view a Claude Code token usage dashboard. Use when the user asks about token usage, API costs, spending, how much they've used, or wants to see their usage dashboard.
---

# Token Dashboard Skill

Generate a standalone HTML dashboard showing Claude Code token usage, costs, cache savings, session breakdowns, and time-series charts.

## When to Use

Trigger on any of these:
- "token usage", "token dashboard", "usage dashboard"
- "how much have I spent", "API costs", "spending"
- "show my usage", "token stats", "usage report"

## How to Run

```bash
python3 ~/.claude/skills/token-dashboard/generate_dashboard.py --days 7
```

### Options
- `--days N` — Load N days of data (default: 7). The UI can filter further.
- `--no-open` — Generate without auto-opening in browser.

The script always overwrites the same `dashboard.html` file — never creates new or dated copies.

## What It Shows

- **Overview KPIs**: Total cost, cache savings, total tokens, session count, avg cost/session
- **Token Usage Over Time**: Interactive bar chart with 1D/3D/7D/30D/1Y views + custom date range
- **Breakdown**: Cost by model, task type distribution
- **Events**: Spike hours, context compaction events (with modal details)
- **Sessions Table**: Sortable by cost, expandable detail rows with top 5 most expensive requests per session
- **Terminology Glossary**: Definitions of key terms (session, request, tokens, savings, spike, compact, session task)

## Data Source

Reads JSONL files from `~/.claude/projects/` — the standard Claude Code usage logs. No external APIs needed.
