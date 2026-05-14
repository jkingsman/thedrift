import json
from datetime import datetime, timezone

import aiosqlite


def _time_ago(iso_str: str | None) -> str:
    if not iso_str:
        return "never"
    try:
        then = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - then
        total = int(delta.total_seconds())
        if total < 0:
            return "just now"
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        if h > 0:
            return f"{h}h{m:02d}m{s:02d}s"
        elif m > 0:
            return f"{m}m{s:02d}s"
        else:
            return f"{s}s"
    except (ValueError, TypeError):
        return "unknown"
from station.db import new_id
from station.services.market import STATION_BLOCKED_CATEGORIES


async def get_tick(db: aiosqlite.Connection) -> int:
    rows = await db.execute_fetchall("SELECT value FROM world_state WHERE key = 'tick'")
    return int(rows[0]["value"]) if rows else 0


async def increment_tick(db: aiosqlite.Connection) -> int:
    tick = await get_tick(db) + 1
    await db.execute("UPDATE world_state SET value = ? WHERE key = 'tick'", (str(tick),))
    return tick


async def get_active_effects(db: aiosqlite.Connection) -> dict:
    rows = await db.execute_fetchall("SELECT value FROM world_state WHERE key = 'active_effects'")
    if rows:
        return json.loads(rows[0]["value"])
    return {}


async def set_active_effects(db: aiosqlite.Connection, effects: dict):
    await db.execute(
        "UPDATE world_state SET value = ? WHERE key = 'active_effects'",
        (json.dumps(effects),),
    )


async def get_world_state(db: aiosqlite.Connection) -> dict:
    tick = await get_tick(db)

    # Items
    items = await db.execute_fetchall("SELECT * FROM items ORDER BY category, name")
    item_list = [
        {
            "name": i["name"],
            "displayName": i["display_name"],
            "description": i["description"],
            "basePrice": f"{i['base_price']:.2f}",
            "currentPrice": f"{i['current_price']:.2f}",
            "supply": f"{i['supply']:.0f}",
            "category": i["category"],
        }
        for i in items
    ]

    # Sectors
    sectors = await db.execute_fetchall("SELECT * FROM sectors ORDER BY name")
    sector_list = []
    for s in sectors:
        agents = await db.execute_fetchall(
            "SELECT name FROM agents WHERE sector = ?", (s["name"],)
        )
        sector_list.append({
            "name": s["name"],
            "displayName": s["display_name"],
            "bonus": s["bonus"],
            "agents": [a["name"] for a in agents],
        })

    # Agents
    agents = await db.execute_fetchall("SELECT name, sector, reputation FROM agents ORDER BY name")
    agent_list = [{"name": a["name"], "sector": a["sector"], "reputation": a["reputation"], "lastSeen": _time_ago(dict(a).get("last_action_at"))} for a in agents]

    # Recent events
    events = await db.execute_fetchall("SELECT * FROM events ORDER BY tick_number DESC LIMIT 5")
    event_list = [{"type": e["event_type"], "description": e["description"], "tick": e["tick_number"]} for e in events]

    # Active contracts
    contracts = await db.execute_fetchall("SELECT COUNT(*) as c FROM contracts WHERE status IN ('open', 'deciding')")
    open_contracts = contracts[0]["c"]

    # Active effects
    effects = await get_active_effects(db)

    return {
        "success": True,
        "world": {
            "stationName": "The Drift",
            "tickNumber": tick,
            "activeEffects": effects,
            "items": item_list,
            "sectors": sector_list,
            "agents": agent_list,
            "recentEvents": event_list,
            "agentCount": len(agent_list),
            "openContracts": open_contracts,
        },
    }


async def get_market(db: aiosqlite.Connection) -> dict:
    rows = await db.execute_fetchall("SELECT * FROM items ORDER BY category, name")
    return {
        "success": True,
        "market": [
            {
                "name": i["name"],
                "displayName": i["display_name"],
                "currentPrice": f"{i['current_price']:.2f}",
                "basePrice": f"{i['base_price']:.2f}",
                "category": i["category"],
                "stationAvailable": i["category"] not in STATION_BLOCKED_CATEGORIES,
                "stationBuyPrice": f"{i['current_price'] * 1.10:.2f}",
                "stationSellPrice": f"{i['current_price'] * 0.90:.2f}",
                "exchangeBuyPrice": f"{i['current_price'] * 1.05:.2f}",
                "exchangeSellPrice": f"{i['current_price'] * 0.95:.2f}",
            }
            for i in rows
        ],
    }


async def get_sectors(db: aiosqlite.Connection) -> dict:
    sectors = await db.execute_fetchall("SELECT * FROM sectors ORDER BY name")
    result = []
    for s in sectors:
        agents = await db.execute_fetchall(
            "SELECT name, reputation FROM agents WHERE sector = ?", (s["name"],)
        )
        result.append({
            "name": s["name"],
            "displayName": s["display_name"],
            "description": s["description"],
            "bonus": s["bonus"],
            "agents": [{"name": a["name"], "reputation": a["reputation"]} for a in agents],
        })
    return {"success": True, "sectors": result}


async def get_events(db: aiosqlite.Connection) -> dict:
    rows = await db.execute_fetchall("SELECT * FROM events ORDER BY tick_number DESC LIMIT 20")
    return {
        "success": True,
        "events": [
            {
                "id": e["id"],
                "eventType": e["event_type"],
                "description": e["description"],
                "effects": json.loads(e["effects"]) if isinstance(e["effects"], str) else e["effects"],
                "tickNumber": e["tick_number"],
                "createdAt": e["created_at"],
            }
            for e in rows
        ],
    }


async def get_leaderboard(db: aiosqlite.Connection) -> dict:
    wealthiest = await db.execute_fetchall(
        "SELECT name, credits FROM agents ORDER BY credits DESC LIMIT 10"
    )
    most_rep = await db.execute_fetchall(
        "SELECT name, reputation FROM agents ORDER BY reputation DESC LIMIT 10"
    )
    return {
        "success": True,
        "leaderboard": {
            "wealthiest": [{"name": w["name"], "credits": f"{w['credits']:.2f}"} for w in wealthiest],
            "mostReputable": [{"name": r["name"], "reputation": r["reputation"]} for r in most_rep],
        },
    }


HIDDEN_ACTIONS = {"message", "read_messages", "broadcast", "status", "view_bounties", "view_contracts", "scan", "read_messages"}


def _summarize_action(action: str, params: dict, result: dict) -> str:
    """Generate a one-line public summary of an action. No strategic details."""
    success = result.get("success", True)
    if not success:
        return f"attempted {action} (failed)"

    match action:
        case "explore":
            return "explored their sector"
        case "move":
            return f"moved to {params.get('sector', 'a new sector')}"
        case "craft":
            crafted = result.get("data", {}).get("crafted", "an item")
            return f"crafted {crafted}"
        case "buy":
            item = result.get("data", {}).get("item", "something")
            return f"bought {item} from the station"
        case "sell":
            item = result.get("data", {}).get("item", "something")
            return f"sold {item} to the station"
        case "list_item":
            return "listed an item for sale"
        case "buy_listing":
            return "bought an item from another agent"
        case "rumor":
            item = result.get("data", {}).get("item", "something")
            return f"spread intel about {item}"
        case "propose_contract":
            recipe = result.get("data", {}).get("recipe", "something")
            return f"proposed a contract for {recipe}"
        case "join_contract":
            return "joined a contract"
        case "fulfill":
            return "fulfilled a contract"
        case "betray":
            return "betrayed a contract"
        case "complete_bounty":
            return "completed a bounty"
        case "sabotage":
            return "attempted sabotage"
        case "forge":
            return "did something shady in Scrap Alley"
        case "research":
            return "conducted research"
        case _:
            return action


def _format_activity(rows) -> list[dict]:
    entries = []
    for r in rows:
        if r["action"] in HIDDEN_ACTIONS:
            continue
        result = json.loads(r["result"]) if isinstance(r["result"], str) else r["result"]
        params = json.loads(r["params"]) if isinstance(r["params"], str) else r["params"]
        entries.append({
            "agentName": r["agent_name"],
            "description": _summarize_action(r["action"], params, result),
            "action": r["action"],
            "tick": r["tick"],
            "timestamp": r["timestamp"],
        })
    return entries


async def get_activity(
    db: aiosqlite.Connection,
    agent: str | None = None,
    action: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    query = "SELECT * FROM activity_log"
    conditions = []
    params = []

    if agent:
        conditions.append("agent_name = ?")
        params.append(agent)
    if action:
        conditions.append("action = ?")
        params.append(action)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
    params.extend([min(limit, 200), offset])

    rows = await db.execute_fetchall(query, params)

    # Get total count for pagination
    count_query = "SELECT COUNT(*) as total FROM activity_log"
    if conditions:
        count_query += " WHERE " + " AND ".join(conditions[:len(conditions)])
    count_rows = await db.execute_fetchall(count_query, params[:len(conditions)])
    total = count_rows[0]["total"] if count_rows else 0

    return {
        "success": True,
        "total": total,
        "limit": min(limit, 200),
        "offset": offset,
        "activity": _format_activity(rows),
    }


async def get_agent_history(db: aiosqlite.Connection, agent_name: str, limit: int = 50) -> dict:
    """Full detail — only accessible by the agent themselves (auth-gated)."""
    rows = await db.execute_fetchall(
        "SELECT * FROM activity_log WHERE agent_name = ? ORDER BY timestamp DESC LIMIT ?",
        (agent_name, min(limit, 200)),
    )
    return {
        "success": True,
        "agent": agent_name,
        "actionCount": len(rows),
        "history": [
            {
                "action": r["action"],
                "params": json.loads(r["params"]) if isinstance(r["params"], str) else r["params"],
                "result": json.loads(r["result"]) if isinstance(r["result"], str) else r["result"],
                "tick": r["tick"],
                "timestamp": r["timestamp"],
            }
            for r in rows
        ],
    }


async def log_activity(db: aiosqlite.Connection, agent_name: str, action: str, params: dict, result: dict, tick: int):
    await db.execute(
        "INSERT INTO activity_log (id, agent_name, action, params, result, tick) VALUES (?,?,?,?,?,?)",
        (new_id(), agent_name, action, json.dumps(params), json.dumps(result), tick),
    )
