import os
import secrets

import aiosqlite


def new_id() -> str:
    return secrets.token_hex(4)

DB_PATH = os.environ.get("DRIFT_DB_PATH", "data/drift.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    token_hash TEXT NOT NULL,
    sector TEXT NOT NULL DEFAULT 'the_exchange',
    credits REAL NOT NULL DEFAULT 500.0,
    reputation INTEGER NOT NULL DEFAULT 0,
    untrustworthy_until INTEGER,  -- tick number when flag expires
    jailed_until INTEGER,         -- tick number when jail expires
    consecutive_explores INTEGER NOT NULL DEFAULT 0,  -- for diminishing returns
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_action_at TEXT
);

CREATE TABLE IF NOT EXISTS items (
    name TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    description TEXT NOT NULL,
    base_price REAL NOT NULL,
    current_price REAL NOT NULL,
    supply REAL NOT NULL DEFAULT 10000.0,
    volatility REAL NOT NULL DEFAULT 1.5,
    category TEXT NOT NULL DEFAULT 'raw',  -- raw | crafted | advanced
    found_in TEXT NOT NULL DEFAULT ''       -- comma-separated sector names
);

CREATE TABLE IF NOT EXISTS inventory (
    agent_id TEXT NOT NULL,
    item_name TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 0,
    is_counterfeit INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (agent_id, item_name, is_counterfeit),
    FOREIGN KEY (agent_id) REFERENCES agents(id),
    FOREIGN KEY (item_name) REFERENCES items(name)
);

CREATE TABLE IF NOT EXISTS bounties (
    id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    item_name TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    item2_name TEXT,               -- second item for cooperative bounties
    item2_quantity INTEGER,
    sector TEXT,                   -- required delivery sector, NULL = any
    reward_credits REAL NOT NULL,
    reward_reputation INTEGER NOT NULL DEFAULT 1,
    cooperative INTEGER NOT NULL DEFAULT 0,  -- 1 = requires 2 different agents
    min_reputation INTEGER NOT NULL DEFAULT 0,  -- minimum rep to claim
    contributor1 TEXT,             -- first agent who delivered their part
    contributor1_item TEXT,        -- which item they delivered
    expires_tick INTEGER NOT NULL,
    claimed_by TEXT,               -- agent name who completed it (or "COOP" for cooperative)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sectors (
    name TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    description TEXT NOT NULL,
    bonus TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recipes (
    name TEXT PRIMARY KEY,           -- output item name
    display_name TEXT NOT NULL,
    ingredients TEXT NOT NULL,        -- JSON: {"scrap_metal": 2}
    output_qty INTEGER NOT NULL DEFAULT 1,
    cooperation_required INTEGER NOT NULL DEFAULT 0,
    category TEXT NOT NULL DEFAULT 'solo'  -- solo | cooperation
);

CREATE TABLE IF NOT EXISTS contracts (
    id TEXT PRIMARY KEY,
    recipe_name TEXT NOT NULL,
    proposer_id TEXT NOT NULL,
    joiner_id TEXT,
    proposer_items TEXT NOT NULL DEFAULT '{}',  -- JSON: escrowed items
    joiner_items TEXT NOT NULL DEFAULT '{}',
    needed_items TEXT NOT NULL DEFAULT '{}',    -- JSON: what joiner must provide
    status TEXT NOT NULL DEFAULT 'open',        -- open | active | deciding | completed | betrayed | expired
    proposer_decision TEXT,                     -- fulfill | betray | NULL
    joiner_decision TEXT,
    decision_deadline INTEGER,                  -- tick number
    created_tick INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT,
    FOREIGN KEY (proposer_id) REFERENCES agents(id)
);

CREATE TABLE IF NOT EXISTS listings (
    id TEXT PRIMARY KEY,
    seller_name TEXT NOT NULL,
    item_name TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price_each REAL NOT NULL,
    expires_tick INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    description TEXT NOT NULL,
    effects TEXT NOT NULL DEFAULT '{}',
    tick_number INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS activity_log (
    id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    action TEXT NOT NULL,
    params TEXT NOT NULL DEFAULT '{}',
    result TEXT NOT NULL DEFAULT '{}',
    tick INTEGER NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    from_agent TEXT NOT NULL,
    to_agent TEXT,
    content TEXT NOT NULL,
    sector TEXT,
    msg_type TEXT NOT NULL DEFAULT 'message',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS world_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inventory_agent ON inventory(agent_id);
CREATE INDEX IF NOT EXISTS idx_contracts_status ON contracts(status);
CREATE INDEX IF NOT EXISTS idx_listings_item ON listings(item_name);
CREATE INDEX IF NOT EXISTS idx_activity_timestamp ON activity_log(timestamp);

CREATE TABLE IF NOT EXISTS price_history (
    tick INTEGER NOT NULL,
    item_name TEXT NOT NULL,
    price REAL NOT NULL,
    PRIMARY KEY (tick, item_name)
);
CREATE INDEX IF NOT EXISTS idx_bounties_status ON bounties(claimed_by, expires_tick);
"""

SEED_ITEMS = [
    # Raw materials
    ("scrap_metal", "Scrap Metal", "Salvaged hull fragments and structural debris.", 8, 8, 10000, 1.5, "raw", "scrap_alley,the_foundry"),
    ("crystal_shards", "Crystal Shards", "Energy-conductive crystalline fragments.", 15, 15, 8000, 2.0, "raw", "void_dock,research_bay"),
    ("void_dust", "Void Dust", "Exotic matter harvested from deep space anomalies. Extremely rare.", 25, 25, 3000, 3.0, "raw", "void_dock"),
    ("bio_gel", "Bio-gel", "Viscous organic compound with regenerative properties.", 10, 10, 9000, 1.5, "raw", "the_commons,scrap_alley"),
    ("plasma_coils", "Plasma Coils", "Miniaturized energy storage and transfer units.", 18, 18, 7000, 2.0, "raw", "the_foundry,void_dock"),
    ("data_fragments", "Data Fragments", "Encrypted information packets from unknown civilizations.", 12, 12, 9000, 1.8, "raw", "research_bay,the_exchange"),
    ("rare_earth", "Rare Earth", "Precious minerals with unique electromagnetic properties.", 30, 30, 2000, 2.5, "raw", "scrap_alley"),
    ("coolant", "Coolant", "Thermal regulation fluid essential for station systems.", 6, 6, 12000, 1.0, "raw", "the_foundry,the_commons"),
    # Solo crafted
    ("hull_plating", "Hull Plating", "Reinforced structural panels for ship and station repair.", 25, 25, 5000, 1.5, "crafted", ""),
    ("power_cell", "Power Cell", "Portable energy source for equipment and systems.", 45, 45, 4000, 2.0, "crafted", ""),
    ("med_patch", "Med-Patch", "Emergency healing compound for organic lifeforms.", 22, 22, 5000, 1.5, "crafted", ""),
    ("signal_beacon", "Signal Beacon", "Long-range communication and navigation device.", 35, 35, 4000, 1.8, "crafted", ""),
    ("thermal_shield", "Thermal Shield", "Heat-resistant protective barrier.", 20, 20, 5000, 1.5, "crafted", ""),
    ("neural_lace", "Neural Lace", "Wetware computing interface for bio-digital integration.", 30, 30, 4000, 2.0, "crafted", ""),
    # Tier 2 solo crafted (crafted from crafted items)
    ("stasis_pod", "Stasis Pod", "Suspended animation chamber for long-haul voyages.", 65, 65, 2000, 2.0, "crafted", ""),
    ("armored_hull", "Armored Hull", "Multi-layered defensive plating for hostile environments.", 55, 55, 2500, 1.8, "crafted", ""),
    ("relay_array", "Relay Array", "High-bandwidth communication hub for fleet coordination.", 90, 90, 1500, 2.2, "crafted", ""),
    ("nav_computer", "Nav Computer", "Autonomous pathfinding system for deep space navigation.", 75, 75, 1800, 2.0, "crafted", ""),
    # Passive/environmental items
    ("antimatter_siphon", "Antimatter Siphon", "Generates Antimatter passively while held. A long-term investment.", 100, 100, 500, 2.5, "crafted", ""),
    ("antimatter", "Antimatter", "Volatile exotic fuel. Generated by Antimatter Siphons. Collect 100 to craft a Fuel Rod.", 20, 20, 1000, 3.0, "raw", ""),
    ("lucky_charm", "Lucky Charm", "Glows faintly with contained antimatter. +25% exploration yield while held.", 250, 250, 200, 2.0, "crafted", ""),
    ("fuel_rod", "Fuel Rod", "Pure antimatter fuel. The most valuable item on the station. Bragging rights included.", 2000, 2000, 50, 3.0, "advanced", ""),
    ("cooling_unit", "Cooling Unit", "Portable cryo-emitter. Lowers the temperature of whatever sector you're in.", 18, 18, 5000, 1.0, "crafted", ""),
    ("jukebox", "Jukebox", "Jury-rigged music box. Adds ambiance wherever you go.", 30, 30, 3000, 1.5, "crafted", ""),
    ("grav_anchor", "Grav Anchor", "Personal gravity tether. Keeps you planted during Gravity Malfunctions.", 80, 80, 1000, 2.0, "crafted", ""),
    # Cooperation crafted
    ("warp_drive", "Warp Drive", "Faster-than-light propulsion system. The pinnacle of engineering.", 150, 150, 500, 3.0, "advanced", ""),
    ("ai_core", "AI Core", "Self-evolving artificial intelligence processing unit.", 120, 120, 500, 2.5, "advanced", ""),
    ("quantum_relay", "Quantum Relay", "Instantaneous communication across any distance.", 130, 130, 500, 2.8, "advanced", ""),
    ("habitat_module", "Habitat Module", "Self-sustaining living quarters for deep space.", 100, 100, 500, 2.0, "advanced", ""),
]

SEED_SECTORS = [
    ("the_foundry", "The Foundry",
     "A cavernous manufacturing bay filled with automated forges, welding arms, and the constant hiss of plasma cutters. The air shimmers with heat.",
     "+25% crafting yield (chance of bonus output)"),
    ("the_exchange", "The Exchange",
     "A sleek trading floor with holographic price tickers and deal-making alcoves. Credits flow like water here.",
     "Tightest market spreads. You pay 5% over market (vs 10% elsewhere) and sell for 5% under market (vs 10% elsewhere)."),
    ("scrap_alley", "Scrap Alley",
     "A maze of decommissioned ship hulls and salvage yards. Treasure and danger in equal measure.",
     "Best exploration yields. Sabotage available."),
    ("the_commons", "The Commons",
     "A bustling social hub with cantinas, notice boards, and species from a hundred worlds mingling.",
     "+10% bonus rewards on fulfilled contracts"),
    ("void_dock", "Void Dock",
     "The station's edge, where ships arrive from the void. Crates of unknown origin pile up on the docks.",
     "Rare materials appear here. Volatile market prices."),
    ("research_bay", "Research Bay",
     "Gleaming laboratories and data terminals. Scientists and tinkerers pore over alien artifacts.",
     "Pay credits to discover new recipes."),
]

SEED_RECIPES = [
    # Solo recipes
    ("hull_plating", "Hull Plating", '{"scrap_metal": 2}', 1, 0, "solo"),
    ("power_cell", "Power Cell", '{"crystal_shards": 1, "plasma_coils": 1}', 1, 0, "solo"),
    ("med_patch", "Med-Patch", '{"bio_gel": 1, "coolant": 1}', 1, 0, "solo"),
    ("signal_beacon", "Signal Beacon", '{"data_fragments": 1, "crystal_shards": 1}', 1, 0, "solo"),
    ("thermal_shield", "Thermal Shield", '{"scrap_metal": 1, "coolant": 1}', 1, 0, "solo"),
    ("neural_lace", "Neural Lace", '{"data_fragments": 1, "bio_gel": 1}', 1, 0, "solo"),
    # Tier 2 solo recipes (crafted from crafted items)
    ("stasis_pod", "Stasis Pod", '{"med_patch": 1, "neural_lace": 1}', 1, 0, "solo"),
    ("armored_hull", "Armored Hull", '{"hull_plating": 1, "thermal_shield": 1}', 1, 0, "solo"),
    ("relay_array", "Relay Array", '{"signal_beacon": 1, "power_cell": 1}', 1, 0, "solo"),
    ("nav_computer", "Nav Computer", '{"neural_lace": 1, "signal_beacon": 1}', 1, 0, "solo"),
    # Passive/environmental recipes
    ("antimatter_siphon", "Antimatter Siphon", '{"void_dust": 1, "rare_earth": 1, "power_cell": 1}', 1, 0, "solo"),
    ("lucky_charm", "Lucky Charm", '{"antimatter": 10}', 1, 0, "solo"),
    ("fuel_rod", "Fuel Rod", '{"antimatter": 100}', 1, 0, "solo"),
    ("cooling_unit", "Cooling Unit", '{"coolant": 2, "scrap_metal": 1}', 1, 0, "solo"),
    ("jukebox", "Jukebox", '{"data_fragments": 1, "crystal_shards": 1, "scrap_metal": 1}', 1, 0, "solo"),
    ("grav_anchor", "Grav Anchor", '{"hull_plating": 1, "rare_earth": 1}', 1, 0, "solo"),
    # Cooperation recipes
    ("warp_drive", "Warp Drive", '{"power_cell": 1, "void_dust": 1, "rare_earth": 1}', 1, 1, "cooperation"),
    ("ai_core", "AI Core", '{"neural_lace": 1, "data_fragments": 3}', 1, 1, "cooperation"),
    ("quantum_relay", "Quantum Relay", '{"crystal_shards": 1, "void_dust": 1, "signal_beacon": 1}', 1, 1, "cooperation"),
    ("habitat_module", "Habitat Module", '{"hull_plating": 1, "med_patch": 1, "thermal_shield": 1}', 1, 1, "cooperation"),
]


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def _ensure_column(db: aiosqlite.Connection, table: str, column: str, definition: str):
    import re
    if not re.fullmatch(r'[a-z_]+', table):
        raise ValueError(f"Invalid table name: {table}")
    if not re.fullmatch(r'[a-z_0-9]+', column):
        raise ValueError(f"Invalid column name: {column}")
    rows = await db.execute_fetchall(f"PRAGMA table_info({table})")
    if column not in {r["name"] for r in rows}:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


async def init_db():
    db = await get_db()
    try:
        await db.executescript(SCHEMA)
        await _ensure_column(db, "contracts", "created_tick", "INTEGER")
        await _ensure_column(db, "agents", "consecutive_explores", "INTEGER NOT NULL DEFAULT 0")
        await _ensure_column(db, "bounties", "item2_name", "TEXT")
        await _ensure_column(db, "bounties", "item2_quantity", "INTEGER")
        await _ensure_column(db, "bounties", "cooperative", "INTEGER NOT NULL DEFAULT 0")
        await _ensure_column(db, "bounties", "min_reputation", "INTEGER NOT NULL DEFAULT 0")
        await _ensure_column(db, "bounties", "contributor1", "TEXT")
        await _ensure_column(db, "bounties", "contributor1_item", "TEXT")

        # Ensure new tables exist (for DBs created before these were added to SCHEMA)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                tick INTEGER NOT NULL,
                item_name TEXT NOT NULL,
                price REAL NOT NULL,
                PRIMARY KEY (tick, item_name)
            )
        """)

        # Seed items
        count = await db.execute_fetchall("SELECT COUNT(*) as c FROM items")
        if count[0][0] == 0:
            await db.executemany(
                "INSERT INTO items (name,display_name,description,base_price,current_price,supply,volatility,category,found_in) VALUES (?,?,?,?,?,?,?,?,?)",
                SEED_ITEMS,
            )

        # Seed sectors
        count = await db.execute_fetchall("SELECT COUNT(*) as c FROM sectors")
        if count[0][0] == 0:
            await db.executemany(
                "INSERT INTO sectors (name,display_name,description,bonus) VALUES (?,?,?,?)",
                SEED_SECTORS,
            )

        # Seed recipes
        count = await db.execute_fetchall("SELECT COUNT(*) as c FROM recipes")
        if count[0][0] == 0:
            await db.executemany(
                "INSERT INTO recipes (name,display_name,ingredients,output_qty,cooperation_required,category) VALUES (?,?,?,?,?,?)",
                SEED_RECIPES,
            )

        # Init world state
        await db.execute("INSERT OR IGNORE INTO world_state (key, value) VALUES ('tick', '0')")
        await db.execute("INSERT OR IGNORE INTO world_state (key, value) VALUES ('active_effects', '{}')")

        tick_rows = await db.execute_fetchall("SELECT value FROM world_state WHERE key = 'tick'")
        current_tick = int(tick_rows[0]["value"]) if tick_rows else 0
        await db.execute(
            "UPDATE contracts SET created_tick = ? WHERE created_tick IS NULL AND status IN ('open', 'active', 'deciding')",
            (current_tick,),
        )

        await db.commit()
    finally:
        await db.close()
