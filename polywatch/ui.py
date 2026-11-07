import select
import sys
import termios
import time
import tty
from contextlib import contextmanager
from datetime import datetime

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .models import consistency, account_status
from .positions import fresh


def money(value):
    return "—" if value is None else f"${value:,.0f}"


def age(timestamp):
    if not timestamp:
        return "waiting"
    seconds = max(0, int(time.time() - timestamp))
    return f"{seconds}s" if seconds < 120 else f"{seconds // 60}m"


def clean(value):
    # Treat every API string as plain untrusted text, including terminal escapes.
    return "".join(c for c in str(value or "") if c.isprintable())


def text(value, style=""):
    return Text(clean(value), style=style)


def linked(label, url):
    return text(label, f"link {url}")


def wallet_link(address, label=None):
    return linked(label or address[:10] + "…", "https://polymarket.com/profile/" + address)


def pnl(value):
    return text(money(value), "green" if value is not None and value >= 0 else "red" if value is not None else "dim")


def trade_table(rows, engine, limit=15):
    rows = [r for r in rows if engine.allowed(r["proxy_wallet"].lower())]
    table = Table(box=box.SIMPLE, expand=True, show_edge=False)
    for title, kwargs in (("Age", {}), ("Signal", {}), ("Wallet", {"max_width": 19}),
                          ("Side / outcome", {"max_width": 22}), ("Amount", {"justify": "right"}),
                          ("Price", {"justify": "right"}), ("Held now", {}), ("Market", {"ratio": 2})):
        table.add_column(title, no_wrap=True, overflow="ellipsis", **kwargs)
    for r in rows[:limit]:
        address = r["proxy_wallet"].lower()
        p = engine.wallets.get(address, {})
        amount = r.get("bundle_amount", float(r["size"]) * float(r["price"]))
        signal = r.get("alert_kind") or ("BIG BET" if amount >= engine.threshold else "TRACKED")
        side = r["side"]
        balance = engine.positions.rows.get((address, str(r["token_id"])))
        held = "unverified"
        if balance and fresh(balance):
            held = "EXITED" if balance["current_size"] <= .000001 else money(balance.get("current_value"))
            if balance.get("redeemable"):
                held = "settled"
        elif balance and balance.get("verification_pending"):
            held = "pending"
        table.add_row(age(r["timestamp"]), text(signal, "bold yellow" if amount >= engine.threshold else "cyan"),
                      wallet_link(address, p.get("name") or r.get("name") or address[:10]),
                      text(f"{side} {r.get('outcome', '?')}", "green" if side == "BUY" else "red"),
                      text(money(amount)), text(f"{float(r['price']):.3f}"), text(held),
                      linked(r.get("title") or r["condition_id"][:12], "https://polymarket.com/event/" + (r.get("event_slug") or r.get("slug", ""))))
    if not rows:
        table.add_row("", "Waiting for matching trades…", "", "", "", "", "", "")
    return table


def ranked_wallets(engine, order="profit", active_only=True):
    balances = {}
    for row in engine.positions.active():
        addr = row["proxy_wallet"]
        balances[addr] = balances.get(addr, 0) + (row.get("net_value") or 0)
    rows = [{**p, "active_exposure": balances.get(addr)} for addr, p in engine.wallets.items()
            if p.get("cohorts") and not engine.excluded(addr) and (not active_only or addr in balances)]
    field = "active_exposure" if order == "whale" else "account_pnl"
    return sorted(rows, key=lambda p: (p.get(field) is not None, p.get(field) or 0), reverse=True)


def ranked_positions(engine, order="profit"):
    rows = engine.positions.active()
    if order == "profit":
        rows.sort(key=lambda r: ((engine.wallets.get(r["proxy_wallet"], {}).get("account_pnl") or 0) > 0,
                                 engine.wallets.get(r["proxy_wallet"], {}).get("account_pnl") or 0,
                                 r.get("net_value") or 0), reverse=True)
    return rows


def position_table(engine, order="profit", selected=-1, limit=12):
    table = Table(box=box.SIMPLE, expand=True, show_edge=False)
    for name, kwargs in (("", {}), ("Market / outcome", {"ratio": 2}), ("Wallet", {"max_width": 18}),
                         ("Net value", {"justify": "right"}), ("Net shares", {"justify": "right"}),
                         ("Entry → mark", {}), ("Open PnL", {"justify": "right"}),
                         ("Change", {}), ("Profitable holders", {}), ("Verified", {})):
        table.add_column(name, no_wrap=True, overflow="ellipsis", **kwargs)
    rows = ranked_positions(engine, order)
    start = max(0, selected - limit + 1)
    for i, r in enumerate(rows[start:start + limit], start):
        addr = r["proxy_wallet"]
        p = engine.wallets.get(addr, {})
        label = f"{r.get('title', '?')} / {r.get('outcome', '?')}"
        change = r.get("last_change", "BASELINE") + " " + age(r.get("change_at"))
        if r.get("paired_shares"):
            change += " · hedged"
        table.add_row("›" if selected == i else "", linked(label, "https://polymarket.com/event/" + (r.get("event_slug") or r.get("slug", ""))),
                      wallet_link(addr, p.get("name") or addr[:12]), text(money(r.get("net_value"))),
                      text(f"{r['net_shares']:,.0f}"), text(f"{r.get('avg_price', 0):.3f} → {r.get('current_price', 0):.3f}"),
                      pnl(r.get("unrealized_pnl")), text(change, "yellow" if r.get("paired_shares") else "cyan"),
                      text(str(r["profitable_holders"]), "green" if r["profitable_holders"] >= 2 else "dim"),
                      text(age(r["verified_at"])))
    if not rows:
        table.add_row("", "Checking current holdings and market availability…", "", "", "", "", "", "", "", "")
    return table


def changes_table(engine, limit=8):
    table = Table(box=box.SIMPLE, expand=True)
    for label in ("Detected", "Change", "Wallet", "Outcome", "Shares before → now", "Market"):
        table.add_column(label, no_wrap=True, overflow="ellipsis", ratio=2 if label == "Market" else None)
    for r in engine.position_changes(limit):
        before = "unknown" if r["previous_size"] is None else f"{r['previous_size']:,.0f}"
        addr = r["proxy_wallet"]
        table.add_row(age(r["detected_at"]), text(r["kind"], "red" if r["kind"] in ("EXITED", "REDUCED") else "green"),
                      wallet_link(addr, engine.wallets.get(addr, {}).get("name") or addr[:12]), text(r.get("outcome")),
                      text(f"{before} → {r['current_size']:,.0f}"), text(r.get("title")))
    return table


def wallet_table(engine, order="profit", selected=-1, limit=12, active_only=True):
    table = Table(box=box.SIMPLE, expand=True)
    for label in ("", "Wallet", "Source", "Overall PnL", "Month marked", "Active value", "Open PnL", "Closed mkts", "Account / balances"):
        table.add_column(label, no_wrap=True, overflow="ellipsis", max_width=21 if label == "Wallet" else None)
    rows = ranked_wallets(engine, order, active_only)
    start = max(0, selected - limit + 1)
    for i, p in enumerate(rows[start:start + limit], start):
        p = {**p, **engine.positions.summary(p["address"])}
        table.add_row("›" if i == selected else "", wallet_link(p["address"], p.get("name") or p["address"][:12]),
                      text(", ".join(p.get("cohorts", []))), pnl(p.get("account_pnl")), pnl(p.get("month_pnl")),
                      text(money(p.get("active_exposure"))), pnl(p.get("unrealized_pnl")),
                      text(str(p.get("closed_markets", "—")) + ("+" if p.get("closed_complete") is False else "")),
                      text(account_status(p) + " / " + age(p.get("positions_updated")),
                           "green" if account_status(p) == "eligible" else "dim"))
    return table


def wallet_detail(p, positions=None, engine=None):
    address = p["address"]
    summary = Text()
    summary.append(clean(p.get("name") or address) + "\n", "bold cyan")
    summary.append(address + "\n")
    summary.append(f"Overall account PnL: {money(p.get('account_pnl'))}   Status: {account_status(p)}   Observation: {age(p.get('pnl_observed_at'))}\n")
    summary.append(f"Realized lifetime: {money(p.get('all_pnl'))}   Month marked: {money(p.get('month_pnl'))}\n")
    if engine:
        p = {**p, **engine.positions.summary(address)}
    summary.append(f"Portfolio snapshot: {money(p.get('portfolio'))}   Open PnL: {money(p.get('unrealized_pnl'))}   Balances: {age(p.get('positions_updated'))}\n")
    rate = p.get("win_rate")
    summary.append(f"Closed markets: {p.get('closed_markets', '—')}   Closed win rate: {rate:.1%}" if rate is not None else "Closed results not yet available")
    daily = p.get("positive_days")
    if daily is not None:
        summary.append(f"   Positive marked days: {daily:.0%} of {p.get('active_days', 0)} active sampled days")
    summary.append("\nConsistency screen: " + ("PASS" if consistency(p) else "not qualified / incomplete") + "\n")
    summary.append("https://polymarket.com/profile/" + address)
    table = Table(title="Latest balances (includes settled / unverified holdings)", box=box.SIMPLE, expand=True)
    for label in ("Market", "Outcome", "Shares", "Entry", "Mark", "Value", "Unrealized", "State / age"):
        table.add_column(label, ratio=2 if label == "Market" else None)
    for r in (positions if positions is not None else p.get("positions", []))[:12]:
        state = "PENDING" if r.get("verification_pending") else "STALE" if not fresh(r) else "SETTLED" if r.get("redeemable") else r.get("last_change", "BASELINE")
        table.add_row(text(r.get("title")), text(r.get("outcome")), text(f"{r['current_size']:,.0f}"),
                      text(f"{r['avg_price']:.3f}"), text(f"{r['current_price']:.3f}"),
                      text(money(r.get("current_value"))), pnl(r.get("unrealized_pnl")), text(state + " / " + age(r.get("verified_at"))))
    return Group(summary, table)


def render(engine, view="live", selected=0, order="whale", height=40):
    now = time.time()
    header = Text(f"POLYMARKET WATCH   ALL MARKETS   ≥ {money(engine.threshold)}   {datetime.now():%H:%M:%S}\n", "bold cyan")
    for f in engine.feeds.values():
        state = "ERROR" if f.error else "CATCHING UP" if f.backlog else "OK" if f.last_ok else "STARTING"
        header.append(f"{f.name}: {state} · response {age(f.last_ok)} · newest trade {age(f.latest)}   ", "red" if f.error else "yellow" if f.backlog or now - f.last_ok > 10 else "green")
    header.append(f"\nPolling target {engine.interval:g}s · {engine.api.requests} requests · {engine.api.retries} retries · CDN age {engine.api.cache_age:g}s · profiles queued {engine.queue.qsize()}\n")
    if engine.status:
        status = engine.status
        lag = status.get("serving", {}).get("lag_seconds")
        status_age = status.get("age_seconds", 0) + int(now - engine.status_time)
        header.append(f"API serving lag: {lag if lag is not None else '?'}s · health snapshot age: {status_age}s · rankings fetched {age(engine.board_time)}")
    else:
        header.append("API health: waiting")
    discovered = sum(bool(p.get("cohorts")) for p in engine.wallets.values())
    header.append(f"\nActive exposures: {len(engine.positions.active())} · wallets checked {len(engine.positions.session_checks)}/{discovered} · balances {age(engine.positions.last_ok)} · pending {len(engine.positions.pending)}")
    counts = {s: sum(account_status(p) == s for p in engine.wallets.values()) for s in ("eligible", "pending", "excluded")}
    header.append(f" · PnL approved {counts['eligible']} / pending {counts['pending']} / excluded {counts['excluded']}")
    budget = max(4, height - 15)
    if view in ("wallets", "history"):
        body = wallet_table(engine, order, selected, max(4, budget - 3), active_only=view != "history")
    elif view == "detail" and engine.selected and not engine.excluded(engine.selected):
        body = wallet_detail(engine.wallets[engine.selected], engine.positions.wallet_rows(engine.selected), engine)
    elif view == "alerts":
        body = trade_table(engine.recent(alerts=True, limit=100), engine, budget - 3)
    elif view == "changes":
        body = changes_table(engine, budget - 3)
    elif view == "trades":
        body = trade_table(engine.recent(limit=100), engine, budget - 3)
    else:
        body = Group(Panel(position_table(engine, order, selected, max(4, budget // 2)), title="Currently held · tradable markets · meaningful unpaired exposure"),
                     changes_table(engine, max(3, budget // 2 - 6)))
    errors = [f.error for f in engine.feeds.values() if f.error] + [e for e in (engine.positions.error, engine.positions.market_error) if e] + engine.notices[-1:]
    footer = Text("1 positions  2 active wallets  3 bet alerts  4 changes  5 trades  h history  p/w sort  j/k select  Enter details  r refresh  q quit", "dim")
    if errors:
        footer.append("\n" + clean(" | ".join(errors))[:230], "yellow")
    footer.append("\nEntry → mark uses snapshot prices. Pending/stale balances, paired exposure, and settled/paused/past-end markets are excluded.", "dim")
    return Group(Panel(header, border_style="cyan"), body, footer)


@contextmanager
def keyboard():
    if not sys.stdin.isatty():
        yield lambda: ""
        return
    previous = termios.tcgetattr(sys.stdin)
    try:
        tty.setcbreak(sys.stdin.fileno())
        yield lambda: sys.stdin.read(1) if select.select([sys.stdin], [], [], 0)[0] else ""
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, previous)
