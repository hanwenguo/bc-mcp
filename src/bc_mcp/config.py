"""Server configuration, loaded from a TOML file."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

READ_SCOPE = "ledger:read"
WRITE_SCOPE = "ledger:write"
ALL_SCOPES = (READ_SCOPE, WRITE_SCOPE)

_SCOPE_ALIASES = {"read": READ_SCOPE, "write": WRITE_SCOPE}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class LayoutConfig:
    # Where new entries go. Patterns are str.format()ed with year, month and day.
    dated_file: str = "books/{year}/{month:02d}/{month:02d}.beancount"
    # Optional per-year index that includes the dated files; None includes them from main.
    year_index: str | None = "books/{year}/{year}.beancount"
    # open/close/commodity directives go here; None puts them in dated files too.
    accounts_file: str | None = "accounts.beancount"
    # Column where posting numbers end, used when a file has no postings to copy from.
    amount_column: int = 47


@dataclass(frozen=True)
class LedgerConfig:
    root: Path
    main: str = "main.beancount"
    # Ledger-relative file served as the MCP instructions (the bookkeeping policy).
    instructions: str | None = "README.md"
    layout: LayoutConfig = field(default_factory=LayoutConfig)

    @property
    def main_path(self) -> Path:
        return self.root / self.main


@dataclass(frozen=True)
class GitConfig:
    enabled: bool = True
    author_name: str = "bc-mcp"
    author_email: str = "bc-mcp@localhost"


@dataclass(frozen=True)
class StaticToken:
    name: str
    sha256: str
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class OIDCConfig:
    issuer: str
    client_id: str
    client_secret: str
    allowed_users: tuple[str, ...] = ()
    allowed_groups: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ("openid", "profile", "email", "groups")
    username_claim: str = "preferred_username"
    token_endpoint_auth_method: str = "client_secret_basic"


@dataclass(frozen=True)
class AuthConfig:
    tokens: tuple[StaticToken, ...] = ()
    oidc: OIDCConfig | None = None
    state_dir: Path | None = None
    access_token_ttl: int = 3600
    refresh_token_ttl: int = 30 * 86400
    # Absolute lifetime of an OAuth grant: after this, refreshing stops and the owner logs in again.
    grant_ttl: int = 90 * 86400
    # Where OAuth clients may send the browser back to, as URL prefixes. Loopback (http://localhost,
    # 127.0.0.1, [::1], any port) is always allowed. Registration with any other redirect URI is refused,
    # so a stranger's client can't collect a code even if the owner is tricked into approving it.
    allowed_redirects: tuple[str, ...] = (
        "https://claude.ai/",
        "https://claude.com/",
        "https://chatgpt.com/connector/oauth/",  # ChatGPT apps: one callback per connector
        "https://chatgpt.com/connector_platform_oauth_redirect",  # ChatGPT's older shared callback
    )

    @property
    def enabled(self) -> bool:
        return bool(self.tokens) or self.oidc is not None


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    path: str = "/mcp"
    # Public origin clients reach the server at, e.g. https://ledger.example.com.
    public_url: str | None = None
    # Extra browser origins allowed to call /mcp (requests without an Origin header are always allowed).
    allowed_origins: tuple[str, ...] = ()

    @property
    def public_origin(self) -> str | None:
        return self.public_url.rstrip("/") if self.public_url else None

    @property
    def resource_url(self) -> str | None:
        return self.public_origin + self.path if self.public_origin else None


@dataclass(frozen=True)
class Config:
    ledger: LedgerConfig
    git: GitConfig = field(default_factory=GitConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    server: ServerConfig = field(default_factory=ServerConfig)


def redirect_allowed(uri: str, prefixes: tuple[str, ...]) -> bool:
    """Whether an OAuth redirect URI is loopback or starts with one of the allowed prefixes."""
    parsed = urlparse(uri)
    if "@" in parsed.netloc or parsed.fragment:
        return False
    if parsed.scheme in ("http", "https") and parsed.hostname in ("localhost", "127.0.0.1", "::1"):
        return True
    return any(uri.startswith(prefix) for prefix in prefixes)


def _redirect_prefix(prefix: Any) -> str:
    parsed = urlparse(str(prefix))
    # The prefix must end its host part, so "https://claude.ai/" can't match "https://claude.ai.evil.com".
    if not parsed.scheme or "@" in parsed.netloc or not parsed.path.startswith("/"):
        raise ConfigError(f"[auth] allowed_redirects entry {prefix!r} must be a URL prefix like 'https://claude.ai/'")
    return str(prefix)


def normalize_scopes(values: Any) -> tuple[str, ...]:
    if isinstance(values, str):
        values = values.replace(",", " ").split()
    scopes = []
    for value in values:
        scope = _SCOPE_ALIASES.get(value, value)
        if scope not in ALL_SCOPES:
            raise ConfigError(f"unknown scope {value!r}; use read or write")
        if scope not in scopes:
            scopes.append(scope)
    if WRITE_SCOPE in scopes and READ_SCOPE not in scopes:
        scopes.insert(0, READ_SCOPE)
    return tuple(scopes)


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def _path(value: str, base: Path) -> Path:
    path = Path(os.path.expanduser(value))
    return path if path.is_absolute() else (base / path)


def _secret(table: dict[str, Any], key: str, env: str, base: Path) -> str | None:
    if os.environ.get(env):
        return os.environ[env]
    if table.get(f"{key}_file"):
        return _path(table[f"{key}_file"], base).read_text().strip()
    return table.get(key)


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    return parse_config(data, base=path.parent)


def parse_config(data: dict[str, Any], base: Path) -> Config:
    ledger_t = _section(data, "ledger")
    if "root" not in ledger_t:
        raise ConfigError("[ledger] root is required")
    layout_t = _section(ledger_t, "layout")
    layout = LayoutConfig(
        dated_file=layout_t.get("dated_file", LayoutConfig.dated_file),
        year_index=layout_t.get("year_index", LayoutConfig.year_index) or None,
        accounts_file=layout_t.get("accounts_file", LayoutConfig.accounts_file) or None,
        amount_column=int(layout_t.get("amount_column", LayoutConfig.amount_column)),
    )
    ledger = LedgerConfig(
        root=_path(ledger_t["root"], base).resolve(),
        main=ledger_t.get("main", "main.beancount"),
        instructions=ledger_t.get("instructions", "README.md") or None,
        layout=layout,
    )
    if not ledger.main_path.is_file():
        raise ConfigError(f"main ledger file not found: {ledger.main_path}")

    git_t = _section(data, "git")
    git = GitConfig(
        enabled=bool(git_t.get("enabled", True)),
        author_name=git_t.get("author_name", GitConfig.author_name),
        author_email=git_t.get("author_email", GitConfig.author_email),
    )

    server_t = _section(data, "server")
    server = ServerConfig(
        host=server_t.get("host", ServerConfig.host),
        port=int(server_t.get("port", ServerConfig.port)),
        path=server_t.get("path", ServerConfig.path),
        public_url=server_t.get("public_url"),
        allowed_origins=tuple(server_t.get("allowed_origins", ())),
    )
    if server.public_url:
        parsed = urlparse(server.public_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path not in ("", "/"):
            raise ConfigError("[server] public_url must be an origin like https://ledger.example.com")

    auth_t = _section(data, "auth")
    tokens = []
    for entry in auth_t.get("tokens", []):
        name, digest = entry.get("name"), str(entry.get("sha256", "")).lower()
        if not name or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ConfigError("each [[auth.tokens]] needs a name and a 64-hex sha256 (see `bc-mcp token`)")
        scopes = normalize_scopes(entry.get("scopes", ["read"]))
        if not scopes:
            raise ConfigError(f"[[auth.tokens]] {name}: scopes must include read or write")
        tokens.append(StaticToken(name, digest, scopes))

    oidc = None
    if "oidc" in auth_t:
        oidc_t = _section(auth_t, "oidc")
        secret = _secret(oidc_t, "client_secret", "BC_MCP_OIDC_CLIENT_SECRET", base)
        missing = [k for k in ("issuer", "client_id") if not oidc_t.get(k)]
        if not secret:
            missing.append("client_secret / client_secret_file")
        if missing:
            raise ConfigError(f"[auth.oidc] missing {', '.join(missing)}")
        oidc = OIDCConfig(
            issuer=oidc_t["issuer"].rstrip("/"),
            client_id=oidc_t["client_id"],
            client_secret=secret,
            allowed_users=tuple(oidc_t.get("allowed_users", ())),
            allowed_groups=tuple(oidc_t.get("allowed_groups", ())),
            scopes=tuple(oidc_t.get("scopes", OIDCConfig.scopes)),
            username_claim=oidc_t.get("username_claim", OIDCConfig.username_claim),
            token_endpoint_auth_method=oidc_t.get("token_endpoint_auth_method", OIDCConfig.token_endpoint_auth_method),
        )
        if not oidc.allowed_users and not oidc.allowed_groups:
            raise ConfigError("[auth.oidc] set allowed_users or allowed_groups, or anyone in your IdP gets in")
        if oidc.token_endpoint_auth_method not in ("client_secret_basic", "client_secret_post"):
            raise ConfigError("[auth.oidc] token_endpoint_auth_method must be client_secret_basic or _post")
        if not server.public_url:
            raise ConfigError("[server] public_url is required when [auth.oidc] is set")

    state_dir = auth_t.get("state_dir")
    auth = AuthConfig(
        tokens=tuple(tokens),
        oidc=oidc,
        state_dir=_path(state_dir, base) if state_dir else None,
        access_token_ttl=int(auth_t.get("access_token_ttl", AuthConfig.access_token_ttl)),
        refresh_token_ttl=int(auth_t.get("refresh_token_ttl", AuthConfig.refresh_token_ttl)),
        grant_ttl=int(auth_t.get("grant_ttl", AuthConfig.grant_ttl)),
        allowed_redirects=tuple(
            _redirect_prefix(p) for p in auth_t.get("allowed_redirects", AuthConfig.allowed_redirects)
        ),
    )
    if oidc and not auth.state_dir:
        raise ConfigError("[auth] state_dir is required when [auth.oidc] is set (OAuth clients and tokens live there)")

    return Config(ledger=ledger, git=git, auth=auth, server=server)
