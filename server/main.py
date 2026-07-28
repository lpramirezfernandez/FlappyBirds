"""
Flappy Royale — authoritative multiplayer server.
FastAPI + WebSockets. Ready to deploy on Render.

Protocol (JSON over WebSocket at /ws)
  client -> server:
    {"type":"create","name":..,"color":..}
    {"type":"join","code":..,"name":..,"color":..}
    {"type":"ready","ready":true|false}
    {"type":"start"}                      # host only
    {"type":"flap"}
    {"type":"leave"}
  server -> client:
    {"type":"created","code":..,"id":..}
    {"type":"joined","code":..,"id":..}
    {"type":"error","message":..}
    {"type":"lobby","code":..,"hostId":..,"players":[{id,name,color,ready,host}]}
    {"type":"countdown","n":3|2|1}
    {"type":"start"}
    {"type":"state","pipes":[{x,gapY}],"birds":[{id,y,alive,score}]}
    {"type":"gameover","standings":[{id,name,color,rank,score}]}

All gameplay values are NORMALIZED: y and gapY are fractions of screen height (0..1),
pipe x is a fraction of screen width. The web client multiplies by its canvas size, so
every player sees the identical world regardless of device.
"""
import asyncio, json, random, string, time, math
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState
from fastapi import Body
import os

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# ---- MongoDB stats (URI comes from the MONGODB_URI env var — never hardcode the password) ----
MONGO_URI = os.environ.get("MONGODB_URI", "")
_mongo = {"client": None, "col": None}


def stats_col():
    if not MONGO_URI:
        return None
    if _mongo["col"] is None:
        from motor.motor_asyncio import AsyncIOMotorClient
        _mongo["client"] = AsyncIOMotorClient(MONGO_URI)
        _mongo["col"] = _mongo["client"]["flappyroyale"]["stats"]
    return _mongo["col"]


async def record_result(name, score, win):
    col = stats_col()
    if col is None or not name:
        return
    try:
        await col.update_one(
            {"_id": name.lower()[:20]},
            {"$set": {"name": name[:20], "lastPlayed": time.time()},
             "$inc": {"games": 1, "wins": 1 if win else 0, "totalPipes": int(score)},
             "$max": {"bestScore": int(score)}},
            upsert=True,
        )
    except Exception as e:
        print("mongo record error:", e)

# ---- physics constants (must match the web client's ratios) ----
GRAV    = 1.7     # height / s^2
FLAP    = 0.52    # upward velocity on tap (height / s)
SPEED   = 0.38    # world scroll speed (width / s)
GAP     = 0.30    # gap size (height)
GROUND  = 0.10    # ground band (height)
BIRD_X  = 0.30    # bird horizontal position (width)
BIRD_RW = 0.045   # bird radius for horizontal test (width)
BIRD_RH = 0.032   # bird radius for vertical test (height)
PIPE_W  = 0.16    # pipe width (width)
SPACING = 0.72    # gap between pipes (width)
TICK    = 1 / 33  # server step

rooms = {}  # code -> Room


def new_code():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        c = "".join(random.choice(alphabet) for _ in range(4))
        if c not in rooms:
            return c


class Player:
    def __init__(self, pid, ws, name, color):
        self.id = pid
        self.ws = ws
        self.name = name[:10] or "Player"
        self.color = color or "#FFD23F"
        self.ready = False
        self.host = False
        # runtime
        self.y = 0.42
        self.vy = 0.0
        self.alive = True
        self.score = 0
        self.rank = 0


class Room:
    def __init__(self, code):
        self.code = code
        self.mode = "server"
        self.players = {}          # pid -> Player
        self.host_id = None
        self.phase = "lobby"       # lobby | playing | over
        self.pipes = []            # [{"x":..,"gapY":..}]
        self.task = None
        self.dead = 0
        self.total = 0

    def add(self, p):
        if not self.players:
            p.host = True
            self.host_id = p.id
        self.players[p.id] = p

    def remove(self, pid):
        self.players.pop(pid, None)
        if pid == self.host_id and self.players:
            self.host_id = next(iter(self.players))
            self.players[self.host_id].host = True

    def lobby_payload(self):
        return {
            "type": "lobby",
            "code": self.code,
            "hostId": self.host_id,
            "players": [
                {"id": p.id, "name": p.name, "color": p.color,
                 "ready": p.ready, "host": p.host}
                for p in self.players.values()
            ],
        }

    async def broadcast(self, msg):
        data = json.dumps(msg)
        dead = []
        for p in list(self.players.values()):
            try:
                if p.ws.application_state == WebSocketState.CONNECTED:
                    await p.ws.send_text(data)
            except Exception:
                dead.append(p.id)
        for pid in dead:
            self.remove(pid)

    # ---------- game loop ----------
    async def run_game(self):
        self.phase = "playing"
        # reset birds
        for p in self.players.values():
            p.y, p.vy, p.alive, p.score, p.rank = 0.42, 0.0, True, 0, 0
        self.pipes = []
        self.spawn_count = 0
        self.pipe_seq = 0
        self.game_t = 0.0
        self.dead = 0
        self.total = len(self.players)

        # countdown 3-2-1
        for n in (3, 2, 1):
            await self.broadcast({"type": "countdown", "n": n})
            await asyncio.sleep(0.9)
        await self.broadcast({"type": "start"})

        last = time.monotonic()
        next_t = last
        while self.phase == "playing":
            now = time.monotonic()
            dt = min(0.05, now - last)
            last = now
            self.step(dt)
            await self.broadcast({
                "type": "state",
                "pipes": [{"id": p["id"], "x": round(p["x"], 4), "gapY": round(p["gapY"], 4),
                           "gapH": round(p["gapH"], 4)} for p in self.pipes],
                "birds": [{"id": p.id, "y": round(p.y, 4), "alive": p.alive, "score": p.score}
                          for p in self.players.values()],
            })
            alive = [p for p in self.players.values() if p.alive]
            if (self.total > 1 and len(alive) <= 1) or (self.total == 1 and len(alive) == 0):
                if len(alive) == 1:
                    alive[0].rank = 1
                break
            next_t += TICK
            delay = next_t - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                next_t = time.monotonic()   # fell behind — resync instead of spiraling

        await self.end_game()

    def spawn_pipe(self):
        self.spawn_count = getattr(self, "spawn_count", 0) + 1
        n = self.spawn_count
        gap = max(0.205, GAP - (GAP - 0.205) * min(1.0, n / 26.0))  # shrinks with progress
        min_c = gap / 2 + 0.06
        max_c = 1 - GROUND - gap / 2 - 0.04
        gap_y = random.uniform(min_c, max_c)
        last_x = self.pipes[-1]["x"] if self.pipes else 1.0
        self.pipe_seq = getattr(self, "pipe_seq", 0) + 1
        p = {"id": self.pipe_seq, "x": max(1.0, last_x + SPACING), "gapY": gap_y, "baseGapY": gap_y,
             "gapH": gap, "passed": set(), "amp": 0.0, "osc": 0.0, "phase": 0.0,
             "minC": min_c, "maxC": max_c}
        if n >= 7 and random.random() < min(0.65, (n - 7) * 0.07):  # moving pipes later
            p["amp"] = random.uniform(0.05, 0.11)
            p["osc"] = random.uniform(1.0, 2.0)
            p["phase"] = random.uniform(0, 6.28)
        self.pipes.append(p)

    def lead_score(self):
        return max((p.score for p in self.players.values()), default=0)

    def step(self, dt):
        self.game_t = getattr(self, "game_t", 0.0) + dt
        sp = SPEED * (1 + min(0.75, self.lead_score() * 0.03))  # speeds up with progress
        # move + spawn pipes
        for p in self.pipes:
            p["x"] -= sp * dt
            if p["amp"]:
                p["gapY"] = min(p["maxC"], max(p["minC"],
                    p["baseGapY"] + math.sin(self.game_t * p["osc"] + p["phase"]) * p["amp"]))
        while not self.pipes or self.pipes[-1]["x"] < 1 - SPACING:
            self.spawn_pipe()
        self.pipes = [p for p in self.pipes if p["x"] + PIPE_W > -0.05]

        ground_y = 1 - GROUND
        for pl in self.players.values():
            if not pl.alive:
                continue
            pl.vy += GRAV * dt
            pl.y += pl.vy * dt
            if pl.y - BIRD_RH < 0:
                pl.y, pl.vy = BIRD_RH, 0.0
            if pl.y + BIRD_RH > ground_y:
                self.kill(pl); continue
            for p in self.pipes:
                gh = p["gapH"]
                within_x = BIRD_X + BIRD_RW > p["x"] and BIRD_X - BIRD_RW < p["x"] + PIPE_W
                if within_x:
                    gt, gb = p["gapY"] - gh / 2, p["gapY"] + gh / 2
                    if pl.y - BIRD_RH < gt or pl.y + BIRD_RH > gb:
                        self.kill(pl); break
            if not pl.alive:
                continue
            for p in self.pipes:
                if pl.id not in p["passed"] and p["x"] + PIPE_W < BIRD_X:
                    p["passed"].add(pl.id)
                    pl.score += 1

    def kill(self, pl):
        if not pl.alive:
            return
        pl.alive = False
        self.dead += 1
        pl.rank = self.total - self.dead + 1

    def flap(self, pid):
        p = self.players.get(pid)
        if p and p.alive and self.phase == "playing":
            p.vy = -FLAP

    async def end_game(self):
        self.phase = "over"
        order = sorted(self.players.values(),
                       key=lambda p: (p.rank or 99, -p.score))
        for i, p in enumerate(order):
            p.rank = i + 1
        for p in order:
            await record_result(p.name, p.score, p.rank == 1)
        await self.broadcast({
            "type": "gameover",
            "standings": [{"id": p.id, "name": p.name, "color": p.color,
                           "rank": p.rank, "score": p.score} for p in order],
        })


@app.get("/health")
async def health():
    return JSONResponse({"ok": True, "rooms": len(rooms), "db": bool(MONGO_URI)})


@app.post("/stats/record")
async def stats_record(payload: dict = Body(...)):
    await record_result(str(payload.get("name", "")),
                        payload.get("score", 0), bool(payload.get("win")))
    return {"ok": True}


@app.get("/leaderboard")
async def leaderboard():
    col = stats_col()
    if col is None:
        return {"players": []}
    out = []
    try:
        cur = col.find({}, {"name": 1, "bestScore": 1, "wins": 1, "games": 1}).sort("bestScore", -1).limit(10)
        async for d in cur:
            out.append({"name": d.get("name", "?"), "bestScore": d.get("bestScore", 0),
                        "wins": d.get("wins", 0), "games": d.get("games", 0)})
    except Exception as e:
        print("mongo leaderboard error:", e)
    return {"players": out}


@app.get("/stats/{name}")
async def stats_get(name: str):
    col = stats_col()
    base = {"name": name, "games": 0, "wins": 0, "bestScore": 0, "totalPipes": 0}
    if col is None:
        return base
    try:
        d = await col.find_one({"_id": name.lower()[:20]})
        if d:
            return {"name": d.get("name", name), "games": d.get("games", 0),
                    "wins": d.get("wins", 0), "bestScore": d.get("bestScore", 0),
                    "totalPipes": d.get("totalPipes", 0)}
    except Exception as e:
        print("mongo stats error:", e)
    return base


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    pid = "p" + "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(8))
    room = None
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            t = msg.get("type")

            if t == "create":
                room = Room(new_code())
                rooms[room.code] = room
                room.mode = msg.get("mode", "server")
                pl = Player(pid, ws, msg.get("name", ""), msg.get("color", ""))
                room.add(pl)
                await ws.send_text(json.dumps({"type": "created", "code": room.code, "id": pid, "mode": room.mode}))
                await room.broadcast(room.lobby_payload())

            elif t == "join":
                code = (msg.get("code") or "").upper()
                r = rooms.get(code)
                if not r:
                    await ws.send_text(json.dumps({"type": "error", "message": "Room not found"}))
                elif r.phase != "lobby":
                    await ws.send_text(json.dumps({"type": "error", "message": "Game already started"}))
                elif len(r.players) >= 4:
                    await ws.send_text(json.dumps({"type": "error", "message": "Room is full"}))
                else:
                    room = r
                    pl = Player(pid, ws, msg.get("name", ""), msg.get("color", ""))
                    room.add(pl)
                    await ws.send_text(json.dumps({"type": "joined", "code": room.code, "id": pid, "mode": getattr(room, "mode", "server")}))
                    await room.broadcast(room.lobby_payload())

            elif t == "ready" and room:
                if pid in room.players:
                    room.players[pid].ready = bool(msg.get("ready"))
                    await room.broadcast(room.lobby_payload())

            elif t == "signal" and room:
                # relay WebRTC offer/answer/ICE between two peers (tiny, connect-time only)
                target = room.players.get(msg.get("to"))
                if target:
                    try:
                        await target.ws.send_text(json.dumps({
                            "type": "signal", "from": pid, "data": msg.get("data")}))
                    except Exception:
                        pass

            elif t == "p2pstart" and room:
                # host tells the room a P2P match is starting (guests connect over datachannels)
                if pid == room.host_id:
                    await room.broadcast({"type": "p2pstart", "hostId": room.host_id})

            elif t == "start" and room:
                if getattr(room, "mode", "server") == "p2p":
                    pass  # P2P rooms run the game on the host device, not here
                elif pid == room.host_id and room.phase in ("lobby", "over"):
                    everyone_ready = all(p.ready for p in room.players.values())
                    if everyone_ready and len(room.players) >= 1:
                        if room.task:
                            room.task.cancel()
                        room.task = asyncio.create_task(room.run_game())

            elif t == "flap" and room:
                room.flap(pid)

            elif t == "leave":
                break

    except WebSocketDisconnect:
        pass
    finally:
        if room:
            room.remove(pid)
            if not room.players:
                if room.task:
                    room.task.cancel()
                rooms.pop(room.code, None)
            else:
                try:
                    await room.broadcast(room.lobby_payload())
                except Exception:
                    pass


# Optionally serve a static web client placed in ./static
if os.path.isdir(os.path.join(os.path.dirname(__file__), "static")):
    app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True), name="static")
