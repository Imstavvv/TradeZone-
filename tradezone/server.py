"""HTTP + WebSocket API and the live dashboard."""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import hmac
import json
import logging
import math
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from .config import LIVE_SWITCH_WORD
from .desk import ModeSwitchError, TradingDesk
from .wallets import WalletError

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
    st.pop("events", None)  # streamed individually as they happen
    st.pop("actions", None)
    st["equity_point"] = [st["uptime"], st["portfolio"]["equity"]]
    return st


class BotToggle(BaseModel):
    enabled: bool


class ControlBody(BaseModel):
    reason: str = "operator"


class ModeBody(BaseModel):
    mode: str
    confirm: str = ""
    flatten: bool = False  # close open positions first


class LinkWalletBody(BaseModel):
    chain: str
    address: str
    tag: str | None = None
    label: str = ""
    no_tag_confirmed: bool = False


class DepositBody(BaseModel):
    chain: str
    asset: str
    amount: float
    tx_hash: str
    tag: str | None = None
    from_address: str | None = None


class CreditBody(BaseModel):
    wallet_id: str


class WithdrawBody(BaseModel):
    wallet_id: str
    asset: str
    amount: float


def create_app(desk: TradingDesk, run_desk: bool = True) -> FastAPI:
    token = desk.settings.api_token
    clients: set[asyncio.Queue] = set()

    def check_token(authorization: str | None = Header(default=None)) -> None:
        if token and not hmac.compare_digest(authorization or "", f"Bearer {token}"):
            raise HTTPException(401, "invalid or missing API token")

    QUIET_TOPICS = {"market.tick", "market.snapshot", "portfolio.update"}
    MAX_QUEUE = 400

    def push(msg: dict) -> None:
        if not clients:
            return
        text = json.dumps(_clean(msg), default=str)
        for q in list(clients):
            if q.qsize() > MAX_QUEUE:  # slow client: drop the oldest frame
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(text)

    def on_event(ev) -> None:  # every team event, the moment it happens
        if ev.topic not in QUIET_TOPICS:
            push({"type": "event", "data": ev.to_dict()})

    desk.listeners.append(on_event)
    desk.ui_listeners.append(lambda a: push(a))  # every bot action ("thought")

    async def broadcaster() -> None:
        while True:
            await asyncio.sleep(desk.settings.cycle_seconds)
            if not clients:
                continue
            push({"type": "state", "data": light_state(desk)})

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

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest():
        return FileResponse(WEB / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/icon.svg", include_in_schema=False)
    async def icon_svg():
        return FileResponse(WEB / "icon.svg", media_type="image/svg+xml")

    @app.get("/icon-{size}.png", include_in_schema=False)
    async def icon_png(size: int):
        if size not in (180, 512):
            raise HTTPException(404)
        return FileResponse(WEB / f"icon-{size}.png", media_type="image/png")

    @app.get("/api/health")
    async def health():
        return {"ok": True, "cycles": desk.cycles, "halted": desk.halted,
                "mode": "live" if desk.is_live else "paper", "feed": desk.feed.name,
                "feed_ok": not desk.feed_down, "auth_required": bool(token)}

    @app.get("/api/auth", dependencies=[Depends(check_token)])
    async def auth_check():
        return {"ok": True}

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

    @app.get("/api/bots/{name}/log", dependencies=[Depends(check_token)])
    async def bot_log(name: str):
        bot = desk.bots.get(name)
        if bot is None:
            raise HTTPException(404, f"unknown bot '{name}'")
        return _clean({**vars(bot.status()), "log": bot.full_log()})

    @app.get("/api/trades", dependencies=[Depends(check_token)])
    async def trades(mode: str | None = None, symbol: str | None = None,
                     limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
        return _clean(desk.journal.trades(mode or None, symbol or None, limit, offset))

    @app.get("/api/trades.csv")
    async def trades_csv(token_q: str | None = Query(default=None, alias="token"),
                         authorization: str | None = Header(default=None),
                         mode: str | None = None, symbol: str | None = None):
        # a download link can't send headers, so the token may also come as ?token=
        if token and not (hmac.compare_digest(token_q or "", token)
                          or hmac.compare_digest(authorization or "", f"Bearer {token}")):
            raise HTTPException(401, "invalid or missing API token")
        cols = ["time_utc", "mode", "symbol", "side", "action", "qty", "price", "notional", "fee", "pnl",
                "position_after", "reason", "order_id", "broker"]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        from datetime import datetime, timezone
        offset = 0
        while True:
            page = desk.journal.trades(mode or None, symbol or None, 1000, offset)["items"]
            for t in page:
                ts = datetime.fromtimestamp(t.get("ts", 0), timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                w.writerow([ts] + [t.get(c, "") if t.get(c) is not None else "" for c in cols[1:]])
            if len(page) < 1000:
                break
            offset += 1000
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=tradezone-trades.csv"})

    @app.get("/api/journal", dependencies=[Depends(check_token)])
    async def journal(topic: str | None = None, limit: int = Query(100, ge=1, le=1000)):
        return desk.journal.query(topic, limit)

    # -- paper / live switch -------------------------------------------------
    @app.get("/api/mode", dependencies=[Depends(check_token)])
    async def mode_check():
        return _clean(await desk.mode_check())

    @app.post("/api/mode", dependencies=[Depends(check_token)])
    async def switch_mode(body: ModeBody):
        if body.mode == "live" and body.confirm.strip().upper() != LIVE_SWITCH_WORD:
            raise HTTPException(400, f"type {LIVE_SWITCH_WORD} to confirm switching to real-money trading")
        try:
            return _clean(await desk.switch_mode(body.mode, flatten=body.flatten))
        except ModeSwitchError as exc:
            raise HTTPException(409, str(exc)) from None

    # -- wallets / funding -------------------------------------------------
    def wallet_call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs).to_dict()
        except WalletError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/api/wallets", dependencies=[Depends(check_token)])
    async def wallets():
        return _clean(desk.wallets.to_dict())

    @app.post("/api/wallets", dependencies=[Depends(check_token)])
    async def link_wallet(body: LinkWalletBody):
        w = desk.wallets
        return wallet_call(w.link, body.chain, body.address, body.tag, body.label, body.no_tag_confirmed)

    @app.delete("/api/wallets/{wallet_id}", dependencies=[Depends(check_token)])
    async def unlink_wallet(wallet_id: str):
        try:
            desk.wallets.unlink(wallet_id)
        except WalletError as exc:
            raise HTTPException(404, str(exc)) from None
        return {"ok": True}

    @app.get("/api/deposits/info", dependencies=[Depends(check_token)])
    async def deposit_info():
        return desk.wallets.deposit_info()

    @app.post("/api/deposits", dependencies=[Depends(check_token)])
    async def deposit(body: DepositBody):
        return wallet_call(desk.wallets.record_deposit, body.chain, body.asset, body.amount, body.tx_hash,
                           body.tag, body.from_address)

    @app.post("/api/deposits/{transfer_id}/credit", dependencies=[Depends(check_token)])
    async def credit(transfer_id: str, body: CreditBody):
        return wallet_call(desk.wallets.credit_held, transfer_id, body.wallet_id)

    @app.post("/api/withdrawals", dependencies=[Depends(check_token)])
    async def withdraw(body: WithdrawBody):
        return wallet_call(desk.wallets.withdraw, body.wallet_id, body.asset, body.amount)

    # -- grok ----------------------------------------------------------------
    @app.post("/api/grok/ping", dependencies=[Depends(check_token)])
    async def grok_ping():
        return await desk.bots["grok"].ping()  # type: ignore[attr-defined]

    @app.websocket("/ws")
    async def ws(socket: WebSocket, token_q: str | None = Query(default=None, alias="token")):
        await socket.accept()  # accept first: a pre-accept close reaches browsers as a bare 403/1006
        if token and not hmac.compare_digest(token_q or "", token):
            await socket.close(code=4401, reason="invalid or missing API token")
            return
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
