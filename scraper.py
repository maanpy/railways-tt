"""
TikTok FYP Scraper

Strategy: intercept TikTok's own recommend API responses.
When TikTok loads the feed it calls /api/recommend/item_list — each response
contains a batch of ~10-20 video items with author + video ID.
We build canonical URLs from those. No DOM scraping, no keyboard tricks.

To trigger repeated API calls we use a mix of:
  - window.scrollTo() to the bottom
  - Simulating mouse wheel events (what TikTok actually listens to)
  - Waiting with network idle detection
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
from typing import Callable, Optional

from playwright.async_api import (
    async_playwright, Browser, BrowserContext, Page, Response,
)

log = logging.getLogger(__name__)

HEADLESS     = os.getenv("HEADLESS", "true").lower() != "false"
PAGE_TIMEOUT = int(os.getenv("PAGE_TIMEOUT_MS", "35000"))

FYP_URL = "https://www.tiktok.com/foryou"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.6312.122 Safari/537.36",
]

# Every URL pattern TikTok uses for feed/recommend API
FEED_PATTERNS = [
    "recommend/item_list",
    "api/item_list",
    "aweme/v1/feed",
    "aweme/v2/feed",
    "web/api/v2/feed",
    "api/feed",
]

VIDEO_URL_RE = re.compile(
    r'https?://(?:www\.)?tiktok\.com/@[\w.]+/video/\d+',
    re.IGNORECASE
)

ESSENTIAL_COOKIES = {
    "sessionid", "sessionid_ss", "sid_guard", "sid_tt",
    "uid_tt", "uid_tt_ss", "sso_uid_tt", "sso_uid_tt_ss",
    "ttwid", "tt_chain_token", "passport_csrf_token",
    "passport_csrf_token_default", "msToken",
    "tt_webid", "tt_webid_v2", "odin_tt",
    "store-idc", "store-country-code", "s_v_web_id",
}


# ── cookie parser ─────────────────────────────────────────────────────────────

def parse_cookies(raw_json: str) -> tuple[list[dict], str]:
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {e}")

    raw: list[dict] = []
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        for k, v in data.items():
            entry = v.copy() if isinstance(v, dict) else {"value": str(v)}
            entry.setdefault("name", k)
            raw.append(entry)
    else:
        raise ValueError("Cookie JSON must be an array or object.")

    filtered: list[dict] = []
    for c in raw:
        name = c.get("name", "")
        if name not in ESSENTIAL_COOKIES:
            continue
        cookie: dict = {
            "name":   name,
            "value":  str(c.get("value", "")),
            "domain": c.get("domain", ".tiktok.com"),
            "path":   c.get("path", "/"),
            "sameSite": c.get("sameSite", "None") if c.get("sameSite") in ("Strict","Lax","None") else "None",
        }
        if c.get("httpOnly"): cookie["httpOnly"] = True
        if c.get("secure"):   cookie["secure"]   = True
        exp = c.get("expires") or c.get("expirationDate")
        if exp:
            try: cookie["expires"] = float(exp)
            except: pass
        filtered.append(cookie)

    if not filtered:
        raise ValueError(
            "No essential auth cookies found.\n"
            "Export cookies from tiktok.com while logged in using Cookie-Editor."
        )

    session_ok = any(c["name"] == "sessionid" for c in filtered)
    uid = next((c["value"][:12]+"…" for c in filtered if c["name"] in ("uid_tt","uid_tt_ss")), "unknown")
    info = f"{len(filtered)} cookies  |  sessionid: {'✅' if session_ok else '❌ MISSING'}  |  uid: {uid}"
    return filtered, info


# ── scraper ───────────────────────────────────────────────────────────────────

class TikTokScraper:

    def __init__(self):
        self._pw      = None
        self._browser: Optional[Browser]       = None
        self._ctx:     Optional[BrowserContext] = None
        self._page:    Optional[Page]           = None
        self._alive   = False
        self._cookies: list[dict] = []

    def is_alive(self) -> bool:
        return self._alive and self._browser is not None

    def set_cookies(self, cookies: list[dict]):
        self._cookies = cookies

    async def start(self):
        log.info("Launching Chromium…")
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1280,800",
            ]
        )
        self._ctx = await self._browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "Accept-Language":    "en-US,en;q=0.9",
                "Accept-Encoding":    "gzip, deflate, br",
                "Sec-Ch-Ua":          '"Chromium";v="124", "Google Chrome";v="124"',
                "Sec-Ch-Ua-Mobile":   "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
            }
        )

        await self._ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver',           {get: () => undefined});
            Object.defineProperty(navigator, 'languages',           {get: () => ['en-US','en']});
            Object.defineProperty(navigator, 'plugins',             {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
            window.chrome = {runtime: {}};
        """)

        # Block only actual video/audio streams — NOT images or JS
        # Images + JS must load or TikTok won't fire API calls
        await self._ctx.route("**/*.{mp4,m4v,webm,mp3,m4a,ogg,flac}", lambda r: r.abort())

        if self._cookies:
            await self._ctx.add_cookies(self._cookies)
            log.info("Injected %d cookies", len(self._cookies))
        else:
            log.warning("No cookies set — will be logged out")

        self._page = await self._ctx.new_page()
        log.info("Navigating to FYP…")
        await self._page.goto(FYP_URL, timeout=PAGE_TIMEOUT, wait_until="networkidle")
        await self._page.wait_for_timeout(3000)
        self._alive = True
        log.info("Browser ready — %s", self._page.url)

    async def stop(self):
        self._alive = False
        try:
            if self._ctx:     await self._ctx.close()
            if self._browser: await self._browser.close()
            if self._pw:      await self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._ctx = self._page = None

    async def debug_screenshot(self) -> tuple[bytes, str, str]:
        if not self._page:
            raise RuntimeError("Browser not started.")
        return (
            await self._page.screenshot(full_page=False),
            self._page.url,
            await self._page.title(),
        )

    # ── main collector ────────────────────────────────────────────────────────

    async def collect_fyp_links(
        self,
        target: int = 200,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
    ) -> list[str]:
        if not self._page:
            raise RuntimeError("Browser not started.")

        page  = self._page
        seen:  set[str]  = set()
        links: list[str] = []
        lock  = asyncio.Lock()

        def add(url: str) -> bool:
            clean = url.split("?")[0].rstrip("/")
            if VIDEO_URL_RE.match(clean) and clean not in seen:
                seen.add(clean)
                links.append(clean)
                return True
            return False

        # ── intercept TikTok feed API ─────────────────────────────────────────
        async def on_response(resp: Response):
            try:
                if resp.status != 200:
                    return
                if not any(p in resp.url for p in FEED_PATTERNS):
                    return

                body = await resp.json()
                log.info("Feed API hit: %s  status=%s", resp.url[:80], resp.status)

                # TikTok uses different field names across versions
                items = (
                    body.get("itemList") or
                    body.get("aweme_list") or
                    body.get("items") or
                    body.get("data") or
                    []
                )

                if not isinstance(items, list):
                    log.debug("Unexpected body structure: %s", list(body.keys()))
                    return

                added = 0
                for item in items:
                    if not isinstance(item, dict):
                        continue

                    vid_id = (
                        str(item.get("id") or item.get("aweme_id") or item.get("itemId") or "")
                    )
                    author = item.get("author") or item.get("authorInfo") or {}
                    if isinstance(author, dict):
                        username = (
                            author.get("uniqueId") or
                            author.get("unique_id") or
                            author.get("uid") or ""
                        )
                    else:
                        username = str(author)

                    if vid_id and username:
                        url = f"https://www.tiktok.com/@{username}/video/{vid_id}"
                        async with lock:
                            if add(url):
                                added += 1

                if added:
                    log.info("  → added %d new links (total=%d)", added, len(links))

            except Exception as e:
                log.debug("Response parse failed: %s", e)

        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        # ── also harvest DOM anchors as fallback ──────────────────────────────
        async def harvest_dom() -> int:
            try:
                hrefs: list[str] = await page.evaluate("""
                    () => Array.from(
                        document.querySelectorAll('a[href*="/video/"]'),
                        a => a.href
                    )
                """)
                added = 0
                async with lock:
                    for h in hrefs:
                        if add(h):
                            added += 1
                return added
            except Exception:
                return 0

        # ── trigger feed by simulating mouse wheel ────────────────────────────
        # TikTok listens to wheel events, not scrollTop changes
        async def trigger_next():
            try:
                # Move mouse to center of page first
                await page.mouse.move(640, 400)
                # Dispatch a wheel event — this is what the FYP actually responds to
                await page.evaluate("""
                    () => {
                        const evt = new WheelEvent('wheel', {
                            deltaY: 800,
                            deltaMode: 0,
                            bubbles: true,
                            cancelable: true
                        });
                        document.dispatchEvent(evt);
                        // Also try on the main video container
                        const container = document.querySelector(
                            '[class*="DivSwiper"], [class*="swiper"], ' +
                            '[class*="feed"], [class*="Feed"], ' +
                            '[class*="main"], main'
                        );
                        if (container) container.dispatchEvent(evt.constructor
                            ? new WheelEvent('wheel', {deltaY:800, deltaMode:0, bubbles:true})
                            : evt
                        );
                        // Fallback scroll
                        window.scrollBy(0, 800);
                    }
                """)
            except Exception as e:
                log.debug("trigger_next error: %s", e)

        log.info("Starting collection — target=%d", target)

        # Grab initial DOM links (page loaded these on start)
        await harvest_dom()
        if progress_cb:
            progress_cb(min(len(links), target), target, "initial load")

        stall     = 0
        iteration = 0
        last_count = len(links)

        while len(links) < target:
            iteration += 1

            await trigger_next()

            # Wait 3-5s for API response + render
            await page.wait_for_timeout(random.randint(3000, 5000))

            # Also harvest DOM (catches videos TikTok pre-renders)
            await harvest_dom()

            current = len(links)
            gained  = current - last_count

            if gained == 0:
                stall += 1
                log.warning("Iteration %d — no new links (stall=%d, total=%d)",
                            iteration, stall, current)

                if stall == 4:
                    # Reload the page and re-inject cookies — sometimes fixes frozen feeds
                    log.info("Feed frozen — reloading page…")
                    await page.reload(timeout=PAGE_TIMEOUT, wait_until="networkidle")
                    await page.wait_for_timeout(4000)
                    await harvest_dom()

                if stall >= 8:
                    log.warning("Stalled 8 iterations — stopping at %d links", current)
                    break
            else:
                stall = 0
                log.info("Iteration %d — +%d new links (total=%d)", iteration, gained, current)

            last_count = len(links)

            if progress_cb:
                progress_cb(min(len(links), target), target, f"iter {iteration}")

            # Human-like longer break every 30 iterations
            if iteration % 30 == 0:
                pause = random.randint(4000, 7000)
                log.info("Long pause — %dms", pause)
                await page.wait_for_timeout(pause)

        result = links[:target]
        log.info("Collection done — %d links in %d iterations", len(result), iteration)
        return result
