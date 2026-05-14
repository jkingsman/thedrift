import json
import random

import aiosqlite

from station.db import new_id
from station.models import ActionResult

STATION_BLOCKED_CATEGORIES = {"advanced"}
SELL_IMPACT_MULTIPLIERS = {
    "raw": 3.0,
    "crafted": 30.0,
    "advanced": 30.0,
}
BUY_IMPACT_MULTIPLIER = 1.0
MAX_PRICE_IMPACT_PER_TRADE = 0.35
ACTIVE_AGENT_LIQUIDITY_WINDOW_TICKS = 30
ACTIVE_AGENT_LIQUIDITY_DIVISOR = 4.0
MAX_ACTIVE_AGENT_LIQUIDITY_MULTIPLIER = 10.0


async def get_item(db: aiosqlite.Connection, name: str) -> dict | None:
    rows = await db.execute_fetchall("SELECT * FROM items WHERE name = ?", (name,))
    return dict(rows[0]) if rows else None


async def get_all_items(db: aiosqlite.Connection) -> list[dict]:
    rows = await db.execute_fetchall("SELECT * FROM items ORDER BY category, name")
    return [dict(r) for r in rows]


async def get_buy_price(db: aiosqlite.Connection, item_name: str, sector: str) -> float | None:
    """Price an agent pays to buy from the station. Higher than base."""
    item = await get_item(db, item_name)
    if not item:
        return None
    spread = 1.05 if sector == "the_exchange" else 1.10
    return round(item["current_price"] * spread, 2)


async def get_sell_price(db: aiosqlite.Connection, item_name: str, sector: str) -> float | None:
    """Price an agent receives when selling to the station. Lower than base."""
    item = await get_item(db, item_name)
    if not item:
        return None
    spread = 0.95 if sector == "the_exchange" else 0.90
    return round(item["current_price"] * spread, 2)


async def buy_from_station(db: aiosqlite.Connection, agent: dict, item_name: str, quantity: int) -> ActionResult:
    if quantity <= 0:
        return ActionResult(False, "Quantity must be positive.")

    item = await get_item(db, item_name)
    if not item:
        return ActionResult(False, f'Item "{item_name}" not found.')
    if item["category"] in STATION_BLOCKED_CATEGORIES:
        return ActionResult(
            False,
            f"The station does not sell {item['display_name']} directly. Advanced goods must be crafted, contracted, or bought from player listings.",
            {
                "item": item["display_name"],
                "category": item["category"],
                "stationAvailable": False,
                "blockedCategories": sorted(STATION_BLOCKED_CATEGORIES),
            },
        )

    buy_price = await get_buy_price(db, item_name, agent["sector"])
    total_cost = buy_price * quantity

    if agent["credits"] < total_cost:
        return ActionResult(False, f"Not enough credits. Need ¤{total_cost:.2f}, have ¤{agent['credits']:.2f}.")

    # Deduct credits atomically (prevents negative balance from concurrent requests)
    cursor = await db.execute(
        "UPDATE agents SET credits = credits - ? WHERE id = ? AND credits >= ?",
        (total_cost, agent["id"], total_cost),
    )
    if cursor.rowcount == 0:
        return ActionResult(False, "Not enough credits (concurrent transaction).")
    # Add to inventory
    from station.services.agents import add_inventory
    await add_inventory(db, agent["id"], item_name, quantity)
    # Adjust market price (buying pushes price up)
    await _adjust_price(db, item_name, quantity, is_buy=True)

    new_price = (await get_item(db, item_name))["current_price"]
    return ActionResult(True, f"Bought {quantity} {item['display_name']} for ¤{total_cost:.2f}.", {
        "item": item["display_name"],
        "quantity": quantity,
        "cost": f"{total_cost:.2f}",
        "unitPrice": f"{buy_price:.2f}",
        "newBalance": f"{agent['credits'] - total_cost:.2f}",
        "newMarketPrice": f"{new_price:.2f}",
    })


async def sell_to_station(db: aiosqlite.Connection, agent: dict, item_name: str, quantity: int, tick: int = 0) -> ActionResult:
    if quantity <= 0:
        return ActionResult(False, "Quantity must be positive.")

    item = await get_item(db, item_name)
    if not item:
        return ActionResult(False, f'Item "{item_name}" not found.')

    from station.services.agents import remove_inventory, get_inventory_qty

    # Check for counterfeits first — agent may try to sell fakes
    counterfeit_held = await get_inventory_qty(db, agent["id"], item_name, counterfeit=True)
    legit_held = await get_inventory_qty(db, agent["id"], item_name, counterfeit=False)

    # Sell counterfeits first (agent is trying to offload them)
    counterfeit_sold = 0
    legit_sold = 0
    if counterfeit_held > 0:
        counterfeit_sold = min(quantity, counterfeit_held)
        legit_sold = min(quantity - counterfeit_sold, legit_held)
    else:
        legit_sold = min(quantity, legit_held)

    total_sold = counterfeit_sold + legit_sold
    if total_sold < quantity:
        return ActionResult(False, f"You only have {total_sold} {item['display_name']}.")

    # Counterfeit detection: 35% chance per counterfeit item
    if counterfeit_sold > 0 and random.random() < 0.35:
        # Caught! Counterfeits confiscated, agent jailed
        await remove_inventory(db, agent["id"], item_name, counterfeit_sold, counterfeit=True)
        jail_until = tick + random.randint(2, 5)
        fine = round(random.uniform(30, 80), 2)
        fine = min(fine, agent["credits"])
        await db.execute(
            "UPDATE agents SET jailed_until = ?, credits = credits - ?, reputation = reputation - 5 WHERE id = ?",
            (jail_until, fine, agent["id"]),
        )
        return ActionResult(False,
            f"BUSTED! Station scanners detected {counterfeit_sold} counterfeit {item['display_name']}! Fined ¤{fine:.2f}, jailed until tick {jail_until}. Reputation -5.",
            {
                "caught": True,
                "counterfeitsConfiscated": counterfeit_sold,
                "fine": f"{fine:.2f}",
                "jailedUntil": jail_until,
                "reputationLost": 5,
            },
        )

    # Sell goes through
    sell_price = await get_sell_price(db, item_name, agent["sector"])
    total_earned = sell_price * total_sold

    if counterfeit_sold > 0:
        await remove_inventory(db, agent["id"], item_name, counterfeit_sold, counterfeit=True)
    if legit_sold > 0:
        await remove_inventory(db, agent["id"], item_name, legit_sold, counterfeit=False)

    await db.execute("UPDATE agents SET credits = credits + ? WHERE id = ?", (total_earned, agent["id"]))
    await _adjust_price(db, item_name, total_sold, is_buy=False)

    new_price = (await get_item(db, item_name))["current_price"]
    return ActionResult(True, f"Sold {total_sold} {item['display_name']} for ¤{total_earned:.2f}.", {
        "item": item["display_name"],
        "quantity": total_sold,
        "earned": f"{total_earned:.2f}",
        "unitPrice": f"{sell_price:.2f}",
        "newBalance": f"{agent['credits'] + total_earned:.2f}",
        "newMarketPrice": f"{new_price:.2f}",
    })


async def list_item(db: aiosqlite.Connection, agent: dict, item_name: str, quantity: int, price: float, tick: int) -> ActionResult:
    if quantity <= 0 or price <= 0:
        return ActionResult(False, "Quantity and price must be positive.")

    item = await get_item(db, item_name)
    if not item:
        return ActionResult(False, f'Item "{item_name}" not found.')

    from station.services.agents import remove_inventory, get_inventory_qty
    held = await get_inventory_qty(db, agent["id"], item_name)
    if held < quantity:
        return ActionResult(False, f"You only have {held} {item['display_name']}.")

    # Remove items from inventory (held in listing)
    await remove_inventory(db, agent["id"], item_name, quantity)

    listing_id = new_id()
    expires = tick + 20
    await db.execute(
        "INSERT INTO listings (id, seller_name, item_name, quantity, price_each, expires_tick) VALUES (?,?,?,?,?,?)",
        (listing_id, agent["name"], item_name, quantity, price, expires),
    )

    return ActionResult(True, f"Listed {quantity} {item['display_name']} at ¤{price:.2f} each. Expires tick {expires}.", {
        "listingId": listing_id,
        "item": item["display_name"],
        "quantity": quantity,
        "priceEach": f"{price:.2f}",
        "expiresTick": expires,
    })


async def buy_listing(db: aiosqlite.Connection, agent: dict, listing_id: str, tick: int = 0) -> ActionResult:
    rows = await db.execute_fetchall(
        "SELECT * FROM listings WHERE id = ? AND expires_tick > ?", (listing_id, tick)
    )
    if not rows:
        return ActionResult(False, "Listing not found or expired.")

    listing = dict(rows[0])
    if listing["seller_name"] == agent["name"]:
        return ActionResult(False, "You can't buy your own listing.")

    total_cost = listing["price_each"] * listing["quantity"]
    if agent["credits"] < total_cost:
        return ActionResult(False, f"Not enough credits. Need ¤{total_cost:.2f}.")

    # Atomically claim the listing (prevents double-buy race)
    cursor = await db.execute("DELETE FROM listings WHERE id = ?", (listing_id,))
    if cursor.rowcount == 0:
        return ActionResult(False, "Listing was just purchased by someone else.")

    # Transfer
    cursor = await db.execute(
        "UPDATE agents SET credits = credits - ? WHERE id = ? AND credits >= ?",
        (total_cost, agent["id"], total_cost),
    )
    if cursor.rowcount == 0:
        # Restore the listing — buyer can't afford it after all
        await db.execute(
            "INSERT INTO listings (id, seller_name, item_name, quantity, price_each, expires_tick) VALUES (?,?,?,?,?,?)",
            (listing_id, listing["seller_name"], listing["item_name"], listing["quantity"], listing["price_each"], listing["expires_tick"]),
        )
        return ActionResult(False, "Not enough credits.")

    await db.execute("UPDATE agents SET credits = credits + ? WHERE name = ?", (total_cost, listing["seller_name"]))

    from station.services.agents import add_inventory
    await add_inventory(db, agent["id"], listing["item_name"], listing["quantity"])

    item = await get_item(db, listing["item_name"])
    display = item["display_name"] if item else listing["item_name"]
    return ActionResult(True, f"Bought {listing['quantity']} {display} from {listing['seller_name']} for ¤{total_cost:.2f}.", {
        "item": display,
        "quantity": listing["quantity"],
        "cost": f"{total_cost:.2f}",
        "seller": listing["seller_name"],
        "newBalance": f"{agent['credits'] - total_cost:.2f}",
    })


async def get_listings(db: aiosqlite.Connection, tick: int) -> dict:
    # Don't delete here — tick loop handles expiry + item return
    rows = await db.execute_fetchall(
        "SELECT * FROM listings WHERE expires_tick > ? ORDER BY created_at DESC", (tick,)
    )
    return {
        "success": True,
        "listings": [
            {
                "id": r["id"],
                "seller": r["seller_name"],
                "item": r["item_name"],
                "quantity": r["quantity"],
                "priceEach": f"{r['price_each']:.2f}",
                "expiresTick": r["expires_tick"],
            }
            for r in rows
        ],
    }


async def _adjust_price(db: aiosqlite.Connection, item_name: str, quantity: int, is_buy: bool):
    """Move price based on trade volume relative to supply."""
    item = await get_item(db, item_name)
    if not item:
        return
    supply = max(1.0, item["supply"])
    if not is_buy:
        active_agents = await _count_recent_active_agents(db)
        liquidity_multiplier = min(
            MAX_ACTIVE_AGENT_LIQUIDITY_MULTIPLIER,
            max(1.0, active_agents / ACTIVE_AGENT_LIQUIDITY_DIVISOR),
        )
        supply *= liquidity_multiplier
    multiplier = BUY_IMPACT_MULTIPLIER if is_buy else SELL_IMPACT_MULTIPLIERS.get(item["category"], 10.0)
    impact = (quantity / supply) * item["volatility"] * 5.0 * multiplier
    impact = min(MAX_PRICE_IMPACT_PER_TRADE, impact)
    if is_buy:
        new_price = item["current_price"] * (1 + impact)
    else:
        new_price = item["current_price"] * (1 - impact)
    new_price = max(0.01, round(new_price, 2))
    await db.execute("UPDATE items SET current_price = ? WHERE name = ?", (new_price, item_name))


async def _count_recent_active_agents(db: aiosqlite.Connection) -> int:
    rows = await db.execute_fetchall("SELECT value FROM world_state WHERE key = 'tick'")
    tick = int(rows[0]["value"]) if rows else 0
    active = await db.execute_fetchall(
        """SELECT COUNT(DISTINCT agent_name) as c
           FROM activity_log
           WHERE tick >= ?""",
        (max(0, tick - ACTIVE_AGENT_LIQUIDITY_WINDOW_TICKS),),
    )
    return active[0]["c"] if active else 0


async def tick_prices(db: aiosqlite.Connection, tick: int):
    """Random walk on all prices each tick, and snapshot for history."""
    items = await get_all_items(db)
    for item in items:
        vol = item["volatility"]
        change_pct = random.uniform(-vol, vol) / 25.0
        new_price = item["current_price"] * (1 + change_pct)
        # Gentle mean reversion
        reversion = (item["base_price"] - new_price) * 0.02
        new_price += reversion
        new_price = max(0.01, round(new_price, 2))
        await db.execute("UPDATE items SET current_price = ? WHERE name = ?", (new_price, item["name"]))
        # Snapshot for price history
        await db.execute(
            "INSERT OR REPLACE INTO price_history (tick, item_name, price) VALUES (?, ?, ?)",
            (tick, item["name"], new_price),
        )
    # Prune old history (keep last 200 ticks)
    await db.execute("DELETE FROM price_history WHERE tick < ?", (tick - 200,))


async def get_price_history(db: aiosqlite.Connection, item_name: str, ticks: int = 30) -> dict:
    item = await get_item(db, item_name)
    if not item:
        return {"success": False, "message": f'Item "{item_name}" not found.'}

    rows = await db.execute_fetchall(
        "SELECT tick, price FROM price_history WHERE item_name = ? ORDER BY tick DESC LIMIT ?",
        (item_name, ticks),
    )
    return {
        "success": True,
        "item": item["display_name"],
        "itemName": item_name,
        "currentPrice": f"{item['current_price']:.2f}",
        "basePrice": f"{item['base_price']:.2f}",
        "history": [{"tick": r["tick"], "price": f"{r['price']:.2f}"} for r in reversed(list(rows))],
    }
