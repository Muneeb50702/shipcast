"""Smoke test: a fake Studio (login check, channels API, MCP that echoes the key it was called with) plus app.py on a
temp DB. Checks Studio-login access, workspace isolation, CSRF, owner-only platform keys and the OAuth/MCP flow.
Run: python test_hub.py"""
import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs as qs, urlencode, urlparse

import uvicorn
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.dependencies import get_http_request
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

PORT, STUDIO_PORT = 4599, 4598
BASE = f"http://127.0.0.1:{PORT}"
C = BASE + "/control"
tmp = tempfile.mkdtemp()

# ---- fake Studio: cookie auth=<name> -> user; API keys key-<org>
USERS = {
    "alice": {"id": "u1", "email": "alice@a.test", "orgId": "orgA", "role": "SUPERADMIN", "admin": False, "publicApi": "key-A"},
    "bob": {"id": "u2", "email": "bob@b.test", "orgId": "orgB", "role": "SUPERADMIN", "admin": False, "publicApi": "key-B"},
    "mia": {"id": "u3", "email": "mia@a.test", "orgId": "orgA", "role": "USER", "admin": False, "publicApi": ""},
    "owner": {"id": "u0", "email": "owner@o.test", "orgId": "orgO", "role": "SUPERADMIN", "admin": True, "publicApi": "key-O"},
}
fake_mcp = FastMCP("fake-studio")


@fake_mcp.tool
def whoami() -> str:
    return get_http_request().headers.get("authorization", "")


fake_mcp_app = fake_mcp.http_app(path="/mcp")


async def self_(req: Request):
    u = USERS.get(req.cookies.get("auth", ""))
    return JSONResponse(u) if u else Response(status_code=401)


async def integrations(req: Request):
    return JSONResponse([{"providerIdentifier": "bluesky", "name": "alice.bsky"}]
                        if req.headers.get("authorization") == "key-A" else [])


studio = Starlette(routes=[Route("/api/user/self", self_), Route("/api/public/v1/integrations", integrations),
                           Mount("/api", app=fake_mcp_app)], lifespan=fake_mcp_app.lifespan)
threading.Thread(target=uvicorn.run, args=(studio,), kwargs={"host": "127.0.0.1", "port": STUDIO_PORT,
                                                             "log_level": "warning"}, daemon=True).start()

env = os.environ | {"HUB_DB": f"{tmp}/hub.db", "POSTIZ_KEYS_FILE": f"{tmp}/keys.env", "HUB_PORT": str(PORT),
                    "STUDIO_PUBLIC": BASE, "POSTIZ_LOCAL": f"http://127.0.0.1:{STUDIO_PORT}"}
srv = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(__file__), "app.py")], env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


opener = urllib.request.build_opener(NoRedirect)


def req(url, data=None, who="", origin=BASE, as_json=False):
    body = (json.dumps(data) if as_json else urlencode(data)).encode() if data is not None else None
    h = {"Content-Type": "application/json" if as_json else "application/x-www-form-urlencoded"}
    if who:
        h["Cookie"] = f"auth={who}"
    if origin:
        h["Origin"] = origin
    try:
        resp = opener.open(urllib.request.Request(url, data=body, headers=h), timeout=15)
    except urllib.error.HTTPError as ex:
        resp = ex
    raw = resp.read().decode()
    return resp.status, resp.headers, (json.loads(raw) if raw.startswith(("{", "[")) else raw)


async def tools(tok, call=None, args=None):
    async with Client(StreamableHttpTransport(C + "/mcp", headers={"Authorization": "Bearer " + tok})) as c:
        if call:
            r = await c.call_tool(call, args or {})
            return r.data if r.data is not None else r.content[0].text
        return {t.name for t in await c.list_tools()}


def dead(tok):
    try:
        asyncio.run(tools(tok))
    except Exception:
        return True
    return False


verifier = secrets.token_urlsafe(48)
challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
CB = "http://localhost:9999/cb"


def connect(who, name):
    """Register an app, approve it as `who`, return (client, tokens)."""
    st, _, cl = req(C + "/register", {"client_name": name, "redirect_uris": [CB], "token_endpoint_auth_method": "none",
                                      "grant_types": ["authorization_code", "refresh_token"],
                                      "response_types": ["code"]}, as_json=True, origin="")
    assert st == 201, cl
    q = urlencode({"response_type": "code", "client_id": cl["client_id"], "redirect_uri": CB, "state": "xyz",
                   "code_challenge": challenge, "code_challenge_method": "S256", "scope": "shipcast",
                   "resource": C + "/mcp"})
    st, h, _ = req(C + "/authorize?" + q, origin="")
    assert st in (302, 307) and "/control/oauth/consent?req=" in h["location"], (st, dict(h))
    rid = qs(urlparse(h["location"]).query)["req"][0]
    assert "Log in to Shipcast" in req(C + "/oauth/consent?req=" + rid)[2], "consent needs a Studio login"
    body = req(C + "/oauth/consent?req=" + rid, who=who)[2]
    assert name in body and USERS[who]["email"] in body and "not an app" not in body, body[-600:]
    assert req(C + "/oauth/consent", {"req": rid}, who=who, origin="https://evil.test")[0] == 403, "CSRF"
    st, h, _ = req(C + "/oauth/consent", {"req": rid}, who=who)
    loc = urlparse(h["location"])
    assert st == 303 and h["location"].startswith(CB) and qs(loc.query)["state"] == ["xyz"], h.get("location")
    form = {"grant_type": "authorization_code", "code": qs(loc.query)["code"][0], "redirect_uri": CB,
            "client_id": cl["client_id"], "code_verifier": verifier, "resource": C + "/mcp"}
    assert req(C + "/token", form | {"code_verifier": "x" * 50}, origin="")[0] >= 400, "PKCE"
    st, _, tok = req(C + "/token", form, origin="")
    assert st == 200 and tok["access_token"], tok
    assert req(C + "/token", form, origin="")[0] >= 400, "codes are one-time"
    return cl, tok


try:
    for _ in range(80):
        try:
            if req(C + "/")[0] == 200:
                break
        except Exception:
            pass
        time.sleep(0.25)

    # ---- UI: Studio login is the only way in
    assert "Log in to Shipcast" in req(C + "/")[2]
    assert "Workspace admins only" in req(C + "/", who="mia")[2]
    st, h, body = req(C + "/", who="alice")
    assert st == 200 and C + "/mcp" in body and "SAMEORIGIN" in h["x-frame-options"], body[-400:]
    assert req(C, who="alice")[0] == 307
    assert req(C + "/brand", {"name": "Alice Co"}, who="alice", origin="https://evil.test")[0] == 403, "CSRF"
    assert "Saved." in req(C + "/brand", {"name": "Alice Co", "handle": "@alice"}, who="alice")[2]
    assert "Alice Co" in req(C + "/brand", who="alice")[2] and "Alice Co" not in req(C + "/brand", who="bob")[2]
    assert "Saved reel-style" in req(C + "/kits", {"name": "reel-style", "content": "# alice reel"}, who="alice")[2]
    assert "reel-style" not in req(C + "/kits", who="bob")[2].split("New kit")[0], "kits are per workspace"
    # owner-only platform keys
    assert req(C + "/platforms", who="alice")[0] == 404 and "Platforms" not in req(C + "/", who="alice")[2]
    assert "Saved facebook" in req(C + "/platforms", {"platform": "facebook", "FACEBOOK_APP_ID": "123",
                                                      "FACEBOOK_APP_SECRET": "abc"}, who="owner")[2]
    keys = open(f"{tmp}/keys.env").read()
    assert "FACEBOOK_APP_ID='123'" in keys and "HIDDEN_PROVIDERS" not in keys, keys
    assert "no spaces" in req(C + "/platforms", {"platform": "facebook", "FACEBOOK_APP_ID": "a b"}, who="owner")[2]
    assert "Saved farcaster" in req(C + "/platforms", {"platform": "farcaster", "NEYNAR_APP_MNEMONIC": "apple bird cat"},
                                    who="owner")[2] and "NEYNAR_APP_MNEMONIC='apple bird cat'" in open(f"{tmp}/keys.env").read()
    assert "single spaces" in req(C + "/platforms", {"platform": "farcaster", "NEYNAR_APP_MNEMONIC": "a  'b"}, who="owner")[2]
    assert "X_API_KEY" in req(C + "/platforms", who="owner")[2]

    # ---- OAuth discovery at the site root (what the tunnel routes here)
    prm = req(BASE + "/.well-known/oauth-protected-resource/control/mcp")[2]
    assert prm["resource"].rstrip("/") == C + "/mcp", prm
    meta = req(BASE + "/.well-known/oauth-authorization-server/control")[2]
    assert meta["authorization_endpoint"] == C + "/authorize" and meta["registration_endpoint"], meta

    # ---- each workspace's Claude sees only its own data and its own Studio key
    a_cl, a_tok = connect("alice", "Alice Claude")
    b_cl, b_tok = connect("bob", "Bob Claude")
    names = asyncio.run(tools(a_tok["access_token"]))
    assert {"setup_status", "get_brand", "save_kit", "studio_whoami"} <= names, names
    assert not [n for n in names if "postiz" in n.lower()], names
    assert asyncio.run(tools(a_tok["access_token"], "studio_whoami")) == "Bearer key-A"
    assert asyncio.run(tools(b_tok["access_token"], "studio_whoami")) == "Bearer key-B"
    assert asyncio.run(tools(a_tok["access_token"], "get_brand"))["brand"]["name"] == "Alice Co"
    assert asyncio.run(tools(b_tok["access_token"], "get_brand"))["brand"] == {}
    st_a = asyncio.run(tools(a_tok["access_token"], "setup_status"))
    assert st_a["steps"][2]["done"] and "alice.bsky" in st_a["steps"][2]["detail"], st_a
    assert not asyncio.run(tools(b_tok["access_token"], "setup_status"))["steps"][2]["done"]
    try:
        asyncio.run(tools(a_tok["access_token"], "set_platform_keys", {"platform": "telegram", "values": {"TELEGRAM_TOKEN": "1:A"}}))
        raise AssertionError("customer changed server keys")
    except AssertionError:
        raise
    except Exception as ex:
        assert "owner" in str(ex), ex
    assert "Bob Claude" not in req(C + "/", who="alice")[2] and "Alice Claude" in req(C + "/", who="alice")[2]

    # refresh rotates; disconnecting one workspace's app leaves the other alone
    st, _, a2 = req(C + "/token", {"grant_type": "refresh_token", "refresh_token": a_tok["refresh_token"],
                                   "client_id": a_cl["client_id"]}, origin="")
    assert st == 200 and a2["access_token"] != a_tok["access_token"], a2
    assert req(C + "/token", {"grant_type": "refresh_token", "refresh_token": a_tok["refresh_token"],
                              "client_id": a_cl["client_id"]}, origin="")[0] >= 400, "refresh tokens rotate"
    assert dead(a_tok["access_token"]) and dead("made-up-token")
    gid = re.search(r'name=grant value="([0-9a-f]+)"', req(C + "/", who="alice")[2]).group(1)
    assert req(C + "/", {"action": "revoke", "grant": gid}, who="bob")[2].count("Disconnected") == 0, "cross-org revoke"
    assert "Disconnected Alice Claude" in req(C + "/", {"action": "revoke", "grant": gid}, who="alice")[2]
    assert dead(a2["access_token"]) and not dead(b_tok["access_token"])

    # deny
    st, _, cl = req(C + "/register", {"client_name": "Nope", "redirect_uris": [CB], "token_endpoint_auth_method": "none",
                                      "grant_types": ["authorization_code"], "response_types": ["code"]}, as_json=True, origin="")
    q = urlencode({"response_type": "code", "client_id": cl["client_id"], "redirect_uri": CB, "state": "s",
                   "code_challenge": challenge, "code_challenge_method": "S256"})
    rid = qs(urlparse(req(C + "/authorize?" + q, origin="")[1]["location"]).query)["req"][0]
    st, h, _ = req(C + "/oauth/consent", {"req": rid, "deny": "1"}, who="bob")
    assert st == 303 and "error=access_denied" in h["location"]

    for path in ("/privacy", "/terms"):  # public legal pages: no login, Google's required statements
        st, _, body = req(C + path)
        assert st == 200 and "Log in to Shipcast" not in body and "youtube.com/t/terms" in body, path
    assert "Limited Use" in req(C + "/privacy")[2]
    for path in ("/", "/brand", "/kits", "/platforms"):  # brand: Shipcast everywhere, no upstream name
        body = req(C + path, who="owner")[2]
        assert "shipcast" in body and "Postiz" not in body, path
    print("ALL HUB CHECKS PASSED")
finally:
    srv.terminate()
    out = srv.communicate(timeout=5)[0]
    if "Traceback" in out:
        print(out[-3000:])
