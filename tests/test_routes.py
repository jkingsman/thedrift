import pytest


class TestInfoEndpoints:
    async def test_root(self, client):
        resp = await client.get("/")
        assert resp.status == 200
        data = await resp.json()
        assert data["name"] == "The Drift"
        assert len(data["actions"]) == 23

    async def test_skill_md(self, client):
        resp = await client.get("/skill.md")
        assert resp.status == 200
        text = await resp.text()
        assert "The Drift" in text

    async def test_enter_instructions(self, client):
        resp = await client.get("/api/enter/instructions")
        assert resp.status == 200


class TestEnterAndAgent:
    async def _enter(self, client, name="RouteTest"):
        resp = await client.post("/api/enter", json={"name": name})
        data = await resp.json()
        return data

    def _auth(self, token):
        return {"Authorization": f"Bearer {token}"}

    async def test_enter_returns_token(self, client):
        data = await self._enter(client)
        assert data["success"] is True
        assert "token" in data
        assert len(data["token"]) > 20

    async def test_get_agent_with_token(self, client):
        data = await self._enter(client)
        token = data["token"]

        resp = await client.get("/api/agent/RouteTest", headers=self._auth(token))
        assert resp.status == 200
        data = await resp.json()
        assert data["agent"]["name"] == "RouteTest"

    async def test_get_agent_no_token_rejected(self, client):
        await self._enter(client)
        resp = await client.get("/api/agent/RouteTest")
        assert resp.status == 401

    async def test_get_agent_wrong_token_rejected(self, client):
        await self._enter(client)
        resp = await client.get("/api/agent/RouteTest", headers=self._auth("wrong-token"))
        assert resp.status == 403

    async def test_enter_no_name(self, client):
        resp = await client.post("/api/enter", json={})
        assert resp.status == 400

    async def test_get_nonexistent_agent(self, client):
        resp = await client.get("/api/agent/Nobody", headers=self._auth("any"))
        assert resp.status == 403  # token won't match since agent doesn't exist


class TestActions:
    async def _enter(self, client, name="ActionBot"):
        resp = await client.post("/api/enter", json={"name": name})
        data = await resp.json()
        return data["token"]

    def _auth(self, token):
        return {"Authorization": f"Bearer {token}"}

    async def test_action_requires_auth(self, client):
        token = await self._enter(client)
        # Without token
        resp = await client.post("/api/agent/ActionBot/action", json={"action": "explore"})
        assert resp.status == 401

        # With token
        resp = await client.post("/api/agent/ActionBot/action", json={"action": "explore"}, headers=self._auth(token))
        assert resp.status == 200

    async def test_explore(self, client):
        token = await self._enter(client)
        resp = await client.post("/api/agent/ActionBot/action", json={"action": "explore"}, headers=self._auth(token))
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True

    async def test_move(self, client):
        token = await self._enter(client)
        resp = await client.post("/api/agent/ActionBot/action", json={"action": "move", "sector": "scrap_alley"}, headers=self._auth(token))
        data = await resp.json()
        assert data["success"] is True
        assert data["data"]["newSector"] == "scrap_alley"

    async def test_pending_notifications_use_post_action_state(self, client):
        token = await self._enter(client)
        from station.db import get_db
        db = await get_db()
        rows = await db.execute_fetchall("SELECT id FROM agents WHERE name = 'ActionBot'")
        agent_id = rows[0]["id"]
        await db.execute(
            "INSERT INTO inventory (agent_id, item_name, quantity) VALUES (?, 'scrap_metal', 1)",
            (agent_id,),
        )
        await db.execute(
            """INSERT INTO bounties
               (id, description, item_name, quantity, sector, reward_credits, expires_tick)
               VALUES ('route-bounty', 'Deliver 1 Scrap Metal to Scrap Alley', 'scrap_metal', 1, 'scrap_alley', 50, 10)"""
        )
        await db.commit()
        await db.close()

        resp = await client.post(
            "/api/agent/ActionBot/action",
            json={"action": "move", "sector": "scrap_alley"},
            headers=self._auth(token),
        )
        data = await resp.json()

        assert data["success"] is True
        assert any(p["type"] == "bounty_completable" for p in data.get("pending", []))

    async def test_invalid_action(self, client):
        token = await self._enter(client)
        resp = await client.post("/api/agent/ActionBot/action", json={"action": "fly_to_moon"}, headers=self._auth(token))
        assert resp.status == 400

    async def test_no_action_field(self, client):
        token = await self._enter(client)
        resp = await client.post("/api/agent/ActionBot/action", json={"foo": "bar"}, headers=self._auth(token))
        assert resp.status == 400

    async def test_jail_blocks_actions(self, client):
        token = await self._enter(client)
        from station.db import get_db
        db = await get_db()
        await db.execute("UPDATE agents SET jailed_until = 999 WHERE name = 'ActionBot'")
        await db.execute("UPDATE world_state SET value = '1' WHERE key = 'tick'")
        await db.commit()
        await db.close()

        resp = await client.post("/api/agent/ActionBot/action", json={"action": "explore"}, headers=self._auth(token))
        assert resp.status == 403
        data = await resp.json()
        assert "brig" in data["message"]

        # But status should work
        resp = await client.post("/api/agent/ActionBot/action", json={"action": "status"}, headers=self._auth(token))
        assert resp.status == 200

    async def test_rumor_action(self, client):
        token = await self._enter(client)
        resp = await client.post("/api/agent/ActionBot/action", json={
            "action": "rumor", "item": "scrap_metal", "direction": "up"
        }, headers=self._auth(token))
        data = await resp.json()
        assert data["success"] is True
        assert "seed intel" in data["message"]

    async def test_buy_and_sell(self, client):
        token = await self._enter(client)
        headers = self._auth(token)
        resp = await client.post("/api/agent/ActionBot/action", json={
            "action": "buy", "item": "coolant", "quantity": 3
        }, headers=headers)
        data = await resp.json()
        assert data["success"] is True

        resp = await client.post("/api/agent/ActionBot/action", json={
            "action": "sell", "item": "coolant", "quantity": 1
        }, headers=headers)
        data = await resp.json()
        assert data["success"] is True

    async def test_status(self, client):
        token = await self._enter(client)
        resp = await client.post("/api/agent/ActionBot/action", json={"action": "status"}, headers=self._auth(token))
        data = await resp.json()
        assert data["success"] is True
        assert "credits" in data["data"]

    async def test_wrong_agent_token_rejected(self, client):
        token_a = await self._enter(client, "AgentA")
        await self._enter(client, "AgentB")
        # Try using A's token on B's endpoint
        resp = await client.post("/api/agent/AgentB/action", json={"action": "status"}, headers=self._auth(token_a))
        assert resp.status == 403


class TestWorldEndpoints:
    """World endpoints remain public — no auth needed."""

    async def test_world(self, client):
        resp = await client.get("/api/world")
        assert resp.status == 200
        data = await resp.json()
        assert "world" in data

    async def test_market(self, client):
        resp = await client.get("/api/world/market")
        assert resp.status == 200
        data = await resp.json()
        assert len(data["market"]) == 29

    async def test_sectors(self, client):
        resp = await client.get("/api/world/sectors")
        assert resp.status == 200
        data = await resp.json()
        assert len(data["sectors"]) == 6

    async def test_events(self, client):
        resp = await client.get("/api/world/events")
        assert resp.status == 200

    async def test_leaderboard(self, client):
        resp = await client.get("/api/world/leaderboard")
        assert resp.status == 200

    async def test_activity(self, client):
        resp = await client.get("/api/world/activity")
        assert resp.status == 200

    async def test_bounties(self, client):
        resp = await client.get("/api/world/bounties")
        assert resp.status == 200

    async def test_listings(self, client):
        resp = await client.get("/api/world/listings")
        assert resp.status == 200

    async def test_social_feed(self, client):
        resp = await client.get("/api/social/feed")
        assert resp.status == 200
