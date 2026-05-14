import pytest
from tests.conftest import make_agent, give_item
from station.services.contracts import (
    propose_contract, join_contract, fulfill_contract, betray_contract,
    view_contracts, auto_resolve_expired, station_fill_stale_contracts,
)
from station.services.agents import get_inventory_qty


async def _setup_two_agents(db):
    a = await make_agent(db, "Alice", credits=1000)
    b = await make_agent(db, "Bob", credits=1000)
    return a, b


class TestProposeContract:
    async def test_propose_escrows_items(self, db):
        a, _ = await _setup_two_agents(db)
        await give_item(db, a["id"], "hull_plating", 2)

        result = await propose_contract(db, a, "habitat_module", {"hull_plating": 1}, tick=0)
        await db.commit()

        assert result.success is True
        assert await get_inventory_qty(db, a["id"], "hull_plating") == 1  # 1 escrowed

    async def test_propose_solo_recipe_rejected(self, db):
        a, _ = await _setup_two_agents(db)
        await give_item(db, a["id"], "scrap_metal", 5)

        result = await propose_contract(db, a, "hull_plating", {"scrap_metal": 1}, tick=0)
        assert result.success is False
        assert "solo" in result.message.lower()

    async def test_propose_insufficient_items(self, db):
        a, _ = await _setup_two_agents(db)
        result = await propose_contract(db, a, "habitat_module", {"hull_plating": 1}, tick=0)
        assert result.success is False

    async def test_propose_untrustworthy_blocked(self, db):
        a, _ = await _setup_two_agents(db)
        await give_item(db, a["id"], "hull_plating", 1)
        await db.execute("UPDATE agents SET untrustworthy_until = 100 WHERE id = ?", (a["id"],))
        await db.commit()
        a["untrustworthy_until"] = 100

        result = await propose_contract(db, a, "habitat_module", {"hull_plating": 1}, tick=50)
        assert result.success is False
        assert "untrustworthy" in result.message.lower()


class TestJoinContract:
    async def test_join_escrows_and_starts_deciding(self, db):
        a, b = await _setup_two_agents(db)
        await give_item(db, a["id"], "hull_plating", 1)
        await give_item(db, b["id"], "med_patch", 1)
        await give_item(db, b["id"], "thermal_shield", 1)

        prop = await propose_contract(db, a, "habitat_module", {"hull_plating": 1}, tick=0)
        await db.commit()
        cid = prop.data["contractId"]

        join = await join_contract(db, b, cid, tick=0)
        await db.commit()

        assert join.success is True
        assert "Decision phase" in join.message
        assert await get_inventory_qty(db, b["id"], "med_patch") == 0
        assert await get_inventory_qty(db, b["id"], "thermal_shield") == 0

    async def test_cant_join_own_contract(self, db):
        a, _ = await _setup_two_agents(db)
        await give_item(db, a["id"], "hull_plating", 1)

        prop = await propose_contract(db, a, "habitat_module", {"hull_plating": 1}, tick=0)
        await db.commit()
        cid = prop.data["contractId"]

        result = await join_contract(db, a, cid, tick=0)
        assert result.success is False


class TestFulfillBetray:
    async def _setup_deciding_contract(self, db):
        a, b = await _setup_two_agents(db)
        await give_item(db, a["id"], "hull_plating", 1)
        await give_item(db, b["id"], "med_patch", 1)
        await give_item(db, b["id"], "thermal_shield", 1)

        prop = await propose_contract(db, a, "habitat_module", {"hull_plating": 1}, tick=0)
        await db.commit()
        cid = prop.data["contractId"]
        await join_contract(db, b, cid, tick=0)
        await db.commit()
        return a, b, cid

    async def test_mutual_fulfill_crafts_item(self, db):
        a, b, cid = await self._setup_deciding_contract(db)

        r1 = await fulfill_contract(db, a, cid)
        await db.commit()
        assert r1.success is True
        assert r1.data.get("waiting") is True

        r2 = await fulfill_contract(db, b, cid)
        await db.commit()
        assert r2.success is True
        assert r2.data["outcome"] == "mutual_cooperation"

        # Both get habitat_module
        assert await get_inventory_qty(db, a["id"], "habitat_module") == 1
        assert await get_inventory_qty(db, b["id"], "habitat_module") == 1

    async def test_diplomatic_summit_boosts_contract_reputation(self, db):
        a, b, cid = await self._setup_deciding_contract(db)
        await db.execute("UPDATE world_state SET value = '7' WHERE key = 'tick'")
        await db.execute("UPDATE world_state SET value = ? WHERE key = 'active_effects'", ('{"diplomatic_summit": 10}',))
        await db.commit()

        await fulfill_contract(db, a, cid)
        r = await fulfill_contract(db, b, cid)
        await db.commit()

        assert r.success is True
        assert r.data["reputationGained"] == 5
        assert "diplomatic_summit" in r.data["activeBonuses"]

        rows = await db.execute_fetchall(
            "SELECT name, reputation FROM agents WHERE id IN (?, ?) ORDER BY name",
            (a["id"], b["id"]),
        )
        assert [row["reputation"] for row in rows] == [5, 5]

    async def test_one_betrays(self, db):
        a, b, cid = await self._setup_deciding_contract(db)

        await fulfill_contract(db, a, cid)
        await db.commit()
        r = await betray_contract(db, b, cid)
        await db.commit()

        assert r.success is True
        assert r.data["outcome"] == "you_betrayed"

        # Betrayer gets all materials, victim gets nothing
        assert await get_inventory_qty(db, b["id"], "hull_plating") == 1
        assert await get_inventory_qty(db, b["id"], "med_patch") == 1
        assert await get_inventory_qty(db, b["id"], "thermal_shield") == 1
        assert await get_inventory_qty(db, a["id"], "hull_plating") == 0

    async def test_mutual_betray(self, db):
        a, b, cid = await self._setup_deciding_contract(db)

        await betray_contract(db, a, cid)
        await db.commit()
        r = await betray_contract(db, b, cid)
        await db.commit()

        assert r.success is True
        assert r.data["outcome"] == "mutual_betrayal"

    async def test_cant_decide_twice(self, db):
        a, b, cid = await self._setup_deciding_contract(db)

        await fulfill_contract(db, a, cid)
        await db.commit()
        r = await fulfill_contract(db, a, cid)
        assert r.success is False
        assert "already chose" in r.message.lower()


class TestAutoResolve:
    async def test_expired_contracts_auto_fulfill(self, db):
        a, b = await _setup_two_agents(db)
        await give_item(db, a["id"], "hull_plating", 1)
        await give_item(db, b["id"], "med_patch", 1)
        await give_item(db, b["id"], "thermal_shield", 1)

        prop = await propose_contract(db, a, "habitat_module", {"hull_plating": 1}, tick=0)
        await db.commit()
        cid = prop.data["contractId"]
        await join_contract(db, b, cid, tick=0)
        await db.commit()

        # Neither decides — auto-resolve past the deadline (DECISION_WINDOW_TICKS=120)
        await auto_resolve_expired(db, tick=200)
        await db.commit()

        # Both should have habitat_module (auto-fulfill)
        assert await get_inventory_qty(db, a["id"], "habitat_module") == 1
        assert await get_inventory_qty(db, b["id"], "habitat_module") == 1

    async def test_open_contract_expires_by_created_tick(self, db):
        a, _ = await _setup_two_agents(db)
        await give_item(db, a["id"], "hull_plating", 1)

        prop = await propose_contract(db, a, "habitat_module", {"hull_plating": 1}, tick=5)
        await db.commit()
        cid = prop.data["contractId"]

        await auto_resolve_expired(db, tick=200)  # well past the 144-tick expiry
        await db.commit()

        rows = await db.execute_fetchall("SELECT status FROM contracts WHERE id = ?", (cid,))
        assert rows[0]["status"] == "expired"


class TestStationFill:
    async def test_station_fill_uses_contract_created_tick(self, db):
        a, _ = await _setup_two_agents(db)
        await give_item(db, a["id"], "hull_plating", 1)

        prop = await propose_contract(db, a, "habitat_module", {"hull_plating": 1}, tick=20)
        await db.commit()
        cid = prop.data["contractId"]

        await station_fill_stale_contracts(db, tick=55)  # not yet (STATION_FILL_AFTER_TICKS=36, created at tick=20)
        await db.commit()
        rows = await db.execute_fetchall("SELECT status FROM contracts WHERE id = ?", (cid,))
        assert rows[0]["status"] == "open"

        await station_fill_stale_contracts(db, tick=57)  # past threshold
        await db.commit()
        rows = await db.execute_fetchall("SELECT status, joiner_id FROM contracts WHERE id = ?", (cid,))
        assert rows[0]["status"] == "completed"
        assert rows[0]["joiner_id"] == "STATION"
        assert await get_inventory_qty(db, a["id"], "habitat_module") == 1
