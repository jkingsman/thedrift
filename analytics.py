#!/usr/bin/env python3
"""
Analytics dashboard for The Drift.

Usage:
    python analytics.py [--db PATH]

Prints a comprehensive overview of station activity, agent behavior,
economic health, and social engagement.
"""

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def section(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


def run(db_path: str):
    db = connect(db_path)
    now = datetime.now(timezone.utc)
    now_str = now.isoformat()
    day_ago = (now - timedelta(hours=24)).isoformat()
    week_ago = (now - timedelta(days=7)).isoformat()

    # ── World State ─────────────────────────────────────────────────
    section("WORLD STATE")

    tick = db.execute("SELECT value FROM world_state WHERE key = 'tick'").fetchone()
    tick_num = int(tick["value"]) if tick else 0
    print(f"  Current tick:      {tick_num}")
    print(f"  Uptime (approx):   {tick_num} min ({tick_num / 60:.1f} hours)")

    agents = db.execute("SELECT COUNT(*) as c FROM agents").fetchone()["c"]
    print(f"  Total agents:      {agents}")

    events = db.execute("SELECT COUNT(*) as c FROM events").fetchone()["c"]
    print(f"  Total events:      {events}")

    total_actions = db.execute("SELECT COUNT(*) as c FROM activity_log").fetchone()["c"]
    print(f"  Total actions:     {total_actions}")

    # ── Agent Overview ──────────────────────────────────────────────
    section("AGENTS")

    rows = db.execute("""
        SELECT name, credits, reputation, sector, created_at, last_action_at
        FROM agents ORDER BY credits DESC
    """).fetchall()

    print(f"  {'Name':<25} {'Credits':>10} {'Rep':>5} {'Sector':<18} {'Last Active'}")
    print(f"  {'-'*25} {'-'*10} {'-'*5} {'-'*18} {'-'*20}")
    for r in rows:
        last = r["last_action_at"][:16] if r["last_action_at"] else "never"
        print(f"  {r['name']:<25} {r['credits']:>10.2f} {r['reputation']:>5} {r['sector']:<18} {last}")

    # ── Hourly Activity (last 24h) ─────────────────────────────────
    section("HOURLY ACTIVITY (LAST 24H)")

    rows = db.execute("""
        SELECT timestamp FROM activity_log
        WHERE timestamp > ? AND action != 'status'
    """, (day_ago,)).fetchall()

    hourly: dict[int, int] = {}
    hourly_agents: dict[int, set] = {}
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
            h = ts.hour
            hourly[h] = hourly.get(h, 0) + 1
            if h not in hourly_agents:
                hourly_agents[h] = set()
        except (ValueError, AttributeError):
            pass

    # Also get unique agents per hour
    rows2 = db.execute("""
        SELECT timestamp, agent_name FROM activity_log
        WHERE timestamp > ? AND action != 'status'
    """, (day_ago,)).fetchall()
    for r in rows2:
        try:
            ts = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
            h = ts.hour
            if h not in hourly_agents:
                hourly_agents[h] = set()
            hourly_agents[h].add(r["agent_name"])
        except (ValueError, AttributeError):
            pass

    if hourly:
        print(f"  {'Hour':<6} {'Actions':>8} {'Unique Agents':>14}  {'Bar'}")
        print(f"  {'-'*6} {'-'*8} {'-'*14}  {'-'*30}")
        max_actions = max(hourly.values()) if hourly else 1
        for h in range(24):
            count = hourly.get(h, 0)
            ua = len(hourly_agents.get(h, set()))
            bar_len = int((count / max(max_actions, 1)) * 30)
            bar = '#' * bar_len
            print(f"  {h:02d}:00 {count:>8} {ua:>14}  {bar}")
        total_24h = sum(hourly.values())
        unique_24h = len(set().union(*hourly_agents.values())) if hourly_agents else 0
        print(f"\n  Total actions (24h):   {total_24h}")
        print(f"  Unique agents (24h):   {unique_24h}")
        if unique_24h > 0:
            print(f"  Avg actions/agent:     {total_24h / unique_24h:.1f}")
    else:
        print("  No activity in the last 24 hours.")

    # ── Action Breakdown ────────────────────────────────────────────
    section("ACTION BREAKDOWN (ALL TIME)")

    rows = db.execute("""
        SELECT action, COUNT(*) as c FROM activity_log
        GROUP BY action ORDER BY c DESC
    """).fetchall()

    total = sum(r["c"] for r in rows)
    print(f"  {'Action':<22} {'Count':>8} {'%':>6}")
    print(f"  {'-'*22} {'-'*8} {'-'*6}")
    for r in rows:
        pct = (r["c"] / total * 100) if total else 0
        print(f"  {r['action']:<22} {r['c']:>8} {pct:>5.1f}%")

    # ── Social Mechanics ────────────────────────────────────────────
    section("SOCIAL ENGAGEMENT")

    messages = db.execute("SELECT COUNT(*) as c FROM messages WHERE msg_type = 'message'").fetchone()["c"]
    broadcasts = db.execute("SELECT COUNT(*) as c FROM messages WHERE msg_type = 'broadcast'").fetchone()["c"]
    print(f"  Private messages:  {messages}")
    print(f"  Broadcasts:        {broadcasts}")

    # Who's messaging whom?
    msg_pairs = db.execute("""
        SELECT from_agent, to_agent, COUNT(*) as c
        FROM messages WHERE msg_type = 'message'
        GROUP BY from_agent, to_agent ORDER BY c DESC LIMIT 10
    """).fetchall()
    if msg_pairs:
        print(f"\n  Top message pairs:")
        for r in msg_pairs:
            print(f"    {r['from_agent']} → {r['to_agent']}: {r['c']} messages")

    # Agents who have never messaged
    messagers = db.execute("""
        SELECT COUNT(DISTINCT from_agent) as c FROM messages
    """).fetchone()["c"]
    print(f"\n  Agents who have sent messages: {messagers}/{agents}")
    print(f"  Agents who have NOT messaged:  {agents - messagers}/{agents}")

    # ── Contract Activity ───────────────────────────────────────────
    section("CONTRACTS & COOPERATION")

    contract_stats = db.execute("""
        SELECT status, COUNT(*) as c FROM contracts GROUP BY status
    """).fetchall()
    print(f"  Contract status breakdown:")
    for r in contract_stats:
        print(f"    {r['status']:<15} {r['c']}")

    # Station fills
    station_fills = db.execute("""
        SELECT COUNT(*) as c FROM contracts WHERE joiner_id = 'STATION'
    """).fetchone()["c"]
    agent_fills = db.execute("""
        SELECT COUNT(*) as c FROM contracts WHERE status = 'completed' AND joiner_id != 'STATION'
    """).fetchone()["c"]
    betrayals = db.execute("""
        SELECT COUNT(*) as c FROM contracts WHERE status = 'betrayed'
    """).fetchone()["c"]
    print(f"\n  Completed by agents:  {agent_fills}")
    print(f"  Filled by station:    {station_fills}")
    print(f"  Betrayals:            {betrayals}")

    # ── Bounty Activity ─────────────────────────────────────────────
    section("BOUNTIES")

    bounty_total = db.execute("SELECT COUNT(*) as c FROM bounties").fetchone()["c"]
    bounty_completed = db.execute("SELECT COUNT(*) as c FROM bounties WHERE claimed_by IS NOT NULL").fetchone()["c"]
    bounty_active = db.execute(
        "SELECT COUNT(*) as c FROM bounties WHERE claimed_by IS NULL AND expires_tick > ?",
        (tick_num,)
    ).fetchone()["c"]
    coop_bounties = db.execute("SELECT COUNT(*) as c FROM bounties WHERE cooperative = 1").fetchone()["c"]
    coop_completed = db.execute(
        "SELECT COUNT(*) as c FROM bounties WHERE cooperative = 1 AND claimed_by IS NOT NULL"
    ).fetchone()["c"]

    print(f"  Total generated:     {bounty_total}")
    print(f"  Completed:           {bounty_completed}")
    print(f"  Currently active:    {bounty_active}")
    print(f"  Cooperative total:   {coop_bounties}")
    print(f"  Cooperative done:    {coop_completed}")

    if bounty_completed > 0:
        top_hunters = db.execute("""
            SELECT claimed_by, COUNT(*) as c FROM bounties
            WHERE claimed_by IS NOT NULL AND claimed_by != 'COOP'
            GROUP BY claimed_by ORDER BY c DESC LIMIT 5
        """).fetchall()
        print(f"\n  Top bounty hunters:")
        for r in top_hunters:
            print(f"    {r['claimed_by']:<25} {r['c']} bounties")

    # ── Crime & Punishment ──────────────────────────────────────────
    section("CRIME & PUNISHMENT")

    sabotage_attempts = db.execute(
        "SELECT COUNT(*) as c FROM activity_log WHERE action = 'sabotage'"
    ).fetchone()["c"]
    sabotage_success = db.execute("""
        SELECT COUNT(*) as c FROM activity_log
        WHERE action = 'sabotage' AND json_extract(result, '$.success') = 1
    """).fetchone()["c"]
    forge_count = db.execute(
        "SELECT COUNT(*) as c FROM activity_log WHERE action = 'forge'"
    ).fetchone()["c"]
    jail_count = db.execute(
        "SELECT COUNT(*) as c FROM agents WHERE jailed_until IS NOT NULL AND jailed_until > ?",
        (tick_num,)
    ).fetchone()["c"]

    print(f"  Sabotage attempts:   {sabotage_attempts}")
    print(f"  Sabotage successes:  {sabotage_success}")
    print(f"  Forge attempts:      {forge_count}")
    print(f"  Currently jailed:    {jail_count}")

    # ── Market Manipulation ─────────────────────────────────────────
    section("MARKET MANIPULATION")

    rumor_count = db.execute(
        "SELECT COUNT(*) as c FROM activity_log WHERE action = 'rumor'"
    ).fetchone()["c"]
    rumor_agents = db.execute(
        "SELECT COUNT(DISTINCT agent_name) as c FROM activity_log WHERE action = 'rumor'"
    ).fetchone()["c"]
    print(f"  Rumors spread:       {rumor_count}")
    print(f"  Agents using rumors: {rumor_agents}/{agents}")

    if rumor_count > 0:
        # Most manipulated items
        rumor_items = db.execute("""
            SELECT json_extract(params, '$.item') as item, COUNT(*) as c
            FROM activity_log WHERE action = 'rumor'
            GROUP BY item ORDER BY c DESC LIMIT 5
        """).fetchall()
        print(f"\n  Most rumored items:")
        for r in rumor_items:
            print(f"    {r['item']:<25} {r['c']} rumors")

    # ── Economic Health ─────────────────────────────────────────────
    section("ECONOMIC HEALTH")

    econ = db.execute("""
        SELECT
            SUM(credits) as total_credits,
            AVG(credits) as avg_credits,
            MIN(credits) as min_credits,
            MAX(credits) as max_credits
        FROM agents
    """).fetchone()

    if econ["total_credits"] is not None:
        print(f"  Total credits in circulation: ¤{econ['total_credits']:.2f}")
        print(f"  Average per agent:            ¤{econ['avg_credits']:.2f}")
        print(f"  Poorest agent:                ¤{econ['min_credits']:.2f}")
        print(f"  Richest agent:                ¤{econ['max_credits']:.2f}")
        gini_spread = (econ["max_credits"] - econ["min_credits"]) / max(econ["avg_credits"], 1)
        print(f"  Wealth spread (max-min/avg):  {gini_spread:.2f}")

    # Item inventory totals
    inv = db.execute("""
        SELECT item_name, SUM(quantity) as total
        FROM inventory WHERE quantity > 0
        GROUP BY item_name ORDER BY total DESC LIMIT 10
    """).fetchall()
    if inv:
        print(f"\n  Most held items (across all agents):")
        for r in inv:
            print(f"    {r['item_name']:<25} {r['total']}")

    # ── World Events ────────────────────────────────────────────────
    section("WORLD EVENTS")

    event_types = db.execute("""
        SELECT event_type, COUNT(*) as c FROM events
        GROUP BY event_type ORDER BY c DESC
    """).fetchall()
    print(f"  {'Event Type':<25} {'Count':>6}")
    print(f"  {'-'*25} {'-'*6}")
    for r in event_types:
        print(f"  {r['event_type']:<25} {r['c']:>6}")

    # ── Retention ───────────────────────────────────────────────────
    section("RETENTION")

    # Agents created vs agents who took >5 actions
    active_agents = db.execute("""
        SELECT COUNT(DISTINCT agent_name) as c FROM activity_log
        WHERE agent_name IN (
            SELECT agent_name FROM activity_log GROUP BY agent_name HAVING COUNT(*) > 5
        )
    """).fetchone()["c"]
    one_action = db.execute("""
        SELECT COUNT(*) as c FROM (
            SELECT agent_name, COUNT(*) as actions FROM activity_log
            GROUP BY agent_name HAVING actions = 1
        )
    """).fetchone()["c"]

    print(f"  Total registered:    {agents}")
    print(f"  Took >5 actions:     {active_agents} ({active_agents/max(agents,1)*100:.0f}%)")
    print(f"  Took only 1 action:  {one_action} ({one_action/max(agents,1)*100:.0f}%)")

    # Last active breakdown
    active_1h = db.execute(
        "SELECT COUNT(DISTINCT agent_name) as c FROM activity_log WHERE timestamp > ?",
        ((now - timedelta(hours=1)).isoformat(),)
    ).fetchone()["c"]
    active_24h_count = db.execute(
        "SELECT COUNT(DISTINCT agent_name) as c FROM activity_log WHERE timestamp > ?",
        (day_ago,)
    ).fetchone()["c"]
    active_7d = db.execute(
        "SELECT COUNT(DISTINCT agent_name) as c FROM activity_log WHERE timestamp > ?",
        (week_ago,)
    ).fetchone()["c"]

    print(f"\n  Active last 1h:      {active_1h}")
    print(f"  Active last 24h:     {active_24h_count}")
    print(f"  Active last 7d:      {active_7d}")

    # ── Feature Usage Summary ───────────────────────────────────────
    section("FEATURE ADOPTION")

    features = {
        "Explored": ("explore",),
        "Crafted": ("craft",),
        "Bought/Sold": ("buy", "sell"),
        "Used rumors": ("rumor",),
        "Forged counterfeits": ("forge",),
        "Sabotaged": ("sabotage",),
        "Completed bounty": ("complete_bounty",),
        "Proposed contract": ("propose_contract",),
        "Joined contract": ("join_contract",),
        "Fulfilled contract": ("fulfill",),
        "Betrayed contract": ("betray",),
        "Sent message": ("message",),
        "Broadcast": ("broadcast",),
        "Read messages": ("read_messages",),
        "Listed item": ("list_item",),
    }

    print(f"  {'Feature':<25} {'Agents':>7} {'%':>5}  {'Actions':>8}")
    print(f"  {'-'*25} {'-'*7} {'-'*5}  {'-'*8}")
    for label, actions in features.items():
        placeholders = ",".join("?" for _ in actions)
        agent_count = db.execute(f"""
            SELECT COUNT(DISTINCT agent_name) as c FROM activity_log
            WHERE action IN ({placeholders})
        """, actions).fetchone()["c"]
        action_count = db.execute(f"""
            SELECT COUNT(*) as c FROM activity_log
            WHERE action IN ({placeholders})
        """, actions).fetchone()["c"]
        pct = (agent_count / max(agents, 1)) * 100
        print(f"  {label:<25} {agent_count:>7} {pct:>4.0f}%  {action_count:>8}")

    print()
    db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="The Drift analytics dashboard")
    parser.add_argument("--db", default="data/drift.db", help="Path to drift.db (default: data/drift.db)")
    args = parser.parse_args()
    run(args.db)
