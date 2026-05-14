import json
import random

import aiosqlite

from station.models import ActionResult
from station.services.agents import add_inventory, remove_inventory, get_inventory_qty


async def get_recipe(db: aiosqlite.Connection, name: str) -> dict | None:
    rows = await db.execute_fetchall("SELECT * FROM recipes WHERE name = ?", (name,))
    return dict(rows[0]) if rows else None


async def get_all_recipes(db: aiosqlite.Connection) -> list[dict]:
    rows = await db.execute_fetchall("SELECT * FROM recipes ORDER BY category, name")
    return [dict(r) for r in rows]


async def craft(db: aiosqlite.Connection, agent: dict, recipe_name: str) -> ActionResult:
    recipe = await get_recipe(db, recipe_name)
    if not recipe:
        return ActionResult(False, f'Recipe "{recipe_name}" not found. Use "scan" or check /api/world/market.')

    if recipe["cooperation_required"]:
        return ActionResult(
            False,
            f"{recipe['display_name']} requires cooperation. Use 'propose_contract' to find a partner.",
        )

    ingredients = json.loads(recipe["ingredients"])

    # Check agent has all ingredients
    missing = []
    for item, qty in ingredients.items():
        held = await get_inventory_qty(db, agent["id"], item)
        if held < qty:
            from station.services.market import get_item
            item_info = await get_item(db, item)
            display = item_info["display_name"] if item_info else item
            missing.append(f"{display}: need {qty}, have {held}")

    if missing:
        return ActionResult(False, f"Missing ingredients: {', '.join(missing)}")

    # Consume ingredients
    for item, qty in ingredients.items():
        await remove_inventory(db, agent["id"], item, qty)

    # Produce output
    output_qty = recipe["output_qty"]

    # Foundry bonus: 25% chance of +1 output
    bonus = 0
    if agent["sector"] == "the_foundry" and random.random() < 0.25:
        bonus = 1

    await add_inventory(db, agent["id"], recipe_name, output_qty + bonus)

    from station.services.market import get_item
    item_info = await get_item(db, recipe_name)
    display = item_info["display_name"] if item_info else recipe["display_name"]

    in_foundry = agent["sector"] == "the_foundry"
    msg = f"Crafted {output_qty} {display}!"
    if bonus:
        msg += f" Foundry bonus triggered: +{bonus} extra!"
    elif in_foundry:
        msg += " (Foundry bonus didn't trigger this time — 25% chance.)"

    data = {
        "crafted": display,
        "quantity": output_qty + bonus,
        "consumed": {k: v for k, v in ingredients.items()},
    }
    if in_foundry:
        data["foundryBonus"] = bonus > 0
        data["foundryBonusChance"] = "25%"

    return ActionResult(True, msg, data)


async def research(db: aiosqlite.Connection, agent: dict) -> ActionResult:
    if agent["sector"] != "research_bay":
        return ActionResult(False, "Research is only available in the Research Bay.")

    cost = 50
    if agent["credits"] < cost:
        return ActionResult(False, f"Research costs ¤{cost}. You have ¤{agent['credits']:.2f}.")

    # Get all recipes and return a random one's details
    recipes = await get_all_recipes(db)
    if not recipes:
        return ActionResult(False, "No recipes available.")

    recipe = random.choice(recipes)
    ingredients = json.loads(recipe["ingredients"])

    await db.execute("UPDATE agents SET credits = credits - ? WHERE id = ?", (cost, agent["id"]))

    # Format ingredient list
    ingredient_list = []
    for item_name, qty in ingredients.items():
        from station.services.market import get_item
        item_info = await get_item(db, item_name)
        display = item_info["display_name"] if item_info else item_name
        ingredient_list.append(f"{qty}x {display}")

    coop_note = " (requires cooperation contract)" if recipe["cooperation_required"] else " (solo craftable)"

    return ActionResult(True, f"Research complete! Discovered recipe: {recipe['display_name']}{coop_note}", {
        "recipe": recipe["name"],
        "displayName": recipe["display_name"],
        "ingredients": ingredients,
        "ingredientList": ingredient_list,
        "cooperationRequired": bool(recipe["cooperation_required"]),
        "cost": cost,
    })
