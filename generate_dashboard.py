#!/usr/bin/env python3
"""
Claude Code Token Usage Dashboard Generator
Usage: python generate_dashboard.py [--days N] [--port N] [--output path] [--no-open]
"""
import argparse
import http.server
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ─── Pricing per 1M tokens ──────────────────────────────────────────────────
PRICING = {
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00, "cache_write": 18.75, "cache_read": 1.50},
    "claude-opus-4-5":           {"input": 15.00, "output": 75.00, "cache_write": 18.75, "cache_read": 1.50},
    "claude-sonnet-4-6":         {"input":  3.00, "output": 15.00, "cache_write":  3.75, "cache_read": 0.30},
    "claude-sonnet-4-5":         {"input":  3.00, "output": 15.00, "cache_write":  3.75, "cache_read": 0.30},
    "claude-haiku-4-5":          {"input":  0.80, "output":  4.00, "cache_write":  1.00, "cache_read": 0.08},
    "claude-haiku-4-5-20251001": {"input":  0.80, "output":  4.00, "cache_write":  1.00, "cache_read": 0.08},
}
DEFAULT_PRICE = {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30}

TASK_KEYWORDS = {
    "planning":   ["plan", "设计", "架构", "architecture", "how to", "strategy", "approach", "design"],
    "debugging":  ["error", "fix", "bug", "fail", "broken", "crash", "exception", "wrong", "issue", "problem", "debug"],
    "coding":     ["write", "create", "implement", "build", "add", "generate", "make", "code", "function", "class", "feature"],
    "refactor":   ["refactor", "clean", "improve", "optimize", "restructure", "rename", "reorganize", "simplify"],
    "research":   ["explain", "what is", "how does", "search", "find", "show me", "list", "tell me", "help me understand"],
    "canvas":     ["canvas", ".canvas", "obsidian", "node", "edge"],
    "document":   ["docx", "pdf", "pptx", "xlsx", "word", "slides", "presentation", "document"],
    "dashboard":  ["dashboard", "chart", "graph", "metric", "analytics", "visualization"],
}

MODEL_COLORS = {
    "claude-opus-4-6":           "#c678dd",
    "claude-opus-4-5":           "#c678dd",
    "claude-sonnet-4-6":         "#61afef",
    "claude-sonnet-4-5":         "#61afef",
    "claude-haiku-4-5":          "#98c379",
    "claude-haiku-4-5-20251001": "#98c379",
    "unknown":                   "#abb2bf",
}

# ─── Helpers ─────────────────────────────────────────────────────────────────

def calc_cost(model: str, usage: dict) -> float:
    p = PRICING.get(model, DEFAULT_PRICE)
    cost = (
        usage.get("input_tokens", 0) * p["input"] / 1_000_000
        + usage.get("output_tokens", 0) * p["output"] / 1_000_000
        + usage.get("cache_creation_input_tokens", 0) * p["cache_write"] / 1_000_000
        + usage.get("cache_read_input_tokens", 0) * p["cache_read"] / 1_000_000
    )
    return cost

def cache_savings(model: str, usage: dict) -> float:
    """Cost saved because cache_read was cheaper than fresh input."""
    p = PRICING.get(model, DEFAULT_PRICE)
    saved = usage.get("cache_read_input_tokens", 0) * (p["input"] - p["cache_read"]) / 1_000_000
    return saved

def classify_task(text: str) -> str:
    if not text:
        return "other"
    lower = text.lower()
    for category, keywords in TASK_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return category
    return "other"

def short_model(model: str) -> str:
    return model.replace("claude-", "").replace("-20251001", "")

def parse_ts(ts_str: str):
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None

def extract_text(content) -> str:
    """Extract plain text from message content (string or list of blocks)."""
    if isinstance(content, str):
        return content[:500]
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    parts.append(f"[tool: {block.get('name','')}]")
        return " ".join(parts)[:500]
    return ""

# ─── Session title loader ─────────────────────────────────────────────────────

def load_session_titles() -> dict:
    """Read claude-code-sessions JSON files → {cliSessionId: title}."""
    titles = {}
    base = Path("/Users") / Path.home().name / "Library" / "Application Support" / "Claude" / "claude-code-sessions"
    if not base.exists():
        return titles
    for dev_dir in base.iterdir():
        if not dev_dir.is_dir():
            continue
        for app_dir in dev_dir.iterdir():
            if not app_dir.is_dir():
                continue
            for jf in app_dir.glob("local_*.json"):
                try:
                    data = json.loads(jf.read_text())
                    cli_id = data.get("cliSessionId")
                    title = data.get("title")
                    if cli_id and title:
                        titles[cli_id] = title
                except Exception:
                    pass
    return titles

# ─── Main parser ─────────────────────────────────────────────────────────────

def parse_projects(days: int):
    claude_dir = Path.home() / ".claude" / "projects"
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # session_id → metadata
    sessions: dict[str, dict] = defaultdict(lambda: {
        "session_id": "",
        "slug": "",
        "session_title": "",
        "project": "",
        "task_description": "",
        "task_type": "other",
        "model": "unknown",
        "start_time": None,
        "end_time": None,
        "messages": [],
        "user_messages": [],        # all user turns: [{ts, text, tools}]
        "_last_user_text": "",      # temp: most recent user message text
        "_last_user_ts": "",        # temp: most recent user message ts
        "compact_events": [],
        "is_subagent": False,
        "agent_id": None,
        "parent_session_id": None,
    })

    # hourly buckets: hour_str → {tokens, cost, models}
    hourly: dict[str, dict] = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "models": defaultdict(int)})

    all_jsonl = sorted(claude_dir.rglob("*.jsonl"))

    for jpath in all_jsonl:
        # Skip if file wasn't recently touched (quick filter)
        try:
            mtime = datetime.fromtimestamp(jpath.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff - timedelta(days=1):  # buffer: file may have older entries
                continue
        except Exception:
            continue

        is_subagent = "/subagents/" in str(jpath)
        # Extract agentId from filename for subagent files
        agent_id = None
        if is_subagent:
            m = re.search(r"agent-([a-f0-9]+)\.jsonl$", jpath.name)
            if m:
                agent_id = m.group(1)

        with open(jpath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts = parse_ts(entry.get("timestamp"))
                if not ts or ts < cutoff:
                    continue

                session_id = entry.get("sessionId", "")
                if not session_id:
                    continue

                s = sessions[session_id]
                s["session_id"] = session_id
                s["is_subagent"] = is_subagent
                if agent_id:
                    s["agent_id"] = agent_id
                if not s["project"]:
                    cwd = entry.get("cwd", "")
                    s["project"] = Path(cwd).name if cwd else "unknown"
                if not s["slug"] and entry.get("slug"):
                    s["slug"] = entry["slug"]

                entry_type = entry.get("type", "")
                subtype = entry.get("subtype", "")

                # ── Assistant message with usage ──────────────────────────
                if entry_type == "assistant":
                    msg = entry.get("message", {})
                    usage = msg.get("usage", {})
                    if not usage:
                        continue
                    model = msg.get("model", "unknown")

                    # Extract tool calls from assistant content
                    tools_called = []
                    for block in (msg.get("content") or []):
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            tools_called.append(block.get("name", ""))

                    total_tokens = (
                        usage.get("input_tokens", 0)
                        + usage.get("output_tokens", 0)
                        + usage.get("cache_creation_input_tokens", 0)
                        + usage.get("cache_read_input_tokens", 0)
                    )
                    cost = calc_cost(model, usage)
                    savings = cache_savings(model, usage)

                    record = {
                        "ts": ts.isoformat(),
                        "model": model,
                        "input_tokens": usage.get("input_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0),
                        "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
                        "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
                        "total_tokens": total_tokens,
                        "cost": cost,
                        "savings": savings,
                        "tools_called": tools_called,
                        "preceding_user": s["_last_user_text"],  # what user said before this call
                        "preceding_user_ts": s["_last_user_ts"],
                    }
                    s["messages"].append(record)
                    s["model"] = model  # last model wins (usually consistent)

                    # Update start/end times
                    if s["start_time"] is None or ts < parse_ts(s["start_time"]):
                        s["start_time"] = ts.isoformat()
                    if s["end_time"] is None or ts > parse_ts(s["end_time"]):
                        s["end_time"] = ts.isoformat()

                    # Hourly aggregation
                    hour_key = ts.strftime("%Y-%m-%dT%H:00")
                    hourly[hour_key]["tokens"] += total_tokens
                    hourly[hour_key]["cost"] += cost
                    hourly[hour_key]["models"][model] += total_tokens

                # ── User message → save all turns + update last ────────────
                elif entry_type == "user":
                    msg = entry.get("message", {})
                    if isinstance(msg, dict):
                        content = msg.get("content", "")
                        text = extract_text(content)
                        if text and len(text) > 5:
                            # Keep full text for first message, 400 chars for rest
                            if not s["task_description"]:
                                s["task_description"] = text[:600]
                                s["task_type"] = classify_task(text)
                            s["_last_user_text"] = text[:400]
                            s["_last_user_ts"] = ts.isoformat()
                            s["user_messages"].append({
                                "ts": ts.isoformat(),
                                "text": text[:400],
                            })

                # ── Compact boundary (context overflow event) ────────────
                elif entry_type == "system" and subtype == "compact_boundary":
                    meta = entry.get("compactMetadata", {})
                    compact_event = {
                        "ts": ts.isoformat(),
                        "pre_tokens": meta.get("preTokens", 0),
                        "trigger": meta.get("trigger", "auto"),
                        "session_id": session_id,
                        "preceding_user": s["_last_user_text"],  # what was happening
                    }
                    s["compact_events"].append(compact_event)

    return sessions, hourly


PLAN_CAPS = {
    "pro": 44_000_000,        # ~44M tokens per 5h window (unofficial estimate)
    "max5x": 210_000_000,     # ~210M tokens per 5h window (calibrated from actual usage data)
    "max20x": 440_000_000,    # ~440M tokens per 5h window (10× Pro estimate)
}


def compute_intensity(sessions: dict, cap: int):
    """Compute rolling 5-hour token usage windows and usage intensity."""
    # Collect all messages across all sessions with timestamps
    all_msgs = []
    for sid, s in sessions.items():
        for m in s["messages"]:
            ts = parse_ts(m["ts"])
            if ts:
                all_msgs.append({
                    "ts": ts,
                    "tokens": m["total_tokens"],
                    "session_id": sid,
                    "slug": s.get("slug") or sid[:8],
                    "task": s.get("task_description", "")[:120],
                    "model": m["model"],
                })
    if not all_msgs:
        return {"windows": [], "peaks": [], "heatmap": [[0]*24 for _ in range(7)], "cap": cap}

    all_msgs.sort(key=lambda x: x["ts"])

    # Bin into 15-minute slots
    slot_tokens = defaultdict(int)   # slot_key → total tokens
    slot_sessions = defaultdict(set) # slot_key → set of session_ids
    slot_details = defaultdict(list) # slot_key → list of {slug, task, tokens}

    for m in all_msgs:
        # Round down to 15-min slot
        t = m["ts"]
        minute_slot = (t.minute // 15) * 15
        slot_key = t.replace(minute=minute_slot, second=0, microsecond=0)
        slot_tokens[slot_key] += m["tokens"]
        slot_sessions[slot_key].add(m["session_id"])
        slot_details[slot_key].append({
            "slug": m["slug"],
            "task": m["task"],
            "tokens": m["tokens"],
            "model": m["model"],
        })

    if not slot_tokens:
        return {"windows": [], "peaks": [], "heatmap": [[0]*24 for _ in range(7)], "cap": cap}

    # Generate rolling 5h windows at 15-min steps
    sorted_slots = sorted(slot_tokens.keys())
    min_time = sorted_slots[0]
    max_time = sorted_slots[-1]
    window_dur = timedelta(hours=5)

    windows = []
    current = min_time
    while current <= max_time:
        # Sum tokens in [current - 5h, current]
        window_start = current - window_dur
        total = 0
        active_sessions = set()
        for sk, tk in slot_tokens.items():
            if window_start < sk <= current:
                total += tk
                active_sessions.update(slot_sessions[sk])
        pct = round(total / cap * 100, 1) if cap > 0 else 0
        windows.append({
            "ts": current.isoformat(),
            "tokens": total,
            "pct": pct,
            "sessions": len(active_sessions),
        })
        current += timedelta(minutes=15)

    # Identify peak windows (>85% or top by tokens)
    peaks = []
    for w in windows:
        if w["pct"] >= 85:
            # Find active sessions in this 5h window
            window_end = parse_ts(w["ts"])
            window_start = window_end - window_dur
            session_info = {}
            for sk, details in slot_details.items():
                if window_start < sk <= window_end:
                    for d in details:
                        if d["slug"] not in session_info:
                            session_info[d["slug"]] = {"task": d["task"], "tokens": 0, "model": d["model"]}
                        session_info[d["slug"]]["tokens"] += d["tokens"]
            peaks.append({
                "ts": w["ts"],
                "tokens": w["tokens"],
                "pct": w["pct"],
                "sessions": [{"slug": k, **v} for k, v in sorted(session_info.items(), key=lambda x: x[1]["tokens"], reverse=True)[:5]],
            })
    # Deduplicate peaks (keep only if >1h apart)
    deduped_peaks = []
    for p in sorted(peaks, key=lambda x: x["pct"], reverse=True):
        pt = parse_ts(p["ts"])
        if not any(abs((pt - parse_ts(dp["ts"])).total_seconds()) < 3600 for dp in deduped_peaks):
            deduped_peaks.append(p)
    deduped_peaks = deduped_peaks[:10]

    # Heatmap: 7 days × 24 hours
    heatmap = [[0]*24 for _ in range(7)]
    for m in all_msgs:
        dow = m["ts"].weekday()  # 0=Mon, 6=Sun
        hour = m["ts"].hour
        heatmap[dow][hour] += m["tokens"]

    return {
        "windows": windows,
        "peaks": deduped_peaks,
        "heatmap": heatmap,
        "cap": cap,
    }


def aggregate(sessions: dict, hourly: dict, days: int = 7, cap: int = 0):
    """Compute final aggregates for the dashboard."""
    session_titles = load_session_titles()   # cliSessionId → human title

    # Per-session summary
    session_list = []
    for sid, s in sessions.items():
        if not s["messages"]:
            continue
        total_tokens = sum(m["total_tokens"] for m in s["messages"])
        total_cost = sum(m["cost"] for m in s["messages"])
        total_savings = sum(m["savings"] for m in s["messages"])
        input_tokens = sum(m["input_tokens"] for m in s["messages"])
        output_tokens = sum(m["output_tokens"] for m in s["messages"])
        cache_read = sum(m["cache_read_tokens"] for m in s["messages"])
        cache_write = sum(m["cache_creation_tokens"] for m in s["messages"])
        request_count = len(s["messages"])

        # Duration
        start = parse_ts(s["start_time"])
        end = parse_ts(s["end_time"])
        duration_min = round((end - start).total_seconds() / 60, 1) if start and end else 0

        # Top 5 most expensive single requests (with context)
        top_requests = sorted(s["messages"], key=lambda m: m["total_tokens"], reverse=True)[:5]
        top_requests_clean = [{
            "ts": r["ts"],
            "model": r["model"],
            "total_tokens": r["total_tokens"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
            "cache_read_tokens": r["cache_read_tokens"],
            "cost": round(r["cost"], 4),
            "tools_called": r.get("tools_called", [])[:8],
            "preceding_user": r.get("preceding_user", "")[:300],
        } for r in top_requests]

        session_list.append({
            "session_id": sid[:8],
            "slug": s["slug"] or sid[:8],
            "session_title": session_titles.get(sid, ""),
            "project": s["project"],
            "task_description": s["task_description"] or "(no description)",
            "task_type": s["task_type"],
            "model": s["model"],
            "is_subagent": s["is_subagent"],
            "start_time": s["start_time"],
            "end_time": s["end_time"],
            "duration_min": duration_min,
            "request_count": request_count,
            "total_tokens": total_tokens,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "total_cost": round(total_cost, 4),
            "total_savings": round(total_savings, 4),
            "compact_events": s["compact_events"],
            "top_requests": top_requests_clean,
            "conversation_preview": s["user_messages"][:8],  # first 8 user turns
        })

    session_list.sort(key=lambda x: x["total_cost"], reverse=True)

    # Model aggregation
    model_totals: dict[str, dict] = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "requests": 0})
    for s in sessions.values():
        for m in s["messages"]:
            model_totals[m["model"]]["tokens"] += m["total_tokens"]
            model_totals[m["model"]]["cost"] += m["cost"]
            model_totals[m["model"]]["requests"] += 1

    # Task type aggregation
    task_totals: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "sessions": 0})
    for sess in session_list:
        t = sess["task_type"]
        task_totals[t]["cost"] += sess["total_cost"]
        task_totals[t]["sessions"] += 1

    # Compact events aggregated
    all_compact = []
    for s in sessions.values():
        for c in s["compact_events"]:
            all_compact.append({
                **c,
                "session_slug": s.get("slug") or c["session_id"][:8],
                "task": s.get("task_description", "")[:200],
                "preceding_user": c.get("preceding_user", "")[:300],
            })
    all_compact.sort(key=lambda x: x["ts"], reverse=True)

    # Hourly time-series (sorted by hour)
    hourly_series = []
    for hour_key in sorted(hourly.keys()):
        h = hourly[hour_key]
        hourly_series.append({
            "hour": hour_key,
            "tokens": h["tokens"],
            "cost": round(h["cost"], 4),
            "models": dict(h["models"]),
        })

    # Spike detection: flag hours where tokens > mean + 2*std
    if len(hourly_series) >= 3:
        token_vals = [h["tokens"] for h in hourly_series]
        mean = sum(token_vals) / len(token_vals)
        variance = sum((x - mean) ** 2 for x in token_vals) / len(token_vals)
        std = variance ** 0.5
        threshold = mean + 2 * std
        for h in hourly_series:
            h["is_spike"] = h["tokens"] > threshold and h["tokens"] > 10000
    else:
        for h in hourly_series:
            h["is_spike"] = False

    # Top KPIs
    grand_total_cost = sum(h["cost"] for h in hourly_series)
    grand_total_tokens = sum(h["tokens"] for h in hourly_series)
    grand_total_savings = sum(sess["total_savings"] for sess in session_list)
    unique_sessions = len([s for s in session_list if not s["is_subagent"]])
    total_requests = sum(sess["request_count"] for sess in session_list)

    # Usage intensity (rolling 5h windows)
    intensity = compute_intensity(sessions, cap) if cap > 0 else None

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "kpis": {
            "total_cost": round(grand_total_cost, 4),
            "total_tokens": grand_total_tokens,
            "total_savings": round(grand_total_savings, 4),
            "unique_sessions": unique_sessions,
            "total_requests": total_requests,
            "avg_cost_per_session": round(grand_total_cost / max(unique_sessions, 1), 4),
            "avg_tokens_per_session": round(grand_total_tokens / max(unique_sessions, 1)),
            "days_in_window": days,
        },
        "hourly_series": hourly_series,
        "model_totals": {k: {"tokens": v["tokens"], "cost": round(v["cost"], 4), "requests": v["requests"]}
                         for k, v in model_totals.items()},
        "task_totals": {k: {"cost": round(v["cost"], 4), "sessions": v["sessions"]}
                        for k, v in task_totals.items()},
        "compact_events": all_compact[:20],  # top 20
        "sessions": session_list,
        "intensity": intensity,
    }


# ─── HTML Template ────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code Token Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0d1117; --bg2: #161b22; --bg3: #21262d;
    --border: #30363d; --text: #e6edf3; --muted: #8b949e;
    --blue: #58a6ff; --green: #3fb950; --red: #f85149;
    --yellow: #d29922; --purple: #bc8cff; --orange: #ffa657;
    --opus: #c678dd; --sonnet: #61afef; --haiku: #98c379;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,monospace; font-size: 13px; line-height: 1.5; }
  a { color: var(--blue); text-decoration: none; }

  /* Layout */
  .header { background: var(--bg2); border-bottom: 1px solid var(--border); padding: 14px 24px; display: flex; align-items: center; justify-content: space-between; }
  .header h1 { font-size: 16px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
  .header .meta { color: var(--muted); font-size: 12px; }
  .refresh-btn { background: var(--bg3); border: 1px solid var(--border); color: var(--text); padding: 5px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; }
  .refresh-btn:hover { background: var(--blue); border-color: var(--blue); color: #fff; }

  .container { max-width: 1400px; margin: 0 auto; padding: 20px 24px; }
  .section-title { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); margin-bottom: 12px; margin-top: 24px; }

  /* KPI cards */
  .kpi-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; }
  @media (max-width: 900px) { .kpi-grid { grid-template-columns: repeat(2, 1fr); } }
  .kpi-card { background: var(--bg2); border: 1px solid var(--border); border-radius: 8px; padding: 16px 18px; }
  .kpi-card .label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }
  .kpi-card .value { font-size: 26px; font-weight: 700; margin-top: 4px; line-height: 1.1; }
  .kpi-card .sub { font-size: 11px; color: var(--muted); margin-top: 4px; }
  .kpi-card.cost .value { color: var(--orange); }
  .kpi-card.savings .value { color: var(--green); }
  .kpi-card.tokens .value { color: var(--blue); }

  /* Charts grid */
  .charts-row { display: grid; grid-template-columns: 1fr; gap: 16px; margin-top: 16px; }
  .chart-card { background: var(--bg2); border: 1px solid var(--border); border-radius: 8px; padding: 18px 20px; }
  .chart-card h3 { font-size: 13px; font-weight: 600; margin-bottom: 14px; color: var(--text); }
  .chart-card canvas { max-height: 280px; }

  .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 900px) { .two-col { grid-template-columns: 1fr; } }

  /* Spike/event cards — compact chips */
  .event-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 8px; }
  .event-card { background: var(--bg2); border-left: 3px solid var(--red); border-radius: 0 5px 5px 0; padding: 8px 10px; cursor: pointer; transition: background 0.15s; }
  .event-card:hover { background: var(--bg3); }
  .event-card.compact { border-left-color: var(--purple); }
  .event-card .ev-title { font-weight: 600; font-size: 11px; color: var(--red); margin-bottom: 3px; }
  .event-card.compact .ev-title { color: var(--purple); }
  .event-card .ev-time { font-family: monospace; font-size: 11px; color: var(--text); }
  .event-card .ev-tokens { font-size: 11px; color: var(--muted); }
  .event-card .ev-session { font-size: 10px; color: var(--muted); margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  /* Modal */
  .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.7); z-index: 1000; align-items: center; justify-content: center; }
  .modal-overlay.open { display: flex; }
  .modal-box { background: var(--bg2); border: 1px solid var(--border); border-radius: 10px; padding: 24px 28px; max-width: 640px; width: 90%; max-height: 80vh; overflow-y: auto; position: relative; }
  .modal-box h2 { font-size: 14px; font-weight: 700; margin-bottom: 16px; }
  .modal-close { position: absolute; top: 14px; right: 18px; background: none; border: none; color: var(--muted); font-size: 18px; cursor: pointer; line-height: 1; }
  .modal-close:hover { color: var(--text); }
  .modal-row { margin-bottom: 10px; font-size: 12px; }
  .modal-row .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 3px; }
  .modal-quote { padding: 10px 12px; background: var(--bg3); border-radius: 5px; border-left: 2px solid var(--border); font-size: 11px; color: #c9d1d9; line-height: 1.6; white-space: pre-wrap; word-break: break-word; margin-top: 4px; }
  .modal-tools { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 4px; }
  .ev-tool-badge { background: #1f2d3d; color: #79c0ff; padding: 2px 8px; border-radius: 8px; font-size: 10px; font-family: monospace; }

  /* Sessions table */
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  thead th { background: var(--bg3); padding: 8px 12px; text-align: left; font-weight: 600; color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 1px solid var(--border); white-space: nowrap; }
  tbody tr { border-bottom: 1px solid var(--border); }
  tbody tr:hover { background: var(--bg3); }
  tbody td { padding: 9px 12px; vertical-align: top; }
  .task-cell { max-width: 280px; }
  .task-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tag { display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 10px; font-weight: 600; text-transform: uppercase; margin-right: 4px; }
  .tag.planning  { background: #1f2d47; color: #58a6ff; }
  .tag.debugging { background: #2d1f1f; color: #f85149; }
  .tag.coding    { background: #1f2d24; color: #3fb950; }
  .tag.refactor  { background: #2d2a1f; color: #d29922; }
  .tag.research  { background: #2a1f2d; color: #bc8cff; }
  .tag.canvas    { background: #1f2d2d; color: #56d364; }
  .tag.document  { background: #2d261f; color: #ffa657; }
  .tag.dashboard { background: #1f2540; color: #79c0ff; }
  .tag.other     { background: var(--bg3); color: var(--muted); }
  .tag.subagent  { background: #1f1f2d; color: #8b949e; }
  .model-dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 5px; }
  .cost-high { color: var(--red); font-weight: 600; }
  .cost-med  { color: var(--orange); }
  .cost-low  { color: var(--muted); }
  .compact-badge { background: #2a1f2d; color: var(--purple); padding: 1px 6px; border-radius: 8px; font-size: 10px; }

  /* Expandable rows */
  .detail-row { display: none; background: #0d1117; }
  .detail-row.open { display: table-row; }
  .detail-cell { padding: 0 !important; }
  .detail-inner { padding: 14px 18px; border-top: 1px solid var(--border); width: calc(100vw - 60px); max-width: 100%; box-sizing: border-box; overflow-x: auto; }
  .detail-section { margin-bottom: 14px; }
  .detail-section h4 { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 8px; }
  .detail-table { width: 100%; min-width: 560px; border-collapse: collapse; font-size: 11px; table-layout: fixed; }
  .detail-table th { text-align: left; color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em; padding: 4px 10px; border-bottom: 1px solid var(--border); font-weight: 500; }
  .detail-table td { padding: 7px 10px; border-bottom: 1px solid #21262d; vertical-align: top; line-height: 1.5; }
  .detail-table tr:last-child td { border-bottom: none; }
  .detail-table tr:hover td { background: #161b22; }
  .dt-label { color: #58a6ff; font-weight: 600; font-size: 11px; word-break: break-word; }
  .dt-msg { color: #c9d1d9; word-break: break-word; overflow-wrap: anywhere; }
  .dt-stats { color: var(--muted); word-break: break-word; }
  .dt-tokens { color: var(--blue); font-family: monospace; font-weight: 600; font-size: 12px; }
  .dt-cost { color: var(--orange); font-family: monospace; font-size: 11px; }
  .dt-breakdown { font-size: 9px; color: #444d56; margin-top: 2px; }
  .req-tools { margin-top: 5px; display: flex; flex-wrap: wrap; gap: 3px; }
  .expand-btn { cursor: pointer; font-size: 10px; color: var(--blue); padding: 2px 6px; border: 1px solid var(--border); border-radius: 4px; background: none; white-space: nowrap; }
  .expand-btn:hover { background: var(--bg3); }
  /* Terminology */
  .glossary { margin: 40px 0 20px; padding: 0 4px; }
  .glossary h3 { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.07em; margin-bottom: 12px; }
  .glossary-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px; }
  .glossary-card { background: var(--bg2); border: 1px solid var(--border); border-radius: 6px; padding: 12px 14px; }
  .glossary-card dt { color: var(--blue); font-weight: 600; font-size: 11px; margin-bottom: 5px; }
  .glossary-card dd { color: #8b949e; font-size: 11px; margin: 0; line-height: 1.5; }

  /* Usage Intensity */
  .intensity-section { margin-top: 28px; }
  .heatmap-table { border-collapse: collapse; font-size: 10px; }
  .heatmap-table th, .heatmap-table td { padding: 4px 6px; text-align: center; min-width: 28px; }
  .heatmap-table th { color: var(--muted); font-weight: 500; }
  .heatmap-cell { border-radius: 3px; cursor: default; }
  .event-card.peak { border-left-color: var(--orange); }
  .event-card.peak .ev-title { color: var(--orange); }

  /* View buttons & date picker */
  .view-btns { display:flex; gap:3px; }
  .view-btn { background:var(--bg3); border:1px solid var(--border); color:var(--muted); border-radius:5px; padding:3px 10px; font-size:11px; cursor:pointer; transition:all .15s; }
  .view-btn:hover { color:var(--fg); border-color:#58a6ff; }
  .view-btn.active { background:#1f6feb; border-color:#58a6ff; color:#fff; }
  .date-input { background:var(--bg3); border:1px solid var(--border); color:var(--fg); border-radius:5px; padding:3px 7px; font-size:11px; cursor:pointer; }
  .date-input::-webkit-calendar-picker-indicator { filter:invert(0.6); cursor:pointer; }

  /* Subagent toggle */
  .toggle-row { display: flex; align-items: center; gap: 12px; margin-top: 12px; margin-bottom: 8px; }
  .toggle-label { font-size: 12px; color: var(--muted); cursor: pointer; user-select: none; }
  .toggle-label input { margin-right: 5px; cursor: pointer; }

  /* Footer */
  footer { text-align: center; color: var(--muted); font-size: 11px; padding: 20px; margin-top: 20px; border-top: 1px solid var(--border); }

  /* Pagination */
  .pagination { display: flex; align-items: center; justify-content: center; gap: 6px; padding: 12px 0 4px; }
  .pg-btn { background: var(--bg3); border: 1px solid var(--border); color: var(--fg); border-radius: 6px; cursor: pointer; font-size: 18px; width: 38px; height: 38px; display: flex; align-items: center; justify-content: center; transition: background .15s; }
  .pg-btn:hover:not(:disabled) { background: var(--accent); color: #fff; }
  .pg-btn:disabled { opacity: 0.3; cursor: default; }
  .pg-info { color: var(--muted); font-size: 12px; min-width: 90px; text-align: center; }

  /* Floating pagination bar */
  .pagination-float {
    display: none; position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
    background: var(--bg2); border: 1px solid var(--border); border-radius: 10px;
    padding: 6px 10px; box-shadow: 0 4px 20px rgba(0,0,0,.5);
    flex-direction: row; align-items: center; gap: 6px; z-index: 500;
  }
  .pagination-float.visible { display: flex; }
</style>
</head>
<body>

<div class="header">
  <h1>⚡ Claude Code Token Dashboard</h1>
  <div style="display:flex;align-items:center;gap:16px;">
    <span class="meta" id="gen-time"></span>
    <button class="refresh-btn" onclick="window.location.reload()">↻ Refresh</button>
  </div>
</div>

<div class="container">

  <!-- KPI Cards -->
  <div class="section-title">Overview</div>
  <div class="kpi-grid" id="kpi-cards"></div>

  <!-- Time Series -->
  <div class="section-title" style="margin-top:28px;">Token Usage Over Time</div>
  <div class="chart-card">
    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:12px">
      <h3 id="ts-title" style="margin:0">Tokens</h3>
      <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
        <div class="view-btns">
          <button class="view-btn" data-view="1d">1D</button>
          <button class="view-btn active" data-view="3d">3D</button>
          <button class="view-btn" data-view="7d">7D</button>
          <button class="view-btn" data-view="30d">30D</button>
          <button class="view-btn" data-view="1y">1Y</button>
        </div>
        <input type="date" id="date-from" class="date-input" title="From">
        <span style="color:var(--muted);font-size:11px">→</span>
        <input type="date" id="date-to" class="date-input" title="To">
        <button class="view-btn" id="date-apply">Apply</button>
      </div>
    </div>
    <canvas id="tsChart"></canvas>
  </div>

  <!-- Usage Intensity -->
  <div id="intensity-section" class="intensity-section" style="display:none">
    <div class="section-title">Usage Intensity <span style="color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0;font-size:11px">— rolling 5h window vs estimated plan cap</span></div>
    <div class="chart-card">
      <h3>Rolling 5-Hour Token Usage</h3>
      <canvas id="intensityChart"></canvas>
    </div>
    <div class="chart-card" style="margin-top:12px">
      <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:10px">
        <h3 style="margin:0">Usage Heatmap <span style="color:var(--muted);font-weight:400;font-size:11px">— tokens by day &amp; hour vs plan cap</span></h3>
        <div style="display:flex;align-items:center;gap:6px">
          <input type="date" id="hm-from" class="date-input" title="From">
          <span style="color:var(--muted);font-size:11px">→</span>
          <input type="date" id="hm-to" class="date-input" title="To">
          <button class="view-btn" id="hm-apply">Apply</button>
        </div>
      </div>
      <div id="heatmap-container"></div>
    </div>
    <div id="peak-events-section" style="margin-top:12px">
      <div class="section-title" id="peaks-title">🔥 Peak Usage Windows <span style="color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0;font-size:11px">— click any card for details</span></div>
      <div class="event-grid" id="peak-cards"></div>
    </div>
  </div>

  <!-- Model + Task charts -->
  <div class="section-title">Breakdown</div>
  <div class="two-col">
    <div class="chart-card">
      <h3>Cost by Model</h3>
      <canvas id="modelChart"></canvas>
    </div>
    <div class="chart-card">
      <h3>Task Type Distribution</h3>
      <canvas id="taskChart"></canvas>
    </div>
  </div>

  <!-- Spike / Compact Events -->
  <div id="events-section">
    <div class="section-title">⚡ Spike &amp; Compact Events <span style="color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0;font-size:11px">— click any card for details</span></div>
    <div class="event-grid" id="event-cards"></div>
  </div>

  <!-- Modal -->
  <div class="modal-overlay" id="ev-modal" onclick="if(event.target===this)closeModal()">
    <div class="modal-box">
      <button class="modal-close" onclick="closeModal()">✕</button>
      <div id="modal-content"></div>
    </div>
  </div>

  <!-- Sessions Table -->
  <div class="section-title" style="margin-top:28px;">Sessions (sorted by cost)</div>
  <div class="toggle-row">
    <label class="toggle-label"><input type="checkbox" id="show-subagents" onchange="renderTable()"> Show subagents</label>
    <span style="color:var(--muted);font-size:11px">Click any row to expand conversation &amp; top requests</span>
  </div>
  <div class="chart-card" style="padding:0;">
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>Session</th>
            <th>Chat</th>
            <th>Type</th>
            <th>Model</th>
            <th>Tokens</th>
            <th>Cost</th>
            <th>Savings</th>
            <th>Requests</th>
            <th>Compact</th>
            <th>Duration</th>
            <th>Start</th>
          </tr>
        </thead>
        <tbody id="session-tbody"></tbody>
      </table>
    </div>
  </div>
  <div class="pagination" id="pagination-inline"></div>

</div>

<!-- Floating pagination bar (visible when toggle-row scrolls off-screen) -->
<div class="pagination-float" id="pagination-float"></div>

<footer>Generated by generate_dashboard.py &nbsp;·&nbsp; Claude Code Token Dashboard</footer>

<script>
const DATA = __DATA__;

// ── Utils ──────────────────────────────────────────────────────────
function fmt_tokens(n) {
  if (n >= 1e6) return (n/1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n/1e3).toFixed(1) + 'K';
  return n;
}
function fmt_cost(c) { return '$' + c.toFixed(2); }
function fmt_kpi_cost(c) { return '$' + c.toLocaleString('en-US', {minimumFractionDigits:1, maximumFractionDigits:1}); }
function fmt_dur(min) {
  const m = Math.round(min);
  if (m < 60) return m + 'm';
  const h = Math.floor(m / 60), r = m % 60;
  return r === 0 ? h + 'h' : h + 'h ' + r + 'm';
}
function cost_class(c) {
  if (c > 1) return 'cost-high';
  if (c > 0.1) return 'cost-med';
  return 'cost-low';
}
const MODEL_COLORS = {
  'claude-opus-4-6': '#c678dd', 'claude-opus-4-5': '#c678dd',
  'claude-sonnet-4-6': '#61afef', 'claude-sonnet-4-5': '#61afef',
  'claude-haiku-4-5': '#98c379', 'claude-haiku-4-5-20251001': '#98c379',
  'unknown': '#abb2bf',
};
function model_color(m) { return MODEL_COLORS[m] || '#abb2bf'; }
function short_model(m) { return m.replace('claude-','').replace('-20251001',''); }

function fmt_hour(hourStr) {
  // hourStr is "2026-04-03T14:00" in UTC — convert to local
  const d = new Date(hourStr + ':00Z');
  if (isNaN(d)) return hourStr.replace('T',' ');
  const yr = d.getFullYear();
  const mo = String(d.getMonth()+1).padStart(2,'0');
  const dy = String(d.getDate()).padStart(2,'0');
  const hh = String(d.getHours()).padStart(2,'0');
  const mm = String(d.getMinutes()).padStart(2,'0');
  return `${yr}-${mo}-${dy} ${hh}:${mm}`;
}

function fmt_time(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return iso.slice(0,16).replace('T',' ');
  const mo = String(d.getMonth()+1).padStart(2,'0');
  const dy = String(d.getDate()).padStart(2,'0');
  const hh = String(d.getHours()).padStart(2,'0');
  const mm = String(d.getMinutes()).padStart(2,'0');
  return `${mo}-${dy} ${hh}:${mm}`;
}

// ── KPIs ───────────────────────────────────────────────────────────
document.getElementById('gen-time').textContent = 'Generated: ' + DATA.generated_at;
const k = DATA.kpis;
const kpi_defs = [
  { label: 'Total Tokens', value: fmt_tokens(k.total_tokens), sub: k.total_requests + ' API requests', sub2: '≈ ' + fmt_kpi_cost(k.total_cost) + ' API equiv', cls: 'tokens' },
  { label: 'Tokens / Day', value: fmt_tokens(Math.round(k.total_tokens / k.days_in_window)), sub: k.days_in_window + '-day avg', cls: 'tokens' },
  { label: 'Sessions', value: k.unique_sessions, sub: 'Main (excl. subagents)', cls: '' },
  { label: 'Tokens / Session', value: fmt_tokens(k.avg_tokens_per_session), sub: 'avg per main session', cls: '' },
  { label: 'Cache Savings', value: fmt_kpi_cost(k.total_savings), sub: 'vs no-cache baseline', cls: 'savings' },
];
const kpiGrid = document.getElementById('kpi-cards');
kpi_defs.forEach(d => {
  kpiGrid.innerHTML += `<div class="kpi-card ${d.cls}">
    <div class="label">${d.label}</div>
    <div class="value">${d.value}</div>
    <div class="sub">${d.sub}</div>
    ${d.sub2 ? `<div class="sub" style="margin-top:2px;opacity:0.6;font-size:10px">${d.sub2}</div>` : ''}
  </div>`;
});

// ── Time-Series Chart ──────────────────────────────────────────────
const allHours = DATA.hourly_series; // full dataset from Python

// Parse each bucket's UTC timestamp into a local Date once
const allHourDates = allHours.map(h => new Date(h.hour + ':00Z'));

// Aggregate hourly buckets into a coarser granularity
function aggregateBuckets(filtered, granularity) {
  // granularity: 'hour' | 'day' | 'week' | 'month'
  const map = new Map();
  filtered.forEach((h, i) => {
    const d = allHourDates[allHours.indexOf(h)];
    let key;
    if (granularity === 'hour') {
      key = h.hour; // already unique per hour
    } else if (granularity === 'day') {
      key = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
    } else if (granularity === 'month') {
      key = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}`;
    } else { // week
      // ISO week start (Monday)
      const tmp = new Date(d); tmp.setHours(0,0,0,0);
      tmp.setDate(tmp.getDate() - (tmp.getDay()||7) + 1);
      key = `${tmp.getFullYear()}-W${String(Math.ceil((((tmp - new Date(tmp.getFullYear(),0,1))/86400000)+1)/7)).padStart(2,'0')}`;
    }
    if (!map.has(key)) map.set(key, {key, tokens:0, cost:0, spikes:0, models:{}});
    const b = map.get(key);
    b.tokens += h.tokens; b.cost += h.cost;
    if (h.is_spike) b.spikes++;
    Object.entries(h.models||{}).forEach(([m,t]) => { b.models[m] = (b.models[m]||0) + t; });
  });
  return [...map.values()];
}

// Fill in zero-value buckets for every period in [startDate, endDate]
function fillGaps(buckets, granularity, startDate, endDate) {
  const map = new Map(buckets.map(b => [b.key, b]));
  const full = [];
  const cur = new Date(startDate);

  if (granularity === 'hour') {
    // Keys must match the UTC strings produced by Python (e.g. "2026-04-04T10:00")
    cur.setUTCMinutes(0, 0, 0);
    while (cur <= endDate) {
      const key = `${cur.getUTCFullYear()}-${String(cur.getUTCMonth()+1).padStart(2,'0')}-${String(cur.getUTCDate()).padStart(2,'0')}T${String(cur.getUTCHours()).padStart(2,'0')}:00`;
      full.push(map.get(key) || {key, tokens:0, cost:0, spikes:0, models:{}});
      cur.setUTCHours(cur.getUTCHours() + 1);
    }
  } else if (granularity === 'day') {
    cur.setHours(0, 0, 0, 0);
    while (cur <= endDate) {
      const key = `${cur.getFullYear()}-${String(cur.getMonth()+1).padStart(2,'0')}-${String(cur.getDate()).padStart(2,'0')}`;
      full.push(map.get(key) || {key, tokens:0, cost:0, spikes:0, models:{}});
      cur.setDate(cur.getDate() + 1);
    }
  } else if (granularity === 'month') {
    cur.setDate(1); cur.setHours(0, 0, 0, 0);
    while (cur <= endDate) {
      const key = `${cur.getFullYear()}-${String(cur.getMonth()+1).padStart(2,'0')}`;
      full.push(map.get(key) || {key, tokens:0, cost:0, spikes:0, models:{}});
      cur.setMonth(cur.getMonth() + 1);
    }
  } else {
    // week — just return sorted existing buckets (gap-fill not needed for weekly)
    return [...map.values()].sort((a, b) => a.key.localeCompare(b.key));
  }
  return full;
}

function bucketLabel(key, granularity) {
  if (granularity === 'hour') {
    const d = new Date(key + ':00Z');
    const mo = String(d.getMonth()+1).padStart(2,'0');
    const dy = String(d.getDate()).padStart(2,'0');
    const hh = String(d.getHours()).padStart(2,'0');
    return `${mo}-${dy} ${hh}:00`;
  } else if (granularity === 'day') {
    return key.slice(5); // MM-DD
  } else if (granularity === 'month') {
    const [yr, mo] = key.split('-');
    const names = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return `${names[parseInt(mo)-1]} ${yr}`;
  } else {
    return key; // YYYY-Www
  }
}

let tsChart = null;
let currentView = '3d';

function getViewConfig(view) {
  const now = new Date();
  let cutoff, granularity, title;
  if (view === '1d')      { cutoff = new Date(now - 1*86400000);  granularity = 'hour';  title = 'Tokens per Hour (past 24 hours)'; }
  else if (view === '3d') { cutoff = new Date(now - 3*86400000);  granularity = 'hour';  title = 'Tokens per Hour (past 3 days)'; }
  else if (view === '7d') { cutoff = new Date(now - 7*86400000);  granularity = 'hour';  title = 'Tokens per Hour (past 7 days)'; }
  else if (view === '30d'){ cutoff = new Date(now - 30*86400000); granularity = 'day';   title = 'Tokens per Day (past 30 days)'; }
  else if (view === '1y') { cutoff = new Date(now - 365*86400000);granularity = 'month'; title = 'Tokens per Month (past year)'; }
  else { cutoff = null; granularity = 'hour'; title = 'Tokens (custom range)'; }
  return { cutoff, granularity, title };
}

function renderChart(view, fromDate, toDate) {
  const { cutoff, granularity, title } = getViewConfig(view);

  // Filter allHours by date range
  const filtered = allHours.filter((h, i) => {
    const d = allHourDates[i];
    if (fromDate && toDate) return d >= fromDate && d <= toDate;
    return cutoff ? d >= cutoff : true;
  });

  const rangeStart = fromDate || cutoff || (allHourDates[0] || new Date());
  const rangeEnd = toDate || new Date();
  const buckets = fillGaps(aggregateBuckets(filtered, granularity), granularity, rangeStart, rangeEnd);
  const labels = buckets.map(b => bucketLabel(b.key, granularity));
  const spikeCount = buckets.reduce((n, b) => n + b.spikes, 0);
  document.getElementById('ts-title').textContent =
    title + (spikeCount ? ` · ⚡ ${spikeCount} spike(s)` : '');

  // Consistent tick limit per view
  const maxTicks = granularity === 'hour' ? (view === '1d' ? 24 : view === '3d' ? 24 : 28) : granularity === 'day' ? 30 : granularity === 'month' ? 12 : 52;

  const chartData = {
    labels,
    datasets: [{
      label: 'Tokens',
      data: buckets.map(b => b.tokens),
      backgroundColor: buckets.map(b => b.spikes ? 'rgba(248,81,73,0.75)' : 'rgba(88,166,255,0.55)'),
      borderColor: buckets.map(b => b.spikes ? '#f85149' : '#58a6ff'),
      borderWidth: 1,
    }, {
      type: 'line',
      label: 'Cost ($)',
      data: buckets.map(b => b.cost),
      borderColor: '#ffa657',
      backgroundColor: 'transparent',
      yAxisID: 'y2',
      tension: 0.3,
      pointRadius: (granularity === 'week' || granularity === 'month') ? 4 : 2,
    }]
  };

  const opts = {
    responsive: true, maintainAspectRatio: true,
    plugins: {
      legend: { labels: { color:'#8b949e', font:{size:11} } },
      tooltip: { callbacks: { afterBody: items => {
        const b = buckets[items[0]?.dataIndex];
        if (!b) return [];
        const lines = [];
        Object.entries(b.models||{}).forEach(([m,t]) => lines.push(`  ${short_model(m)}: ${fmt_tokens(t)}`));
        if (b.spikes) lines.push(`⚡ ${b.spikes} spike(s)`);
        return lines;
      }}}
    },
    scales: {
      x: { ticks: { color:'#8b949e', font:{size:9}, maxRotation:45, maxTicksLimit: maxTicks, autoSkip: true }, grid:{color:'#21262d'} },
      y: { ticks: { color:'#8b949e', font:{size:10}, callback: v => fmt_tokens(v) }, grid:{color:'#21262d'}, title:{display:true,text:'Tokens',color:'#8b949e',font:{size:10}} },
      y2: { position:'right', ticks:{color:'#ffa657',font:{size:10},callback:v=>'$'+v.toFixed(2)}, grid:{drawOnChartArea:false}, title:{display:true,text:'Cost ($)',color:'#ffa657',font:{size:10}} }
    }
  };

  const tsCtx = document.getElementById('tsChart').getContext('2d');
  if (tsChart) { tsChart.destroy(); }
  tsChart = new Chart(tsCtx, { type:'bar', data: chartData, options: opts });
}

// Init default 3D view
renderChart('3d');

// View button handlers
document.querySelectorAll('.view-btn[data-view]').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.view-btn[data-view]').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentView = btn.dataset.view;
    document.getElementById('date-from').value = '';
    document.getElementById('date-to').value = '';
    const { cutoff } = getViewConfig(btn.dataset.view);
    renderChart(currentView);
    renderIntensityChart(cutoff, null);
  });
});

// Date range apply
document.getElementById('date-apply').addEventListener('click', () => {
  const from = document.getElementById('date-from').value;
  const to = document.getElementById('date-to').value;
  if (!from && !to) { renderChart(currentView); const {cutoff} = getViewConfig(currentView); renderIntensityChart(cutoff, null); return; }
  document.querySelectorAll('.view-btn[data-view]').forEach(b => b.classList.remove('active'));
  const fromDate = from ? new Date(from + 'T00:00:00') : null;
  const toDate = to ? new Date(to + 'T23:59:59') : new Date();
  renderChart('custom', fromDate, toDate);
  renderIntensityChart(fromDate, toDate);
});

// ── Model Chart ─────────────────────────────────────────────────────
const modelData = DATA.model_totals;
const modelNames = Object.keys(modelData)
  .filter(m => m !== '<synthetic>')
  .sort((a,b) => modelData[b].cost - modelData[a].cost);
const modelCtx = document.getElementById('modelChart').getContext('2d');
new Chart(modelCtx, {
  type: 'bar',
  data: {
    labels: modelNames.map(short_model),
    datasets: [
      {
        label: 'Cost ($)',
        type: 'bar',
        data: modelNames.map(m => modelData[m].cost),
        backgroundColor: modelNames.map(m => model_color(m) + '99'),
        borderColor: modelNames.map(m => model_color(m)),
        borderWidth: 1,
        xAxisID: 'xCost',
        order: 2,
      },
      {
        label: 'Tokens',
        type: 'line',
        data: modelNames.map(m => modelData[m].tokens),
        borderColor: 'transparent',
        backgroundColor: '#8b949e',
        pointBackgroundColor: modelNames.map(m => model_color(m)),
        pointBorderColor: modelNames.map(m => model_color(m)),
        pointRadius: 6,
        pointHoverRadius: 8,
        pointStyle: 'circle',
        showLine: false,
        xAxisID: 'xTokens',
        order: 1,
      }
    ]
  },
  options: {
    indexAxis: 'y',
    responsive: true,
    maintainAspectRatio: true,
    plugins: {
      legend: { labels: { color: '#e6edf3', font: { size: 11 }, boxWidth: 32, boxHeight: 10 } },
      tooltip: {
        callbacks: {
          label: item => {
            const m = modelNames[item.dataIndex];
            if (!m) return '';
            if (item.datasetIndex === 0) return ` Cost: $${modelData[m].cost.toFixed(4)}`;
            return ` Tokens: ${fmt_tokens(modelData[m].tokens)}`;
          },
          afterLabel: item => {
            const m = modelNames[item.dataIndex];
            if (!m || item.datasetIndex !== 0) return [];
            return [`Requests: ${modelData[m].requests}`];
          }
        }
      }
    },
    scales: {
      xCost: {
        position: 'top',
        ticks: { color: '#bc8cff', font: { size: 9 }, callback: v => '$' + v.toFixed(2) },
        grid: { color: '#21262d' },
        title: { display: true, text: 'Cost ($)', color: '#bc8cff', font: { size: 10 } }
      },
      xTokens: {
        position: 'bottom',
        ticks: { color: '#8b949e', font: { size: 9 }, callback: v => fmt_tokens(v) },
        grid: { color: '#21262d', drawOnChartArea: false },
        title: { display: true, text: 'Tokens', color: '#8b949e', font: { size: 10 } }
      },
      y: {
        ticks: { color: '#e6edf3', font: { size: 11 } },
        grid: { color: '#21262d' }
      }
    }
  }
});

// ── Task Type Chart ─────────────────────────────────────────────────
const taskColors = {
  planning:'#58a6ff', debugging:'#f85149', coding:'#3fb950',
  refactor:'#d29922', research:'#bc8cff', canvas:'#56d364',
  document:'#ffa657', dashboard:'#79c0ff', other:'#8b949e'
};
const taskData = DATA.task_totals;
const taskNames = Object.keys(taskData).sort((a,b) => taskData[b].cost - taskData[a].cost);
const taskCtx = document.getElementById('taskChart').getContext('2d');
new Chart(taskCtx, {
  type: 'doughnut',
  data: {
    labels: taskNames.map(t => t + ' (' + taskData[t].sessions + ')'),
    datasets: [{ data: taskNames.map(t => taskData[t].cost),
      backgroundColor: taskNames.map(t => (taskColors[t]||'#8b949e')+'99'),
      borderColor: taskNames.map(t => taskColors[t]||'#8b949e'),
      borderWidth: 2 }]
  },
  options: {
    responsive: true, maintainAspectRatio: true,
    plugins: {
      legend: { position:'right', labels:{color:'#e6edf3', font:{size:11}, padding:10} },
      tooltip: { callbacks: { label: item => {
        const t = taskNames[item.dataIndex];
        return ` $${taskData[t].cost.toFixed(4)} (${taskData[t].sessions} sessions)`;
      }}}
    }
  }
});

// ── Spike / Compact Events ──────────────────────────────────────────
const evGrid = document.getElementById('event-cards');
const evSection = document.getElementById('events-section');

function esc(s) { return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function openModal(html) {
  document.getElementById('modal-content').innerHTML = html;
  document.getElementById('ev-modal').classList.add('open');
}
function closeModal() {
  document.getElementById('ev-modal').classList.remove('open');
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

// Build modal HTML helpers
function modalRow(label, value) {
  return `<div class="modal-row"><div class="label">${label}</div><div>${value}</div></div>`;
}

// Spike hours
const spikes = allHours.filter(h => h.is_spike);
spikes.forEach(h => {
  const topModel = Object.entries(h.models||{}).sort((a,b)=>b[1]-a[1])[0];
  // Collect ALL sessions active during this hour
  const activeSessions = DATA.sessions.filter(s =>
    (s.top_requests||[]).some(r => r.ts && r.ts.startsWith(h.hour.slice(0,13)))
  );
  const bigReq = activeSessions.flatMap(s =>
    (s.top_requests||[]).filter(r => r.ts && r.ts.startsWith(h.hour.slice(0,13)))
  ).sort((a,b) => b.total_tokens - a.total_tokens)[0];
  const bigSess = bigReq ? activeSessions.find(s => (s.top_requests||[]).includes(bigReq)) : null;

  const onclickData = JSON.stringify({type:'spike', hour: h.hour, tokens: h.tokens, cost: h.cost,
    topModel: topModel?.[0]||'', activeSessions: activeSessions.map(s=>s.slug||s.session_id),
    bigReqTokens: bigReq?.total_tokens||0, bigReqCost: bigReq?.cost||0,
    bigReqUser: bigReq?.preceding_user||'', bigReqTools: bigReq?.tools_called||[],
    taskDesc: bigSess?.task_description||''
  }).replace(/'/g, "&#39;");

  evGrid.innerHTML += `<div class="event-card" onclick='openSpikeModal(${onclickData})'>
    <div class="ev-title">⚡ Spike</div>
    <div class="ev-time"><span style="color:var(--muted);font-size:9px">hour </span>${fmt_hour(h.hour)}</div>
    <div class="ev-tokens"><span style="color:var(--muted);font-size:9px">tokens </span>${fmt_tokens(h.tokens)} &nbsp; <span style="color:var(--muted);font-size:9px">cost </span>${fmt_cost(h.cost)}</div>
  </div>`;
});

function openSpikeModal(d) {
  const sessLinks = d.activeSessions.map(s => `<span style="color:var(--blue)">${esc(s)}</span>`).join(', ');
  const toolsHtml = d.bigReqTools?.length
    ? `<div class="modal-tools">${d.bigReqTools.map(t=>`<span class="ev-tool-badge">${esc(t)}</span>`).join('')}</div>` : '';
  openModal(`
    <h2>⚡ Token Spike — ${fmt_hour(d.hour)}</h2>
    ${modalRow('Total this hour', `${fmt_tokens(d.tokens)} tokens &nbsp;·&nbsp; ${fmt_cost(d.cost)}`)}
    ${d.topModel ? modalRow('Dominant model', `<span style="color:var(--blue)">${esc(short_model(d.topModel))}</span>`) : ''}
    ${sessLinks ? modalRow('Active sessions', sessLinks) : ''}
    ${d.bigReqTokens ? modalRow('Largest single request', `${fmt_tokens(d.bigReqTokens)} tokens &nbsp;·&nbsp; ${fmt_cost(d.bigReqCost)}`) : ''}
    ${d.taskDesc ? modalRow('Session task', `<div style="color:#c9d1d9">${esc(d.taskDesc.slice(0,300))}</div>`) : ''}
    ${d.bigReqUser ? `<div class="modal-row"><div class="label">User message before spike</div><div class="modal-quote">💬 ${esc(d.bigReqUser.slice(0,400))}</div></div>` : ''}
    ${toolsHtml ? `<div class="modal-row"><div class="label">Tools called</div>${toolsHtml}</div>` : ''}
  `);
}

// Compact events
(DATA.compact_events || []).forEach(c => {
  const onclickData = JSON.stringify({
    ts: c.ts, preTokens: c.pre_tokens, slug: c.session_slug,
    task: c.task||'', user: c.preceding_user||''
  }).replace(/'/g, "&#39;");

  evGrid.innerHTML += `<div class="event-card compact" onclick='openCompactModal(${onclickData})'>
    <div class="ev-title">🗜 Compact</div>
    <div class="ev-time">${fmt_time(c.ts)}</div>
    <div class="ev-tokens"><span style="color:var(--muted);font-size:9px">tokens </span>${fmt_tokens(c.pre_tokens)}</div>
  </div>`;
});

function openCompactModal(d) {
  openModal(`
    <h2>🗜 Context Compaction — ${fmt_time(d.ts)}</h2>
    ${modalRow('Pre-compact tokens', `${fmt_tokens(d.preTokens)}`)}
    ${d.task ? modalRow('Session task', `<div style="color:#c9d1d9">${esc(d.task.slice(0,300))}</div>`) : ''}
    ${d.user ? `<div class="modal-row"><div class="label">User was asking</div><div class="modal-quote">💬 ${esc(d.user.slice(0,400))}</div></div>` : ''}
    <div class="modal-row" style="margin-top:14px;color:var(--muted);font-size:11px">
      Context compaction is triggered automatically when the conversation history approaches the model's context limit.
      The session continued after compaction — but this is why token usage spiked.
    </div>
  `);
}

if (!spikes.length && !DATA.compact_events?.length) {
  evSection.style.display = 'none';
}

// ── Sessions Table ──────────────────────────────────────────────────
let expandedRows = new Set();

function toggleRow(rowId) {
  const dr = document.getElementById('detail-' + rowId);
  if (!dr) return;
  if (expandedRows.has(rowId)) {
    expandedRows.delete(rowId);
    dr.classList.remove('open');
  } else {
    expandedRows.add(rowId);
    dr.classList.add('open');
  }
}

const PAGE_SIZE = 10;
let currentPage = 0;

function pgHtml(page, total) {
  const pages = Math.ceil(total / PAGE_SIZE);
  if (pages <= 1) return '';
  return `<button class="pg-btn" onclick="goPage(${page-1})" ${page===0?'disabled':''}>&#8592;</button>
          <span class="pg-info">${page+1} / ${pages}</span>
          <button class="pg-btn" onclick="goPage(${page+1})" ${page>=pages-1?'disabled':''}>&#8594;</button>`;
}

function goPage(p) {
  currentPage = p;
  renderTable();
  document.getElementById('session-tbody').closest('.chart-card').scrollIntoView({behavior:'smooth', block:'start'});
}

function renderTable() {
  const showSub = document.getElementById('show-subagents').checked;
  const tbody = document.getElementById('session-tbody');
  let rows = DATA.sessions;
  if (!showSub) rows = rows.filter(s => !s.is_subagent);

  const pages = Math.ceil(rows.length / PAGE_SIZE);
  if (currentPage >= pages) currentPage = Math.max(0, pages - 1);
  const pageRows = rows.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE);

  const pHtml = pgHtml(currentPage, rows.length);
  document.getElementById('pagination-inline').innerHTML = pHtml;
  document.getElementById('pagination-float').innerHTML = pHtml;

  tbody.innerHTML = '';
  pageRows.forEach((s, pi) => {
    const i = currentPage * PAGE_SIZE + pi;
    const rowId = 'sess-' + i;
    const compactBadge = s.compact_events?.length
      ? `<span class="compact-badge">🗜 ${s.compact_events.length}×</span>` : '—';
    const mColor = model_color(s.model);
    const cClass = cost_class(s.total_cost);
    // Task: show first 160 chars; slug is secondary
    const taskShort = s.task_description.slice(0, 160) + (s.task_description.length > 160 ? '…' : '');
    const colCount = 12;

    // ── Detail panel HTML ─────────────────────────────────────────
    const sessionTask = (s.task_description||'').split(/\n/)[0].trim().slice(0, 50);

    // Col 1: reader-friendly intent label
    function reqLabel(text) {
      if (!text) return sessionTask || 'Auto-continued';
      const t = text.trim();
      if (/^(CRITICAL:|Stop hook|Hook feedback)/i.test(t)) return 'System hook check';
      if (/^<scheduled-task/i.test(t)) return 'Scheduled task run';
      if (/^continue\.?$/i.test(t) || /^Continue from where/i.test(t))
        return sessionTask || 'Continue session';
      // real user message — extract intent
      const stripped = t.replace(/^(please |can you |could you |i need to |i want to |i'd like to |help me |let's |lets )/i, '');
      const sentMatch = stripped.match(/^[^.!?\n]{4,80}[.!?]/);
      if (sentMatch) return sentMatch[0];
      const fl = stripped.split(/\n/)[0].trim();
      if (fl.length <= 50) return fl;
      const cut = fl.lastIndexOf(' ', 50);
      return fl.slice(0, cut > 15 ? cut : 50);
    }

    // Col 2: describe what Claude was doing (tools + context)
    function describeWork(r) {
      const t = (r.preceding_user||'').trim();
      const tools = r.tools_called||[];
      const toolBadges = tools.map(t => `<span class="ev-tool-badge">${esc(t)}</span>`).join('');
      const toolsHtml = toolBadges ? `<div class="req-tools" style="margin-top:4px">${toolBadges}</div>` : '';

      if (/^(CRITICAL:|Stop hook|Hook feedback)/i.test(t)) {
        return `<span style="color:var(--muted)">Automated verification check by hook system</span>${toolsHtml}`;
      }
      if (!t || /^continue\.?$/i.test(t) || /^Continue from where/i.test(t)) {
        return sessionTask
          ? `<span style="color:#8b949e">Working on: </span>${esc(sessionTask)}${toolsHtml}`
          : `<span style="color:var(--muted)">Autonomous continuation</span>${toolsHtml}`;
      }
      // Real user message — show as context (truncated)
      return `${esc(t.slice(0, 250))}${toolsHtml}`;
    }

    // Top 5 requests sorted chronologically
    const topReqs = [...(s.top_requests||[])].sort((a,b) => a.ts < b.ts ? -1 : 1);
    const tableRows = topReqs.map(r => {
      const breakdown = `in:${fmt_tokens(r.input_tokens)} cache:${fmt_tokens(r.cache_read_tokens)} out:${fmt_tokens(r.output_tokens)}`;
      const statsCell = `<div class="dt-tokens">${fmt_tokens(r.total_tokens)}</div>
        <div class="dt-cost">${fmt_cost(r.cost)} &nbsp; ${fmt_time(r.ts)}</div>
        <div class="dt-breakdown">${breakdown}</div>`;
      return `<tr>
        <td class="dt-label">${esc(reqLabel(r.preceding_user))}</td>
        <td class="dt-msg">${describeWork(r)}</td>
        <td class="dt-stats">${statsCell}</td>
      </tr>`;
    }).join('');

    const reqHtml = topReqs.length
      ? `<table class="detail-table"><thead><tr><th style="width:160px">User request</th><th>Claude's work</th><th style="width:170px">API stats</th></tr></thead><tbody>${tableRows}</tbody></table>`
      : '<div style="color:var(--muted);font-size:11px">No requests captured</div>';

    // SESSION: Claude Code UI title (from claude-code-sessions JSON)
    const sessionTitle = s.session_title ? esc(s.session_title) : `<span style="opacity:.4">${s.session_id}…</span>`;
    // CHAT: raw first message, clamped to 2 lines via CSS
    const chatShort = s.task_description ? esc(s.task_description) : '(no message)';
    tbody.innerHTML += `
    <tr style="cursor:pointer" onclick="toggleRow('${rowId}')">
      <td style="color:var(--muted)">${i+1}</td>
      <td style="min-width:140px;max-width:180px">
        <div style="color:#c9d1d9;font-size:12px;line-height:1.4;font-weight:500">${sessionTitle}</div>
        ${s.is_subagent ? '<span class="tag subagent">subagent</span>' : ''}
      </td>
      <td style="min-width:120px;max-width:200px;color:#abb2bf;font-size:11px;line-height:1.4"><div style="display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">${chatShort}</div></td>
      <td class="task-cell">
        <span class="tag ${s.task_type}">${s.task_type}</span>
      </td>
      <td style="white-space:nowrap">
        <span class="model-dot" style="background:${mColor}"></span>${short_model(s.model)}
      </td>
      <td style="white-space:nowrap;font-family:monospace">${fmt_tokens(s.total_tokens)}</td>
      <td style="white-space:nowrap;font-family:monospace" class="${cClass}">${fmt_cost(s.total_cost)}</td>
      <td style="white-space:nowrap;font-family:monospace;color:var(--green)">${fmt_cost(s.total_savings)}</td>
      <td style="text-align:center">${s.request_count}</td>
      <td>${compactBadge}</td>
      <td style="white-space:nowrap;color:var(--muted)">${fmt_dur(s.duration_min)}</td>
      <td style="white-space:nowrap;color:var(--muted)">${fmt_time(s.start_time)}</td>
    </tr>
    <tr class="detail-row" id="detail-${rowId}">
      <td class="detail-cell" colspan="${colCount}">
        <div class="detail-inner">
          <div class="detail-section">
            <h4>Top ${topReqs.length} most expensive requests</h4>
            ${reqHtml}
          </div>
        </div>
      </td>
    </tr>`;
  });
}
renderTable();

// Show floating pagination only while user is inside the sessions table
// (past the toggle-row but before the inline pagination becomes visible)
const floatBar = document.getElementById('pagination-float');
const toggleRowEl = document.querySelector('.toggle-row');
let pastToggle = false, atInlinePg = false;
const obsToggle = new IntersectionObserver(([e]) => {
  pastToggle = !e.isIntersecting && e.boundingClientRect.top < 0;
  floatBar.classList.toggle('visible', pastToggle && !atInlinePg);
}, { threshold: 0 });
const obsInline = new IntersectionObserver(([e]) => {
  atInlinePg = e.isIntersecting;
  floatBar.classList.toggle('visible', pastToggle && !atInlinePg);
}, { threshold: 0 });
obsToggle.observe(toggleRowEl);
obsInline.observe(document.getElementById('pagination-inline'));

// ─── Usage Intensity ──────────────────────────────────────────────────────────
function openPeakModal(d) {
  const timeStr = fmt_time(d.ts);
  const pctColor = d.pct >= 100 ? 'var(--red)' : 'var(--orange)';
  let sessRows = '';
  if (d.sessions && d.sessions.length) {
    sessRows = d.sessions.map(s =>
      `<div class="modal-row">
        <div class="label"><span style="color:var(--blue)">${esc(s.slug)}</span></div>
        <div>
          ${fmt_tokens(s.tokens)} tokens &nbsp;·&nbsp; <span style="color:var(--muted)">${esc(short_model(s.model))}</span>
          <div style="color:#c9d1d9;margin-top:4px;font-size:12px">${esc((s.task||'').slice(0,300))}</div>
        </div>
      </div>`
    ).join('');
  }
  openModal(`
    <h2>🔥 Peak Usage — ${timeStr}</h2>
    ${modalRow('Usage level', `<span style="color:${pctColor};font-weight:700;font-size:18px">${d.pct}%</span> of estimated plan cap`)}
    ${modalRow('Total tokens in 5h window', fmt_tokens(d.tokens))}
    <div style="margin:14px 0 8px;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:0.05em">Active Sessions During This Window</div>
    ${sessRows || '<div style="color:var(--muted);font-size:12px">No session details available</div>'}
    <div class="modal-row" style="margin-top:14px;color:var(--muted);font-size:11px">
      This is a rolling 5-hour window. Usage above 100% of estimated cap may trigger rate limiting on your subscription plan.
    </div>
  `);
}

let intensityChart = null;

function renderIntensityChart(fromDate, toDate) {
  const intensity = DATA.intensity;
  if (!intensity) return;

  const allWindows = intensity.windows || [];
  const now = new Date();
  const end = toDate || now;
  const start = fromDate || (allWindows.length > 0 ? new Date(allWindows[0].ts) : new Date(now - 7*86400000));

  // Build a lookup: ISO ts (rounded to 15min) -> window data
  const windowMap = {};
  allWindows.forEach(w => { windowMap[w.ts] = w; });

  // Decide slot size: ≤7d → 15min, ≤30d → 1h, else → 6h
  const rangeMs = end - start;
  const slotMs = rangeMs <= 7*86400000 ? 900000 : rangeMs <= 30*86400000 ? 3600000 : 21600000;

  // For coarser slots, aggregate from 15-min windows
  const aggregated = {};
  allWindows.forEach(w => {
    const d = new Date(w.ts);
    const slotKey = Math.floor(d.getTime() / slotMs) * slotMs;
    if (!aggregated[slotKey]) aggregated[slotKey] = { tokens: 0, pctMax: 0 };
    aggregated[slotKey].tokens += w.tokens;
    aggregated[slotKey].pctMax = Math.max(aggregated[slotKey].pctMax, w.pct);
  });

  // Generate full timeline from start to end
  const labels = [], pcts = [], tokens = [];
  const slotStart = Math.ceil(start.getTime() / slotMs) * slotMs;
  for (let t = slotStart; t <= end.getTime(); t += slotMs) {
    const d = new Date(t);
    const fmtLabel = slotMs < 3600000
      ? d.toLocaleDateString('en-US',{month:'short',day:'numeric'}) + ' ' + d.toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit',hour12:false})
      : slotMs < 86400000
        ? d.toLocaleDateString('en-US',{month:'short',day:'numeric'}) + ' ' + d.toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit',hour12:false})
        : d.toLocaleDateString('en-US',{month:'short',day:'numeric'});
    labels.push(fmtLabel);
    const entry = aggregated[t];
    pcts.push(entry ? entry.pctMax : 0);
    tokens.push(entry ? entry.tokens : 0);
  }

  const ctx = document.getElementById('intensityChart').getContext('2d');
  if (intensityChart) { intensityChart.destroy(); }
  intensityChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: 'Usage %',
        data: pcts,
        borderColor: pcts.map(p => p >= 100 ? '#f85149' : p >= 75 ? '#d29922' : '#58a6ff'),
        backgroundColor: (context) => {
          const chart = context.chart;
          const {ctx: c, chartArea} = chart;
          if (!chartArea) return 'rgba(88,166,255,0.1)';
          const g = c.createLinearGradient(0, chartArea.bottom, 0, chartArea.top);
          g.addColorStop(0, 'rgba(88,166,255,0.02)');
          g.addColorStop(0.75, 'rgba(88,166,255,0.08)');
          g.addColorStop(1, 'rgba(248,81,73,0.2)');
          return g;
        },
        fill: true,
        borderWidth: 1.5,
        pointRadius: 0,
        pointHitRadius: 8,
        tension: 0.3,
        segment: {
          borderColor: (ctx2) => {
            const v = pcts[ctx2.p1DataIndex];
            return v >= 100 ? '#f85149' : v >= 75 ? '#d29922' : '#58a6ff';
          }
        }
      }]
    },
    options: {
      responsive: true,
      plugins: {
        legend: { display: false },
        annotation: undefined,
        tooltip: {
          callbacks: {
            label: (ctx2) => {
              const i = ctx2.dataIndex;
              return `${pcts[i]}% of cap (${fmt_tokens(tokens[i])})`;
            }
          }
        }
      },
      scales: {
        x: {
          ticks: { color: '#8b949e', font: { size: 9 }, maxTicksLimit: 12, maxRotation: 45 },
          grid: { color: 'rgba(48,54,61,0.5)' }
        },
        y: {
          min: 0,
          suggestedMax: 110,
          ticks: { color: '#8b949e', font: { size: 10 }, callback: v => v + '%' },
          grid: { color: 'rgba(48,54,61,0.5)' }
        }
      }
    },
    plugins: [{
      id: 'thresholdLine',
      afterDraw(chart) {
        const yAxis = chart.scales.y;
        const y100 = yAxis.getPixelForValue(100);
        if (y100 === undefined || y100 < chart.chartArea.top) return;
        const ctx3 = chart.ctx;
        ctx3.save();
        ctx3.strokeStyle = '#f85149';
        ctx3.lineWidth = 1;
        ctx3.setLineDash([6, 4]);
        ctx3.beginPath();
        ctx3.moveTo(chart.chartArea.left, y100);
        ctx3.lineTo(chart.chartArea.right, y100);
        ctx3.stroke();
        ctx3.fillStyle = '#f85149';
        ctx3.font = '10px sans-serif';
        ctx3.fillText('100% cap', chart.chartArea.right - 62, y100 - 4);
        ctx3.restore();
      }
    }]
  });
}

(function() {
  const intensity = DATA.intensity;
  if (!intensity || !intensity.windows || intensity.windows.length === 0) return;

  document.getElementById('intensity-section').style.display = '';

  // Sync initial render with whatever view is active in the token usage chart
  renderIntensityChart(getViewConfig(currentView).cutoff, null);

  // Heatmap — rendered client-side from hourly_series, colored by % of hourly plan cap
  const hmCap = intensity.cap || 0;
  const hourlyCapTokens = hmCap > 0 ? hmCap / 5 : 0;
  const dayNames = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];

  function renderHeatmapRange(fromDate, toDate) {
    const grid = Array.from({length: 7}, () => new Array(24).fill(0));
    const counts = Array.from({length: 7}, () => new Array(24).fill(0));
    DATA.hourly_series.forEach(h => {
      const d = new Date(h.hour);
      if (fromDate && d < fromDate) return;
      if (toDate && d > toDate) return;
      const dow = (d.getDay() + 6) % 7;
      const hr = d.getHours();
      grid[dow][hr] += h.tokens;
      counts[dow][hr]++;
    });
    for (let d = 0; d < 7; d++) for (let h = 0; h < 24; h++)
      if (counts[d][h] > 1) grid[d][h] = Math.round(grid[d][h] / counts[d][h]);
    _drawHeatmap(grid);
  }

  function renderHeatmap(nDays) {
    const now = Date.now();
    const cutoffMs = nDays > 0 ? now - nDays * 86400000 : 0;
    const grid = Array.from({length: 7}, () => new Array(24).fill(0));
    const counts = Array.from({length: 7}, () => new Array(24).fill(0));
    DATA.hourly_series.forEach(h => {
      const d = new Date(h.hour);
      if (cutoffMs && d.getTime() < cutoffMs) return;
      const dow = (d.getDay() + 6) % 7;
      const hr = d.getHours();
      grid[dow][hr] += h.tokens;
      counts[dow][hr]++;
    });
    for (let d = 0; d < 7; d++) for (let h = 0; h < 24; h++)
      if (counts[d][h] > 1) grid[d][h] = Math.round(grid[d][h] / counts[d][h]);
    _drawHeatmap(grid);
  }

  function _drawHeatmap(grid) {
    let html = '<table class="heatmap-table"><thead><tr><th></th>';
    for (let h = 0; h < 24; h++) html += `<th>${h}</th>`;
    html += '</tr></thead><tbody>';
    for (let d = 0; d < 7; d++) {
      html += `<tr><th>${dayNames[d]}</th>`;
      for (let h = 0; h < 24; h++) {
        const v = grid[d][h];
        const pct = hourlyCapTokens > 0 ? v / hourlyCapTokens * 100 : 0;
        let color;
        if (v === 0) color = 'transparent';
        else if (pct < 30)  color = `rgba(88,166,255,${0.15 + pct/100})`;
        else if (pct < 75)  color = `rgba(210,153,34,${0.25 + pct/200})`;
        else if (pct < 100) color = `rgba(210,153,34,${0.65 + pct/400})`;
        else                color = `rgba(248,81,73,${Math.min(0.9 + (pct-100)/200, 1)})`;
        const capStr = hourlyCapTokens > 0 ? ` · ${pct.toFixed(1)}% of hrly cap` : '';
        const title = v > 0 ? `${dayNames[d]} ${h}:00 — ${fmt_tokens(v)}${capStr}` : '';
        html += `<td class="heatmap-cell" style="background:${color}" title="${title}"></td>`;
      }
      html += '</tr>';
    }
    html += '</tbody></table>';
    html += `<div style="display:flex;align-items:center;gap:8px;margin-top:10px;font-size:10px;color:var(--muted);flex-wrap:wrap">
      <span style="opacity:0.7">% of hourly cap:</span>
      <div style="display:flex;align-items:center;gap:3px"><div style="width:14px;height:14px;border-radius:3px;background:rgba(88,166,255,0.35)"></div><span>&lt;30%</span></div>
      <div style="display:flex;align-items:center;gap:3px"><div style="width:14px;height:14px;border-radius:3px;background:rgba(210,153,34,0.50)"></div><span>30–75%</span></div>
      <div style="display:flex;align-items:center;gap:3px"><div style="width:14px;height:14px;border-radius:3px;background:rgba(210,153,34,0.85)"></div><span>75–100%</span></div>
      <div style="display:flex;align-items:center;gap:3px"><div style="width:14px;height:14px;border-radius:3px;background:rgba(248,81,73,0.92)"></div><span>&gt;100%</span></div>
      <span style="margin-left:6px;opacity:0.5">— avg tokens/hr · hover for details</span>
    </div>`;
    document.getElementById('heatmap-container').innerHTML = html;
  }

  // Default: last 7 days
  const hmDefaultFrom = new Date(Date.now() - 7*86400000);
  document.getElementById('hm-from').value = hmDefaultFrom.toISOString().slice(0,10);
  renderHeatmap(7);

  document.getElementById('hm-apply').addEventListener('click', () => {
    const from = document.getElementById('hm-from').value;
    const to = document.getElementById('hm-to').value;
    const fromDate = from ? new Date(from + 'T00:00:00') : null;
    const toDate = to ? new Date(to + 'T23:59:59') : null;
    renderHeatmapRange(fromDate, toDate);
  });

  // Peak windows — rendered as event-cards with modals (matching spike/compact format)
  const peaks = intensity.peaks;
  const peakGrid = document.getElementById('peak-cards');
  const peakSection = document.getElementById('peak-events-section');
  if (peaks.length === 0) {
    peakSection.style.display = 'none';
  } else {
    peaks.forEach(p => {
      const d = new Date(p.ts);
      const timeStr = d.toLocaleDateString('en-US', {month:'short',day:'numeric'}) + ' ' + d.toLocaleTimeString('en-US', {hour:'2-digit',minute:'2-digit',hour12:false});
      const onclickData = JSON.stringify({
        ts: p.ts, pct: p.pct, tokens: p.tokens,
        sessions: (p.sessions||[]).map(s => ({slug:s.slug, tokens:s.tokens, model:s.model, task:s.task}))
      }).replace(/'/g, "&#39;");
      peakGrid.innerHTML += `<div class="event-card peak" onclick='openPeakModal(${onclickData})'>
        <div class="ev-title">🔥 ${p.pct}% usage</div>
        <div class="ev-time">${timeStr}</div>
        <div class="ev-tokens"><span style="color:var(--muted);font-size:9px">tokens </span>${fmt_tokens(p.tokens)}</div>
        <div class="ev-session">${(p.sessions||[]).map(s=>s.slug).join(', ')}</div>
      </div>`;
    });
  }
})();
</script>

<div class="container">
<div class="glossary">
  <h3>Terminology</h3>
  <div class="glossary-grid">
    <dl class="glossary-card"><dt>Session</dt><dd>One Claude Code conversation window. Each session has its own context and history. The title is set by the Claude Code sidebar.</dd></dl>
    <dl class="glossary-card"><dt>Request</dt><dd>A single API call to Claude. Every message you send (and every tool use) triggers one or more requests under the hood.</dd></dl>
    <dl class="glossary-card"><dt>Tokens</dt><dd>The unit of text Claude processes. Input = what you send; output = Claude's reply; cached = re-used context at a much lower cost.</dd></dl>
    <dl class="glossary-card"><dt>Savings</dt><dd>Cost avoided thanks to prompt caching — the dollar amount saved vs. paying full input price for every request.</dd></dl>
    <dl class="glossary-card"><dt>Spike</dt><dd>An hour where token usage was &gt;2 standard deviations above the session average. Usually large file reads or many rapid requests.</dd></dl>
    <dl class="glossary-card"><dt>Compact</dt><dd>Context compaction: Claude auto-summarised history when nearing the context limit. Token count shown is the size just before compaction.</dd></dl>
    <dl class="glossary-card"><dt>Session task</dt><dd>The first user message of a session — used as a short label to identify what you were working on.</dd></dl>
    <dl class="glossary-card"><dt>Cost</dt><dd>Theoretical cost calculated from token counts × standard API rates (e.g. $15/M for Opus input). If you use a Claude subscription plan, this is <em>not</em> your actual charge — it shows what the same usage would cost on pay-per-token API billing.</dd></dl>
  </div>
</div>
</div>

</body>
</html>
"""


# ─── Build ────────────────────────────────────────────────────────────────────

def build_html(days: int, cap: int = 0) -> str:
    sessions, hourly = parse_projects(days)
    data = aggregate(sessions, hourly, days, cap)
    return HTML_TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False, separators=(',', ':')))


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate Claude Code token usage dashboard")
    parser.add_argument("--days", type=int, default=7, help="Number of days back to load (default: 7)")
    parser.add_argument("--port", type=int, default=8765, help="Local server port (default: 8765)")
    parser.add_argument("--output", type=str, default=None, help="Write static HTML to file and exit (skips server)")
    parser.add_argument("--plan", type=str, default="max5x", choices=["pro", "max5x", "max20x"],
                        help="Subscription plan for usage intensity tracking (default: max5x)")
    parser.add_argument("--cap", type=int, default=None, help="Custom token cap per 5h window (overrides --plan)")
    parser.add_argument("--open", action="store_true", default=True, help="Auto-open in browser (default: true)")
    parser.add_argument("--no-open", dest="open", action="store_false", help="Do not auto-open")
    args = parser.parse_args()

    cap = args.cap if args.cap else PLAN_CAPS.get(args.plan, 0)

    # Static file mode (--output): write once and exit
    if args.output:
        print(f"Generating static dashboard for last {args.days} day(s)...")
        html = build_html(args.days, cap)
        Path(args.output).write_text(html, encoding="utf-8")
        print(f"✅ Written to: {args.output}")
        if args.open:
            subprocess.Popen(["open", args.output], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return

    # Server mode (default): regenerate data on every Cmd+R
    days = args.days

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            html = build_html(days, cap)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *a):
            pass  # suppress per-request logs

    url = f"http://localhost:{args.port}"
    server = http.server.HTTPServer(("localhost", args.port), Handler)
    print(f"✅ Dashboard server running at {url}")
    print(f"   Cmd+R refreshes data live from ~/.claude/projects/")
    print(f"   Ctrl+C to stop")

    if args.open:
        subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
