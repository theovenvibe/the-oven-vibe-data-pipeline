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

The cookie is read straight out of the Chrome profile you are already signed
in with. **Nothing is written to disk** — the cookie lives in memory for the
length of the run, because a Cloudflare Access session copied into a file is a
login somebody else can use.

Chrome usually has to be fully closed: it holds a lock on its cookie database
while running.

The long-term fix is a Cloudflare Access **service token**
(`CF-Access-Client-Id` / `CF-Access-Client-Secret`), which would let this run
unattended from a timer with no browser involved. Until that exists in the
Cloudflare dashboard, this is what makes a manual `ov sync` work.
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


def access_cookie() -> str:
    """The Cloudflare Access cookie out of your signed-in Chrome."""
    try:
        import browser_cookie3
    except ImportError:
        raise AccessError(
            "browser_cookie3 is missing. Run:  uv sync  in the-oven-vibe-data-pipeline"
        )

    host = urllib.parse.urlsplit(worker_url()).hostname or ""
    try:
        jar = browser_cookie3.chrome(domain_name=host)
    except Exception as e:
        raise AccessError(
            f"Could not read Chrome's cookies: {e}\n"
            "  Chrome locks its cookie database while it is running — close it fully and retry."
        )

    for cookie in jar:
        if cookie.name == "CF_Authorization":
            return cookie.value

    raise AccessError(
        "Signed in to Chrome, but there is no CF_Authorization cookie for the Worker.\n"
        f"  Open {worker_url()}/admin in Chrome, sign in through Cloudflare Access,\n"
        "  then run this again. Use the Cloudflare sign-in button, not the PIN email."
    )


def fetch_admin_json(path: str, timeout: int = 30) -> dict:
    """GET an /admin/api/* endpoint carrying both locks, or fail saying which one.

    Cloudflare Access answers an unauthenticated request with a redirect to a
    sign-in *page*, so a naive caller gets HTML and 200 rather than an error.
    That is checked for explicitly here — silently parsing a login page as data
    is how a sync ends up reporting success with nothing in it.
    """
    url = f"{worker_url()}{path}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {admin_token()}",
            "Cookie": f"CF_Authorization={access_cookie()}",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            body = resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308, 403):
            raise AccessError(
                f"Cloudflare Access refused {path} (HTTP {e.code}).\n"
                "  The sign-in has expired. Open the admin in Chrome, sign in, and retry."
            )
        raise AccessError(f"{path} failed: HTTP {e.code} {e.reason} — {e.read().decode(errors='replace')[:400]}")
    except urllib.error.URLError as e:
        raise AccessError(f"{path} failed: could not reach {worker_url()} ({e.reason})")

    if "application/json" not in content_type:
        raise AccessError(
            f"Cloudflare Access served a sign-in page instead of {path}.\n"
            "  The session has expired. Open the admin in Chrome, sign in, and retry."
        )

    payload = json.loads(body)
    if not payload.get("ok"):
        raise AccessError(f"{path} failed: the Worker returned {payload}")
    return payload
