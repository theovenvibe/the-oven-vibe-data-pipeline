# The nightly sync

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
