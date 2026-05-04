# TikTok FYP Scraper Bot

Telegram bot that logs into TikTok using your cookies, scrolls your For You page, and sends back a plain .txt file of links — one per line.

---

## How it works

1. You export your TikTok cookies from your browser as JSON
2. You send that file to the bot
3. Bot injects the cookies into Chromium (logs in as you)
4. Bot scrolls /foryou and collects video links
5. Bot sends you a .txt file — pure links, nothing else

---

## Setup

### 1. Create a Telegram bot
- Message @BotFather on Telegram
- Send /newbot and follow prompts
- Copy the token it gives you

### 2. Get your Telegram user ID
- Message @userinfobot on Telegram
- It replies with your numeric ID like 123456789

### 3. Deploy to Railway
- Push this folder to a GitHub repo
- Go to railway.app → New Project → Deploy from GitHub → select your repo
- Railway auto-detects the Dockerfile

### 4. Set environment variables (see section below)

### 5. Export TikTok cookies
- Log into tiktok.com in Chrome or Firefox
- Install Cookie-Editor extension (free, on Chrome Web Store / Firefox Add-ons)
- Click the extension → Export → Export as JSON
- Save the file

### 6. Use the bot
- Send /start
- Upload your cookie .json file
- Run /fyp

---

## Railway Variables — exactly what to add

Go to Railway → your project → your service → Variables tab → add each one:

| Variable | Value | Notes |
|---|---|---|
| TELEGRAM_BOT_TOKEN | paste your token | from @BotFather — required |
| ALLOWED_USER_IDS | your numeric ID | from @userinfobot — recommended |
| MAX_LINKS | 200 | max links per /fyp — optional |
| SCROLL_DELAY_MIN_MS | 2500 | min ms between scrolls — optional |
| SCROLL_DELAY_MAX_MS | 4500 | max ms between scrolls — optional |
| PAGE_TIMEOUT_MS | 30000 | page load timeout ms — optional |
| HEADLESS | true | always true on Railway — optional |

Railway restarts the bot automatically after you save variables.

---

## Speed

200 links in ~15-20 minutes with default delays.
The delays are intentional — too fast and TikTok detects the bot.

Scroll timing:
- 2.5 to 4.5 seconds between each scroll
- Extra 3-6 second pause every 15 scrolls
- Each scroll yields roughly 4-8 new links

---

## Commands

| Command | What it does |
|---|---|
| /fyp [N] | Scrape N links from your FYP (default 200) |
| /debug | Screenshot of what the browser currently sees |
| /status | Browser running? Cookies loaded? |
| /restart | Restart browser, keep current cookies |
| /stop | Stop browser (cookies stay loaded) |
| /clear | Remove cookies and stop browser |
| /help | Show instructions |

---

## Cookie expiry

TikTok session cookies expire after a few weeks.
When /fyp stops working or /debug shows a login page, just export fresh cookies and send the new file to the bot.

---

## Output format

fyp_20260504_143022.txt:
```
https://www.tiktok.com/@user1/video/7234567890123456789
https://www.tiktok.com/@user2/video/7234567890123456780
https://www.tiktok.com/@user3/video/7234567890123456771
```
One URL per line. No headers, no stats, no extra text.
