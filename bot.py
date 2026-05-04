"""
TikTok FYP Scraper Bot

Flow:
  1. User sends a cookie JSON file
  2. Bot validates & filters it to essential auth cookies
  3. Bot starts browser, injects cookies, navigates to /foryou
  4. Bot scrolls and collects video links
  5. Bot sends a plain .txt file — one URL per line, nothing else

Commands:
  /start  /help  — instructions
  /debug         — screenshot of what browser sees right now
  /status        — browser + session status
  /restart       — restart browser (keeps current cookies)
  /stop          — stop browser
  /clear         — clear loaded cookies (logout)
  /fyp [N]       — scrape N links from FYP (default 200)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from io import BytesIO

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters,
)

from scraper import TikTokScraper, parse_cookies

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ── env ───────────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_IDS = {
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()
}
MAX_LINKS = int(os.getenv("MAX_LINKS", "200"))

# ── state (per-process, survives restarts within same Railway deploy) ─────────
scraper: TikTokScraper | None = None
scraper_lock = asyncio.Lock()
loaded_cookies: list[dict] = []      # last successfully parsed cookies
cookie_info:    str        = ""      # human-readable summary of loaded cookies


# ── auth guard ────────────────────────────────────────────────────────────────
def allowed(update: Update) -> bool:
    if not ALLOWED_IDS:
        return True
    return (update.effective_user.id if update.effective_user else 0) in ALLOWED_IDS


def guard(fn):
    async def _w(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not allowed(update):
            await update.message.reply_text("⛔ Access denied.")
            return
        return await fn(update, ctx)
    _w.__name__ = fn.__name__
    return _w


# ── scraper accessor ──────────────────────────────────────────────────────────
async def get_scraper() -> TikTokScraper:
    global scraper
    if scraper is None or not scraper.is_alive():
        async with scraper_lock:
            if scraper is None or not scraper.is_alive():
                s = TikTokScraper()
                if loaded_cookies:
                    s.set_cookies(loaded_cookies)
                await s.start()
                scraper = s
    return scraper


# ── /start /help ──────────────────────────────────────────────────────────────
HELP_TEXT = """🎵 TikTok FYP Scraper

HOW TO USE:
1. Export your TikTok cookies as JSON
   (use Cookie-Editor or EditThisCookie browser extension)
2. Send the .json file to this bot
3. Bot confirms login and loads the session
4. Run /fyp to scrape your For You page

COMMANDS:
/fyp [N]   scrape N links from FYP  (default & max: 200)
/debug     screenshot of what the browser sees right now
/status    browser + cookie session status
/restart   restart browser (keeps current cookies)
/stop      stop the browser
/clear     remove loaded cookies (logout)
/help      this message

OUTPUT:
A plain .txt file — one TikTok link per line, nothing else."""


@guard
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


@guard
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


# ── /status ───────────────────────────────────────────────────────────────────
@guard
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    browser_status = "✅ Running" if (scraper and scraper.is_alive()) else "💤 Stopped"
    cookie_status  = f"✅ {cookie_info}" if loaded_cookies else "❌ No cookies loaded — send a cookie JSON file"
    await update.message.reply_text(
        f"Browser:  {browser_status}\n"
        f"Cookies:  {cookie_status}"
    )


# ── /clear ────────────────────────────────────────────────────────────────────
@guard
async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global loaded_cookies, cookie_info, scraper
    loaded_cookies = []
    cookie_info    = ""
    if scraper:
        await scraper.stop()
        scraper = None
    await update.message.reply_text("🗑 Cookies cleared. Browser stopped. Send a new cookie file to start fresh.")


# ── /restart ──────────────────────────────────────────────────────────────────
@guard
async def cmd_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global scraper
    if not loaded_cookies:
        await update.message.reply_text("❌ No cookies loaded. Send a cookie JSON file first.")
        return
    msg = await update.message.reply_text("🔄 Restarting browser…")
    async with scraper_lock:
        if scraper:
            await scraper.stop()
        s = TikTokScraper()
        s.set_cookies(loaded_cookies)
        await s.start()
        scraper = s
    await msg.edit_text("✅ Browser restarted with existing cookies.")


# ── /stop ─────────────────────────────────────────────────────────────────────
@guard
async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global scraper
    if scraper:
        await scraper.stop()
        scraper = None
        await update.message.reply_text("🛑 Browser stopped. Cookies still loaded — /fyp will restart it.")
    else:
        await update.message.reply_text("ℹ️ Browser was not running.")


# ── /debug ────────────────────────────────────────────────────────────────────
@guard
async def cmd_debug(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("📸 Taking screenshot…")
    try:
        sc = await get_scraper()
        shot, url, title = await sc.debug_screenshot()
        await update.message.reply_photo(
            photo=shot,
            caption=f"URL: {url}\nTitle: {title}\n{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC",
        )
        await msg.delete()
    except Exception as e:
        log.exception("debug failed")
        await msg.edit_text(f"❌ Screenshot failed: {e}")


# ── cookie file handler ───────────────────────────────────────────────────────
@guard
async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global loaded_cookies, cookie_info, scraper

    doc = update.message.document
    if not doc:
        return

    # Only accept .json files
    fname = doc.file_name or ""
    if not fname.lower().endswith(".json"):
        await update.message.reply_text(
            "❌ Please send a .json file.\n"
            "Export your TikTok cookies using Cookie-Editor or EditThisCookie browser extension."
        )
        return

    msg = await update.message.reply_text("🍪 Reading cookie file…")

    try:
        # Download file content
        file = await ctx.bot.get_file(doc.file_id)
        raw_bytes = await file.download_as_bytearray()
        raw_json  = raw_bytes.decode("utf-8")

        # Parse + filter
        cookies, info = parse_cookies(raw_json)

        # Stop existing browser (need fresh one with new cookies)
        if scraper and scraper.is_alive():
            await scraper.stop()
            scraper = None

        # Store
        loaded_cookies = cookies
        cookie_info    = info

        await msg.edit_text(
            f"✅ Cookies loaded!\n"
            f"📋 {info}\n\n"
            f"Now run /fyp to start scraping your For You page."
        )

    except ValueError as e:
        await msg.edit_text(f"❌ Cookie error:\n{e}")
    except UnicodeDecodeError:
        await msg.edit_text("❌ File is not valid UTF-8 text. Make sure it's a JSON file.")
    except Exception as e:
        log.exception("Cookie file handling failed")
        await msg.edit_text(f"❌ Unexpected error: {e}")


# ── /fyp ──────────────────────────────────────────────────────────────────────
@guard
async def cmd_fyp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global scraper

    if not loaded_cookies:
        await update.message.reply_text(
            "❌ No cookies loaded.\n\n"
            "Send your TikTok cookie JSON file first, then run /fyp."
        )
        return

    # Parse count arg
    count = MAX_LINKS
    if ctx.args:
        try:
            count = max(1, min(int(ctx.args[0]), MAX_LINKS))
        except ValueError:
            await update.message.reply_text("Usage: /fyp [number]  e.g. /fyp 100")
            return

    msg = await update.message.reply_text(
        f"🚀 Starting FYP scrape — collecting {count} links…\n"
        f"⏱ Estimated time: {_estimate_time(count)}"
    )

    last_edit = [0.0]

    def progress(done: int, total: int, detail: str = ""):
        now = time.monotonic()
        if now - last_edit[0] < 4.0:   # max one edit every 4s
            return
        last_edit[0] = now
        pct = int(done / max(total, 1) * 100)
        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
        asyncio.create_task(
            msg.edit_text(
                f"⏳ Scraping FYP…\n"
                f"[{bar}] {pct}%\n"
                f"{done} / {total} links  {detail}"
            )
        )

    try:
        sc = await get_scraper()
        links = await sc.collect_fyp_links(target=count, progress_cb=progress)

        n = len(links)
        await msg.edit_text(f"✅ Done — collected {n} links!")

        if not links:
            await update.message.reply_text(
                "⚠️ No links collected.\n"
                "Use /debug to see what the browser sees.\n"
                "If it shows a login page, your cookies may have expired — send a fresh cookie file."
            )
            return

        # Build plain text file — one URL per line, zero extra content
        content = "\n".join(links)
        fname   = f"fyp_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
        bio     = BytesIO(content.encode("utf-8"))
        bio.name = fname

        await update.message.reply_document(
            document=bio,
            filename=fname,
            caption=f"🔗 {n} TikTok links",
        )

    except Exception as e:
        log.exception("FYP scrape crashed")
        await msg.edit_text(
            f"💥 Scrape failed: {e}\n\n"
            "Try /debug to inspect the browser, or /restart to get a fresh session."
        )


def _estimate_time(count: int) -> str:
    # ~3.5s avg delay per scroll, ~5 links per scroll
    scrolls = count / 5
    seconds = scrolls * 3.5
    minutes = int(seconds / 60)
    return f"~{minutes} min" if minutes > 1 else "~1 min"


# ── catch-all text handler ────────────────────────────────────────────────────
@guard
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me a TikTok cookie JSON file to get started, or type /help."
    )


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("fyp",     cmd_fyp))
    app.add_handler(CommandHandler("debug",   cmd_debug))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("stop",    cmd_stop))
    app.add_handler(CommandHandler("clear",   cmd_clear))

    # Document handler — catches all file uploads
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    # Text fallback
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Bot polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
