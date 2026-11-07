# Polymarket Watch

A Python watcher and Raspberry Pi web research dashboard centered on **meaningful positions that wallets still hold in tradable markets**. Covers all prediction-market categories, $10,000 big trades, and equal discovery of whales and historically profitable accounts. Uses public, read-only APIs; no credentials or wallet connection. Existing environment files are not read.

## Web research dashboard

```sh
.venv/bin/python -m pip install -e .
.venv/bin/python watch.py --web --host 0.0.0.0 --port 8080
```

Open `http://<raspberry-pi-ip>:8080` on your local network. Without `--host`, the web interface binds to `127.0.0.1`. No Node.js or frontend build is needed. Terminal mode remains available by omitting `--web`; run only one mode per database.

The responsive interface includes **Top traders**, **Market radar**, **Activity log**, **Paper lab**, **Research archive**, and **System**. Save a persistent shortlist, manually add public wallets, inspect individual trader evidence, change future paper budgets, and export complete research datasets. Freshness, unknown values, insufficient evidence, skipped signals, and excluded sources are explicit.

The long-term screen requires 90 days of locally observed PnL, 60 covered daily comparisons, at least 30 active days with 60% positive, positive observed net PnL, fresh nonnegative account verification, recent passing historical analysis, and 20 fully priced profitable-in-aggregate simulated closes spanning 30 days. Each requirement is shown separately. Imported API history cannot substitute for locally observed evidence. No live copy orders are submitted.

Research records now persist indefinitely. [Raspberry Pi setup, boot service, retention details, and backup instructions](deploy/README.md) explain the deployment and methodology. Browser assets are packaged with the Python application.

## Run

Install with Python 3.11+ on macOS or Linux:

```sh
git clone https://github.com/ntodst/polywatch.git
cd polywatch
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python watch.py
```

The installed command `.venv/bin/polywatch` also works. Stop with **q** or **Ctrl+C**. The dashboard runs while the process is open.
On macOS, double-click `run_watch.command` to open it in Terminal.

## Controls

| Key | Action |
| --- | --- |
| `1` / `2` / `3` | Markets / Wallets / Alerts tabs |
| Arrow keys or `j` / `k` | Move the stable row selection |
| `Enter` | Scrollable market or wallet details |
| `/` | Focus live search; `Esc` returns to the table |
| `f` | Category, minimum exposure, holder count, recency, pinned-only and order-budget settings |
| `s` | Cycle exposure / holders / latest change / aggregate account-profit sorting |
| `*` | Pin/unpin the selected market (saved across launches) |
| `Space` | Freeze the visual snapshot while scanning continues |
| `d` | Diagnostics, errors, cache sizes and separate data freshness clocks |
| `v` | Prospective paper results, including skipped and losing signals |
| `?` | Keyboard help |
| `o` in market details | Open the market in the browser |
| `r` in market details | Request a fresh executable book |
| `Esc` in details/dialogs | Return to the dashboard |
| `q` | Exit and save history |

The interactive UI uses Textual and targets **100 columns × 28 rows**. Tables resize and scroll, and the full market name remains visible in details. Search, filters, sorting, selected tab, pinned markets and simulation order size are saved in the database. Historical details are loaded separately from the compact main view. A second watcher using the same database exits with a clear single-instance message. Wallet details and JSON exports also work as standalone commands:

```sh
.venv/bin/python watch.py --wallet 0x56687bf447db6ffa42ffe2204a05edaa20f55839
.venv/bin/python watch.py --once
.venv/bin/python watch.py --seconds 30 --json > snapshot.json
.venv/bin/python watch.py --threshold 10000 --interval 1 --wallets 50
```

## Compact interface and resource use

The default Markets tab groups verified remaining exposure by condition and outcome, with admitted holder counts, observed one-hour changes and the oldest holding-verification age in each group. Wallets shows exposure within the currently filtered markets. Alerts combines significant observed fills with confirmed position changes; initial FIRST SEEN baselines are omitted from that alert list. Category labels come from available market metadata, with sports classified from the market's sports type; unclassified metadata is shown as Other.

Selection follows a stable market/wallet identifier when values reorder. Freeze captures the visual positions and alert list; the scanner and paper tracker continue. Account admission remains live during a freeze, so disqualified accounts are still removed. A pinned market whose qualifying holders disappear remains a clearly labeled WATCH row with no asserted held exposure. It is a market bookmark, not continued monitoring of an excluded wallet.

The app refreshes its dashboard about once a second and shares one active-book snapshot across its tables. It does not decode the entire persisted position catalog at startup. Current rows use compact financial fields and a bounded 6,000-row cache, with complementary conditions evicted together. Older position detail stays in SQLite. Position jobs are capped at 256, the profile queue at 200, and order-book targets/books/market-info caches at 64 each. SQLite writes are batched within updates; paper records are written on changes, and passive WAL checkpoints run with maintenance. The sub-250-MiB memory figure is a design target, not a measured guarantee.

## Execution estimates and prospective paper tracking

Market details fetch current CLOB bids and asks, walk the available depth, and show estimated average fill price, slippage, fees and immediate executable exit proceeds. The chosen order budget **includes estimated taker fees**. Partial depth, minimum-size failures and unknown fees are shown explicitly. The CLOB's fee-curve parameters are preferred; a nonzero legacy base-fee number alone is not assumed to specify a fee curve. Active trading identifiers are resolved from the market's outcome slot for migrated instruments. Quotes target a three-second refresh for the selected outcome and fifteen seconds for other actively watched paper outcomes, subject to shared API pacing and latency. They are estimates, not guaranteed fills.

Paper tracking is read-only and enabled for newly detected qualifying BUY alerts. It never submits orders, uses private keys or connects a wallet. Admission and the account-PnL observation are captured at **detection time**. Catch-up trades older than two minutes do not create retroactive entries. An entry needs an accepting market, known fees and a fully fillable ask book fetched **after** detection. A signal without a usable quote within 45 seconds is recorded as skipped; later favorable prices cannot backfill it. Existing paper exposure in the same source-wallet/outcome and capacity limits also produce explicit skipped records. Changing the order budget affects future signals only.

Open simulations are marked using executable bid depth after estimated exit fees; insufficient exit depth leaves the full position unpriced. The record retains the first available post-horizon observations at 5m, 1h, 6h and 24h, plus worst/best observed mark PnL. These observations are not continuous intraperiod extrema. Simulations exit on a confirmed source exit, source disqualification or 24h, using a quote fetched after the exit decision. A confirmed resolved token payout can close a simulation as a simulated redemption. An elapsed market end date alone never creates a settlement payout.

There are at most 25 new open paper positions and 125 active/queued records. Simulations survive restarts. Completed/skipped paper records are retained permanently. Paper history includes results from subsequently disqualified sources to avoid hiding losing signals; those accounts receive no further dedicated wallet polling. The paper panel summarizes the latest 100 records, not an all-time audited trading return. Estimated fees, snapshot liquidity and polling latency cannot reproduce exchange queue priority or future market impact.

## Scanning and freshness

Two independent global trade pollers target a **one-second interval**: a cash-filtered $10,000 feed, and the unfiltered feed for tracked activity and split buys. All categories are included. Both request maker and taker wallet rows; their BUY/SELL fields are interpreted from the named wallet's perspective. Opposite wallet sides are separate wallet observations, not additive market-volume estimates.

Requests share a **10 requests/second** ceiling. Cursor pagination, response latency, and server-directed backoff can lengthen actual intervals. Large-trade scanning continues while the full feed catches up. The app doesn't use the public market WebSocket for wallet attribution because that stream doesn't expose wallet addresses. No unsupported legacy RTDS trade subscription is assumed.

Holdings verification runs independently of historical performance analysis. **Every newly observed tracked-wallet trade**, including a small sell, queues a priority check of that wallet and market unless a fresh balance's ledger clock already covers it. Requests are coalesced by wallet/condition and repeated fills are deduplicated. Catch-up trades older than two minutes coalesce into present-day wallet checks, keeping old markets out of the live priority queue. A separate rotation targets a full open-book refresh every **30 seconds**; large books and API load can lengthen this. Up to three targeted jobs run before giving a waiting baseline job a turn. Baselines are rechecked after every restart.

Position and market checks have independent timestamps. After **120 seconds** without fresh coverage, a position leaves the active view. Market metadata is batched from Gamma and targets a **60-second** refresh. A scheduled past end date, resolved/redeemable holding, closed or archived market, disabled order book, or a market not explicitly accepting orders excludes it from the main screen. An elapsed scheduled end is a relevance filter, not proof of resolution. Unknown metadata is never assumed tradable.

## Current positions and changes

**Mandatory account filter:** a wallet must have verified **nonnegative overall account position PnL** before its positions, trades, alerts, or history appear in the watcher. Overall PnL is the Data API's atomic `all_time_pnl.position_pnl` from `/v2/user-stats`: lifetime `realized_pnl + unrealized_pnl`, including the publisher's standard, combo, and LP position accounting. Deposits, account balances, and rewards are not substituted for profit. The components must be available and agree with the published total. Positive realized profit alone cannot qualify an account with larger open losses. Exactly zero is allowed; it does not count as a profitable holder.

Admission runs before holdings or detailed historical analysis. Unknown, inconsistent, or stale account data remains pending and produces no published trade alerts or active positions. Approved candidates get a lightweight account check about every minute. A check expires after five minutes; the API's underlying PnL observation must also be no older than one hour. The timestamp shown in wallet details is the publisher's actual observation time, which may lag current trades and marks. The stats endpoint rejects the usual cache-busting parameter, so it is queried with its supported `user` parameter and the no-cache header; upstream publishing/CDN lag still applies.

Once verified negative, the wallet is **disqualified persistently across restarts**: no further dedicated holdings/history/PnL polling, new alerts, holder counts, or automatic readmission through the leaderboard or big-bet feed. Old database records remain available for audit but are hidden from watcher views and JSON activity lists. The unavoidable global trade feed is still scanned to discover other wallets; rows from known rejected addresses are discarded before aggregation and storage. Existing exclusions are not automatically rechecked, including through `--wallet`.

The default view lists outcome positions, not empty historical winners. Its $10,000 size floor applies to **net remaining marked value**, after offsetting complementary shares held by the same wallet. A large original entry cost alone does not qualify a holding whose present value has fallen below the floor. The threshold and all other settings use the same CLI options as trade scanning.

Equal YES and NO shares offset each other even when their dollar marks differ. Fully paired holdings are hidden; partial hedges show remaining shares and marked value. Unknown opposite-side coverage excludes the position until it is checked. Offsets cover fetched complementary tokens within one wallet; they do not establish a person's exposure across other wallets, related markets, or external hedges.

Changes are derived from balance snapshots:

- **FIRST SEEN:** a holding found without a preceding confirmed zero. An initial scan does not pretend old positions were just opened.
- **OPENED:** a positive balance after a confirmed zero, or a new acquisition whose recorded entry time follows a complete preceding baseline.
- **ADDED / REDUCED:** confirmed increase/decrease in shares.
- **EXITED:** an explicit zero balance, including a closed-position record. This describes holdings, not necessarily a sell; merges and redemptions can also remove shares.

A trade triggers verification rather than immediately changing the displayed balance. If the position ledger's event timestamp is behind the observed fill, the previous confirmed balance is retained as pending; no change event is emitted yet. Missing balances, empty results, partial pages, errors, and unavailable markets never manufacture a zero or an exit. Missing previous holdings get a targeted closed-position check. The main view excludes pending snapshots. Checks retry with backoff; still-unknown balances stay unverified and remain available in wallet details.

Open-book checks are capped at **three pages of 1,000 rows**, ordered by current value. Partial books cannot establish absence. Meaningful rows without opposite-side coverage trigger targeted checks. All fetched position snapshots are persisted; they are no longer reduced to the thirty largest profile rows. Full-book freshness and coverage are shown in the JSON snapshot. Non-trade changes such as transfers and redemptions are picked up on the holdings rotation; no separate transfer WebSocket is assumed.

**Profitable holders** counts admitted tracked wallets with **positive overall account PnL** that currently hold verified meaningful, unpaired exposure in that outcome. The count drops when an account becomes overall negative, exits, becomes pending/stale, or loses qualifying exposure. It is a current holdings count rather than proof of coordinated buying or future returns. The legacy historical CO-BUY alert is no longer generated by the scanner; old database alerts remain history.

Entry → mark compares the position's weighted average entry with its latest fetched mark. It is **not** an executable order-book ask, spread, or liquidity estimate. Changes show detection time and verification age. Wallet details include settled and unverified balances with explicit labels; the main view includes only positions that pass its freshness and market filters.

Live URLs use a unique query value plus a no-cache header because the API's CDN otherwise serves cached feed heads. The screen reports time since the last successful response, newest observed trade age, catching-up/error state, returned CDN Age, and API health snapshot age. Trade age on the cash-filtered feed can be high simply because no qualifying trade happened recently. A successful request is not proof that the upstream index is fresh.

The initial scan covers about **30 seconds of ordinary trades and five minutes of big trades**, with a 30-second overlap. Subsequent scans walk back to the previous watermark and overlap again to catch late indexing. A deep backlog retains its cursor between polling turns and across restarts. Downtime is caught up instead of silently skipped. This is public API near-real-time monitoring, not an exchange-direct or exhaustive on-chain index. Indexing delays beyond the overlap can miss late rows; there is no guarantee of every fill.

## Wallet discovery and performance

Every five minutes, discovery allocates **25 slots to profit leaders** (alternating lifetime and monthly boards) and **25 to monthly volume leaders**. Accounts can qualify for both, so the unique count can be below 50. Previously unknown wallets making qualifying large trades are added as observed whales. All tracked wallets' observed buys and sells appear even below $10,000. Profiles are enriched in the background; pending fields display `—`, never invented zeros. Board figures are displayed immediately. Heavy historical analysis refreshes when requeued after **30 minutes**, independently of holdings verification; selected wallets can be refreshed with `r`.

- **All-time realized:** the API's lifetime PnL leaderboard value.
- **Month marked:** the monthly board's equity change net of flows, including changes in unrealized value. It is not monthly realized profit.
- **Active value:** sum of verified, qualifying unpaired marked exposure. The separate portfolio snapshot in details comes from the value endpoint and is not a cash balance or total account wealth. Whale discovery uses the API's volume board; volume is in **shares**, not dollars. Rankings apply to discovered candidates, not every wallet on Polymarket.
- **Open PnL:** summed unrealized PnL across fetched open positions, including archived markets. A truncated or uncovered open book is shown as unknown. The API can exclude inactive markets even with archived inclusion.
- **Closed markets:** fetched closed outcomes grouped by condition, so opposite sides of the same market aren't counted as independent wins. A `+` means the page cap was reached. Win rate excludes roughly breakeven markets and describes closed results only; it is not a probability of future success.
- **Consistency PASS:** a transparent historical screen: complete fetched closed book with at least 30 distinct conditions, positive lifetime realized and monthly marked PnL, at least 10 active daily samples in the last month, at least 60% of those days positive, covered open PnL, and lifetime realized plus open PnL positive. Daily changes use the API's position PnL series. This is an observed consistency filter, not proof of skill or a guarantee of profitability. Closed-book statistics cover standard positions; headline leaderboard PnL can also include combos.

Closed-book history is capped at ten pages of 1,000 rows. Partial closed books cannot pass the consistency screen. Profile scoring uses the independently verified open book; missing or partial coverage leaves open PnL unknown. It does not infer opening/closing intent from a fill alone.

## Alerts and persistence

- **BIG BET:** individual wallet fill worth at least $10,000 (`shares × execution price`). Buys and sells qualify.
- **ACCUMULATION:** at least two observed buys by the same wallet in the same outcome totaling at least $10,000 over five minutes. Repeats suppressed for five minutes. This may overlap a big-bet alert.

SQLite at `data/polywatch.sqlite3` stores watermarks, profiles, alerts, trades, latest position snapshots, verification metadata, and position-change history, plus local account-PnL observations, imported historical PnL, and a research journal. Existing databases are extended automatically without deleting history. Big/tracked-wallet trades, alerts, meaningful position changes, and paper records are retained permanently. Other raw trade observations are retained for 15 minutes for aggregation. Position, profile, and market economic-state changes are journaled, with one unchanged daily baseline; imported closed positions are journaled on change. Latest snapshots and metadata persist across restarts, with freshness filters still applied. Previously deleted history cannot be recovered. Cleanup runs with health checks. Trade amounts are gross fill notional, not fee-adjusted spending. Shared-market trades are not summed into a platform volume total.

Trade identity includes transaction hash, wallet, outcome token, side, size, price, and timestamp. The API doesn't expose a fill/log index: byte-identical economic fills in the same transaction may be indistinguishable and collapse. Wallet addresses are identities for this tool; it does not claim to identify people or link accounts belonging to one person. The global prediction-market trade feed is scanned; dedicated combo RFQ feeds and perpetuals are not separately monitored. Combo structural condition IDs observed in the global trade feed are excluded from standard position-query batches; their trade history may appear, but their holdings remain unverified and are never presented as active standard-market positions.

## Verification

```sh
.venv/bin/python -m pytest -q
.venv/bin/python watch.py --seconds 30 --json
```

Tests cover thresholds, restart deduplication, split buys, out-of-order data, ledger lag, explicit exits, partial/empty snapshots, hedged exposure, current-holder agreement, market availability and expiry, priority checks after small sells, metadata batching, terminal views, rate pacing, retries, and cursor catch-up. Live checks require access to `data-api.polymarket.com` and `gamma-api.polymarket.com`.

API contracts checked during implementation:

- [Data API v2 / interactive documentation](https://data-api.polymarket.com/v2/docs)
- [Data API v2 OpenAPI schema](https://data-api.polymarket.com/v2/openapi.json)
- [Public real-time market data](https://docs.polymarket.com/market-data/realtime-data)
- [Market WebSocket schema](https://docs.polymarket.com/asyncapi.json)
- [Gamma market metadata schema](https://docs.polymarket.com/api-spec/gamma-openapi.yaml)
