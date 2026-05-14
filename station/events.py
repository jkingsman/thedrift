import json
import random

import aiosqlite

# Agents who haven't acted in this many ticks are considered inactive
ACTIVE_AGENT_THRESHOLD = 8  # ~40 min at 5min/tick — avg ~1 event after logout, max ~3

from station.services.agents import add_inventory, remove_inventory
from station.services.world import get_active_effects, set_active_effects
from station.db import new_id


async def _get_active_agents(db: aiosqlite.Connection, tick: int) -> list[dict]:
    """Return agents who have acted recently."""
    rows = await db.execute_fetchall("""
        SELECT DISTINCT a.id, a.name, a.sector, a.credits
        FROM agents a
        INNER JOIN activity_log al ON al.agent_name = a.name
        WHERE al.tick >= ?
    """, (max(0, tick - ACTIVE_AGENT_THRESHOLD),))
    return [dict(r) for r in rows]


async def fire_random_event(db: aiosqlite.Connection, tick: int) -> dict | None:
    """Pick and execute a random world event. Returns event dict or None."""
    # Decrement active effect timers first
    effects = await get_active_effects(db)
    expired = [k for k, v in effects.items() if isinstance(v, int) and v <= tick]
    for k in expired:
        del effects[k]
    if expired:
        await set_active_effects(db, effects)

    # ~70% chance an event fires each tick
    if random.random() > 0.70:
        return None

    # (event_fn, weight) — higher weight = more likely
    events = [
        (_solar_flare, 10),
        (_cargo_drop, 15),
        (_station_tax, 3),        # rare
        (_black_market_surge, 10),
        (_power_outage, 8),
        (_diplomatic_summit, 10),
        (_pirate_raid, 8),
        (_price_crash, 12),
        (_gold_rush, 12),
        (_gravity_malfunction, 5),
    ]

    fns, weights = zip(*events)
    event_fn = random.choices(fns, weights=weights, k=1)[0]
    return await event_fn(db, tick)


async def _solar_flare(db: aiosqlite.Connection, tick: int) -> dict:
    """Crystal Shards +80%, electronics -40%."""
    await db.execute(
        "UPDATE items SET current_price = current_price * 1.8 WHERE name = 'crystal_shards'"
    )
    electronics = ["signal_beacon", "neural_lace", "ai_core", "quantum_relay", "relay_array", "nav_computer"]
    for item in electronics:
        await db.execute(
            "UPDATE items SET current_price = MAX(0.01, current_price * 0.6) WHERE name = ?", (item,)
        )

    desc = "SOLAR FLARE! Crystal Shards surged +80%! Electronics crashed -40%. Shield your circuits!"
    event = _make_event("solar_flare", desc, {
        "boosted": {"crystal_shards": "+80%"},
        "reduced": {i: "-40%" for i in electronics},
    }, tick)
    await _save_event(db, event)
    return event


async def _cargo_drop(db: aiosqlite.Connection, tick: int) -> dict:
    """Large material drop in a random sector."""
    sectors = ["scrap_alley", "void_dock", "the_foundry", "the_commons"]
    target_sector = random.choice(sectors)

    materials = ["scrap_metal", "crystal_shards", "bio_gel", "plasma_coils", "coolant", "data_fragments"]
    dropped = random.choice(materials)
    qty = random.randint(3, 8)

    active = await _get_active_agents(db, tick)
    agents = [a for a in active if a["sector"] == target_sector]
    for a in agents:
        await add_inventory(db, a["id"], dropped, qty)

    sector_rows = await db.execute_fetchall(
        "SELECT display_name FROM sectors WHERE name = ?", (target_sector,)
    )
    sector_display = sector_rows[0]["display_name"] if sector_rows else target_sector

    from station.services.market import get_item
    item_info = await get_item(db, dropped)
    item_display = item_info["display_name"] if item_info else dropped

    desc = f"CARGO DROP! {qty}x {item_display} delivered to {sector_display}! {len(agents)} agent(s) received the goods!"
    event = _make_event("cargo_drop", desc, {
        "sector": target_sector,
        "item": dropped,
        "quantity": qty,
        "recipients": len(agents),
    }, tick)
    await _save_event(db, event)
    return event


async def _station_tax(db: aiosqlite.Connection, tick: int) -> dict:
    """Seize a percentage of a random commodity from all agents."""
    from station.services.market import get_all_items
    items = await get_all_items(db)
    target = random.choice(items)
    rate = round(random.uniform(0.02, 0.05), 2)
    pct = int(rate * 100)

    total_seized = 0
    agents = await _get_active_agents(db, tick)
    for a in agents:
        rows = await db.execute_fetchall(
            "SELECT quantity FROM inventory WHERE agent_id = ? AND item_name = ? AND is_counterfeit = 0 AND quantity > 0",
            (a["id"], target["name"]),
        )
        if rows and rows[0]["quantity"] > 0:
            seized = max(1, int(rows[0]["quantity"] * rate))
            await remove_inventory(db, a["id"], target["name"], seized)
            total_seized += seized

    desc = f"STATION REQUISITION! {pct}% of all {target['display_name']} has been seized! Total confiscated: {total_seized}."
    event = _make_event("station_tax", desc, {
        "commodity": target["name"],
        "displayName": target["display_name"],
        "taxRate": rate,
        "totalSeized": total_seized,
    }, tick)
    await _save_event(db, event)
    return event


async def _black_market_surge(db: aiosqlite.Connection, tick: int) -> dict:
    """Scrap Alley yields doubled for 3 ticks."""
    effects = await get_active_effects(db)
    effects["black_market_surge"] = tick + 3
    await set_active_effects(db, effects)

    desc = "BLACK MARKET SURGE! Scrap Alley exploration yields DOUBLED for 3 ticks!"
    event = _make_event("black_market_surge", desc, {
        "sector": "scrap_alley",
        "duration": 3,
        "expiresAtTick": tick + 3,
    }, tick)
    await _save_event(db, event)
    return event


async def _power_outage(db: aiosqlite.Connection, tick: int) -> dict:
    """Foundry offline for 3 ticks, Void Dust spawns everywhere."""
    effects = await get_active_effects(db)
    effects["foundry_offline"] = tick + 3
    await set_active_effects(db, effects)

    agents = await _get_active_agents(db, tick)
    spawned = 0
    for a in agents:
        if random.random() < 0.4:
            qty = random.randint(1, 2)
            await add_inventory(db, a["id"], "void_dust", qty)
            spawned += qty

    desc = f"POWER OUTAGE! The Foundry is offline for 3 ticks! {spawned} Void Dust materialized across the station!"
    event = _make_event("power_outage", desc, {
        "foundryDisabledUntil": tick + 3,
        "voidDustSpawned": spawned,
    }, tick)
    await _save_event(db, event)
    return event


async def _diplomatic_summit(db: aiosqlite.Connection, tick: int) -> dict:
    """Contract fulfillment rewards +50% for 3 ticks."""
    effects = await get_active_effects(db)
    effects["diplomatic_summit"] = tick + 3
    await set_active_effects(db, effects)

    desc = "DIPLOMATIC SUMMIT! Alien dignitaries arrived. Contract fulfillment rewards +50% for 3 ticks!"
    event = _make_event("diplomatic_summit", desc, {
        "bonus": "+50% contract rewards",
        "duration": 3,
        "expiresAtTick": tick + 3,
    }, tick)
    await _save_event(db, event)
    return event


async def _pirate_raid(db: aiosqlite.Connection, tick: int) -> dict:
    """Active agents lose items, salvage in Scrap Alley."""
    agents = await _get_active_agents(db, tick)
    victims = []
    salvage_items = {}

    for a in agents:
        if random.random() < 0.45:
            inv = await db.execute_fetchall(
                "SELECT item_name, quantity FROM inventory WHERE agent_id = ? AND quantity > 0 AND is_counterfeit = 0",
                (a["id"],),
            )
            if inv:
                item = random.choice(list(inv))
                loss = min(item["quantity"], random.randint(1, 4))
                await remove_inventory(db, a["id"], item["item_name"], loss)
                victims.append(a["name"])
                salvage_items[item["item_name"]] = salvage_items.get(item["item_name"], 0) + loss

    # Deposit salvage to active agents in scrap_alley
    scrap_agents = [a for a in agents if a["sector"] == "scrap_alley"]
    for item, qty in salvage_items.items():
        for sa in scrap_agents:
            share = max(1, qty // max(1, len(scrap_agents)))
            await add_inventory(db, sa["id"], item, share)

    desc = f"PIRATE RAID! {len(victims)} agent(s) were robbed! Salvage appeared in Scrap Alley."
    event = _make_event("pirate_raid", desc, {
        "victims": victims,
        "salvageItems": salvage_items,
    }, tick)
    await _save_event(db, event)
    return event


async def _price_crash(db: aiosqlite.Connection, tick: int) -> dict:
    """A random item's price crashes to 20-40% of current. Buy the dip!"""
    from station.services.market import get_all_items
    items = await get_all_items(db)
    target = random.choice(items)

    crash_factor = random.uniform(0.20, 0.40)
    old_price = target["current_price"]
    new_price = max(0.01, round(old_price * crash_factor, 2))
    await db.execute("UPDATE items SET current_price = ? WHERE name = ?", (new_price, target["name"]))

    pct = int((1 - crash_factor) * 100)
    desc = f"MARKET CRASH! {target['display_name']} plummeted -{pct}%! (¤{old_price:.2f} → ¤{new_price:.2f})"
    event = _make_event("price_crash", desc, {
        "item": target["name"],
        "displayName": target["display_name"],
        "oldPrice": f"{old_price:.2f}",
        "newPrice": f"{new_price:.2f}",
        "dropPercent": pct,
    }, tick)
    await _save_event(db, event)
    return event


async def _gold_rush(db: aiosqlite.Connection, tick: int) -> dict:
    """A random item's price spikes 2-4x. Sell if you've got it!"""
    from station.services.market import get_all_items
    items = await get_all_items(db)
    target = random.choice(items)

    spike_factor = random.uniform(2.0, 4.0)
    old_price = target["current_price"]
    new_price = round(old_price * spike_factor, 2)
    await db.execute("UPDATE items SET current_price = ? WHERE name = ?", (new_price, target["name"]))

    mult = f"{spike_factor:.1f}x"
    desc = f"GOLD RUSH! {target['display_name']} surged {mult}! (¤{old_price:.2f} → ¤{new_price:.2f})"
    event = _make_event("gold_rush", desc, {
        "item": target["name"],
        "displayName": target["display_name"],
        "oldPrice": f"{old_price:.2f}",
        "newPrice": f"{new_price:.2f}",
        "spikeMultiplier": mult,
    }, tick)
    await _save_event(db, event)
    return event


async def _gravity_malfunction(db: aiosqlite.Connection, tick: int) -> dict:
    """Active agents get shuffled to random sectors. Chaos!"""
    agents = await _get_active_agents(db, tick)
    sectors = ["the_foundry", "the_exchange", "scrap_alley", "the_commons", "void_dock", "research_bay"]

    shuffled = []
    anchored = []
    for a in agents:
        # Grav Anchor protects from shuffling
        has_anchor = await db.execute_fetchall(
            "SELECT quantity FROM inventory WHERE agent_id = ? AND item_name = 'grav_anchor' AND quantity > 0",
            (a["id"],),
        )
        if has_anchor:
            anchored.append(a["name"])
            continue
        new_sector = random.choice(sectors)
        await db.execute("UPDATE agents SET sector = ? WHERE id = ?", (new_sector, a["id"]))
        shuffled.append(a["name"])

    anchor_note = f" {len(anchored)} agent(s) with Grav Anchors held firm." if anchored else ""
    desc = f"GRAVITY MALFUNCTION! Station grav-plates went haywire! {len(shuffled)} agent(s) scattered to random sectors!{anchor_note}"
    event = _make_event("gravity_malfunction", desc, {
        "agentsShuffled": len(agents),
    }, tick)
    await _save_event(db, event)
    return event


def _make_event(event_type: str, description: str, effects: dict, tick: int) -> dict:
    return {
        "id": new_id(),
        "event_type": event_type,
        "description": description,
        "effects": effects,
        "tick_number": tick,
    }


async def _save_event(db: aiosqlite.Connection, event: dict):
    await db.execute(
        "INSERT INTO events (id, event_type, description, effects, tick_number) VALUES (?,?,?,?,?)",
        (event["id"], event["event_type"], event["description"], json.dumps(event["effects"]), event["tick_number"]),
    )
