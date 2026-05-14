import json
import random

import aiosqlite

from station.models import ActionResult
from station.services.agents import add_inventory, remove_inventory, get_inventory_qty


# Loot tables: sector -> [(item, weight, min_qty, max_qty)]
SECTOR_LOOT = {
    "scrap_alley": [
        ("scrap_metal", 30, 1, 4),
        ("bio_gel", 20, 1, 3),
        ("rare_earth", 5, 1, 1),
        ("coolant", 15, 1, 2),
        ("data_fragments", 10, 1, 2),
    ],
    "the_foundry": [
        ("scrap_metal", 25, 1, 3),
        ("plasma_coils", 20, 1, 2),
        ("coolant", 25, 1, 3),
    ],
    "void_dock": [
        ("crystal_shards", 20, 1, 2),
        ("void_dust", 10, 1, 1),
        ("plasma_coils", 15, 1, 2),
    ],
    "the_commons": [
        ("bio_gel", 25, 1, 2),
        ("coolant", 25, 1, 2),
        ("data_fragments", 10, 1, 1),
    ],
    "research_bay": [
        ("crystal_shards", 15, 1, 2),
        ("data_fragments", 30, 1, 3),
    ],
    "the_exchange": [
        ("data_fragments", 20, 1, 2),
        ("scrap_metal", 10, 1, 1),
    ],
}


async def reset_consecutive_explores(db: aiosqlite.Connection, agent_id: str):
    """Call this when agent does something other than explore."""
    await db.execute("UPDATE agents SET consecutive_explores = 0 WHERE id = ?", (agent_id,))


async def explore(db: aiosqlite.Connection, agent: dict, active_effects: dict) -> ActionResult:
    sector = agent["sector"]
    loot_table = SECTOR_LOOT.get(sector, [])

    if not loot_table:
        return ActionResult(True, "Nothing of interest here.", {"found": []})

    # Track consecutive explores for diminishing returns
    consec = agent.get("consecutive_explores", 0)
    await db.execute(
        "UPDATE agents SET consecutive_explores = consecutive_explores + 1 WHERE id = ?",
        (agent["id"],),
    )

    # Diminishing returns: after 3 consecutive explores, yields drop
    # 0-2: full yields, 3-5: 50% chance of finding nothing, 6+: 75% nothing
    fatigue_msg = ""
    if consec >= 6:
        if random.random() < 0.75:
            return ActionResult(True, "You've been scavenging too long — the area's picked clean. Try crafting, trading, or moving to a new sector.", {
                "found": [], "fatigued": True, "consecutiveExplores": consec + 1,
            })
        fatigue_msg = " (area getting sparse)"
    elif consec >= 3:
        if random.random() < 0.50:
            return ActionResult(True, "Slim pickings — you've been exploring the same area too long. Mix up your activities.", {
                "found": [], "fatigued": True, "consecutiveExplores": consec + 1,
            })
        fatigue_msg = " (diminishing finds)"

    # Determine if we find something (70% base chance)
    if random.random() > 0.70:
        cr = round(random.uniform(5, 25), 2)
        # Reduce credit finds with fatigue too
        if consec >= 3:
            cr = round(cr * 0.5, 2)
        await db.execute("UPDATE agents SET credits = credits + ? WHERE id = ?", (cr, agent["id"]))
        return ActionResult(True, f"Found ¤{cr:.2f} wedged between some panels!{fatigue_msg}", {
            "found": [{"type": "credits", "amount": cr}],
        })

    # Pick from loot table (weighted)
    items_found = []
    # Check for black_market_surge doubling yields
    multiplier = 2 if active_effects.get("black_market_surge") and sector == "scrap_alley" else 1

    # Lucky Charm: +25% yield (extra find chance)
    has_charm = await get_inventory_qty(db, agent["id"], "lucky_charm") > 0

    # Roll 1-2 finds (Lucky Charm gives chance of 3)
    num_finds = random.randint(1, 2)
    if has_charm and random.random() < 0.25:
        num_finds += 1
    total_weight = sum(w for _, w, _, _ in loot_table)

    for _ in range(num_finds):
        roll = random.uniform(0, total_weight)
        cumulative = 0
        for item_name, weight, min_q, max_q in loot_table:
            cumulative += weight
            if roll <= cumulative:
                qty = random.randint(min_q, max_q) * multiplier
                await add_inventory(db, agent["id"], item_name, qty)

                from station.services.market import get_item
                item_info = await get_item(db, item_name)
                display = item_info["display_name"] if item_info else item_name

                items_found.append({"item": display, "itemName": item_name, "quantity": qty})
                break

    if not items_found:
        return ActionResult(True, "You search around but find nothing useful.", {"found": []})

    # Aggregate duplicate items
    aggregated = {}
    for f in items_found:
        key = f["itemName"]
        if key in aggregated:
            aggregated[key]["quantity"] += f["quantity"]
        else:
            aggregated[key] = dict(f)
    items_found = list(aggregated.values())

    desc_parts = [f"{f['quantity']}x {f['item']}" for f in items_found]
    return ActionResult(True, f"Scavenged {', '.join(desc_parts)} from {sector}.{fatigue_msg}", {
        "found": items_found,
        "sector": sector,
    })


async def scan(db: aiosqlite.Connection, agent: dict) -> ActionResult:
    sector = agent["sector"]
    loot_table = SECTOR_LOOT.get(sector, [])

    # Get sector info
    rows = await db.execute_fetchall("SELECT * FROM sectors WHERE name = ?", (sector,))
    sector_info = dict(rows[0]) if rows else {}

    # Who's here
    agents_here = await db.execute_fetchall(
        "SELECT name, reputation FROM agents WHERE sector = ? AND name != ?",
        (sector, agent["name"]),
    )

    available_materials = []
    for item_name, weight, min_q, max_q in loot_table:
        from station.services.market import get_item
        item_info = await get_item(db, item_name)
        display = item_info["display_name"] if item_info else item_name
        rarity = "common" if weight >= 20 else "uncommon" if weight >= 10 else "rare"
        available_materials.append({"item": display, "itemName": item_name, "rarity": rarity})

    # Show what other agents are carrying (encourages trade outreach)
    agents_detail = []
    for a in agents_here:
        inv_rows = await db.execute_fetchall(
            "SELECT item_name, SUM(quantity) as qty FROM inventory WHERE agent_id = (SELECT id FROM agents WHERE name = ?) AND quantity > 0 GROUP BY item_name",
            (a["name"],),
        )
        carrying = [{"item": r["item_name"], "quantity": r["qty"]} for r in inv_rows]
        agents_detail.append({
            "name": a["name"],
            "reputation": a["reputation"],
            "carrying": carrying,
        })

    # Environmental effects from items held by agents in this sector
    all_agents_in_sector = await db.execute_fetchall(
        "SELECT id FROM agents WHERE sector = ?", (sector,)
    )
    sector_agent_ids = [a["id"] for a in all_agents_in_sector]

    cooling_units = 0
    jukeboxes = 0
    for aid in sector_agent_ids:
        cu = await db.execute_fetchall(
            "SELECT COALESCE(SUM(quantity), 0) as qty FROM inventory WHERE agent_id = ? AND item_name = 'cooling_unit'", (aid,)
        )
        cooling_units += cu[0]["qty"] if cu else 0
        jb = await db.execute_fetchall(
            "SELECT COALESCE(SUM(quantity), 0) as qty FROM inventory WHERE agent_id = ? AND item_name = 'jukebox'", (aid,)
        )
        jukeboxes += jb[0]["qty"] if jb else 0

    # Temperature flavor
    if cooling_units >= 10:
        temp = "FREEZING — frost forming on the walls"
    elif cooling_units >= 5:
        temp = "Cold — breath visible"
    elif cooling_units >= 1:
        temp = "Cool — pleasant"
    else:
        temp = "Normal"

    # Noise flavor
    if jukeboxes >= 5:
        noise = "DEAFENING — you can barely hear yourself think"
    elif jukeboxes >= 3:
        noise = "Loud — music from every direction"
    elif jukeboxes >= 1:
        noise = "Lively — someone's playing tunes"
    else:
        noise = "Quiet"

    environment = {
        "temperature": temp,
        "coolingUnits": cooling_units,
        "ambiance": noise,
        "jukeboxes": jukeboxes,
    }

    return ActionResult(True, f"Scan of {sector_info.get('display_name', sector)} complete.", {
        "sector": sector,
        "displayName": sector_info.get("display_name", sector),
        "description": sector_info.get("description", ""),
        "bonus": sector_info.get("bonus", ""),
        "availableMaterials": available_materials,
        "agentsPresent": agents_detail,
        "environment": environment,
    })


async def sabotage(db: aiosqlite.Connection, agent: dict, target_name: str, tick: int = 0) -> ActionResult:
    if agent["sector"] != "scrap_alley":
        return ActionResult(False, "Sabotage is only possible in Scrap Alley.")

    if target_name == agent["name"]:
        return ActionResult(False, "You can't sabotage yourself.")

    # Find target
    from station.services.agents import get_agent_raw
    target = await get_agent_raw(db, target_name)
    if not target:
        return ActionResult(False, f"Agent '{target_name}' not found.")
    if target["sector"] != "scrap_alley":
        return ActionResult(False, f"{target_name} is not in Scrap Alley.")

    # 40% success chance, modified by reputation
    rep_bonus = max(-0.1, min(0.1, (agent["reputation"] - target["reputation"]) * 0.01))
    success_chance = 0.40 + rep_bonus

    if random.random() < success_chance:
        # Steal a random item from target
        target_inv = await db.execute_fetchall(
            "SELECT item_name, quantity FROM inventory WHERE agent_id = ? AND quantity > 0",
            (target["id"],),
        )
        if not target_inv:
            # Steal credits instead
            stolen_cr = min(target["credits"] * 0.1, 50)
            if stolen_cr > 0:
                await db.execute("UPDATE agents SET credits = credits - ? WHERE id = ?", (stolen_cr, target["id"]))
                await db.execute("UPDATE agents SET credits = credits + ? WHERE id = ?", (stolen_cr, agent["id"]))
                return ActionResult(True, f"Lifted ¤{stolen_cr:.2f} from {target_name}'s account!", {
                    "stolen": "credits", "amount": f"{stolen_cr:.2f}", "from": target_name,
                })
            return ActionResult(True, f"{target_name} has nothing worth stealing.", {"stolen": None})

        # Pick random item
        victim_item = random.choice(list(target_inv))
        steal_qty = min(victim_item["quantity"], random.randint(1, 3))

        await remove_inventory(db, target["id"], victim_item["item_name"], steal_qty)
        await add_inventory(db, agent["id"], victim_item["item_name"], steal_qty)

        from station.services.market import get_item
        item_info = await get_item(db, victim_item["item_name"])
        display = item_info["display_name"] if item_info else victim_item["item_name"]

        return ActionResult(True, f"Nabbed {steal_qty} {display} from {target_name}'s stash!", {
            "stolen": display, "itemName": victim_item["item_name"],
            "quantity": steal_qty, "from": target_name,
        })
    else:
        # Failed — fined and jailed
        penalty = round(random.uniform(30, 80), 2)
        penalty = min(penalty, agent["credits"])
        jail_until = tick + random.randint(2, 4)
        await db.execute(
            "UPDATE agents SET credits = credits - ?, reputation = reputation - 3, jailed_until = ? WHERE id = ?",
            (penalty, jail_until, agent["id"]),
        )

        return ActionResult(False, f"Sabotage failed! Station security fined you ¤{penalty:.2f} and threw you in the brig until tick {jail_until}. Reputation -3.", {
            "penalty": f"{penalty:.2f}",
            "reputationLost": 3,
            "jailedUntil": jail_until,
            "target": target_name,
        })


RUMOR_WINDOW_MINUTES = 30
RUMOR_BURST_THRESHOLD = 3   # per agent per item in the window
RUMOR_COOLDOWN_MINUTES = 60  # after a bubble pop, this agent can't move this item
RUMOR_MIN_COST = 8.0
RUMOR_MAX_COST = 75.0
RUMOR_COST_PRICE_FACTOR = 0.12
RUMOR_MIN_MOVE_PCT = 0.02
RUMOR_MAX_MOVE_PCT = 0.10
RUMOR_MAX_ABSOLUTE_MOVE = 20.0


async def spread_rumor(db: aiosqlite.Connection, agent: dict, item_name: str, direction: str) -> ActionResult:
    """Spread a rumor to push an item's price up or down."""
    if direction not in ("up", "down"):
        return ActionResult(False, 'Direction must be "up" or "down".')

    from station.services.market import get_item
    item = await get_item(db, item_name)
    if not item:
        return ActionResult(False, f'Item "{item_name}" not found.')

    rumor_cost = round(max(RUMOR_MIN_COST, min(RUMOR_MAX_COST, item["current_price"] * RUMOR_COST_PRICE_FACTOR)), 2)

    # Check cooldown: has this agent popped a bubble on this item recently?
    cooldown_check = await db.execute_fetchall(
        """SELECT COUNT(*) as c FROM activity_log
           WHERE agent_name = ? AND action = 'rumor'
           AND json_extract(result, '$.data.reason') = 'bubble_popped'
           AND json_extract(params, '$.item') = ?
           AND timestamp > datetime('now', ? || ' minutes')""",
        (agent["name"], item_name, f"-{RUMOR_COOLDOWN_MINUTES}"),
    )
    if cooldown_check and cooldown_check[0]["c"] > 0:
        return ActionResult(True,
            f"The market remembers your last hype campaign on {item['display_name']}. You can't influence this item's price for a while.",
            {"item": item["display_name"], "direction": direction, "effectiveness": 0, "cooldown": True, "cost": "0.00"},
        )

    if agent["credits"] < rumor_cost:
        return ActionResult(False, f"Starting a credible rumor about {item['display_name']} costs ¤{rumor_cost:.2f}. You have ¤{agent['credits']:.2f}.")

    cursor = await db.execute(
        "UPDATE agents SET credits = credits - ? WHERE id = ? AND credits >= ?",
        (rumor_cost, agent["id"], rumor_cost),
    )
    if cursor.rowcount == 0:
        return ActionResult(False, "Not enough credits to start the rumor.")

    # Per-agent, per-item: how many times has THIS agent rumored THIS item recently?
    agent_item_rumors = await db.execute_fetchall(
        """SELECT COUNT(*) as c FROM activity_log
           WHERE agent_name = ? AND action = 'rumor'
           AND json_extract(params, '$.item') = ?
           AND timestamp > datetime('now', ? || ' minutes')""",
        (agent["name"], item_name, f"-{RUMOR_WINDOW_MINUTES}"),
    )
    my_item_count = agent_item_rumors[0]["c"] if agent_item_rumors else 0

    if my_item_count >= RUMOR_BURST_THRESHOLD:
        # Bubble pops for THIS agent on THIS item — price crashes toward base
        crash = round(item["base_price"] * random.uniform(0.7, 0.9), 2)
        old_price = item["current_price"]
        await db.execute("UPDATE items SET current_price = ? WHERE name = ?", (crash, item_name))
        return ActionResult(True,
            f'BUBBLE BURST! You paid ¤{rumor_cost:.2f} to hype {item["display_name"]}, but pushed too hard — the market overcorrected. Price crashed to ¤{crash:.2f}. You won\'t be able to influence this item for a while.',
            {
                "item": item["display_name"],
                "direction": "crash",
                "cost": f"{rumor_cost:.2f}",
                "oldPrice": f"{old_price:.2f}",
                "newPrice": f"{crash:.2f}",
                "priceMove": f"{crash - old_price:.2f}",
                "reason": "bubble_popped",
                "newBalance": f"{agent['credits'] - rumor_cost:.2f}",
            },
        )

    # General credibility: count ALL rumors in this agent's last 20 actions
    recent = await db.execute_fetchall(
        "SELECT action FROM activity_log WHERE agent_name = ? ORDER BY timestamp DESC LIMIT 20",
        (agent["name"],),
    )
    recent_rumors = sum(1 for r in recent if r["action"] == "rumor")
    credibility = max(0.0, 1.0 - (recent_rumors * 0.25))

    if credibility <= 0:
        return ActionResult(True,
            f"You paid ¤{rumor_cost:.2f}, but the market has heard enough from you. The rumor had no effect — try other activities first.",
            {
                "item": item["display_name"],
                "direction": direction,
                "cost": f"{rumor_cost:.2f}",
                "effectiveness": 0,
                "credibility": 0,
                "newBalance": f"{agent['credits'] - rumor_cost:.2f}",
            },
        )

    # Base effect: a capped absolute price move. The bribe may cost more than the
    # resulting per-unit price change, especially for cheap commodities.
    base_delta = item["current_price"] * random.uniform(RUMOR_MIN_MOVE_PCT, RUMOR_MAX_MOVE_PCT)

    # Sector bonuses
    sector = agent["sector"]
    sector_mult = 1.0
    if sector == "the_commons":
        sector_mult = 2.0
    elif sector == "the_exchange":
        sector_mult = 1.5
    elif sector == "void_dock":
        sector_mult = 0.5

    price_delta = min(RUMOR_MAX_ABSOLUTE_MOVE, round(base_delta * sector_mult * credibility, 2))
    if direction == "down":
        price_delta = -price_delta

    old_price = item["current_price"]
    new_price = max(0.01, round(old_price + price_delta, 2))
    actual_move = round(new_price - old_price, 2)
    await db.execute("UPDATE items SET current_price = ? WHERE name = ?", (new_price, item_name))

    pct_display = abs(round((actual_move / old_price) * 100, 1)) if old_price else 0
    cred_note = f" (credibility: {credibility:.0%})" if credibility < 1.0 else ""
    sect_note = f" ({sector_mult:.1f}x from {sector.replace('_', ' ').title()}!)" if sector_mult != 1.0 else ""
    move_vs_cost = "exceeded" if abs(actual_move) > rumor_cost else "did not exceed"

    return ActionResult(True,
        f'Paid ¤{rumor_cost:.2f} to seed intel: "{item["display_name"]} trending {direction}." Market shifted {("+" if actual_move >= 0 else "-")}¤{abs(actual_move):.2f} ({pct_display}%){sect_note}{cred_note}; the per-unit move {move_vs_cost} the bribe.',
        {
            "item": item["display_name"],
            "direction": direction,
            "cost": f"{rumor_cost:.2f}",
            "oldPrice": f"{old_price:.2f}",
            "newPrice": f"{new_price:.2f}",
            "priceMove": f"{actual_move:.2f}",
            "moveExceededCost": abs(actual_move) > rumor_cost,
            "effectiveness": sector_mult,
            "credibility": credibility,
            "newBalance": f"{agent['credits'] - rumor_cost:.2f}",
        },
    )


async def forge(db: aiosqlite.Connection, agent: dict, item_name: str, quantity: int, tick: int) -> ActionResult:
    """Forge counterfeit items. Only in Scrap Alley. ¤5 each."""
    if agent["sector"] != "scrap_alley":
        return ActionResult(False, "Forgery is only possible in Scrap Alley.")

    if quantity <= 0:
        return ActionResult(False, "Quantity must be positive.")

    from station.services.market import get_item
    item = await get_item(db, item_name)
    if not item:
        return ActionResult(False, f'Item "{item_name}" not found.')

    cost = 5.0 * quantity
    if agent["credits"] < cost:
        return ActionResult(False, f"Forging costs ¤{cost:.0f} ({quantity} x ¤5). You have ¤{agent['credits']:.2f}.")

    await db.execute("UPDATE agents SET credits = credits - ? WHERE id = ?", (cost, agent["id"]))
    await add_inventory(db, agent["id"], item_name, quantity, counterfeit=True)

    return ActionResult(True,
        f"Forged {quantity} counterfeit {item['display_name']} for ¤{cost:.0f}. Shoddy work, but it might pass a quick glance.",
        {
            "forged": item["display_name"],
            "quantity": quantity,
            "cost": cost,
            "warning": "Counterfeits may be detected when selling to the station.",
        },
    )
