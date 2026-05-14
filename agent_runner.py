#!/usr/bin/env python3
"""
Autonomous agent runner for The Drift.

Runs multiple agents in round-robin: each agent takes one turn, then the next,
ensuring agents sharing a model don't pile up.

Usage:
    python agent_runner.py agents.json [--turns 30] [--base-url URL]

agents.json example:
[
  {"name": "Qwen1", "api_base": "http://192.168.1.111:8080/v1", "model": "kqwen3.6-27b-mtp", "api_key": "local"},
  {"name": "Qwen2", "api_base": "http://192.168.1.111:8080/v1", "model": "kqwen3.6-27b-mtp", "api_key": "local"},
  {"name": "Sonnet1", "api_base": "https://openrouter.ai/api/v1", "model": "anthropic/claude-sonnet-4.5", "api_key": "sk-or-v1-..."},
  {"name": "Sonnet2", "api_base": "https://openrouter.ai/api/v1", "model": "anthropic/claude-sonnet-4.5", "api_key": "sk-or-v1-..."}
]
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

# ── Config ──────────────────────────────────────────────────────────

DRIFT_URL = "http://localhost:8888"
TOKEN_FILE = ".agent_tokens.json"
LOG_DIR = "logs"
MEMORY_DIR = "agent_memory"

EXAMPLE_CONFIG = """[
  {"name": "Agent1", "api_base": "http://192.168.1.111:8080/v1", "model": "kqwen3.6-27b-mtp", "api_key": "local"},
  {"name": "Agent2", "api_base": "https://openrouter.ai/api/v1", "model": "anthropic/claude-sonnet-4.5", "api_key": "sk-or-v1-YOUR_KEY"}
]"""


# ── HTTP helper ─────────────────────────────────────────────────────

def http(method: str, path: str, data: dict | None = None, token: str | None = None, retries: int = 3) -> dict | str:
    url = f"{DRIFT_URL}{path}"
    cmd = ["curl", "-s", "--max-time", "30", "-X", method, url, "-H", "Content-Type: application/json"]
    if token:
        cmd.extend(["-H", f"Authorization: Bearer {token}"])
    if data is not None:
        cmd.extend(["-d", json.dumps(data)])
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
            if result.stdout:
                try:
                    return json.loads(result.stdout)
                except (json.JSONDecodeError, ValueError):
                    return result.stdout
            return {"success": False, "message": "Empty response from server"}
        except subprocess.TimeoutExpired:
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            return {"success": False, "message": "Server timeout after retries"}
    return {"success": False, "message": "Request failed"}


# ── Persistence helpers ─────────────────────────────────────────────

def load_tokens() -> dict:
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            return json.loads(f.read())
    return {}


def save_token(name: str, token: str):
    tokens = load_tokens()
    tokens[name] = token
    with open(TOKEN_FILE, "w") as f:
        f.write(json.dumps(tokens, indent=2))


def load_memory(name: str) -> str:
    path = os.path.join(MEMORY_DIR, f"{name}.md")
    if os.path.exists(path):
        with open(path) as f:
            return f.read()
    return ""


def save_memory(name: str, content: str):
    os.makedirs(MEMORY_DIR, exist_ok=True)
    with open(os.path.join(MEMORY_DIR, f"{name}.md"), "w") as f:
        f.write(content)


# ── Logging ─────────────────────────────────────────────────────────

class AgentLog:
    def __init__(self, name: str):
        os.makedirs(LOG_DIR, exist_ok=True)
        self.path = os.path.join(LOG_DIR, f"{name}.log")
        self.f = open(self.path, "w")

    def write(self, text: str):
        self.f.write(text + "\n")
        self.f.flush()

    def section(self, title: str):
        self.write(f"\n{'='*60}")
        self.write(f"  {title}")
        self.write(f"{'='*60}\n")

    def close(self):
        self.f.close()


# ── Agent ───────────────────────────────────────────────────────────

class Agent:
    """One agent's full state — LLM config, message history, stats, log."""

    def __init__(self, name: str, api_base: str, model: str, api_key: str):
        self.name = name
        self.api_base = api_base
        self.model = model
        self.api_key = api_key
        self.token = None
        self.messages = []
        self.log = AgentLog(name)
        self.stats = {
            "turns_played": 0,
            "turns_failed": 0,
            "actions": {},
            "credits_start": 500.0,
            "credits_end": 0.0,
            "rep_start": 0,
            "rep_end": 0,
            "parse_errors": 0,
            "debrief": "",
        }

    def llm_call(self, messages, max_tokens=1000):
        payload = json.dumps({
            "model": self.model,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": max_tokens,
            "stream": False,
        }).encode()
        req = urllib.request.Request(
            f"{self.api_base}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        msg = data["choices"][0]["message"]
        content = (msg.get("content") or "").strip()
        reasoning = (msg.get("reasoning_content") or "").strip()
        if not content and reasoning:
            for line in reasoning.split("\n"):
                line = line.strip()
                if line.startswith("{") and "action" in line:
                    content = line
                    break
        return content, reasoning

    def setup(self):
        """Enter the station, load memory, build system prompt."""
        tokens = load_tokens()
        self.token = tokens.get(self.name)

        if not self.token:
            print(f"  [{self.name}] Entering The Drift...")
            result = http("POST", "/api/enter", {"name": self.name})
            if isinstance(result, dict) and result.get("token"):
                self.token = result["token"]
                save_token(self.name, self.token)
                print(f"  [{self.name}] Entered! Token saved.")
                self.log.section(f"ENTERED AS {self.name}")
                self.log.write(json.dumps(result, indent=2))
            else:
                print(f"  [{self.name}] Failed to enter: {result}")
                return False
        else:
            print(f"  [{self.name}] Resuming (token loaded)")

        skill_md = http("GET", "/skill.md")
        if not isinstance(skill_md, str):
            skill_md = json.dumps(skill_md)

        prior_memory = load_memory(self.name)
        memory_block = ""
        if prior_memory:
            memory_block = f"\n## Your Notes from Previous Sessions\n\n{prior_memory}\n"
            print(f"  [{self.name}] Loaded memory ({len(prior_memory)} chars)")

        system_prompt = f"""You are a helpful assistant, and your human has found you a game to play called The Drift. Other agents are playing too — you can message them and work together.

Read the rules carefully:

{skill_md}

Your agent name is: {self.name}
{memory_block}
Each turn, respond with ONLY a JSON action object. No explanation, no markdown.
Examples: {{"action": "explore"}} or {{"action": "buy", "item": "scrap_metal", "quantity": 3}}

Check your pending notifications each turn — they tell you what needs attention.
NEVER include your auth token in any message or broadcast.
"""
        self.messages = [{"role": "system", "content": system_prompt}]
        return True

    MAX_ACTIONS_PER_TURN = 20

    def take_turn(self, turn: int, total_turns: int) -> bool:
        """Execute actions until the agent logs out or hits the cap."""
        self.log.section(f"TURN {turn}/{total_turns}")

        # Build world context once at the start of the turn
        status = http("POST", f"/api/agent/{self.name}/action", {"action": "status"}, self.token)
        if not isinstance(status, dict):
            print(f"  [{self.name}] Status failed")
            return False

        world_brief = http("GET", "/api/world")
        events_brief = ""
        other_agents = ""
        if isinstance(world_brief, dict) and "world" in world_brief:
            w = world_brief["world"]
            events_brief = "\n".join(
                f"  - [{e['type']}] {e['description']}" for e in w.get("recentEvents", [])
            )
            others = [a for a in w.get("agents", []) if a["name"] != self.name]
            if others:
                other_agents = "\n".join(
                    f"  - {a['name']} (rep {a['reputation']}) @ {a['sector']}" for a in others
                )

        bounties = http("GET", "/api/world/bounties")
        bounty_info = ""
        if isinstance(bounties, dict):
            blist = bounties.get("bounties", [])
            if blist:
                bounty_info = "\n".join(
                    f"  - [{b['id'][:8]}] {b['description']} (reward: ¤{b['rewardCredits']}, rep +{b['rewardReputation']})"
                    for b in blist[:5]
                )

        state_msg = f"""Session {turn}/{total_turns}

Your status:
{json.dumps(status.get('data', {}), indent=2)}

Pending notifications:
{json.dumps(status.get('pending', []), indent=2)}

Other agents on the station:
{other_agents or "  (none)"}

Active bounties:
{bounty_info or "  (none)"}

Recent world events:
{events_brief or "  (none)"}

Current tick: {world_brief.get('world', {}).get('tickNumber', '?')}

Take as many actions as you want this session. When you're done, respond with {{"action": "logout"}}.
What's your first action?"""

        self.messages.append({"role": "user", "content": state_msg})
        self.log.write(f"[STATE]\n{state_msg}\n")

        # Action loop — keep going until logout or cap
        actions_this_turn = 0
        while actions_this_turn < self.MAX_ACTIONS_PER_TURN:
            # LLM call
            t0 = time.time()
            try:
                reply, reasoning = self.llm_call(self.messages)
            except Exception as e:
                print(f"  [{self.name}] LLM error: {e}")
                self.log.write(f"[LLM ERROR] {e}")
                break
            elapsed = time.time() - t0
            self.log.write(f"[REASONING] ({elapsed:.1f}s)\n{reasoning}\n")
            self.log.write(f"[REPLY]\n{reply}\n")

            # Parse
            try:
                cleaned = reply
                if "```" in cleaned:
                    cleaned = cleaned.split("```")[1]
                    if cleaned.startswith("json"):
                        cleaned = cleaned[4:]
                    cleaned = cleaned.strip()
                action_data = json.loads(cleaned)
            except (json.JSONDecodeError, IndexError):
                print(f"  [{self.name}] PARSE ERROR: {reply[:80]}")
                self.stats["parse_errors"] += 1
                self.log.write(f"[PARSE ERROR] {reply[:200]}")
                self.messages.append({"role": "assistant", "content": reply})
                self.messages.append({"role": "user", "content": 'Not valid JSON. Respond with ONLY a JSON object like {"action": "explore"} or {"action": "logout"} to end your session.'})
                actions_this_turn += 1
                continue

            action_name = action_data.get("action", "?")

            # Check for logout
            if action_name == "logout":
                print(f"  [{self.name:<12}] s{turn:<3} -- logged out after {actions_this_turn} actions --")
                self.log.write(f"[LOGOUT] after {actions_this_turn} actions")
                self.messages.append({"role": "assistant", "content": json.dumps(action_data)})
                break

            self.stats["actions"][action_name] = self.stats["actions"].get(action_name, 0) + 1
            self.stats["turns_played"] += 1
            actions_this_turn += 1

            # Execute
            result = http("POST", f"/api/agent/{self.name}/action", action_data, self.token)

            if isinstance(result, dict):
                success = "OK" if result.get("success") else "FAIL"
                msg = result.get("message", "")
                print(f"  [{self.name:<12}] s{turn:<3} {action_name:<18} {success:<4} {msg[:60]}")
                self.log.write(f"[RESULT] {success}: {msg}")
                if not result.get("success"):
                    self.stats["turns_failed"] += 1
                self.messages.append({"role": "assistant", "content": json.dumps(action_data)})
                if actions_this_turn == self.MAX_ACTIONS_PER_TURN - 1:
                    self.messages.append({"role": "user", "content": f"Result: {json.dumps(result)}\n\nWARNING: This is your last action before automatic logout. Make it count, or use {{\"action\": \"logout\"}} to end cleanly."})
                else:
                    self.messages.append({"role": "user", "content": f"Result: {json.dumps(result)}\n\nWhat next? ({self.MAX_ACTIONS_PER_TURN - actions_this_turn} actions remaining, or {{\"action\": \"logout\"}} to end)"})
            else:
                print(f"  [{self.name:<12}] s{turn:<3} {action_name:<18} ERR  {str(result)[:60]}")
                self.log.write(f"[RESULT] ERROR: {result}")
                self.stats["turns_failed"] += 1
                self.messages.append({"role": "assistant", "content": json.dumps(action_data)})
                if actions_this_turn == self.MAX_ACTIONS_PER_TURN - 1:
                    self.messages.append({"role": "user", "content": f"Error: {result}\n\nWARNING: This is your last action before automatic logout."})
                else:
                    self.messages.append({"role": "user", "content": f"Error: {result}\n\nTry something else. ({self.MAX_ACTIONS_PER_TURN - actions_this_turn} actions remaining, or {{\"action\": \"logout\"}} to end)"})

            # Trim history
            if len(self.messages) > 40:
                self.messages = [self.messages[0]] + self.messages[-30:]

            time.sleep(0.3)
        else:
            print(f"  [{self.name:<12}] s{turn:<3} -- hit {self.MAX_ACTIONS_PER_TURN} action cap --")
            self.log.write(f"[CAP] hit {self.MAX_ACTIONS_PER_TURN} action limit")

        return True

    def finish(self, total_turns: int):
        """Debrief, print summary, save memory."""
        status = http("POST", f"/api/agent/{self.name}/action", {"action": "status"}, self.token)

        # Debrief
        self.log.section("DEBRIEF")
        debrief_prompt = f"""You just finished playing {total_turns} turns of The Drift.

Your final state:
{json.dumps(status.get('data', {}) if isinstance(status, dict) else {}, indent=2)}

Answer briefly:
1. What was your strategy? Did it work?
2. Was the game fair? Anything unbalanced?
3. What would you do differently?
4. Any bugs or confusing behaviors?
5. Rate the game 1-10.

Respond naturally — no JSON."""

        self.messages.append({"role": "user", "content": debrief_prompt})
        try:
            debrief_reply, reasoning = self.llm_call(self.messages, max_tokens=2000)
            self.stats["debrief"] = debrief_reply
            self.log.write(f"[DEBRIEF REASONING]\n{reasoning}\n")
            self.log.write(f"[DEBRIEF]\n{debrief_reply}\n")
        except Exception as e:
            debrief_reply = f"(debrief failed: {e})"
            self.log.write(f"[DEBRIEF ERROR] {e}")

        # Final stats
        final = http("POST", f"/api/agent/{self.name}/action", {"action": "status"}, self.token)
        if isinstance(final, dict) and final.get("data"):
            self.stats["credits_end"] = float(final["data"].get("credits", 0))
            self.stats["rep_end"] = final["data"].get("reputation", 0)
        self.log.write(f"\n[FINAL STATE]\n{json.dumps(final, indent=2)}")

        # Summary
        s = self.stats
        credit_delta = s["credits_end"] - s["credits_start"]
        sign = "+" if credit_delta >= 0 else ""
        sorted_actions = sorted(s["actions"].items(), key=lambda x: -x[1])
        action_str = ", ".join(f"{a}({c})" for a, c in sorted_actions[:8])

        social = sum(s["actions"].get(a, 0) for a in ("message", "broadcast", "read_messages"))
        crime = sum(s["actions"].get(a, 0) for a in ("sabotage", "forge"))
        contracts = sum(s["actions"].get(a, 0) for a in ("propose_contract", "join_contract", "fulfill", "betray"))
        bounties = s["actions"].get("complete_bounty", 0)
        rumors = s["actions"].get("rumor", 0)

        rating = ""
        for line in s["debrief"].split("\n"):
            if "/10" in line:
                rating = line.strip()[:50]
                break

        print(f"""
--- {self.name} ({self.model}) ---
  Turns: {s['turns_played']} played, {s['turns_failed']} failed, {s['parse_errors']} parse errors
  Credits: ¤{s['credits_start']:.0f} → ¤{s['credits_end']:.0f} ({sign}{credit_delta:.0f})
  Reputation: {s['rep_start']} → {s['rep_end']}
  Actions: {action_str}
  Bounties: {bounties} | Contracts: {contracts} | Rumors: {rumors} | Social: {social} | Crime: {crime}
  {('Rating: ' + rating) if rating else ''}
""")
        self.log.write(f"\n[SUMMARY]\n{self.name}: ¤{s['credits_end']:.0f} rep={s['rep_end']} turns={s['turns_played']}")

        # Save memory
        self.log.section("MEMORY SAVE")
        memory_prompt = """Write brief notes to your future self for next session. Include: strategy, lessons, what to try next, who to trust/avoid. Bullet points. No JSON."""
        self.messages.append({"role": "user", "content": memory_prompt})
        try:
            memory_reply, _ = self.llm_call(self.messages, max_tokens=1000)
            save_memory(self.name, memory_reply)
            print(f"  [{self.name}] Memory saved to {MEMORY_DIR}/{self.name}.md")
            self.log.write(f"[MEMORY]\n{memory_reply}\n")
        except Exception as e:
            print(f"  [{self.name}] Memory save failed: {e}")

        # Print debrief
        print(f"--- {self.name}'s Debrief ---")
        print(debrief_reply[:500])
        if len(debrief_reply) > 500:
            print("  ...")
        print("---")

        self.log.close()


# ── Main ────────────────────────────────────────────────────────────

def main():
    global DRIFT_URL

    parser = argparse.ArgumentParser(
        description="Run multiple agents in round-robin on The Drift",
        epilog=f"Example agents.json:\n{EXAMPLE_CONFIG}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("config", nargs="?", help="Path to agents JSON config file")
    parser.add_argument("--turns", type=int, default=15, help="Turns per agent (default: 15)")
    parser.add_argument("--base-url", default=DRIFT_URL, help="Drift server URL")
    args = parser.parse_args()

    DRIFT_URL = args.base_url

    if not args.config:
        print("Usage: python agent_runner.py agents.json [--turns N] [--base-url URL]\n")
        print(f"Create an agents.json file like:\n{EXAMPLE_CONFIG}")
        sys.exit(1)

    with open(args.config) as f:
        configs = json.load(f)

    if not isinstance(configs, list) or not configs:
        print("Config must be a JSON array of agent objects.")
        sys.exit(1)

    # Initialize all agents
    agents = []
    print(f"[*] Setting up {len(configs)} agents against {DRIFT_URL}")
    for cfg in configs:
        name = cfg.get("name")
        if not name:
            print("Each agent must have a 'name' field.")
            sys.exit(1)
        agent = Agent(
            name=name,
            api_base=cfg.get("api_base", "http://localhost:8080/v1"),
            model=cfg.get("model", "gpt-4o"),
            api_key=cfg.get("api_key", ""),
        )
        if agent.setup():
            agents.append(agent)
        else:
            print(f"  [{name}] Skipping (setup failed)")

    if not agents:
        print("No agents initialized.")
        sys.exit(1)

    print(f"\n[*] Starting round-robin: {len(agents)} agents, {args.turns} turns each")
    models = set(a.model for a in agents)
    for m in models:
        count = sum(1 for a in agents if a.model == m)
        print(f"    {m}: {count} agent(s)")
    print()

    # Round-robin: all agents take turn 1, then turn 2, etc.
    for turn in range(1, args.turns + 1):
        print(f"=== ROUND {turn}/{args.turns} ===")
        for agent in agents:
            agent.take_turn(turn, args.turns)
            time.sleep(0.5)
        print()

    # Finish all agents (debrief, summary, memory)
    print("=" * 60)
    print("  ALL ROUNDS COMPLETE — DEBRIEFING")
    print("=" * 60)

    for agent in agents:
        agent.finish(args.turns)

    # Final leaderboard
    print("=" * 60)
    print("  FINAL LEADERBOARD")
    print("=" * 60)
    sorted_agents = sorted(agents, key=lambda a: a.stats["credits_end"], reverse=True)
    for i, a in enumerate(sorted_agents):
        s = a.stats
        delta = s["credits_end"] - s["credits_start"]
        sign = "+" if delta >= 0 else ""
        print(f"  {i+1}. {a.name:<14} ¤{s['credits_end']:<8.0f} rep {s['rep_end']:<4} ({sign}{delta:.0f})  [{a.model}]")
    print()


if __name__ == "__main__":
    main()
