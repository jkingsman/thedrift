#!/usr/bin/env python3
"""Deterministic autoplayer for The Drift.

This is intentionally simple and inspectable. It is useful for smoke-testing
the live economy because it plays like a competent low-risk agent without using
an LLM or private strategy hidden in prompts.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from station.db import SEED_RECIPES


TOKEN_FILE = os.environ.get("AUTOPLAYER_TOKEN_FILE", ".autoplayer_tokens.json")
SECTORS = ["the_exchange", "scrap_alley", "the_foundry", "the_commons", "void_dock", "research_bay"]
RAW_ITEM_SECTORS = {
    "scrap_metal": ["scrap_alley", "the_foundry"],
    "crystal_shards": ["void_dock", "research_bay"],
    "void_dust": ["void_dock"],
    "bio_gel": ["the_commons", "scrap_alley"],
    "plasma_coils": ["the_foundry", "void_dock"],
    "data_fragments": ["research_bay", "the_exchange"],
    "rare_earth": ["scrap_alley"],
    "coolant": ["the_foundry", "the_commons"],
}
PASSIVE_ACTIONS = {"status", "scan", "view_bounties", "view_contracts", "read_messages"}

RECIPES = {
    name: {
        "display": display,
        "ingredients": json.loads(ingredients),
        "output_qty": output_qty,
        "cooperation_required": bool(coop),
        "category": category,
    }
    for name, display, ingredients, output_qty, coop, category in SEED_RECIPES
}
SOLO_RECIPES = {name: recipe for name, recipe in RECIPES.items() if not recipe["cooperation_required"]}


@dataclass
class Agent:
    name: str
    token: str


class Client:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"

        req = Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read().decode())
        except HTTPError as exc:
            raw = exc.read().decode()
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, {"success": False, "message": raw}
        except (URLError, TimeoutError) as exc:
            return 0, {"success": False, "message": str(exc)}

    def get(self, path: str, token: str | None = None) -> dict[str, Any]:
        _status, data = self.request("GET", path, token=token)
        return data

    def action(self, agent: Agent, action: str, **params: Any) -> dict[str, Any]:
        _status, data = self.request(
            "POST",
            f"/api/agent/{agent.name}/action",
            {"action": action, **params},
            token=agent.token,
        )
        return data

    def enter_or_resume(self, name: str) -> Agent:
        tokens = load_tokens()
        if name in tokens:
            return Agent(name, tokens[name])

        status, data = self.request("POST", "/api/enter", {"name": name})
        if status == 409:
            raise SystemExit(f"Agent {name!r} already exists, but no token is saved in {TOKEN_FILE}.")
        if not data.get("success") or "token" not in data:
            raise SystemExit(f"Failed to enter as {name}: {data}")
        tokens[name] = data["token"]
        save_tokens(tokens)
        return Agent(name, data["token"])


def load_tokens() -> dict[str, str]:
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            return json.load(f)
    return {}


def save_tokens(tokens: dict[str, str]):
    with open(TOKEN_FILE, "w") as f:
        json.dump(tokens, f, indent=2, sort_keys=True)


def as_float(value: Any) -> float:
    return float(str(value).replace(",", ""))


def inventory_map(state: dict[str, Any]) -> dict[str, int]:
    inv: dict[str, int] = {}
    for entry in state.get("inventory", []):
        name = entry.get("item")
        qty = entry.get("qty", entry.get("quantity", 0))
        if name:
            inv[name] = inv.get(name, 0) + int(qty)
    return inv


def market_index(client: Client) -> dict[str, dict[str, Any]]:
    data = client.get("/api/world/market")
    return {item["name"]: item for item in data.get("market", [])}


def sell_price(item: dict[str, Any], sector: str) -> float:
    key = "exchangeSellPrice" if sector == "the_exchange" else "stationSellPrice"
    return as_float(item[key])


def buy_price(item: dict[str, Any], sector: str) -> float:
    key = "exchangeBuyPrice" if sector == "the_exchange" else "stationBuyPrice"
    return as_float(item[key])


def can_craft(recipe: dict[str, Any], inv: dict[str, int]) -> bool:
    return all(inv.get(item, 0) >= qty for item, qty in recipe["ingredients"].items())


def missing_for_recipe(recipe: dict[str, Any], inv: dict[str, int]) -> dict[str, int]:
    missing = {}
    for item, qty in recipe["ingredients"].items():
        have = inv.get(item, 0)
        if have < qty:
            missing[item] = qty - have
    return missing


def choose_craft(inv: dict[str, int], market: dict[str, dict[str, Any]], sector: str) -> str | None:
    candidates = []
    for name, recipe in SOLO_RECIPES.items():
        if not can_craft(recipe, inv):
            continue
        item = market.get(name)
        if not item:
            continue
        current = as_float(item["currentPrice"])
        base = as_float(item["basePrice"])
        # Avoid dumping into a visibly saturated station market.
        score = sell_price(item, sector) - (0 if current >= base * 0.75 else 1000)
        candidates.append((score, name))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1] if candidates[0][0] > -100 else None


def choose_sale(inv: dict[str, int], market: dict[str, dict[str, Any]], sector: str) -> tuple[str, int] | None:
    candidates = []
    for item_name, qty in inv.items():
        item = market.get(item_name)
        if not item:
            continue
        current = as_float(item["currentPrice"])
        base = as_float(item["basePrice"])
        category = item["category"]
        if category == "raw" and qty > 8:
            candidates.append((sell_price(item, sector), item_name, max(1, min(3, qty - 5))))
        elif category == "crafted" and qty > 0 and current >= base * 0.85:
            candidates.append((sell_price(item, sector), item_name, 1))
        elif category == "advanced" and qty > 0 and current >= base:
            candidates.append((sell_price(item, sector), item_name, 1))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    _value, item_name, qty = candidates[0]
    return item_name, qty


def choose_bounty_action(state: dict[str, Any], pending: list[dict[str, Any]]) -> dict[str, Any] | None:
    for notice in pending:
        if notice.get("type") == "bounty_completable":
            return {"action": "complete_bounty", "bounty_id": notice["bountyId"]}
    return None


def choose_target_sector(inv: dict[str, int]) -> str:
    # Work toward the cheapest early recipes first.
    priority = ["hull_plating", "thermal_shield", "med_patch", "power_cell", "neural_lace", "signal_beacon"]
    for recipe_name in priority:
        missing = missing_for_recipe(SOLO_RECIPES[recipe_name], inv)
        for item_name in missing:
            sectors = RAW_ITEM_SECTORS.get(item_name)
            if sectors:
                return random.choice(sectors)
    return random.choice(["scrap_alley", "the_foundry", "the_commons", "void_dock", "research_bay"])


def decide(client: Client, state: dict[str, Any], pending: list[dict[str, Any]], strategy: str) -> dict[str, Any]:
    inv = inventory_map(state)
    sector = state["sector"]
    credits = as_float(state["credits"])
    market = market_index(client)

    for notice in pending:
        if notice.get("type") == "contract_decision":
            return {"action": "fulfill", "contract_id": notice["contractId"]}
        if notice.get("type") == "unread_messages":
            return {"action": "read_messages"}

    bounty = choose_bounty_action(state, pending)
    if bounty:
        return bounty

    craft = choose_craft(inv, market, sector)
    if craft and strategy in {"balanced", "crafter"}:
        return {"action": "craft", "recipe": craft}

    sale = choose_sale(inv, market, sector)
    if sale and (credits < 150 or strategy != "scavenger"):
        item, qty = sale
        if sector != "the_exchange":
            return {"action": "move", "sector": "the_exchange"}
        return {"action": "sell", "item": item, "quantity": qty}

    # Conservative buy: only fill a single missing component for a nearly-ready
    # recipe when the output is not saturated and the wallet can absorb it.
    if strategy == "crafter" and credits > 250:
        for recipe_name in ["power_cell", "neural_lace", "signal_beacon", "hull_plating", "thermal_shield", "med_patch"]:
            recipe = SOLO_RECIPES[recipe_name]
            missing = missing_for_recipe(recipe, inv)
            if len(missing) == 1:
                item_name, qty = next(iter(missing.items()))
                item = market.get(item_name)
                output = market.get(recipe_name)
                if item and output and item.get("stationAvailable", True):
                    cost = buy_price(item, sector) * qty
                    if cost < credits * 0.25 and as_float(output["currentPrice"]) >= as_float(output["basePrice"]) * 0.8:
                        if sector != "the_exchange":
                            return {"action": "move", "sector": "the_exchange"}
                        return {"action": "buy", "item": item_name, "quantity": qty}

    target = choose_target_sector(inv)
    if sector != target and random.random() < 0.35:
        return {"action": "move", "sector": target}
    return {"action": "explore"}


def run(args: argparse.Namespace):
    client = Client(args.base_url)
    agent = client.enter_or_resume(args.name)
    print(f"Autoplayer: {agent.name} at {args.base_url}")

    for turn in range(1, args.turns + 1):
        status = client.action(agent, "status")
        if not status.get("success"):
            print(f"{turn:03d} status failed: {status.get('message')}")
            break
        state = status.get("data", {})
        pending = status.get("pending", [])
        action = decide(client, state, pending, args.strategy)
        if args.dry_run:
            print(f"{turn:03d} dry-run {json.dumps(action)}")
            continue
        result = client.action(agent, **action)
        ok = "OK" if result.get("success") else "NO"
        new_state = result.get("state", {})
        credits = new_state.get("credits", state.get("credits"))
        print(f"{turn:03d} {ok} {action['action']:<16} {result.get('message', '')[:95]} credits={credits}")
        if args.delay:
            time.sleep(args.delay)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8888")
    parser.add_argument("--name", default="CodexAuto")
    parser.add_argument("--turns", type=int, default=50)
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--strategy", choices=["balanced", "scavenger", "crafter"], default="balanced")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
