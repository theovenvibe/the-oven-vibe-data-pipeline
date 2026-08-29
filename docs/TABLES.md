# Every table in the warehouse

`warehouse.duckdb`, in `the-oven-vibe-data-pipeline/`. Refresh it all with
`ov sync`. Row counts below are from 2026-08-29 and will move.

Query it however you like:

```bash
cd ~/workbench/the-oven-vibe/the-oven-vibe-data-pipeline
duckdb warehouse.duckdb          # or: ./.venv/bin/python -c "import duckdb; ..."
```

## The four schemas, and what each one means

| schema | what it is | source |
|---|---|---|
| `bronze` | raw, untransformed, as it arrived | Zomato CSVs + the D1 exports |
| `silver` | cleaned and typed, one row per real thing | derived from bronze |
| `gold` | answers — aggregates the dashboard reads | derived from silver |
| **`d1`** | **a live mirror of Cloudflare D1, raw** | `/admin/api/export/tables` |

**`d1.*` is the direct/offline business. `silver.orders` and everything gold
builds on it is Zomato.** They are separate channels that happen to share a
file — do not join them without deciding, on purpose, that the question really
spans both. `gold.combined_weekly_sales` is the one place that unions them,
and it unions rather than joins.

## `d1` — live Cloudflare tables (the direct business)

| table | rows | what it answers |
|---|---|---|
| `d1.stock_moves` | 28 | every piece of stock in or out, with `reason` (`sold`, `wasted`, `correction`) and `channel` |
| `d1.stock_batches` | 9 | what was bought, when, what it cost, and the best-before |
| `d1.customer_phones` | 75 | second numbers linked to one customer |
| `d1.automated_sends` | 28 | which automated push went to whom, and whether it landed |
| `d1.store_state_log` | 17 | every time the kitchen opened or closed, and why |
| `d1.dough_lookups` | 13 | someone checked a Dough balance — intent, even without an order |
| `d1.item_offers` | 5 | offer prices, live and past |
| `d1.settings` | 5 | key/value config the Worker reads |
| `d1.demand_signals` | 4 | someone asked for an area or a thing we do not serve |
| `d1.campaign_claims` | 4 | who claimed a campaign offer |
| `d1.claim_campaigns` | 3 | the campaigns themselves |
| `d1.campaigns` | 2 | push campaigns |
| `d1.item_availability` | 2 | what is marked sold out |
| `d1.push_ask_events` | 2 | when the notification permission was asked for |
| `d1.push_subscriptions` | 2 | devices subscribed. **Keys withheld on purpose** — `endpoint`, `p256dh` and `auth` never leave the Worker |
| `d1.store_state` | 1 | the kitchen's current open/closed state |

Empty in D1 and therefore not created: `campaign_sends`, `rejected_orders`,
`reopen_waitlist`, `scheduled_campaigns`, `stock_waitlist`. They appear the
moment they have a row.

Every `d1` table carries a `synced_at` column — when that snapshot was taken.

## `silver` — cleaned

| table | rows | note |
|---|---|---|
| `silver.orders` | 273 | **Zomato** orders |
| `silver.order_items` | 327 | their lines |
| `silver.menu_items` | 28 | the menu |
| `silver.direct_orders` | 7 | **our own site's** orders. Confirmed, non-test only — this will not equal `COUNT(*)` in D1, and that is correct |
| `silver.direct_order_items` | 17 | their lines |
| `silver.dough_ledger` | 154 | every Dough movement |
| `silver.dough_balances` | 11 | customers holding a balance or a code |
| `silver.dough_balances_derived` | 71 | balances recomputed from the ledger, for checking the cache |
| `silver.referrals` | 0 | |

## `gold` — answers

| table | rows |
|---|---|
| `gold.customer_summary` | 215 |
| `gold.item_performance` | 28 |
| `gold.item_prices` | 28 |
| `gold.combined_weekly_sales` | 25 |
| `gold.weekly_sales` | 23 |
| `gold.ops_quality` | 23 |
| `gold.data_quality` | 6 |
| `gold.dough_customers` | 11 (view) |
| `gold.dough_balance_check` | 0 (view) — rows here mean the cache disagrees with the ledger |

## `bronze` — raw

`bronze.order_history_raw` (273), `bronze.dough_ledger` (154),
`bronze.dough_balances` (11), `bronze.direct_orders_raw` (7),
`bronze.dough_referrals` (0).

## Worked example

Sold against wasted, per ingredient — a question the warehouse could not
answer before the `d1` schema existed:

```sql
SELECT b.sku,
       SUM(CASE WHEN m.reason = 'sold'   THEN -m.delta ELSE 0 END) AS sold,
       SUM(CASE WHEN m.reason = 'wasted' THEN -m.delta ELSE 0 END) AS wasted
FROM d1.stock_moves m
JOIN d1.stock_batches b ON b.id = m.batch_id
GROUP BY 1 ORDER BY 2 DESC;
```

Remember what `sold` can and cannot see: it counts stock consumed by orders
**in D1**. A sandwich sold over WhatsApp never entered D1, so its bread shows
up as neither sold nor wasted — it simply vanishes from the batch. Read any
sold-vs-wasted number with that in mind until manual order entry exists.
