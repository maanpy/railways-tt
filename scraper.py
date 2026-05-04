"""
TikTok FYP Scraper — Playwright-based browser automation.

Two modes:
  1. scrape_fyp(count)   → scroll the For You page, collect N video links,
                           extract metadata for each, return clean results.
  2. scrape_links(urls)  → scrape a user-supplied list of TikTok URLs.

Both return (list[VideoResult], list[FailedURL]).
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

from playwright.async_api import (
    async_playwright, Browser, BrowserContext, Page, Response
)

log = logging.getLogger(__name__)

# ── tunables ────────────────────────────────────────────────────────────────────
CONCURRENCY  = int(os.getenv("SCRAPE_CONCURRENCY", "3"))
PAGE_TIMEOUT = int(os.getenv("PAGE_TIMEOUT_MS",   "30000"))
MAX_RETRIES  = int(os.getenv("MAX_RETRIES",       "2"))
HEADLESS     = os.getenv("HEADLESS", "true").lower() != "false"

FYP_URL = "https://www.tiktok.com/foryou"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.6312.122 Safari/537.36",
]

VIDEO_URL_RE = re.compile(
    r'https://(?:www\.)?tiktok\.com/@[\w.]+/video/\d+', re.IGNORECASE
)
# ── cookie support ───────────────────────────────────────────────────────────────
# Set TIKTOK_COOKIES env var as a JSON array of cookie objects, e.g.:
# [{"name":"sessionid","value":"abc123","domain":".tiktok.com","path":"/"}]
# Export them from your browser using EditThisCookie or Cookie-Editor extension.

import json as _json

def _load_cookies() -> list[dict]:
    raw = os.getenv("TIKTOK_COOKIES", "").strip()
    if not raw:
        return []
    try:
        cookies = _json.loads(raw)
        # Normalize: ensure required fields, fix sameSite
        out = []
        for c in cookies:
            entry = {
                "name":   c.get("name", ""),
                "value":  c.get("value", ""),
                "domain": c.get("domain", ".tiktok.com"),
                "path":   c.get("path", "/"),
            }
            ss = c.get("sameSite", "None")
            if ss not in ("Strict", "Lax", "None"):
                ss = "None"
            entry["sameSite"] = ss
            if c.get("httpOnly"):  entry["httpOnly"]  = True
            if c.get("secure"):    entry["secure"]    = True
            out.append(entry)
        log.info("Loaded %d cookies from TIKTOK_COOKIES", len(out))
        return out
    except Exception as e:
        log.warning("Failed to parse TIKTOK_COOKIES: %s", e)
        return []




# ── clean data model ────────────────────────────────────────────────────────────
@dataclass
class VideoResult:
    url:          str
    video_id:     str       = ""
    author:       str       = ""        # @username
    author_name:  str       = ""        # display name
    verified:     bool      = False
    description:  str       = ""
    hashtags:     list[str] = field(default_factory=list)
    views:        int       = 0
    likes:        int       = 0
    comments:     int       = 0
    shares:       int       = 0
    saves:        int       = 0
    duration_sec: int       = 0
    music_title:  str       = ""
    music_author: str       = ""
    cover_url:    str       = ""
    created_at:   int       = 0         # unix timestamp

    def to_dict(self) -> dict:
        return asdict(self)

    def to_text(self) -> str:
        """Clean plain-text card — safe for Telegram without parse_mode."""
        tags = " ".join(f"#{t}" for t in self.hashtags[:8])
        dur  = f"{self.duration_sec//60}:{self.duration_sec%60:02d}" if self.duration_sec else "?"
        vrf  = " ✓" if self.verified else ""

        return "\n".join([
            f"🎵 @{self.author}{vrf}  {self.author_name}",
            f"📝 {self.description[:200] or '(no description)'}",
            f"🏷  {tags or '(no tags)'}",
            "",
            f"👁  Views:    {_fmt(self.views)}",
            f"❤️  Likes:    {_fmt(self.likes)}",
            f"💬 Comments: {_fmt(self.comments)}",
            f"🔁 Shares:   {_fmt(self.shares)}",
            f"🔖 Saves:    {_fmt(self.saves)}",
            "",
            f"⏱  Duration: {dur}",
            f"🎵 Music:    {self.music_title} — {self.music_author}",
            f"🔗 {self.url}",
        ])


def _fmt(n: int) -> str:
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000:     return f"{n/1_000:.1f}K"
    return str(n)


# ── scraper class ───────────────────────────────────────────────────────────────
class TikTokScraper:

    def __init__(self):
        self._pw       = None
        self._browser: Optional[Browser]       = None
        self._ctx:     Optional[BrowserContext] = None
        self._fyp_page: Optional[Page]          = None
        self._alive    = False

    def is_alive(self) -> bool:
        return self._alive and self._browser is not None

    # ── lifecycle ────────────────────────────────────────────────────────────────
    async def start(self):
        log.info("Launching browser…")
        self._pw      = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox", "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage", "--disable-gpu",
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
            Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
            window.chrome={runtime:{}};
            Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
            Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3]});
        """)
        # Inject cookies before loading FYP (needed to see personalised feed)
        cookies = _load_cookies()
        if cookies:
            await self._ctx.add_cookies(cookies)
            log.info("Cookies injected into browser context")
        else:
            log.warning("No TIKTOK_COOKIES set — FYP may show generic/empty content")

        self._fyp_page = await self._ctx.new_page()
        await self._fyp_page.goto(FYP_URL, timeout=PAGE_TIMEOUT,
                                   wait_until="domcontentloaded")
        await self._fyp_page.wait_for_timeout(3000)
        self._alive = True
        log.info("Browser ready  url=%s", self._fyp_page.url)

    async def stop(self):
        self._alive = False
        try:
            if self._ctx:     await self._ctx.close()
            if self._browser: await self._browser.close()
            if self._pw:      await self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._ctx = self._fyp_page = None

    # ── /debug ───────────────────────────────────────────────────────────────────
    async def debug_screenshot(self) -> tuple[bytes, str, str]:
        if not self._fyp_page:
            raise RuntimeError("Browser not started.")
        shot  = await self._fyp_page.screenshot(full_page=False)
        return shot, self._fyp_page.url, await self._fyp_page.title()

    # ── FYP ──────────────────────────────────────────────────────────────────────
    async def scrape_fyp(
        self,
        count: int = 20,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
    ) -> tuple[list[VideoResult], list[tuple[str, str]]]:
        """Scroll FYP, collect `count` unique video links, scrape each."""
        links = await self._collect_fyp_links(count, progress_cb)
        log.info("Collected %d links — now scraping metadata…", len(links))
        return await self._scrape_many(links, progress_cb)

    # ── user-supplied links ──────────────────────────────────────────────────────
    async def scrape_links(
        self,
        urls: list[str],
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
    ) -> tuple[list[VideoResult], list[tuple[str, str]]]:
        return await self._scrape_many(urls, progress_cb)

    # ── FYP link collector ───────────────────────────────────────────────────────
    async def _collect_fyp_links(
        self,
        target: int,
        progress_cb: Optional[Callable] = None,
    ) -> list[str]:
        page    = self._fyp_page
        seen:   set[str]  = set()
        links:  list[str] = []
        scrolls = 0
        max_sc  = target * 5  # safety

        while len(links) < target and scrolls < max_sc:
            hrefs: list[str] = await page.evaluate("""
                () => Array.from(
                    document.querySelectorAll('a[href*="/video/"]'),
                    a => a.href
                )
            """)
            for href in hrefs:
                clean = href.split("?")[0]
                if VIDEO_URL_RE.match(clean) and clean not in seen:
                    seen.add(clean)
                    links.append(clean)

            if progress_cb:
                progress_cb(min(len(links), target), target, "collecting…")

            if len(links) >= target:
                break

            await page.evaluate("window.scrollBy(0, window.innerHeight * 3)")
            await page.wait_for_timeout(random.randint(1500, 2500))
            scrolls += 1

        return links[:target]

    # ── concurrent metadata scraper ──────────────────────────────────────────────
    async def _scrape_many(
        self,
        urls: list[str],
        progress_cb: Optional[Callable] = None,
    ) -> tuple[list[VideoResult], list[tuple[str, str]]]:
        results: list[VideoResult]     = []
        errors:  list[tuple[str, str]] = []
        done = 0
        lock = asyncio.Lock()
        sem  = asyncio.Semaphore(CONCURRENCY)

        async def worker(url: str):
            nonlocal done
            async with sem:
                for attempt in range(1, MAX_RETRIES + 2):
                    try:
                        v = await self._scrape_one(url)
                        async with lock:
                            results.append(v)
                            done += 1
                            if progress_cb:
                                progress_cb(done, len(urls), url)
                        return
                    except Exception as exc:
                        log.warning("attempt %d failed %s: %s", attempt, url, exc)
                        if attempt <= MAX_RETRIES:
                            await asyncio.sleep(2 ** attempt)
                        else:
                            async with lock:
                                errors.append((url, str(exc)))
                                done += 1
                                if progress_cb:
                                    progress_cb(done, len(urls), url)

        await asyncio.gather(*[worker(u) for u in urls])
        return results, errors

    # ── single-URL scraper ───────────────────────────────────────────────────────
    async def _scrape_one(self, url: str) -> VideoResult:
        page = await self._ctx.new_page()
        await page.route(
            "**/*.{png,jpg,jpeg,gif,webp,woff,woff2,ttf,mp4,mp3}",
            lambda r: r.abort()
        )
        captured: dict = {}

        async def on_resp(resp: Response):
            try:
                if resp.status != 200: return
                u = resp.url
                if "api/item/detail" in u or "api16-normal" in u:
                    body = await resp.json()
                    if isinstance(body, dict) and body.get("itemInfo"):
                        captured.update(body)
            except Exception:
                pass

        page.on("response", lambda r: asyncio.create_task(on_resp(r)))

        try:
            await page.goto(url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)

            if captured:
                return _parse_api(captured, url)

            raw = await _extract_embedded_json(page)
            if raw:
                return _parse_embedded(raw, url)

            return await _parse_dom(page, url)
        finally:
            await page.close()


# ── JSON parsing helpers ─────────────────────────────────────────────────────────

async def _extract_embedded_json(page: Page) -> dict:
    for expr in [
        "()=>{const s=document.getElementById('SIGI_STATE');return s?JSON.parse(s.textContent):null}",
        "()=>{const s=document.getElementById('__NEXT_DATA__');return s?JSON.parse(s.textContent):null}",
        """()=>{
            for(const s of document.querySelectorAll('script[type="application/json"]')){
                try{const d=JSON.parse(s.textContent);if(d&&(d.ItemModule||d.itemInfo||d.props))return d;}catch(e){}
            }
            return null;
        }""",
    ]:
        try:
            val = await page.evaluate(expr)
            if val: return val
        except Exception:
            pass
    return {}


def _parse_api(raw: dict, url: str) -> VideoResult:
    item = raw.get("itemInfo", {}).get("itemStruct", {})
    return _build(item, url)


def _parse_embedded(raw: dict, url: str) -> VideoResult:
    item: dict = {}
    if "ItemModule" in raw:
        mods = raw["ItemModule"]
        if mods:
            item = next(iter(mods.values()))
    elif "props" in raw:
        try:
            item = (raw["props"]["pageProps"]
                    .get("itemInfo", {})
                    .get("itemStruct", {}))
        except (KeyError, AttributeError):
            pass
    elif "itemInfo" in raw:
        item = raw["itemInfo"].get("itemStruct", {})

    if not item:
        item = _deep_find(raw, lambda v: isinstance(v,dict) and "desc" in v and "stats" in v) or {}

    return _build(item, url)


async def _parse_dom(page: Page, url: str) -> VideoResult:
    v = VideoResult(url=url)
    m = re.search(r'tiktok\.com/@([\w.]+)/video/(\d+)', url)
    if m:
        v.author   = m.group(1)
        v.video_id = m.group(2)
    for prop, attr in [("og:description","description"),("og:title","author_name")]:
        val: str = await page.evaluate(
            f'()=>{{const m=document.querySelector(\'meta[property="{prop}"]\');return m?m.content:"";}}')
        if val: setattr(v, attr, val.strip())
    v.hashtags = re.findall(r'#(\w+)', v.description)
    return v


def _build(item: dict, url: str) -> VideoResult:
    if not item:
        raise ValueError("Empty item — JSON structure not recognised")

    author = item.get("author", {})
    if not isinstance(author, dict): author = {}

    stats = item.get("stats", {}) or item.get("statsV2", {})
    video = item.get("video", {})
    music = item.get("music", {})

    def _i(v):
        try: return int(v or 0)
        except: return 0

    tags: list[str] = []
    for c in item.get("challenges", []):
        if isinstance(c, dict) and c.get("title"): tags.append(c["title"])
    for t in item.get("textExtra", []):
        if isinstance(t, dict) and t.get("hashtagName"): tags.append(t["hashtagName"])
    if not tags:
        tags = re.findall(r'#(\w+)', item.get("desc", ""))
    tags = list(dict.fromkeys(tags))

    uid = author.get("uniqueId") or author.get("uid") or ""
    if not uid:
        m = re.search(r'tiktok\.com/@([\w.]+)/', url)
        if m: uid = m.group(1)

    vid_id = item.get("id") or item.get("itemId") or ""
    if not vid_id:
        m = re.search(r'/video/(\d+)', url)
        if m: vid_id = m.group(1)

    return VideoResult(
        url          = url,
        video_id     = str(vid_id),
        author       = uid,
        author_name  = author.get("nickname", ""),
        verified     = bool(author.get("verified", False)),
        description  = item.get("desc", "").strip(),
        hashtags     = tags,
        views        = _i(stats.get("playCount") or stats.get("vvCount")),
        likes        = _i(stats.get("diggCount")),
        comments     = _i(stats.get("commentCount")),
        shares       = _i(stats.get("shareCount")),
        saves        = _i(stats.get("collectCount")),
        duration_sec = _i(video.get("duration")),
        music_title  = music.get("title", ""),
        music_author = music.get("authorName", ""),
        cover_url    = video.get("cover") or video.get("dynamicCover", ""),
        created_at   = _i(item.get("createTime")),
    )


def _deep_find(obj, pred, d=0):
    if d > 8: return None
    if pred(obj): return obj
    if isinstance(obj, dict):
        for v in obj.values():
            r = _deep_find(v, pred, d+1)
            if r: return r
    elif isinstance(obj, list):
        for v in obj[:10]:
            r = _deep_find(v, pred, d+1)
            if r: return r
    return None
