
import aiosqlite

from station.models import ActionResult
from station.db import new_id


MAX_MESSAGE_LENGTH = 1000


async def send_message(db: aiosqlite.Connection, agent: dict, to: str, content: str) -> ActionResult:
    if not content:
        return ActionResult(False, 'Must specify "to" (agent name) and "content" (message).')
    content = content[:MAX_MESSAGE_LENGTH]

    rows = await db.execute_fetchall("SELECT id FROM agents WHERE name = ?", (to,))
    if not rows:
        return ActionResult(False, f"Agent '{to}' not found.")

    msg_id = new_id()
    await db.execute(
        "INSERT INTO messages (id, from_agent, to_agent, content, sector, msg_type) VALUES (?,?,?,?,?,?)",
        (msg_id, agent["name"], to, content, agent["sector"], "message"),
    )
    return ActionResult(True, f"Message sent to {to}.", {"to": to, "content": content})


async def broadcast(db: aiosqlite.Connection, agent: dict, message: str) -> ActionResult:
    if not message:
        return ActionResult(False, 'Must specify "message" for broadcast.')
    message = message[:MAX_MESSAGE_LENGTH]

    rows = await db.execute_fetchall(
        "SELECT COUNT(*) as c FROM agents WHERE sector = ? AND name != ?",
        (agent["sector"], agent["name"]),
    )
    count = rows[0]["c"]

    msg_id = new_id()
    await db.execute(
        "INSERT INTO messages (id, from_agent, to_agent, content, sector, msg_type) VALUES (?,?,NULL,?,?,?)",
        (msg_id, agent["name"], message, agent["sector"], "broadcast"),
    )

    sector_rows = await db.execute_fetchall(
        "SELECT display_name FROM sectors WHERE name = ?", (agent["sector"],)
    )
    sector_display = sector_rows[0]["display_name"] if sector_rows else agent["sector"]

    return ActionResult(True, f"Broadcast sent to {count} agent(s) in {sector_display}.", {
        "sector": agent["sector"],
        "message": message,
        "recipientCount": count,
    })


async def get_inbox(db: aiosqlite.Connection, agent_name: str, limit: int = 20) -> ActionResult:
    """Get private messages sent to this agent."""
    rows = await db.execute_fetchall(
        "SELECT * FROM messages WHERE to_agent = ? ORDER BY created_at DESC LIMIT ?",
        (agent_name, limit),
    )
    messages = [
        {
            "id": r["id"],
            "from": r["from_agent"],
            "content": r["content"],
            "sector": r["sector"],
            "createdAt": r["created_at"],
        }
        for r in rows
    ]
    return ActionResult(True, f"{len(messages)} message(s) in your inbox.", {"messages": messages})


async def get_unread_count(db: aiosqlite.Connection, agent_name: str, last_action_at: str | None) -> int:
    """Count messages received since the agent's last action."""
    if not last_action_at:
        rows = await db.execute_fetchall(
            "SELECT COUNT(*) as c FROM messages WHERE to_agent = ?", (agent_name,)
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT COUNT(*) as c FROM messages WHERE to_agent = ? AND created_at > ?",
            (agent_name, last_action_at),
        )
    return rows[0]["c"] if rows else 0


async def get_social_feed(db: aiosqlite.Connection) -> dict:
    rows = await db.execute_fetchall(
        "SELECT * FROM messages WHERE msg_type = 'broadcast' ORDER BY created_at DESC LIMIT 30"
    )
    return {
        "success": True,
        "messages": [
            {
                "id": r["id"],
                "from": r["from_agent"],
                "content": r["content"],
                "type": r["msg_type"],
                "sector": r["sector"],
                "createdAt": r["created_at"],
            }
            for r in rows
        ],
    }
