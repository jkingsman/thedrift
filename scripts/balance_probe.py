#!/usr/bin/env python3
"""Run small balance probes against a live The Drift server.

This intentionally creates fresh agents and performs real actions. It keeps
trade sizes small by default so the probe is useful on a shared local server
without heavily distorting the market.
"""

from __future__ import annotations

import argparse
import json
import random
import string
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from station.db import SEED_RECIPES


SOLO_RECIPES = {
    name: {
        "display": display,
        "ingredients": json.loads(ingredients),
        "output_qty": output_qty,
    }
    for name, display, ingredients, output_qty, coop, _category in SEED_RECIPES
    if not coop
}


@dataclass
class Agent:
    name: str
    token: str


class Client:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def _request(
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
            with urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except HTTPError as exc:
            raw = exc.read().decode()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"success": False, "message": raw}
            return exc.code, payload

    def get(self, path: str, token: str | None = None) -> dict[str, Any]:
        status, data = self._request("GET", path, token=token)
        if status >= 400:
            raise RuntimeError(f"GET {path} failed [{status}]: {data}")
        return data

    def post(self, path: str, body: dict[str, Any], token: str | None = None) -> tuple[int, dict[str, Any]]:
        return self._request("POST", path, body=body, token=token)

    def enter(self, prefix: str) -> Agent:
        suffix = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(8))
        name = f"{prefix}_{int(time.time())}_{suffix}"[:32]
        status, data = self.post("/api/enter", {"name": name})
        if status >= 400 or not data.get("success"):
            raise RuntimeError(f"enter failed [{status}]: {data}")
        return Agent(name=name, token=data["token"])

    def action(self, agent: Agent, action: str, **params: Any) -> dict[str, Any]:
        status, data = self.post(
            f"/api/agent/{agent.name}/action",
            {"action": action, **params},
            token=agent.token,
        )
        if status >= 500:
            raise RuntimeError(f"{agent.name} {action} failed [{status}]: {data}")
        return data


def as_float(value: Any) -> float:
    return float(str(value).replace(",", ""))


def market_index(client: Client) -> dict[str, dict[str, Any]]:
    data = client.get("/api/world/market")
    return {item["name"]: item for item in data["market"]}


def station_buy_price(item: dict[str, Any], sector: str = "the_exchange") -> float:
    key = "exchangeBuyPrice" if sector == "the_exchange" else "stationBuyPrice"
    return as_float(item[key])


def station_sell_price(item: dict[str, Any], sector: str = "the_exchange") -> float:
    key = "exchangeSellPrice" if sector == "the_exchange" else "stationSellPrice"
    return as_float(item[key])


def state_wealth(state: dict[str, Any], market: dict[str, dict[str, Any]]) -> float:
    total = as_float(state["credits"])
    for entry in state.get("inventory", []):
        item_name = entry.get("item")
        qty = entry.get("qty", entry.get("quantity", 0))
        if item_name in market:
            total += int(qty) * station_sell_price(market[item_name])
    return round(total, 2)


def scan_recipe_arbitrage(market: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for recipe_name, recipe in SOLO_RECIPES.items():
        output = market.get(recipe_name)
        if not output:
            continue
        ingredient_cost = 0.0
        missing = []
        for item_name, qty in recipe["ingredients"].items():
            item = market.get(item_name)
            if not item or not item.get("stationAvailable", True):
                missing.append(item_name)
                continue
            ingredient_cost += station_buy_price(item) * qty
        if missing:
            continue
        output_value = station_sell_price(output) * recipe["output_qty"]
        profit = round(output_value - ingredient_cost, 2)
        roi = round((profit / ingredient_cost) * 100, 1) if ingredient_cost else 0.0
        rows.append({
            "recipe": recipe_name,
            "display": recipe["display"],
            "buyCost": round(ingredient_cost, 2),
            "sellValue": round(output_value, 2),
            "profit": profit,
            "roiPct": roi,
            "affordableAtStart": ingredient_cost <= 500,
        })
    return sorted(rows, key=lambda r: r["profit"], reverse=True)


def run_direct_buy_sell(client: Client, item_name: str = "scrap_metal", quantity: int = 10) -> dict[str, Any]:
    agent = client.enter("ProbeSpread")
    market = market_index(client)
    start = 500.0
    buy = client.action(agent, "buy", item=item_name, quantity=quantity)
    sell = client.action(agent, "sell", item=item_name, quantity=quantity)
    end_market = market_index(client)
    end_state = sell.get("state") or client.action(agent, "status")["data"]
    return {
        "agent": agent.name,
        "strategy": "direct_buy_sell",
        "item": item_name,
        "quantity": quantity,
        "buySuccess": buy.get("success"),
        "sellSuccess": sell.get("success"),
        "startWealth": start,
        "endWealth": state_wealth(end_state, end_market),
        "delta": round(state_wealth(end_state, end_market) - start, 2),
        "initialBuyPrice": station_buy_price(market[item_name]),
        "initialSellPrice": station_sell_price(market[item_name]),
    }


def run_craft_loop(client: Client, recipe_name: str, cycles: int, include_cycles: bool = True) -> dict[str, Any]:
    agent = client.enter("ProbeCraft")
    start_market = market_index(client)
    start_state = client.action(agent, "status")["data"]
    start_wealth = state_wealth(start_state, start_market)
    recipe = SOLO_RECIPES[recipe_name]
    cycle_results = []

    for _ in range(cycles):
        ok = True
        for item_name, qty in recipe["ingredients"].items():
            result = client.action(agent, "buy", item=item_name, quantity=qty)
            ok = ok and bool(result.get("success"))
            if not result.get("success"):
                cycle_results.append({"success": False, "stage": "buy", "message": result.get("message")})
                break
        if not ok:
            break
        craft = client.action(agent, "craft", recipe=recipe_name)
        if not craft.get("success"):
            cycle_results.append({"success": False, "stage": "craft", "message": craft.get("message")})
            break
        output_qty = int(craft.get("data", {}).get("quantity", recipe["output_qty"]))
        sell = client.action(agent, "sell", item=recipe_name, quantity=output_qty)
        cycle_results.append({
            "success": bool(sell.get("success")),
            "craftedQty": output_qty,
            "message": sell.get("message"),
        })
        if not sell.get("success"):
            break

    end_market = market_index(client)
    end_state = client.action(agent, "status")["data"]
    end_wealth = state_wealth(end_state, end_market)
    completed = sum(1 for r in cycle_results if r.get("success"))
    result = {
        "agent": agent.name,
        "strategy": "craft_loop",
        "recipe": recipe_name,
        "requestedCycles": cycles,
        "completedCycles": completed,
        "startWealth": start_wealth,
        "endWealth": end_wealth,
        "delta": round(end_wealth - start_wealth, 2),
        "profitPerCycle": round((end_wealth - start_wealth) / completed, 2) if completed else 0.0,
    }
    if include_cycles:
        result["cycles"] = cycle_results
    return result


def run_rumor_pump(client: Client, item_name: str = "scrap_metal", quantity: int = 30) -> dict[str, Any]:
    agent = client.enter("ProbeRumor")
    start_market = market_index(client)
    start_state = client.action(agent, "status")["data"]
    start_wealth = state_wealth(start_state, start_market)
    buy = client.action(agent, "buy", item=item_name, quantity=quantity)
    rumor = client.action(agent, "rumor", item=item_name, direction="up")
    sell = client.action(agent, "sell", item=item_name, quantity=quantity)
    end_market = market_index(client)
    end_state = sell.get("state") or client.action(agent, "status")["data"]
    end_wealth = state_wealth(end_state, end_market)
    return {
        "agent": agent.name,
        "strategy": "buy_rumor_sell",
        "item": item_name,
        "quantity": quantity,
        "buySuccess": buy.get("success"),
        "rumorSuccess": rumor.get("success"),
        "sellSuccess": sell.get("success"),
        "rumor": rumor.get("data", {}),
        "startWealth": start_wealth,
        "endWealth": end_wealth,
        "delta": round(end_wealth - start_wealth, 2),
    }


def print_table(rows: list[dict[str, Any]], limit: int = 10):
    print("Top static solo craft loops at current Exchange prices:")
    print("recipe                 cost    sell   profit   roi")
    for row in rows[:limit]:
        print(
            f"{row['recipe']:<20} "
            f"{row['buyCost']:>7.2f} "
            f"{row['sellValue']:>7.2f} "
            f"{row['profit']:>8.2f} "
            f"{row['roiPct']:>6.1f}%"
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8888")
    parser.add_argument("--cycles", type=int, default=5)
    parser.add_argument("--craft-top", type=int, default=3, help="run live craft loops for the top N static opportunities")
    parser.add_argument("--summary-only", action="store_true", help="omit per-cycle details from live craft runs")
    parser.add_argument("--json", action="store_true", help="emit only JSON")
    args = parser.parse_args()

    client = Client(args.base_url)
    market = market_index(client)
    static_rows = scan_recipe_arbitrage(market)
    craft_targets = [
        r for r in static_rows
        if r["profit"] > 0 and r["affordableAtStart"]
    ][:args.craft_top]

    live_runs = [
        run_direct_buy_sell(client),
        run_rumor_pump(client),
    ]
    for target in craft_targets:
        live_runs.append(run_craft_loop(
            client,
            target["recipe"],
            args.cycles,
            include_cycles=not args.summary_only,
        ))

    report = {
        "baseUrl": args.base_url,
        "staticCraftArbitrage": static_rows,
        "liveRuns": live_runs,
        "flags": [
            row for row in static_rows
            if row["profit"] > 5 and row["roiPct"] > 10 and row["affordableAtStart"]
        ],
    }

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_table(static_rows)
        print()
        print("Live runs:")
        for run in live_runs:
            print(json.dumps(run, indent=2, sort_keys=True))
        if report["flags"]:
            print()
            print("Potential exploit flags:")
            for flag in report["flags"]:
                print(f"- {flag['recipe']}: +{flag['profit']:.2f} CR per cycle ({flag['roiPct']:.1f}% ROI)")


if __name__ == "__main__":
    main()
