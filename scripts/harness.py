#!/usr/bin/env python3
"""
Test harness for The Drift — observe and interact with a live server.

Provides three primitives:
  - run_curl(method, path, data?, headers?) → parsed JSON or text
  - write_file(path, content) → writes content to a local file
  - read_file(path) → reads content from a local file

Usage:
    python harness.py <base_url>

    # Interactive mode — drops into a REPL with the helpers available.
    # Or import and use programmatically.

Examples:
    python harness.py http://localhost:8080

    >>> world()                        # GET /api/world
    >>> activity()                     # recent activity
    >>> activity(agent="Alpha")        # filter by agent
    >>> enter("ObserverBot")           # register an agent
    >>> act("ObserverBot", "explore")  # take an action
    >>> agent("ObserverBot")           # check agent state
    >>> history("ObserverBot")         # full action history
    >>> agents_list()                  # all agents
    >>> market()                       # current prices
    >>> events()                       # recent world events
    >>> bounties()                     # active bounties
    >>> tail(n=20)                     # last N actions across all agents
"""

import json
import os
import subprocess
import sys

BASE_URL = ""
TOKENS = {}  # name -> token, persisted to .harness_tokens.json
TOKENS_FILE = ".harness_tokens.json"


# ── Core primitives ─────────────────────────────────────────────────

def run_curl(method: str, path: str, data: dict | None = None, headers: dict | None = None) -> dict | str:
    """Execute an HTTP request via curl and return parsed JSON (or raw text)."""
    url = f"{BASE_URL}{path}"
    cmd = ["curl", "-s", "-X", method, url]

    all_headers = {"Content-Type": "application/json"}
    if headers:
        all_headers.update(headers)

    for k, v in all_headers.items():
        cmd.extend(["-H", f"{k}: {v}"])

    if data is not None:
        cmd.extend(["-d", json.dumps(data)])

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return result.stdout


def write_file(path: str, content: str):
    """Write content to a local file."""
    with open(path, "w") as f:
        f.write(content)
    print(f"Wrote {len(content)} bytes to {path}")


def read_file(path: str) -> str:
    """Read content from a local file."""
    with open(path) as f:
        return f.read()


# ── Token management ────────────────────────────────────────────────

def _load_tokens():
    global TOKENS
    if os.path.exists(TOKENS_FILE):
        TOKENS = json.loads(read_file(TOKENS_FILE))


def _save_tokens():
    write_file(TOKENS_FILE, json.dumps(TOKENS, indent=2))


def _auth(name: str) -> dict:
    """Get auth headers for an agent. Returns empty dict if no token stored."""
    token = TOKENS.get(name)
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


# ── Convenience wrappers ────────────────────────────────────────────

def enter(name: str) -> dict:
    """Register a new agent. Saves token automatically."""
    result = run_curl("POST", "/api/enter", {"name": name})
    if isinstance(result, dict) and result.get("token"):
        TOKENS[name] = result["token"]
        _save_tokens()
        print(f"Entered as {name} — token saved.")
    return result


def act(name: str, action: str, **params) -> dict:
    """Execute an action as an agent."""
    body = {"action": action, **params}
    return run_curl("POST", f"/api/agent/{name}/action", body, _auth(name))


def agent(name: str) -> dict:
    """Get agent state."""
    return run_curl("GET", f"/api/agent/{name}", headers=_auth(name))


def history(name: str, limit: int = 50) -> dict:
    """Get an agent's action history."""
    return run_curl("GET", f"/api/agent/{name}/history?limit={limit}", headers=_auth(name))


def world() -> dict:
    """Get full world state."""
    return run_curl("GET", "/api/world")


def market() -> dict:
    """Get market prices."""
    return run_curl("GET", "/api/world/market")


def events(limit: int = 20) -> dict:
    """Get recent world events."""
    return run_curl("GET", "/api/world/events")


def bounties() -> dict:
    """Get active bounties."""
    return run_curl("GET", "/api/world/bounties")


def activity(agent: str | None = None, action: str | None = None, limit: int = 50) -> dict:
    """Get activity log with optional filters."""
    params = []
    if agent:
        params.append(f"agent={agent}")
    if action:
        params.append(f"action={action}")
    params.append(f"limit={limit}")
    qs = "&".join(params)
    return run_curl("GET", f"/api/world/activity?{qs}")


def agents_list() -> list:
    """Get all agents from world state."""
    w = world()
    if isinstance(w, dict) and "world" in w:
        return w["world"]["agents"]
    return []


def contracts() -> dict:
    """Get open contracts."""
    return run_curl("GET", "/api/world/contracts")


def listings() -> dict:
    """Get player listings."""
    return run_curl("GET", "/api/world/listings")


def sectors() -> dict:
    """Get sector info."""
    return run_curl("GET", "/api/world/sectors")


def feed() -> dict:
    """Get social feed."""
    return run_curl("GET", "/api/social/feed")


def tail(n: int = 20, agent: str | None = None) -> None:
    """Print the last N actions in a readable format."""
    data = activity(agent=agent, limit=n)
    if not isinstance(data, dict):
        print(data)
        return

    for entry in reversed(data.get("activity", [])):
        success = "OK" if entry.get("result", {}).get("success") else "FAIL"
        msg = entry.get("result", {}).get("message", "")[:80]
        print(f"  [{entry['tick']:>4}] {entry['agentName']:<16} {entry['action']:<18} {success:<4}  {msg}")


def pp(data) -> None:
    """Pretty-print JSON data."""
    if isinstance(data, (dict, list)):
        print(json.dumps(data, indent=2))
    else:
        print(data)


# ── Entry point ─────────────────────────────────────────────────────

def main():
    global BASE_URL

    if len(sys.argv) < 2:
        print("Usage: python harness.py <base_url>")
        print("  e.g. python harness.py http://localhost:8080")
        sys.exit(1)

    BASE_URL = sys.argv[1].rstrip("/")
    _load_tokens()

    # Quick connectivity check
    info = run_curl("GET", "/")
    if isinstance(info, dict) and info.get("name"):
        print(f"Connected to {info['name']} ({info.get('status', '?')}) at {BASE_URL}")
    else:
        print(f"Warning: could not reach {BASE_URL}")
        print(f"Response: {info}")

    if TOKENS:
        print(f"Loaded {len(TOKENS)} saved token(s): {', '.join(TOKENS.keys())}")

    print()
    print("Helpers: enter(name), act(name, action, **params), agent(name), history(name)")
    print("         world(), market(), events(), bounties(), activity(agent=, action=, limit=)")
    print("         agents_list(), contracts(), listings(), sectors(), feed()")
    print("         tail(n=20, agent=None), pp(data)")
    print("         run_curl(method, path, data=, headers=), write_file(path, content), read_file(path)")
    print()

    # Drop into interactive REPL
    import code
    code.interact(
        banner="Drift harness ready. Type help(tail) etc. for usage.",
        local=globals(),
        exitmsg="Bye.",
    )


if __name__ == "__main__":
    main()
