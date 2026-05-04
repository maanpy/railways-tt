# 🎵 TikTok FYP Scraper Bot

A Telegram-controlled bot that **browses TikTok's For You Page itself**, collects video links automatically, and sends you clean text results with all the stats.
Built for Railway deployment. Handles 200+ videos per batch.

---

## ✨ Features

| Feature | Details |
|---|---|
| 🚀 Bulk scraping | 1–200+ TikTok links per batch |
| ⚡ Concurrent | Configurable parallel pages (default: 3) |
| 🔄 Auto-retry | Exponential backoff on failures |
| 📸 /debug | Screenshot of what the browser currently sees |
| 📦 Clean data | Strips TikTok's bloated JSON to only useful fields |
| 📁 Auto export | Results sent as `.json` file for large batches |
| 🔒 Auth | Whitelist-based user ID protection |
| 📊 Progress bar | Live updates as links are processed |
| 🐳 Docker | Ready for Railway one-click deploy |

---

## 🤖 Bot Commands

| Command | Description |
|---|---|
| `/fyp [N]` | 🔥 **Scrape N videos from TikTok For You page** (default 20, max 200) |
| `/process <links>` | Scrape specific TikTok links you supply |
| `/debug` | Screenshot of what the browser sees right now |
| `/status` | Is the browser running? |
| `/restart` | Restart the browser (fixes CAPTCHAs / stuck sessions) |
| `/stop` | Stop the browser |
| `/help` | Show command list |

You can also **paste TikTok links directly** — no command needed, the bot detects them automatically.

---

## 📦 Output JSON Structure

Each scraped video produces a clean object:

```json
{
  "id": "7234567890123456789",
  "url": "https://www.tiktok.com/@user/video/...",
  "created_at": 1716000000,
  "description": "Check this out! #viral #fyp",
  "hashtags": ["viral", "fyp"],

  "author": {
    "id": "123456",
    "username": "cooluser",
    "nickname": "Cool User",
    "verified": false,
    "follower_count": 150000
  },

  "stats": {
    "views": 5200000,
    "likes": 430000,
    "comments": 12000,
    "shares": 8500,
    "saves": 22000
  },

  "video": {
    "duration_sec": 42,
    "width": 1080,
    "height": 1920,
    "cover": "https://...",
    "download_url": "https://...",
    "format": "mp4",
    "ratio": "9:16"
  },

  "music": {
    "id": "789",
    "title": "original sound",
    "author": "cooluser",
    "original": true,
    "cover": "https://..."
  },

  "is_ad": false,
  "duet_enabled": true,
  "stitch_enabled": true,
  "comment_enabled": true
}
```

---

## 🚀 Deploy to Railway

### Step 1 — Create the Telegram Bot
1. Open Telegram → search `@BotFather`
2. `/newbot` → follow prompts → copy the **token**

### Step 2 — Get your Telegram user ID
1. Message `@userinfobot` on Telegram → it replies with your ID

### Step 3 — Deploy on Railway
1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub**
3. Select your repo
4. Railway detects the `Dockerfile` automatically

### Step 4 — Set Environment Variables in Railway
Go to your project → **Variables** tab → add:

```
TELEGRAM_BOT_TOKEN=your_token_here
ALLOWED_USER_IDS=your_telegram_id_here
SCRAPE_CONCURRENCY=3
MAX_RETRIES=2
PAGE_TIMEOUT_MS=30000
HEADLESS=true
```

### Step 5 — Deploy
Click **Deploy** — Railway builds the Docker image and starts the bot.
Message your bot `/start` to confirm it's working!

---

## 🏠 Local Development

```bash
# Install Python deps
pip install -r requirements.txt

# Install Playwright browser
playwright install chromium

# Copy and fill env file
cp .env.example .env
# Edit .env with your values

# Run (set HEADLESS=false to see the browser)
python bot.py
```

---

## ⚙️ Configuration

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | *(required)* | Your bot token from BotFather |
| `ALLOWED_USER_IDS` | *(empty = everyone)* | Comma-separated Telegram user IDs |
| `SCRAPE_CONCURRENCY` | `3` | Parallel browser pages |
| `PAGE_TIMEOUT_MS` | `30000` | Page load timeout (ms) |
| `MAX_RETRIES` | `2` | Retries per failed link |
| `HEADLESS` | `true` | Show browser window (local dev only) |

---

## 🔧 Troubleshooting

**Bot not responding?**
- Check Railway logs for errors
- Verify `TELEGRAM_BOT_TOKEN` is correct
- Make sure your user ID is in `ALLOWED_USER_IDS`

**Scrape failing / returning no data?**
- Use `/debug` to see what the browser is actually seeing
- TikTok may be showing a CAPTCHA — use `/restart` to get a fresh session
- Try reducing `SCRAPE_CONCURRENCY` to `1` or `2`

**Bot is slow?**
- Railway free tier may be slow — upgrade for better CPU
- Increase `SCRAPE_CONCURRENCY` carefully (>5 risks bans)
- Large batches (100+) will take several minutes — that's normal
