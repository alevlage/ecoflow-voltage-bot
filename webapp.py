"""Live voltage web page, opened from the bot as a Telegram Mini App.

It updates every second without touching Telegram's rate limits: the page
polls this server directly. Only Telegram users in TELEGRAM_ALLOWED_IDS can
read the data — each request carries Telegram's signed `initData`, which is
checked against the bot token.

Serves on 127.0.0.1; a reverse proxy (Caddy) adds HTTPS in front, which
Telegram requires for Mini Apps.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

import history

log = logging.getLogger(__name__)

PAGE = Path(__file__).with_name("static") / "index.html"
INIT_DATA_MAX_AGE = 24 * 3600  # seconds a signed Telegram login stays valid


def telegram_user_id(init_data: str, bot_token: str) -> int | None:
    """Return the user id from a valid Telegram Mini App initData, else None.

    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = fields.pop("hash", "")
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not received_hash or not hmac.compare_digest(expected, received_hash):
        return None
    if time.time() - int(fields.get("auth_date", 0)) > INIT_DATA_MAX_AGE:
        return None
    try:
        return int(json.loads(fields["user"])["id"])
    except (KeyError, ValueError, TypeError):
        return None


def make_app(reader, bot_token: str, allowed_ids: set[int], limits: tuple[float, float]) -> web.Application:
    def authorized(request: web.Request) -> bool:
        user_id = telegram_user_id(request.headers.get("X-Telegram-Init-Data", ""), bot_token)
        return user_id is not None and (not allowed_ids or user_id in allowed_ids)

    async def page(request: web.Request) -> web.Response:
        return web.FileResponse(PAGE, headers={"Cache-Control": "no-cache"})

    async def data(request: web.Request) -> web.Response:
        if not authorized(request):
            return web.json_response({"error": "Open this page from the Telegram bot"}, status=403)
        try:
            since = float(request.query.get("since", 0))
        except ValueError:
            since = 0.0
        since = max(since, time.time() - history.KEEP_MINUTES * 60)
        return web.json_response({
            "now": {
                "volts": reader.volts,
                "on": reader.electricity_on(),
                "ts": reader.updated_at or None,
            },
            "history": history.recent(since),
            "keep_minutes": history.KEEP_MINUTES,
            "low": limits[0],
            "high": limits[1],
        })

    app = web.Application()
    app.router.add_get("/", page)
    app.router.add_get("/api/data", data)
    return app


async def start(reader, bot_token: str, allowed_ids: set[int], limits: tuple[float, float],
                port: int) -> web.AppRunner:
    runner = web.AppRunner(make_app(reader, bot_token, allowed_ids, limits), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    log.info("Live page served on 127.0.0.1:%d", port)
    return runner
