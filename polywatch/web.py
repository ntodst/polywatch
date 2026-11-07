"""A small same-process web UI for Raspberry Pi; no frontend build required."""
import asyncio
import csv
import io
import json
import math
import re
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote, urlsplit

from aiohttp import web

from .dashboard import aggregate
from .research import ARCHIVES, SCREEN

ASSETS = Path(__file__).with_name("web_assets")
ADDRESS = re.compile(r"0x[a-fA-F0-9]{40}\Z")


def clean(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [clean(v) for v in value]
    return value


def dumps(value):
    return json.dumps(clean(value), separators=(",", ":"), allow_nan=False)


def response(value, status=200):
    return web.json_response(value, dumps=dumps, status=status)


def wallet_arg(value, optional=False):
    if optional and not value:
        return ""
    if not isinstance(value, str) or not ADDRESS.fullmatch(value):
        raise web.HTTPBadRequest(text="Use a 0x-prefixed, 40-digit wallet address.")
    return value.lower()


async def json_object(request):
    body = await request.json()
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="Expected a JSON object.")
    return body


@web.middleware
async def policy(request, handler):
    try:
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("Origin")
            if (origin and urlsplit(origin).netloc != request.host) or request.headers.get("X-Polywatch") != "1":
                raise web.HTTPForbidden(text="Open this dashboard directly to change its settings.")
            if request.content_type != "application/json":
                raise web.HTTPBadRequest(text="Expected a JSON request.")
        result = await handler(request)
    except web.HTTPException as exc:
        if not request.path.startswith("/api/"):
            raise
        result = response({"error": exc.text}, exc.status)
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        result = response({"error": "Invalid request parameters."}, 400)
    if not result.prepared:
        result.headers.update({
            "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            "Cache-Control": "no-store" if request.path.startswith("/api/") else "no-cache",
        })
    return result


def create_app(engine):
    app = web.Application(middlewares=[policy], client_max_size=8192)
    cache = {"at": 0, "data": None}

    async def index(request):
        return web.FileResponse(ASSETS / "index.html")

    async def asset(request):
        name = request.match_info["name"]
        if name not in ("app.js", "style.css", "favicon.svg"):
            raise web.HTTPNotFound()
        return web.FileResponse(ASSETS / name)

    async def summary(request):
        now = time.time()
        if now - cache["at"] < 5 and cache["data"] is not None:
            return response(cache["data"])
        traders = engine.research.traders()
        markets = aggregate(engine, engine.positions.active())
        markets.sort(key=lambda r: r["held"], reverse=True)
        stages = {s: sum(r["stage"] == s for r in traders) for s in ("long_term", "building", "pending", "excluded")}
        feeds = {k: asdict(v) for k, v in engine.feeds.items()}
        connected = all(f.last_ok and now - f.last_ok < max(15, engine.interval * 3) and not f.error for f in engine.feeds.values())
        storage = engine.research.storage()
        paper = engine.research.paper_summary()
        data = {
            "at": now, "started_at": engine.started, "connected": connected,
            "feeds": feeds, "notices": engine.notices, "stages": stages,
            "traders": traders[:500], "trader_count": len(traders), "markets": markets[:100], "market_count": len(markets),
            "activity": sorted([*[{**r, "at": r["timestamp"], "kind": r.get("alert_kind", "TRADE")} for r in engine.recent(50, alerts=True)],
                                *[{**r, "at": r["detected_at"]} for r in engine.position_changes(50) if r["kind"] != "FIRST SEEN"]],
                               key=lambda r: r["at"], reverse=True)[:100],
            "paper_records": engine.store.papers(limit=100),
            "paper_totals": {"closed": sum(p["closed"] for p in paper.values()),
                             "open": sum(p["open"] for p in paper.values()),
                             "skipped": sum(p["skipped"] for p in paper.values()),
                             "pnl": sum(p["pnl"] or 0 for p in paper.values()),
                             "priced": sum(p["priced"] for p in paper.values())},
            "settings": {"budget": engine.preferences["size"], "threshold": engine.threshold,
                         "interval": engine.interval, "shortlist": engine.store.state("web_shortlist", [])},
            "storage": storage, "screen": SCREEN,
            "verification": {"last_ok": engine.positions.last_ok, "pending": len(engine.positions.pending),
                             "error": engine.positions.error, "market_error": engine.positions.market_error},
        }
        cache.update(at=now, data=data)
        return response(data)

    async def trader(request):
        address = wallet_arg(request.match_info["wallet"])
        if address not in engine.wallets:
            raise web.HTTPNotFound(text="This wallet has not been discovered yet.")
        row = next(r for r in engine.research.traders() if r["address"] == address)
        return response({**row, **engine.research.detail(address)})

    async def shortlist(request):
        body = await json_object(request)
        address = wallet_arg(body.get("wallet"))
        if type(body.get("saved")) is not bool:
            raise web.HTTPBadRequest(text="saved must be true or false.")
        saved = set(engine.store.state("web_shortlist", []))
        if body["saved"]:
            if len(saved) >= 200 and address not in saved:
                raise web.HTTPBadRequest(text="Your shortlist is limited to 200 wallets.")
            saved.add(address)
            p = engine.wallets.setdefault(address, {"address": address, "cohorts": [], "name": address[:10]})
            if not engine.excluded(address):
                p["cohorts"] = list(set(p.get("cohorts", []) + ["watchlist"]))
                engine.enqueue(address)
            engine.store.wallet(address, p)
        else:
            saved.discard(address)
        engine.store.save_state("web_shortlist", sorted(saved))
        engine.store.db.commit()
        cache["at"] = 0
        return response({"shortlist": sorted(saved)})

    async def settings(request):
        body = await json_object(request)
        value = body.get("budget")
        if type(value) not in (int, float) or not math.isfinite(value) or not 1 <= value <= 1e6:
            raise web.HTTPBadRequest(text="Paper order budget must be between $1 and $1,000,000.")
        engine.preferences["size"] = float(value)
        engine.store.save_state("ui_preferences", engine.preferences)
        engine.store.db.commit()
        cache["at"] = 0
        return response({"budget": value})

    def archive_args(request):
        kind = request.query.get("kind", "trades")
        if kind not in ARCHIVES:
            raise web.HTTPBadRequest(text="Unknown archive type.")
        wallet = wallet_arg(request.query.get("wallet", ""), optional=True)
        return kind, wallet

    async def archive(request):
        kind, wallet = archive_args(request)
        offset, limit = int(request.query.get("offset", "0")), int(request.query.get("limit", "50"))
        if offset < 0 or not 1 <= limit <= 100:
            raise web.HTTPBadRequest(text="Invalid page size or offset.")
        return response(engine.research.archive(kind, wallet, offset, limit))

    async def export(request):
        kind, wallet = archive_args(request)
        table, _, owner = ARCHIVES[kind]
        # An independent SQLite read transaction gives exports a consistent view
        # while the scanner keeps writing. Never load the archive into memory.
        location = engine.store.db.execute("PRAGMA database_list").fetchone()[2]
        db = sqlite3.connect(f"file:{quote(location, safe='/')}?mode=ro", uri=True) if location else engine.store.db
        db.row_factory = sqlite3.Row
        own_db = db is not engine.store.db
        clause, args = (f" WHERE {owner}=?", [wallet]) if wallet else ("", [])
        if kind == "trades":
            clause += (" AND " if clause else " WHERE ") + "retained=1"
        stream = web.StreamResponse(headers={"Content-Type": "application/x-ndjson",
            "Content-Disposition": f'attachment; filename="polywatch-{kind}.ndjson"', "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store"})
        try:
            if own_db:
                db.execute("BEGIN")
            cursor = db.execute(f"SELECT * FROM {table}" + clause + " ORDER BY rowid", args)
            await stream.prepare(request)
            while batch := cursor.fetchmany(250):
                lines = []
                for row in batch:
                    meta = dict(row)
                    raw = json.loads(meta.pop("raw"))
                    lines.append(dumps({"meta": meta, "data": raw}) + "\n")
                await stream.write("".join(lines).encode())
                await asyncio.sleep(0)
            await stream.write_eof()
        finally:
            if own_db:
                db.close()
        return stream

    async def traders_csv(request):
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["wallet", "name", "screen", "account_pnl_usd", "month_marked_pnl_usd", "observed_days",
                         "daily_comparisons", "positive_active_day_fraction", "observed_pnl_change_usd",
                         "daily_sampled_drawdown_usd", "paper_closed", "paper_priced", "paper_net_usd"])
        for r in engine.research.traders():
            # Neutralize spreadsheet formula prefixes in external profile names.
            name = r["name"]
            if name.lstrip().startswith(("=", "+", "-", "@")):
                name = "'" + name
            local = r["local"]
            writer.writerow([r["address"], name, r["stage"], r["account_pnl"], r["month_pnl"], local["observation_days"],
                             local["daily_samples"], local["positive_days"], local["net_pnl"], local["drawdown"],
                             r["paper"]["closed"], r["paper"]["priced"], r["paper"]["pnl"]])
        return web.Response(text=output.getvalue(), content_type="text/csv", headers={
            "Content-Disposition": 'attachment; filename="polywatch-traders.csv"'})

    app.add_routes([web.get("/", index), web.get("/assets/{name}", asset),
                    web.get("/api/summary", summary), web.get("/api/trader/{wallet}", trader),
                    web.post("/api/shortlist", shortlist), web.post("/api/settings", settings),
                    web.get("/api/archive", archive), web.get("/api/export", export),
                    web.get("/api/traders.csv", traders_csv)])
    return app


async def serve(engine, host, port, tasks, seconds=None):
    runner = web.AppRunner(create_app(engine), access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, host, port)
        await site.start()
        display = "127.0.0.1" if host == "0.0.0.0" else f"[{host}]" if ":" in host else host
        print(f"Polywatch web dashboard: http://{display}:{port}", flush=True)
        if host == "0.0.0.0":
            print(f"On your local network: http://<raspberry-pi-ip>:{port}", flush=True)
        deadline = time.monotonic() + seconds if seconds else float("inf")
        while time.monotonic() < deadline:
            for task in tasks:
                if task.done():
                    task.result()
                    raise RuntimeError("A scanner task stopped unexpectedly")
            await asyncio.sleep(.5)
    finally:
        await runner.cleanup()
