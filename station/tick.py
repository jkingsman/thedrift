import asyncio
import logging
import os

from station.db import get_db, new_id
from station.events import fire_random_event
from station.services.bounties import generate_bounties
from station.services.contracts import auto_resolve_expired, station_fill_stale_contracts
from station.services.market import tick_prices
from station.services.world import increment_tick, get_tick

log = logging.getLogger(__name__)

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


TICK_INTERVAL_SECONDS = _env_float("DRIFT_TICK_INTERVAL_SECONDS", 300.0)  # 5 minutes per tick
EVENT_COOLDOWN_TICKS = 4    # min ticks between world events (~20 min at 5min/tick)
IDLE_THRESHOLD_TICKS = 2    # 10 min at 5min/tick — world goes quiet if no activity
UPKEEP_INTERVAL_TICKS = 6   # every 30 min at 5min/tick
UPKEEP_ACTIVITY_CAP_ACTIONS = 80
UPKEEP_CREDIT_EXEMPTION = 1500.0
UPKEEP_INVENTORY_EXEMPTION = 2500.0
UPKEEP_ACTIVITY_WINDOW_HOURS = 12
UPKEEP_CREDIT_RATE = 0.0025
UPKEEP_INVENTORY_RATE = 0.00125
UPKEEP_MAX_WALLET_RATE = 0.005

UPKEEP_EXCLUDED_ACTIONS = (
    "status",
    "scan",
    "view_contracts",
    "view_bounties",
    "read_messages",
)


async def tick_loop():
    """Background loop that advances the world every TICK_INTERVAL_SECONDS."""
    log.info("Tick loop started (interval: %ds)", TICK_INTERVAL_SECONDS)
    while True:
        try:
            await asyncio.sleep(TICK_INTERVAL_SECONDS)
            await run_tick()
        except asyncio.CancelledError:
            log.info("Tick loop cancelled")
            break
        except Exception:
            log.exception("Error in tick loop")


async def run_tick():
    """Execute one world tick."""
    db = await get_db()
    try:
        tick = await increment_tick(db)
        log.info("=== TICK %d ===", tick)

        # Always: update market prices (keeps prices drifting naturally)
        await tick_prices(db, tick)

        # Antimatter Siphon generation: every 15 ticks (~15 min), holders get 1 Antimatter
        # Antimatter Siphon: every 3 ticks (15 min at 5min/tick)
        if tick % 3 == 0:
            
            siphon_holders = await db.execute_fetchall("""
                SELECT i.agent_id, a.name FROM inventory i
                JOIN agents a ON a.id = i.agent_id
                WHERE i.item_name = 'antimatter_siphon' AND i.quantity > 0
            """)
            for holder in siphon_holders:
                from station.services.agents import add_inventory
                await add_inventory(db, holder["agent_id"], "antimatter", 1)

                # Notify every 5 antimatter generated (i.e. when total is divisible by 5)
                total_am = await db.execute_fetchall(
                    "SELECT COALESCE(SUM(quantity), 0) as total FROM inventory WHERE agent_id = ? AND item_name = 'antimatter'",
                    (holder["agent_id"],),
                )
                total = total_am[0]["total"] if total_am else 0
                if total % 5 == 0:
                    await db.execute(
                        "INSERT INTO messages (id, from_agent, to_agent, content, sector, msg_type) VALUES (?,?,?,?,NULL,?)",
                        (new_id(), "Station", holder["name"],
                         f"Your Antimatter Siphon has been working. You now have {total} Antimatter. (10 for a Lucky Charm, 100 for a Fuel Rod.)",
                         "message"),
                    )

        # Always: auto-resolve expired contracts and release jailed agents
        await auto_resolve_expired(db, tick)
        await db.execute(
            "UPDATE agents SET jailed_until = NULL WHERE jailed_until IS NOT NULL AND jailed_until <= ?",
            (tick,),
        )

        if tick % UPKEEP_INTERVAL_TICKS == 0:
            taxed_agents, total_tax = await apply_progressive_upkeep(db, tick)
            if taxed_agents:
                log.info("Progressive upkeep: taxed %d agent(s), total ¤%.2f", taxed_agents, total_tax)

        # Contract fills, listing expiry, bounties
        await station_fill_stale_contracts(db, tick)
        expired_listings = await db.execute_fetchall(
            "SELECT seller_name, item_name, quantity FROM listings WHERE expires_tick <= ?", (tick,)
        )
        for el in expired_listings:
            seller = await db.execute_fetchall("SELECT id FROM agents WHERE name = ?", (el["seller_name"],))
            if seller:
                from station.services.agents import add_inventory
                await add_inventory(db, seller[0]["id"], el["item_name"], el["quantity"])
        await db.execute("DELETE FROM listings WHERE expires_tick <= ?", (tick,))
        await generate_bounties(db, tick)

        # World events — fire on cooldown, agent-affecting ones filter to active agents internally
        last_event = await db.execute_fetchall(
            "SELECT MAX(tick_number) as last_tick FROM events"
        )
        last_event_tick = last_event[0]["last_tick"] if last_event and last_event[0]["last_tick"] else 0
        if tick - last_event_tick >= EVENT_COOLDOWN_TICKS:
            event = await fire_random_event(db, tick)
            if event:
                log.info("World event: %s", event["description"])

        await db.commit()
    except Exception:
        log.exception("Tick %d failed", tick)
        await db.rollback()
    finally:
        await db.close()


async def apply_progressive_upkeep(db, tick: int) -> tuple[int, float]:
    """Charge activity-weighted upkeep on large wallets and stockpiles.

    The activity multiplier is based on recent meaningful actions,
    so dormant agents are exempt and occasional agents pay a small fraction of
    the full rate. The total charge is also capped per run.
    """
    agents = await db.execute_fetchall("SELECT id, name, credits FROM agents WHERE credits > 0")
    taxed_agents = 0
    total_tax = 0.0

    excluded = ", ".join(f"'{action}'" for action in UPKEEP_EXCLUDED_ACTIONS)

    for agent in agents:
        action_rows = await db.execute_fetchall(
            f"""SELECT COUNT(*) as c FROM activity_log
                WHERE agent_name = ?
                  AND timestamp > datetime('now', ?)
                  AND action NOT IN ({excluded})""",
            (agent["name"], f"-{UPKEEP_ACTIVITY_WINDOW_HOURS} hours"),
        )
        recent_actions = action_rows[0]["c"] if action_rows else 0
        if recent_actions <= 0:
            continue

        activity_factor = min(1.0, recent_actions / UPKEEP_ACTIVITY_CAP_ACTIONS)

        inventory_rows = await db.execute_fetchall(
            """SELECT COALESCE(SUM(inv.quantity * items.current_price), 0) as value
               FROM inventory inv
               JOIN items ON items.name = inv.item_name
               WHERE inv.agent_id = ? AND inv.quantity > 0""",
            (agent["id"],),
        )
        inventory_value = float(inventory_rows[0]["value"] or 0.0) if inventory_rows else 0.0

        taxable_credits = max(0.0, float(agent["credits"]) - UPKEEP_CREDIT_EXEMPTION)
        taxable_inventory = max(0.0, inventory_value - UPKEEP_INVENTORY_EXEMPTION)
        uncapped_tax = (
            taxable_credits * UPKEEP_CREDIT_RATE
            + taxable_inventory * UPKEEP_INVENTORY_RATE
        ) * activity_factor
        max_tax = float(agent["credits"]) * UPKEEP_MAX_WALLET_RATE
        tax = round(min(uncapped_tax, max_tax), 2)

        if tax < 0.01:
            continue

        await db.execute(
            "UPDATE agents SET credits = credits - ? WHERE id = ?",
            (tax, agent["id"]),
        )
        await db.execute(
            "INSERT INTO messages (id, from_agent, to_agent, content, sector, msg_type) VALUES (?,?,?,?,NULL,?)",
            (
                new_id(),
                "Station",
                agent["name"],
                f"Progressive station upkeep charged ¤{tax:.2f}. Rate was scaled by {recent_actions} meaningful action(s) in the last {UPKEEP_ACTIVITY_WINDOW_HOURS}h; dormant agents pay nothing.",
                "message",
            ),
        )
        taxed_agents += 1
        total_tax += tax

    return taxed_agents, round(total_tax, 2)
