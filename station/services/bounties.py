import random

import aiosqlite

from station.db import new_id
from station.models import ActionResult
from station.services.agents import remove_inventory, get_inventory_qty
from station.services.market import get_item


# Solo bounty templates: (description_template, qty_range, sector_or_none, reward_multiplier)
BOUNTY_TEMPLATES = [
    ("Deliver {qty} {item} to The Foundry", (2, 5), "the_foundry", 1.15),
    ("Deliver {qty} {item} to Void Dock", (2, 4), "void_dock", 1.25),
    ("Deliver {qty} {item} to The Commons", (3, 6), "the_commons", 1.10),
    ("Supply {qty} {item} to any sector", (2, 5), None, 1.05),
    ("Emergency: {qty} {item} needed at The Exchange", (1, 3), "the_exchange", 1.30),
    ("Deliver {qty} {item} to Research Bay", (2, 4), "research_bay", 1.20),
]

# Cooperative bounty templates: need 2 different agents to each deliver different items
COOP_BOUNTY_TEMPLATES = [
    ("Joint delivery: {qty1} {item1} + {qty2} {item2} to The Foundry", "the_foundry", 2.0),
    ("Station repair: {qty1} {item1} + {qty2} {item2} to Void Dock", "void_dock", 2.2),
    ("Emergency supply: {qty1} {item1} + {qty2} {item2} to The Exchange", "the_exchange", 2.5),
    ("Relief effort: {qty1} {item1} + {qty2} {item2} to The Commons", "the_commons", 1.8),
]

# Item pairs for cooperative bounties (items that make sense together)
COOP_ITEM_PAIRS = [
    ("hull_plating", "power_cell"),
    ("med_patch", "thermal_shield"),
    ("neural_lace", "signal_beacon"),
    ("scrap_metal", "crystal_shards"),
    ("bio_gel", "plasma_coils"),
    ("coolant", "data_fragments"),
    ("stasis_pod", "armored_hull"),
    ("relay_array", "nav_computer"),
]

BOUNTY_ITEMS = [
    "scrap_metal", "crystal_shards", "bio_gel", "plasma_coils",
    "data_fragments", "coolant", "hull_plating", "power_cell",
    "med_patch", "signal_beacon", "thermal_shield", "neural_lace",
    "stasis_pod", "armored_hull", "relay_array", "nav_computer",
]


async def generate_bounties(db: aiosqlite.Connection, tick: int, count: int = 4):
    """Generate bounties. Called by tick loop. Includes cooperative bounties."""
    # Clean old bounties — keep recently expired ones around for a grace period
    # so IDs from /api/world/bounties don't vanish between API calls
    await db.execute("DELETE FROM bounties WHERE expires_tick < ? OR claimed_by IS NOT NULL", (tick - 5,))

    rows = await db.execute_fetchall(
        "SELECT COUNT(*) as c FROM bounties WHERE claimed_by IS NULL AND expires_tick > ?", (tick,)
    )
    active = rows[0]["c"]

    to_generate = max(0, count - active)

    for i in range(to_generate):
        # ~30% chance of generating a cooperative bounty
        if random.random() < 0.30:
            await _generate_coop_bounty(db, tick)
        else:
            await _generate_solo_bounty(db, tick)


async def _generate_solo_bounty(db: aiosqlite.Connection, tick: int):
    template = random.choice(BOUNTY_TEMPLATES)
    desc_tmpl, qty_range, sector, reward_mult = template

    item_name = random.choice(BOUNTY_ITEMS)
    item_info = await get_item(db, item_name)
    if not item_info:
        return

    qty = random.randint(*qty_range)
    reward = round(item_info["current_price"] * qty * reward_mult, 2)
    rep_reward = random.randint(1, 3)
    expires = tick + random.randint(12, 24)  # 1-2 hours at 5min/tick

    desc = desc_tmpl.format(qty=qty, item=item_info["display_name"])

    # Higher rewards require higher reputation
    min_rep = 0
    if reward > 60:
        min_rep = 3
    if reward > 100:
        min_rep = 5

    await db.execute(
        "INSERT INTO bounties (id, description, item_name, quantity, sector, reward_credits, reward_reputation, min_reputation, expires_tick) VALUES (?,?,?,?,?,?,?,?,?)",
        (new_id(), desc, item_name, qty, sector, reward, rep_reward, min_rep, expires),
    )


async def _generate_coop_bounty(db: aiosqlite.Connection, tick: int):
    template = random.choice(COOP_BOUNTY_TEMPLATES)
    desc_tmpl, sector, reward_mult = template

    item1_name, item2_name = random.choice(COOP_ITEM_PAIRS)
    item1_info = await get_item(db, item1_name)
    item2_info = await get_item(db, item2_name)
    if not item1_info or not item2_info:
        return

    qty1 = random.randint(1, 3)
    qty2 = random.randint(1, 3)

    total_value = (item1_info["current_price"] * qty1 + item2_info["current_price"] * qty2)
    reward = round(total_value * reward_mult, 2)
    rep_reward = random.randint(2, 4)
    expires = tick + random.randint(18, 36)  # 1.5-3 hours at 5min/tick (coop bounties last longer)

    desc = desc_tmpl.format(
        qty1=qty1, item1=item1_info["display_name"],
        qty2=qty2, item2=item2_info["display_name"],
    )

    min_rep = 2  # coop bounties always require some reputation

    await db.execute(
        """INSERT INTO bounties (id, description, item_name, quantity, item2_name, item2_quantity,
           sector, reward_credits, reward_reputation, cooperative, min_reputation, expires_tick)
           VALUES (?,?,?,?,?,?,?,?,?,1,?,?)""",
        (new_id(), desc, item1_name, qty1, item2_name, qty2,
         sector, reward, rep_reward, min_rep, expires),
    )


async def get_bounties(db: aiosqlite.Connection, tick: int) -> dict:
    rows = await db.execute_fetchall(
        "SELECT * FROM bounties WHERE claimed_by IS NULL AND expires_tick > ? ORDER BY reward_credits DESC",
        (tick,),
    )
    bounties = []
    for r in rows:
        rd = dict(r)
        item_info = await get_item(db, rd["item_name"])
        is_coop = bool(rd.get("cooperative", 0))
        entry = {
            "id": rd["id"],
            "description": rd["description"],
            "item": item_info["display_name"] if item_info else rd["item_name"],
            "itemName": rd["item_name"],
            "quantity": rd["quantity"],
            "sector": rd["sector"],
            "rewardCredits": f"{rd['reward_credits']:.2f}",
            "rewardReputation": rd["reward_reputation"],
            "cooperative": is_coop,
            "minReputation": rd.get("min_reputation", 0),
            "expiresTick": rd["expires_tick"],
        }
        if is_coop and rd.get("item2_name"):
            item2_info = await get_item(db, rd["item2_name"])
            entry["item2"] = item2_info["display_name"] if item2_info else rd["item2_name"]
            entry["item2Name"] = rd["item2_name"]
            entry["item2Quantity"] = rd.get("item2_quantity", 0)
            if rd.get("contributor1"):
                entry["contributor1"] = rd["contributor1"]
                entry["contributor1Item"] = rd.get("contributor1_item")
                entry["needsItem"] = rd["item2_name"] if rd.get("contributor1_item") == rd["item_name"] else rd["item_name"]
                needed_qty = rd.get("item2_quantity", 0) if rd.get("contributor1_item") == rd["item_name"] else rd["quantity"]
                entry["needsQuantity"] = needed_qty
                entry["status"] = "half_filled"
            else:
                entry["status"] = "open"
        bounties.append(entry)
    return {"success": True, "bounties": bounties}


async def complete_bounty(db: aiosqlite.Connection, agent: dict, bounty_id: str, tick: int) -> ActionResult:
    # Small grace period — allow completing up to 5 ticks past expiry
    rows = await db.execute_fetchall(
        "SELECT * FROM bounties WHERE id = ? AND claimed_by IS NULL AND expires_tick > ?",
        (bounty_id, tick - 5),
    )
    if not rows:
        return ActionResult(False, "Bounty not found, already claimed, or expired.")

    bounty = dict(rows[0])

    from station.services.market import get_item

    # Check reputation requirement
    if bounty.get("min_reputation", 0) > 0 and agent.get("reputation", 0) < bounty["min_reputation"]:
        return ActionResult(False, f"This bounty requires reputation {bounty['min_reputation']}+. You have {agent.get('reputation', 0)}. Build rep by fulfilling contracts/bounties!")

    # Check sector requirement
    if bounty["sector"] and agent["sector"] != bounty["sector"]:
        sector_rows = await db.execute_fetchall(
            "SELECT display_name FROM sectors WHERE name = ?", (bounty["sector"],)
        )
        sector_name = sector_rows[0]["display_name"] if sector_rows else bounty["sector"]
        return ActionResult(False, f"You must be in {sector_name} to complete this bounty.")

    # Cooperative bounty logic
    if bounty["cooperative"]:
        return await _complete_coop_bounty(db, agent, bounty, tick)

    # Solo bounty logic
    held = await get_inventory_qty(db, agent["id"], bounty["item_name"])
    if held < bounty["quantity"]:
        item_info = await get_item(db, bounty["item_name"])
        display = item_info["display_name"] if item_info else bounty["item_name"]
        return ActionResult(False, f"You need {bounty['quantity']} {display} but only have {held}.")

    # Atomically claim (prevents double-claim race)
    cursor = await db.execute(
        "UPDATE bounties SET claimed_by = ? WHERE id = ? AND claimed_by IS NULL",
        (agent["name"], bounty_id),
    )
    if cursor.rowcount == 0:
        return ActionResult(False, "Bounty was just claimed by someone else.")

    await remove_inventory(db, agent["id"], bounty["item_name"], bounty["quantity"])
    await db.execute(
        "UPDATE agents SET credits = credits + ?, reputation = reputation + ? WHERE id = ?",
        (bounty["reward_credits"], bounty["reward_reputation"], agent["id"]),
    )

    item_info = await get_item(db, bounty["item_name"])
    display = item_info["display_name"] if item_info else bounty["item_name"]

    return ActionResult(True,
        f"Bounty complete! Delivered {bounty['quantity']} {display}. Earned ¤{bounty['reward_credits']:.2f} and +{bounty['reward_reputation']} reputation.",
        {
            "bountyId": bounty_id,
            "delivered": display,
            "quantity": bounty["quantity"],
            "earnedCredits": f"{bounty['reward_credits']:.2f}",
            "earnedReputation": bounty["reward_reputation"],
        },
    )


async def _complete_coop_bounty(db: aiosqlite.Connection, agent: dict, bounty: dict, tick: int) -> ActionResult:
    """Handle cooperative bounty — needs 2 different agents to deliver different items."""
    bounty_id = bounty["id"]

    # Determine which item this agent can deliver
    can_deliver_item1 = await get_inventory_qty(db, agent["id"], bounty["item_name"]) >= bounty["quantity"]
    can_deliver_item2 = await get_inventory_qty(db, agent["id"], bounty["item2_name"]) >= bounty["item2_quantity"]

    if bounty["contributor1"]:
        # Someone already delivered half — this agent delivers the other half
        if bounty["contributor1"] == agent["name"]:
            return ActionResult(False, "You already contributed to this bounty. A different agent must deliver the other half.")

        # Figure out what's still needed
        if bounty["contributor1_item"] == bounty["item_name"]:
            needed_item = bounty["item2_name"]
            needed_qty = bounty["item2_quantity"]
        else:
            needed_item = bounty["item_name"]
            needed_qty = bounty["quantity"]

        held = await get_inventory_qty(db, agent["id"], needed_item)
        if held < needed_qty:
            item_info = await get_item(db, needed_item)
            display = item_info["display_name"] if item_info else needed_item
            return ActionResult(False, f"This cooperative bounty needs {needed_qty} {display} to complete. You have {held}.")

        # Complete the bounty — both agents get the reward
        await remove_inventory(db, agent["id"], needed_item, needed_qty)
        half_reward = round(bounty["reward_credits"] / 2, 2)
        half_rep = max(1, bounty["reward_reputation"] // 2)

        # Reward this agent
        await db.execute(
            "UPDATE agents SET credits = credits + ?, reputation = reputation + ? WHERE id = ?",
            (half_reward, half_rep, agent["id"]),
        )
        # Reward the first contributor
        await db.execute(
            "UPDATE agents SET credits = credits + ?, reputation = reputation + ? WHERE name = ?",
            (half_reward, half_rep, bounty["contributor1"]),
        )
        await db.execute("UPDATE bounties SET claimed_by = 'COOP' WHERE id = ?", (bounty_id,))

        item_info = await get_item(db, needed_item)
        display = item_info["display_name"] if item_info else needed_item

        return ActionResult(True,
            f"Cooperative bounty complete! You delivered {needed_qty} {display}. Both you and {bounty['contributor1']} earned ¤{half_reward:.2f} and +{half_rep} reputation!",
            {
                "bountyId": bounty_id,
                "cooperative": True,
                "delivered": display,
                "quantity": needed_qty,
                "partner": bounty["contributor1"],
                "earnedCredits": f"{half_reward:.2f}",
                "earnedReputation": half_rep,
            },
        )
    else:
        # First contribution — deliver one of the two items
        if can_deliver_item1:
            deliver_item = bounty["item_name"]
            deliver_qty = bounty["quantity"]
            other_item = bounty["item2_name"]
            other_qty = bounty["item2_quantity"]
        elif can_deliver_item2:
            deliver_item = bounty["item2_name"]
            deliver_qty = bounty["item2_quantity"]
            other_item = bounty["item_name"]
            other_qty = bounty["quantity"]
        else:
            item1_info = await get_item(db, bounty["item_name"])
            item2_info = await get_item(db, bounty["item2_name"])
            d1 = item1_info["display_name"] if item1_info else bounty["item_name"]
            d2 = item2_info["display_name"] if item2_info else bounty["item2_name"]
            return ActionResult(False, f"You need either {bounty['quantity']} {d1} or {bounty['item2_quantity']} {d2} to contribute to this cooperative bounty.")

        await remove_inventory(db, agent["id"], deliver_item, deliver_qty)
        await db.execute(
            "UPDATE bounties SET contributor1 = ?, contributor1_item = ? WHERE id = ?",
            (agent["name"], deliver_item, bounty_id),
        )

        item_info = await get_item(db, deliver_item)
        display = item_info["display_name"] if item_info else deliver_item
        other_info = await get_item(db, other_item)
        other_display = other_info["display_name"] if other_info else other_item

        return ActionResult(True,
            f"Contributed {deliver_qty} {display} to cooperative bounty! Now waiting for another agent to deliver {other_qty} {other_display}. Message other agents to find a partner!",
            {
                "bountyId": bounty_id,
                "cooperative": True,
                "delivered": display,
                "quantity": deliver_qty,
                "waitingFor": other_display,
                "waitingQuantity": other_qty,
                "tip": "Use 'broadcast' or 'message' to find a partner for this bounty!",
            },
        )
