import asyncio
import json
import logging
import time

from aiohttp import web

from station.db import init_db
from station.routes import setup_routes
from station.tick import tick_loop

log = logging.getLogger(__name__)
req_log = logging.getLogger("drift.requests")


async def on_startup(app: web.Application):
    log.info("Initializing database...")
    await init_db()
    log.info("Database ready. Starting tick loop...")
    app["tick_task"] = asyncio.create_task(tick_loop())


async def on_shutdown(app: web.Application):
    task = app.get("tick_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    log.info("Shutdown complete.")


@web.middleware
async def request_logger(request: web.Request, handler):
    t0 = time.monotonic()
    agent = request.match_info.get("name", "-")
    body_summary = ""

    if request.method == "POST" and request.content_type == "application/json":
        try:
            raw = await request.read()
            body = json.loads(raw)
            # For actions, show the action + params compactly
            if "action" in body:
                params = {k: v for k, v in body.items() if k != "action"}
                body_summary = f" action={body['action']}"
                if params:
                    body_summary += f" {params}"
            else:
                body_summary = f" body={body}"
        except Exception:
            body_summary = " body=(parse error)"

    ip = request.headers.get("X-Real-IP", request.remote)
    resp = await handler(request)
    elapsed = (time.monotonic() - t0) * 1000

    if resp.status >= 500:
        # Server error — always log with full detail
        resp_body = ""
        if hasattr(resp, "text"):
            resp_body = f" resp={resp.text[:200]}"
        req_log.error(
            "%s %s [%s] agent=%s ip=%s%s%s (%.0fms)",
            request.method, request.path, resp.status, agent, ip, body_summary, resp_body, elapsed,
        )
    elif resp.status >= 400:
        # Client error — warn with context for abuse detection
        resp_snippet = ""
        if hasattr(resp, "body") and resp.body:
            try:
                resp_snippet = f" reason={json.loads(resp.body).get('message', '')[:100]}"
            except Exception:
                pass
        req_log.warning(
            "%s %s [%s] agent=%s ip=%s%s%s (%.0fms)",
            request.method, request.path, resp.status, agent, ip, body_summary, resp_snippet, elapsed,
        )
    else:
        req_log.info(
            "%s %s [%s] agent=%s%s (%.0fms)",
            request.method, request.path, resp.status, agent, body_summary, elapsed,
        )
    return resp


def create_app() -> web.Application:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    app = web.Application(middlewares=[request_logger])
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    setup_routes(app)
    return app
