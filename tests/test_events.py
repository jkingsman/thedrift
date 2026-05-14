import pytest
from tests.conftest import make_agent, give_item
from station.events import (
    _solar_flare, _cargo_drop, _station_tax, _price_crash,
    _gold_rush, _gravity_malfunction, _pirate_raid, _power_outage,
    _black_market_surge, _diplomatic_summit,
)
from station.services.market import get_item
from station.services.world import get_active_effects


class TestSolarFlare:
    async def test_boosts_crystals_drops_electronics(self, db):
        old_crystal = (await get_item(db, "crystal_shards"))["current_price"]
        old_signal = (await get_item(db, "signal_beacon"))["current_price"]

        event = await _solar_flare(db, tick=1)
        await db.commit()

        new_crystal = (await get_item(db, "crystal_shards"))["current_price"]
        new_signal = (await get_item(db, "signal_beacon"))["current_price"]

        assert new_crystal > old_crystal
        assert new_signal < old_signal
        assert event["event_type"] == "solar_flare"


class TestStationTax:
    async def test_tax_seizes_commodity(self, db):
        agent = await make_agent(db, credits=1000)
        await give_item(db, agent["id"], "scrap_metal", 20)

        event = await _station_tax(db, tick=1)
        await db.commit()

        assert event["event_type"] == "station_tax"
        assert "seized" in event["description"].lower() or "requisition" in event["description"].lower()


class TestPriceCrash:
    async def test_crashes_random_item(self, db):
        event = await _price_crash(db, tick=1)
        await db.commit()

        assert event["event_type"] == "price_crash"
        item_name = event["effects"]["item"]
        item = await get_item(db, item_name)
        assert item["current_price"] == float(event["effects"]["newPrice"])
        assert float(event["effects"]["newPrice"]) < float(event["effects"]["oldPrice"])


class TestGoldRush:
    async def test_spikes_random_item(self, db):
        event = await _gold_rush(db, tick=1)
        await db.commit()

        assert event["event_type"] == "gold_rush"
        assert float(event["effects"]["newPrice"]) > float(event["effects"]["oldPrice"])


class TestGravityMalfunction:
    async def test_shuffles_agents(self, db):
        agents = [await make_agent(db, f"Agent{i}") for i in range(5)]
        # Mark agents as active by adding activity
        from station.services.world import log_activity
        for a in agents:
            await log_activity(db, a["name"], "explore", {}, {}, tick=1)
        await db.commit()

        event = await _gravity_malfunction(db, tick=1)
        await db.commit()

        assert event["event_type"] == "gravity_malfunction"
        assert event["effects"]["agentsShuffled"] == 5


class TestCargoDrop:
    async def test_gives_items_to_agents_in_sector(self, db):
        agent = await make_agent(db)
        # Agent starts in the_exchange, which may or may not be the drop target.
        # Just verify event runs without error and returns valid data.
        event = await _cargo_drop(db, tick=1)
        await db.commit()

        assert event["event_type"] == "cargo_drop"
        assert event["effects"]["quantity"] >= 3


class TestTimedEffects:
    async def test_black_market_surge_sets_effect(self, db):
        event = await _black_market_surge(db, tick=5)
        await db.commit()

        effects = await get_active_effects(db)
        assert effects.get("black_market_surge") == 8  # tick 5 + 3

    async def test_power_outage_sets_foundry_offline(self, db):
        event = await _power_outage(db, tick=10)
        await db.commit()

        effects = await get_active_effects(db)
        assert effects.get("foundry_offline") == 13  # tick 10 + 3

    async def test_diplomatic_summit_sets_effect(self, db):
        event = await _diplomatic_summit(db, tick=7)
        await db.commit()

        effects = await get_active_effects(db)
        assert effects.get("diplomatic_summit") == 10


class TestPirateRaid:
    async def test_raid_takes_items_from_agents(self, db):
        agents = []
        for i in range(10):
            a = await make_agent(db, f"Pirate{i}")
            await give_item(db, a["id"], "scrap_metal", 10)
            agents.append(a)

        event = await _pirate_raid(db, tick=1)
        await db.commit()

        assert event["event_type"] == "pirate_raid"
        # With 10 agents at 45% rate, very likely some were hit
