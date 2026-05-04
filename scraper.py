"""
TikTok FYP Scraper — core engine.

Flow:
  1. Accept cookie JSON → filter to essential auth cookies → inject into browser
  2. Navigate to /foryou  (now logged in as that account)
  3. Scroll + collect video links until target reached
  4. Return plain list of URLs

Speed target: 200 links in under 20 minutes (well within 25 min limit).
No metadata scraping — links only, straight from DOM. Fast and clean.
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
    async_playwright, Browser, BrowserContext, Page,
)

log = logging.getLogger(__name__)

# ── config ────────────────────────────────────────────────────────────────────
HEADLESS     = os.getenv("HEADLESS", "true").lower() != "false"
PAGE_TIMEOUT = int(os.getenv("PAGE_TIMEOUT_MS", "30000"))

# Scroll delay range in ms — keeps behaviour human-like, avoids rate limits
SCROLL_DELAY_MIN = int(os.getenv("SCROLL_DELAY_MIN_MS", "2500"))
SCROLL_DELAY_MAX = int(os.getenv("SCROLL_DELAY_MAX_MS", "4500"))

FYP_URL = "https://www.tiktok.com/foryou"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.6312.122 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

VIDEO_URL_RE = re.compile(
    r'https?://(?:www\.)?tiktok\.com/@[\w.]+/video/\d+',
    re.IGNORECASE
)

# ── essential TikTok auth cookie names ───────────────────────────────────────
# TikTok uses many cookies. Only these matter for authentication.
# Everything else (analytics, tracking, A/B flags) is stripped out.
ESSENTIAL_COOKIES = {
    "sessionid",
    "sessionid_ss",
    "sid_guard",
    "sid_tt",
    "uid_tt",
    "uid_tt_ss",
    "sso_uid_tt",
    "sso_uid_tt_ss",
    "ttwid",
    "tt_chain_token",
    "passport_csrf_token",
    "passport_csrf_token_default",
    "msToken",
    "tt_webid",
    "tt_webid_v2",
    "odin_tt",
    "store-idc",
    "store-country-code",
    "store-country-code-src",
    "s_v_web_id",
}


def parse_cookies(raw_json: str) -> tuple[list[dict], str]:
    """
    Parse raw cookie JSON (from Cookie-Editor, EditThisCookie, Netscape, etc).
    Returns (filtered_cookies, account_info_string).

    Accepts two formats:
      A) Array of cookie objects  →  [{name, value, domain, ...}, ...]
      B) Object keyed by name     →  {"sessionid": "abc", ...}  (rare)
    """
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {e}")

    cookies_raw: list[dict] = []

    if isinstance(data, list):
        cookies_raw = data
    elif isinstance(data, dict):
        # Convert {name: value} or {name: {value:..., domain:...}} format
        for k, v in data.items():
            if isinstance(v, dict):
                entry = v.copy()
                entry.setdefault("name", k)
                cookies_raw.append(entry)
            else:
                cookies_raw.append({"name": k, "value": str(v)})
    else:
        raise ValueError("Cookie JSON must be an array or object.")

    # ── filter to essential auth cookies only ─────────────────────────────────
    filtered: list[dict] = []
    for c in cookies_raw:
        name = c.get("name", "")
        if name not in ESSENTIAL_COOKIES:
            continue

        cookie: dict = {
            "name":   name,
            "value":  str(c.get("value", "")),
            "domain": c.get("domain", ".tiktok.com"),
            "path":   c.get("path", "/"),
        }

        # sameSite must be exactly one of these values for Playwright
        ss = c.get("sameSite", "None")
        if ss not in ("Strict", "Lax", "None"):
            ss = "None"
        cookie["sameSite"] = ss

        if c.get("httpOnly"):
            cookie["httpOnly"] = True
        if c.get("secure"):
            cookie["secure"] = True

        # Expiry — Playwright uses "expires" (float), some exporters use "expirationDate"
        exp = c.get("expires") or c.get("expirationDate")
        if exp:
            try:
                cookie["expires"] = float(exp)
            except (TypeError, ValueError):
                pass

        filtered.append(cookie)

    if not filtered:
        raise ValueError(
            "No essential auth cookies found in the file.\n"
            "Make sure you exported cookies from tiktok.com while logged in.\n"
            f"Looking for: {', '.join(sorted(ESSENTIAL_COOKIES))}"
        )

    # Try to extract account hint from uid cookies
    uid = next(
        (c["value"][:12] + "…" for c in filtered if c["name"] in ("uid_tt", "uid_tt_ss")),
        "unknown"
    )
    session_present = any(c["name"] == "sessionid" for c in filtered)
    info = (
        f"{len(filtered)} auth cookies loaded  |  "
        f"sessionid: {'✅' if session_present else '❌ MISSING'}  |  "
        f"uid: {uid}"
    )

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
        """Store parsed cookies — applied on next start()."""
        self._cookies = cookies

    # ── lifecycle ─────────────────────────────────────────────────────────────

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

        # Anti-detection
        await self._ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
        """)

        # Inject auth cookies BEFORE navigating
        if self._cookies:
            await self._ctx.add_cookies(self._cookies)
            log.info("Injected %d cookies into browser context", len(self._cookies))
        else:
            log.warning("No cookies set — browser will be logged out")

        # Block images/media to save bandwidth & speed up scrolling
        # (we only need the HTML/JS that contains video links)
        await self._ctx.route(
            "**/*.{png,jpg,jpeg,gif,webp,mp4,mp3,woff,woff2,ttf,otf}",
            lambda r: r.abort()
        )

        self._page = await self._ctx.new_page()

        log.info("Navigating to FYP…")
        await self._page.goto(
            FYP_URL,
            timeout=PAGE_TIMEOUT,
            wait_until="domcontentloaded"
        )
        # Let JS hydrate and login state settle
        await self._page.wait_for_timeout(4000)

        self._alive = True
        log.info("Browser ready — URL: %s", self._page.url)

    async def stop(self):
        self._alive = False
        try:
            if self._ctx:     await self._ctx.close()
            if self._browser: await self._browser.close()
            if self._pw:      await self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._ctx = self._page = None
        log.info("Browser stopped.")

    # ── /debug screenshot ─────────────────────────────────────────────────────

    async def debug_screenshot(self) -> tuple[bytes, str, str]:
        if not self._page:
            raise RuntimeError("Browser not started.")
        shot  = await self._page.screenshot(full_page=False)
        url   = self._page.url
        title = await self._page.title()
        return shot, url, title

    # ── FYP link collector ────────────────────────────────────────────────────

    async def collect_fyp_links(
        self,
        target: int = 200,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
    ) -> list[str]:
        """
        Collect `target` unique video URLs from TikTok's FYP.

        TikTok desktop FYP is a full-screen single-video feed — each Arrow-Down
        keypress advances exactly one video.  We:
          1. Grab all links visible right now (pre-loaded batch, usually 3-5)
          2. Press ArrowDown once  →  TikTok slides to the next video
          3. Wait for a NEW <a href="/video/..."> to appear in the DOM
             (proves the next card actually loaded — no fixed sleep guessing)
          4. Repeat until target reached
          5. Extra human-like delay every 20 videos + random micro-jitter
        """
        if not self._page:
            raise RuntimeError("Browser not started — call start() first.")

        page = self._page
        seen:  set[str]  = set()
        links: list[str] = []
        stall_count = 0
        press_count = 0

        log.info("Starting FYP collection  target=%d", target)

        # ── focus the page so keyboard events work ────────────────────────────
        await page.mouse.click(640, 400)
        await page.wait_for_timeout(1000)

        async def harvest() -> int:
            """Grab every /video/ link in DOM, return count of NEW ones."""
            hrefs: list[str] = await page.evaluate("""
                () => Array.from(
                    document.querySelectorAll('a[href*="/video/"]'),
                    a => a.href
                )
            """)
            added = 0
            for href in hrefs:
                clean = href.split("?")[0].rstrip("/")
                if VIDEO_URL_RE.match(clean) and clean not in seen:
                    seen.add(clean)
                    links.append(clean)
                    added += 1
            return added

        # Grab whatever is already rendered on first load
        await harvest()
        if progress_cb and links:
            progress_cb(min(len(links), target), target, "initial load")

        while len(links) < target:

            prev_count = len(seen)

            # Press ArrowDown — TikTok advances to next video
            await page.keyboard.press("ArrowDown")
            press_count += 1

            # Wait up to 8 s for at least one NEW video link to appear
            try:
                await page.wait_for_function(
                    f"() => document.querySelectorAll('a[href*=\"/video/\"]').length > {prev_count}",
                    timeout=8000,
                )
            except Exception:
                # No new link appeared — harvest anyway (might be cached)
                pass

            # Small jitter so behaviour isn't perfectly mechanical
            await page.wait_for_timeout(random.randint(
                SCROLL_DELAY_MIN, SCROLL_DELAY_MAX
            ))

            new = await harvest()

            if new == 0:
                stall_count += 1
                log.warning("No new links on press %d  stall=%d", press_count, stall_count)
                if stall_count >= 10:
                    log.warning("Stalled 10 times — stopping early at %d links", len(links))
                    break
            else:
                stall_count = 0

            if progress_cb:
                progress_cb(min(len(links), target), target,
                            f"video #{press_count}  +{new} new")

            # Every 20 videos take a longer human-like break
            if press_count % 20 == 0:
                pause = random.randint(3000, 5000)
                log.info("Long pause  press=%d  collected=%d  pause=%dms",
                         press_count, len(links), pause)
                await page.wait_for_timeout(pause)

        collected = links[:target]
        log.info("Collection done  links=%d  presses=%d", len(collected), press_count)
        return collected
