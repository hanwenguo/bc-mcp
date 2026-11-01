"""End to end over real HTTP: static tokens, and the OAuth flow with a fake OIDC provider (Authelia)."""

import base64
import contextlib
import hashlib
import json
import re
import secrets
import socket
import threading
import time
from urllib.parse import parse_qs, urlencode, urlparse

import httpx2
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from bc_mcp.app import build_app
from bc_mcp.config import (
    ALL_SCOPES,
    READ_SCOPE,
    AuthConfig,
    Config,
    LedgerConfig,
    OIDCConfig,
    ServerConfig,
    StaticToken,
)

from .conftest import git

pytestmark = pytest.mark.anyio

READ_TOKEN, WRITE_TOKEN = "bcm_read_" + "r" * 30, "bcm_write_" + "w" * 30


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


class ServerThread:
    def __init__(self, sock: socket.socket, app):
        self.sock = sock
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [sock]}, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            assert time.time() < deadline, "server did not start"
            time.sleep(0.02)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(5)


def listen() -> tuple[socket.socket, str]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    return sock, f"http://127.0.0.1:{sock.getsockname()[1]}"


class FakeAuthelia:
    """Just enough of an OIDC provider: discovery, auto-login authorize, token, JWKS, userinfo."""

    def __init__(self, url: str):
        self.url = url
        self.user = "alice"
        self.extra_claims: dict = {}
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.codes: dict[str, dict] = {}
        self.app = Starlette(
            routes=[
                Route("/.well-known/openid-configuration", self.discovery),
                Route("/api/oidc/authorization", self.authorize),
                Route("/api/oidc/token", self.token, methods=["POST"]),
                Route("/jwks.json", self.jwks),
                Route("/api/oidc/userinfo", self.userinfo),
            ]
        )

    async def discovery(self, _: Request):
        return JSONResponse({
            "issuer": self.url,
            "authorization_endpoint": f"{self.url}/api/oidc/authorization",
            "token_endpoint": f"{self.url}/api/oidc/token",
            "jwks_uri": f"{self.url}/jwks.json",
            "userinfo_endpoint": f"{self.url}/api/oidc/userinfo",
        })  # fmt: skip

    async def authorize(self, request: Request):
        params = dict(request.query_params)
        assert params["client_id"] == "bc-mcp" and params["code_challenge_method"] == "S256"
        code = secrets.token_urlsafe(16)
        self.codes[code] = params | {"user": self.user}
        query = urlencode({"code": code, "state": params["state"]})
        return RedirectResponse(f"{params['redirect_uri']}?{query}", status_code=302)

    async def token(self, request: Request):
        expected = "Basic " + base64.b64encode(b"bc-mcp:idp-secret").decode()
        if request.headers.get("authorization") != expected:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        form = await request.form()
        params = self.codes.pop(form["code"])
        assert s256(form["code_verifier"]) == params["code_challenge"]
        assert form["redirect_uri"] == params["redirect_uri"]
        now = int(time.time())
        claims = {"iss": self.url, "aud": "bc-mcp", "sub": f"sub-{params['user']}", "iat": now, "exp": now + 300,
                  "nonce": params["nonce"]} | self.extra_claims  # fmt: skip
        id_token = jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "k1"})
        return JSONResponse({"access_token": f"at-{params['user']}", "id_token": id_token, "token_type": "Bearer"})

    async def jwks(self, _: Request):
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return JSONResponse({"keys": [jwk | {"kid": "k1", "alg": "RS256", "use": "sig"}]})

    async def userinfo(self, request: Request):
        user = request.headers["authorization"].removeprefix("Bearer at-")
        return JSONResponse({"sub": f"sub-{user}", "preferred_username": user, "groups": ["family"]})


@pytest.fixture
def stack(ledger_dir, tmp_path):
    idp_sock, idp_url = listen()
    idp = FakeAuthelia(idp_url)
    mcp_sock, mcp_url = listen()
    cfg = Config(
        ledger=LedgerConfig(root=ledger_dir),
        auth=AuthConfig(
            tokens=(
                StaticToken("reader", sha(READ_TOKEN), (READ_SCOPE,)),
                StaticToken("agent", sha(WRITE_TOKEN), ALL_SCOPES),
            ),
            oidc=OIDCConfig(issuer=idp_url, client_id="bc-mcp", client_secret="idp-secret", allowed_users=("alice",)),
            state_dir=tmp_path / "state",
        ),
        server=ServerConfig(host="127.0.0.1", port=mcp_sock.getsockname()[1], public_url=mcp_url),
    )
    app, _ = build_app(cfg)
    with ServerThread(idp_sock, idp.app), ServerThread(mcp_sock, app):
        yield mcp_url, idp


@contextlib.asynccontextmanager
async def mcp_client(url: str, token: str):
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30) as http:
        async with Client(streamable_http_client(f"{url}/mcp", http_client=http)) as client:
            yield client


async def test_requests_without_a_valid_token_get_401_with_metadata(stack):
    url, _ = stack
    async with httpx2.AsyncClient() as http:
        response = await http.post(f"{url}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response.status_code == 401
        assert (
            f'resource_metadata="{url}/.well-known/oauth-protected-resource/mcp"'
            in response.headers["www-authenticate"]
        )
        bad = await http.post(f"{url}/mcp", json={}, headers={"Authorization": "Bearer nope"})
        assert bad.status_code == 401
        meta = (await http.get(f"{url}/.well-known/oauth-protected-resource/mcp")).json()
        assert meta["resource"] == f"{url}/mcp" and meta["authorization_servers"] == [f"{url}"]
        assert (await http.get(f"{url}/healthz")).json() == {"ok": True}


async def test_static_tokens_and_scopes(stack, ledger_dir):
    url, _ = stack
    async with mcp_client(url, READ_TOKEN) as client:
        info = await client.call_tool("ledger_info", {})
        assert not info.is_error and json.loads(info.content[0].text)["errors"] == 0
        denied = await client.call_tool("add_entries", {"text": '2026-10-31 note Assets:Bank:Acme:Checking "x"'})
        assert denied.is_error and "lacks the ledger:write scope" in denied.content[0].text
    async with mcp_client(url, WRITE_TOKEN) as client:
        ok = await client.call_tool("add_entries", {"text": '2026-10-31 note Assets:Bank:Acme:Checking "x"'})
        assert not ok.is_error, ok.content[0].text
    assert "Client: token:agent" in git(ledger_dir, "log", "-1", "--format=%b")


async def oauth_login(
    url: str,
    *,
    approve: bool = True,
    write: bool = True,
    scope: str | None = " ".join(ALL_SCOPES),
    registered_scope: str | None = None,
) -> dict:
    redirect_uri = "http://127.0.0.1:9/callback"
    async with httpx2.AsyncClient(follow_redirects=False) as http:
        reg = await http.post(
            f"{url}/register",
            json={"redirect_uris": [redirect_uri], "client_name": "Test Client", "token_endpoint_auth_method": "none"}
            | ({"scope": registered_scope} if registered_scope else {}),
        )
        assert reg.status_code == 201, reg.text
        client_id = reg.json()["client_id"]
        verifier = secrets.token_urlsafe(40)
        auth = await http.get(f"{url}/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "state": "st4te",
            "code_challenge": s256(verifier), "code_challenge_method": "S256", "resource": f"{url}/mcp",
        } | ({"scope": scope} if scope else {}))  # fmt: skip
        assert auth.status_code == 302, auth.text
        login = await http.get(auth.headers["location"])  # the fake IdP logs in immediately
        callback = await http.get(login.headers["location"])
        if callback.status_code == 302:  # rejected user: straight back to the client with an error
            return {"redirect": callback.headers["location"]}
        if callback.status_code == 400:
            return {"error_page": callback.text}
        assert callback.status_code == 200 and "Test Client" in callback.text and "http://127.0.0.1:9" in callback.text
        assert callback.headers["x-frame-options"] == "DENY"
        assert "name=write value=1>" in callback.text  # write is offered, never pre-checked
        fields = dict(re.findall(r'name=(\w+) value="([^"]*)"', callback.text))
        form = {"consent": fields["consent"], "csrf": fields["csrf"], "decision": "approve" if approve else "deny"}
        if write:
            form["write"] = "1"
        consent = await http.post(f"{url}/oauth/consent", data=form)
        assert consent.status_code == 302
        location = consent.headers["location"]
        query = parse_qs(urlparse(location).query)
        assert query["state"] == ["st4te"]
        if "code" not in query:
            return {"redirect": location}
        # Replaying the consent form must not mint a second code.
        assert (await http.post(f"{url}/oauth/consent", data=form)).status_code == 400
        token = await http.post(f"{url}/token", data={
            "grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": redirect_uri,
            "client_id": client_id, "code_verifier": verifier, "resource": f"{url}/mcp",
        })  # fmt: skip
        assert token.status_code == 200, token.text
        return token.json() | {"client_id": client_id, "code": query["code"][0], "verifier": verifier}


async def test_oauth_flow_through_the_oidc_provider(stack, ledger_dir):
    url, _ = stack
    tokens = await oauth_login(url)
    assert tokens["scope"] == "ledger:read ledger:write"
    async with mcp_client(url, tokens["access_token"]) as client:
        added = await client.call_tool("add_transaction", {
            "date": "2026-10-31", "payee": "Corner Cafe", "narration": "Coffee",
            "postings": [
                {"account": "Expenses:Food:Snacks", "amount": "4.50", "currency": "USD"},
                {"account": "Liabilities:CreditCard:Acme:Gold"},
            ],
        })  # fmt: skip
        assert not added.is_error, added.content[0].text
    assert "Client: Test Client (alice)" in git(ledger_dir, "log", "-1", "--format=%b")

    async with httpx2.AsyncClient() as http:
        refreshed = await http.post(f"{url}/token", data={
            "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": tokens["client_id"],
        })  # fmt: skip
        assert refreshed.status_code == 200, refreshed.text
        new = refreshed.json()
        assert new["access_token"] != tokens["access_token"]
        old = await http.post(f"{url}/mcp", json={}, headers={"Authorization": f"Bearer {tokens['access_token']}"})
        assert old.status_code == 401  # rotation revoked the old access token
        reused = await http.post(f"{url}/token", data={
            "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": tokens["client_id"],
        })  # fmt: skip
        assert reused.status_code == 400
        # Reuse of a rotated refresh token means it leaked: the whole grant is revoked.
        revoked = await http.post(f"{url}/mcp", json={}, headers={"Authorization": f"Bearer {new['access_token']}"})
        assert revoked.status_code == 401
        again = await http.post(f"{url}/token", data={
            "grant_type": "refresh_token", "refresh_token": new["refresh_token"], "client_id": tokens["client_id"],
        })  # fmt: skip
        assert again.status_code == 400


async def test_code_replay_revokes_the_tokens_it_produced(stack):
    url, _ = stack
    tokens = await oauth_login(url)
    async with httpx2.AsyncClient() as http:
        replay = await http.post(f"{url}/token", data={
            "grant_type": "authorization_code", "code": tokens["code"], "redirect_uri": "http://127.0.0.1:9/callback",
            "client_id": tokens["client_id"], "code_verifier": tokens["verifier"], "resource": f"{url}/mcp",
        })  # fmt: skip
        assert replay.status_code == 400
        check = await http.post(f"{url}/mcp", json={}, headers={"Authorization": f"Bearer {tokens['access_token']}"})
        assert check.status_code == 401


async def test_refresh_keeps_read_with_write(stack):
    url, _ = stack
    tokens = await oauth_login(url)
    async with httpx2.AsyncClient() as http:
        refreshed = await http.post(f"{url}/token", data={
            "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": tokens["client_id"],
            "scope": "ledger:write",
        })  # fmt: skip
        assert refreshed.status_code == 200 and refreshed.json()["scope"] == "ledger:read ledger:write"


async def register(http, url: str, uris: list[str], **extra):
    return await http.post(
        f"{url}/register", json={"redirect_uris": uris, "token_endpoint_auth_method": "none"} | extra
    )


async def test_registration_only_accepts_allowed_redirects(stack):
    url, _ = stack
    async with httpx2.AsyncClient() as http:
        for uri in (
            "https://evil.example/callback",
            "https://claude.ai@evil.example/api/mcp/auth_callback",
            "https://claude.ai.evil.example/api/mcp/auth_callback",
            "http://localhost@evil.example/callback",
            "http://claude.ai/api/mcp/auth_callback",
        ):
            reg = await register(http, url, [uri])
            assert reg.status_code == 400 and reg.json()["error"] == "invalid_redirect_uri", (uri, reg.text)
        for uri in (
            "https://claude.ai/api/mcp/auth_callback",
            "https://chatgpt.com/connector/oauth/abc123",
            "https://chatgpt.com/connector_platform_oauth_redirect",
            "http://localhost:33418/callback",
            "http://[::1]:5/cb",
        ):
            assert (await register(http, url, [uri])).status_code == 201, uri
        for uri in ("https://chatgpt.com/backend-api/x", "https://chatgpt.com.evil.example/connector/oauth/x"):
            assert (await register(http, url, [uri])).status_code == 400, uri
        too_long = await register(http, url, ["https://claude.ai/api/mcp/auth_callback"], client_name="x" * 500)
        assert too_long.status_code == 400


async def authorize(http, url: str, **params):
    reg = await register(http, url, ["http://127.0.0.1:9/callback"])
    query = {
        "response_type": "code", "client_id": reg.json()["client_id"], "redirect_uri": "http://127.0.0.1:9/callback",
        "state": "s", "code_challenge": s256("v" * 43), "code_challenge_method": "S256",
    } | params  # fmt: skip
    return await http.get(f"{url}/authorize", params=query)


async def test_authorize_input_is_bounded(stack, monkeypatch):
    url, _ = stack
    async with httpx2.AsyncClient() as http:
        assert "error=invalid_request" in (await authorize(http, url, code_challenge="x" * 5000)).headers["location"]
        assert "error=invalid_request" in (await authorize(http, url, state="s" * 1000)).headers["location"]
        monkeypatch.setattr("bc_mcp.auth.provider.MAX_PENDING", 2)
        for _ in range(2):
            assert "/api/oidc/authorization" in (await authorize(http, url)).headers["location"]
        assert "error=temporarily_unavailable" in (await authorize(http, url)).headers["location"]


async def test_resource_may_be_the_origin_or_the_mcp_url(stack):
    url, _ = stack
    async with httpx2.AsyncClient() as http:
        for resource in (url, f"{url}/", f"{url}/mcp"):  # ChatGPT sends the bare origin
            assert "/api/oidc/authorization" in (await authorize(http, url, resource=resource)).headers["location"]
        assert (
            "error=invalid_target"
            in (await authorize(http, url, resource="https://other.example/mcp")).headers["location"]
        )


async def test_consent_form_body_is_bounded(stack):
    url, _ = stack
    async with httpx2.AsyncClient() as http:
        form = {"content-type": "application/x-www-form-urlencoded"}
        big = await http.post(f"{url}/oauth/consent", content=b"consent=" + b"a" * 200_000, headers=form)
        assert big.status_code == 400
        multipart = await http.post(f"{url}/oauth/consent", files={"f": ("f", b"a" * 1000)})
        assert multipart.status_code == 400


async def test_id_token_for_another_client_is_refused(stack):
    url, idp = stack
    idp.extra_claims = {"aud": ["bc-mcp", "other-app"]}
    assert "issued to another client" in (await oauth_login(url))["error_page"]


async def test_clients_that_ask_for_no_scope_or_other_scopes_can_still_get_write(stack):
    url, _ = stack
    async with httpx2.AsyncClient() as http:
        prm = (await http.get(f"{url}/.well-known/oauth-protected-resource/mcp")).json()
        assert "scopes_supported" not in prm  # so clients don't narrow themselves to read-only
    assert (await oauth_login(url, scope=None))["scope"] == "ledger:read ledger:write"
    tokens = await oauth_login(url, scope="openid offline_access", registered_scope="openid offline_access")
    assert tokens["scope"] == "ledger:read ledger:write"
    async with mcp_client(url, tokens["access_token"]) as client:
        assert not (await client.call_tool("ledger_info", {})).is_error


async def test_oauth_read_only_consent(stack):
    url, _ = stack
    tokens = await oauth_login(url, write=False)
    assert tokens["scope"] == "ledger:read"
    async with mcp_client(url, tokens["access_token"]) as client:
        denied = await client.call_tool("delete_entry", {"id": "000000000000"})
        assert denied.is_error and "ledger:write" in denied.content[0].text


async def test_oauth_denied_consent_and_unknown_user(stack):
    url, idp = stack
    denied = await oauth_login(url, approve=False)
    assert "error=access_denied" in denied["redirect"]
    idp.user = "mallory"
    rejected = await oauth_login(url)
    assert "error=access_denied" in rejected["redirect"] and rejected["redirect"].startswith("http://127.0.0.1:9/")
