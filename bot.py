"""Telegram bot that shows the EcoFlow AC input voltage, live.

Commands:
    /voltage  - send a message and keep updating it every second
    /stop     - stop updating
    /plot [minutes] - add a live chart, refreshed every 30 s (default: last 60 minutes, max 180)
    /start    - help

The live chart and text stay at the bottom of the chat: when the bot posts
an alert, it re-posts them below it. Every reading is logged to
voltage_log.csv; charts use the last 3 hours kept in memory.

Sends a warning to TELEGRAM_ALLOWED_IDS when the voltage stays out of range
(VOLTAGE_LOW / VOLTAGE_HIGH in .env, default 190 / 250 V) for
VOLTAGE_ALERT_AFTER seconds (default 10), and again when it's back to normal.

Reads data via the app-login route (ecoflow_mqtt.py), so it needs
ECOFLOW_EMAIL, ECOFLOW_PASSWORD and ECOFLOW_SN in .env, plus:
    TELEGRAM_BOT_TOKEN   - from @BotFather
    TELEGRAM_ALLOWED_IDS - comma-separated chat IDs allowed to use the bot (optional but recommended)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from telegram import BotCommand, InputMediaPhoto, Update
from telegram.error import BadRequest, RetryAfter
from telegram.ext import Application, CommandHandler, ContextTypes

import history
from alerts import VoltageAlarm
from ecoflow_api import load_dotenv
from ecoflow_mqtt import LiveReader

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)
# httpx logs every request URL, and Telegram URLs contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)

load_dotenv()
sn = os.environ["ECOFLOW_SN"]
allowed_ids = {int(x) for x in os.environ.get("TELEGRAM_ALLOWED_IDS", "").split(",") if x.strip()}

UPDATE_EVERY = 1.0  # seconds between message edits
STALE_AFTER = 15    # seconds without device data before saying so
CHART_EVERY = 30.0  # seconds between live chart refreshes

# Alerts go to every chat in TELEGRAM_ALLOWED_IDS.
alarm = VoltageAlarm(
    low=float(os.environ.get("VOLTAGE_LOW", 190)),
    high=float(os.environ.get("VOLTAGE_HIGH", 250)),
    hold=float(os.environ.get("VOLTAGE_ALERT_AFTER", 10)),
)

reader = LiveReader(sn, interval=UPDATE_EVERY)

# Each chat can have a live chart and, below it, a live text message, both kept
# at the bottom of the chat. Saved so updates resume after a restart:
# {chat id: {"text": message id, "chart": message id or None, "minutes": int}}
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
    # Older versions saved just the text message id.
    return {int(c): v if isinstance(v, dict) else {"text": v} for c, v in saved.items()}


def is_allowed(update: Update) -> bool:
    return not allowed_ids or update.effective_chat.id in allowed_ids


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


async def keep_editing(chat_id: int, message_id: int, every: float, edit) -> None:
    """Call `edit()` every `every` seconds until the message is gone."""
    while True:
        await asyncio.sleep(every)
        try:
            await edit()
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except BadRequest as e:
            if "not modified" in str(e).lower():
                continue
            log.warning("Stopped live updates in %s: %s", chat_id, e)  # e.g. message deleted
            return
        except Exception:
            log.exception("Live update failed in %s, retrying", chat_id)  # e.g. network blip


def resume_live(bot, chat_id: int, state: dict) -> None:
    live[chat_id] = state
    tasks = []
    if state.get("chart"):
        async def edit_chart(message_id=state["chart"], minutes=state.get("minutes", 60)):
            png = await asyncio.to_thread(history.plot_png, minutes)
            if png:
                await bot.edit_message_media(InputMediaPhoto(png), chat_id=chat_id, message_id=message_id)
        tasks.append(asyncio.create_task(keep_editing(chat_id, state["chart"], CHART_EVERY, edit_chart)))
    if state.get("text"):
        last_text = [None]

        async def edit_text(message_id=state["text"]):
            text = status_text()
            if text != last_text[0]:
                await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
                last_text[0] = text
        tasks.append(asyncio.create_task(keep_editing(chat_id, state["text"], UPDATE_EVERY, edit_text)))
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


async def post_live(bot, chat_id: int, chart_minutes: int | None = None) -> None:
    """(Re)post the live chart and text at the bottom of the chat.

    chart_minutes: show a chart of that many minutes; None keeps whatever
    chart the chat already had (or none).
    """
    old = await stop_live(bot, chat_id, delete=True) or {}
    if chart_minutes is None and old.get("chart"):
        chart_minutes = old.get("minutes", 60)
    state = {"text": None, "chart": None, "minutes": chart_minutes}
    if chart_minutes:
        png = await asyncio.to_thread(history.plot_png, chart_minutes)
        if png:
            state["chart"] = (await bot.send_photo(chat_id, png)).message_id
    state["text"] = (await bot.send_message(chat_id, status_text())).message_id
    resume_live(bot, chat_id, state)
    save_live()


async def keep_live_last(bot, chat_id: int) -> None:
    """After the bot posts something else, move the live messages back to the bottom."""
    if chat_id in live:
        await post_live(bot, chat_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Send /voltage to see the AC input voltage, updated every second. /stop stops it.\n"
        f"/plot adds a live chart of the last hour, refreshed every {CHART_EVERY:.0f} s; "
        f"/plot 15 shows the last 15 minutes (up to {history.KEEP_MINUTES}).\n"
        f"You'll get a warning if the voltage stays below {alarm.low:.0f} V or above {alarm.high:.0f} V.\n"
        f"Your chat ID: {update.effective_chat.id}"
    )


async def voltage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    await post_live(context.bot, update.effective_chat.id)


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
    if await asyncio.to_thread(history.plot_png, minutes) is None:
        await update.message.reply_text("No readings recorded yet — try again in a minute")
        return
    await post_live(context.bot, update.effective_chat.id, chart_minutes=minutes)


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
        text = alarm.message(level, reader.volts)
        log.info("Alert: %s", text)
        for chat_id in allowed_ids:
            try:
                await app.bot.send_message(chat_id, text)
                await keep_live_last(app.bot, chat_id)
            except Exception:
                log.exception("Could not send alert to %s", chat_id)


async def on_startup(app: Application) -> None:
    # Shown in Telegram's Menu button and when typing "/".
    await app.bot.set_my_commands([
        BotCommand("voltage", "Live voltage, updated every second"),
        BotCommand("plot", "Live chart, last 60 min (e.g. /plot 15)"),
        BotCommand("stop", "Stop live updates"),
        BotCommand("start", "Help and your chat ID"),
    ])
    await asyncio.to_thread(history.load_recent)
    await asyncio.to_thread(reader.start)
    log.info("Connected to EcoFlow, watching %s", sn)
    # app.create_task warns before the app is running, so use asyncio directly.
    asyncio.create_task(record_history())
    if allowed_ids:
        asyncio.create_task(watch_voltage(app))
    else:
        log.warning("TELEGRAM_ALLOWED_IDS is empty, so voltage alerts are off")
    log.info("Alerts below %.0f V or above %.0f V", alarm.low, alarm.high)
    for chat_id, state in load_live().items():
        if not allowed_ids or chat_id in allowed_ids:
            resume_live(app.bot, chat_id, state)
    if live:
        log.info("Resumed live updates in %d chat(s)", len(live))


async def on_shutdown(app: Application) -> None:
    reader.stop()


def main() -> None:
    app = (
        Application.builder()
        .token(os.environ["TELEGRAM_BOT_TOKEN"])
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("voltage", voltage))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("plot", plot))
    app.run_polling()


if __name__ == "__main__":
    main()
