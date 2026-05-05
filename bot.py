"""
TikTok FYP Scraper — Telegram Bot
Uses TikTok's internal API directly with session cookies.
No browser = no bot detection = no stalling.
"""

import os
import re
import json
import time
import random
import asyncio
import logging
import threading
from io import BytesIO
from datetime import datetime
from collections import defaultdict

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── env ───────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED_USERS_RAW = os.environ.get("ALLOWED_USERS", "")
ALLOWED_USERS = set(int(x.strip()) for x in ALLOWED_USERS_RAW.split(",") if x.strip())

# ── per-user state ─────────────────────────────────────────────────────────────
user_state: dict[int, dict] = defaultdict(lambda: {
    "running":     False,
    "target":      50,
    "results":     [],
    "thread":      None,
    "stop_event":  None,
    "cookies":     None,   # dict — {name: value} for requests
    "cookie_info": "",
})


# ── auth ──────────────────────────────────────────────────────────────────────
def is_allowed(uid: int) -> bool:
    return not ALLOWED_USERS or uid in ALLOWED_USERS

def guard(fn):
    async def _w(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_allowed(update.effective_user.id):
            await update.effective_message.reply_text("⛔ Not authorized.")
            return
        return await fn(update, ctx)
    _w.__name__ = fn.__name__
    return _w


# ── cookie parsing ────────────────────────────────────────────────────────────
def parse_cookie_json(raw: str) -> tuple[dict, str]:
    """
    Parse cookie JSON from Cookie-Editor.
    Returns (cookie_dict, info_string).
    cookie_dict is {name: value} — ready for requests.Session.cookies.update()
    """
    data = json.loads(raw)

    if isinstance(data, dict):
        items = []
        for k, v in data.items():
            if isinstance(v, dict):
                e = v.copy(); e.setdefault("name", k); items.append(e)
            else:
                items.append({"name": k, "value": str(v)})
        data = items

    if not isinstance(data, list):
        raise ValueError("Cookie JSON must be an array or object.")

    # Build flat {name: value} dict — that's all requests needs
    cookie_dict = {}
    for c in data:
        name  = c.get("name", "").strip()
        value = str(c.get("value", "")).strip()
        if name and value:
            cookie_dict[name] = value

    if not cookie_dict:
        raise ValueError("No cookies found in file.")

    session_ok = "sessionid" in cookie_dict
    uid_val    = cookie_dict.get("uid_tt", "?")[:12] + "…"
    info = (
        f"{len(cookie_dict)} cookies loaded  |  "
        f"sessionid: {'OK' if session_ok else 'MISSING'}  |  "
        f"uid: {uid_val}"
    )
    return cookie_dict, info


# ── TikTok API scraper ────────────────────────────────────────────────────────
# TikTok's internal recommend/item_list endpoint.
# This is the exact same API the website calls when you scroll the FYP.
# Sending session cookies here = logged-in FYP = your personalised feed.

FYP_API = "https://www.tiktok.com/api/recommend/item_list/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer":          "https://www.tiktok.com/foryou",
    "Accept":           "application/json, text/plain, */*",
    "Accept-Language":  "en-US,en;q=0.9",
    "Accept-Encoding":  "gzip, deflate, br",
    "Sec-Fetch-Dest":   "empty",
    "Sec-Fetch-Mode":   "cors",
    "Sec-Fetch-Site":   "same-origin",
}

def build_params(count: int = 30) -> dict:
    """Query params TikTok's FYP API expects."""
    return {
        "aid":              "1988",
        "app_name":         "tiktok_web",
        "browser_language": "en-US",
        "browser_platform": "Win32",
        "browser_name":     "Mozilla",
        "browser_version":  "5.0",
        "count":            str(count),        # videos per page (max ~30)
        "pullType":         "1",
        "itemID":           "1",
        "insertedItemList": "",
        "device_id":        str(random.randint(10**18, 10**19 - 1)),
        "history_len":      str(random.randint(3, 15)),
    }


def run_scraper(user_id: int, target: int,
                cookie_dict: dict,
                stop_event: threading.Event,
                on_progress, on_done, on_error):
    """
    Calls TikTok's FYP API in a loop using requests.
    Each call returns ~30 videos. Loop until target reached.
    No browser, no bot detection, no stalling.
    """
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        session.cookies.update(cookie_dict)

        collected = []
        seen      = set()
        page      = 0
        empty_pages = 0

        logger.info("Starting API scrape  target=%d  user=%d", target, user_id)

        while len(collected) < target:
            if stop_event.is_set():
                logger.info("Stop requested — ending at %d", len(collected))
                break

            params = build_params(count=30)

            try:
                resp = session.get(
                    FYP_API,
                    params=params,
                    timeout=20,
                    allow_redirects=False,
                )
            except requests.RequestException as e:
                logger.warning("Request failed: %s — retrying in 5s", e)
                time.sleep(5)
                continue

            logger.info("Page %d  status=%d", page, resp.status_code)

            # Redirect or non-200 = session likely expired
            if resp.status_code != 200:
                logger.warning("Non-200 response: %d", resp.status_code)
                empty_pages += 1
                if empty_pages >= 3:
                    on_error(
                        f"TikTok returned {resp.status_code} — "
                        "cookies may have expired. Upload a fresh cookie file."
                    )
                    return
                time.sleep(5)
                continue

            try:
                body = resp.json()
            except Exception:
                logger.warning("Non-JSON response on page %d", page)
                empty_pages += 1
                if empty_pages >= 3:
                    on_error("TikTok API returned non-JSON — cookies may have expired.")
                    return
                time.sleep(5)
                continue

            items = (
                body.get("itemList") or
                body.get("aweme_list") or
                body.get("item_list") or
                []
            )

            if not items:
                logger.warning("Empty itemList on page %d — body keys: %s",
                               page, list(body.keys()))
                empty_pages += 1
                if empty_pages >= 5:
                    logger.warning("5 empty pages in a row — stopping")
                    break
                time.sleep(3)
                page += 1
                continue

            empty_pages = 0  # reset on success
            new_this_page = 0

            for item in items:
                if len(collected) >= target:
                    break

                # Extract video ID
                vid_id = (
                    item.get("id") or
                    item.get("aweme_id") or
                    str(item.get("itemId", ""))
                )

                # Extract author username
                author_obj = item.get("author") or {}
                username = (
                    author_obj.get("uniqueId") or
                    author_obj.get("unique_id") or
                    author_obj.get("nickname") or
                    ""
                )

                if not vid_id or not username:
                    continue

                url = f"https://www.tiktok.com/@{username}/video/{vid_id}"
                if url in seen:
                    continue

                seen.add(url)
                collected.append(url)
                new_this_page += 1

                # Progress update every 10 links
                if len(collected) % 10 == 0 or len(collected) == target:
                    on_progress(len(collected), target)

            logger.info("Page %d: +%d new  total=%d", page, new_this_page, len(collected))

            page += 1

            # Polite delay between API calls — 1.5 to 3 seconds
            if len(collected) < target:
                time.sleep(random.uniform(1.5, 3.0))

        logger.info("Scrape done — %d links collected", len(collected))
        on_done(collected)

    except Exception as e:
        logger.exception("Scraper crashed")
        on_error(str(e))


# ── commands ──────────────────────────────────────────────────────────────────

@guard
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "TikTok FYP Scraper\n\n"
        "HOW TO USE:\n"
        "1. Export cookies from tiktok.com using Cookie-Editor extension\n"
        "   (Chrome / Firefox → install Cookie-Editor → tiktok.com → Export as JSON)\n"
        "2. Send the .json file to this bot\n"
        "3. Run /scrape to collect links\n"
        "4. Run /download to get your .txt file\n\n"
        "COMMANDS:\n"
        "/scrape [N]  — scrape N links (default 50)\n"
        "/stop        — stop current scrape\n"
        "/download    — download collected links as .txt\n"
        "/status      — check progress\n"
        "/cookies     — show cookie status\n"
        "/settings    — change target count\n"
        "/help        — this message"
    )

@guard
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)


@guard
async def cmd_cookies(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    state = user_state[uid]
    if state["cookies"]:
        await update.message.reply_text(
            "Cookies loaded!\n\n"
            f"{state['cookie_info']}\n\n"
            "Run /scrape to start."
        )
    else:
        await update.message.reply_text(
            "No cookies loaded.\n\n"
            "Send your TikTok cookie .json file to this bot.\n\n"
            "How to export:\n"
            "1. Log into tiktok.com in Chrome or Firefox\n"
            "2. Install Cookie-Editor extension\n"
            "3. Click it on tiktok.com\n"
            "4. Click Export > Export as JSON\n"
            "5. Send that file here"
        )


@guard
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    state = user_state[uid]
    if state["running"]:
        n   = len(state["results"])
        t   = state["target"]
        pct = int(n / t * 100) if t else 0
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        await update.message.reply_text(
            f"Running\n[{bar}] {pct}%\n{n} / {t} links\n\nUse /stop to cancel."
        )
    else:
        n = len(state["results"])
        await update.message.reply_text(
            f"Idle\nLast run: {n} links collected\nUse /scrape to start."
        )


@guard
async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    state = user_state[uid]
    kb = [[
        InlineKeyboardButton("25",  callback_data="target_25"),
        InlineKeyboardButton("50",  callback_data="target_50"),
        InlineKeyboardButton("100", callback_data="target_100"),
        InlineKeyboardButton("200", callback_data="target_200"),
    ]]
    await update.message.reply_text(
        f"Settings\n\nCurrent target: {state['target']} links\n\nTap to change:",
        reply_markup=InlineKeyboardMarkup(kb),
    )


@guard
async def cmd_scrape(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    state = user_state[uid]

    if state["running"]:
        await update.message.reply_text("Already running. Use /stop first.")
        return

    if not state["cookies"]:
        await update.message.reply_text(
            "No cookies loaded.\n\n"
            "Send your TikTok cookie .json file first, then run /scrape."
        )
        return

    # Allow inline target: /scrape 100
    target = state["target"]
    if ctx.args:
        try:
            target = max(1, min(int(ctx.args[0]), 500))
            state["target"] = target
        except ValueError:
            pass

    state["running"] = True
    state["results"] = []
    stop_event = threading.Event()
    state["stop_event"] = stop_event

    # Estimate: ~30 links per API call, ~2s per call → target/30 * 2 seconds
    est_sec = max(10, int(target / 30 * 2.5))
    est_str = f"~{est_sec}s" if est_sec < 60 else f"~{est_sec//60}m"

    await update.message.reply_text(
        f"Scrape started!\n\n"
        f"Target: {target} links\n"
        f"Estimated time: {est_str}\n\n"
        f"Updates every 10 links. Use /stop to cancel."
    )

    loop = asyncio.get_event_loop()

    def on_progress(count, total):
        asyncio.run_coroutine_threadsafe(
            ctx.bot.send_message(
                chat_id=uid,
                text=f"{count}/{total} links collected…",
            ),
            loop,
        )

    def on_done(results):
        state["running"] = False
        state["results"] = results
        asyncio.run_coroutine_threadsafe(
            ctx.bot.send_message(
                chat_id=uid,
                text=f"Done! {len(results)} links collected.\nUse /download to get your file.",
            ),
            loop,
        )

    def on_error(err):
        state["running"] = False
        asyncio.run_coroutine_threadsafe(
            ctx.bot.send_message(chat_id=uid, text=f"Error: {err}"),
            loop,
        )

    threading.Thread(
        target=run_scraper,
        args=(uid, target, state["cookies"], stop_event, on_progress, on_done, on_error),
        daemon=True,
    ).start()


@guard
async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    state = user_state[uid]
    if not state["running"]:
        await update.message.reply_text("Nothing is running.")
        return
    if state["stop_event"]:
        state["stop_event"].set()
    state["running"] = False
    await update.message.reply_text(
        f"Stopped.\n{len(state['results'])} links collected.\nUse /download to get them."
    )


@guard
async def cmd_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid     = update.effective_user.id
    results = user_state[uid]["results"]
    if not results:
        await update.message.reply_text("Nothing to download. Run /scrape first.")
        return
    content = "\n".join(results)
    fname   = f"fyp_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
    await update.message.reply_document(
        document=BytesIO(content.encode("utf-8")),
        filename=fname,
        caption=f"{len(results)} TikTok links",
    )


# ── cookie file upload ─────────────────────────────────────────────────────────
@guard
async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    doc = update.message.document
    if not doc:
        return

    if not (doc.file_name or "").lower().endswith(".json"):
        await update.message.reply_text(
            "Please send a .json file.\n"
            "Export from tiktok.com using Cookie-Editor > Export as JSON."
        )
        return

    msg = await update.message.reply_text("Reading cookie file…")
    try:
        file      = await ctx.bot.get_file(doc.file_id)
        raw_bytes = await file.download_as_bytearray()
        cookie_dict, info = parse_cookie_json(raw_bytes.decode("utf-8"))

        user_state[uid]["cookies"]     = cookie_dict
        user_state[uid]["cookie_info"] = info

        await msg.edit_text(
            f"Cookies loaded!\n\n"
            f"{info}\n\n"
            f"Run /scrape to start collecting your FYP links."
        )
    except (json.JSONDecodeError, ValueError) as e:
        await msg.edit_text(f"Invalid cookie file: {e}")
    except Exception as e:
        logger.exception("Cookie upload failed")
        await msg.edit_text(f"Error reading file: {e}")


# ── settings callback ──────────────────────────────────────────────────────────
async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid   = query.from_user.id
    await query.answer()
    if not is_allowed(uid):
        return
    data = query.data
    if data.startswith("target_"):
        val = int(data.split("_")[1])
        user_state[uid]["target"] = val
        await query.edit_message_text(f"Target set to {val} links.")


@guard
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me a TikTok cookie .json file to get started, or type /help."
    )


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("scrape",   cmd_scrape))
    app.add_handler(CommandHandler("stop",     cmd_stop))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("download", cmd_download))
    app.add_handler(CommandHandler("cookies",  cmd_cookies))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
