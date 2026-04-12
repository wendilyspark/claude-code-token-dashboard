---
name: token-dashboard
description: Generate and view a Claude Code token usage dashboard. Use when the user asks about token usage, API costs, spending, how much they've used, or wants to see their usage dashboard.
---

# Token Dashboard Skill

When this skill is triggered, immediately run the dashboard server and open it in the browser. Do not ask the user anything first.

## Steps to Execute

1. Check if the server is already running:
```bash
lsof -i :8765 | grep LISTEN
```

2. If NOT running, start it in the background:
```bash
nohup python3 ~/.claude/skills/token-dashboard/generate_dashboard.py --no-open > /tmp/token-dashboard.log 2>&1 &
sleep 2
```

3. Open the dashboard in the browser:
```bash
open http://localhost:8765
```

4. Tell the user:
> Dashboard is live at http://localhost:8765 — Cmd+R refreshes data in real time. The server runs in the background until you restart your machine or kill it manually.

## Important: Never Generate Static Files

Always work with the live server at `http://localhost:8765`. **Never use `--output`** to generate a static `dashboard.html` — the server regenerates HTML on every Cmd+R automatically. When debugging or verifying changes, reload `http://localhost:8765` in the browser (or use the preview server pointed at that URL), not a static file.

## Options

- `--days N` — Load N days of data (default: 7)
- `--port N` — Use a different port (default: 8765)

## What It Shows

- **Overview KPIs**: Total tokens, tokens/day, sessions, tokens/session, cache savings
- **Token Usage Over Time**: Interactive bar chart with 1D/3D/7D/30D/1Y views + custom date range
- **Breakdown**: Cost by model, task type distribution
- **Events**: Spike hours, context compaction events (with modal details)
- **Sessions Table**: Sortable by cost, expandable detail rows with top 5 most expensive requests per session
- **Terminology Glossary**: Definitions of key terms

## Data Source

Reads JSONL files from `~/.claude/projects/` — standard Claude Code usage logs. No external APIs needed.

## Output

Always serves from `http://localhost:8765`. Cmd+R fetches live data on every reload. The script never creates dated copies — one server, one URL.
