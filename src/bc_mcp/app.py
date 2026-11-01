"""Assemble the MCP server and its HTTP app from a Config."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx2
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .auth.oidc import OIDCClient
from .auth.provider import LedgerOAuthProvider, StaticTokens
from .auth.store import Store
from .config import ALL_SCOPES, READ_SCOPE, Config
from .gitops import GitRepo
from .ledger import Ledger
from .ratelimit import RateLimit
from .tools import build_instructions, load_policy, register_tools

log = logging.getLogger(__name__)


@dataclass
class Built:
    server: MCPServer
    ledger: Ledger
    provider: LedgerOAuthProvider | None


def build_server(cfg: Config, *, oidc_http: httpx2.AsyncClient | None = None) -> Built:
    ledger = Ledger(cfg.ledger)
    git = GitRepo(ledger.root, cfg.git)
    instructions = build_instructions(load_policy(cfg))
    common = dict(name="bc-mcp", title="Beancount ledger", version=__version__, instructions=instructions)

    provider = None
    if cfg.auth.oidc:
        resource = cfg.server.resource_url
        store = Store(cfg.auth.state_dir / "oauth.sqlite3")
        oidc = OIDCClient(cfg.auth.oidc, f"{cfg.server.public_origin}/oauth/callback", http=oidc_http)
        provider = LedgerOAuthProvider(cfg.auth, store, oidc, resource)
        auth = AuthSettings(
            issuer_url=cfg.server.public_origin,
            resource_server_url=resource,
            validate_token_resource=True,
            # No endpoint-wide scope: the SDK would advertise it as the only supported scope, and
            # clients would then never ask for write. Every token carries ledger:read (consent and
            # refresh always grant it, static tokens need a scope), and each tool checks its own.
            required_scopes=None,
            client_registration_options=ClientRegistrationOptions(enabled=True, default_scopes=list(ALL_SCOPES)),
            revocation_options=RevocationOptions(enabled=True),
        )
        server = MCPServer(**common, auth_server_provider=provider, auth=auth)
        server.custom_route("/oauth/callback", methods=["GET"], include_in_schema=False)(provider.handle_callback)
        server.custom_route("/oauth/consent", methods=["POST"], include_in_schema=False)(provider.handle_consent)
    elif cfg.auth.tokens:
        auth = AuthSettings(
            issuer_url=cfg.server.public_origin or f"http://{cfg.server.host}:{cfg.server.port}",
            resource_server_url=None,
            required_scopes=[READ_SCOPE],
        )
        server = MCPServer(**common, token_verifier=StaticTokens(cfg.auth.tokens, None), auth=auth)
    else:
        server = MCPServer(**common)

    @server.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    register_tools(server, cfg, ledger, git)
    return Built(server, ledger, provider)


def transport_security(cfg: Config) -> TransportSecuritySettings:
    hosts = [f"127.0.0.1:{cfg.server.port}", f"localhost:{cfg.server.port}", f"[::1]:{cfg.server.port}"]
    origins = [f"http://{h}" for h in hosts]
    if cfg.server.public_origin:
        parsed = urlparse(cfg.server.public_origin)
        hosts += [parsed.netloc]
        origins += [cfg.server.public_origin]
    origins += list(cfg.server.allowed_origins)
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins)


def build_app(cfg: Config, *, oidc_http: httpx2.AsyncClient | None = None) -> tuple[Starlette, Built]:
    built = build_server(cfg, oidc_http=oidc_http)
    app = built.server.streamable_http_app(
        streamable_http_path=cfg.server.path,
        stateless_http=True,
        json_response=True,
        transport_security=transport_security(cfg),
        host=cfg.server.host,
    )
    app.add_middleware(RateLimit)
    return app, built
