import json

import aiosqlite

from station.services.world import get_tick, get_active_effects, _format_activity


async def get_dashboard_data(db: aiosqlite.Connection) -> dict:
    tick = await get_tick(db)
    effects = await get_active_effects(db)

    # --- Price history (all items, last 100 ticks) ---
    items_rows = await db.execute_fetchall(
        "SELECT name, display_name, base_price, current_price, category FROM items ORDER BY category, name"
    )
    items_by_name = {r["name"]: dict(r) for r in items_rows}

    ph_rows = await db.execute_fetchall(
        "SELECT tick, item_name, price FROM price_history WHERE tick >= ? ORDER BY item_name, tick",
        (max(0, tick - 100),),
    )
    price_history = {}
    for r in ph_rows:
        name = r["item_name"]
        if name not in price_history:
            item = items_by_name.get(name, {})
            price_history[name] = {
                "displayName": item.get("display_name", name),
                "category": item.get("category", "raw"),
                "basePrice": f"{item.get('base_price', 0):.2f}",
                "currentPrice": f"{item.get('current_price', 0):.2f}",
                "history": [],
            }
        price_history[name]["history"].append({"tick": r["tick"], "price": round(r["price"], 2)})

    # Items with no history yet still get an entry
    for name, item in items_by_name.items():
        if name not in price_history:
            price_history[name] = {
                "displayName": item["display_name"],
                "category": item["category"],
                "basePrice": f"{item['base_price']:.2f}",
                "currentPrice": f"{item['current_price']:.2f}",
                "history": [],
            }

    # --- Recent activity (last 50 non-hidden) ---
    activity_rows = await db.execute_fetchall(
        "SELECT * FROM activity_log ORDER BY timestamp DESC LIMIT 100"
    )
    activity = _format_activity(activity_rows)[:50]

    # --- Sectors with agents ---
    sector_rows = await db.execute_fetchall("SELECT name, display_name, bonus FROM sectors ORDER BY name")
    sectors = []
    for s in sector_rows:
        agents_in = await db.execute_fetchall(
            "SELECT name, reputation FROM agents WHERE sector = ?", (s["name"],)
        )
        sectors.append({
            "name": s["name"],
            "displayName": s["display_name"],
            "bonus": s["bonus"],
            "agentCount": len(agents_in),
            "agents": [{"name": a["name"], "reputation": a["reputation"]} for a in agents_in],
        })

    # --- Leaderboard with net worth ---
    leaderboard_rows = await db.execute_fetchall("""
        SELECT a.name, a.credits, a.reputation, a.sector, a.last_action_at,
               COALESCE(SUM(inv.quantity * i.current_price), 0) as inventory_value
        FROM agents a
        LEFT JOIN inventory inv ON inv.agent_id = a.id AND inv.quantity > 0
        LEFT JOIN items i ON i.name = inv.item_name
        GROUP BY a.id
        ORDER BY (a.credits + COALESCE(SUM(inv.quantity * i.current_price), 0)) DESC
        LIMIT 20
    """)
    leaderboard = []
    for r in leaderboard_rows:
        credits = float(r["credits"])
        inv_val = float(r["inventory_value"] or 0)
        leaderboard.append({
            "name": r["name"],
            "credits": f"{credits:.2f}",
            "inventoryValue": f"{inv_val:.2f}",
            "netWorth": f"{credits + inv_val:.2f}",
            "reputation": r["reputation"],
            "sector": r["sector"],
        })

    # --- Inventory breakdown for top agents ---
    inventories = {}
    for entry in leaderboard[:10]:
        agent_row = await db.execute_fetchall("SELECT id FROM agents WHERE name = ?", (entry["name"],))
        if not agent_row:
            continue
        aid = agent_row[0]["id"]
        inv_rows = await db.execute_fetchall("""
            SELECT inv.item_name, i.display_name, inv.quantity, i.current_price,
                   (inv.quantity * i.current_price) as value
            FROM inventory inv
            JOIN items i ON i.name = inv.item_name
            WHERE inv.agent_id = ? AND inv.quantity > 0 AND inv.is_counterfeit = 0
            ORDER BY value DESC
        """, (aid,))
        if inv_rows:
            inventories[entry["name"]] = [
                {"item": r["display_name"], "qty": r["quantity"], "value": f"{r['value']:.2f}"}
                for r in inv_rows
            ]

    # --- Recent events ---
    event_rows = await db.execute_fetchall(
        "SELECT id, event_type, description, tick_number, created_at FROM events ORDER BY tick_number DESC LIMIT 20"
    )
    events = [
        {
            "id": e["id"],
            "eventType": e["event_type"],
            "description": e["description"],
            "tick": e["tick_number"],
            "createdAt": e["created_at"],
        }
        for e in event_rows
    ]

    # --- Contracts summary ---
    contract_counts = await db.execute_fetchall("""
        SELECT status, COUNT(*) as count FROM contracts
        WHERE status IN ('open', 'active', 'deciding')
        GROUP BY status
    """)
    contract_summary = {r["status"]: r["count"] for r in contract_counts}

    recent_contracts = await db.execute_fetchall("""
        SELECT c.id, c.status, r.display_name as recipe, a.name as proposer
        FROM contracts c
        JOIN recipes r ON r.name = c.recipe_name
        JOIN agents a ON a.id = c.proposer_id
        WHERE c.status IN ('open', 'active', 'deciding')
        ORDER BY c.created_at DESC LIMIT 10
    """)
    contracts = {
        "summary": contract_summary,
        "recent": [
            {"id": r["id"], "recipe": r["recipe"], "proposer": r["proposer"], "status": r["status"]}
            for r in recent_contracts
        ],
    }

    # --- Bounties summary ---
    bounty_stats = await db.execute_fetchall("""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN cooperative = 1 THEN 1 ELSE 0 END) as coop,
               SUM(reward_credits) as total_rewards
        FROM bounties
        WHERE claimed_by IS NULL AND expires_tick > ?
    """, (tick,))
    top_bounties = await db.execute_fetchall("""
        SELECT id, description, reward_credits, cooperative
        FROM bounties
        WHERE claimed_by IS NULL AND expires_tick > ?
        ORDER BY reward_credits DESC LIMIT 5
    """, (tick,))
    bounties = {
        "active": bounty_stats[0]["total"] if bounty_stats else 0,
        "cooperativeActive": bounty_stats[0]["coop"] if bounty_stats else 0,
        "totalRewards": f"{bounty_stats[0]['total_rewards'] or 0:.2f}" if bounty_stats else "0.00",
        "top": [
            {"id": r["id"], "description": r["description"], "reward": f"{r['reward_credits']:.2f}", "cooperative": bool(r["cooperative"])}
            for r in top_bounties
        ],
    }

    # --- Comms feed (broadcasts + DMs, excluding Station system messages) ---
    comms_rows = await db.execute_fetchall("""
        SELECT id, from_agent, to_agent, content, sector, msg_type, created_at
        FROM messages
        WHERE from_agent != 'Station'
        ORDER BY created_at DESC LIMIT 30
    """)
    comms = [
        {
            "id": r["id"],
            "from": r["from_agent"],
            "to": r["to_agent"],
            "content": r["content"],
            "sector": r["sector"],
            "type": r["msg_type"],
            "createdAt": r["created_at"],
        }
        for r in comms_rows
    ]

    # --- Agent count ---
    agent_count_row = await db.execute_fetchall("SELECT COUNT(*) as c FROM agents")
    agent_count = agent_count_row[0]["c"] if agent_count_row else 0

    return {
        "success": True,
        "tick": tick,
        "agentCount": agent_count,
        "activeEffects": effects,
        "priceHistory": price_history,
        "activity": activity,
        "sectors": sectors,
        "leaderboard": leaderboard,
        "inventories": inventories,
        "events": events,
        "contracts": contracts,
        "bounties": bounties,
        "comms": comms,
    }
