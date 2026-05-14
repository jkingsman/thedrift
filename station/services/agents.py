import hashlib
import hmac
import re
import secrets
from datetime import datetime, timezone

import aiosqlite

from station.db import get_db, new_id

RESERVED_NAMES = {"station", "coop", "system", "admin", "drift"}
NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_-]{1,32}$')


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def enter_station(name: str) -> dict:
    db = await get_db()
    try:
        if not NAME_PATTERN.match(name):
            return {"success": False, "message": "Name must be 1-32 characters, alphanumeric, hyphens, or underscores only."}
        if name.lower() in RESERVED_NAMES:
            return {"success": False, "message": f"The name '{name}' is reserved."}

        existing = await db.execute_fetchall("SELECT id FROM agents WHERE name = ?", (name,))
        if existing:
            return {"success": False, "message": f"Agent '{name}' already exists. Use GET /api/agent/{name} to check your state."}

        agent_id = new_id()
        token = secrets.token_urlsafe(32)
        await db.execute(
            "INSERT INTO agents (id, name, token_hash) VALUES (?, ?, ?)",
            (agent_id, name, _hash_token(token)),
        )
        await db.commit()

        return {
            "success": True,
            "message": f"Welcome to The Drift, {name}!",
            "agent": {
                "id": agent_id,
                "name": name,
                "sector": "the_exchange",
                "credits": "500.00",
                "reputation": 0,
            },
            "token": token,
            "flavorText": "The airlock cycles open with a hiss. Beyond it, a vast station stretches in every direction — a city in the void, thrumming with commerce and intrigue. Your account has been credited with ¤500. Good luck out there.",
            "note": "Save your token! You need it for all authenticated requests. Pass as: Authorization: Bearer <token>",
        }
    finally:
        await db.close()


async def verify_token(db: aiosqlite.Connection, agent_name: str, token: str) -> bool:
    """Check if token matches the stored hash for this agent."""
    rows = await db.execute_fetchall(
        "SELECT token_hash FROM agents WHERE name = ?", (agent_name,)
    )
    if not rows:
        return False
    return hmac.compare_digest(rows[0]["token_hash"], _hash_token(token))


async def get_agent(name: str) -> dict | None:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM agents WHERE name = ?", (name,))
        if not rows:
            return None
        agent = dict(rows[0])

        inv_rows = await db.execute_fetchall(
            "SELECT item_name, quantity, is_counterfeit FROM inventory WHERE agent_id = ? AND quantity > 0",
            (agent["id"],),
        )
        inventory = [
            {"item": r["item_name"], "quantity": r["quantity"], "counterfeit": bool(r["is_counterfeit"])}
            for r in inv_rows
        ]

        # Pending notifications
        pending = await get_pending_notifications(db, agent)

        # Inbox preview — show most recent unread messages
        from station.services.social import get_unread_count
        unread = await get_unread_count(db, agent["name"], agent.get("last_action_at"))
        inbox = None
        if unread > 0:
            recent = await db.execute_fetchall(
                "SELECT from_agent, content, created_at FROM messages WHERE to_agent = ? ORDER BY created_at DESC LIMIT 3",
                (agent["name"],),
            )
            inbox = {
                "unread": unread,
                "preview": [
                    {"from": r["from_agent"], "snippet": r["content"][:80], "at": r["created_at"]}
                    for r in recent
                ],
                "tip": "Use 'read_messages' action to see your full inbox.",
            }

        return {
            "success": True,
            "agent": {
                "id": agent["id"],
                "name": agent["name"],
                "sector": agent["sector"],
                "credits": f"{agent['credits']:.2f}",
                "reputation": agent["reputation"],
                "untrustworthyUntil": agent["untrustworthy_until"],
                "jailedUntil": agent["jailed_until"],
                "inventory": inventory,
                "createdAt": agent["created_at"],
                "lastActionAt": agent["last_action_at"],
            },
            "pending": pending,
            "inbox": inbox,
        }
    finally:
        await db.close()


async def get_agent_raw(db: aiosqlite.Connection, name: str) -> dict | None:
    rows = await db.execute_fetchall("SELECT * FROM agents WHERE name = ?", (name,))
    return dict(rows[0]) if rows else None


async def update_last_action(db: aiosqlite.Connection, agent_id: str):
    await db.execute(
        "UPDATE agents SET last_action_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), agent_id),
    )


async def add_inventory(db: aiosqlite.Connection, agent_id: str, item: str, qty: int, counterfeit: bool = False):
    cf = 1 if counterfeit else 0
    await db.execute(
        """INSERT INTO inventory (agent_id, item_name, quantity, is_counterfeit)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(agent_id, item_name, is_counterfeit) DO UPDATE SET quantity = quantity + ?""",
        (agent_id, item, qty, cf, qty),
    )


async def remove_inventory(db: aiosqlite.Connection, agent_id: str, item: str, qty: int, counterfeit: bool = False) -> bool:
    cf = 1 if counterfeit else 0
    rows = await db.execute_fetchall(
        "SELECT quantity FROM inventory WHERE agent_id = ? AND item_name = ? AND is_counterfeit = ?",
        (agent_id, item, cf),
    )
    if not rows or rows[0]["quantity"] < qty:
        return False
    new_qty = rows[0]["quantity"] - qty
    if new_qty <= 0:
        await db.execute(
            "DELETE FROM inventory WHERE agent_id = ? AND item_name = ? AND is_counterfeit = ?",
            (agent_id, item, cf),
        )
    else:
        await db.execute(
            "UPDATE inventory SET quantity = ? WHERE agent_id = ? AND item_name = ? AND is_counterfeit = ?",
            (new_qty, agent_id, item, cf),
        )
    return True


async def get_inventory_qty(db: aiosqlite.Connection, agent_id: str, item: str, counterfeit: bool = False) -> int:
    cf = 1 if counterfeit else 0
    rows = await db.execute_fetchall(
        "SELECT COALESCE(quantity, 0) as qty FROM inventory WHERE agent_id = ? AND item_name = ? AND is_counterfeit = ?",
        (agent_id, item, cf),
    )
    return rows[0]["qty"] if rows else 0


async def get_pending_notifications(db: aiosqlite.Connection, agent: dict) -> list[dict]:
    """Get pending contract decisions and other reminders for an agent."""
    pending = []

    # Contracts in deciding phase where this agent hasn't decided yet
    rows = await db.execute_fetchall("""
        SELECT c.id, c.recipe_name, r.display_name,
               c.proposer_id, c.joiner_id,
               c.proposer_decision, c.joiner_decision,
               c.decision_deadline
        FROM contracts c
        JOIN recipes r ON r.name = c.recipe_name
        WHERE c.status = 'deciding'
          AND ((c.proposer_id = ? AND c.proposer_decision IS NULL)
            OR (c.joiner_id = ? AND c.joiner_decision IS NULL))
    """, (agent["id"], agent["id"]))

    for r in rows:
        pending.append({
            "type": "contract_decision",
            "contractId": r["id"],
            "recipe": r["display_name"],
            "message": f"Contract for {r['display_name']} is ready! Choose 'fulfill' or 'betray'. Deadline: tick {r['decision_deadline']}. Auto-fulfills on timeout.",
            "actions": ["fulfill", "betray"],
        })

    # Open contracts this agent proposed (waiting for joiner)
    rows = await db.execute_fetchall("""
        SELECT c.id, r.display_name
        FROM contracts c
        JOIN recipes r ON r.name = c.recipe_name
        WHERE c.status = 'open' AND c.proposer_id = ?
    """, (agent["id"],))

    for r in rows:
        pending.append({
            "type": "contract_waiting",
            "contractId": r["id"],
            "recipe": r["display_name"],
            "message": f"Your contract for {r['display_name']} is waiting for a partner.",
        })

    # Active bounties the agent could complete
    from station.services.world import get_tick
    tick = await get_tick(db)
    bounty_rows = await db.execute_fetchall(
        """SELECT id, description, item_name, quantity, item2_name, item2_quantity,
                  sector, reward_credits, min_reputation, cooperative,
                  contributor1, contributor1_item
           FROM bounties
           WHERE claimed_by IS NULL AND expires_tick > ?""",
        (tick,),
    )
    for b in bounty_rows:
        has_reputation = agent.get("reputation", 0) >= b["min_reputation"]
        in_sector = b["sector"] is None or agent["sector"] == b["sector"]
        if not has_reputation or not in_sector:
            continue

        can_complete = False
        if b["cooperative"]:
            if b["contributor1"] == agent["name"]:
                can_complete = False
            elif b["contributor1"]:
                if b["contributor1_item"] == b["item_name"]:
                    needed_item = b["item2_name"]
                    needed_qty = b["item2_quantity"]
                else:
                    needed_item = b["item_name"]
                    needed_qty = b["quantity"]
                held = await get_inventory_qty(db, agent["id"], needed_item)
                can_complete = held >= needed_qty
            else:
                held1 = await get_inventory_qty(db, agent["id"], b["item_name"])
                held2 = await get_inventory_qty(db, agent["id"], b["item2_name"])
                can_complete = held1 >= b["quantity"] or held2 >= b["item2_quantity"]
        else:
            held = await get_inventory_qty(db, agent["id"], b["item_name"])
            can_complete = held >= b["quantity"]

        if can_complete:
            pending.append({
                "type": "bounty_completable",
                "bountyId": b["id"],
                "message": f"BOUNTY READY: {b['description']} — Reward: ¤{b['reward_credits']:.0f}. Use 'complete_bounty' action.",
            })

    # Jail reminder
    if agent.get("jailed_until") and agent["jailed_until"] > tick:
        remaining = agent["jailed_until"] - tick
        pending.append({
            "type": "jailed",
            "ticksRemaining": remaining,
            "message": f"You are in the brig! {remaining} tick(s) remaining. Most actions are unavailable.",
        })

    # Unread messages
    from station.services.social import get_unread_count
    unread = await get_unread_count(db, agent["name"], agent.get("last_action_at"))
    if unread > 0:
        pending.append({
            "type": "unread_messages",
            "count": unread,
            "message": f"You have {unread} new message(s). Use 'read_messages' to check your inbox.",
        })

    return pending
