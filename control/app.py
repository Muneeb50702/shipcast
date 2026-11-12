"""Shipcast Control: the Claude side of Shipcast, served inside Studio at <studio>/control (one site, one login).

Multi-tenant. Identity is the Studio login: each request's Studio cookies are checked with Studio's own
/api/user/self, so there is no separate account, password or setup link. Every Studio workspace has its own brand
profile, kits and connected Claude apps; Studio's own MCP tools are proxied as studio_* with that workspace's API
key, so a customer's Claude only ever sees their workspace. Platform developer keys (one set for the server, used
by every workspace to connect its accounts) are for Studio super admins only.

UI   /control/  overview + connect Claude + connected apps · /brand · /kits · /platforms (super admin)
MCP  /control/mcp, OAuth 2.1: apps self-register, a workspace admin approves them on /control/oauth/consent
The Cloudflare tunnel routes /control/* and /.well-known/oauth-*/control* to this process; the rest goes to Studio.
"""
import asyncio
import hashlib
import html
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

from fastmcp import FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, OAuthProvider
from fastmcp.server.auth.auth import ClientRegistrationOptions, RevocationOptions
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.providers.proxy import FastMCPProxy, _create_client_factory
from mcp.server.auth.provider import (AuthorizationCode, AuthorizationParams, AuthorizeError, RefreshToken,
                                      RegistrationError, TokenError, construct_redirect_uri)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Mount, Route

DB_PATH = os.environ.get("HUB_DB", "/var/lib/hub/hub.db")
KEYS_FILE = os.environ.get("POSTIZ_KEYS_FILE", "/opt/postiz/keys.env")
POSTIZ_LOCAL = os.environ.get("POSTIZ_LOCAL", "http://127.0.0.1:4007")
STUDIO = os.environ.get("STUDIO_PUBLIC", "https://social.muhammadmuneeb.tech")
PORT = int(os.environ.get("HUB_PORT", "4010"))
P = "/control"  # mount path under the Studio site
BASE = STUDIO + P
MCP_URL = BASE + "/mcp"
ADMIN_ROLES = ("ADMIN", "SUPERADMIN")  # workspace roles that may manage Claude for their workspace

# ------------------------------------------------------------------ storage

db = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
db.execute("PRAGMA journal_mode=WAL")
db.executescript("""
create table if not exists org_kv (org text, k text, v text not null, primary key (org, k));
create table if not exists orgs (id text primary key, api_key text not null, updated integer);
create table if not exists events (ts integer, org text, actor text, action text);
create table if not exists oauth_clients (id text primary key, info text not null, created integer);
create table if not exists oauth_grants (id text primary key, client_id text, org text, email text, admin integer,
                                         created integer, used integer);
create table if not exists oauth_tokens (h text primary key, kind text, grant_id text, client_id text, exp integer,
                                         data text);  -- only the sha256 of each token is stored
""")
_lock = threading.Lock()


def oget(org, k, default=None):
    row = db.execute("select v from org_kv where org=? and k=?", (org, k)).fetchone()
    return json.loads(row[0]) if row else default


def oput(org, k, v):
    with _lock:
        db.execute("insert into org_kv values(?,?,?) on conflict(org,k) do update set v=excluded.v",
                   (org, k, json.dumps(v)))


def odrop(org, k):
    with _lock:
        db.execute("delete from org_kv where org=? and k=?", (org, k))


def log(org, actor, action):
    with _lock:
        db.execute("insert into events values(?,?,?,?)", (int(time.time()), org, actor, action))


def org_key(org):
    row = db.execute("select api_key from orgs where id=?", (org,)).fetchone()
    return row[0] if row else ""


def remember_org_key(org, key):
    if key and key != org_key(org):
        with _lock:
            db.execute("insert into orgs values(?,?,?) on conflict(id) do update set api_key=excluded.api_key, "
                       "updated=excluded.updated", (org, key, int(time.time())))


# ------------------------------------------------------------------ Studio session = identity

_sessions: dict[str, tuple[float, dict | None]] = {}


def studio_user(req: Request) -> dict | None:
    """The Studio user behind this request's cookies (30 s cache), or None when not logged in."""
    cookies = {k: req.cookies[k] for k in ("auth", "showorg", "impersonate") if req.cookies.get(k)}
    if "auth" not in cookies:
        return None
    raw = "; ".join(f"{k}={v}" for k, v in cookies.items())
    key = hashlib.sha256(raw.encode()).hexdigest()
    hit = _sessions.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    user = None
    try:
        r = urllib.request.Request(POSTIZ_LOCAL + "/api/user/self", headers={"Cookie": raw})
        with urllib.request.urlopen(r, timeout=10) as resp:
            u = json.load(resp)
        user = {"id": u["id"], "email": u.get("email") or "", "org": u["orgId"], "role": u.get("role"),
                "super": bool(u.get("admin")), "api_key": u.get("publicApi") or ""}
        remember_org_key(user["org"], user["api_key"])  # keeps the proxy key fresh after a rotation in Studio
    except (urllib.error.URLError, KeyError, ValueError, TimeoutError):
        user = None
    if len(_sessions) > 5000:
        _sessions.clear()
    _sessions[key] = (time.time() + 30, user)
    return user


# ------------------------------------------------------------------ domain: brand, kits, platforms

BRAND_FIELDS = {
    "name": "Brand / creator name",
    "handle": "Main @handle",
    "niche": "What the account is about, in one line",
    "audience": "Who it's for",
    "promise": "Why people follow: the value promise",
    "voice": "How it sounds: tone, slang level, words to use",
    "never": "Never say / never do",
    "pillars": "Content pillars, one per line",
    "cta_bank": "Calls to action, one per line",
    "visual": "Visual direction: colours (hex), fonts, mood, references",
    "links": "Website, link in bio, offers",
}
RECOMMENDED_KITS = {
    "reel-style": "Reel theme kit: canvas, colours, type, motion, sound rules",
    "carousel-style": "Carousel kit: slide size, layout, components, typography",
    "caption-style": "Caption rules: hook line, structure, CTA, hashtags per platform",
}
KIT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")

# Every Studio provider that needs server developer keys (read from Studio's provider code, v2.25.3).
# id -> label, required env vars, optional env vars, developer console, Studio provider ids, setup notes.
# Redirect URI for each provider id: <studio>/integrations/social/<provider id>.
PLATFORMS = {
    "youtube": ("YouTube", ["YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET"], [],
                "https://console.cloud.google.com/auth/clients", ["youtube"],
                "Google Cloud: enable YouTube Data API v3 + YouTube Analytics API, set up the OAuth consent screen "
                "(External; publish it, or refresh tokens die after 7 days), then Clients → Web application."),
    "facebook": ("Facebook Pages + Instagram (business, via Facebook)", ["FACEBOOK_APP_ID", "FACEBOOK_APP_SECRET"], [],
                 "https://developers.facebook.com/apps", ["facebook", "instagram"],
                 "Meta app (Business type): Facebook Login for Business + Instagram Graph API; add the redirect URIs "
                 "under Facebook Login settings. Other people's accounts need Meta App Review."),
    "instagram": ("Instagram (Instagram login, no Facebook page)", ["INSTAGRAM_APP_ID", "INSTAGRAM_APP_SECRET"], [],
                  "https://developers.facebook.com/apps", ["instagram-standalone"],
                  "Meta app with the 'Instagram API with Instagram Login' product; use its Instagram app ID/secret."),
    "threads": ("Threads", ["THREADS_APP_ID", "THREADS_APP_SECRET"], [], "https://developers.facebook.com/apps",
                ["threads"], "Meta app with the 'Access the Threads API' use case; use the Threads app ID/secret."),
    "linkedin": ("LinkedIn (profiles + pages)", ["LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET"], [],
                 "https://www.linkedin.com/developers/apps", ["linkedin", "linkedin-page"],
                 "Products: Share on LinkedIn + Sign In with LinkedIn (OpenID Connect); Community Management API "
                 "for company pages."),
    "tiktok": ("TikTok", ["TIKTOK_CLIENT_ID", "TIKTOK_CLIENT_SECRET"], [], "https://developers.tiktok.com/apps",
               ["tiktok"], "App with Login Kit + Content Posting API (direct post). Public use needs TikTok's review."),
    "tiktok-business": ("TikTok Business", ["TIKTOK_BUSINESS_CLIENT_ID", "TIKTOK_BUSINESS_CLIENT_SECRET"], [],
                        "https://business-api.tiktok.com/portal/apps", ["tiktok-business"],
                        "TikTok API for Business app."),
    "pinterest": ("Pinterest", ["PINTEREST_CLIENT_ID", "PINTEREST_CLIENT_SECRET"], [],
                  "https://developers.pinterest.com/apps", ["pinterest"],
                  "Pinterest app; trial access only posts to your own boards until upgraded to standard access."),
    "reddit": ("Reddit", ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"], [], "https://www.reddit.com/prefs/apps",
               ["reddit"], "Create app → type 'web app'; the ID is under the app name."),
    "discord": ("Discord", ["DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "DISCORD_BOT_TOKEN_ID"], [],
                "https://discord.com/developers/applications", ["discord"],
                "Application with a bot: OAuth2 client ID/secret, and the bot token as DISCORD_BOT_TOKEN_ID."),
    "slack": ("Slack", ["SLACK_ID", "SLACK_SECRET"], [], "https://api.slack.com/apps", ["slack"],
              "Slack app: OAuth & Permissions → add the redirect URL; bot scopes chat:write, channels:read."),
    "telegram": ("Telegram", ["TELEGRAM_BOT_NAME", "TELEGRAM_TOKEN"], [], "https://t.me/BotFather", ["telegram"],
                 "Create a bot with @BotFather: its username (without @) and token."),
    "mastodon": ("Mastodon", ["MASTODON_CLIENT_ID", "MASTODON_CLIENT_SECRET"], ["MASTODON_URL"],
                 "https://mastodon.social/settings/applications", ["mastodon"],
                 "Preferences → Development → New application on the instance; MASTODON_URL if not mastodon.social."),
    "twitch": ("Twitch", ["TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"], [], "https://dev.twitch.tv/console/apps",
               ["twitch"], "Register an application (category: Website Integration)."),
    "kick": ("Kick", ["KICK_CLIENT_ID", "KICK_SECRET"], [], "https://kick.com/settings/developer", ["kick"],
             "Kick developer app."),
    "tumblr": ("Tumblr", ["TUMBLR_CLIENT_ID", "TUMBLR_CLIENT_SECRET"], [], "https://www.tumblr.com/oauth/apps",
               ["tumblr"], "Register an application; OAuth consumer key/secret."),
    "dribbble": ("Dribbble", ["DRIBBBLE_CLIENT_ID", "DRIBBBLE_CLIENT_SECRET"], [],
                 "https://dribbble.com/account/applications", ["dribbble"], "Register an application."),
    "vk": ("VK", ["VK_ID"], [], "https://id.vk.com/about/business/go", ["vk"], "VK ID app; its app ID."),
    "whop": ("Whop", ["WHOP_CLIENT_ID"], [], "https://whop.com/dashboard/developer", ["whop"], "Whop developer app."),
    "mewe": ("MeWe", ["MEWE_APP_ID", "MEWE_API_KEY", "MEWE_HOST"], [], "https://mewe.com/business", ["mewe"],
             "MeWe partner API access (by request)."),
    "x": ("X (Twitter)", ["X_API_KEY", "X_API_SECRET"], [], "https://developer.x.com/en/portal/dashboard", ["x"],
          "X developer app, OAuth 1.0a with Read and write; callback URL below. Posting volume depends on your X "
          "API plan (free tier is very limited)."),
    "farcaster": ("Farcaster (via Neynar)", ["NEYNAR_CLIENT_ID", "NEYNAR_SECRET_KEY", "NEYNAR_APP_FID",
                                             "NEYNAR_APP_MNEMONIC"], ["NEYNAR_SPONSOR_SIGNERS"],
                  "https://dev.neynar.com/app", ["wrapcast"],
                  "Neynar app (client ID + API key) plus your app's Farcaster account FID and its recovery phrase "
                  "(12/24 words, spaces allowed); NEYNAR_SPONSOR_SIGNERS=true to pay signer fees for users."),
    "gmb": ("Google Business Profile", ["GOOGLE_GMB_CLIENT_ID", "GOOGLE_GMB_CLIENT_SECRET"], [],
            "https://console.cloud.google.com/auth/clients", ["gmb"],
            "Needs Google's Business Profile API access approval first; then a Web OAuth client like YouTube's."),
}
ALL_VARS = {v for p in PLATFORMS.values() for v in p[1] + p[2]}
SAFE_VALUE = re.compile(r"^[^\s'\\]*$")  # values are written single-quoted, so compose never interpolates them
SPACED_VALUE = re.compile(r"^[a-z]+( [a-z]+)*$")  # recovery phrases: lowercase words, single spaces
SPACED_VARS = {"NEYNAR_APP_MNEMONIC"}



def read_keys():
    out = {}
    try:
        for line in open(KEYS_FILE):
            if "=" in line and not line.startswith("#"):
                k, v = line.rstrip("\n").split("=", 1)
                out[k] = v[1:-1] if len(v) >= 2 and v[0] == v[-1] == "'" else v
    except FileNotFoundError:
        pass
    return out


def write_keys(updates: dict, org: str | None, actor: str):
    """Merge updates into Studio's keys.env. Saving it makes systemd (postiz-keys.path) restart Studio."""
    for k, v in updates.items():
        if k not in ALL_VARS:
            raise ValueError(f"unknown key {k}")
        if not (SPACED_VALUE.match(v) if k in SPACED_VARS else SAFE_VALUE.match(v)):
            raise ValueError(f"{k}: words separated by single spaces" if k in SPACED_VARS
                             else f"{k}: no spaces, quotes or backslashes")
    keys = read_keys() | updates
    with open(KEYS_FILE, "w") as f:
        f.write("# written by Shipcast Control; every save restarts Studio\n")
        f.writelines(f"{k}='{v}'\n" for k, v in sorted(keys.items()) if v)
    if updates:
        log(org, actor, "platform keys saved: " + ", ".join(sorted(updates)))



def platform_rows():
    keys = read_keys()
    return [{
        "id": pid, "label": label, "console": console, "notes": notes,
        "keys": {v: bool(keys.get(v)) for v in req}, "optional": {v: bool(keys.get(v)) for v in opt},
        "configured": all(keys.get(v) for v in req),
        "redirect_uris": [f"{STUDIO}/integrations/social/{p}" for p in providers if p != "telegram"],  # bot, no OAuth
    } for pid, (label, req, opt, console, providers, notes) in PLATFORMS.items()]


def kit_names(org):
    return [r[0][4:] for r in db.execute("select k from org_kv where org=? and k like 'kit:%' order by k", (org,))]


def studio_channels(org):
    key = org_key(org)
    if not key:
        return None, "workspace not linked yet: open the Claude page in Studio once"
    req = urllib.request.Request(POSTIZ_LOCAL + "/api/public/v1/integrations", headers={"Authorization": key})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r), None
    except Exception as ex:  # noqa: BLE001 - reported to the user as status text
        return None, f"Studio error: {ex}"


def setup_status(org):
    brand = oget(org, "brand", {})
    missing = [k for k in BRAND_FIELDS if not str(brand.get(k, "")).strip()]
    kits = kit_names(org)
    missing_kits = [k for k in RECOMMENDED_KITS if k not in kits]
    channels, err = studio_channels(org)
    steps = [
        {"step": "brand profile", "done": not missing,
         "detail": "complete" if not missing else "missing: " + ", ".join(missing)},
        {"step": "kits", "done": not missing_kits,
         "detail": "complete" if not missing_kits else "missing: " + ", ".join(missing_kits)},
        {"step": "channels connected", "done": bool(channels),
         "detail": err or (", ".join(f"{c.get('providerIdentifier', c.get('identifier', '?'))}:{c.get('name')}"
                                     for c in channels) if channels else "none yet: add channels in Studio")},
    ]
    nxt = next((s["step"] for s in steps if not s["done"]), None)
    ready = [p["id"] for p in platform_rows() if p["configured"]]
    return {"ready_for_generation": nxt is None, "next_step": nxt, "steps": steps,
            "platforms_with_server_keys": ready,
            "note": "Studio lists every platform; ones in platforms_with_server_keys (and keyless ones like Bluesky, "
                    "WordPress, Medium, Dev.to, Hashnode, Lemmy, Nostr, Listmonk) can connect now."}


# ------------------------------------------------------------------ OAuth for MCP apps

TTL = {"pending": 600, "code": 300, "access": 3600, "refresh": 60 * 86400}
KNOWN_HOSTS = {"claude.ai", "claude.com", "chatgpt.com", "localhost", "127.0.0.1"}


def _h(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def tok_put(kind: str, client_id: str, grant_id: str | None, data: dict) -> str:
    tok = secrets.token_urlsafe(32)
    with _lock:
        db.execute("insert into oauth_tokens values(?,?,?,?,?,?)",
                   (_h(tok), kind, grant_id, client_id, int(time.time()) + TTL[kind], json.dumps(data)))
    return tok


def tok_get(kind: str, tok: str, client_id: str | None = None, take=False) -> dict | None:
    """Live token data; take=True also deletes it atomically (one-time codes, rotated refresh tokens)."""
    with _lock:
        row = db.execute("select grant_id, client_id, exp, data from oauth_tokens where h=? and kind=?",
                         (_h(tok), kind)).fetchone()
        if not row or row[2] < time.time() or (client_id and row[1] != client_id):
            return None
        if take:
            db.execute("delete from oauth_tokens where h=?", (_h(tok),))
    return {"grant": row[0], "client_id": row[1], "exp": row[2], **json.loads(row[3])}


def grant_row(gid: str):
    return db.execute("select org, email, admin from oauth_grants where id=?", (gid,)).fetchone()


def grant_drop(gid: str):
    with _lock:
        db.execute("delete from oauth_tokens where grant_id=?", (gid,))
        db.execute("delete from oauth_grants where id=?", (gid,))


def oauth_prune():
    with _lock:
        db.execute("delete from oauth_tokens where exp<?", (int(time.time()),))
        db.execute("delete from oauth_grants where id not in (select grant_id from oauth_tokens where grant_id is not null)")
        db.execute("delete from oauth_clients where created<? and id not in (select client_id from oauth_grants)",
                   (int(time.time()) - 86400,))


class ShipcastAuth(OAuthProvider):
    def __init__(self):
        super().__init__(base_url=BASE, revocation_options=RevocationOptions(enabled=True),
                         client_registration_options=ClientRegistrationOptions(
                             enabled=True, valid_scopes=["shipcast"], default_scopes=["shipcast"]))

    async def get_client(self, client_id):
        row = db.execute("select info from oauth_clients where id=?", (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate_json(row[0]) if row else None

    async def register_client(self, client_info):
        oauth_prune()
        if db.execute("select count(*) from oauth_clients where id not in "
                      "(select client_id from oauth_grants)").fetchone()[0] >= 500:
            raise RegistrationError(error="invalid_client_metadata", error_description="too many unapproved apps")
        with _lock:
            db.execute("insert or replace into oauth_clients values(?,?,?)",
                       (client_info.client_id, client_info.model_dump_json(), int(time.time())))

    async def authorize(self, client, params):
        if params.resource and params.resource.rstrip("/") not in (MCP_URL, BASE):
            raise AuthorizeError(error="invalid_request", error_description="tokens here are only for " + MCP_URL)
        oauth_prune()
        req_id = tok_put("pending", client.client_id, None, {"params": params.model_dump(mode="json")})
        return f"{BASE}/oauth/consent?req={req_id}"  # a workspace admin approves there, then is sent back with a code

    async def load_authorization_code(self, client, authorization_code):
        d = tok_get("code", authorization_code, client.client_id)
        return d and AuthorizationCode(
            code=authorization_code, client_id=d["client_id"], scopes=d["scopes"], expires_at=d["exp"],
            code_challenge=d["code_challenge"], redirect_uri=d["redirect_uri"],
            redirect_uri_provided_explicitly=d["redirect_uri_provided_explicitly"], resource=d["resource"])

    def _issue(self, client_id: str, grant: str, scopes: list[str]) -> OAuthToken:
        g = grant_row(grant)
        if not g:
            raise TokenError(error="invalid_grant", error_description="this app was disconnected")
        data = {"scopes": scopes, "org": g[0], "admin": bool(g[2])}
        with _lock:
            db.execute("delete from oauth_tokens where grant_id=? and kind='access'", (grant,))  # one live pair per app
        return OAuthToken(access_token=tok_put("access", client_id, grant, data), token_type="Bearer",
                          expires_in=TTL["access"], scope=" ".join(scopes),
                          refresh_token=tok_put("refresh", client_id, grant, {"scopes": scopes}))

    async def exchange_authorization_code(self, client, authorization_code):
        d = tok_get("code", authorization_code.code, client.client_id, take=True)
        if not d:
            raise TokenError(error="invalid_grant", error_description="code already used or expired")
        return self._issue(client.client_id, d["grant"], authorization_code.scopes)

    async def load_refresh_token(self, client, refresh_token):
        d = tok_get("refresh", refresh_token, client.client_id)
        return d and RefreshToken(token=refresh_token, client_id=d["client_id"], scopes=d["scopes"], expires_at=d["exp"])

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        d = tok_get("refresh", refresh_token.token, client.client_id, take=True)
        if not d:
            raise TokenError(error="invalid_grant", error_description="refresh token already used or expired")
        return self._issue(client.client_id, d["grant"], scopes or refresh_token.scopes)

    async def load_access_token(self, token):
        d = tok_get("access", token)
        if not d:
            return None
        with _lock:
            db.execute("update oauth_grants set used=? where id=?", (int(time.time()), d["grant"]))
        return AccessToken(token=token, client_id=d["client_id"], scopes=d["scopes"], expires_at=d["exp"],
                           resource=MCP_URL, claims={"org": d["org"], "admin": d["admin"]})

    async def revoke_token(self, token):  # an app logging out ends its whole grant
        for kind in ("access", "refresh"):
            d = tok_get(kind, token.token)
            if d:
                grant_drop(d["grant"])


AUTH = ShipcastAuth()
mcp = FastMCP("shipcast", auth=AUTH, instructions="""
Shipcast: this workspace's control center for its social accounts. Always call setup_status first and work on its next_step:
1. brand profile: interview the owner (get_brand lists the fields), then save with update_brand.
2. kits: write the reel, carousel and caption kits from the brand (save_kit), show them to the owner first.
3. channels: the owner connects accounts in Shipcast Studio (OAuth needs their login).
When ready_for_generation is true, skip onboarding: plan and make content, then schedule it with the studio_* tools.
Never publish or schedule anything the owner has not approved.""")


def caller() -> tuple[str, bool]:
    """(workspace id, is Studio super admin) of the app calling a tool."""
    tok = get_access_token()
    org = tok and (tok.claims or {}).get("org")
    if not org:
        raise ToolError("not connected to a Shipcast workspace")
    return org, bool(tok.claims.get("admin"))


@mcp.tool(name="setup_status")
def tool_setup_status() -> dict:
    """What is set up in this workspace and what is next. Call this first in every session."""
    return setup_status(caller()[0])


@mcp.tool
def get_brand() -> dict:
    """The brand profile plus a description of every field."""
    return {"fields": BRAND_FIELDS, "brand": oget(caller()[0], "brand", {})}


@mcp.tool
def update_brand(fields: dict[str, str]) -> dict:
    """Merge fields into the brand profile (keys from get_brand().fields; extra keys allowed). Empty string clears."""
    org = caller()[0]
    brand = {k: v for k, v in (oget(org, "brand", {}) | fields).items() if str(v).strip()}
    oput(org, "brand", brand)
    log(org, "claude", "brand updated: " + ", ".join(sorted(fields)))
    return brand


@mcp.tool
def list_kits() -> dict:
    """Saved kits plus the recommended ones."""
    return {"saved": kit_names(caller()[0]), "recommended": RECOMMENDED_KITS}


@mcp.tool
def get_kit(name: str) -> str:
    """Full text of a kit."""
    org = caller()[0]
    kit = oget(org, "kit:" + name)
    if kit is None:
        raise ToolError(f"no kit {name!r}; saved: {kit_names(org)}")
    return kit


@mcp.tool
def save_kit(name: str, content: str) -> str:
    """Create or replace a kit (lowercase-dashed name, e.g. reel-style). Markdown, CSS or JSON text."""
    org = caller()[0]
    if not KIT_NAME.match(name):
        raise ToolError("kit name: lowercase letters, digits and dashes")
    oput(org, "kit:" + name, content)
    log(org, "claude", f"kit saved: {name}")
    return f"saved {name} ({len(content)} chars)"


@mcp.tool
def list_platforms() -> list[dict]:
    """Platforms Studio can connect, whether the server has their developer keys (never the values), redirect URIs."""
    return platform_rows()


@mcp.tool
def set_platform_keys(platform: str, values: dict[str, str]) -> dict:
    """Shipcast owner only: save a platform's server developer keys (names from list_platforms). Restarts Studio."""
    org, admin = caller()
    if not admin:
        raise ToolError("only the Shipcast owner can change server platform keys")
    if platform not in PLATFORMS:
        raise ToolError(f"unknown platform; one of {sorted(PLATFORMS)}")
    allowed = set(PLATFORMS[platform][1] + PLATFORMS[platform][2])
    bad = set(values) - allowed
    if bad:
        raise ToolError(f"{platform} takes {sorted(allowed)}, not {sorted(bad)}")
    try:
        write_keys({k: v.strip() for k, v in values.items()}, org, "claude")
    except ValueError as ex:
        raise ToolError(str(ex)) from ex
    return next(p for p in platform_rows() if p["id"] == platform)


def studio_client():
    """Studio's MCP, opened with the calling workspace's own API key (one fresh client per request)."""
    key = org_key(caller()[0])
    if not key:
        raise ToolError("this workspace's Studio key is unknown: open the Claude page in Studio once, then retry")
    return _create_client_factory(StreamableHttpTransport(
        POSTIZ_LOCAL + "/api/mcp", headers={"Authorization": "Bearer " + key}))()


mcp.mount(FastMCPProxy(client_factory=studio_client, name="studio"), namespace="studio",
          tool_names={"ask_postiz": "ask"})  # keep the upstream name out of the tool list

# ------------------------------------------------------------------ UI

e = html.escape
CSS = """
:root{--bg:#1a1919;--card:#0e0e0e;--line:#252525;--ink:#fff;--mute:#9c9c9c;--acc:#cf4410;--acc2:#ff5a1f;
--ok:#3ddc84;--bad:#ff6b6b;--btn:#313030}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 "Plus Jakarta Sans",system-ui,sans-serif}
header{display:flex;gap:10px;align-items:center;padding:14px 20px;border-bottom:1px solid var(--line)}
header a{color:var(--mute);text-decoration:none;margin-left:auto}.embedded header{display:none}
.brand{display:flex;align-items:center;gap:9px;font-weight:800;font-size:17px}.brand svg{width:26px;height:26px}
.brand i{font-style:normal;color:var(--acc2)}
main{max-width:900px;margin:0 auto;padding:20px 16px 60px}h1{font-size:20px;margin:0 0 14px}h2{font-size:15px;margin:0 0 8px}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px}
.tabs a{padding:8px 14px;border-radius:8px;background:var(--btn);color:var(--mute);text-decoration:none;font-weight:600;font-size:13px}
.tabs a.on{background:var(--acc);color:#fff}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:14px}
label{display:block;color:var(--mute);font-size:13px;margin:10px 0 4px}
input,textarea{width:100%;background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:9px 10px;font:inherit}
textarea{min-height:70px;font-family:ui-monospace,monospace;font-size:13px}
button,.btn{display:inline-block;background:var(--acc);color:#fff;border:0;border-radius:8px;padding:9px 16px;font:inherit;
  font-weight:600;cursor:pointer;margin-top:12px;text-decoration:none}
button:hover,.btn:hover{background:#a8360b}button.ghost{background:var(--btn);color:var(--ink)}
pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin:6px 0;white-space:pre-wrap;
  word-break:break-all;font-size:13px}
.ok{color:var(--ok)}.bad{color:var(--bad)}.mute{color:var(--mute);font-size:13px}
.row{display:flex;gap:10px;align-items:center;justify-content:space-between;border-top:1px solid var(--line);padding:8px 0}
.row button{margin:0}.flash{border-color:var(--acc)}a{color:var(--acc2)}ul{padding-left:18px}
"""
MARK = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128"><defs><linearGradient id="sg" x1="0" y1="0" x2="1" '
        'y2="1"><stop offset="0" stop-color="#FF7A2F"/><stop offset="1" stop-color="#E8400C"/></linearGradient></defs>'
        '<rect width="128" height="128" rx="34" fill="url(#sg)"/><path d="M38 64L90 33M38 64L97 64M38 64L90 95" '
        'stroke="#fff" stroke-width="9" stroke-linecap="round" fill="none"/><circle cx="38" cy="64" r="14" fill="#fff"/>'
        '<circle cx="90" cy="33" r="8.5" fill="#fff"/><circle cx="97" cy="64" r="8.5" fill="#fff"/>'
        '<circle cx="90" cy="95" r="8.5" fill="#fff"/></svg>')
HEADERS = {"X-Frame-Options": "SAMEORIGIN", "Content-Security-Policy": "frame-ancestors 'self'",
           "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}


def page(title: str, body: str, user: dict | None = None, here: str = "", flash: str = "") -> HTMLResponse:
    tabs = ""
    if user and here:
        items = [("/", "Overview"), ("/brand", "Brand"), ("/kits", "Kits")] + ([("/platforms", "Platforms")]
                                                                               if user["super"] else [])
        tabs = '<nav class=tabs>' + "".join(
            f'<a href="{P}{h}" class="{"on" if h == here else ""}">{t}</a>' for h, t in items) + "</nav>"
    msg = f'<div class="card flash">{e(flash)}</div>' if flash else ""
    return HTMLResponse(
        "<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{e(title)} · Shipcast</title><style>{CSS}</style>"
        "<script>if(window.top!==window)document.documentElement.className='embedded'</script></head><body>"
        f'<header><span class=brand>{MARK}<span>shipcast<i>.</i></span></span><a href="/launches">Open Studio</a></header>'
        f"<main>{tabs}<h1>{e(title)}</h1>{msg}{body}</main></body></html>", headers=HEADERS)


def copy_btn(text: str) -> str:
    return (f'<button type=button class=ghost data-copy="{e(text)}" onclick="navigator.clipboard.writeText('
            f'this.dataset.copy);this.textContent=\'Copied\'">Copy</button>')


def same_site(req: Request) -> bool:
    """POSTs must come from our own pages: Studio's login cookie is SameSite=None, so check Origin (CSRF)."""
    return req.headers.get("origin") == STUDIO


async def form(req: Request) -> dict:
    return {k: v[0] for k, v in parse_qs((await req.body()).decode(), keep_blank_values=True).items()}


def login_page() -> HTMLResponse:
    return page("Log in to Shipcast", f'<div class=card><p>Log in to Shipcast Studio first, then reload this page.</p>'
                                      f'<a class=btn href="{STUDIO}/auth/login" target=_blank>Log in</a></div>')


def admins_only() -> HTMLResponse:
    return page("Workspace admins only", "<div class=card><p class=mute>Ask an admin of this workspace to manage "
                                         "Claude, the brand profile and kits.</p></div>")


def ui(path: str, methods=("GET",), super_only=False):
    """Register a page that needs a Studio login with an admin role in the current workspace."""
    def wrap(fn):
        async def handler(req: Request):
            if req.method == "POST" and not same_site(req):
                return Response("Forbidden", status_code=403)
            user = await asyncio.to_thread(studio_user, req)
            if not user:
                return login_page()
            if user["role"] not in ADMIN_ROLES:
                return admins_only()
            if super_only and not user["super"]:
                return Response(status_code=404)
            return await fn(req, user)
        mcp.custom_route(path, methods=list(methods))(handler)
        return fn
    return wrap


@ui("/", methods=("GET", "POST"))
async def overview(req: Request, user: dict):
    org, flash = user["org"], ""
    if req.method == "POST":
        f = await form(req)
        g = db.execute("select c.info from oauth_grants g left join oauth_clients c on c.id=g.client_id "
                       "where g.id=? and g.org=?", (f.get("grant", ""), org)).fetchone()
        if f.get("action") == "revoke" and g:
            grant_drop(f["grant"])
            name = g[0] and json.loads(g[0]).get("client_name") or "app"
            log(org, user["email"], f"app disconnected: {name}")
            flash = f"Disconnected {name}. It has to be approved again to get back in."
    st = await asyncio.to_thread(setup_status, org)
    steps = "".join(
        f'<li><span class="{"ok" if s["done"] else "bad"}">{"✓" if s["done"] else "✗"}</span> '
        f'<b>{e(s["step"])}</b> <span class=mute>{e(s["detail"])}</span></li>' for s in st["steps"])
    head = ("Ready: Claude can go straight to making content." if st["ready_for_generation"]
            else f'Next: <b>{e(st["next_step"])}</b>. Connect Claude below, then say "start brand onboarding".')
    oauth_prune()
    when = lambda t: time.strftime("%b %d %H:%M", time.localtime(t)) if t else "not yet"  # noqa: E731
    apps = []
    for gid, email, created, used, info in db.execute(
            "select g.id, g.email, g.created, g.used, c.info from oauth_grants g left join oauth_clients c "
            "on c.id=g.client_id where g.org=? order by g.created desc", (org,)):
        c = json.loads(info or "{}")
        hosts = sorted({urlparse(u).hostname or u.split(":")[0] for u in c.get("redirect_uris") or []})
        apps.append(f"""<form method=post class=row><input type=hidden name=action value=revoke>
          <input type=hidden name=grant value="{gid}"><span><b>{e((c.get("client_name") or "app")[:80])}</b>
          <span class=mute>{e(", ".join(hosts))} · by {e(email or "?")} · connected {when(created)} ·
          last used {when(used)}</span></span><button class=ghost>Disconnect</button></form>""")
    cmd = f"claude mcp add --transport http --scope user shipcast {MCP_URL}"
    rows = db.execute("select ts, actor, action from events where org=? order by ts desc limit 10", (org,)).fetchall()
    logs = "".join(f"<li><span class=mute>{time.strftime('%b %d %H:%M', time.localtime(t))} · {e(a)}</span> {e(x)}</li>"
                   for t, a, x in rows) or "<li class=mute>nothing yet</li>"
    return page("Claude", f"""
      <div class=card><p>{head}</p><ul>{steps}</ul></div>
      <div class=card><h2>Connect Claude</h2>
        <label>1. Your MCP link</label><pre>{e(MCP_URL)}</pre>{copy_btn(MCP_URL)}
        <label>2a. Claude Code: run this, then type /mcp, pick shipcast and choose Authenticate</label>
        <pre>{e(cmd)}</pre>{copy_btn(cmd)}
        <label>2b. claude.ai, Claude Desktop or any other MCP app</label>
        <p class=mute>Settings → Connectors → Add custom connector, then paste the MCP link.</p>
        <label>3. Approve</label><p class=mute>A Shipcast page opens. While logged in here, click Allow.</p></div>
      <div class=card><h2>Connected apps</h2>{"".join(apps) or "<p class=mute>none yet</p>"}</div>
      <div class=card><h2>Recent activity</h2><ul>{logs}</ul></div>""", user, "/", flash)


@ui("/brand", methods=("GET", "POST"))
async def brand_page(req: Request, user: dict):
    org, flash = user["org"], ""
    b = oget(org, "brand", {})
    if req.method == "POST":
        f = await form(req)
        b = {k: f.get(k, "").strip() for k in BRAND_FIELDS} | {k: v for k, v in b.items() if k not in BRAND_FIELDS}
        b = {k: v for k, v in b.items() if str(v).strip()}
        oput(org, "brand", b)
        log(org, user["email"], "brand edited")
        flash = "Saved."
    fields = "".join(f'<label>{e(desc)}</label><textarea name="{k}">{e(str(b.get(k, "")))}</textarea>'
                     for k, desc in BRAND_FIELDS.items())
    return page("Brand profile", "<p class=mute>Claude fills this in with you during onboarding; edit anything here.</p>"
                                 f"<form method=post class=card>{fields}<button>Save</button></form>", user, "/brand", flash)


@ui("/kits", methods=("GET", "POST"))
async def kits_page(req: Request, user: dict):
    org, flash = user["org"], ""
    if req.method == "POST":
        f = await form(req)
        name = f.get("name", "").strip()
        if not KIT_NAME.match(name):
            flash = "Kit name: lowercase letters, digits and dashes."
        elif f.get("delete"):
            odrop(org, "kit:" + name)
            log(org, user["email"], f"kit deleted: {name}")
            flash = f"Deleted {name}."
        else:
            oput(org, "kit:" + name, f.get("content", ""))
            log(org, user["email"], f"kit saved: {name}")
            flash = f"Saved {name}."
    names = kit_names(org)
    cards = "".join(f"""<form method=post class=card><input type=hidden name=name value="{n}"><h2>{n}</h2>
      <div class=mute>{e(RECOMMENDED_KITS.get(n, ""))}</div><textarea name=content style="min-height:220px">{e(oget(org, "kit:" + n))}</textarea>
      <button>Save</button> <button class=ghost name=delete value=1>Delete</button></form>""" for n in names)
    missing = ", ".join(k for k in RECOMMENDED_KITS if k not in names)
    new = f"""<form method=post class=card><h2>New kit</h2>{f'<p class=mute>Recommended, not made yet: {missing}</p>' if missing else ''}
      <label>Name</label><input name=name placeholder="reel-style"><label>Content</label><textarea name=content></textarea>
      <button>Create</button></form>"""
    return page("Kits", cards + new, user, "/kits", flash)


@ui("/platforms", methods=("GET", "POST"), super_only=True)
async def platforms_page(req: Request, user: dict):
    flash = ""
    if req.method == "POST":
        f = await form(req)
        pid = f.get("platform", "")
        if pid in PLATFORMS:
            vars_ = PLATFORMS[pid][1] + PLATFORMS[pid][2]
            try:
                if f.get("clear"):
                    write_keys({v: "" for v in vars_}, user["org"], user["email"])
                    flash = f"Cleared {pid}. Studio restarts in the background (~1 min)."
                else:
                    upd = {v: f[v].strip() for v in vars_ if f.get(v, "").strip()}  # blank field = keep current
                    if upd:
                        write_keys(upd, user["org"], user["email"])
                        flash = f"Saved {pid}. Studio restarts in the background (~1 min)."
                    else:
                        flash = "Nothing to save: all fields were empty."
            except ValueError as ex:
                flash = str(ex)
    cards = []
    for p in sorted(platform_rows(), key=lambda r: not r["configured"]):
        fields = "".join(
            f'<label>{v} {"<span class=ok>· set</span>" if ok else ("<span class=mute>· optional</span>" if v in p["optional"] else "<span class=bad>· not set</span>")}</label>'
            f'<input name="{v}" type="{"password" if re.search("SECRET|TOKEN|KEY", v) else "text"}" '
            f'placeholder="{"leave blank to keep" if ok else ""}" autocomplete=off>'
            for v, ok in (p["keys"] | p["optional"]).items())
        uris = "".join(f"<pre>{e(u)}</pre>" for u in p["redirect_uris"])
        cards.append(f"""<form method=post class=card><input type=hidden name=platform value="{p['id']}">
          <h2>{e(p['label'])} <span class="{"ok" if p["configured"] else "mute"}">·
          {"configured" if p["configured"] else "not configured"}</span></h2>
          <p class=mute>{e(p['notes'])} <a href="{p['console']}" target=_blank>Developer console ↗</a></p>
          {f'<label>OAuth redirect URI(s) to register</label>{uris}' if uris else ''}{fields}
          <button>Save</button> <button class=ghost name=clear value=1>Clear</button></form>""")
    shown = [p["label"] for p in platform_rows() if p["configured"]]
    note = ("<p class=mute>Server-wide developer apps: every workspace connects its own accounts through them. Only "
            "Shipcast owners see this page. Studio lists every platform; one connects once all its required keys are "
            "set here. Saving restarts Studio for everyone (~1 min). Keyless platforms (Bluesky, WordPress, Medium, "
            "Dev.to, Hashnode, Lemmy, Nostr, Listmonk) work without keys.</p>"
            f"<p>Ready to connect: <b>{e(', '.join(shown) or 'keyless platforms only')}</b></p>")
    return page("Platforms", note + "".join(cards), user, "/platforms", flash)


# ------------------------------------------------------------------ public legal pages (no login)

CONTACT = os.environ.get("CONTACT_EMAIL", "")  # set on the server (systemd drop-in), not in the repo
LEGAL_DATE = "12 November 2026"


def contact_line() -> str:
    return (f'email <a href="mailto:{e(CONTACT)}">{e(CONTACT)}</a>' if CONTACT
            else "use the contact options inside Shipcast")


@mcp.custom_route("/privacy", methods=["GET"])
async def privacy(req: Request):
    return page("Privacy Policy", f"""<div class=card>
<p class=mute>Effective {LEGAL_DATE}. Shipcast ({e(STUDIO)}) is a social media scheduling and management platform.
This policy explains what we collect, why, and the choices you have.</p>
<h2>What we collect</h2><ul>
<li><b>Account data:</b> your email address, name and password (stored hashed) when you sign up.</li>
<li><b>Your content:</b> posts, media, schedules, brand profile and style kits you create in your workspace.</li>
<li><b>Connected platforms:</b> when you connect a social account (for example YouTube, Facebook, Instagram,
LinkedIn, TikTok), we receive access tokens and basic profile details (name, picture, account or channel ID)
from that platform, and the data needed to publish your posts and show your statistics.</li>
<li><b>Connected AI apps:</b> if you connect an AI assistant such as Claude, it can use your workspace through
the tools you approve; we record which apps you connected and when they were last used.</li>
<li><b>Technical data:</b> server logs (IP address, browser, errors) kept for security and reliability.</li></ul>
<h2>How we use it</h2>
<p>Only to provide the features you ask for: storing your content, publishing or scheduling it to the accounts
you connect, showing analytics, and keeping the service secure. We do not sell your data, do not use it for
advertising, and do not use it to train AI models.</p>
<h2>Google and YouTube data</h2>
<p>Shipcast uses YouTube API Services. When you connect YouTube we access your channel details, upload and manage
the videos you schedule, and read your channel statistics. Shipcast's use and transfer to any other app of
information received from Google APIs will adhere to the
<a href="https://developers.google.com/terms/api-services-user-data-policy" target=_blank>Google API Services User
Data Policy</a>, including the Limited Use requirements. By connecting YouTube you agree to the
<a href="https://www.youtube.com/t/terms" target=_blank>YouTube Terms of Service</a>; see also the
<a href="https://policies.google.com/privacy" target=_blank>Google Privacy Policy</a>. You can revoke Shipcast's
access at any time at <a href="https://myaccount.google.com/permissions" target=_blank>Google security settings</a>.</p>
<h2>Sharing</h2>
<p>Your content goes to the platforms you choose to post to. We use service providers to run Shipcast (our own
servers and Cloudflare for secure connections); they process data only to provide that service. We may disclose
data if required by law.</p>
<h2>Storage, security and retention</h2>
<p>Data is stored on our servers and sent over encrypted connections (HTTPS); access tokens are kept server-side
with restricted access. We keep your data while your account is active. Disconnecting a channel deletes its access
tokens; on request we delete your account and its data within 30 days.</p>
<h2>Your choices</h2>
<p>You can edit or delete your content, disconnect channels and AI apps from inside Shipcast, and ask us to export
or delete your data. Shipcast is not intended for children under 13.</p>
<h2>Changes and contact</h2>
<p>We will update the date above when this policy changes. Questions or requests: {contact_line()}.</p>
<p class=mute><a href="{P}/terms">Terms of Service</a></p></div>""")


@mcp.custom_route("/terms", methods=["GET"])
async def terms(req: Request):
    return page("Terms of Service", f"""<div class=card>
<p class=mute>Effective {LEGAL_DATE}. These terms apply to your use of Shipcast ({e(STUDIO)}). By creating an
account you agree to them.</p>
<h2>The service</h2>
<p>Shipcast lets you plan, create, schedule and publish content to social platforms you connect, and connect AI
assistants that act in your workspace on your behalf.</p>
<h2>Your account</h2>
<p>Keep your login secure; you are responsible for activity in your account and workspace, including actions an AI
app you connected takes there. Review anything an AI app prepares before you approve publishing it.</p>
<h2>Your content and platform rules</h2>
<p>You own your content and give us permission to store and process it only to run the service for you. You are
responsible for what you publish and must follow each platform's terms, including the
<a href="https://www.youtube.com/t/terms" target=_blank>YouTube Terms of Service</a> when you connect YouTube. Do not
use Shipcast for spam, harassment, illegal content or to break any platform's rules.</p>
<h2>Availability</h2>
<p>The service is provided "as is", without warranties. Platforms can change or limit their APIs, which can affect
features. To the extent the law allows, we are not liable for indirect or consequential losses, or for content
published through your account.</p>
<h2>Ending your use</h2>
<p>You can stop using Shipcast and ask us to delete your account at any time. We may suspend accounts that break
these terms.</p>
<h2>Changes and contact</h2>
<p>We may update these terms and will change the date above when we do. Questions: {contact_line()}.</p>
<p class=mute><a href="{P}/privacy">Privacy Policy</a></p></div>""")


@mcp.custom_route("/oauth/consent", methods=["GET", "POST"])
async def consent(req: Request):
    """An app asked for access (via /authorize). A workspace admin, logged in to Studio, allows or denies it."""
    if req.method == "POST" and not same_site(req):
        return Response("Forbidden", status_code=403)
    f = await form(req) if req.method == "POST" else {}
    rid = f.get("req") or req.query_params.get("req", "")
    pend = tok_get("pending", rid) if rid else None
    client = pend and await AUTH.get_client(pend["client_id"])
    if not client:
        return page("Link expired", "<p class=mute>Start connecting again from your app.</p>")
    user = await asyncio.to_thread(studio_user, req)
    if not user:
        return login_page()
    if user["role"] not in ADMIN_ROLES:
        return admins_only()
    p = AuthorizationParams.model_validate(pend["params"])
    back = str(p.redirect_uri)
    host = urlparse(back).hostname or back.split(":")[0]
    name = (client.client_name or "An app")[:80]
    if req.method == "POST":
        if not tok_get("pending", rid, take=True):
            return page("Link expired", "<p class=mute>Start connecting again from your app.</p>")
        if f.get("deny"):
            log(user["org"], user["email"], f"app denied: {name}")
            return RedirectResponse(construct_redirect_uri(back, error="access_denied", state=p.state), 303)
        if not user["api_key"]:
            return page("Workspace not ready", "<p class=mute>Studio did not return this workspace's API key.</p>")
        remember_org_key(user["org"], user["api_key"])
        gid = secrets.token_hex(8)
        with _lock:
            db.execute("insert into oauth_grants values(?,?,?,?,?,?,?)", (gid, client.client_id, user["org"],
                       user["email"], int(user["super"]), int(time.time()), 0))
        code = tok_put("code", client.client_id, gid, {
            "scopes": p.scopes or [], "code_challenge": p.code_challenge, "redirect_uri": back,
            "redirect_uri_provided_explicitly": p.redirect_uri_provided_explicitly, "resource": p.resource})
        log(user["org"], user["email"], f"app connected: {name} ({host})")
        return RedirectResponse(construct_redirect_uri(back, code=code, state=p.state), 303)
    warn = "" if host in KNOWN_HOSTS else (" <span class=bad>· not an app Shipcast recognises. Allow only if you "
                                           "started this connection yourself.</span>")
    return page("Connect an app", f"""<form method=post class=card><input type=hidden name=req value="{e(rid)}">
      <h2>{e(name)} wants to use Shipcast</h2>
      <p>Signed in as <b>{e(user["email"])}</b>. It gets every Shipcast tool for this workspace: brand, kits,
      channels and scheduling posts. You can disconnect it any time on the Claude page.</p>
      <p>After you allow, you go back to <b>{e(host)}</b>{warn}</p>
      <button>Allow</button> <button class=ghost name=deny value=1>Deny</button></form>""")


# ------------------------------------------------------------------ app: well-known discovery at the site root

mcp_app = mcp.http_app(path="/mcp", allowed_hosts=[urlparse(STUDIO).netloc, f"127.0.0.1:{PORT}", f"localhost:{PORT}"],
                       allowed_origins=[STUDIO, "https://claude.ai", "https://claude.com", "https://chatgpt.com"])


async def to_slash(req: Request):
    return RedirectResponse(P + "/", 307)


app = Starlette(routes=[*AUTH.get_well_known_routes(mcp_path="/mcp"), Route(P, to_slash), Mount(P, app=mcp_app)],
                lifespan=mcp_app.lifespan)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT, proxy_headers=False, log_level="warning")
