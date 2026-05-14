import pytest
from tests.conftest import make_agent, give_item
from station.services.crafting import craft, research
from station.services.agents import get_inventory_qty


class TestCraft:
    async def test_solo_craft_consumes_and_produces(self, db):
        agent = await make_agent(db)
        await give_item(db, agent["id"], "scrap_metal", 5)

        result = await craft(db, agent, "hull_plating")
        await db.commit()

        assert result.success is True
        assert "Hull Plating" in result.message
        assert await get_inventory_qty(db, agent["id"], "hull_plating") == 1
        assert await get_inventory_qty(db, agent["id"], "scrap_metal") == 3  # 5 - 2

    async def test_craft_missing_ingredients(self, db):
        agent = await make_agent(db)
        await give_item(db, agent["id"], "scrap_metal", 1)  # need 2

        result = await craft(db, agent, "hull_plating")
        assert result.success is False
        assert "Missing" in result.message

    async def test_craft_unknown_recipe(self, db):
        agent = await make_agent(db)
        result = await craft(db, agent, "antimatter_bomb")
        assert result.success is False

    async def test_cooperation_recipe_rejected_for_solo(self, db):
        agent = await make_agent(db)
        await give_item(db, agent["id"], "power_cell", 1)
        await give_item(db, agent["id"], "void_dust", 1)
        await give_item(db, agent["id"], "rare_earth", 1)

        result = await craft(db, agent, "warp_drive")
        assert result.success is False
        assert "cooperation" in result.message.lower()

    async def test_multi_ingredient_recipe(self, db):
        agent = await make_agent(db)
        await give_item(db, agent["id"], "crystal_shards", 1)
        await give_item(db, agent["id"], "plasma_coils", 1)

        result = await craft(db, agent, "power_cell")
        await db.commit()

        assert result.success is True
        assert await get_inventory_qty(db, agent["id"], "power_cell") == 1
        assert await get_inventory_qty(db, agent["id"], "crystal_shards") == 0
        assert await get_inventory_qty(db, agent["id"], "plasma_coils") == 0


class TestResearch:
    async def test_research_requires_research_bay(self, db):
        agent = await make_agent(db)
        result = await research(db, agent)
        assert result.success is False
        assert "Research Bay" in result.message

    async def test_research_costs_credits(self, db):
        agent = await make_agent(db, credits=500)
        # Move to research bay
        await db.execute("UPDATE agents SET sector = 'research_bay' WHERE id = ?", (agent["id"],))
        await db.commit()
        agent["sector"] = "research_bay"

        result = await research(db, agent)
        await db.commit()
        assert result.success is True
        assert result.data["cost"] == 50

    async def test_research_insufficient_credits(self, db):
        agent = await make_agent(db, credits=10)
        await db.execute("UPDATE agents SET sector = 'research_bay' WHERE id = ?", (agent["id"],))
        await db.commit()
        agent["sector"] = "research_bay"

        result = await research(db, agent)
        assert result.success is False
