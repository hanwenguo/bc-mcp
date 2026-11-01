"""OAuth 2.1 authorization server for MCP clients, with login delegated to an OIDC provider.

Flow: client -> /authorize -> OIDC provider login -> /oauth/callback -> consent page ->
/oauth/consent -> client redirect_uri with our code -> /token. Static bearer tokens from the
config are accepted alongside OAuth-issued ones.
"""

from __future__ import annotations

import hmac
import html
import json
import logging
import re
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from ..config import ALL_SCOPES, READ_SCOPE, WRITE_SCOPE, AuthConfig, StaticToken, redirect_allowed
from .oidc import LoginError, OIDCClient, pkce_pair
from .store import Store, digest

log = logging.getLogger(__name__)

LOGIN_TTL = 600  # seconds a user has to finish logging in and consenting
CODE_TTL = 300
# Registration and /authorize are open to anyone, so everything they store is capped.
MAX_CLIENTS = 500
MAX_CLIENT_BYTES = 8192
MAX_CLIENT_NAME = 100
MAX_REDIRECT_URIS = 5
MAX_PENDING = 200
MAX_STATE = 512
MAX_FORM_BYTES = 4096
CODE_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")  # base64url(sha256), the only method the SDK accepts


class StaticTokens:
    """Long-lived tokens from the config file, compared by SHA-256 digest."""

    def __init__(self, tokens: tuple[StaticToken, ...], resource: str | None):
        self.tokens = tokens
        self.resource = resource

    def lookup(self, token: str) -> AccessToken | None:
        hashed = digest(token)
        for entry in self.tokens:
            if hmac.compare_digest(hashed, entry.sha256):
                return AccessToken(
                    token=token,
                    client_id=f"token:{entry.name}",
                    scopes=list(entry.scopes),
                    resource=self.resource,
                    subject=entry.name,
                    claims={"client_name": f"token:{entry.name}"},
                )
        return None

    async def verify_token(self, token: str) -> AccessToken | None:
        return self.lookup(token)


class LedgerOAuthProvider:
    def __init__(self, cfg: AuthConfig, store: Store, oidc: OIDCClient, resource: str):
        self.cfg = cfg
        self.store = store
        self.oidc = oidc
        self.resource = resource
        self.static = StaticTokens(cfg.tokens, resource)

    # -- clients (dynamic registration)

    def _redirects_allowed(self, uris) -> bool:
        return bool(uris) and all(redirect_allowed(str(u), self.cfg.allowed_redirects) for u in uris)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        info = self.store.get_client(client_id)
        if not info:
            return None
        client = OAuthClientInformationFull.model_validate(info)
        # A client registered before allowed_redirects was narrowed is treated as unknown, so the SDK
        # answers with an error page instead of redirecting to it.
        return client if self._redirects_allowed(client.redirect_uris) else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in client_info.redirect_uris or []]
        if not uris or len(uris) > MAX_REDIRECT_URIS:
            raise RegistrationError("invalid_redirect_uri", f"give 1 to {MAX_REDIRECT_URIS} redirect URIs")
        refused = next((u for u in uris if not redirect_allowed(u, self.cfg.allowed_redirects)), None)
        if refused:
            log.warning("refused OAuth registration with redirect URI %.200r", refused)
            raise RegistrationError(
                "invalid_redirect_uri",
                f"redirect URI {refused[:200]} is not allowed by this server; the ledger owner can add it to "
                "[auth] allowed_redirects",
            )
        if client_info.client_name and len(client_info.client_name) > MAX_CLIENT_NAME:
            raise RegistrationError("invalid_client_metadata", f"client_name is longer than {MAX_CLIENT_NAME}")
        info = client_info.model_dump(mode="json", exclude_none=True)
        if len(json.dumps(info)) > MAX_CLIENT_BYTES:
            raise RegistrationError("invalid_client_metadata", "client metadata is too large")
        # Make room by dropping clients that never got (or no longer hold) a token, so junk
        # registrations can't lock the owner out. Clients younger than a login are left alone.
        self.store.evict_clients(MAX_CLIENTS - 1, created_before=time.time() - LOGIN_TTL)
        if self.store.count_clients() >= MAX_CLIENTS:
            raise RegistrationError("invalid_client_metadata", "too many registered clients; try again later")
        self.store.put_client(client_info.client_id, info)
        log.info("registered OAuth client %s (%.100r)", client_info.client_id, client_info.client_name)

    # -- authorization

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        # Either form names this server: ChatGPT sends the bare origin, MCP clients the /mcp URL.
        ours = {self.resource.rstrip("/"), _origin(self.resource)}
        if params.resource and params.resource.rstrip("/") not in ours:
            raise AuthorizeError("invalid_target", f"this server only issues tokens for {self.resource}")
        if not CODE_CHALLENGE.fullmatch(params.code_challenge or ""):
            raise AuthorizeError("invalid_request", "code_challenge must be an S256 PKCE challenge")
        if params.state and len(params.state) > MAX_STATE:
            raise AuthorizeError("invalid_request", f"state is longer than {MAX_STATE} characters")
        if self.store.count_pending() >= MAX_PENDING:
            raise AuthorizeError("temporarily_unavailable", "too many logins in progress; try again in a few minutes")
        requested = params.scopes or (client.scope.split() if client.scope else [])
        # Clients that ask for none of our scopes (or for unrelated ones) get the full set to
        # choose from on the consent page. Write is never pre-selected there.
        scopes = [s for s in ALL_SCOPES if s in requested] or list(ALL_SCOPES)
        login_id = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(24)
        verifier, challenge = pkce_pair()
        self.store.put_pending(
            login_id,
            {
                "stage": "login",
                "client_id": client.client_id,
                "client_name": client.client_name,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "state": params.state,
                "scopes": scopes,
                "code_challenge": params.code_challenge,
                "nonce": nonce,
                "verifier": verifier,
            },
            LOGIN_TTL,
        )
        try:
            return await self.oidc.authorization_url(login_id, nonce, challenge)
        except LoginError as exc:
            log.error("OIDC provider unavailable: %s", exc)
            raise AuthorizeError("temporarily_unavailable", "the login provider is unavailable") from exc

    def _client_error(self, pending: dict[str, Any], error: str, description: str) -> Response:
        url = construct_redirect_uri(
            pending["redirect_uri"], error=error, error_description=description, state=pending.get("state")
        )
        return RedirectResponse(url, status_code=302)

    async def handle_callback(self, request: Request) -> Response:
        """The OIDC provider sends the browser here after login."""
        login_id = request.query_params.get("state", "")
        pending = self.store.get_pending(login_id)
        if not pending or pending.get("stage") != "login":
            return _page("Login expired", "<p>This login link is no longer valid. Start again from your app.</p>", 400)
        if request.query_params.get("error"):
            self.store.take_pending(login_id)
            return self._client_error(pending, "access_denied", request.query_params.get("error", "login failed"))
        code = request.query_params.get("code")
        if not code:
            return _page("Login failed", "<p>The login provider did not return a code.</p>", 400)
        try:
            claims = await self.oidc.finish(code, pending["verifier"], pending["nonce"])
        except LoginError as exc:
            log.warning("OIDC login failed: %s", exc)
            self.store.take_pending(login_id)
            return _page("Login failed", f"<p>{html.escape(str(exc))}</p>", 400)
        username = self.oidc.username(claims)
        if not self.oidc.allowed(claims):
            log.warning("OIDC user %s is not allowed", username)
            self.store.take_pending(login_id)
            return self._client_error(pending, "access_denied", "this user may not access the ledger")

        csrf = secrets.token_urlsafe(32)
        pending |= {"stage": "consent", "subject": username, "csrf": csrf}
        for key in ("verifier", "nonce"):
            pending.pop(key, None)
        consent_id = secrets.token_urlsafe(32)
        self.store.take_pending(login_id)
        self.store.put_pending(consent_id, pending, LOGIN_TTL)
        return _consent_page(consent_id, csrf, pending)

    async def handle_consent(self, request: Request) -> Response:
        form = await _small_form(request)
        if form is None:
            return _page("Bad request", "<p>This form could not be read.</p>", 400)
        consent_id = form.get("consent", "")
        pending = self.store.get_pending(consent_id)
        if (
            not pending
            or pending.get("stage") != "consent"
            or not hmac.compare_digest(form.get("csrf", ""), pending["csrf"])
        ):
            return _page("Request expired", "<p>This approval is no longer valid. Start again from your app.</p>", 400)
        self.store.take_pending(consent_id)
        if form.get("decision") != "approve":
            return self._client_error(pending, "access_denied", "the ledger owner declined")
        scopes = [READ_SCOPE] + ([WRITE_SCOPE] if form.get("write") and WRITE_SCOPE in pending["scopes"] else [])
        code = secrets.token_urlsafe(32)
        self.store.put_code(
            code,
            {
                "client_id": pending["client_id"],
                "scopes": scopes,
                "code_challenge": pending["code_challenge"],
                "redirect_uri": pending["redirect_uri"],
                "redirect_uri_provided_explicitly": pending["redirect_uri_provided_explicitly"],
                "subject": pending["subject"],
                "client_name": pending.get("client_name"),
                "expires_at": time.time() + CODE_TTL,
            },
            time.time() + CODE_TTL,
        )
        log.info("authorized client %s for %s with %s", pending["client_id"], pending["subject"], scopes)
        return RedirectResponse(
            construct_redirect_uri(pending["redirect_uri"], code=code, state=pending.get("state")), status_code=302
        )

    # -- codes and tokens

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        found = self.store.get_code(authorization_code)
        if not found:
            return None
        data, used_by = found
        if used_by:
            # A code presented twice was intercepted or replayed: revoke what it was exchanged for.
            log.warning("authorization code reused; revoking grant %s", used_by)
            self.store.revoke_grant(used_by)
            return None
        if data["client_id"] != client.client_id:
            return None
        return _Code(code=authorization_code, resource=self.resource, **data)

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code) -> OAuthToken:
        grant_id = secrets.token_urlsafe(16)
        used_by = self.store.use_code(authorization_code.code, grant_id)
        if used_by is not None:
            if used_by:
                self.store.revoke_grant(used_by)
            raise TokenError("invalid_grant", "authorization code was already used")
        now = time.time()
        grant = {
            "client_id": client.client_id,
            "client_name": authorization_code.client_name or client.client_name,
            "scopes": authorization_code.scopes,
            "subject": authorization_code.subject,
            "created_at": now,
            "expires_at": now + self.cfg.grant_ttl,
        }
        return self._issue(grant_id, grant)

    def _issue(self, grant_id: str, grant: dict[str, Any]) -> OAuthToken:
        access = "bcmo_" + secrets.token_urlsafe(32)
        refresh = "bcmr_" + secrets.token_urlsafe(32)
        now = time.time()
        end = grant.setdefault("expires_at", now + self.cfg.grant_ttl)
        access_until = min(now + self.cfg.access_token_ttl, end)
        self.store.put_token(access, "access", grant_id, grant, access_until)
        self.store.put_token(refresh, "refresh", grant_id, grant, min(now + self.cfg.refresh_token_ttl, end))
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=max(1, int(access_until - now)),
            refresh_token=refresh,
            scope=" ".join(grant["scopes"]),
        )

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        found = self.store.get_token(refresh_token, "refresh")
        if not found:
            used = self.store.get_token(refresh_token, "refresh_used")
            if used:
                # A rotated-away refresh token came back: one of the two holders is not the owner's
                # app. Revoke the whole grant; the owner reconnects.
                log.warning("refresh token reused; revoking grant %s (%s)", used[0], used[1].get("client_id"))
                self.store.revoke_grant(used[0])
            return None
        if found[1]["client_id"] != client.client_id:
            return None
        grant_id, grant, expires_at = found
        return _Refresh(
            token=refresh_token,
            client_id=grant["client_id"],
            scopes=grant["scopes"],
            expires_at=int(expires_at) if expires_at else None,
            resource=self.resource,
            subject=grant["subject"],
            grant_id=grant_id,
            client_name=grant.get("client_name"),
        )

    async def exchange_refresh_token(self, client, refresh_token, scopes: list[str]) -> OAuthToken:
        found = self.store.get_token(refresh_token.token, "refresh")
        if not found or not self.store.retire_refresh(refresh_token.token):
            raise TokenError("invalid_grant", "refresh token was already used")
        self.store.rotate_grant(refresh_token.grant_id)  # drop the old access token too
        granted = [s for s in ALL_SCOPES if s in scopes and s in refresh_token.scopes]
        if WRITE_SCOPE in granted and READ_SCOPE not in granted:
            granted.insert(0, READ_SCOPE)  # write implies read, as everywhere else
        grant = found[1] | {"scopes": granted or [READ_SCOPE]}
        return self._issue(refresh_token.grant_id, grant)

    async def load_access_token(self, token: str) -> AccessToken | None:
        static = self.static.lookup(token)
        if static:
            return static
        found = self.store.get_token(token, "access")
        if not found:
            return None
        grant_id, grant, expires_at = found
        return _Access(
            token=token,
            client_id=grant["client_id"],
            scopes=grant["scopes"],
            expires_at=int(expires_at) if expires_at else None,
            resource=self.resource,
            subject=grant["subject"],
            claims={"client_name": grant.get("client_name")},
            grant_id=grant_id,
        )

    async def revoke_token(self, token) -> None:
        grant_id = getattr(token, "grant_id", None)
        if grant_id:
            self.store.revoke_grant(grant_id)


class _Code(AuthorizationCode):
    client_name: str | None = None


class _Refresh(RefreshToken):
    grant_id: str
    client_name: str | None = None


class _Access(AccessToken):
    grant_id: str | None = None


# -- pages ------------------------------------------------------------------------------------

_STYLE = """
body{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem;color:#1d1d1f}
h1{font-size:1.4rem}code{background:#f2f2f2;padding:.1rem .3rem;border-radius:4px;word-break:break-all}
.box{border:1px solid #ddd;border-radius:8px;padding:1rem 1.2rem;margin:1rem 0}
button{font:inherit;padding:.5rem 1.2rem;border-radius:6px;border:1px solid #888;background:#fff;cursor:pointer}
button.primary{background:#1d1d1f;color:#fff;border-color:#1d1d1f}label{display:block;margin:.4rem 0}
button.primary{pointer-events:none;opacity:.5;animation:arm 0s 1s forwards}
@keyframes arm{to{pointer-events:auto;opacity:1}}
"""

_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = (
        f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><h1>{html.escape(title)}</h1>{body}</body></html>"
    )
    return HTMLResponse(doc, status_code=status, headers=_HEADERS)


async def _small_form(request: Request) -> dict[str, str] | None:
    """The urlencoded body of a small form, or None. Custom routes get no body limit from the SDK."""
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/x-www-form-urlencoded":
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            return None
    fields = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True, max_num_fields=10)
    return {key: values[0] for key, values in fields.items()}


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _destination(uri: str) -> str:
    """Where a redirect URI sends the browser, as the user should read it: host (and port, and the
    scheme unless it is https), or the scheme for app links."""
    parsed = urlparse(uri)
    if parsed.scheme in ("http", "https") and parsed.hostname:
        where = parsed.hostname + (f":{parsed.port}" if parsed.port else "")
        return where if parsed.scheme == "https" else f"{parsed.scheme}://{where}"
    return f"{parsed.scheme}:" if parsed.scheme else uri


def _consent_page(consent_id: str, csrf: str, pending: dict[str, Any]) -> HTMLResponse:
    name = html.escape(pending.get("client_name") or "An unnamed app")
    host = html.escape(_destination(pending["redirect_uri"]))
    redirect = html.escape(pending["redirect_uri"])
    user = html.escape(pending["subject"])
    can_write = WRITE_SCOPE in pending["scopes"]
    # Unchecked by default: granting changes is a deliberate act, not a side effect of "Approve".
    write_box = (
        "<label><input type=checkbox name=write value=1> Also allow changes: add, edit and delete entries</label>"
        if can_write
        else "<p>This app asked for read-only access.</p>"
    )
    body = f"""
<p>Signed in as <b>{user}</b>.</p>
<div class=box>
  <p><b>{name}</b> wants to access your ledger.</p>
  <p>After approval you will be sent to <code>{host}</code><br><small>{redirect}</small></p>
  <p>Only approve if you just started connecting this app yourself.</p>
</div>
<form method=post action="/oauth/consent">
  <input type=hidden name=consent value="{html.escape(consent_id)}">
  <input type=hidden name=csrf value="{html.escape(csrf)}">
  <label><input type=checkbox checked disabled> Read accounts, balances and entries</label>
  {write_box}
  <p><button class=primary name=decision value=approve>Approve</button>
     <button name=decision value=deny>Deny</button></p>
</form>"""
    return _page("Connect to your ledger", body)
