"""Live voltage web page, opened from the bot as a Telegram Mini App.

It updates every second without touching Telegram's rate limits: the page
polls this server directly. Only Telegram users in TELEGRAM_ALLOWED_IDS can
read the data — each request carries Telegram's signed `initData`, which is
checked against the bot token.

The share button asks the server to prepare a message (chart picture and a
summary) with Bot API `savePreparedInlineMessage`; the page then opens
Telegram's chat picker with `shareMessage`. Telegram downloads the picture
from /share/<random>.jpg, so those links are public but unguessable, and
expire with the prepared message.

Serves on 127.0.0.1; a reverse proxy (Caddy) adds HTTPS in front, which
Telegram requires for Mini Apps.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web
from telegram import Bot, InlineQueryResultPhoto

import history

log = logging.getLogger(__name__)

PAGE = Path(__file__).with_name("static") / "index.html"
INIT_DATA_MAX_AGE = 24 * 3600  # seconds a signed Telegram login stays valid
SHARE_KEEP = 24 * 3600  # seconds a shared chart picture stays downloadable at most
SHARE_MAX = 30          # shared pictures kept in memory at once


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


def share_caption(reader, minutes: int) -> str:
    """Summary text sent with a shared chart."""
    now = time.time()
    rows = history.recent(now - minutes * 60)
    known = [v for _, v in rows if v is not None]
    hm = lambda ts: datetime.fromtimestamp(ts).strftime("%H:%M")
    on = reader.electricity_on() if reader.updated_at and now - reader.updated_at < 15 else None
    lines = [f"⚡ AC input voltage, last {minutes} min"]
    if on:
        lines.append(f"🔌 Now: {reader.volts:.1f} V")
    elif on is False:
        lines.append("⚫ Now: no electricity")
    if known:
        lines.append(f"📉 Min {min(known):.1f} V · 📈 Max {max(known):.1f} V")
    spans = []
    for start, end in history.outages(rows):
        if (end or now) - start < history.MARK_OUTAGES_FROM:
            continue
        if rows and start == rows[0][0]:  # began before this range: start unknown
            spans.append(f"until {hm(end)}" if end else "the whole time")
        else:
            spans.append(f"{hm(start)}–{hm(end) if end else 'now'}")
    if spans:
        lines.append("⚫ No electricity: " + ", ".join(spans))
    lines.append(f"🕒 {datetime.fromtimestamp(now):%d.%m %H:%M}")
    return "\n".join(lines)


def make_app(reader, bot: Bot, allowed_ids: set[int], limits: tuple[float, float],
             public_url: str = "") -> web.Application:
    shared: dict[str, tuple[bytes, float]] = {}  # token -> (JPEG, expires at)

    def user_of(request: web.Request) -> int | None:
        user_id = telegram_user_id(request.headers.get("X-Telegram-Init-Data", ""), bot.token)
        if user_id is None or (allowed_ids and user_id not in allowed_ids):
            return None
        return user_id

    def authorized(request: web.Request) -> bool:
        return user_of(request) is not None

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

    async def share(request: web.Request) -> web.Response:
        user_id = user_of(request)
        if user_id is None:
            return web.json_response({"error": "Open this page from the Telegram bot"}, status=403)
        if not public_url:
            return web.json_response({"error": "Sharing needs WEBAPP_URL in .env"}, status=503)
        try:
            minutes = int((await request.json()).get("minutes", 60))
        except (ValueError, TypeError, AttributeError):
            minutes = 60
        minutes = max(1, min(minutes, history.KEEP_MINUTES))
        jpeg = await asyncio.to_thread(history.plot_png, minutes, limits, "jpeg")
        if jpeg is None:
            return web.json_response({"error": "No readings yet"}, status=404)
        now = time.time()
        for token in [t for t, (_, exp) in shared.items() if exp < now]:
            del shared[token]
        while len(shared) >= SHARE_MAX:
            del shared[next(iter(shared))]  # oldest first
        token = secrets.token_urlsafe(16)
        shared[token] = (jpeg, now + SHARE_KEEP)
        photo_url = f"{public_url.rstrip('/')}/share/{token}.jpg"
        try:
            prepared = await bot.save_prepared_inline_message(
                user_id,
                InlineQueryResultPhoto(token, photo_url, photo_url,
                                       caption=share_caption(reader, minutes)),
                allow_user_chats=True, allow_bot_chats=True,
                allow_group_chats=True, allow_channel_chats=True,
            )
        except Exception as e:
            del shared[token]
            log.warning("Could not prepare a shared chart: %s", e)
            return web.json_response({"error": "Telegram didn't accept it, try again"}, status=502)
        if prepared.expiration_date:
            shared[token] = (jpeg, min(prepared.expiration_date.timestamp(), now + SHARE_KEEP))
        return web.json_response({"id": prepared.id})

    async def shared_picture(request: web.Request) -> web.Response:
        item = shared.get(request.match_info["token"])
        if not item or item[1] < time.time():
            raise web.HTTPNotFound()
        return web.Response(body=item[0], content_type="image/jpeg")

    app = web.Application()
    app.router.add_get("/", page)
    app.router.add_get("/api/data", data)
    app.router.add_post("/api/share", share)
    app.router.add_get("/share/{token}.jpg", shared_picture)
    return app


async def start(reader, bot: Bot, allowed_ids: set[int], limits: tuple[float, float],
                port: int, public_url: str = "") -> web.AppRunner:
    runner = web.AppRunner(make_app(reader, bot, allowed_ids, limits, public_url), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    log.info("Live page served on 127.0.0.1:%d", port)
    return runner
