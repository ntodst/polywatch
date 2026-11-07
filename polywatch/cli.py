import argparse
import asyncio
import json
import math
import re
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.live import Live

from .api import API
from .engine import Engine
from .store import Store
from .ui import keyboard, ranked_wallets, ranked_positions, render, wallet_detail
from .runtime import InstanceLock


def positive(value):
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return result


def parser():
    p = argparse.ArgumentParser(description="Watch verified current Polymarket positions, whales, and big bets. No credentials required.")
    p.add_argument("--threshold", type=positive, default=10000, help="big trade / accumulation / net position size floor, dollars (default: 10000)")
    p.add_argument("--interval", type=positive, default=1, help="feed head poll target in seconds (default: 1)")
    p.add_argument("--rps", type=positive, default=10, help="shared requests per second cap (default: 10)")
    p.add_argument("--wallets", type=int, default=50, help="leaderboard slots, half profit / half volume (default: 50)")
    p.add_argument("--db", type=Path, default=Path(__file__).resolve().parent.parent / "data" / "polywatch.sqlite3")
    p.add_argument("--wallet", help="fetch details for a single public wallet and exit")
    p.add_argument("--seconds", type=positive, help="stop after this many seconds (useful for smoke tests)")
    p.add_argument("--once", action="store_true", help="fetch feeds and rankings, print a snapshot, then exit")
    p.add_argument("--json", action="store_true", help="print JSON on exit, without the live screen")
    return p


async def run(args):
    if args.wallets < 2 or args.wallets > 200 or args.wallets % 2:
        raise ValueError("--wallets must be an even number from 2 to 200")
    if args.rps > 15:
        raise ValueError("--rps must be <= 15 to leave headroom for API limits")
    if args.interval < .25:
        raise ValueError("--interval must be >= 0.25 seconds")
    if args.wallet and not re.fullmatch(r"0x[a-fA-F0-9]{40}", args.wallet):
        raise ValueError("--wallet must be a 0x-prefixed 40-digit address")
    api, store = API(args.rps), Store(args.db)
    engine = Engine(api, store, args.threshold, args.interval, args.wallets)
    console = Console()
    tasks = []
    try:
        if args.wallet:
            wallet = args.wallet.lower()
            if not await engine.check_account(wallet, force=True):
                p = engine.wallets[wallet]
                result = {k: p.get(k) for k in ("address", "account_pnl", "disqualified", "pnl_reason", "pnl_observed_at")}
                if args.json:
                    print(json.dumps(result, indent=2))
                else:
                    console.print(p.get("pnl_reason", "Overall PnL awaiting verification"), markup=False)
                return
            await engine.positions.refresh(wallet)
            p = await engine.profile(wallet)
            p["positions"] = engine.positions.wallet_rows(wallet)
            recent, complete = await api.pages("/v2/trades", user=wallet, taker_only=False, limit=50, max_pages=1)
            p["recent_trades"] = recent
            if args.json:
                print(json.dumps(p, indent=2))
            else:
                console.print(wallet_detail(p, p["positions"], engine))
                from .ui import trade_table
                console.print(trade_table(recent, engine))
            return
        tasks = engine.tasks()
        if not args.json and not args.once and console.is_terminal:
            from .tui import WatchApp
            app = WatchApp(engine, seconds=args.seconds)
            await app.run_async()
            if app.return_code:
                raise RuntimeError("Terminal UI stopped with an error")
            console.print(f"Watcher stopped. History saved to {args.db}", markup=False)
            return
        deadline = time.monotonic() + (args.seconds or (30 if args.once else float("inf")))

        async def tick(get_key):
            view, order, selected = "live", "whale", 0
            while time.monotonic() < deadline:
                key = get_key()
                if key == "q":
                    break
                if key in "12345h" and key:
                    view = {"1": "live", "2": "wallets", "3": "alerts", "4": "changes", "5": "trades", "h": "history"}[key]
                    selected = 0
                elif key in ("p", "w"):
                    order = "profit" if key == "p" else "whale"
                    if view not in ("live", "wallets", "history"):
                        view = "live"
                    selected = 0
                elif key == "j":
                    selected += 1
                elif key == "k":
                    selected = max(0, selected - 1)
                elif key == "b":
                    view = "wallets"
                rows = (ranked_positions(engine, order) if view == "live" else ranked_wallets(engine, order, active_only=view != "history")) if key else []
                selected = min(selected, max(0, len(rows) - 1))
                if key in ("\n", "\r") and rows:
                    engine.selected = rows[selected].get("address") or rows[selected]["proxy_wallet"]
                    engine.enqueue(engine.selected)
                    engine.positions.request(engine.selected)
                    view = "detail"
                elif key == "r" and engine.selected:
                    engine.wallets[engine.selected]["updated"] = 0
                    engine.enqueue(engine.selected)
                    engine.positions.request(engine.selected)
                yield view, order, selected
                baselines_done = bool(engine.wallets) and all(a in engine.positions.session_checks for a, p in engine.wallets.items() if p.get("cohorts"))
                if args.once and all(f.last_ok for f in engine.feeds.values()) and engine.board_time and (engine.positions.active() or baselines_done):
                    break
                # Never let a crashed background task leave a healthy-looking UI.
                for task in tasks:
                    if task.done():
                        task.result()
                        raise RuntimeError("A scanner task stopped unexpectedly")
                await asyncio.sleep(.2)

        if args.json or args.once or not console.is_terminal:
            async for _ in tick(lambda: ""):
                pass
        else:
            with keyboard() as get_key, Live(console=console, screen=True, auto_refresh=False) as live:
                async for view, order, selected in tick(get_key):
                    live.update(render(engine, view, selected, order, console.size.height), refresh=True)
        if args.json:
            print(json.dumps(engine.snapshot(), indent=2))
        else:
            console.print(render(engine, height=console.size.height))
            console.print(f"History saved to {args.db}", markup=False)
        if args.once and (not all(f.last_ok for f in engine.feeds.values()) or not engine.board_time):
            raise RuntimeError("Snapshot incomplete: one or more API feeds unavailable")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await api.close()
        store.close()


def main():
    args = parser().parse_args()
    try:
        with InstanceLock(args.db):
            asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        Console(stderr=True).print(f"polywatch: {exc}", markup=False, style="red")
        sys.exit(1)
