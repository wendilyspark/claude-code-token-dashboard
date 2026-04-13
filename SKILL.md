---
name: token-dashboard
description: Generate and view a Claude Code token usage dashboard. Use when the user asks about token usage, API costs, spending, how much they've used, or wants to see their usage dashboard.
---

# Token Dashboard Skill

When this skill is triggered, follow the steps below. Do not ask the user anything first.

## Steps to Execute

1. Check if the plan config exists:
```bash
cat ~/.claude/skills/token-dashboard/config.json 2>/dev/null
```

2. If config is missing (file not found or empty), run setup interactively **in the foreground** (not background — setup requires user input):
```bash
python3 ~/.claude/skills/token-dashboard/generate_dashboard.py --setup
```
Wait for the user to complete plan selection before continuing.

3. Check if the server is already running:
```bash
lsof -i :8765 | grep LISTEN
```

4. If NOT running, start it in the background:
```bash
nohup python3 ~/.claude/skills/token-dashboard/generate_dashboard.py --no-open > /tmp/token-dashboard.log 2>&1 &
sleep 2
```

5. Open the dashboard in the browser:
```bash
open http://localhost:8765
```

6. Tell the user:
> Dashboard is live at http://localhost:8765 — Cmd+R refreshes data in real time. The server runs in the background until you restart your machine or kill it manually.

## Changing Plans

If the user says they have changed their Claude subscription plan, run setup again:
```bash
pkill -f "generate_dashboard.py" 2>/dev/null
python3 ~/.claude/skills/token-dashboard/generate_dashboard.py --setup
```
Then restart the server (step 4 above) so the new plan takes effect.

## Important: Never Generate Static Files

Always work with the live server at `http://localhost:8765`. **Never use `--output`** to generate a static `dashboard.html` — the server regenerates HTML on every Cmd+R automatically.

## Options

- `--days N` — Load N days of data (default: 7)
- `--port N` — Use a different port (default: 8765)
- `--setup` — Re-run plan selection (saves to config.json, then exits)
- `--plan pro|max5x|max20x` — Override plan for this run only (does not save)

## What It Shows

- **Overview KPIs**: Total tokens, tokens/day, sessions, tokens/session, cache savings
- **Token Usage Over Time**: Interactive bar chart with 1D/3D/7D/30D/1Y views + custom date range
- **Usage Intensity**: Rolling 5-hour window chart, scaled relative to your plan's session budget
- **Breakdown**: Cost by model, task type distribution
- **Events**: Spike hours, context compaction events (with modal details)
- **Sessions Table**: Sortable by cost, expandable detail rows with top 5 most expensive requests per session
- **Terminology Glossary**: Definitions of key terms

## Data Source

Reads JSONL files from `~/.claude/projects/` — standard Claude Code usage logs. No external APIs needed.

## Output

Always serves from `http://localhost:8765`. Cmd+R fetches live data on every reload. The script never creates dated copies — one server, one URL.
