# Deploying to Vercel — read this first

## TL;DR

Vercel can host the **dashboard UI**, but **not the automation engine**.

| Endpoint | Works on Vercel? | Why |
|---|---|---|
| `GET /` (dashboard) | ✅ | Static-ish render, no browser needed |
| `GET /status` | ✅ | Returns in-memory stats (will read as idle/empty) |
| `POST /start/<id>` | ❌ | Spawns a thread that launches headless Chromium — impossible in a serverless function |
| `GET /logs/<id>` (SSE) | ❌ | Open-ended stream; cut off at the function timeout |
| `GET /screenshot/<id>` | ❌ | Reads PNGs from a writable disk that doesn't exist on Vercel |

**Why the engine can't run here:** Vercel functions are stateless, have a
read-only filesystem (except a per-invocation `/tmp`), time out in seconds, and
cannot install or launch the Playwright Chromium binary. The engine in
[`core/`](../core) needs a long-lived process with a real browser.

## Recommended architecture (split deploy)

```
┌────────────────────────┐        HTTPS        ┌──────────────────────────────┐
│  Vercel                │  ───────────────▶   │  Railway / Fly.io / VPS      │
│  (this dashboard UI)   │   start/stop/status │  (Docker container)          │
│                        │ ◀───────────────    │  Flask app.py + Playwright   │
└────────────────────────┘                     │  + Chromium + Google Sheets  │
                                                └──────────────────────────────┘
```

The engine host runs the **existing** `app.py` unchanged (it already has a
[`Dockerfile`](../Dockerfile)). The UI on Vercel would point its fetch calls at
that host's public URL instead of same-origin.

> If you don't need the split, **skip Vercel entirely** and just deploy the
> Dockerfile to Railway/Fly/Render — you get the full working app in one place.

## Steps to deploy the UI to Vercel

1. Install the CLI and log in:
   ```bash
   npm i -g vercel
   vercel login
   ```
2. From the project root:
   ```bash
   vercel          # first run: link/create the project
   vercel --prod   # deploy to production
   ```
   Vercel auto-detects [`vercel.json`](../vercel.json) and the
   [`api/index.py`](index.py) entry point.
3. (Optional) Set env vars in **Vercel → Project → Settings → Environment
   Variables** — but note the engine vars (`SHEET_URL_*`, `ROTATING_PROXY_*`,
   service-account JSON) are only useful on the **engine host**, not on Vercel.

## Making the engine reachable (when you set up the worker host)

Once the engine runs on Railway/Fly/VPS, change the dashboard's `fetch()` calls
in the inline `<script>` of [`app.py`](../app.py) from same-origin paths
(`/start/...`) to the engine's base URL, e.g.:

```js
const ENGINE = "https://your-engine.up.railway.app";
fetch(ENGINE + '/start/' + key, { method: 'POST' })
```

and enable CORS on the engine (`pip install flask-cors`, `CORS(app)`).
