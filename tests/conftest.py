import os
import uuid
import pytest
import pytest_asyncio

import station.db as db_mod
from station.db import get_db, init_db


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path):
    """Point every test at a fresh SQLite database."""
    test_db = str(tmp_path / f"test_{uuid.uuid4().hex[:8]}.db")
    db_mod.DB_PATH = test_db
    yield
    if os.path.exists(test_db):
        os.unlink(test_db)


@pytest_asyncio.fixture
async def db(_isolate_db):
    """Initialized DB connection for service-level tests."""
    await init_db()
    conn = await get_db()
    yield conn
    await conn.close()


@pytest_asyncio.fixture
async def client(_isolate_db, aiohttp_client):
    """Test client that disables the tick loop."""
    from aiohttp import web
    from station.db import init_db
    from station.routes import setup_routes

    app = web.Application()
    setup_routes(app)

    async def _init_db_only(app):
        await init_db()

    app.on_startup.append(_init_db_only)
    return await aiohttp_client(app)


# ── helpers ──────────────────────────────────────────────────────────

async def make_agent(db, name="TestAgent", credits=500.0):
    """Insert an agent directly and return its dict."""
    from station.services.agents import _hash_token
    agent_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO agents (id, name, token_hash, credits) VALUES (?, ?, ?, ?)",
        (agent_id, name, _hash_token("test-token"), credits),
    )
    await db.commit()
    rows = await db.execute_fetchall("SELECT * FROM agents WHERE id = ?", (agent_id,))
    return dict(rows[0])


async def give_item(db, agent_id, item, qty, counterfeit=False):
    """Add items to an agent's inventory."""
    from station.services.agents import add_inventory
    await add_inventory(db, agent_id, item, qty, counterfeit=counterfeit)
    await db.commit()
