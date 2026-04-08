# SocialScope — Social Intelligence Platform

Multi-platform content intelligence for TikTok + YouTube.
Cross-platform keyword search, creator deep-dive, and engagement analytics.

---

## Quick Start (5 minutes)

### 1. Install backend dependencies

```bash
cd backend
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure API keys

```bash
cp .env.example .env
# Edit .env and fill in your keys (see below)
```

### 3. Start the backend

```bash
uvicorn main:app --reload --port 8000
```

### 4. Open the dashboard

Open `frontend/index.html` in your browser.
The dashboard connects to `http://localhost:8000` by default.
You can change this in the top-right URL field.

---

## API Keys

### YouTube Data API v3 (free, highly recommended)

1. Go to https://console.cloud.google.com
2. Create a project → Enable "YouTube Data API v3"
3. Credentials → Create API key
4. Paste into `.env` as `YOUTUBE_API_KEY=...`

Free quota: 10,000 units/day (~100 keyword searches/day)

### TikTok msToken (optional, improves region targeting)

1. Log into TikTok in Chrome
2. F12 → Application → Cookies → `www.tiktok.com`
3. Find the `msToken` cookie → copy its value
4. Paste into `.env` as `TIKTOK_MS_TOKEN=...`

**Without keys:** The app runs in demo mode with realistic mock data.
All UI features work — perfect for testing.

---

## Features

### Keyword Search
- Search across TikTok + YouTube simultaneously
- Filter by region, date range, minimum views
- Async job system — no timeouts on large searches
- Cross-platform results table with sorting
- 4 analytics charts: platform breakdown, top content, engagement distribution, posting frequency

### Account Deep-dive
- Look up any creator by handle
- YouTube: subscribers, total views, video count, views/sub ratio
- TikTok: requires live backend session (msToken)

### Export
- CSV: flat table, ready for Excel / Google Sheets
- JSON: full nested structure for further processing

---

## Deployment (share with team)

### Option A: Railway (easiest, free tier available)

```bash
# Install Railway CLI
npm i -g @railway/cli
railway login
railway init
railway up
```

Then update the API URL in the frontend to your Railway domain.

### Option B: Render

1. Push to GitHub
2. New Web Service → connect repo → set:
   - Build: `pip install -r backend/requirements.txt && playwright install chromium`
   - Start: `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`
3. Add env vars in Render dashboard

### Option C: VPS (DigitalOcean / Hetzner ~$6/mo)

```bash
# On your server:
git clone your-repo && cd your-repo/backend
pip install -r requirements.txt
playwright install chromium --with-deps
# Start with pm2 or systemd:
uvicorn main:app --host 0.0.0.0 --port 8000
```

Host `frontend/index.html` on Nginx or any static host.
Update the API URL in the frontend to your server's IP/domain.

---

## Project Structure

```
socialscope/
├── backend/
│   ├── main.py              # FastAPI app + all scrapers
│   ├── requirements.txt
│   └── .env.example         # API key template
└── frontend/
    └── index.html           # Full dashboard (single file, no build needed)
```

---

## Adding X/Twitter or Facebook later

The backend is modular — add a new scraper function and a new platform option:

```python
# In main.py, add:
async def search_twitter(keyword: str, count: int) -> list[dict]:
    # Use snscrape (free) or X API Basic ($100/mo)
    ...

# Then in run_search_job(), add:
elif platform == "twitter":
    r = await search_twitter(kw, req.count)
```

Then add a `<button class="platform-btn">` in the frontend for Twitter.

---

## Notes

- TikTok's unofficial API can break when TikTok updates. Run `pip install --upgrade TikTokApi` if it stops working.
- For team use, move the in-memory job store (`jobs = {}`) to Redis using `redis-py`.
- YouTube free quota resets daily at midnight Pacific time.
