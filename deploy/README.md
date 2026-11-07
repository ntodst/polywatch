# Raspberry Pi deployment

Use Raspberry Pi OS with Python 3.11 or later. This app uses one Python process,
SQLite, and static browser assets. Node.js, a frontend build, and a cloud database
are not required. Copy this updated project to the Pi; cloning an older version
will not include these changes.

From the project directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
mkdir -p data
.venv/bin/python watch.py --web --host 0.0.0.0 --port 8080
```

Open `http://<raspberry-pi-ip>:8080` from a device on the same network. The default
without `--host` is loopback only (`127.0.0.1`). The LAN interface has no sign-in:
use it on your trusted network. It is not intended to be exposed through a public
router port. The browser can close while the scanner continues running.

## Run at boot

Edit `deploy/polywatch.service` and replace **all** occurrences of `pi` and
`/home/pi/polywatch` with your Linux username and absolute project path. Keep
the `data` directory writable by that user. Then:

```sh
sudo cp deploy/polywatch.service /etc/systemd/system/polywatch.service
sudo systemctl daemon-reload
sudo systemctl enable --now polywatch
```

Service management:

```sh
sudo systemctl status polywatch
journalctl -u polywatch -f
sudo systemctl restart polywatch
sudo systemctl stop polywatch
```

Run one scanner per database. The terminal and web modes share the same database
lock. Stop the terminal watcher before starting the web service.

## What is retained

The default database is `data/polywatch.sqlite3`. Existing databases are extended
in place. Keep this directory when updating the source.

- Retained big/tracked trades, alerts, confirmed position changes, and all paper
  records are permanent, including losses, skips, and excluded sources.
- Verified overall account PnL is recorded under the publisher's observation
  timestamp. Repeated observations of that timestamp are deduplicated. Corrections
  replace that source observation; profile changes are separately journaled.
- Position, market, and profile economic-state changes are journaled, with one
  daily baseline for unchanged states. Verification-only clock changes do not
  duplicate the journal. Imported closed positions are journaled on change.
- Scanner diagnostics are retained in the research journal.
- Historical PnL imported from the API is stored separately from locally observed
  account PnL. Backfill cannot advance the 90-day observation requirement.
- The untracked global feed remains a temporary 15-minute aggregation buffer.
  The app does not archive every public API response or order-book tick, and it
  does not promise a complete on-chain trade history.

History already deleted by an older version cannot be recovered by migration.
The mandatory existing nonnegative-account admission rule is preserved. Once a
wallet is disqualified, its dedicated polling is stopped permanently; its prior
research and paper outcomes remain available for audit. This means the local
series ends rather than hiding that source's failure.

Research archive exports stream complete matching records as NDJSON. The trader
CSV exports all discovered wallets, including excluded and pending sources.
The live dashboard displays up to 500 ranked wallets, 100 market outcomes, and
100 recent activity/paper records. Archive browsing is paginated in SQLite.

## Reading the screen

A long-term pass requires all seven disclosed requirements: fresh nonnegative
account PnL, historical analysis within one hour, 90 days of local observation,
60 covered daily comparisons, positive net observed PnL with at least 30 active
days and 60% positive active days, the historical consistency screen, and at
least 20 fully priced simulated closes spanning 30 days with positive net PnL.
Missing evidence is shown in each trader's profile. The thresholds are in
`polywatch/research.py`; they are screening rules, not optimized predictions.

Local charts show changes in lifetime position PnL (realized plus unrealized),
not cash balances or investment ROI. Positive days and drawdown use the last
available source observation per UTC day. Only consecutive samples 20–28 hours
apart contribute to daily comparisons; missing days are not interpolated.
Dollar drawdown is daily-sampled and may miss intraday losses. Open-day samples
can change as that day's new observations arrive.

Paper simulations follow newly detected qualifying BUY alerts, estimate fees and
executable depth, and exit at source exit, disqualification, or 24 hours. They
measure that specific copy rule. Closed totals include all stored sources;
open marks and skipped/unpriced signals are displayed separately.

## Backup and disk usage

Storage grows with observed activity. The System screen reports database size
excluding the active WAL; retain space for both database and journal files.
For a consistent backup while the service is running, use SQLite's backup API
rather than copying only the live database file:

```sh
sqlite3 data/polywatch.sqlite3 '.backup /path/to/backup/polywatch.sqlite3'
```

Alternatively stop the service before copying the database. NDJSON exports are
research data exports, not a replacement for a full database backup with saved
preferences and scanner watermarks.

## Optional checks to run yourself

```sh
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q
.venv/bin/python watch.py --web --host 127.0.0.1 --port 8080 --seconds 30
```

Open the dashboard during the last command to check the UI and local API. Live
collection needs access to the public Polymarket Data, Gamma, and CLOB APIs.
