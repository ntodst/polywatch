"""Local web contract checks. These tests do not call Polymarket."""
import asyncio
import time

from aiohttp.test_utils import TestClient, TestServer

from polywatch.engine import Engine
from polywatch.store import Store
from polywatch.web import create_app

WALLET = "0x" + "b" * 40


def test_dashboard_shortlist_validation_settings_and_export(tmp_path):
    async def run():
        store = Store(tmp_path / "web.sqlite3")
        engine = Engine(None, store)
        now = time.time()
        engine.wallets[WALLET] = {"address": WALLET, "name": "<script>alert(1)</script>", "cohorts": [],
                                  "disqualified": True, "account_pnl": -100}
        store.wallet(WALLET, engine.wallets[WALLET])
        store.save_paper({"id": "loss", "wallet": WALLET, "token": "123", "condition": "condition",
                          "status": "closed", "detected_at": now - 100, "closed_at": now, "pnl": -25})
        async with TestClient(TestServer(create_app(engine))) as client:
            home = await client.get("/")
            assert home.status == 200
            assert "frame-ancestors 'none'" in home.headers["Content-Security-Policy"]
            assert "Trader research" in await home.text()
            assets = await client.get("/assets/app.js")
            assert assets.status == 200
            summary = await (await client.get("/api/summary")).json()
            assert summary["stages"]["excluded"] == 1
            assert summary["paper_totals"]["pnl"] == -25
            assert not summary["connected"]
            headers = {"X-Polywatch": "1"}
            denied = await client.post("/api/settings", json={"budget": 2000})
            assert denied.status == 403
            foreign = await client.post("/api/settings", json={"budget": 2000},
                                        headers={**headers, "Origin": "https://another-site.test"})
            assert foreign.status == 403
            bad = await client.post("/api/settings", json={"budget": -1}, headers=headers)
            assert bad.status == 400
            array = await client.post("/api/settings", json=[], headers=headers)
            assert array.status == 400
            saved = await client.post("/api/settings", json={"budget": 2500}, headers=headers)
            assert saved.status == 200
            assert engine.preferences["size"] == 2500
            assert store.state("ui_preferences")["size"] == 2500
            shortlist = await client.post("/api/shortlist", json={"wallet": WALLET, "saved": True}, headers=headers)
            assert shortlist.status == 200
            assert store.state("web_shortlist") == [WALLET]
            assert engine.excluded(WALLET)
            assert not engine.wallets[WALLET]["cohorts"]
            invalid = await client.get("/api/archive?kind=wallets")
            assert invalid.status == 400
            page = await (await client.get("/api/archive?kind=paper&limit=1")).json()
            assert page["total"] == 1
            exported = await client.get("/api/export?kind=paper")
            assert exported.status == 200
            assert '"pnl":-25' in await exported.text()
            detail = await (await client.get(f"/api/trader/{WALLET}")).json()
            assert detail["audit_only"]
            assert detail["positions"] == []
            assert detail["paper"]["pnl"] == -25
        store.close()
    asyncio.run(run())
