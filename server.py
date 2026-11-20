"""
Office Spy server: serves the web app and relays game actions over WebSockets.

Run:  python server.py            (then open the printed link on phones)
      python server.py --port 9000 --ip 192.168.1.25
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import re
import secrets
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from game import Game, GameError
from wordbank import WordBank

BASE = Path(__file__).parent
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
TICK_SECONDS = 0.5
SEND_TIMEOUT = 5  # seconds; a phone that can't take an update in this time is dropped (it will reconnect)
MAX_CONNECTIONS = 60  # players' phones plus shared screens; plenty for a 20-player game
MAX_MESSAGE_CHARS = 8_000  # the largest real message is a custom word pack (80 words of 30 chars)

log = logging.getLogger("office_spy")
bank = WordBank(BASE / "data")
game = Game(bank, scores_path=BASE / "data" / "scores.json")
sockets: dict[str, set[WebSocket]] = {}  # public player id -> open connections
# Secret device token -> public player id. Tokens never leave this process: other players only
# ever see public ids, so knowing someone's id doesn't let you connect as them.
identities: dict[str, str] = {}
screens: set[WebSocket] = set()  # shared TV/projector views
JOIN_URL = ""


def lan_ip() -> str:
    """Best-effort guess of this machine's IP on the local network."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is actually sent
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


async def send(ws: WebSocket, data: dict):
    try:
        await asyncio.wait_for(ws.send_json(data), SEND_TIMEOUT)
    except asyncio.TimeoutError:
        # A stuck phone must not hold everyone else up. Closing it makes the handler clean up,
        # and the phone reconnects (with its role) once its network recovers.
        try:
            await ws.close()
        except Exception:
            pass
    except Exception:
        pass  # connection already closing; cleanup happens in the handler


def connection_count() -> int:
    return len(screens) + sum(len(c) for c in sockets.values())


async def broadcast():
    """Send every connection its own state, all at once, so one slow phone doesn't delay the rest."""
    now = time.time()
    jobs = []
    for pid, conns in list(sockets.items()):
        state = game.state_for(pid, now)
        state["join_url"] = JOIN_URL
        jobs += [send(ws, state) for ws in list(conns)]
    if screens:
        state = game.state_for(None, now)
        state["join_url"] = JOIN_URL
        jobs += [send(ws, state) for ws in list(screens)]
    if jobs:
        await asyncio.gather(*jobs)


def dispatch(pid: str, msg: dict, now: float):
    g, t = game, msg.get("type")
    actions = {
        "join": lambda: g.join(pid, msg.get("name"), now),
        "leave": lambda: g.leave(pid),
        "settings": lambda: g.update_settings(pid, msg.get("settings")),
        "start": lambda: g.start(pid, now),
        "clue": lambda: g.submit_clue(pid, msg.get("text"), now),
        "skip_turn": lambda: g.skip_turn(pid, now),
        "start_vote": lambda: g.start_vote(pid, now),
        "vote": lambda: g.vote(pid, msg.get("target"), now),
        "end_vote": lambda: g.end_vote(pid, now),
        "continue": lambda: g.continue_round(pid, now),
        "spy_reveal": lambda: g.spy_reveal(pid, now),
        "guess": lambda: g.submit_guess(pid, msg.get("word")),
        "lobby": lambda: g.back_to_lobby(pid),
        "kick": lambda: g.kick(pid, msg.get("target")),
        "make_host": lambda: g.make_host(pid, msg.get("target")),
        "reset_scores": lambda: g.reset_scores(pid),
        "add_pack": lambda: g.add_pack(pid, msg.get("name"), msg.get("words")),
        "add_pairs": lambda: g.add_pairs(pid, msg.get("name"), msg.get("pairs")),
        "delete_pack": lambda: g.delete_pack(pid, msg.get("name")),
        "approve_rejoin": lambda: g.approve_rejoin(pid, msg.get("target")),
        "deny_rejoin": lambda: g.deny_rejoin(pid, msg.get("target")),
        "cancel_rejoin": lambda: g.cancel_rejoin(pid),
    }
    action = actions.get(t)
    if not action:
        raise GameError("Unknown action.")
    action()
    return t


async def ticker():
    while True:
        await asyncio.sleep(TICK_SECONDS)
        try:
            if game.tick(time.time()):
                await broadcast()
        except Exception:
            log.exception("tick failed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(ticker())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
NO_CACHE = {"Cache-Control": "no-store"}


@app.get("/")
@app.get("/screen")
async def index():
    return FileResponse(BASE / "static" / "index.html", headers=NO_CACHE)


@app.get("/qr.svg")
async def qr():
    try:
        import segno
    except ImportError:
        return Response(status_code=404)
    buf = io.BytesIO()
    segno.make(JOIN_URL, error="m").save(buf, kind="svg", scale=8, border=2, dark="#111", light="#fff")
    return Response(buf.getvalue(), media_type="image/svg+xml")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, token: str = "", screen: int = 0):
    await ws.accept()
    if connection_count() >= MAX_CONNECTIONS:
        await ws.close(code=1013)  # "try again later"
        return

    if screen:
        screens.add(ws)
        state = game.state_for(None, time.time())
        state["join_url"] = JOIN_URL
        await send(ws, state)
        try:
            while True:
                await ws.receive_text()
        except Exception:
            pass
        finally:
            screens.discard(ws)
        return

    if not TOKEN_RE.match(token):
        await ws.close(code=1008)
        return
    pid = player_id_for(token)

    sockets.setdefault(pid, set()).add(ws)
    game.set_online(pid, True, time.time())  # no-op if they haven't joined yet
    await broadcast()

    try:
        while True:
            raw = await ws.receive_text()
            if len(raw) > MAX_MESSAGE_CHARS:
                await send(ws, {"type": "error", "message": "That message is too long."})
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            try:
                action = dispatch(pid, msg, time.time())
            except GameError as e:
                await send(ws, {"type": "error", "message": str(e)})
                continue
            if action in ("leave", "kick"):
                await notify_removed()
            await broadcast()
    except Exception:
        pass  # disconnects surface as exceptions here
    finally:
        conns = sockets.get(pid)
        if conns is not None:
            conns.discard(ws)
            if not conns:
                del sockets[pid]
                game.set_online(pid, False, time.time())
                game.cancel_rejoin(pid)  # a waiting device that closes stops waiting
        await broadcast()


def player_id_for(token: str) -> str:
    """The public id for a device's secret token, creating one on first connect."""
    pid = identities.get(token)
    if pid is None:
        pid = identities[token] = secrets.token_hex(8)
    return pid


async def notify_removed():
    """Tell connections whose player was removed (left or kicked)."""
    for pid, conns in list(sockets.items()):
        if pid not in game.players:
            for ws in list(conns):
                await send(ws, {"type": "removed"})
    # Forget tokens whose player is gone and who have no open connection.
    for token, pid in list(identities.items()):
        if pid not in game.players and pid not in sockets:
            del identities[token]


def main():
    global JOIN_URL
    ap = argparse.ArgumentParser(description="Office Spy game server")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--ip", default=None, help="IP to show in the join link (auto-detected if omitted)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="  %(asctime)s  %(message)s", datefmt="%H:%M:%S")
    JOIN_URL = f"http://{args.ip or lan_ip()}:{args.port}"
    print("\n  \U0001F575  Office Spy is running!\n")
    print(f"  Players join at:  {JOIN_URL}")
    print(f"  Shared screen:    {JOIN_URL}/screen   (optional, for a TV/projector)\n")
    try:
        import segno
        segno.make(JOIN_URL, error="m").terminal(compact=True)
    except Exception:
        pass
    print("\n  Press Ctrl+C to stop.\n")
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning", ws_max_size=64 * 1024)


if __name__ == "__main__":
    main()
