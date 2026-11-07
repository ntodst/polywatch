"""Compact, keyboard-first Textual interface for laptop terminals."""
import asyncio
import time
import webbrowser

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static, DataTable, Input, Button, Label, Select, Checkbox, TabbedContent, TabPane

from .dashboard import Dashboard
from .execution import estimate
from .models import account_status
from .ui import clean, money, age, wallet_detail


def compact_money(value):
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    value = abs(value)
    return f"{sign}${value/1e6:.2f}m" if value >= 1e6 else f"{sign}${value/1000:.1f}k" if value >= 1000 else f"{sign}${value:.0f}"


def cash(value):
    return "—" if value is None else f"${value:,.4f}" if 0 < abs(value) < 1 else f"${value:,.2f}"


class MessageScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Back"), ("q", "dismiss", "Back")]
    def __init__(self, title, content):
        super().__init__()
        self.heading, self.content = title, content

    def compose(self):
        with Vertical(id="message-dialog"):
            yield Static(self.heading, classes="dialog-title", markup=False)
            with VerticalScroll():
                yield Static(self.content, markup=False, id="message-content")
            yield Button("Back  [Esc]", id="back")

    @on(Button.Pressed, "#back")
    def back(self):
        self.dismiss()


class Filters(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Cancel")]
    def __init__(self, prefs, categories, floor):
        super().__init__()
        self.prefs, self.categories, self.floor = prefs, categories, floor

    def compose(self):
        with Vertical(id="filters-dialog"):
            yield Static("Filters & paper order size", classes="dialog-title")
            with VerticalScroll():
                yield Label("Category (Other = unclassified metadata)")
                options = [("All", "All")] + [(c, c) for c in sorted(set(self.categories) - {"All"})]
                yield Select(options, value=self.prefs["category"] if self.prefs["category"] in dict(options) else "All", allow_blank=False, id="category")
                for label, key, widget in (("Minimum remaining exposure ($)", "min_value", "min-value"),
                                           ("Minimum admitted holders", "min_holders", "min-holders"),
                                           ("Change within minutes (0 = all)", "recent_minutes", "recent"),
                                           ("Order/paper budget incl. fees ($)", "size", "budget")):
                    yield Label(label)
                    yield Input(str(self.prefs[key]), id=widget)
                yield Checkbox("Pinned markets only", value=self.prefs["pinned_only"], id="pinned-only")
            yield Static("Negative-overall-PnL exclusion stays mandatory.", classes="muted")
            yield Static("", id="validation", markup=False)
            with Horizontal():
                yield Button("Apply", variant="primary", id="apply")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed)
    def pressed(self, event):
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "apply":
            try:
                values = {"category": self.query_one("#category", Select).value,
                          "min_value": float(self.query_one("#min-value", Input).value),
                          "min_holders": int(self.query_one("#min-holders", Input).value),
                          "recent_minutes": int(self.query_one("#recent", Input).value),
                          "size": float(self.query_one("#budget", Input).value),
                          "pinned_only": self.query_one("#pinned-only", Checkbox).value}
                if not self.floor <= values["min_value"] <= 1e10 or not 1 <= values["min_holders"] <= 10000 or not 0 <= values["recent_minutes"] <= 10080 or not 1 <= values["size"] <= 1e6:
                    raise ValueError()
            except (ValueError, TypeError):
                self.query_one("#validation", Static).update(f"Use finite values. Exposure floor ${self.floor:,.0f}; holders ≥1; budget $1–$1m.")
                return
            self.dismiss(values)


class Detail(MessageScreen):
    BINDINGS = [*MessageScreen.BINDINGS, ("o", "open_market", "Open market"), ("r", "refresh", "Refresh quote")]
    def __init__(self, engine, row, kind):
        self.engine, self.row, self.kind = engine, row, kind
        super().__init__("Details · Esc back · o open · r refresh", "")

    def on_mount(self):
        self.refresh_content()
        self.set_interval(1, self.refresh_content)

    def refresh_content(self):
        engine, row = self.engine, self.row
        if self.kind == "wallets":
            wallet = row["address"]
            if engine.excluded(wallet):
                content = Text("Account disqualified: negative overall PnL. No further monitoring.")
            else:
                content = wallet_detail(engine.wallets[wallet], engine.positions.wallet_rows(wallet), engine)
            self.query_one("#message-content", Static).update(content)
            return
        token = str(row.get("token_id"))
        engine.execution.select(token, row)
        content = Text(clean(row.get("title", "")) + "\n" + clean(row.get("outcome", "")) + "\n\n", "bold")
        content.append(f"Remaining exposure {money(row.get('held'))} · holders {row.get('holders', '—')} · category {row.get('category', 'Other')}\n")
        content.append("Market mark and executable book are separate. Estimates can change before an order fills.\n\n")
        book = engine.execution.books.get(token)
        if book and time.time() - book["checked_at"] <= 15:
            content.append(f"BOOK checked {age(book['checked_at'])} · bid {book['bid']} · ask {book['ask']} · spread {book['spread']}\n", "cyan")
            content.append("Paper/order budget includes estimated taker fees.\n")
            for size in sorted(set((100, engine.preferences["size"], 5000))):
                fill = estimate(book, budget=size)
                if fill.get("average") is not None:
                    content.append(f"{money(size):>9} → {fill['shares']:,.2f} shares · avg {fill['average']:.4f} · fees {cash(fill['fees'])} · slippage {fill['slippage']:.4f} · {fill['status']}\n")
                else:
                    content.append(f"{money(size)} → {fill['status']}\n")
            fill = estimate(book, budget=engine.preferences["size"])
            if fill.get("shares"):
                exit_fill = estimate(book, shares=fill["shares"])
                content.append(f"Immediate exit of that size: {exit_fill['status']} · net proceeds {cash(exit_fill.get('cash'))}\n")
            content.append(f"Active trading asset: {book['asset_id']}\n\n", "dim")
        else:
            content.append("Fetching a current executable book… " + clean(engine.execution.error) + "\n\n", "yellow")
        content.append("CURRENT WALLET POSITIONS\n", "bold")
        for p in row.get("positions", []):
            wallet = p["proxy_wallet"]
            if not engine.allowed(wallet):
                continue
            profile = engine.wallets[wallet]
            content.append(f"{clean(profile.get('name') or wallet[:12])}\n")
            content.append(f"  Held {money(p['net_value'])} · entry {p.get('avg_price')} → mark {p.get('current_price')} · overall PnL {money(profile.get('account_pnl'))}\n")
            content.append(f"  Holding checked {age(p['verified_at'])} · account PnL observation {age(profile.get('pnl_observed_at'))}\n")
        self.query_one("#message-content", Static).update(content)

    def action_refresh(self):
        self.engine.execution.watch(self.row["token_id"], self.row, urgent=True)

    def action_open_market(self):
        slug = self.row.get("event_slug") or next((p.get("event_slug") or p.get("slug") for p in self.row.get("positions", []) if p.get("event_slug") or p.get("slug")), None)
        if slug:
            webbrowser.open("https://polymarket.com/event/" + slug)


class WatchApp(App):
    TITLE = "Polywatch"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: #0b1117; color: #d9e2ee; }
    #status { height: 2; padding: 0 1; background: #132235; color: #8dd7fa; }
    #hint { height: 1; padding: 0 1; color: #8497aa; }
    #search { height: 3; margin: 0 1; }
    TabbedContent { height: 1fr; }
    TabPane { padding: 0 1; }
    DataTable { height: 1fr; background: #0b1117; }
    DataTable > .datatable--header { background: #16283b; text-style: bold; }
    #footer { height: 2; padding: 0 1; background: #132235; color: #a5bdd2; }
    ModalScreen { align: center middle; background: #000000 65%; }
    #message-dialog { width: 96%; height: 94%; border: round #4a9abe; background: #0b1117; padding: 0 1; }
    #message-dialog VerticalScroll { height: 1fr; }
    #message-dialog Button { height: 3; }
    .dialog-title { height: 2; color: #8dd7fa; text-style: bold; }
    #filters-dialog { width: 70; max-width: 96%; height: 26; max-height: 98%; padding: 0 2; background: #101c29; border: round #4a9abe; }
    #filters-dialog VerticalScroll { height: 1fr; }
    #filters-dialog Input { height: 3; }
    #filters-dialog Select { height: 3; }
    #filters-dialog Horizontal { height: 3; }
    #validation { color: #ffb86b; height: auto; }
    .muted { color: #8497aa; }
    """
    BINDINGS = [Binding("q", "quit", "Quit"), Binding("1", "tab('markets')", "Markets"),
                Binding("2", "tab('wallets')", "Wallets"), Binding("3", "tab('alerts')", "Alerts"),
                Binding("slash", "search", "Search"), Binding("f", "filters", "Filters"),
                Binding("space", "freeze", "Freeze"), Binding("asterisk", "pin", "Pin"),
                Binding("s", "sort", "Sort"), Binding("d", "diagnostics", "Status"),
                Binding("v", "paper", "Paper"), Binding("question_mark", "help", "Help"),
                Binding("j", "down", "Down", show=False), Binding("k", "up", "Up", show=False)]

    def __init__(self, engine, seconds=None):
        super().__init__()
        self.engine, self.seconds = engine, seconds
        self.dashboard = Dashboard(engine)
        self.data = {"markets": {}, "wallets": {}, "alerts": {}}
        self.selection = {k: None for k in self.data}
        self.signatures = {}
        self.frozen_alerts = None

    def compose(self) -> ComposeResult:
        yield Static("Starting scanner…", id="status", markup=False)
        yield Static("", id="hint", markup=False)
        yield Input(value=self.engine.preferences["query"], placeholder="Search markets, outcomes or wallet names  [/ to focus · Esc back]", id="search")
        with TabbedContent(initial=self.engine.preferences["tab"], id="tabs"):
            for tab, label in (("markets", "Markets"), ("wallets", "Wallets"), ("alerts", "Alerts")):
                with TabPane(label, id=tab):
                    yield DataTable(id="table-" + tab, cursor_type="row", zebra_stripes=True)
        yield Static("1–3 Tabs  ↑↓ Select  Enter Details  / Search  f Filters  Space Freeze\ns Sort  * Pin  d Status  v Paper  ? Help  q Quit", id="footer", markup=False)

    def on_mount(self):
        self.configure_tables()
        self.set_interval(1, self.refresh_data)
        self.refresh_data()
        self.table().focus()
        if self.seconds:
            self.set_timer(self.seconds, self.exit)

    def table(self):
        tab = self.query_one("#tabs", TabbedContent).active
        return self.query_one("#table-" + tab, DataTable)

    def configure_tables(self):
        width = self.size.width
        market_width = max(20, width - 53)
        definitions = {"markets": [("Market / outcome", market_width), ("Held", 10), ("Holders", 7), ("Δ 1h", 10), ("Checked", 8)],
                       "wallets": [("Wallet", max(18, width - 48)), ("Overall PnL", 14), ("Active", 12), ("PnL age", 10)],
                       "alerts": [("Signal / market", market_width), ("Outcome", 10), ("Wallet", 15), ("Age", 8)]}
        for tab, columns in definitions.items():
            table = self.query_one("#table-" + tab, DataTable)
            table.clear(columns=True)
            for name, w in columns:
                table.add_column(name, width=w)
        self.signatures.clear()

    def on_resize(self):
        if self.is_mounted:
            self.configure_tables()
            self.refresh_data()

    def save(self):
        self.engine.store.save_state("ui_preferences", self.engine.preferences)
        self.engine.store.db.commit()

    def refresh_data(self):
        if not self.is_mounted:
            return
        e = self.engine
        feeds = list(e.feeds.values())
        errors = [f.error for f in feeds if f.error]
        state = "ERROR" if errors else "CATCHING UP" if any(f.backlog for f in feeds) else "LIVE" if all(f.last_ok for f in feeds) else "STARTING"
        mode = "FROZEN VIEW · scanning continues" if self.dashboard.frozen is not None else state
        counts = {s: sum(account_status(p) == s for p in e.wallets.values()) for s in ("eligible", "pending", "excluded")}
        feed_age = age(max((f.latest for f in feeds), default=0))
        self.query_one("#status", Static).update(f"POLYWATCH  {mode}  · feed {feed_age} · holdings {age(e.positions.last_ok)}\nAccounts: {counts['eligible']} admitted · {counts['pending']} pending · {counts['excluded']} excluded · paper {len(e.paper.records)} active")
        prefs = e.preferences
        self.query_one("#hint", Static).update(f"{prefs['category']} · ≥{compact_money(prefs['min_value'])} · holders ≥{prefs['min_holders']} · sort {prefs['sort']} · {len(prefs['pins'])} pins")
        markets = self.dashboard.markets()
        wallets = self.dashboard.wallets(markets)
        alerts = self.frozen_alerts if self.frozen_alerts is not None else self.dashboard.alerts()
        alerts = [a for a in alerts if e.allowed(a["proxy_wallet"])]
        for tab, rows in (("markets", markets), ("wallets", wallets), ("alerts", alerts)):
            table = self.query_one("#table-" + tab, DataTable)
            values = []
            for row in rows:
                if tab == "markets":
                    cells = (("★ WATCH " if row.get("watch_only") else "★ " if row["pinned"] else "") + clean(row["title"] + " / " + row["outcome"]), "—" if row.get("watch_only") else compact_money(row["held"]), str(row["holders"]), compact_money(row["flow"]), "—" if row.get("watch_only") else age(row["verified_at"]))
                elif tab == "wallets":
                    cells = (clean(row.get("name") or row["address"][:16]), compact_money(row.get("account_pnl")), compact_money(row["active_value"]), age(row.get("pnl_observed_at")))
                else:
                    wallet = e.wallets.get(row["proxy_wallet"], {})
                    cells = (clean(row["kind"] + " · " + row.get("title", "")), clean(row.get("outcome")), clean(wallet.get("name") or row["proxy_wallet"][:12]), age(row["at"]))
                values.append((row["id"], cells))
            signature = tuple(values)
            self.data[tab] = {r["id"]: r for r in rows}
            if signature != self.signatures.get(tab):
                selected = self.selection.get(tab)
                table.clear()
                for key, cells in values:
                    table.add_row(*cells, key=key)
                keys = [k for k, _ in values]
                index = keys.index(selected) if selected in keys else min(table.cursor_row, max(0, len(keys) - 1))
                if keys:
                    table.move_cursor(row=index)
                    self.selection[tab] = keys[index]
                else:
                    self.selection[tab] = None
                self.signatures[tab] = signature

    @on(DataTable.RowHighlighted)
    def highlighted(self, event):
        tab = event.data_table.id.removeprefix("table-")
        key = event.row_key.value
        if key not in self.data[tab]:
            return
        self.selection[tab] = key
        row = self.data[tab].get(key)
        if row and tab == "markets":
            self.engine.execution.select(row["token_id"], row)

    @on(DataTable.RowSelected)
    def selected(self, event):
        tab = event.data_table.id.removeprefix("table-")
        row = self.data[tab].get(event.row_key.value)
        if row:
            if tab == "wallets":
                self.engine.selected = row["address"]
                self.engine.positions.request(row["address"])
            self.push_screen(Detail(self.engine, row, tab))

    @on(Input.Changed, "#search")
    def searched(self, event):
        self.engine.preferences["query"] = event.value[:200]
        self.refresh_data()
        self.save()

    def on_key(self, event):
        if event.key == "escape" and isinstance(self.focused, Input) and self.focused.id == "search":
            self.table().focus()

    def action_tab(self, tab):
        self.query_one("#tabs", TabbedContent).active = tab
        self.engine.preferences["tab"] = tab
        self.save()
        self.call_after_refresh(self.table().focus)

    @on(TabbedContent.TabActivated)
    def tabbed(self, event):
        self.engine.preferences["tab"] = event.pane.id
        self.save()

    def action_search(self):
        self.query_one("#search", Input).focus()

    def action_down(self):
        self.table().action_cursor_down()

    def action_up(self):
        self.table().action_cursor_up()

    def action_freeze(self):
        self.dashboard.freeze()
        self.frozen_alerts = self.dashboard.alerts() if self.dashboard.frozen is not None else None
        self.refresh_data()

    def action_sort(self):
        choices = ("exposure", "holders", "change", "profit")
        prefs = self.engine.preferences
        prefs["sort"] = choices[(choices.index(prefs["sort"]) + 1) % len(choices)]
        self.save()
        self.refresh_data()

    def action_pin(self):
        row = self.data["markets"].get(self.selection.get("markets"))
        if row:
            pins = self.engine.preferences["pins"]
            contexts = self.engine.preferences["pin_contexts"]
            token = row["token_id"]
            existing = next((p for p in pins if p == token or contexts.get(p, {}).get("id") == row["id"]), None)
            if existing:
                pins.remove(existing)
                contexts.pop(existing, None)
            elif len(pins) < 50:
                pins.append(token)
                contexts[token] = {k: row[k] for k in ("id", "title", "outcome", "condition_id", "token_id", "outcome_index", "category") if k in row}
            self.save()
            self.refresh_data()

    def action_filters(self):
        categories = [m.get("category") or "Other" for m in self.engine.positions.markets.values()]
        def apply(result):
            if result:
                self.engine.preferences.update(result)
                self.save()
                self.refresh_data()
        self.push_screen(Filters(self.engine.preferences, categories, self.engine.threshold), apply)

    def action_help(self):
        self.push_screen(MessageScreen("Keyboard help", "1 / 2 / 3  Markets / Wallets / Alerts\nArrow keys or j/k  Move selection\nEnter  Open full details\n/  Search; Esc returns to table\nf  Filters and paper/order size\ns  Cycle exposure / holders / change / account profit\n*  Pin selected market (saved)\nSpace  Freeze display; background scanning continues\nv  Paper results, including skipped signals\nd  Diagnostics and data freshness\no in details  Open market in browser\nr in details  Refresh execution quote\nq  Quit safely\n\nOnly verified nonnegative-overall-PnL accounts are admitted. Freezing never suspends that account exclusion. Quote fills are estimates; no real orders are submitted."))

    def action_diagnostics(self):
        e = self.engine
        lines = [f"Requests {e.api.requests} · retries {e.api.retries} · cap {1/e.api.spacing:g}/s",
                 f"Positions cached {len(e.positions.rows)} (bounded working set; history is in SQLite)",
                 f"Profile queue {e.queue.qsize()}/200 · position jobs {len(e.positions.pending)} · book targets {len(e.execution.targets)}/64",
                 f"Paper active {len(e.paper.records)}/125; at most 25 open entries",
                 "", "Position checks expire after 120s; market checks expire after 120s.",
                 "Account checks expire after 5m; their source observation must be within 1h.",
                 "Feed/holdings freshness and account-PnL observation freshness are separate.",
                 ""]
        lines += [f"{f.name}: response {age(f.last_ok)} · latest trade {age(f.latest)} · pages {f.pages} · backlog {f.backlog} · {f.error}" for f in e.feeds.values()]
        lines += ["", "RECENT NOTICES", *e.notices, e.positions.error, e.positions.market_error, e.execution.error]
        self.push_screen(MessageScreen("Diagnostics", "\n".join(clean(l) for l in lines)))

    def action_paper(self):
        rows = self.engine.store.papers(limit=100)
        closed = [r for r in rows if r["status"] == "closed"]
        lines = ["SIMULATIONS ONLY · no orders are submitted", "Entry uses a fresh post-detection ask book and estimated taker fees.",
                 "Exit uses executable bids or confirmed resolved payout. Depth failures stay unpriced.",
                 "Entry at detection; exit on source closure/disqualification or 24h.",
                 "5m/1h/6h/24h marks are first available observations after each horizon.",
                 f"Last {len(rows)} signals · closed {len(closed)} · PnL of these closed simulations {cash(sum(r.get('pnl', 0) for r in closed))}", ""]
        for r in rows:
            stale = r.get("mark_at") and time.time() - r["mark_at"] > 30 and r["status"] == "open"
            label = "stale mark" if stale else cash(r.get("pnl"))
            lines.append(f"{r['status'].upper():8} {clean(r.get('title'))} / {clean(r.get('outcome'))}\n  Budget {money(r['budget'])} · PnL {label} · latency {r.get('latency', 0):.2f}s · {clean(r.get('reason') or r.get('mark_warning', ''))}")
            marks = r.get("marks", {})
            lines.append("  " + " · ".join(f"{name} {money(marks.get(str(horizon), {}).get('pnl'))}" for name, horizon in (("5m", 300), ("1h", 3600), ("6h", 21600), ("24h", 86400))) + f" · worst observed {money(r.get('worst_pnl'))}")
        self.push_screen(MessageScreen("Prospective paper results", "\n".join(lines)))
