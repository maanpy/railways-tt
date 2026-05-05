"""
TikTok FYP Scraper — Telegram Bot
Cookies uploaded as JSON file → injected into browser → scrapes FYP → sends .txt of links
"""

import os
import re
import csv
import json
import time
import random
import asyncio
import logging
import threading
from io import StringIO, BytesIO
from datetime import datetime
from collections import defaultdict

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

TIKTOK_VIDEO_RE = re.compile(r"https://www\.tiktok\.com/@[\w.]+/video/\d+")

# ── per-user state ─────────────────────────────────────────────────────────────
user_state: dict[int, dict] = defaultdict(lambda: {
    "running":    False,
    "target":     50,
    "pause":      3.0,
    "fmt":        "txt",
    "results":    [],
    "thread":     None,
    "stop_event": None,
    "cookies":    None,   # list[dict] — set when user uploads cookie file
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
def parse_cookie_json(raw: str) -> tuple[list[dict], str]:
    """
    Accept cookie JSON from Cookie-Editor or EditThisCookie.
    Normalise fields so Playwright accepts them without errors.
    Returns (cookies, info_string).
    """
    data = json.loads(raw)

    if isinstance(data, dict):
        # {name: value} or {name: {value:..., domain:...}}
        items = []
        for k, v in data.items():
            if isinstance(v, dict):
                e = v.copy(); e.setdefault("name", k); items.append(e)
            else:
                items.append({"name": k, "value": str(v)})
        data = items

    if not isinstance(data, list):
        raise ValueError("Cookie JSON must be an array or object.")

    samesite_map = {
        "strict": "Strict", "lax": "Lax",
        "none": "None", "no_restriction": "None", "unspecified": "Lax",
    }

    cleaned = []
    for c in data:
        # Drop keys Playwright rejects
        for bad in ["hostOnly", "session", "storeId", "id", "sameSite_"]:
            c.pop(bad, None)

        # Normalise sameSite
        ss = c.get("sameSite", "lax")
        c["sameSite"] = samesite_map.get(str(ss).lower(), "Lax")

        # Ensure domain points to TikTok
        if not c.get("domain", "").endswith("tiktok.com"):
            c["domain"] = ".tiktok.com"

        # Playwright wants float or absent — remove non-numeric expiry
        for key in ("expirationDate", "expires"):
            val = c.get(key)
            if val is not None:
                try:
                    c["expires"] = float(val)
                except (TypeError, ValueError):
                    pass
                c.pop("expirationDate", None)
                break

        cleaned.append(c)

    if not cleaned:
        raise ValueError("No cookies found in file.")

    session_ok = any(c.get("name") == "sessionid" for c in cleaned)
    uid_val = next((c["value"][:10] + "…" for c in cleaned if c.get("name") == "uid_tt"), "?")
    info = (
        f"{len(cleaned)} cookies  |  "
        f"sessionid: {'✅' if session_ok else '❌ MISSING'}  |  "
        f"uid_tt: {uid_val}"
    )
    return cleaned, info


# ── scraper (sync, runs in background thread) ──────────────────────────────────
def run_scraper(user_id: int, target: int, pause: float,
                cookies: list[dict],
                stop_event: threading.Event,
                on_progress, on_done, on_error):
    try:
        from playwright.sync_api import sync_playwright

        collected = []   # list of URL strings
        seen      = set()
        api_urls  = []   # filled by response interceptor

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                ]
            )
            # ── Use mobile viewport + UA so TikTok renders a normal
            #    infinite-scroll feed instead of the desktop swiper.
            #    The desktop /foryou swiper only pre-loads ~9 cards and
            #    ArrowDown stops firing new API calls after that.
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                    "Version/17.0 Mobile/15E148 Safari/604.1"
                ),
                viewport={"width": 390, "height": 844},
                is_mobile=True,
                has_touch=True,
                java_script_enabled=True,
                locale="en-US",
                timezone_id="America/New_York",
            )

            context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'plugins',   { get: () => [1,2,3,4,5] });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
                window.chrome = { runtime: {} };
            """)

            # ── Intercept TikTok API responses (Method 1) ─────────────────────
            def handle_response(response):
                try:
                    url = response.url
                    if not ("recommend/item_list" in url or
                            "aweme/v1" in url or
                            "item_list" in url or
                            "/feed" in url):
                        return
                    body = response.json()
                    items = (
                        body.get("aweme_list") or
                        body.get("itemList") or
                        body.get("item_list") or
                        []
                    )
                    for item in items:
                        aweme_id = (
                            item.get("aweme_id") or
                            item.get("id") or
                            (item.get("video") or {}).get("id")
                        )
                        author_obj = item.get("author") or {}
                        author = (
                            author_obj.get("unique_id") or
                            author_obj.get("uniqueId") or
                            author_obj.get("nickname") or
                            item.get("authorMeta", {}).get("name")
                        )
                        if aweme_id and author and author != "user":
                            api_urls.append(
                                f"https://www.tiktok.com/@{author}/video/{aweme_id}"
                            )
                        elif aweme_id:
                            api_urls.append(f"__ID__{aweme_id}")
                except Exception:
                    pass

            page = context.new_page()
            page.on("response", handle_response)

            # Inject cookies (log in as the user)
            if cookies:
                context.add_cookies(cookies)
                logger.info("Injected %d cookies", len(cookies))

            logger.info("Loading TikTok FYP (mobile)…")
            # Mobile URL gives a normal infinite-scroll feed — reliable with scrollBy
            page.goto(
                "https://www.tiktok.com/",
                wait_until="domcontentloaded",
                timeout=45_000,
            )
            time.sleep(8)

            # Dismiss popups
            for sel in [
                "button:has-text('Accept all')",
                "button:has-text('I am 18+')",
                "[data-e2e='cookie-banner-accept']",
                "[data-e2e='modal-close-inner-button']",
            ]:
                try:
                    page.click(sel, timeout=1500)
                    time.sleep(0.5)
                except Exception:
                    pass

            # Wait for first video links to appear
            try:
                page.wait_for_selector("a[href*='/video/']", timeout=15_000)
                time.sleep(2)
            except Exception:
                logger.warning("No video links found on initial load")

            scroll_count = 0
            last_count   = 0
            stuck_count  = 0
            last_scroll_y = 0

            def harvest():
                """Collect all video links currently in DOM + from intercepted API."""
                added = 0

                # From API interceptor
                pending_ids = []
                for u in list(api_urls):
                    if u.startswith("__ID__"):
                        pending_ids.append(u[6:])
                        continue
                    clean = u.split("?")[0]
                    if TIKTOK_VIDEO_RE.match(clean) and clean not in seen:
                        seen.add(clean); collected.append(clean); added += 1
                api_urls.clear()

                # Resolve bare video IDs via HTML
                if pending_ids:
                    html = page.content()
                    for vid_id in pending_ids:
                        m = re.search(
                            r'https://www\.tiktok\.com/@([\w\.]+)/video/' + vid_id, html
                        )
                        url = (
                            f"https://www.tiktok.com/@{m.group(1)}/video/{vid_id}"
                            if m else f"https://www.tiktok.com/video/{vid_id}"
                        )
                        if url not in seen:
                            seen.add(url); collected.append(url); added += 1

                # From DOM anchors
                try:
                    hrefs = page.eval_on_selector_all(
                        "a[href*='/video/']", "els => els.map(e => e.href)"
                    )
                    for href in hrefs:
                        clean = href.split("?")[0]
                        if TIKTOK_VIDEO_RE.match(clean) and clean not in seen:
                            seen.add(clean); collected.append(clean); added += 1
                except Exception:
                    pass

                return added

            # Initial harvest
            harvest()
            if collected:
                on_progress(len(collected), target)

            while len(collected) < target:
                if stop_event.is_set():
                    break

                # Scroll down by 2 full screen heights
                page.evaluate("window.scrollBy({top: window.innerHeight * 2, behavior: 'smooth'})")
                time.sleep(pause)

                # Extra wait if page hasn't moved (lazy-load trigger)
                new_y = page.evaluate("window.scrollY")
                if new_y == last_scroll_y:
                    # Page didn't scroll — try a hard scroll
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(2)
                last_scroll_y = page.evaluate("window.scrollY")

                added = harvest()

                if len(collected) >= target:
                    break

                # Progress update every 5 new links
                prev_reported = ((len(collected) - added) // 5) * 5
                curr_reported = (len(collected) // 5) * 5
                if curr_reported > prev_reported or len(collected) == target:
                    on_progress(min(len(collected), target), target)

                # Stuck detection
                if len(collected) == last_count:
                    stuck_count += 1
                    logger.warning("No new links — scroll %d stuck=%d total=%d",
                                   scroll_count, stuck_count, len(collected))
                    if stuck_count == 5:
                        # Try scrolling back up a bit then down — can unstick lazy load
                        page.evaluate("window.scrollBy({top: -400, behavior: 'smooth'})")
                        time.sleep(1)
                        page.evaluate("window.scrollBy({top: 800, behavior: 'smooth'})")
                        time.sleep(2)
                    if stuck_count == 10:
                        # Try navigating to /foryou as fallback
                        logger.info("Trying /foryou fallback…")
                        page.goto("https://www.tiktok.com/foryou",
                                  wait_until="domcontentloaded", timeout=20_000)
                        time.sleep(5)
                        stuck_count = 0
                    if stuck_count >= 20:
                        logger.warning("Stuck for 20 attempts — stopping at %d", len(collected))
                        break
                else:
                    stuck_count = 0

                last_count = len(collected)
                scroll_count += 1

                # Human-like longer pause every 30 scrolls
                if scroll_count % 30 == 0:
                    logger.info("Long pause at scroll %d…", scroll_count)
                    time.sleep(random.uniform(4, 7))

            if not collected:
                try:
                    page.screenshot(path="/tmp/debug.png", full_page=False)
                    logger.warning("0 results — debug screenshot saved")
                except Exception:
                    pass

            browser.close()

        on_done(collected)

    except Exception as e:
        logger.exception("Scraper crashed")
        on_error(str(e))


# ── commands ──────────────────────────────────────────────────────────────────

@guard
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 *TikTok FYP Scraper*\n\n"
        "*How to use:*\n"
        "1️⃣ Export cookies from tiktok.com using Cookie-Editor extension\n"
        "2️⃣ Send the .json file to this bot\n"
        "3️⃣ Run /scrape to start collecting links\n"
        "4️⃣ Run /download when done to get your .txt file\n\n"
        "*Commands:*\n"
        "/scrape — start scraping your FYP\n"
        "/stop — stop current scrape\n"
        "/download — download collected links\n"
        "/settings — change target count & speed\n"
        "/status — check progress\n"
        "/cookies — show loaded cookie status\n"
        "/debug — screenshot of what browser sees\n"
        "/help — this message",
    )


@guard
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)


@guard
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    state = user_state[uid]
    if state["running"]:
        n   = len(state["results"])
        t   = state["target"]
        pct = int(n / t * 100) if t else 0
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        msg = (
            f"🟢 *Running*\n"
            f"`[{bar}]` {pct}%\n"
            f"{n} / {t} links collected\n\n"
            f"Use /stop to cancel."
        )
    else:
        n = len(state["results"])
        msg = (
            f"⚪ *Idle*\n"
            f"Last run: {n} links\n"
            f"Use /scrape to start."
        )
    await update.message.reply_text(msg)


@guard
async def cmd_cookies(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    state = user_state[uid]
    if state["cookies"]:
        await update.message.reply_text(
            f"🍪 *Cookies loaded*\n{state['cookie_info']}\n\n"
            f"Ready to scrape. Run /scrape!",
            )
    else:
        await update.message.reply_text(
            "❌ *No cookies loaded.*\n\n"
            "Send your TikTok cookie .json file to this bot first.\n\n"
            "How to export:\n"
            "1. Log into tiktok.com in Chrome/Firefox\n"
            "2. Install Cookie-Editor extension\n"
            "3. Click it → Export → Export as JSON\n"
            "4. Send that file here",
            )


@guard
async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    state = user_state[uid]
    kb = [
        [
            InlineKeyboardButton("🎯 25",  callback_data="target_25"),
            InlineKeyboardButton("🎯 50",  callback_data="target_50"),
            InlineKeyboardButton("🎯 100", callback_data="target_100"),
            InlineKeyboardButton("🎯 200", callback_data="target_200"),
        ],
        [
            InlineKeyboardButton("⏱ 2s", callback_data="pause_2"),
            InlineKeyboardButton("⏱ 3s", callback_data="pause_3"),
            InlineKeyboardButton("⏱ 4s", callback_data="pause_4"),
            InlineKeyboardButton("⏱ 5s", callback_data="pause_5"),
        ],
    ]
    await update.message.reply_text(
        f"⚙️ *Settings*\n\n"
        f"Target: `{state['target']}` links\n"
        f"Pause:  `{state['pause']}s` per video\n\n"
        f"Tap to change:",
        reply_markup=InlineKeyboardMarkup(kb),
    )


@guard
async def cmd_scrape(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    state = user_state[uid]

    if state["running"]:
        await update.message.reply_text("⚠️ Already running. Use /stop first.")
        return

    if not state["cookies"]:
        await update.message.reply_text(
            "❌ No cookies loaded.\n\n"
            "Send your TikTok cookie .json file first, then run /scrape."
        )
        return

    target = state["target"]
    pause  = state["pause"]
    state["running"] = True
    state["results"] = []
    stop_event = threading.Event()
    state["stop_event"] = stop_event

    await update.message.reply_text(
        f"🚀 *Scrape started!*\n\n"
        f"🎯 Target: {target} links\n"
        f"⏱ Pause: {pause}s per video\n"
        f"⏳ Est. time: ~{int(target * pause / 60) + 2} min\n\n"
        f"I'll update every 10 links. Use /stop to cancel.",
    )

    loop = asyncio.get_event_loop()

    def on_progress(count, total):
        asyncio.run_coroutine_threadsafe(
            ctx.bot.send_message(
                chat_id=uid,
                text=f"📊 *{count}/{total}* links collected…",
            ),
            loop,
        )

    def on_done(results):
        state["running"] = False
        state["results"] = results
        asyncio.run_coroutine_threadsafe(
            ctx.bot.send_message(
                chat_id=uid,
                text=(
                    f"✅ *Done!*\n\n"
                    f"Collected *{len(results)}* links.\n"
                    f"Use /download to get your .txt file."
                ),
            ),
            loop,
        )

    def on_error(err):
        state["running"] = False
        asyncio.run_coroutine_threadsafe(
            ctx.bot.send_message(
                chat_id=uid,
                text=f"❌ *Error:*\n`{err}`",
            ),
            loop,
        )

    t = threading.Thread(
        target=run_scraper,
        args=(uid, target, pause, state["cookies"],
              stop_event, on_progress, on_done, on_error),
        daemon=True,
    )
    state["thread"] = t
    t.start()


@guard
async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    state = user_state[uid]
    if not state["running"]:
        await update.message.reply_text("ℹ️ Nothing is running.")
        return
    if state["stop_event"]:
        state["stop_event"].set()
    state["running"] = False
    await update.message.reply_text(
        f"⏹ Stopped.\n"
        f"{len(state['results'])} links collected so far.\n"
        f"Use /download to grab them."
    )


@guard
async def cmd_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid     = update.effective_user.id
    state   = user_state[uid]
    results = state["results"]

    if not results:
        await update.message.reply_text("📭 Nothing to download. Run /scrape first.")
        return

    # Plain .txt — one URL per line, nothing else
    content = "\n".join(results)
    fname   = f"fyp_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
    await update.message.reply_document(
        document=BytesIO(content.encode("utf-8")),
        filename=fname,
        caption=f"🔗 {len(results)} TikTok links",
    )


@guard
async def cmd_debug(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text("📸 Opening browser and taking screenshot… (~15s)")
    loop = asyncio.get_event_loop()

    def take():
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox","--disable-setuid-sandbox",
                          "--disable-dev-shm-usage","--disable-gpu"]
                )
                ctx2 = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/124.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 900},
                )
                ctx2.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                )
                cookies = user_state[uid].get("cookies")
                if cookies:
                    ctx2.add_cookies(cookies)
                pg = ctx2.new_page()
                pg.goto("https://www.tiktok.com/foryou",
                        wait_until="domcontentloaded", timeout=30_000)
                time.sleep(5)
                path = "/tmp/debug.png"
                pg.screenshot(path=path, full_page=False)
                browser.close()
            return path
        except Exception as e:
            return str(e)

    def run():
        result = take()
        async def send():
            if result.endswith(".png"):
                with open(result, "rb") as f:
                    await ctx.bot.send_photo(
                        chat_id=uid, photo=f,
                        caption=(
                            "🖥 What the browser sees.\n"
                            "If you see a login wall → cookies expired, upload a new file.\n"
                            "If you see a CAPTCHA → try again in a few minutes."
                        ),
                    )
            else:
                await ctx.bot.send_message(chat_id=uid, text=f"❌ Screenshot failed: {result}")
        asyncio.run_coroutine_threadsafe(send(), loop)

    threading.Thread(target=run, daemon=True).start()


# ── cookie file upload handler ─────────────────────────────────────────────────
@guard
async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    doc  = update.message.document
    if not doc:
        return

    if not (doc.file_name or "").lower().endswith(".json"):
        await update.message.reply_text(
            "❌ Please send a .json file.\n"
            "Export from tiktok.com using Cookie-Editor → Export as JSON."
        )
        return

    msg = await update.message.reply_text("🍪 Reading cookie file…")
    try:
        file     = await ctx.bot.get_file(doc.file_id)
        raw_bytes = await file.download_as_bytearray()
        cookies, info = parse_cookie_json(raw_bytes.decode("utf-8"))

        user_state[uid]["cookies"]     = cookies
        user_state[uid]["cookie_info"] = info

        await msg.edit_text(
            f"✅ *Cookies loaded!*\n\n"
            f"📋 {info}\n\n"
            f"Run /scrape to start collecting your FYP links.",
            )
    except (json.JSONDecodeError, ValueError) as e:
        await msg.edit_text(f"❌ Invalid cookie file:\n{e}")
    except Exception as e:
        logger.exception("Cookie upload failed")
        await msg.edit_text(f"❌ Error reading file: {e}")


# ── settings callback ──────────────────────────────────────────────────────────
async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid   = query.from_user.id
    await query.answer()
    if not is_allowed(uid):
        return
    data  = query.data
    state = user_state[uid]

    if data.startswith("target_"):
        state["target"] = int(data.split("_")[1])
        await query.edit_message_text(f"✅ Target set to {state['target']} links.")
    elif data.startswith("pause_"):
        state["pause"] = float(data.split("_")[1])
        await query.edit_message_text(f"✅ Pause set to {state['pause']}s.")


# ── text fallback ──────────────────────────────────────────────────────────────
@guard
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me a TikTok cookie .json file to get started, or type /help."
    )


# ── main ───────────────────────────────────────────────────────────────────────
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
    app.add_handler(CommandHandler("debug",    cmd_debug))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
