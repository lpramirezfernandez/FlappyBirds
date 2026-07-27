# Flappy Royale — Multiplayer Server (Python + Render)

Authoritative real-time server for the Flappy Royale game. FastAPI + WebSockets.
Handles rooms (4-char codes), lobby, ready-up, and a shared-world battle-royale
game loop that streams the same pipes and every bird to all players.

## Files
- `main.py` — the server (WebSocket endpoint at `/ws`, health check at `/health`)
- `requirements.txt` — Python dependencies
- `render.yaml` — one-click Render Blueprint

## Run locally
```bash
cd server
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```
The WebSocket is then at `ws://localhost:8000/ws`.

## Deploy to Render (2 ways)

### A) Blueprint (recommended)
1. Push this `server/` folder to a GitHub repo.
2. On Render → **New → Blueprint**, pick the repo. Render reads `render.yaml`
   and creates the web service automatically.
3. Wait for the build. Your service gets a URL like
   `https://flappy-royale.onrender.com`.

### B) Manual web service
1. Render → **New → Web Service** → connect the repo (root = `server/`).
2. Environment: **Python 3**.
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Health check path: `/health`

Render gives you HTTPS, so the WebSocket URL is **`wss://`** (secure):
```
wss://YOUR-SERVICE.onrender.com/ws
```

> Note: Render's free plan sleeps after inactivity — the first connection may
> take ~30–50s to wake the server.

## Point the game at your server
Open `Multiplayer Flappy Birds.dc.html` and, at the very top of the game's
inline script config, set your server URL (or define it globally before the
game loads):
```html
<script>window.FLAPPY_SERVER_URL = "wss://YOUR-SERVICE.onrender.com/ws";</script>
```
When this is set, **Create Room / Join Room** use the live server for real
players. When it is empty, the game runs the built-in offline mode with CPU
opponents (great for testing and solo play).

## Stats in MongoDB
Set the `MONGODB_URI` env var in Render (never commit the password). The server
records each match into database `flappyroyale`, collection `stats`, and exposes:
- `POST /stats/record` — `{name, score, win}`
- `GET /leaderboard` — top 10 by best score
- `GET /stats/{name}` — one player's totals

`GET /health` returns `"db": true` once the URI is set.

## The server also HOSTS the game (PWA / Android APK)
The `static/` folder holds the bundled game (`index.html`) plus PWA files
(`manifest.webmanifest`, `sw.js`, bird icons). Once deployed, the game is live at
your root URL:
```
https://YOUR-SERVICE.onrender.com/
```
That single URL is a full installable PWA (works offline for Solo/Practice).

### Turn it into an Android APK with PWABuilder (no coding)
1. Deploy the server so `https://YOUR-SERVICE.onrender.com/` shows the game.
2. Go to **https://www.pwabuilder.com** and paste that URL → **Start**.
3. It scores the PWA (manifest + service worker are already included). Click
   **Package for stores → Android**.
4. Choose the package options (defaults are fine) and **Download** — you get a
   signed `.apk` (for sideloading/sharing) and an `.aab` (for Google Play).
5. Install the `.apk` on any Android phone (enable "install from unknown
   sources"), or upload the `.aab` to the Play Console.

> To refresh the game after code changes: re-bundle the DC into
> `static/index.html` and redeploy; bump `CACHE` in `sw.js` so devices pick up
> the new version.
