import json
import math
from datetime import datetime, timezone

import aiosqlite

from station.db import new_id
from station.models import ActionResult
from station.services.agents import add_inventory, remove_inventory, get_inventory_qty
from station.services.crafting import get_recipe
from station.services.market import get_item


DECISION_WINDOW_TICKS = 72  # 6 hours at 5min/tick


async def propose_contract(
    db: aiosqlite.Connection, agent: dict, recipe_name: str, offer_items: dict, tick: int
) -> ActionResult:
    recipe = await get_recipe(db, recipe_name)
    if not recipe:
        return ActionResult(False, f'Recipe "{recipe_name}" not found.')

    if not recipe["cooperation_required"]:
        return ActionResult(
            False,
            f"{recipe['display_name']} can be crafted solo. Use 'craft' instead.",
        )

    # Check untrustworthiness
    if agent["untrustworthy_until"] and agent["untrustworthy_until"] > tick:
        return ActionResult(
            False,
            f"You are flagged as untrustworthy until tick {agent['untrustworthy_until']}. Cannot create contracts.",
        )

    # Check agent already has an open contract
    existing = await db.execute_fetchall(
        "SELECT id FROM contracts WHERE proposer_id = ? AND status IN ('open', 'active', 'deciding')",
        (agent["id"],),
    )
    if existing:
        return ActionResult(False, "You already have an active contract. Complete or cancel it first.")

    ingredients = json.loads(recipe["ingredients"])

    # Validate offer_items are a subset of the recipe ingredients
    for item, qty in offer_items.items():
        if item not in ingredients:
            item_info = await get_item(db, item)
            display = item_info["display_name"] if item_info else item
            return ActionResult(False, f"{display} is not needed for {recipe['display_name']}.")
        if qty > ingredients[item]:
            return ActionResult(False, f"Recipe only needs {ingredients[item]} {item}, you offered {qty}.")

    # Check agent has the offered items
    for item, qty in offer_items.items():
        held = await get_inventory_qty(db, agent["id"], item)
        if held < qty:
            item_info = await get_item(db, item)
            display = item_info["display_name"] if item_info else item
            return ActionResult(False, f"You need {qty} {display} but only have {held}.")

    # Calculate what the joiner needs to provide
    needed = {}
    for item, qty in ingredients.items():
        offered = offer_items.get(item, 0)
        remaining = qty - offered
        if remaining > 0:
            needed[item] = remaining

    if not needed:
        return ActionResult(False, "You're offering all ingredients. Use 'craft' for solo recipes, or leave some for your partner.")

    # Escrow the proposer's items
    for item, qty in offer_items.items():
        await remove_inventory(db, agent["id"], item, qty)

    contract_id = new_id()
    await db.execute(
        """INSERT INTO contracts (id, recipe_name, proposer_id, proposer_items, needed_items, created_tick)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (contract_id, recipe_name, agent["id"], json.dumps(offer_items), json.dumps(needed), tick),
    )

    # Format for display
    offered_display = []
    for item, qty in offer_items.items():
        info = await get_item(db, item)
        offered_display.append(f"{qty}x {info['display_name'] if info else item}")

    needed_display = []
    for item, qty in needed.items():
        info = await get_item(db, item)
        needed_display.append(f"{qty}x {info['display_name'] if info else item}")

    return ActionResult(
        True,
        f"Contract posted for {recipe['display_name']}! You contributed {', '.join(offered_display)}. Waiting for a partner to provide {', '.join(needed_display)}.",
        {
            "contractId": contract_id,
            "recipe": recipe["display_name"],
            "yourContribution": offer_items,
            "partnerNeeds": needed,
        },
    )


async def view_contracts(db: aiosqlite.Connection, agent: dict, tick: int) -> ActionResult:
    rows = await db.execute_fetchall("""
        SELECT c.*, r.display_name as recipe_display, a.name as proposer_name
        FROM contracts c
        JOIN recipes r ON r.name = c.recipe_name
        JOIN agents a ON a.id = c.proposer_id
        WHERE c.status IN ('open', 'active', 'deciding')
        ORDER BY c.created_at DESC
    """)

    contracts = []
    for r in rows:
        needed = json.loads(r["needed_items"])
        needed_display = {}
        for item, qty in needed.items():
            info = await get_item(db, item)
            needed_display[item] = {"display": info["display_name"] if info else item, "quantity": qty}

        entry = {
            "id": r["id"],
            "recipe": r["recipe_display"],
            "proposer": r["proposer_name"],
            "status": r["status"],
            "partnerNeeds": needed_display,
        }
        if r["status"] == "deciding":
            entry["decisionDeadline"] = r["decision_deadline"]
            # Show this agent's decision status
            if r["proposer_id"] == agent["id"]:
                entry["yourDecision"] = r["proposer_decision"]
            elif r["joiner_id"] == agent["id"]:
                entry["yourDecision"] = r["joiner_decision"]
        contracts.append(entry)

    if not contracts:
        return ActionResult(True, "No active contracts right now.", {"contracts": []})

    return ActionResult(True, f"{len(contracts)} active contract(s).", {"contracts": contracts})


async def join_contract(db: aiosqlite.Connection, agent: dict, contract_id: str, tick: int) -> ActionResult:
    rows = await db.execute_fetchall(
        "SELECT * FROM contracts WHERE id = ? AND status = 'open'", (contract_id,)
    )
    if not rows:
        return ActionResult(False, "Contract not found or not open.")

    contract = dict(rows[0])

    if contract["proposer_id"] == agent["id"]:
        return ActionResult(False, "You can't join your own contract.")

    # Check untrustworthiness
    if agent["untrustworthy_until"] and agent["untrustworthy_until"] > tick:
        return ActionResult(False, f"You are flagged as untrustworthy until tick {agent['untrustworthy_until']}.")

    needed = json.loads(contract["needed_items"])

    # Check agent has the needed items
    for item, qty in needed.items():
        held = await get_inventory_qty(db, agent["id"], item)
        if held < qty:
            info = await get_item(db, item)
            display = info["display_name"] if info else item
            return ActionResult(False, f"You need {qty} {display} but only have {held}.")

    # Escrow joiner's items
    for item, qty in needed.items():
        await remove_inventory(db, agent["id"], item, qty)

    recipe = await get_recipe(db, contract["recipe_name"])
    deadline = tick + DECISION_WINDOW_TICKS

    # Atomic status transition (prevents double-join race)
    cursor = await db.execute("""
        UPDATE contracts
        SET joiner_id = ?, joiner_items = ?, status = 'deciding', decision_deadline = ?
        WHERE id = ? AND status = 'open'
    """, (agent["id"], json.dumps(needed), deadline, contract_id))

    if cursor.rowcount == 0:
        # Race: someone else joined first — refund escrowed items
        for item, qty in needed.items():
            await add_inventory(db, agent["id"], item, qty)
        return ActionResult(False, "Contract was just joined by someone else.")

    return ActionResult(
        True,
        f"Joined contract for {recipe['display_name']}! Decision phase begins. You have until tick {deadline} to choose 'fulfill' or 'betray'. Auto-fulfills on timeout.",
        {
            "contractId": contract_id,
            "recipe": recipe["display_name"],
            "decisionDeadline": deadline,
            "yourContribution": needed,
            "actions": ["fulfill", "betray"],
        },
    )


async def fulfill_contract(db: aiosqlite.Connection, agent: dict, contract_id: str) -> ActionResult:
    return await _make_decision(db, agent, contract_id, "fulfill")


async def betray_contract(db: aiosqlite.Connection, agent: dict, contract_id: str) -> ActionResult:
    return await _make_decision(db, agent, contract_id, "betray")


async def _make_decision(db: aiosqlite.Connection, agent: dict, contract_id: str, decision: str) -> ActionResult:
    rows = await db.execute_fetchall(
        "SELECT * FROM contracts WHERE id = ? AND status = 'deciding'", (contract_id,)
    )
    if not rows:
        return ActionResult(False, "Contract not found or not in decision phase.")

    contract = dict(rows[0])
    recipe = await get_recipe(db, contract["recipe_name"])

    # Determine role
    if contract["proposer_id"] == agent["id"]:
        role = "proposer"
        other_decision_col = "joiner_decision"
    elif contract["joiner_id"] == agent["id"]:
        role = "joiner"
        other_decision_col = "proposer_decision"
    else:
        return ActionResult(False, "You are not part of this contract.")

    # Check if already decided
    my_decision_col = f"{role}_decision"
    if contract[my_decision_col] is not None:
        return ActionResult(False, f"You already chose to {contract[my_decision_col]}.")

    # Record decision
    await db.execute(
        f"UPDATE contracts SET {my_decision_col} = ? WHERE id = ?",
        (decision, contract_id),
    )

    # Check if both have decided
    other_decision = contract[other_decision_col]
    if other_decision is None:
        return ActionResult(
            True,
            f"You chose to {decision}. Waiting for the other party to decide.",
            {"contractId": contract_id, "yourDecision": decision, "waiting": True},
        )

    # Both decided — resolve
    return await _resolve_contract(db, contract_id, contract, recipe, role, decision, other_decision)


async def _resolve_contract(
    db: aiosqlite.Connection,
    contract_id: str,
    contract: dict,
    recipe: dict,
    my_role: str,
    my_decision: str,
    other_decision: str,
) -> ActionResult:
    proposer_items = json.loads(contract["proposer_items"])
    joiner_items = json.loads(contract["joiner_items"])
    all_items = {}
    for item, qty in proposer_items.items():
        all_items[item] = all_items.get(item, 0) + qty
    for item, qty in joiner_items.items():
        all_items[item] = all_items.get(item, 0) + qty

    proposer_id = contract["proposer_id"]
    joiner_id = contract["joiner_id"]

    now = datetime.now(timezone.utc).isoformat()

    if my_decision == "fulfill" and other_decision == "fulfill":
        # Both cooperate — craft succeeds, each gets 1 output
        output = recipe["name"]

        proposer = await db.execute_fetchall("SELECT sector FROM agents WHERE id = ?", (proposer_id,))
        joiner = await db.execute_fetchall("SELECT sector FROM agents WHERE id = ?", (joiner_id,))
        commons_bonus = (
            (proposer and proposer[0]["sector"] == "the_commons")
            or (joiner and joiner[0]["sector"] == "the_commons")
        )

        reputation_gain = 3
        reward_multiplier = 1.0
        active_bonuses = []
        if commons_bonus:
            reward_multiplier *= 1.10
            active_bonuses.append("commons")

        from station.services.world import get_active_effects, get_tick
        current_tick = await get_tick(db)
        active_effects = await get_active_effects(db)
        if active_effects.get("diplomatic_summit", 0) > current_tick:
            reward_multiplier *= 1.50
            active_bonuses.append("diplomatic_summit")

        if reward_multiplier > 1:
            reputation_gain = math.ceil(reputation_gain * reward_multiplier)

        await add_inventory(db, proposer_id, output, 1)
        await add_inventory(db, joiner_id, output, 1)

        # Credit bonus: each agent gets 25% of item value as a cooperation dividend
        item_info = await get_item(db, output)
        credit_bonus = round((item_info["current_price"] if item_info else 50) * 0.25 * reward_multiplier, 2)
        await db.execute("UPDATE agents SET credits = credits + ? WHERE id IN (?, ?)", (credit_bonus, proposer_id, joiner_id))

        # Reputation boost
        await db.execute("UPDATE agents SET reputation = reputation + ? WHERE id IN (?, ?)", (reputation_gain, proposer_id, joiner_id))

        await db.execute(
            "UPDATE contracts SET status = 'completed', proposer_decision = ?, joiner_decision = ?, resolved_at = ? WHERE id = ?",
            (my_decision if my_role == "proposer" else other_decision,
             my_decision if my_role == "joiner" else other_decision,
             now, contract_id),
        )

        display = item_info["display_name"] if item_info else output
        return ActionResult(True, f"Both parties honored the contract! Crafted {display}. Each party receives 1 + ¤{credit_bonus:.2f} cooperation bonus. Reputation +{reputation_gain}.", {
            "outcome": "mutual_cooperation",
            "crafted": display,
            "creditBonus": f"{credit_bonus:.2f}",
            "reputationGained": reputation_gain,
            "rewardMultiplier": round(reward_multiplier, 2),
            "activeBonuses": active_bonuses,
        })

    elif my_decision == "betray" and other_decision == "betray":
        # Both betray — 50% materials returned, rest destroyed
        for item, qty in proposer_items.items():
            returned = max(1, qty // 2)
            await add_inventory(db, proposer_id, item, returned)
        for item, qty in joiner_items.items():
            returned = max(1, qty // 2)
            await add_inventory(db, joiner_id, item, returned)

        # Reputation penalty for both
        await db.execute("UPDATE agents SET reputation = reputation - 5 WHERE id IN (?, ?)", (proposer_id, joiner_id))

        await db.execute(
            "UPDATE contracts SET status = 'betrayed', proposer_decision = 'betray', joiner_decision = 'betray', resolved_at = ? WHERE id = ?",
            (now, contract_id),
        )

        return ActionResult(True, "Both parties betrayed! Materials partially returned, some destroyed. Reputation -5.", {
            "outcome": "mutual_betrayal",
            "materialsLost": "~50%",
            "reputationLost": 5,
        })

    else:
        # One betrayed, one cooperated
        if my_decision == "betray":
            betrayer_id = contract["proposer_id"] if my_role == "proposer" else contract["joiner_id"]
            victim_id = contract["joiner_id"] if my_role == "proposer" else contract["proposer_id"]
        else:
            betrayer_id = contract["joiner_id"] if my_role == "proposer" else contract["proposer_id"]
            victim_id = contract["proposer_id"] if my_role == "proposer" else contract["joiner_id"]

        # Betrayer gets ALL escrowed materials
        for item, qty in all_items.items():
            await add_inventory(db, betrayer_id, item, qty)

        # Reputation effects
        await db.execute("UPDATE agents SET reputation = reputation - 10 WHERE id = ?", (betrayer_id,))
        await db.execute("UPDATE agents SET reputation = reputation + 1 WHERE id = ?", (victim_id,))

        # Flag betrayer as untrustworthy
        from station.services.world import get_tick
        current_tick = await get_tick(db)
        untrust_until = current_tick + 20
        await db.execute(
            "UPDATE agents SET untrustworthy_until = ? WHERE id = ?",
            (untrust_until, betrayer_id),
        )

        p_dec = "betray" if my_role == "proposer" and my_decision == "betray" else ("betray" if my_role != "proposer" and other_decision == "betray" else "fulfill")
        j_dec = "betray" if my_role == "joiner" and my_decision == "betray" else ("betray" if my_role != "joiner" and other_decision == "betray" else "fulfill")

        await db.execute(
            "UPDATE contracts SET status = 'betrayed', proposer_decision = ?, joiner_decision = ?, resolved_at = ? WHERE id = ?",
            (p_dec, j_dec, now, contract_id),
        )

        if my_decision == "betray":
            return ActionResult(True, "You betrayed your partner and took all escrowed materials! But you've been flagged as untrustworthy for 20 ticks. Reputation -10.", {
                "outcome": "you_betrayed",
                "materialsGained": all_items,
                "reputationLost": 10,
                "untrustworthyUntil": untrust_until,
            })
        else:
            return ActionResult(True, "Your partner betrayed you! They took all escrowed materials. You gained +1 reputation as the victim.", {
                "outcome": "you_were_betrayed",
                "materialsLost": proposer_items if my_role == "proposer" else joiner_items,
                "reputationGained": 1,
            })


async def auto_resolve_expired(db: aiosqlite.Connection, tick: int):
    """Auto-fulfill contracts past their deadline. Called by tick loop."""
    rows = await db.execute_fetchall(
        "SELECT * FROM contracts WHERE status = 'deciding' AND decision_deadline <= ?",
        (tick,),
    )
    for row in rows:
        contract = dict(row)
        recipe = await get_recipe(db, contract["recipe_name"])
        if not recipe:
            continue

        # Default undecided parties to 'fulfill'
        p_dec = contract["proposer_decision"] or "fulfill"
        j_dec = contract["joiner_decision"] or "fulfill"

        # Update decisions
        await db.execute(
            "UPDATE contracts SET proposer_decision = ?, joiner_decision = ? WHERE id = ?",
            (p_dec, j_dec, contract["id"]),
        )
        contract["proposer_decision"] = p_dec
        contract["joiner_decision"] = j_dec

        # Resolve using proposer's perspective
        await _resolve_contract(db, contract["id"], contract, recipe, "proposer", p_dec, j_dec)

    # Expire open contracts older than 100 ticks — return escrowed items to proposer
    expiring = await db.execute_fetchall(
        "SELECT id, proposer_id, proposer_items FROM contracts WHERE status = 'open' AND ? - created_tick > 144",  # 12 hours at 5min/tick
        (tick,),
    )
    for c in expiring:
        items = json.loads(c["proposer_items"]) if c["proposer_items"] else {}
        for item, qty in items.items():
            await add_inventory(db, c["proposer_id"], item, qty)
        await db.execute("UPDATE contracts SET status = 'expired' WHERE id = ?", (c["id"],))


STATION_FILL_AFTER_TICKS = 36  # 3 hours at 5min/tick
STATION_FEE_MULTIPLIER = 1.5  # station charges 50% premium over material value


async def station_fill_stale_contracts(db: aiosqlite.Connection, tick: int):
    """Fill contracts that have been open too long with no joiner.
    The station acts as the partner — always fulfills, but charges a credit fee.
    The proposer gets 1 of the output item (not 2, since there's no real partner)."""
    rows = await db.execute_fetchall(
        "SELECT * FROM contracts WHERE status = 'open'",
    )
    for row in rows:
        contract = dict(row)
        if tick - contract["created_tick"] < STATION_FILL_AFTER_TICKS:
            continue

        recipe = await get_recipe(db, contract["recipe_name"])
        if not recipe:
            continue

        needed = json.loads(contract["needed_items"])

        # Calculate the station fee: sum of base prices of needed items * multiplier
        total_fee = 0.0
        for item_name, qty in needed.items():
            item = await get_item(db, item_name)
            if item:
                total_fee += item["current_price"] * qty
        total_fee = round(total_fee * STATION_FEE_MULTIPLIER, 2)

        # Check proposer can afford the fee
        proposer = await db.execute_fetchall(
            "SELECT credits, name FROM agents WHERE id = ?", (contract["proposer_id"],)
        )
        if not proposer or proposer[0]["credits"] < total_fee:
            continue  # can't afford, leave open

        # Charge the fee and complete
        await db.execute(
            "UPDATE agents SET credits = credits - ? WHERE id = ?",
            (total_fee, contract["proposer_id"]),
        )

        # Give the proposer 1 output item
        output = recipe["name"]
        await add_inventory(db, contract["proposer_id"], output, 1)

        # Reputation for completing via station (smaller bonus)
        await db.execute(
            "UPDATE agents SET reputation = reputation + 1 WHERE id = ?",
            (contract["proposer_id"],),
        )

        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "UPDATE contracts SET status = 'completed', joiner_id = 'STATION', joiner_items = ?, proposer_decision = 'fulfill', joiner_decision = 'fulfill', resolved_at = ? WHERE id = ?",
            (json.dumps(needed), now, contract["id"]),
        )

        # Log it
        from station.services.world import log_activity
        await log_activity(
            db, proposer[0]["name"], "station_contract_fill",
            {"contract_id": contract["id"], "recipe": recipe["display_name"], "fee": total_fee},
            {"success": True, "message": f"Station filled your contract for {recipe['display_name']}. Fee: ¤{total_fee:.2f}. You received 1 {recipe['display_name']}."},
            tick,
        )
