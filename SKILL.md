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

5. **Always confirm the subscription plan AND the reset time** before opening the dashboard. Both are required every time the skill is invoked. Ask them together (a single AskUserQuestion with two questions is ideal), reading the **current saved plan from config.json** and naming it explicitly so the user has an easy option to change it.

   **5a — Confirm subscription plan.** Read the currently-saved plan from config and state it by name (`pro` → Pro $20/mo · `max5x` → Max 5× $100/mo · `max20x` → Max 20× $200/mo). Offer keep-or-switch. Phrase it like:
   > Still on **Max 5× ($100/mo)**? (Plan sets the 5-hour window budget the intensity chart & heatmap scale against.) — Keep Max 5× · Switch to Pro · Switch to Max 20×

   - If the user **keeps** the current plan, leave the plan field untouched.
   - If the user **switches**, write the new plan id and restart the server (a plan change only takes effect on restart):
     ```bash
     python3 -c "
     import json, pathlib
     PLAN = '<pro|max5x|max20x>'
     p = pathlib.Path.home() / '.claude/skills/token-dashboard/config.json'
     cfg = json.loads(p.read_text()); cfg['plan'] = PLAN; p.write_text(json.dumps(cfg, indent=2))
     print('Plan set to', PLAN)
     "
     pkill -f "generate_dashboard.py" 2>/dev/null
     nohup python3 ~/.claude/skills/token-dashboard/generate_dashboard.py --no-open > /tmp/token-dashboard.log 2>&1 &
     sleep 2
     ```

   **5b — Confirm reset time.** Ask:
   > How many minutes until your next 5-hour window resets? (Check the countdown shown in your Claude UI — e.g. "50 min")

   Once they reply with a number of minutes (N), save it to config.json so the dashboard's windows back-derive from Claude's real reset clock:
   ```bash
   python3 -c "
   from datetime import datetime, timedelta, timezone
   import json, pathlib
   N = <minutes_from_user>
   p = pathlib.Path.home() / '.claude/skills/token-dashboard/config.json'
   cfg = json.loads(p.read_text())
   now = datetime.now(timezone.utc)
   cfg['next_reset_at'] = (now + timedelta(minutes=N)).isoformat()
   cfg['next_reset_set_at'] = now.isoformat()
   p.write_text(json.dumps(cfg, indent=2))
   print('Anchored. Reset at', (now + timedelta(minutes=N)).astimezone().strftime('%H:%M local'))
   "
   ```

   **5c — If the user skips either question.**
   - **Skips the plan** → keep the previously-saved plan in config (do nothing).
   - **Skips the reset time** → make the best guess from the standard Claude 5-hour window rule (windows are fixed 5-hour blocks anchored to the first message of the window). If the existing `next_reset_set_at` anchor is still recent, leave it in place; otherwise clear the anchor so the dashboard falls back to gap-based derivation:
     ```bash
     python3 -c "
     import json, pathlib
     p = pathlib.Path.home() / '.claude/skills/token-dashboard/config.json'
     cfg = json.loads(p.read_text())
     cfg.pop('next_reset_at', None); cfg.pop('next_reset_set_at', None)
     p.write_text(json.dumps(cfg, indent=2)); print('Anchor cleared — using gap-based derivation')
     "
     ```
   - **Whenever the user skips, briefly warn (1–2 lines, no long explanation)** which metrics may be off:
     - *Skipped reset time* → Usage Intensity 5-hour window boundaries & cumulative-budget % may be misaligned (windows guessed, not anchored to your real reset clock).
     - *Plan left unchanged but actually changed* → every "% of plan cap" figure (intensity chart, heatmap colors, rate-limit warnings) scales against the wrong budget.

6. Open the dashboard in the browser:
```bash
open http://localhost:8765
```

7. Tell the user:
> Dashboard is live at http://localhost:8765 — anchored to Claude's reset clock (counts down live above the intensity chart). Cmd+R refreshes data in real time. You can re-set the reset time directly on the page using the "Resets in __ min" input if Claude UI shifts.

## Changing Plans

The plan is now confirmed on every run in step 5a, so a mid-session change is usually handled there. If the user explicitly says they changed plans outside that flow, you can also re-run full setup:
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
