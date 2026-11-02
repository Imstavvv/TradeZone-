"""HTTP + WebSocket API and the live dashboard."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import math
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .desk import TradingDesk

log = logging.getLogger(__name__)
WEB = Path(__file__).parent / "web"


def _clean(o):
    """Make data JSON-safe (no NaN/inf)."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def light_state(desk: TradingDesk) -> dict:
    st = desk.state()
    st.pop("history", None)
    st.pop("equity_curve", None)
    st["equity_point"] = [st["uptime"], st["portfolio"]["equity"]]
    return st


class BotToggle(BaseModel):
    enabled: bool


class ControlBody(BaseModel):
    reason: str = "operator"


def create_app(desk: TradingDesk, run_desk: bool = True) -> FastAPI:
    token = desk.settings.api_token
    clients: set[asyncio.Queue] = set()

    def check_token(authorization: str | None = Header(default=None)) -> None:
        if token and not hmac.compare_digest(authorization or "", f"Bearer {token}"):
            raise HTTPException(401, "invalid or missing API token")

    async def broadcaster() -> None:
        while True:
            await asyncio.sleep(desk.settings.cycle_seconds)
            if not clients:
                continue
            msg = json.dumps(_clean({"type": "state", "data": light_state(desk)}), default=str)
            for q in list(clients):
                if q.qsize() > 20:  # slow client: drop stale frames
                    with contextlib.suppress(asyncio.QueueEmpty):
                        q.get_nowait()
                q.put_nowait(msg)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = [asyncio.create_task(broadcaster())]
        if run_desk:
            tasks.append(asyncio.create_task(desk.run_forever()))
        try:
            yield
        finally:
            desk.stop()
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t

    app = FastAPI(title="TradeZone", version="1.0.0", lifespan=lifespan)
    app.state.desk = desk

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(WEB / "index.html")

    @app.get("/api/health")
    async def health():
        return {"ok": True, "cycles": desk.cycles, "halted": desk.halted, "mode": desk.state()["mode"]}

    @app.get("/api/state", dependencies=[Depends(check_token)])
    async def state():
        return JSONResponse(_clean(desk.state()))

    @app.get("/api/bots", dependencies=[Depends(check_token)])
    async def bots():
        return _clean([vars(b.status()) for b in desk.bots.values()])

    @app.post("/api/bots/{name}", dependencies=[Depends(check_token)])
    async def toggle_bot(name: str, body: BotToggle):
        bot = desk.bots.get(name)
        if bot is None:
            raise HTTPException(404, f"unknown bot '{name}'")
        if bot.critical and not body.enabled and desk.portfolio.open_positions:
            raise HTTPException(
                409, f"{bot.title} is safety-critical and cannot be disabled while positions are open"
            )
        desk.set_bot_enabled(name, body.enabled)
        return {"ok": True, "bot": name, "enabled": body.enabled}

    @app.post("/api/control/{action}", dependencies=[Depends(check_token)])
    async def control(action: str, body: ControlBody | None = None):
        reason = body.reason if body else "operator"
        if action == "pause":
            desk.pause(reason)
        elif action == "resume":
            desk.resume()
        elif action == "flatten":
            return {"ok": True, "closing": desk.flatten(reason)}
        elif action == "kill":
            desk.halt(f"manual kill: {reason}", flatten=True)
        elif action == "reset":
            desk.reset_halt()
        else:
            raise HTTPException(400, "action must be pause|resume|flatten|kill|reset")
        return {"ok": True, "paused": desk.paused, "halted": desk.halted}

    @app.get("/api/journal", dependencies=[Depends(check_token)])
    async def journal(topic: str | None = None, limit: int = Query(100, ge=1, le=1000)):
        return desk.journal.query(topic, limit)

    @app.websocket("/ws")
    async def ws(socket: WebSocket, token_q: str | None = Query(default=None, alias="token")):
        if token and not hmac.compare_digest(token_q or "", token):
            await socket.close(code=4401)
            return
        await socket.accept()
        q: asyncio.Queue = asyncio.Queue()
        clients.add(q)
        try:
            await socket.send_text(json.dumps(_clean({"type": "snapshot", "data": desk.state()}), default=str))
            while True:
                await socket.send_text(await q.get())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            clients.discard(q)

    return app
