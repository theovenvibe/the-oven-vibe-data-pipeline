"""Getting past the two locks in front of the admin API.

`/admin/*` sits behind Cloudflare Access, so every request needs the
`CF_Authorization` cookie Access sets when you sign in with Google. The API
behind it also wants `Authorization: Bearer <ADMIN_TOKEN>`. Neither is
optional, they fail in completely different ways, and for weeks this pipeline
had only the second one — which is why `silver.direct_orders` sat at zero rows
from 19 August to 29 August while the sync reported success every time.

This is a port of `ov-tools/ov_auth.py`, which already solved the same problem
for the same Worker. Kept as a copy rather than a shared package because the
two repos are independently installable and a broken import between them would
take out both; the cost is that a fix here belongs there too.

The cookie is read straight out of whichever Chromium browser you are already
signed in with — Brave, Chrome, Chromium or Edge, tried in that order.
**Nothing is written to disk** — the cookie lives in memory for the
length of the run, because a Cloudflare Access session copied into a file is a
login somebody else can use.

The browser usually has to be fully closed: it holds a lock on its cookie
database while running.

A Cloudflare Access **service token** (`CF-Access-Client-Id` /
`CF-Access-Client-Secret`) is the way past this without a browser at all, and
it is what runs first when `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET`
are set. That is the path a cron takes: no browser to close, no session to
expire, no human. The browser-cookie reader below is the fallback for a laptop
that has not had the service token set up yet.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ENV_FILE = Path(__file__).parent.parent / ".env"


def _from_env_file(key: str) -> str | None:
    """Read one value out of .env, without taking a dotenv dependency.

    `ov sync` runs this pipeline directly rather than through
    sync-nightly.sh, which is the only thing that used to source .env — so
    reading it here is what makes the token available however the pipeline is
    started. Values may contain '='; keys may not.
    """
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip().strip("'\"") or None
    return None

BASE = "https://api.theovenvibe.com"

# Cloudflare's bot protection (error 1010) blocks Python's default urllib
# User-Agent on the Worker's hosts; a normal-looking one clears it. Not a security
# control on our side either way — the real gates are the two below.
USER_AGENT = "Mozilla/5.0 (compatible; oven-vibe-data-pipeline/1.0)"


class AccessError(SystemExit):
    """Raised with a message that says which of the two locks refused."""


def worker_url() -> str:
    return (os.environ.get("OVEN_VIBE_WORKER_URL") or _from_env_file("OVEN_VIBE_WORKER_URL") or BASE).rstrip("/")


def admin_token() -> str:
    token = os.environ.get("OVEN_VIBE_ADMIN_TOKEN") or _from_env_file("OVEN_VIBE_ADMIN_TOKEN")
    if not token:
        raise AccessError(
            "OVEN_VIBE_ADMIN_TOKEN is not set.\n"
            "  Copy .env.example to .env and fill it in — the value is in\n"
            "  ~/workbench/the-oven-vibe/CREDENTIALS.local.md (keep the newest row;\n"
            "  the token was rotated on 2026-08-29, so older copies are dead)."
        )
    return token


def service_token() -> tuple[str, str] | None:
    """The Access service-token pair, if one is configured.

    A service token is a machine identity Cloudflare Access accepts in place of
    a signed-in human. It never expires on a session timer, so this is the only
    version of the D1 pull that can run from a cron at 3am. Both halves must be
    present -- half a credential is a misconfiguration, not a fallback, and
    silently dropping to the browser reader would hide it.
    """
    cid = os.environ.get("CF_ACCESS_CLIENT_ID") or _from_env_file("CF_ACCESS_CLIENT_ID")
    secret = os.environ.get("CF_ACCESS_CLIENT_SECRET") or _from_env_file("CF_ACCESS_CLIENT_SECRET")
    if cid and secret:
        return cid, secret
    if cid or secret:
        raise AccessError(
            "Only half of the Access service token is set.\n"
            "  Both CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET are needed —\n"
            "  see docs/UNATTENDED-SYNC.md for where they come from."
        )
    return None


def access_cookies() -> list[tuple[str, str]]:
    """Every browser session on this machine, as (browser name, Cookie header).

    Plural on purpose. The old version returned the first browser that had a
    cookie at all and stopped there, which on 2026-08-31 meant Brave -- whose
    `CF_Authorization` was unexpired and carried the right `aud`, and which
    Access refused anyway. Chrome's session, sitting right beside it, worked.
    An unexpired cookie is not a valid session, and only the endpoint can say
    which is which, so every candidate is returned for the caller to try.

    Both of them, not just `CF_Authorization`. Access also sets `CF_AppSession`,
    and on 2026-08-31 sending the identity cookie alone got the sign-in page back
    with an unexpired, correct-`aud` token in hand -- which reads exactly like an
    expired session and is not one. Sending whatever Access set, rather than the
    one cookie we think matters, is also the version that keeps working if
    Cloudflare adds a third.
    """
    try:
        import browser_cookie3
    except ImportError:
        raise AccessError(
            "browser_cookie3 is missing. Run:  uv sync  in the-oven-vibe-data-pipeline"
        )

    host = urllib.parse.urlsplit(worker_url()).hostname or ""

    # Try every Chromium browser on the machine, not just Chrome. Milan moved
    # to Brave mid-2026 and the sync silently stopped finding a cookie —
    # "signed in but no cookie" reads like an expired session rather than
    # "wrong browser", which is the sort of wrong diagnosis that costs an hour.
    readers = [
        ("Brave", browser_cookie3.brave),
        ("Chrome", browser_cookie3.chrome),
        ("Chromium", browser_cookie3.chromium),
        ("Edge", browser_cookie3.edge),
    ]

    problems = []
    sessions = []
    for name, reader in readers:
        try:
            jar = reader(domain_name=host)
        except Exception as e:
            # No such browser installed, or its cookie DB is locked because it
            # is running. Neither is fatal while another browser might have it.
            problems.append(f"{name}: {e}")
            continue
        found = {c.name: c.value for c in jar if c.name.startswith("CF_")}
        # CF_Authorization is the identity; without it there is no session at
        # all, whatever else is in the jar. CF_AppSession rides along because
        # Access sets it too and sending only the identity cookie gets the
        # sign-in page back.
        if "CF_Authorization" in found:
            sessions.append((name, "; ".join(f"{k}={v}" for k, v in found.items())))
        else:
            problems.append(f"{name}: no CF_Authorization cookie for {host}")

    if sessions:
        return sessions

    raise AccessError(
        f"No Cloudflare Access session found for {host} in any browser.\n"
        f"  Open {worker_url()}/admin in your browser, sign in through Cloudflare\n"
        "  Access, then run this again. Use the Cloudflare sign-in button, not the\n"
        "  PIN email. A browser locks its cookie database while running, so close\n"
        "  it fully if it is the one you signed in with.\n"
        "  Tried:\n    " + "\n    ".join(problems)
    )


def _retry_hint() -> str:
    """Say what to do about it, which depends on which credential was used.

    Telling someone to sign in to Chrome when a cron ran with a service token
    sends them to fix the wrong thing.
    """
    if service_token():
        return (
            "The service token was rejected. Check it is still listed under\n"
            "  Zero Trust -> Access -> Service Auth, and that the /admin policy still\n"
            "  includes it — see docs/UNATTENDED-SYNC.md."
        )
    return (
        "The sign-in has expired. Open the admin in your browser, sign in, close the\n"
        "  browser fully, and retry — or set up a service token so this never happens\n"
        "  again (docs/UNATTENDED-SYNC.md)."
    )


def fetch_admin_json(path: str, timeout: int = 30) -> dict:
    """GET an /admin/api/* endpoint carrying both locks, or fail saying which one.

    Cloudflare Access answers an unauthenticated request with a redirect to a
    sign-in *page*, so a naive caller gets HTML and 200 rather than an error.
    That is checked for explicitly here — silently parsing a login page as data
    is how a sync ends up reporting success with nothing in it.
    """
    url = f"{worker_url()}{path}"
    headers = {
        "Authorization": f"Bearer {admin_token()}",
        "User-Agent": USER_AGENT,
    }
    # The service token first: it is the one that works with no browser on the
    # machine at all. Reading the cookie jar is only attempted when there is no
    # service token, because on a headless run it can only fail.
    pair = service_token()
    # One attempt with the service token; otherwise one attempt per browser
    # session, because a browser having a cookie does not mean Access accepts it
    # and the endpoint is the only thing that can tell us.
    attempts = [("service token", None)] if pair else access_cookies()
    if pair:
        headers["CF-Access-Client-Id"], headers["CF-Access-Client-Secret"] = pair

    refused = []
    for who, cookie in attempts:
        if cookie is not None:
            headers["Cookie"] = cookie
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                content_type = resp.headers.get("Content-Type", "")
                body = resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308, 403):
                refused.append(f"{who}: HTTP {e.code}")
                continue
            raise AccessError(f"{path} failed: HTTP {e.code} {e.reason} — {e.read().decode(errors='replace')[:400]}")
        except urllib.error.URLError as e:
            raise AccessError(f"{path} failed: could not reach {worker_url()} ({e.reason})")

        if "application/json" in content_type:
            break
        # Access answers an unauthenticated request with a sign-in *page* and a
        # 200, so HTML here is a refusal, not data.
        refused.append(f"{who}: sign-in page")
    else:
        raise AccessError(
            f"Cloudflare Access refused {path} for every credential on this machine.\n"
            f"  Tried: {', '.join(refused)}\n  " + _retry_hint()
        )

    payload = json.loads(body)
    if not payload.get("ok"):
        raise AccessError(f"{path} failed: the Worker returned {payload}")
    return payload
