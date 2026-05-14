import pytest
from tests.conftest import make_agent, give_item
from station.services.bounties import generate_bounties, get_bounties, complete_bounty


class TestBountyGeneration:
    async def test_generates_bounties(self, db):
        await generate_bounties(db, tick=1, count=3)
        await db.commit()

        result = await get_bounties(db, tick=1)
        assert result["success"] is True
        assert len(result["bounties"]) == 3

    async def test_doesnt_over_generate(self, db):
        await generate_bounties(db, tick=1, count=3)
        await db.commit()
        await generate_bounties(db, tick=2, count=3)
        await db.commit()

        result = await get_bounties(db, tick=2)
        # Should still be ~3, not 6
        assert len(result["bounties"]) <= 4

    async def test_expired_bounties_cleaned(self, db):
        await generate_bounties(db, tick=1, count=3)
        await db.commit()

        # Jump far ahead — all should expire
        result = await get_bounties(db, tick=500)
        assert len(result["bounties"]) == 0


class TestCompleteBounty:
    async def test_complete_bounty_rewards(self, db):
        agent = await make_agent(db, credits=500)
        await db.execute("UPDATE agents SET reputation = 10 WHERE id = ?", (agent["id"],))
        await db.commit()
        agent["reputation"] = 10

        await generate_bounties(db, tick=1, count=5)
        await db.commit()

        bounties = await get_bounties(db, tick=1)
        # Pick a solo bounty (not cooperative)
        solo = [b for b in bounties["bounties"] if not b["cooperative"]]
        assert solo, "No solo bounties generated"
        bounty = solo[0]

        # Give agent the required items
        await give_item(db, agent["id"], bounty["itemName"], bounty["quantity"])

        # Move to required sector if needed
        if bounty["sector"]:
            await db.execute("UPDATE agents SET sector = ? WHERE id = ?", (bounty["sector"], agent["id"]))
            await db.commit()
            agent["sector"] = bounty["sector"]

        result = await complete_bounty(db, agent, bounty["id"], tick=1)
        await db.commit()

        assert result.success is True
        assert "Bounty complete" in result.message
        assert float(result.data["earnedCredits"]) > 0

    async def test_complete_bounty_wrong_sector(self, db):
        agent = await make_agent(db, credits=500)
        await db.execute("UPDATE agents SET reputation = 10 WHERE id = ?", (agent["id"],))
        await db.commit()
        agent["reputation"] = 10

        await generate_bounties(db, tick=1, count=5)
        await db.commit()

        bounties = await get_bounties(db, tick=1)
        # Find a bounty with a sector requirement
        sector_bounty = None
        for b in bounties["bounties"]:
            if b["sector"]:
                sector_bounty = b
                break

        if sector_bounty is None:
            pytest.skip("No sector-specific bounty generated")

        await give_item(db, agent["id"], sector_bounty["itemName"], sector_bounty["quantity"])
        # Agent is in the_exchange by default, bounty requires different sector
        if agent["sector"] != sector_bounty["sector"]:
            result = await complete_bounty(db, agent, sector_bounty["id"], tick=1)
            assert result.success is False
            assert "must be in" in result.message.lower()

    async def test_complete_bounty_insufficient_items(self, db):
        agent = await make_agent(db, credits=500)
        await db.execute("UPDATE agents SET reputation = 10 WHERE id = ?", (agent["id"],))
        await db.commit()
        agent["reputation"] = 10

        await generate_bounties(db, tick=1, count=3)
        await db.commit()

        bounties = await get_bounties(db, tick=1)
        bounty = bounties["bounties"][0]

        # Don't give items — should fail
        if bounty["sector"]:
            await db.execute("UPDATE agents SET sector = ? WHERE id = ?", (bounty["sector"], agent["id"]))
            await db.commit()
            agent["sector"] = bounty["sector"]

        result = await complete_bounty(db, agent, bounty["id"], tick=1)
        assert result.success is False

    async def test_bounty_cant_be_completed_twice(self, db):
        agent = await make_agent(db, credits=500)
        await db.execute("UPDATE agents SET reputation = 10 WHERE id = ?", (agent["id"],))
        await db.commit()
        agent["reputation"] = 10

        await generate_bounties(db, tick=1, count=3)
        await db.commit()

        bounties = await get_bounties(db, tick=1)
        bounty = bounties["bounties"][0]

        await give_item(db, agent["id"], bounty["itemName"], bounty["quantity"] * 2)
        if bounty["sector"]:
            await db.execute("UPDATE agents SET sector = ? WHERE id = ?", (bounty["sector"], agent["id"]))
            await db.commit()
            agent["sector"] = bounty["sector"]

        result = await complete_bounty(db, agent, bounty["id"], tick=1)
        await db.commit()
        assert result.success is True

        result = await complete_bounty(db, agent, bounty["id"], tick=1)
        assert result.success is False
