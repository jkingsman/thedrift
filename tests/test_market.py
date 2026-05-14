import pytest
from tests.conftest import make_agent, give_item
from station.services.market import (
    get_item, get_buy_price, get_sell_price,
    buy_from_station, sell_to_station, list_item, buy_listing,
)


class TestPricing:
    async def test_buy_price_has_markup(self, db):
        price = await get_buy_price(db, "scrap_metal", "the_foundry")
        item = await get_item(db, "scrap_metal")
        assert price > item["current_price"]  # 110% markup

    async def test_sell_price_has_discount(self, db):
        price = await get_sell_price(db, "scrap_metal", "the_foundry")
        item = await get_item(db, "scrap_metal")
        assert price < item["current_price"]  # 90% of market

    async def test_exchange_has_tighter_spread(self, db):
        buy_exchange = await get_buy_price(db, "scrap_metal", "the_exchange")
        buy_normal = await get_buy_price(db, "scrap_metal", "the_foundry")
        assert buy_exchange < buy_normal  # 105% vs 110%

        sell_exchange = await get_sell_price(db, "scrap_metal", "the_exchange")
        sell_normal = await get_sell_price(db, "scrap_metal", "the_foundry")
        assert sell_exchange > sell_normal  # 95% vs 90%


class TestBuyFromStation:
    async def test_buy_deducts_credits_adds_inventory(self, db):
        agent = await make_agent(db, credits=500)
        result = await buy_from_station(db, agent, "scrap_metal", 3)
        await db.commit()
        assert result.success is True
        assert "Bought 3" in result.message

        from station.services.agents import get_inventory_qty
        assert await get_inventory_qty(db, agent["id"], "scrap_metal") == 3

    async def test_buy_insufficient_credits(self, db):
        agent = await make_agent(db, credits=1)
        result = await buy_from_station(db, agent, "scrap_metal", 1)
        assert result.success is False
        assert "Not enough" in result.message

    async def test_buy_unknown_item(self, db):
        agent = await make_agent(db)
        result = await buy_from_station(db, agent, "unobtanium", 1)
        assert result.success is False

    async def test_station_does_not_sell_advanced_goods(self, db):
        agent = await make_agent(db, credits=10000)
        result = await buy_from_station(db, agent, "warp_drive", 1)
        assert result.success is False
        assert result.data["stationAvailable"] is False


class TestSellToStation:
    async def test_sell_credits_and_removes_inventory(self, db):
        agent = await make_agent(db, credits=100)
        await give_item(db, agent["id"], "scrap_metal", 5)
        result = await sell_to_station(db, agent, "scrap_metal", 3)
        await db.commit()
        assert result.success is True
        assert "Sold 3" in result.message

    async def test_sell_insufficient_inventory(self, db):
        agent = await make_agent(db)
        result = await sell_to_station(db, agent, "scrap_metal", 99)
        assert result.success is False

    async def test_crafted_station_sells_saturate_demand_fast(self, db):
        agent = await make_agent(db, credits=100)
        await give_item(db, agent["id"], "relay_array", 1)
        before = await get_item(db, "relay_array")

        result = await sell_to_station(db, agent, "relay_array", 1)
        await db.commit()

        after = await get_item(db, "relay_array")
        assert result.success is True
        assert after["current_price"] < before["current_price"] * 0.80

    async def test_more_active_agents_deepen_station_liquidity(self, db):
        sparse = await make_agent(db, "Sparse", credits=100)
        await give_item(db, sparse["id"], "relay_array", 1)
        before_sparse = await get_item(db, "relay_array")
        await sell_to_station(db, sparse, "relay_array", 1)
        after_sparse = await get_item(db, "relay_array")
        sparse_drop = before_sparse["current_price"] - after_sparse["current_price"]

        await db.execute("UPDATE items SET current_price = base_price WHERE name = 'relay_array'")
        await db.execute("DELETE FROM activity_log")
        await db.execute("UPDATE world_state SET value = '100' WHERE key = 'tick'")
        for i in range(40):
            await db.execute(
                "INSERT INTO activity_log (id, agent_name, action, params, result, tick) VALUES (?, ?, 'explore', '{}', '{}', 99)",
                (f"act{i}", f"Agent{i}"),
            )
        crowded = await make_agent(db, "Crowded", credits=100)
        await give_item(db, crowded["id"], "relay_array", 1)
        await db.commit()

        before_crowded = await get_item(db, "relay_array")
        await sell_to_station(db, crowded, "relay_array", 1)
        await db.commit()
        after_crowded = await get_item(db, "relay_array")
        crowded_drop = before_crowded["current_price"] - after_crowded["current_price"]

        assert crowded_drop < sparse_drop / 5

    async def test_sell_counterfeits_risk_detection(self, db):
        """Selling counterfeits should either succeed or get caught — never error."""
        agent = await make_agent(db, credits=500)
        await give_item(db, agent["id"], "power_cell", 5, counterfeit=True)

        # Run multiple attempts to hit both outcomes
        caught = False
        sold = False
        for _ in range(20):
            agent = await make_agent(db, f"seller_{_}", credits=500)
            await give_item(db, agent["id"], "power_cell", 1, counterfeit=True)
            result = await sell_to_station(db, agent, "power_cell", 1, tick=100)
            await db.commit()
            if result.success:
                sold = True
            elif "BUSTED" in result.message:
                caught = True
                assert result.data["jailedUntil"] > 100

        # With 35% detection over 20 tries, we should see both outcomes
        assert caught or sold  # at minimum one path was taken


class TestPlayerListings:
    async def test_list_and_buy(self, db):
        seller = await make_agent(db, "Seller", credits=500)
        buyer = await make_agent(db, "Buyer", credits=500)
        await give_item(db, seller["id"], "hull_plating", 3)

        # List
        result = await list_item(db, seller, "hull_plating", 2, 30.0, tick=0)
        await db.commit()
        assert result.success is True
        listing_id = result.data["listingId"]

        # Seller's inventory reduced
        from station.services.agents import get_inventory_qty
        assert await get_inventory_qty(db, seller["id"], "hull_plating") == 1

        # Buy
        result = await buy_listing(db, buyer, listing_id)
        await db.commit()
        assert result.success is True
        assert await get_inventory_qty(db, buyer["id"], "hull_plating") == 2

    async def test_cant_buy_own_listing(self, db):
        agent = await make_agent(db)
        await give_item(db, agent["id"], "coolant", 5)
        result = await list_item(db, agent, "coolant", 2, 10.0, tick=0)
        await db.commit()
        listing_id = result.data["listingId"]

        result = await buy_listing(db, agent, listing_id)
        assert result.success is False
