# Syncing the warehouse

## The short version

```bash
ov sync
```

That pulls direct orders and the Dough ledger from D1, re-derives everything
from the Zomato exports on disk, and rebuilds the dashboard. **Chrome must be
signed in to the admin** — see below for why.

## What comes down

| | into | from |
|---|---|---|
| Confirmed direct orders + their items | `silver.direct_orders`, `silver.direct_order_items` | `/admin/api/export/orders` |
| Dough ledger, balances, referrals | `silver.dough_*`, `silver.referrals` | `/admin/api/export/dough` |
| **Everything else in D1, raw** — stock batches and moves, campaigns and claims, rejected orders, demand signals, kitchen open/close history, settings | **`d1.*`** | `/admin/api/export/tables` |
| Zomato weekly exports | `bronze.*`, `silver.orders`, `gold.*` | CSVs on disk |

The `d1` schema is its own stream. **Zomato is a separate business channel**
that happens to land in the same DuckDB file — nothing in `d1.*` is joined to
it, and nothing should be. `d1.stock_moves` means "the live table in
Cloudflare", never something derived from a CSV.

Two things are deliberately withheld from `d1.*`: the push subscription keys
(`endpoint`, `p256dh`, `auth`), which are credentials for sending
notifications to a device, and any table not on the allowlist in
`the-oven-vibe-backend/src/analytics-export.ts`.

The orders export carries **confirmed** orders only — a click is not an order —
and drops test orders, so `d1`-side counts and `silver.direct_orders` will not
match `SELECT COUNT(*) FROM orders` in D1. That is correct, not drift.

## The two locks, and why this needs a browser

`/admin/*` sits behind Cloudflare Access, so an `ADMIN_TOKEN` on its own gets
a **302 to a sign-in page**, not data. `pipeline/access.py` therefore sends
both: the bearer token from `.env`, and the `CF_Authorization` cookie read
straight out of your signed-in Chrome profile. Nothing is written to disk —
an Access session copied into a file is a login somebody else can use.

So the requirements for `ov sync` are:

1. `.env` exists with `OVEN_VIBE_ADMIN_TOKEN` (the value lives in
   `~/workbench/the-oven-vibe/CREDENTIALS.local.md`, outside every git repo)
2. You have signed into the admin in Chrome at some point in the last 30 days

If the session has expired, the sync stops and says so. It does **not**
continue with stale numbers.

`OVEN_VIBE_SKIP_D1=1 ov sync` rebuilds from the Zomato exports alone, on
purpose. Between 19 and 29 August that skip was the *default* and silent, so
the sync reported success while `silver.direct_orders` held zero rows. It is
an explicit choice now.

## What would make this unattended

A Cloudflare Access **service token** (`CF-Access-Client-Id` /
`CF-Access-Client-Secret`), created in the Cloudflare dashboard and sent as
headers by `pipeline/access.py`. With one, no browser is involved and the
timer below becomes worth installing. Without one, a 2:30am run cannot work,
because reading Chrome's cookie database needs Chrome closed and a human who
signed in this month.

## The nightly timer (not installed — needs the service token first)

Pulls Zomato CSVs, direct orders and the Dough ledger into `warehouse.duckdb`,
so every question is answerable from this laptop without a Cloudflare login.

## Set it up once

```bash
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/ov-sync.service <<'UNIT'
[Unit]
Description=The Oven Vibe — pull D1 into the local warehouse
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=%h/workbench/the-oven-vibe/the-oven-vibe-data-pipeline
ExecStart=%h/workbench/the-oven-vibe/the-oven-vibe-data-pipeline/sync-nightly.sh
UNIT

cat > ~/.config/systemd/user/ov-sync.timer <<'UNIT'
[Unit]
Description=Nightly Oven Vibe warehouse sync

[Timer]
OnCalendar=*-*-* 02:30:00
# The important line: if the laptop was off at 02:30, run once it is on again
# rather than waiting another whole day.
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now ov-sync.timer
# Survives logout — without this the timer only runs while a session is open.
sudo loginctl enable-linger "$USER"
```

## Check on it

```bash
systemctl --user list-timers ov-sync.timer   # when it last ran, when it runs next
journalctl --user -u ov-sync.service -n 30   # what happened
./sync-nightly.sh                            # run it now, by hand
```

**Offline is not an error.** The script pings the Worker first and exits 0 if it
cannot reach it. A laptop closed at 2:30am is normal, and an alert for it is
noise you would learn to ignore.

## Then ask it anything

```bash
./.venv/bin/python -c "
import duckdb; con = duckdb.connect('warehouse.duckdb', read_only=True)
print(con.execute(\"SELECT * FROM gold.dough_customers WHERE phone LIKE '%8895607686'\").fetchall())"
```

| Table | The question it answers |
|---|---|
| `gold.dough_customers` | Balance, expiry, code, earned and spent all-time, per customer |
| `silver.dough_ledger` | Every entry — *"where did my ₹20 go?"* |
| `silver.referrals` | Who brought whom, and whether it paid |
| `gold.dough_balance_check` | **Should always be empty.** A row means a cached balance disagrees with the ledger |
| `gold.combined_weekly_sales` | Zomato and direct, side by side |

**`gold.dough_balance_check` is the one to glance at.** The backend writes the
ledger row and the cached balance in a single transaction, so a row here means
either a bug or somebody edited the database by hand — and **the ledger is
always the one to believe.**
