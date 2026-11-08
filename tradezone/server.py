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
    # per coin, only what the screen shows (with up to 100+ coins watched, the rest made every update ~3x bigger)
    keep = ("momentum", "mean_reversion", "breakout", "ensemble")
    st["scores"] = {sym: {k: round(v, 3) for k in keep if isinstance(v := sc.get(k), (int, float))}
                    for sym, sc in (st.get("scores") or {}).items()}
    st["regimes"] = {sym: {"regime": r.get("regime")} for sym, r in (st.get("regimes") or {}).items() if isinstance(r, dict)}
    return st


class LoginBody(BaseModel):
    token: str




class BrokerBody(BaseModel):
    key: str
    secret: str = ""
    venue: str = "alpaca"  # alpaca | kraken (Key + Secret) | uphold (key = access token)


class AccountBody(BaseModel):
    venue: str
    key: str
    secret: str = ""
    label: str = ""


class StackMoveBody(BaseModel):
    symbol: str
    direction: str  # "in" (trading -> hold stack) | "out" (hold stack -> trading)
    fraction: float = 1.0  # share of what's there to move


class ProtocolBody(BaseModel):
    action: str  # "best" (tested-best settings) | "cap" (real-money cap in USD)
    amount: float = 0.0


class StrategyBody(BaseModel):
    on: bool


class RiskBody(BaseModel):
    profile: str
    confirm: str = ""


class GoalBody(BaseModel):
    start: float
    target: float
    daily: float | None = None  # the daily profit goal in USD (0 = off)


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


class MemeDeskBody(BaseModel):
    url: str = ""


class ControlBody(BaseModel):
    reason: str = "operator"


class TradeSellBody(BaseModel):
    symbol: str
    account: str = "main"
    fraction: float = 1.0
    confirm_loss: bool = False


class TradePlanBody(BaseModel):
    symbol: str
    account: str = "main"
    stop_pct: float | None = None  # e.g. 0.05 = 5% below what was paid
    target_pct: float | None = None  # e.g. 0.15 = 15% above, after fees


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
            try:
                push({"type": "state", "data": light_state(desk)})
            except Exception:  # one bad update must never stop every screen from updating
                log.exception("live update failed")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = [asyncio.create_task(broadcaster())]
        if run_desk:
            tasks.append(asyncio.create_task(desk.run_forever()))
            await desk.fleet.start()  # every extra linked account trades at the same time
        try:
            yield
        finally:
            desk.stop()
            await desk.fleet.stop()
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

    @app.get("/fonts/{name}.woff2", include_in_schema=False)
    async def font(name: str):
        # the bundled typeface (Inter, SIL Open Font License, see web/fonts/); nothing else is served from here
        if name != "inter":
            raise HTTPException(404)
        return FileResponse(WEB / "fonts" / f"{name}.woff2", media_type="font/woff2",
                            headers={"cache-control": "public, max-age=31536000, immutable"})

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

    def _trade_desk(account: str):
        try:
            return desk.fleet.desk(account or "main")
        except KeyError:
            raise HTTPException(404, "no such account") from None

    @app.post("/api/accounts/meme-desk", dependencies=[Depends(check_token)])
    async def meme_desk(request: Request):
        """Start the Meme desk: TradeZone's own bot team for meme coins only (practice money)."""
        from .fleet import FleetError
        try:
            row = desk.fleet.add_meme_desk()
        except FleetError as exc:
            raise HTTPException(400, str(exc)) from None
        audit(request, "started the Meme desk", "info")
        return _clean({"ok": True, "account": row})

    @app.post("/api/memedesk", dependencies=[Depends(check_token)])
    async def memedesk(body: MemeDeskBody, request: Request):
        """Attach the MemeCoin desk by its address (read-only), or detach it with an empty one."""
        try:
            view = await desk.attach_memedesk(body.url)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        audit(request, "attached the MemeCoin desk" if body.url else "detached the MemeCoin desk", "info")
        return _clean({"ok": True, "memedesk": view})

    @app.post("/api/trade/sell", dependencies=[Depends(check_token)])
    async def trade_sell(body: TradeSellBody, request: Request):
        d = _trade_desk(body.account)
        try:
            out = d.sell_trade(body.symbol, body.fraction, body.confirm_loss)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        if out.get("needs_confirm"):
            raise HTTPException(409, f"Selling now loses about ${-out['net']:,.2f} after fees - confirm to sell anyway")
        audit(request, f"sold {out['selling']:.0%} of {out['symbol']} ({body.account})", "warning")
        await d.bus.drain()
        return _clean({"ok": True, **out})

    @app.post("/api/trade/plan", dependencies=[Depends(check_token)])
    async def trade_plan(body: TradePlanBody, request: Request):
        d = _trade_desk(body.account)
        try:
            out = d.set_trade_plan(body.symbol, body.stop_pct, body.target_pct)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        audit(request, f"set the plan of {out['symbol']} ({body.account})", "info")
        return _clean({"ok": True, **out})

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

    @app.get("/api/chart", dependencies=[Depends(check_token)])
    async def chart(mode: str | None = None, hours: float = Query(1.0, ge=0, le=24 * 365)):
        """Profit/loss over the last ``hours`` (0 = since the start) with the trades on it."""
        return _clean(desk.chart(mode, hours))

    @app.get("/api/trades", dependencies=[Depends(check_token)])
    async def trades(mode: str | None = None, symbol: str | None = None,
                     limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
        return _clean(desk.journal.trades(mode or None, symbol or None, limit, offset))

    _holdings_cache: dict[str, tuple[float, dict]] = {}

    @app.get("/api/holdings", dependencies=[Depends(check_token)])
    async def holdings(account: str | None = None):
        """Everything an exchange account holds, coin by coin: the bots' trades, the hold stack, and yours."""
        try:
            d = desk if not account or account == "main" else desk.fleet.desk(account)
        except Exception:
            raise HTTPException(404, "no such account") from None
        broker = d.live_broker
        if broker is None or not hasattr(broker, "holdings"):
            return {"available": False, "why": "only a linked Kraken account can list every coin it holds"}
        import time as _t
        key = account or "main"
        hit = _holdings_cache.get(key)
        if hit and _t.time() - hit[0] < 30:  # (one Balance call per 30 s at most: Kraken's request limit)
            data = hit[1]
        else:
            data = await broker.holdings()
            if data is None:
                return {"available": False, "why": f"{broker.label} didn't answer - try again in a moment"}
            _holdings_cache[key] = (_t.time(), data)
        bots = {p.symbol: p.qty for p in (d.live_portfolio.open_positions if d.live_portfolio else [])}
        stack = {s: h["qty"] for s, h in d.bots["stacker"].books.get("live", {}).items()} if "stacker" in d.bots else {}
        rows = []
        for sym, h in data.items():
            total = h["qty"] + h["locked"]
            price = h["price"] or d.prices.get(sym)
            b, st_ = max(0.0, bots.get(sym, 0.0)), max(0.0, stack.get(sym, 0.0))
            rows.append({"symbol": sym, "total": total, "free": h["qty"], "locked": h["locked"], "bots": b, "stack": st_,
                         "yours": max(0.0, h["qty"] - b - st_), "price": price, "value": total * price if price else None})
        rows.sort(key=lambda r: -(r["value"] or 0))
        return {"available": True, "venue": broker.label, "coins": rows,
                "value": sum(r["value"] or 0 for r in rows), "unpriced": [r["symbol"] for r in rows if r["value"] is None]}

    @app.get("/api/trades/trips", dependencies=[Depends(check_token)])
    async def trade_trips(mode: str | None = None, symbol: str | None = None, account: str | None = None,
                          days: float = Query(0, ge=0, le=3650)):
        """Every trade from buy to sale (the Trades tab), for one account and one book."""
        try:
            d = desk if not account or account == "main" else desk.fleet.desk(account)
        except Exception:
            raise HTTPException(404, "no such account") from None
        mode = mode if mode in ("paper", "live") else d.mode
        book = d.live_portfolio if mode == "live" else d.paper_portfolio
        held = {} if book is None else {
            p.symbol: {"qty": p.qty, "last_price": p.last_price, "unrealized_pnl": p.unrealized_pnl,
                       "net": d.settings.net_if_sold(p, p.last_price)} for p in book.open_positions}
        import time as _t
        since = _t.time() - days * 86_400 if days else 0.0
        return _clean({**d.journal.round_trips(mode, symbol or None, since, held), "mode": mode,
                       "account": account or "main", "record": d.journal.track_record(mode)})

    @app.get("/api/reports/gains.csv", dependencies=[Depends(check_token)])
    async def gains_csv(mode: str = "live", year: int | None = Query(None, ge=2000, le=2100)):
        """A tax report: every sale's proceeds, cost basis (first-in first-out, fees included) and gain."""
        rows = desk.journal.gains("live" if mode == "live" else "paper", year)
        buf = io.StringIO()
        w = csv.writer(buf)
        cols = ["symbol", "qty", "acquired", "sold", "proceeds", "cost", "gain", "term", "note"]
        w.writerow(["description", *cols[1:]])
        for r in rows:
            w.writerow([f"{r['qty']:g} {r['symbol'].split('-')[0]}", *[r[c] for c in cols[1:]]])
        w.writerow([])
        w.writerow(["total", "", "", "", round(sum(r["proceeds"] for r in rows), 2), round(sum(r["cost"] for r in rows), 2),
                    round(sum(r["gain"] for r in rows), 2), "", "first-in first-out, both fees included; not tax advice"])
        name = f"tradezone-gains-{mode}{'-' + str(year) if year else ''}.csv"
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

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
    @app.post("/api/risk-profile", dependencies=[Depends(check_token)])
    async def set_risk_profile(body: RiskBody, request: Request):
        try:
            info = desk.set_risk_profile(body.profile, body.confirm)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        for d in desk.fleet.desks.values():  # one risk level for every linked account
            d.set_risk_profile(body.profile, body.confirm)
        audit(request, f"real-money risk level set to {body.profile}", "warning")
        return info

    @app.post("/api/stack/move", dependencies=[Depends(check_token)])
    async def stack_move(body: StackMoveBody, request: Request):
        st = desk.bots["stacker"]
        sym = body.symbol.strip().upper()
        frac = max(0.0, min(1.0, body.fraction))
        if frac <= 0 or body.direction not in ("in", "out"):
            raise HTTPException(400, "choose in or out, and how much to move")
        try:
            if body.direction == "out":
                held = st.book.get(sym, {}).get("qty", 0.0)  # type: ignore[attr-defined]
                res = st.move_out(sym, held * frac)  # type: ignore[attr-defined]
            else:
                res = st.move_in(sym, desk.portfolio.position(sym).qty * frac)  # type: ignore[attr-defined]
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        await desk.bus.drain()  # book it now, so the reply shows the new balances
        audit(request, f"moved {res['moved']:g} {sym} to the {res['to']}", "warning")
        return _clean({**res, "stack": st.summary()})  # type: ignore[attr-defined]

    @app.post("/api/protocol", dependencies=[Depends(check_token)])
    async def protocol(body: ProtocolBody, request: Request):
        try:
            if body.action == "best":
                out = desk.apply_best()
                for d in desk.fleet.desks.values():
                    d.apply_best()
            elif body.action == "cap":
                out = desk.set_live_cap(body.amount)
                for d in desk.fleet.desks.values():  # the same cap on every linked account
                    d.set_live_cap(body.amount)
            else:
                raise ValueError("action must be best or cap")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        audit(request, f"protocol: {body.action}" + (f" ${body.amount:,.0f}" if body.action == "cap" else ""), "warning")
        return _clean(out)

    @app.post("/api/never-sell", dependencies=[Depends(check_token)])
    async def never_sell(body: StrategyBody, request: Request):
        out = desk.set_never_sell(body.on)
        for d in desk.fleet.desks.values():
            d.set_never_sell(body.on)
        audit(request, f"never sell at a loss: {'on' if body.on else 'off'}", "warning")
        return out

    @app.post("/api/loss-floor", dependencies=[Depends(check_token)])
    async def loss_floor(body: StrategyBody, request: Request):
        out = desk.set_loss_floor(body.on)
        for d in desk.fleet.desks.values():
            d.set_loss_floor(body.on)
        audit(request, f"loss floor: {'on' if body.on else 'off'}", "warning")
        return out

    @app.post("/api/falling-guard", dependencies=[Depends(check_token)])
    async def falling_guard(body: StrategyBody, request: Request):
        out = desk.set_falling_guard(body.on)
        for d in desk.fleet.desks.values():
            d.set_falling_guard(body.on)
        audit(request, f"falling-market guard: {'on' if body.on else 'off'}", "warning")
        return out

    @app.post("/api/loser-cap", dependencies=[Depends(check_token)])
    async def loser_cap(body: StrategyBody, request: Request):
        out = desk.set_loser_cap(body.on)
        for d in desk.fleet.desks.values():
            d.set_loser_cap(body.on)
        audit(request, f"underwater cap: {'on' if body.on else 'off'}", "warning")
        return _clean(out)

    @app.post("/api/strategies/{key}", dependencies=[Depends(check_token)])
    async def set_strategy(key: str, body: StrategyBody, request: Request):
        try:
            info = desk.set_strategy(key, body.on)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        for d in desk.fleet.desks.values():  # the same strategies for every linked account
            d.set_strategy(key, body.on)
        audit(request, f"strategy {key} {'on' if body.on else 'off'}")
        return _clean(info)

    @app.post("/api/goal", dependencies=[Depends(check_token)])
    async def set_goal(body: GoalBody, request: Request):
        audit(request, f"goal set: ${body.start:,.2f} -> ${body.target:,.2f}"
                       + (f", ${body.daily:,.0f} a day" if body.daily is not None else ""))
        try:
            if body.daily is not None:
                desk.set_daily_goal(body.daily)
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
            acct = await desk.link_broker(body.key, body.secret, venue=body.venue)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        audit(request, f"linked a real-money {desk.broker_info()['label']} account (trading only, no withdrawals)", "warning")
        return _clean({"ok": True, "account": acct, **desk.broker_info()})

    @app.delete("/api/accounts/broker", dependencies=[Depends(check_token)])
    async def unlink_broker(request: Request):
        try:
            await desk.unlink_broker()
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        audit(request, "unlinked the real-money account", "warning")
        return {"ok": True}

    # -- more than one real-money account at the same time (each with its own bot team) --------------
    def fleet_desk(acct_id: str) -> TradingDesk:
        try:
            return desk.fleet.desk(acct_id)
        except KeyError:
            raise HTTPException(404, "no such account") from None

    @app.get("/api/fleet", dependencies=[Depends(check_token)])
    async def fleet(refresh: bool = False):
        if refresh:
            await desk.fleet.refresh_balances(force=True)
        return _clean({"accounts": desk.fleet.summary(), "totals": desk.fleet.totals()})

    @app.post("/api/fleet", dependencies=[Depends(check_token)])
    async def fleet_add(body: AccountBody, request: Request):
        from .fleet import FleetError
        try:
            row = await desk.fleet.add(body.venue, body.key, body.secret, body.label)
        except (FleetError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from None
        audit(request, f"linked another real-money account: {row['label']} ({row['venue_label']}, trading only, "
                       "no withdrawals)", "warning")
        return _clean(row)

    @app.delete("/api/fleet/{acct_id}", dependencies=[Depends(check_token)])
    async def fleet_remove(acct_id: str, request: Request):
        from .fleet import FleetError
        if acct_id == "main":
            raise HTTPException(400, "unlink the main account in Settings → Linked accounts")
        fleet_desk(acct_id)
        try:
            await desk.fleet.remove(acct_id)
        except FleetError as exc:
            raise HTTPException(409, str(exc)) from None
        audit(request, f"unlinked account {acct_id}", "warning")
        return {"ok": True}

    @app.get("/api/fleet/{acct_id}/mode", dependencies=[Depends(check_token)])
    async def fleet_mode_check(acct_id: str):
        return _clean(await fleet_desk(acct_id).mode_check())

    @app.post("/api/fleet/{acct_id}/mode", dependencies=[Depends(check_token)])
    async def fleet_mode(acct_id: str, body: ModeBody, request: Request):
        d = fleet_desk(acct_id)
        if body.mode == "live" and body.confirm.strip().upper() != LIVE_SWITCH_WORD:
            raise HTTPException(400, f"type {LIVE_SWITCH_WORD} to confirm switching to real-money trading")
        audit(request, f"account {acct_id}: switch to {body.mode.upper()}", "critical" if body.mode == "live" else "warning")
        try:
            out = await d.switch_mode(body.mode, flatten=body.flatten)
        except ModeSwitchError as exc:
            raise HTTPException(409, str(exc)) from None
        return _clean({**out, "account": desk.fleet.row(acct_id)})

    @app.post("/api/fleet/{acct_id}/control/{action}", dependencies=[Depends(check_token)])
    async def fleet_control(acct_id: str, action: str, request: Request):
        from .fleet import FleetError
        audit(request, f"account {acct_id}: {action}", "warning" if action in ("kill", "flatten") else "info")
        if acct_id == "all":
            try:
                return {"ok": True, "accounts": desk.fleet.control_all(action)}
            except FleetError as exc:
                raise HTTPException(400, str(exc)) from None
        d = fleet_desk(acct_id)
        if action == "pause":
            d.pause("operator")
        elif action == "resume":
            d.resume()
        elif action == "flatten":
            return {"ok": True, "closing": d.flatten("operator flatten")}
        elif action == "kill":
            d.halt("manual kill", flatten=True)
        elif action == "reset":
            d.reset_halt()
        else:
            raise HTTPException(400, "action must be pause|resume|flatten|kill|reset")
        return _clean({"ok": True, "account": desk.fleet.row(acct_id)})

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
                with contextlib.suppress(asyncio.TimeoutError):
                    await socket.send_text(await asyncio.wait_for(q.get(), 5))
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
