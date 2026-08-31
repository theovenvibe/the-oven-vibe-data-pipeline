# Unattended sync — running the D1 pull with no browser

`main.py` pulls direct orders, the Dough ledger and the D1 mirror tables from
the Worker at `api.theovenvibe.com/admin/api/*`. That path sits behind
Cloudflare Access, so every request needs an Access identity as well as the
`ADMIN_TOKEN` bearer.

By default that identity is **your browser session**: the pipeline reads the
`CF_Authorization` cookie straight out of signed-in Brave/Chrome. It works, but
it cannot be automated — the session expires, and the browser has to be closed
so its cookie database is unlocked. That is why the sync kept failing with:

```
✗ The D1 pull failed, so the warehouse is NOT up to date.
Cloudflare Access served a sign-in page instead of /admin/api/export/orders.
```

An Access **service token** replaces that with a machine identity that never
expires on a session timer. Set it up once and the pull works from a cron at
3am on a laptop with no browser open.

---

## Step 1 — create the service token (2 minutes, Cloudflare dashboard)

1. Go to <https://one.dash.cloudflare.com> and pick the `theovenvibe@gmail.com`
   account.
2. Left sidebar: **Access → Service Auth → Service Tokens**.
3. **Create Service Token**.
   - Name: `oven-vibe-data-pipeline`
   - Duration: **Non-expiring** (or 1 year, and put a reminder in the calendar).
4. Cloudflare shows a **Client ID** and a **Client Secret**.
   **The secret is shown once and never again.** Copy both now.

## Step 2 — let that token into the admin application

A service token that no policy mentions is refused exactly like a stranger.

1. **Access → Applications** → open the app covering `api.theovenvibe.com/admin`.
2. **Policies** → **Add a policy** (do not edit the Google sign-in policy — that
   one is how you get in, and breaking it locks you out of your own kitchen).
   - Name: `data pipeline service token`
   - Action: **Service Auth**  ← not Allow. Service Auth is a separate action,
     and a service token will not work under an Allow policy.
   - Include → **Service Token** → `oven-vibe-data-pipeline`
3. Save.

## Step 3 — put the credentials on the laptop

In `the-oven-vibe-data-pipeline/.env` (gitignored):

```
CF_ACCESS_CLIENT_ID=<the client id>.access
CF_ACCESS_CLIENT_SECRET=<the client secret>
```

Also copy them into `~/workbench/the-oven-vibe/CREDENTIALS.local.md`, which sits
outside every git repo — the secret cannot be recovered from Cloudflare later.

## Step 4 — prove it works with no browser

```
cd ~/workbench/the-oven-vibe/the-oven-vibe-data-pipeline
uv run python -c "from pipeline import access; print(access.fetch_admin_json('/admin/api/export/orders')['orders'][:1])"
```

One order printed means both locks opened. `pipeline/access.py` prefers the
service token whenever both variables are set and never touches the cookie jar,
so this proves the headless path specifically — not your browser session.

Then the real thing:

```
ov sync
```

---

## Step 5 — the nightly timer

Installed as a systemd **user** timer, so it runs as you and needs no root:

```
systemctl --user status oven-vibe-sync.timer     # is it scheduled
systemctl --user start oven-vibe-sync.service    # run it now, once
journalctl --user -u oven-vibe-sync -n 50        # what happened last time
```

It runs at 03:30 daily, with `Persistent=true` so a run missed while the laptop
was off happens shortly after the next boot rather than being skipped. Lingering
is enabled for the user (`loginctl enable-linger`), so the timer survives logging
out — without it, user timers only exist while a session does.

The unit files live at `~/.config/systemd/user/oven-vibe-sync.{service,timer}`.
They run `ov sync` — the same command you type — on purpose: a timer that runs
its own private code path is a timer that stays green while the real command is
broken.

The timer being *active* is not proof the sync *works* — an enabled unit whose
every run fails looks identical in `systemctl status`. Read the journal.

---

## If it starts failing

```
Cloudflare Access refused /admin/api/export/orders (HTTP 403).
  The service token was rejected.
```

In order of likelihood:

1. The token was deleted, or it expired (if you did not pick non-expiring).
   **Access → Service Auth** — is it still listed?
2. The policy was removed or its action is **Allow** instead of **Service Auth**.
3. The `.env` values were overwritten. `CF_ACCESS_CLIENT_ID` ends in `.access`.

To fall back to the browser session for one run, unset the two variables:

```
CF_ACCESS_CLIENT_ID= CF_ACCESS_CLIENT_SECRET= uv run python main.py
```

To skip D1 entirely and rebuild from the Zomato exports alone:

```
OVEN_VIBE_SKIP_D1=1 uv run python main.py
```

That is honest but incomplete — direct orders and Dough stay at whatever the
last successful pull left behind.
