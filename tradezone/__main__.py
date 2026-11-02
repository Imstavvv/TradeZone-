"""Entry point: ``python -m tradezone [serve|sim] ...``"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os

from .config import Settings


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader; real environment variables take precedence."""
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def main() -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--log-level", default="INFO")
    ap = argparse.ArgumentParser(
        prog="tradezone", description="TradeZone multi-bot trading desk", parents=[common]
    )
    sub = ap.add_subparsers(dest="cmd")
    sv = sub.add_parser("serve", parents=[common], help="run the desk with the web dashboard (default)")
    sv.add_argument("--host")
    sv.add_argument("--port", type=int)
    sm = sub.add_parser("sim", parents=[common], help="run a fast headless simulation and print a report")
    sm.add_argument("--steps", type=int, default=2000)
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    load_dotenv()
    settings = Settings.from_env()
    if args.cmd == "sim":
        asyncio.run(_sim(settings, args.steps))
        return

    import uvicorn

    from .desk import TradingDesk
    from .server import create_app

    host = getattr(args, "host", None) or settings.host
    port = getattr(args, "port", None) or settings.port
    desk = TradingDesk(settings)
    if desk.settings.mode == "live":
        logging.warning("*** LIVE TRADING ENABLED - real money is at risk ***")
    uvicorn.run(create_app(desk), host=host, port=port, log_level=args.log_level.lower())


async def _sim(settings: Settings, steps: int) -> None:
    from .desk import TradingDesk
    from .journal import Journal

    settings.feed, settings.broker, settings.mode = "sim", "paper", "paper"
    settings.risk.symbol_cooldown_s = 0
    settings.risk.max_orders_per_minute = 10**9
    desk = TradingDesk(settings, journal=Journal(":memory:"))
    await desk.start()
    for _ in range(steps):
        await desk.step()
    st = desk.state()
    report = {
        "steps": steps,
        "portfolio": {k: v for k, v in st["portfolio"].items() if k != "positions"},
        "performance": st["performance"],
        "team": {b["title"]: b["metrics"] for b in st["bots"]},
    }
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
