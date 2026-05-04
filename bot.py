"""
TikTok FYP Scraper Bot

Commands:
  /fyp [N]  — scrape N videos from For You page, send links as .txt file
  /debug    — screenshot of what browser sees
  /status   — browser running?
  /restart  — restart browser
  /stop     — stop browser
  /help     — command list
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from io import BytesIO

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from scraper import TikTokScraper, VideoResult

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

BOT_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_IDS = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()}
MAX_FYP     = int(os.getenv("MAX_FYP_COUNT", "200"))

scraper: TikTokScraper | None = None
scraper_lock = asyncio.Lock()


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


async def get_scraper() -> TikTokScraper:
    global scraper
    if scraper is None or not scraper.is_alive():
        async with scraper_lock:
            if scraper is None or not scraper.is_alive():
                scraper = TikTokScraper()
                await scraper.start()
    return scraper


@guard
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 TikTok FYP Scraper\n\n"
        "/fyp [N]   scrape N videos from For You page (default 20)\n"
        "           example: /fyp 50\n\n"
        "/debug     screenshot of the browser\n"
        "/status    is the browser running?\n"
        "/restart   restart browser (fixes CAPTCHAs)\n"
        "/stop      stop the browser\n"
        "/help      this message"
    )

@guard
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)

@guard
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if scraper and scraper.is_alive():
        await update.message.reply_text("✅ Browser: Running")
    else:
        await update.message.reply_text("💤 Browser: Stopped")

@guard
async def cmd_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global scraper
    msg = await update.message.reply_text("🔄 Restarting browser…")
    async with scraper_lock:
        if scraper:
            await scraper.stop()
        scraper = TikTokScraper()
        await scraper.start()
    await msg.edit_text("✅ Browser restarted!")

@guard
async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global scraper
    if scraper:
        await scraper.stop()
        scraper = None
        await update.message.reply_text("🛑 Browser stopped.")
    else:
        await update.message.reply_text("ℹ️ Browser was not running.")

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
        await msg.edit_text(f"❌ Screenshot failed: {e}")

@guard
async def cmd_fyp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    count = 20
    if ctx.args:
        try:
            count = max(1, min(int(ctx.args[0]), MAX_FYP))
        except ValueError:
            await update.message.reply_text("Usage: /fyp [number]   e.g. /fyp 50")
            return

    msg = await update.message.reply_text(f"🚀 Collecting {count} videos from FYP…")
    last_edit = [0.0]

    def progress(done: int, total: int, current: str = ""):
        now = time.monotonic()
        if now - last_edit[0] < 3.0:
            return
        last_edit[0] = now
        pct = int(done / max(total, 1) * 100)
        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
        asyncio.create_task(msg.edit_text(f"⏳ [{bar}] {pct}%  {done}/{total}"))

    try:
        sc = await get_scraper()
        results, errors = await sc.scrape_fyp(count=count, progress_cb=progress)

        ok  = len(results)
        bad = len(errors)
        await msg.edit_text(f"✅ Done — {ok} links collected" + (f"  |  ❌ {bad} failed" if bad else ""))

        if results:
            # Pure links, one per line, nothing else
            content = "\n".join(v.url for v in results)
            fname   = f"fyp_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
            bio     = BytesIO(content.encode("utf-8"))
            bio.name = fname
            await update.message.reply_document(
                document=bio,
                filename=fname,
                caption=f"🔗 {ok} TikTok links",
            )
        else:
            await update.message.reply_text("No links collected. Try /debug to see what the browser sees, or /restart.")

    except Exception as e:
        log.exception("FYP scrape crashed")
        await msg.edit_text(f"💥 Crashed: {e}")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("fyp",     cmd_fyp))
    app.add_handler(CommandHandler("debug",   cmd_debug))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("stop",    cmd_stop))
    log.info("Bot polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
