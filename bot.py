"""Telegram bot that shows the EcoFlow AC input voltage.

Commands:
    /voltage  - a message that updates for 15 minutes, then pauses
    /plot [minutes] - add a chart above it (default: last 60 minutes, max 180)
    /live     - button that opens the live page (updates every second)
    /stop     - stop updating
    /start    - help

Why live updates in the chat are limited: Telegram blocks bots that edit
messages nonstop. Editing every 3 s for ~4.5 hours got this bot blocked for
16 hours, alerts included. So chat updates run in 15-minute sessions
(LIVE_MINUTES), text at most every 10 s, chart every 2 minutes. The live
page (webapp.py, at WEBAPP_URL) is the always-on, every-second view: it
talks to this server directly, not through Telegram.

The live chart and text stay at the bottom of the chat: when the bot posts
an alert, it re-posts them below it. Every reading is logged to
voltage_log.csv; charts use the last 3 hours kept in memory.

Sends a warning to TELEGRAM_ALLOWED_IDS when the voltage stays out of range
(VOLTAGE_LOW / VOLTAGE_HIGH in .env, default 180 / 250 V) for
VOLTAGE_ALERT_AFTER seconds (default 10), and again when it's back to normal.
Also says when the electricity comes back after an outage of a minute or more.

Reads data via the app-login route (ecoflow_mqtt.py), so it needs
ECOFLOW_EMAIL, ECOFLOW_PASSWORD and ECOFLOW_SN in .env, plus:
    TELEGRAM_BOT_TOKEN   - from @BotFather
    TELEGRAM_ALLOWED_IDS - comma-separated chat IDs allowed to use the bot (optional but recommended)
    WEBAPP_URL           - public https URL of the live page (optional; no button without it)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto,
                      Update, WebAppInfo)
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes

import history
import webapp
from alerts import VoltageAlarm
from ecoflow_api import load_dotenv
from ecoflow_mqtt import LiveReader

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)
# httpx logs every request URL, and Telegram URLs contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)

load_dotenv()
sn = os.environ["ECOFLOW_SN"]
bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
allowed_ids = {int(x) for x in os.environ.get("TELEGRAM_ALLOWED_IDS", "").split(",") if x.strip()}
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").strip()
WEBAPP_PORT = int(os.environ.get("WEBAPP_PORT", 8080))

UPDATE_EVERY = 1.0  # seconds between device snapshot requests
TEXT_EVERY = float(os.environ.get("LIVE_UPDATE_EVERY", 10))  # seconds between chat text edits
CHART_EVERY = 120.0  # seconds between chat chart refreshes
LIVE_MINUTES = float(os.environ.get("LIVE_MINUTES", 15))  # length of a chat live session
STALE_AFTER = 15    # seconds without device data before saying so
LATE_AFTER = 120.0  # seconds; a message sent later than this says it's late
POWER_BACK_AFTER = 60.0  # seconds; shorter outages get no "electricity is back" message
MAX_BACKOFF = 300.0  # slowest edit interval after repeated rate limits
COMMAND_MAX_WAIT = 30.0  # a command gives up rather than wait longer for a rate limit

# Alerts go to every chat in TELEGRAM_ALLOWED_IDS.
alarm = VoltageAlarm(
    low=float(os.environ.get("VOLTAGE_LOW", 180)),
    high=float(os.environ.get("VOLTAGE_HIGH", 250)),
    hold=float(os.environ.get("VOLTAGE_ALERT_AFTER", 10)),
)

reader = LiveReader(sn, interval=UPDATE_EVERY)

# Each chat can have a live chart and, below it, a live text message, both kept
# at the bottom of the chat. Saved so updates resume after a restart:
# {chat id: {"text": id, "chart": id or None, "minutes": int, "until": timestamp}}
LIVE_PATH = Path(__file__).with_name("live_messages.json")
live: dict[int, dict] = {}
live_tasks: dict[int, list[asyncio.Task]] = {}


def save_live() -> None:
    LIVE_PATH.write_text(json.dumps({str(c): v for c, v in live.items()}))


def load_live() -> dict[int, dict]:
    try:
        saved = json.loads(LIVE_PATH.read_text())
    except (FileNotFoundError, ValueError):
        return {}
    # Older versions saved just the text message id, and had no "until".
    return {int(c): v if isinstance(v, dict) else {"text": v} for c, v in saved.items()}


def is_allowed(update: Update) -> bool:
    return not allowed_ids or update.effective_chat.id in allowed_ids


def page_button() -> InlineKeyboardMarkup | None:
    if not WEBAPP_URL:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton("📈 Live page", web_app=WebAppInfo(WEBAPP_URL))]])


def status_text() -> str:
    if not reader.updated_at:
        return "⏳ Waiting for data from EcoFlow…"
    stamp = datetime.fromtimestamp(reader.updated_at).strftime("%H:%M:%S")
    if time.time() - reader.updated_at > STALE_AFTER:
        return f"⚠️ No data from the device since {stamp}"
    on = reader.electricity_on()
    if on is None:
        return f"⏳ Checking whether there's electricity…\n🕒 {stamp}"
    if on:
        return f"🔌 Electricity is on: {reader.volts:.1f} V\n🕒 {stamp}"
    return f"⚫ No electricity\n🕒 {stamp}"


def paused_text() -> str:
    hint = "Tap 📈 Live page for every-second updates, or" if WEBAPP_URL else "Send"
    return f"{status_text()}\n⏸ Updates paused. {hint} /voltage to resume."


async def keep_editing(chat_id: int, every: float, until: float, edit, finish=None) -> None:
    """Call `edit()` every `every` seconds until `until`, then `finish()` once.

    When Telegram rate-limits us, wait as told and halve the edit rate; speed
    back up gradually once edits go through again.
    """
    interval = every
    ok_streak = 0
    while time.time() < until:
        await asyncio.sleep(min(interval, max(0.0, until - time.time())))
        if time.time() >= until:
            break
        try:
            await edit()
            ok_streak += 1
            if interval > every and ok_streak >= 20:
                interval = max(every, interval / 2)
                ok_streak = 0
        except RetryAfter as e:
            ok_streak = 0
            interval = min(interval * 2, MAX_BACKOFF)
            log.warning("Telegram rate limit in %s: waiting %s s, then editing every %.0f s",
                        chat_id, e.retry_after, interval)
            await asyncio.sleep(float(e.retry_after))
        except BadRequest as e:
            if "not modified" in str(e).lower():
                continue
            log.warning("Stopped live updates in %s: %s", chat_id, e)  # e.g. message deleted
            return
        except Exception as e:
            log.warning("Live update failed in %s (%s), retrying", chat_id, e)  # e.g. network blip
    if finish:
        try:
            await finish()
        except Exception as e:
            log.warning("Could not mark live updates as paused in %s: %s", chat_id, e)


async def with_retry(send, attempts: int = 3, max_wait: float | None = None):
    """Run `send()`, waiting out Telegram rate limits, so alerts aren't lost.

    max_wait: give up (re-raise) instead of waiting longer than this, so a
    command doesn't hang for minutes during a long rate limit.
    """
    for attempt in range(attempts):
        try:
            return await send()
        except RetryAfter as e:
            wait = float(e.retry_after)
            if attempt == attempts - 1 or (max_wait is not None and wait > max_wait):
                raise
            log.warning("Telegram rate limit: waiting %.0f s before sending", wait)
            await asyncio.sleep(wait)


def resume_live(bot, chat_id: int, state: dict) -> None:
    live[chat_id] = state
    until = state.get("until", 0)
    tasks = []
    if state.get("chart"):
        async def edit_chart(message_id=state["chart"], minutes=state.get("minutes", 60)):
            png = await asyncio.to_thread(history.plot_png, minutes, (alarm.low, alarm.high))
            if png:
                await bot.edit_message_media(InputMediaPhoto(png), chat_id=chat_id, message_id=message_id)
        tasks.append(asyncio.create_task(
            keep_editing(chat_id, CHART_EVERY, until, edit_chart, finish=edit_chart)))
    if state.get("text"):
        last_text = [None]

        async def edit_text(message_id=state["text"]):
            text = status_text()
            if text != last_text[0]:
                await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                                            reply_markup=page_button())
                last_text[0] = text

        async def finish_text(message_id=state["text"]):
            await bot.edit_message_text(paused_text(), chat_id=chat_id, message_id=message_id,
                                        reply_markup=page_button())
            if live.get(chat_id) is state:  # session over: nothing to re-post after alerts
                live.pop(chat_id)
                live_tasks.pop(chat_id, None)
                save_live()
        tasks.append(asyncio.create_task(
            keep_editing(chat_id, TEXT_EVERY, until, edit_text, finish=finish_text)))
    live_tasks[chat_id] = tasks


async def stop_live(bot, chat_id: int, delete: bool = False) -> dict | None:
    """Stop live updates in a chat; returns what was live, or None."""
    for task in live_tasks.pop(chat_id, []):
        task.cancel()
    state = live.pop(chat_id, None)
    save_live()
    if delete and state:
        for message_id in (state.get("chart"), state.get("text")):
            if message_id:
                try:
                    await bot.delete_message(chat_id, message_id)
                except Exception:
                    pass  # already deleted, or too old to delete
    return state


post_locks: dict[int, asyncio.Lock] = {}  # commands run concurrently; one re-post per chat at a time


async def post_live(bot, chat_id: int, chart_minutes: int | None = None,
                    max_wait: float | None = None, until: float | None = None) -> None:
    """(Re)post the live chart and text at the bottom of the chat.

    chart_minutes: show a chart of that many minutes; None keeps whatever
    chart the chat already had (or none).
    until: when the session ends; None starts a new LIVE_MINUTES session.

    The new messages are sent first and the old ones deleted only after
    that worked, so a failure (e.g. a rate limit) leaves the old ones live.
    """
    async with post_locks.setdefault(chat_id, asyncio.Lock()):
        old = live.get(chat_id) or {}
        if chart_minutes is None and old.get("chart"):
            chart_minutes = old.get("minutes", 60)
        state = {"text": None, "chart": None, "minutes": chart_minutes,
                 "until": until or time.time() + LIVE_MINUTES * 60}
        if chart_minutes:
            png = await asyncio.to_thread(history.plot_png, chart_minutes, (alarm.low, alarm.high))
            if png:
                state["chart"] = (await with_retry(lambda: bot.send_photo(chat_id, png),
                                                   max_wait=max_wait)).message_id
        try:
            state["text"] = (await with_retry(
                lambda: bot.send_message(chat_id, status_text(), reply_markup=page_button()),
                max_wait=max_wait)).message_id
        except Exception:
            if state["chart"]:  # don't leave a half-posted pair behind
                try:
                    await bot.delete_message(chat_id, state["chart"])
                except Exception:
                    pass
            raise
        await stop_live(bot, chat_id, delete=True)
        resume_live(bot, chat_id, state)
        save_live()


async def keep_live_last(bot, chat_id: int) -> None:
    """After the bot posts something else, move a running session back to the bottom."""
    state = live.get(chat_id)
    if state and state.get("until", 0) > time.time():
        await post_live(bot, chat_id, until=state["until"])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    page = "/live opens a page that updates every second.\n" if WEBAPP_URL else ""
    await update.message.reply_text(
        f"/voltage shows the AC input voltage, updated every {TEXT_EVERY:.0f} s "
        f"for {LIVE_MINUTES:.0f} min. /stop stops it.\n"
        f"/plot adds a chart of the last hour; /plot 15 shows the last 15 minutes "
        f"(up to {history.KEEP_MINUTES}).\n"
        f"{page}"
        f"You'll get a warning if the voltage stays below {alarm.low:.0f} V or above {alarm.high:.0f} V, "
        f"and a message when the electricity comes back.\n"
        f"Your chat ID: {update.effective_chat.id}",
        reply_markup=page_button(),
    )


async def voltage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    await post_live(context.bot, update.effective_chat.id, max_wait=COMMAND_MAX_WAIT)


async def live_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    if not WEBAPP_URL:
        await update.message.reply_text("The live page isn't set up (WEBAPP_URL is empty).")
        return
    await update.message.reply_text("Tap to open the live page:", reply_markup=page_button())


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    stopped = await stop_live(context.bot, update.effective_chat.id)
    await update.message.reply_text("⏹ Stopped live updates" if stopped else "Nothing to stop")


async def plot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    try:
        minutes = max(1, int(context.args[0])) if context.args else 60
    except ValueError:
        await update.message.reply_text("Usage: /plot [minutes], e.g. /plot 30")
        return
    minutes = min(minutes, history.KEEP_MINUTES)
    if await asyncio.to_thread(history.plot_png, minutes, (alarm.low, alarm.high)) is None:
        await update.message.reply_text("No readings recorded yet — try again in a minute")
        return
    await post_live(context.bot, update.effective_chat.id, chart_minutes=minutes,
                    max_wait=COMMAND_MAX_WAIT)


async def record_history() -> None:
    """Log each new reading once, so the chart has one point per update."""
    last = 0.0
    while True:
        await asyncio.sleep(UPDATE_EVERY)
        if reader.updated_at and reader.updated_at != last:
            last = reader.updated_at
            on = reader.electricity_on()
            if on is not None:  # skip the first seconds while it's still unclear
                await asyncio.to_thread(history.append, reader.volts, on, reader.watts, last)


# Alerts wait here for send_notifications(), so a long Telegram rate limit
# delays the messages but never the voltage checks. Created in on_startup.
outbox: asyncio.Queue | None = None


def notify_all(text: str, at: float | None = None) -> None:
    """Queue `text` for every allowed chat. `at` is when it happened (default: now)."""
    log.info("Notify: %s", text.replace("\n", " | "))
    outbox.put_nowait((text, at or time.time()))


def with_time(text: str, at: float) -> str:
    """Add when it happened, and say so if the message is going out late."""
    when = datetime.fromtimestamp(at)
    text += f"\n🕒 {when:%H:%M}" if when.date() == datetime.now().date() else f"\n🕒 {when:%d.%m %H:%M}"
    late = time.time() - at
    if late > LATE_AFTER:
        text += f"\n⏳ Sent {took(late)} late: Telegram was holding the bot's messages"
    return text


async def send_notifications(app: Application) -> None:
    while True:
        text, at = await outbox.get()
        for chat_id in allowed_ids:
            try:
                # Built on each attempt, so a late note shows the real delay.
                await with_retry(lambda: app.bot.send_message(
                    chat_id, with_time(text, at), reply_markup=page_button()))
                await keep_live_last(app.bot, chat_id)
            except Exception:
                log.exception("Could not send a message to %s", chat_id)


def took(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{max(minutes, 1)} min"
    return f"{minutes // 60} h {minutes % 60} min" if minutes % 60 else f"{minutes // 60} h"


async def watch_power(app: Application) -> None:
    """Message every allowed chat when the electricity comes back after an outage."""
    off_since = history.current_outage_start()  # outage already going on at startup
    while True:
        await asyncio.sleep(UPDATE_EVERY)
        now = time.time()
        if not reader.updated_at or now - reader.updated_at > STALE_AFTER:
            continue  # no fresh data from the device: can't tell
        on = reader.electricity_on()
        if on is False and off_since is None:
            # The voltage froze when the power went, so that's when it started.
            off_since = reader.volts_changed_at or now
        elif on and off_since is not None:
            off_for, started = now - off_since, off_since
            off_since = None
            if off_for >= POWER_BACK_AFTER:
                notify_all(f"💡 Electricity is back: {reader.volts:.1f} V\n"
                           f"⚫ It was off for {took(off_for)}, "
                           f"since {datetime.fromtimestamp(started):%H:%M}")


async def watch_voltage(app: Application) -> None:
    """Message every allowed chat when the voltage goes out of range or back."""
    last = 0.0
    while True:
        await asyncio.sleep(UPDATE_EVERY)
        # Only judge fresh readings while there's electricity; during an
        # outage the device shows a frozen leftover voltage.
        if not reader.updated_at or reader.updated_at == last or not reader.electricity_on():
            continue
        last = reader.updated_at
        level = alarm.update(reader.volts, last)
        if level is None:
            continue
        notify_all(alarm.message(level, reader.volts), last)


async def on_startup(app: Application) -> None:
    global outbox
    # Shown in Telegram's Menu button and when typing "/".
    commands = [
        BotCommand("voltage", f"Voltage, updated for {LIVE_MINUTES:.0f} min"),
        BotCommand("plot", "Chart, last 60 min (e.g. /plot 15)"),
        BotCommand("stop", "Stop updates"),
        BotCommand("start", "Help and your chat ID"),
    ]
    if WEBAPP_URL:
        commands.insert(0, BotCommand("live", "Live page, updated every second"))
    try:
        await app.bot.set_my_commands(commands)
    except Exception as e:  # e.g. still rate-limited; must not stop the bot from starting
        log.warning("Could not set the command menu: %s", e)
    await asyncio.to_thread(history.load_recent)
    await asyncio.to_thread(reader.start)
    log.info("Connected to EcoFlow, watching %s", sn)
    # app.create_task warns before the app is running, so use asyncio directly.
    asyncio.create_task(record_history())
    if allowed_ids:
        outbox = asyncio.Queue()
        asyncio.create_task(send_notifications(app))
        asyncio.create_task(watch_voltage(app))
        asyncio.create_task(watch_power(app))
    else:
        log.warning("TELEGRAM_ALLOWED_IDS is empty, so voltage alerts are off")
    log.info("Alerts below %.0f V or above %.0f V", alarm.low, alarm.high)
    app.bot_data["web"] = await webapp.start(reader, app.bot, allowed_ids,
                                             (alarm.low, alarm.high), WEBAPP_PORT, WEBAPP_URL)
    for chat_id, state in load_live().items():
        # Resume only sessions that haven't ended; old saves have no "until".
        if (not allowed_ids or chat_id in allowed_ids) and state.get("until", 0) > time.time():
            resume_live(app.bot, chat_id, state)
    save_live()
    if live:
        log.info("Resumed live updates in %d chat(s)", len(live))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log command failures in one line; rate limits and timeouts aren't bugs."""
    err = context.error
    if isinstance(err, (RetryAfter, TimedOut, NetworkError)):
        log.warning("Command failed: %s", err)
    else:
        log.error("Command failed", exc_info=err)


async def on_shutdown(app: Application) -> None:
    reader.stop()
    if "web" in app.bot_data:
        await app.bot_data["web"].cleanup()


def main() -> None:
    app = (
        Application.builder()
        .concurrent_updates(True)  # a slow command must not block the others
        .token(bot_token)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("voltage", voltage))
    app.add_handler(CommandHandler("live", live_page))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("plot", plot))
    app.add_error_handler(on_error)
    app.run_polling()


if __name__ == "__main__":
    main()
