"""HTTP + WebSocket API and the live dashboard."""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import json
import logging
import math
import secrets
import time
from pathlib import Path

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from .config import LIVE_SWITCH_WORD
from .desk import ModeSwitchError, TradingDesk
from .security import (
    SESSION_COOKIE,
    SESSION_TTL_S,
    SecurityConfig,
    SecurityManager,
    SecurityMiddleware,
    client_ip,
    is_local_host,
)
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


class LoginBody(BaseModel):
    token: str




class BrokerBody(BaseModel):
    key: str
    secret: str


class GoalBody(BaseModel):
    start: float
    target: float


class CodeBody(BaseModel):
    code: str




class GrokConfigBody(BaseModel):
    api_key: str | None = None
    model: str | None = None
    mode: str | None = None


class UnbanBody(BaseModel):
    ip: str


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


MIN_PUBLIC_TOKEN_LEN = 16


def create_app(desk: TradingDesk, run_desk: bool = True) -> FastAPI:
    s = desk.settings
    token = s.api_token
    # The access token is optional now (sign-in is PIN / Face ID / device pairing), but if one is
    # configured it is a master key for scripts and recovery, so it must not be guessable.
    if token and not is_local_host(s.host) and len(token) < MIN_PUBLIC_TOKEN_LEN:
        raise RuntimeError(
            f"TZ_API_TOKEN is set but too short (at least {MIN_PUBLIC_TOKEN_LEN} random characters). "
            "Remove it (it's optional) or generate one with: "
            "python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    sec = SecurityManager(SecurityConfig(
        token=token, allowed_ips=s.allowed_ips, requests_per_min=s.rate_limit_per_min,
        login_failures_before_ban=s.login_failures_before_ban, ban_s=s.ban_minutes * 60,
        public=not is_local_host(s.host),
    ), desk.journal)
    desk.security = sec  # type: ignore[attr-defined]
    clients: set[asyncio.Queue] = set()

    def agent(request) -> str:
        return request.headers.get("user-agent", "")[:200]

    def auth_state(request) -> tuple[bool, str | None]:
        """(signed in?, session hash). A device's session cookie, or 'Authorization: Bearer <token>'.

        A wrong bearer token counts as a failed login (and repeated failures ban the IP).
        """
        if not sec.auth_required:
            return True, None
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            if sec.check_token(auth[7:]):
                return True, None
            sec.login_failed(client_ip(request.scope), agent(request))
            return False, None
        sid = request.cookies.get(SESSION_COOKIE)
        if not sec.session_valid(sid, client_ip(request.scope)):
            return False, None
        return True, sec.sid_hash(sid)  # type: ignore[arg-type]

    def check_token(request: Request) -> None:
        if not auth_state(request)[0]:
            raise HTTPException(401, "sign in required")

    def start_session(request: Request, response: Response, how: str) -> str:
        """Trust this device for a year (sliding). Returns the session hash."""
        ip = client_ip(request.scope)
        sid = sec.create_session(ip, agent(request))
        response.set_cookie(SESSION_COOKIE, sid, max_age=int(SESSION_TTL_S), httponly=True,
                            secure=request.url.scheme == "https", samesite="lax", path="/")
        sec.claim_owner()
        sec.audit("login", ip, how, agent(request))
        return sec.sid_hash(sid)

    def audit(request: Request, what: str, level: str = "info") -> None:
        sec.audit("action", client_ip(request.scope), what, agent(request), level)

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

    # no public API docs: they would hand attackers a map of every control endpoint
    app = FastAPI(title="TradeZone", version="1.0.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.desk = desk
    app.add_middleware(SecurityMiddleware, sec=sec)
    page = (WEB / "index.html").read_text()

    @app.get("/", include_in_schema=False)
    async def index(request: Request):
        # per-request nonce: only our own inline script may run (CSP blocks injected scripts)
        nonce = secrets.token_urlsafe(18)
        request.state.csp_nonce = nonce
        return HTMLResponse(page.replace("<script>", f'<script nonce="{nonce}">'),
                            headers={"cache-control": "no-store"})

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
        return {"ok": True}  # deliberately reveals nothing about the desk

    # -- sign in / sessions / security ----------------------------------------
    @app.post("/api/login")
    async def login(body: LoginBody, request: Request, response: Response):
        ip = client_ip(request.scope)
        if not sec.auth_required:
            return {"ok": True, "auth_required": False}
        if not sec.check_token(body.token.strip()):
            sec.login_failed(ip, agent(request))
            raise HTTPException(401, "that access token is not correct")
        start_session(request, response, "signed in with the API token")
        return {"ok": True, "auth_required": True}

    # -- simple sign-in: a code from the server logs, or from a device that's already in ----------
    @app.get("/api/setup/status")
    async def setup_status():
        return {"owner": sec.owner_exists, "auth_required": sec.auth_required}

    @app.post("/api/signin/code")
    async def signin_code(body: CodeBody, request: Request, response: Response):
        """One box for both: the 8-digit code from the server logs, or a 6-digit code from a device
        that's already logged in. Either logs this device in for a year."""
        digits = "".join(c for c in body.code if c.isdigit())
        ip = client_ip(request.scope)
        if len(digits) == 6:
            if not sec.redeem_pairing_code(digits):
                sec.login_failed(ip, agent(request))
                raise HTTPException(401, "That code didn't work. Codes from your other phone last 5 minutes - make a new one there.")
            start_session(request, response, "logged in with a code from another device")
            return {"ok": True}
        if not sec.check_recovery(body.code):
            sec.login_failed(ip, agent(request))
            raise HTTPException(401, "That code isn't right. Copy the newest TradeZone login code from your Render logs.")
        start_session(request, response, "logged in with the login code from the server logs")
        return {"ok": True}

    @app.post("/api/devices/pair", dependencies=[Depends(check_token)])
    async def pair_device(request: Request):
        audit(request, "created a code to add a device", "warning")
        return {"code": sec.create_pairing_code(), "expires_in": sec.PAIRING_TTL_S}

    @app.post("/api/logout")
    async def logout(request: Request, response: Response):
        sec.revoke(request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE, path="/")
        sec.audit("logout", client_ip(request.scope), "signed out", agent(request))
        return {"ok": True}

    @app.get("/api/auth")
    async def auth_check(request: Request):
        if not auth_state(request)[0]:
            raise HTTPException(401, "sign in required")
        return {"ok": True, "auth_required": sec.auth_required}

    @app.get("/api/security", dependencies=[Depends(check_token)])
    async def security_summary():
        return _clean(sec.summary())

    @app.post("/api/security/unban", dependencies=[Depends(check_token)])
    async def unban(body: UnbanBody, request: Request):
        audit(request, f"unbanned {body.ip}", "warning")
        return {"ok": sec.unban(body.ip.strip())}

    @app.post("/api/security/signout-all", dependencies=[Depends(check_token)])
    async def signout_all(request: Request, response: Response):
        n = sec.revoke_all(client_ip(request.scope))
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True, "revoked": n}

    @app.get("/api/state", dependencies=[Depends(check_token)])
    async def state():
        return JSONResponse(_clean(desk.state()))

    @app.get("/api/bots", dependencies=[Depends(check_token)])
    async def bots():
        return _clean([vars(b.status()) for b in desk.bots.values()])

    @app.post("/api/bots/{name}", dependencies=[Depends(check_token)])
    async def toggle_bot(name: str, body: BotToggle, request: Request):
        bot = desk.bots.get(name)
        if bot is None:
            raise HTTPException(404, f"unknown bot '{name}'")
        if bot.critical and not body.enabled and desk.portfolio.open_positions:
            raise HTTPException(
                409, f"{bot.title} is safety-critical and cannot be disabled while positions are open"
            )
        desk.set_bot_enabled(name, body.enabled)
        audit(request, f"{'enabled' if body.enabled else 'disabled'} bot {name}", "info" if body.enabled else "warning")
        return {"ok": True, "bot": name, "enabled": body.enabled}

    @app.post("/api/control/{action}", dependencies=[Depends(check_token)])
    async def control(action: str, request: Request, body: ControlBody | None = None):
        reason = body.reason if body else "operator"
        audit(request, f"control: {action}", "warning" if action in ("kill", "reset", "flatten") else "info")
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

    @app.get("/api/trades.csv", dependencies=[Depends(check_token)])
    async def trades_csv(mode: str | None = None, symbol: str | None = None):
        # the session cookie authorises the download link; tokens never go in URLs
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
    async def switch_mode(body: ModeBody, request: Request):
        if body.mode == "live" and body.confirm.strip().upper() != LIVE_SWITCH_WORD:
            raise HTTPException(400, f"type {LIVE_SWITCH_WORD} to confirm switching to real-money trading")
        audit(request, f"switch to {body.mode.upper()}", "critical" if body.mode == "live" else "warning")
        try:
            return _clean(await desk.switch_mode(body.mode, flatten=body.flatten))
        except ModeSwitchError as exc:
            raise HTTPException(409, str(exc)) from None

    # -- the goal ------------------------------------------------------------
    @app.post("/api/goal", dependencies=[Depends(check_token)])
    async def set_goal(body: GoalBody, request: Request):
        audit(request, f"goal set: ${body.start:,.2f} -> ${body.target:,.2f}")
        try:
            return desk.set_goal(body.start, body.target)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

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
    async def link_wallet(body: LinkWalletBody, request: Request):
        audit(request, f"link {body.chain} wallet {body.address[:10]}…", "warning")
        w = desk.wallets
        return wallet_call(w.link, body.chain, body.address, body.tag, body.label, body.no_tag_confirmed)

    @app.get("/api/wallets/check", dependencies=[Depends(check_token)])
    async def check_wallet_address(address: str = Query("", max_length=200)):
        return desk.wallets.check(address)

    @app.post("/api/wallets/{wallet_id}/received", dependencies=[Depends(check_token)])
    async def wallet_received(wallet_id: str, request: Request):
        audit(request, f"confirmed a test send arrived at wallet {wallet_id}", "warning")
        return wallet_call(desk.wallets.confirm_received, wallet_id)

    @app.delete("/api/wallets/{wallet_id}", dependencies=[Depends(check_token)])
    async def unlink_wallet(wallet_id: str, request: Request):
        audit(request, f"unlink wallet {wallet_id}", "warning")
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
    async def credit(transfer_id: str, body: CreditBody, request: Request):
        audit(request, f"credit held deposit {transfer_id}", "warning")
        return wallet_call(desk.wallets.credit_held, transfer_id, body.wallet_id)

    @app.post("/api/withdrawals", dependencies=[Depends(check_token)])
    async def withdraw(body: WithdrawBody, request: Request):
        audit(request, f"withdraw {body.amount} {body.asset} to {body.wallet_id}", "critical")
        return wallet_call(desk.wallets.withdraw, body.wallet_id, body.asset, body.amount)

    # -- grok ----------------------------------------------------------------
    # -- linked accounts (real-money broker) --------------------------------------
    @app.get("/api/accounts", dependencies=[Depends(check_token)])
    async def accounts():
        acct = None
        if desk.live_broker is not None:
            acct = await desk.live_broker.account()
        return _clean({"broker": {**desk.broker_info(), "account": acct}, "grok": desk.bots["grok"].info()})

    @app.post("/api/accounts/broker", dependencies=[Depends(check_token)])
    async def link_broker(body: BrokerBody, request: Request):
        try:
            acct = await desk.link_broker(body.key, body.secret)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        audit(request, "linked a real-money Alpaca account", "warning")
        return _clean({"ok": True, "account": acct, **desk.broker_info()})

    @app.delete("/api/accounts/broker", dependencies=[Depends(check_token)])
    async def unlink_broker(request: Request):
        try:
            await desk.unlink_broker()
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        audit(request, "unlinked the real-money Alpaca account", "warning")
        return {"ok": True}

    @app.post("/api/grok/ping", dependencies=[Depends(check_token)])
    async def grok_ping():
        res = await desk.bots["grok"].ping()  # type: ignore[attr-defined]
        return {**res, **desk.bots["grok"].info()}  # type: ignore[attr-defined]

    @app.get("/api/grok", dependencies=[Depends(check_token)])
    async def grok_info():
        return desk.bots["grok"].info()  # type: ignore[attr-defined]

    @app.post("/api/grok/config", dependencies=[Depends(check_token)])
    async def grok_config(body: GrokConfigBody, request: Request):
        if body.mode and body.mode not in ("veto", "advisory", "off"):
            raise HTTPException(400, "mode must be veto, advisory or off")
        if body.api_key is not None and body.api_key.strip() and len(body.api_key.strip()) < 20:
            raise HTTPException(400, "that doesn't look like a full xAI API key - copy it again from console.x.ai")
        grok = desk.bots["grok"]
        grok.save(body.api_key, body.model, body.mode)  # type: ignore[attr-defined]
        audit(request, f"Grok settings changed (mode {grok.mode}{', new key' if body.api_key else ''})", "warning")
        res = await grok.ping() if grok.client else {"ok": False, "error": "Grok is off"}  # type: ignore[attr-defined]
        return {**res, **grok.info()}  # type: ignore[attr-defined]

    @app.delete("/api/grok/config", dependencies=[Depends(check_token)])
    async def grok_forget(request: Request):
        desk.bots["grok"].forget()  # type: ignore[attr-defined]
        audit(request, "Grok key removed from dashboard settings", "warning")
        return desk.bots["grok"].info()  # type: ignore[attr-defined]

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()  # accept first: a pre-accept close reaches browsers as a bare 403/1006
        ok, ws_sid = auth_state(socket)
        if not ok and sec.auth_required:
            # non-browser clients may authenticate with a first message {"type":"auth","token":...}
            try:
                first = json.loads(await asyncio.wait_for(socket.receive_text(), 5))
                ok = first.get("type") == "auth" and sec.check_token(str(first.get("token", "")))
                if not ok:
                    sec.login_failed(client_ip(socket.scope), socket.headers.get("user-agent", ""))
            except Exception:
                ok = False
        if not ok:
            await socket.close(code=4401, reason="sign in required")
            return
        q: asyncio.Queue = asyncio.Queue()
        clients.add(q)
        try:
            await socket.send_text(json.dumps(_clean({"type": "snapshot", "data": desk.state()}), default=str))
            last_check = time.monotonic()
            while True:
                try:
                    await socket.send_text(await asyncio.wait_for(q.get(), 5))
                except asyncio.TimeoutError:
                    pass
                if ws_sid and time.monotonic() - last_check > 30:  # stop streaming once signed out
                    last_check = time.monotonic()
                    if not sec.session_valid(socket.cookies.get(SESSION_COOKIE)):
                        await socket.close(code=4401, reason="signed out")
                        return
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            clients.discard(q)

    return app
