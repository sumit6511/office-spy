"""End-to-end tests for the WebSocket server: a real server on a spare port, real sockets."""

import asyncio
import json
import random
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server  # noqa: E402
from game import Game  # noqa: E402
from wordbank import WordBank  # noqa: E402


@pytest.fixture
def url(tmp_path):
    for f in ("words.json", "pairs.json"):
        shutil.copy(ROOT / "data" / f, tmp_path / f)
    server.game = Game(WordBank(tmp_path), random.Random(7))
    server.sockets.clear(); server.screens.clear(); server.identities.clear()

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="error",
                                        ws_max_size=64 * 1024))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    deadline = time.time() + 10
    while not srv.started:
        assert time.time() < deadline, "server didn't start"
        time.sleep(0.02)
    yield f"ws://127.0.0.1:{port}/ws"
    srv.should_exit = True
    t.join(5)


async def latest(ws, wait=0.3):
    """The most recent state message, after the server goes quiet."""
    state = None
    try:
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), wait))
            if m.get("type") == "state":
                state = m
    except asyncio.TimeoutError:
        return state


async def table(url, n=4):
    tokens = [f"{chr(97 + i)}" * 24 for i in range(n)]
    conns = []
    for i, tok in enumerate(tokens):
        ws = await websockets.connect(f"{url}?token={tok}")
        await ws.send(json.dumps({"type": "join", "name": f"Agent{i}"}))
        conns.append(ws)
    for ws in conns:
        await latest(ws, 0.15)
    return tokens, conns


def test_public_ids_cannot_be_used_to_connect_as_someone_else(url):
    async def run():
        tokens, conns = await table(url)
        await conns[0].send(json.dumps({"type": "start"}))
        states = [await latest(ws) for ws in conns]
        assert all("role" in s for s in states)
        public_ids = [p["id"] for p in states[0]["players"]]
        assert not set(public_ids) & set(tokens)
        for pid in public_ids:  # an attacker trying every id they can see
            async with websockets.connect(f"{url}?token={pid}") as spy:
                st = await latest(spy)
                assert st["you"] is None and "role" not in st
        # The real owner reconnects to their own seat and role.
        async with websockets.connect(f"{url}?token={tokens[1]}") as back:
            st = await latest(back)
            assert st["you"]["id"] == states[1]["you"]["id"] and "role" in st
        for ws in conns:
            await ws.close()
    asyncio.run(run())


def test_bad_tokens_are_rejected(url):
    async def run():
        for bad in ("", "short", "x" * 100, "has%20spaces%20in%20it"):
            with pytest.raises((websockets.ConnectionClosed, websockets.InvalidStatus)):
                async with websockets.connect(f"{url}?token={bad}") as ws:
                    await asyncio.wait_for(ws.recv(), 2)
    asyncio.run(run())


def test_secret_word_only_reaches_its_owner(url):
    async def run():
        _, conns = await table(url, 5)
        async with websockets.connect(f"{url}?token=&screen=1") as screen:
            await conns[0].send(json.dumps({"type": "start"}))
            states = [await latest(ws) for ws in conns]
            word = server.game.word
            spies = server.game.spies
            for st in states:
                is_spy = st["you"]["id"] in spies
                assert (word in json.dumps(st)) != is_spy
            assert word not in json.dumps(await latest(screen))
        for ws in conns:
            await ws.close()
    asyncio.run(run())


async def messages(ws, wait=0.5):
    out = []
    try:
        while True:
            out.append(json.loads(await asyncio.wait_for(ws.recv(), wait)))
    except (asyncio.TimeoutError, websockets.ConnectionClosed):
        return out


def test_kicked_player_is_told_and_oversized_messages_are_refused(url):
    async def run():
        _, conns = await table(url, 3)
        await conns[2].send(json.dumps({"type": "join", "name": "Agent2"}))
        target_id = (await latest(conns[2]))["you"]["id"]
        await conns[0].send(json.dumps({"type": "kick", "target": target_id}))
        assert any(m.get("type") == "removed" for m in await messages(conns[2]))

        await latest(conns[0])
        await conns[0].send(json.dumps({"type": "add_pack", "name": "x", "words": "w," * 5000}))
        assert {"type": "error", "message": "That message is too long."} in await messages(conns[0])
        assert not server.game.bank.has_category("x")
        for ws in conns:
            await ws.close()
    asyncio.run(run())


def test_host_approves_a_new_device_mid_round(url):
    async def run():
        tokens, conns = await table(url, 4)
        await conns[0].send(json.dumps({"type": "start"}))
        before = await latest(conns[2])
        await conns[2].close()  # Agent2's phone dies
        await asyncio.sleep(0.2)

        new = await websockets.connect(f"{url}?token={'n' * 24}")
        await new.send(json.dumps({"type": "join", "name": "Agent2"}))
        waiting = await latest(new)
        assert waiting["you"] is None and waiting["rejoin_pending"] == "Agent2" and "role" not in waiting

        host = await latest(conns[0])
        [req] = host["rejoin_requests"]
        await conns[0].send(json.dumps({"type": "approve_rejoin", "target": req["id"]}))
        back = await latest(new)
        assert back["you"]["name"] == "Agent2" and back["role"] == before["role"]
        for ws in [*conns[:2], conns[3], new]:
            await ws.close()
    asyncio.run(run())
