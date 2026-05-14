import pytest
from tests.conftest import make_agent, give_item
from station.services.exploration import explore, scan, sabotage, spread_rumor, forge
from station.services.market import get_item


class TestExplore:
    async def test_explore_finds_something(self, db):
        agent = await make_agent(db)
        await db.execute("UPDATE agents SET sector = 'scrap_alley' WHERE id = ?", (agent["id"],))
        await db.commit()
        agent["sector"] = "scrap_alley"

        # Run enough times that at least one finds items
        found_items = False
        found_credits = False
        for _ in range(20):
            result = await explore(db, agent, {})
            await db.commit()
            assert result.success is True
            for f in result.data["found"]:
                if f.get("type") == "credits":
                    found_credits = True
                else:
                    found_items = True

        assert found_items or found_credits

    async def test_explore_sector_affects_loot(self, db):
        agent = await make_agent(db)
        await db.execute("UPDATE agents SET sector = 'void_dock' WHERE id = ?", (agent["id"],))
        await db.commit()
        agent["sector"] = "void_dock"

        items_found = set()
        for _ in range(30):
            result = await explore(db, agent, {})
            await db.commit()
            for f in result.data["found"]:
                if "itemName" in f:
                    items_found.add(f["itemName"])

        # Void dock should yield crystal_shards, void_dust, or plasma_coils
        possible = {"crystal_shards", "void_dust", "plasma_coils"}
        assert items_found.issubset(possible | {"credits"})


class TestScan:
    async def test_scan_returns_sector_info(self, db):
        agent = await make_agent(db)
        agent["sector"] = "the_exchange"
        result = await scan(db, agent)
        assert result.success is True
        assert result.data["sector"] == "the_exchange"
        assert "availableMaterials" in result.data


class TestSabotage:
    async def test_sabotage_requires_scrap_alley(self, db):
        agent = await make_agent(db)
        target = await make_agent(db, "Target")
        result = await sabotage(db, agent, "Target", tick=0)
        assert result.success is False
        assert "Scrap Alley" in result.message

    async def test_sabotage_cant_target_self(self, db):
        agent = await make_agent(db)
        await db.execute("UPDATE agents SET sector = 'scrap_alley' WHERE id = ?", (agent["id"],))
        await db.commit()
        agent["sector"] = "scrap_alley"

        result = await sabotage(db, agent, agent["name"], tick=0)
        assert result.success is False

    async def test_sabotage_target_must_be_in_scrap_alley(self, db):
        agent = await make_agent(db)
        target = await make_agent(db, "Target")
        await db.execute("UPDATE agents SET sector = 'scrap_alley' WHERE id = ?", (agent["id"],))
        await db.commit()
        agent["sector"] = "scrap_alley"

        result = await sabotage(db, agent, "Target", tick=0)
        assert result.success is False
        assert "not in Scrap Alley" in result.message

    async def test_failed_sabotage_jails(self, db):
        """Run enough attempts that at least one fails → jail."""
        jailed = False
        for i in range(20):
            agent = await make_agent(db, f"Thief{i}", credits=500)
            target = await make_agent(db, f"Victim{i}", credits=500)
            await db.execute("UPDATE agents SET sector = 'scrap_alley' WHERE id IN (?, ?)", (agent["id"], target["id"]))
            await give_item(db, target["id"], "scrap_metal", 5)
            await db.commit()
            agent["sector"] = "scrap_alley"

            result = await sabotage(db, agent, f"Victim{i}", tick=10)
            await db.commit()
            if not result.success and "brig" in result.message:
                jailed = True
                assert result.data["jailedUntil"] > 10
                break

        assert jailed, "Expected at least one failed sabotage to result in jail"


class TestRumor:
    async def test_rumor_moves_price_up(self, db):
        agent = await make_agent(db)
        agent["sector"] = "the_exchange"
        old = (await get_item(db, "scrap_metal"))["current_price"]

        result = await spread_rumor(db, agent, "scrap_metal", "up")
        await db.commit()

        assert result.success is True
        new = (await get_item(db, "scrap_metal"))["current_price"]
        assert new > old
        rows = await db.execute_fetchall("SELECT credits FROM agents WHERE id = ?", (agent["id"],))
        assert rows[0]["credits"] < agent["credits"]
        assert "cost" in result.data

    async def test_rumor_moves_price_down(self, db):
        agent = await make_agent(db)
        agent["sector"] = "the_exchange"
        old = (await get_item(db, "scrap_metal"))["current_price"]

        result = await spread_rumor(db, agent, "scrap_metal", "down")
        await db.commit()

        assert result.success is True
        new = (await get_item(db, "scrap_metal"))["current_price"]
        assert new < old

    async def test_commons_doubles_effectiveness(self, db):
        agent = await make_agent(db)
        agent["sector"] = "the_commons"
        result = await spread_rumor(db, agent, "scrap_metal", "up")
        assert result.data["effectiveness"] == 2.0

    async def test_invalid_direction_rejected(self, db):
        agent = await make_agent(db)
        result = await spread_rumor(db, agent, "scrap_metal", "sideways")
        assert result.success is False

    async def test_rumor_requires_credits(self, db):
        agent = await make_agent(db, credits=1)
        result = await spread_rumor(db, agent, "scrap_metal", "up")
        assert result.success is False
        assert "costs" in result.message


class TestForge:
    async def test_forge_creates_counterfeits(self, db):
        agent = await make_agent(db, credits=500)
        await db.execute("UPDATE agents SET sector = 'scrap_alley' WHERE id = ?", (agent["id"],))
        await db.commit()
        agent["sector"] = "scrap_alley"

        result = await forge(db, agent, "power_cell", 3, tick=0)
        await db.commit()

        assert result.success is True
        from station.services.agents import get_inventory_qty
        assert await get_inventory_qty(db, agent["id"], "power_cell", counterfeit=True) == 3
        assert await get_inventory_qty(db, agent["id"], "power_cell", counterfeit=False) == 0

    async def test_forge_requires_scrap_alley(self, db):
        agent = await make_agent(db)
        result = await forge(db, agent, "power_cell", 1, tick=0)
        assert result.success is False
        assert "Scrap Alley" in result.message

    async def test_forge_costs_credits(self, db):
        agent = await make_agent(db, credits=10)
        await db.execute("UPDATE agents SET sector = 'scrap_alley' WHERE id = ?", (agent["id"],))
        await db.commit()
        agent["sector"] = "scrap_alley"

        result = await forge(db, agent, "power_cell", 5, tick=0)
        assert result.success is False  # 5 * 5 = 25 > 10
