from tests.conftest import make_agent
from station.services.world import log_activity
from station.tick import apply_progressive_upkeep


async def _log_actions(db, agent_name: str, count: int):
    for _ in range(count):
        await log_activity(db, agent_name, "explore", {}, {"success": True}, tick=1)


class TestProgressiveUpkeep:
    async def test_dormant_agent_is_not_taxed(self, db):
        agent = await make_agent(db, credits=5000)

        taxed, total = await apply_progressive_upkeep(db, tick=30)
        await db.commit()

        rows = await db.execute_fetchall("SELECT credits FROM agents WHERE id = ?", (agent["id"],))
        assert taxed == 0
        assert total == 0
        assert rows[0]["credits"] == 5000

    async def test_upkeep_scales_with_recent_actions(self, db):
        active = await make_agent(db, "Active", credits=5000)
        light = await make_agent(db, "Light", credits=5000)
        await _log_actions(db, active["name"], 80)
        await _log_actions(db, light["name"], 4)
        await db.commit()

        taxed, total = await apply_progressive_upkeep(db, tick=30)
        await db.commit()

        rows = await db.execute_fetchall(
            "SELECT name, credits FROM agents WHERE name IN ('Active', 'Light') ORDER BY name"
        )
        balances = {r["name"]: r["credits"] for r in rows}
        active_tax = 5000 - balances["Active"]
        light_tax = 5000 - balances["Light"]

        assert taxed == 2
        assert total > 0
        assert active_tax > light_tax > 0
