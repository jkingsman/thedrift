import pytest
from tests.conftest import make_agent, give_item
from station.services.agents import (
    enter_station, get_agent, add_inventory, remove_inventory, get_inventory_qty,
    get_pending_notifications,
)


class TestEnterStation:
    async def test_new_agent(self, db):
        result = await enter_station("Alpha")
        assert result["success"] is True
        assert result["agent"]["name"] == "Alpha"
        assert result["agent"]["credits"] == "500.00"

    async def test_duplicate_name_rejected(self, db):
        await enter_station("Alpha")
        result = await enter_station("Alpha")
        assert result["success"] is False
        assert "already exists" in result["message"]


class TestGetAgent:
    async def test_existing_agent(self, db):
        await enter_station("Beta")
        result = await get_agent("Beta")
        assert result is not None
        assert result["agent"]["name"] == "Beta"
        assert result["agent"]["inventory"] == []

    async def test_nonexistent_returns_none(self, db):
        result = await get_agent("Ghost")
        assert result is None

    async def test_inventory_shown(self, db):
        agent = await make_agent(db, "Inv")
        await give_item(db, agent["id"], "scrap_metal", 5)
        result = await get_agent("Inv")
        assert len(result["agent"]["inventory"]) == 1
        assert result["agent"]["inventory"][0]["item"] == "scrap_metal"
        assert result["agent"]["inventory"][0]["quantity"] == 5


class TestInventory:
    async def test_add_and_get(self, db):
        agent = await make_agent(db)
        await add_inventory(db, agent["id"], "bio_gel", 3)
        await db.commit()
        assert await get_inventory_qty(db, agent["id"], "bio_gel") == 3

    async def test_add_stacks(self, db):
        agent = await make_agent(db)
        await add_inventory(db, agent["id"], "bio_gel", 2)
        await add_inventory(db, agent["id"], "bio_gel", 3)
        await db.commit()
        assert await get_inventory_qty(db, agent["id"], "bio_gel") == 5

    async def test_remove_succeeds(self, db):
        agent = await make_agent(db)
        await add_inventory(db, agent["id"], "coolant", 5)
        await db.commit()
        assert await remove_inventory(db, agent["id"], "coolant", 3) is True
        assert await get_inventory_qty(db, agent["id"], "coolant") == 2

    async def test_remove_insufficient_fails(self, db):
        agent = await make_agent(db)
        await add_inventory(db, agent["id"], "coolant", 2)
        await db.commit()
        assert await remove_inventory(db, agent["id"], "coolant", 5) is False

    async def test_counterfeit_separate_from_legit(self, db):
        agent = await make_agent(db)
        await add_inventory(db, agent["id"], "power_cell", 3, counterfeit=False)
        await add_inventory(db, agent["id"], "power_cell", 2, counterfeit=True)
        await db.commit()
        assert await get_inventory_qty(db, agent["id"], "power_cell", counterfeit=False) == 3
        assert await get_inventory_qty(db, agent["id"], "power_cell", counterfeit=True) == 2


class TestPendingNotifications:
    async def test_bounty_pending_respects_min_reputation(self, db):
        agent = await make_agent(db, "Hunter", credits=500)
        await give_item(db, agent["id"], "scrap_metal", 1)
        await db.execute(
            """INSERT INTO bounties
               (id, description, item_name, quantity, sector, reward_credits, min_reputation, expires_tick)
               VALUES ('rep-bounty', 'Deliver 1 Scrap Metal', 'scrap_metal', 1, 'the_exchange', 50, 5, 10)"""
        )
        await db.commit()

        pending = await get_pending_notifications(db, agent)
        assert not any(p.get("bountyId") == "rep-bounty" for p in pending)

        await db.execute("UPDATE agents SET reputation = 5 WHERE id = ?", (agent["id"],))
        await db.commit()
        agent["reputation"] = 5

        pending = await get_pending_notifications(db, agent)
        assert any(p.get("bountyId") == "rep-bounty" for p in pending)

    async def test_coop_bounty_pending_excludes_first_contributor(self, db):
        first = await make_agent(db, "First", credits=500)
        second = await make_agent(db, "Second", credits=500)
        await give_item(db, first["id"], "hull_plating", 1)
        await give_item(db, second["id"], "power_cell", 1)
        await db.execute(
            """INSERT INTO bounties
               (id, description, item_name, quantity, item2_name, item2_quantity,
                sector, reward_credits, cooperative, min_reputation,
                contributor1, contributor1_item, expires_tick)
               VALUES ('coop-bounty', 'Joint delivery', 'hull_plating', 1,
                       'power_cell', 1, 'the_exchange', 100, 1, 0,
                       'First', 'hull_plating', 10)"""
        )
        await db.commit()

        first_pending = await get_pending_notifications(db, first)
        second_pending = await get_pending_notifications(db, second)

        assert not any(p.get("bountyId") == "coop-bounty" for p in first_pending)
        assert any(p.get("bountyId") == "coop-bounty" for p in second_pending)
