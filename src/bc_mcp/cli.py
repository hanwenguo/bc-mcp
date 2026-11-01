"""Command line: serve, check, token."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import secrets
import sys
from pathlib import Path

from .config import ConfigError, LedgerConfig, normalize_scopes


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .app import build_app
    from .config import Config, load_config

    if args.config:
        cfg = load_config(args.config)
    elif args.ledger:
        cfg = Config(ledger=LedgerConfig(root=Path(args.ledger).resolve()))
    else:
        print("give --config, or --ledger DIR --insecure-no-auth for local testing", file=sys.stderr)
        return 2
    if args.port:
        cfg = _with_server(cfg, port=args.port)
    if args.host:
        cfg = _with_server(cfg, host=args.host)
    if not cfg.auth.enabled:
        if not args.insecure_no_auth:
            print("no [auth] tokens or oidc configured; refusing to serve the ledger without auth", file=sys.stderr)
            return 2
        if not _is_loopback(cfg.server.host):
            print("--insecure-no-auth only works on a loopback host", file=sys.stderr)
            return 2
    app, _ = build_app(cfg)
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, proxy_headers=True, log_level=args.log_level)
    return 0


def _with_server(cfg, **changes):
    from dataclasses import replace

    return replace(cfg, server=replace(cfg.server, **changes))


def cmd_check(args: argparse.Namespace) -> int:
    from .auth.oidc import LoginError, OIDCClient
    from .config import load_config
    from .gitops import GitRepo
    from .ledger import Ledger

    cfg = load_config(args.config)
    ledger = Ledger(cfg.ledger)
    snap = ledger.snapshot()
    print(f"ledger: {ledger.root} ({len(snap.files)} files, {len(snap.entries)} entries, {len(snap.errors)} errors)")
    for error in snap.errors[:20]:
        print(f"  {error}")
    git = GitRepo(ledger.root, cfg.git)
    print(f"git: {'commits each write' if git.available else 'off (not a repository or disabled)'}")
    print(f"static tokens: {', '.join(t.name for t in cfg.auth.tokens) or 'none'}")
    if cfg.auth.oidc:
        client = OIDCClient(cfg.auth.oidc, f"{cfg.server.public_origin}/oauth/callback")
        try:
            meta = asyncio.run(client.metadata())
            print(f"oidc: {meta['issuer']} ok; redirect URI to register: {client.redirect_uri}")
        except LoginError as exc:
            print(f"oidc: {exc}")
            return 1
    print(f"mcp endpoint: {cfg.server.resource_url or f'http://{cfg.server.host}:{cfg.server.port}{cfg.server.path}'}")
    return 0 if not snap.errors else 1


def cmd_token(args: argparse.Namespace) -> int:
    scopes = normalize_scopes(args.scopes)
    token = "bcm_" + secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    print(f"token (give this to the agent; it is not stored anywhere):\n  {token}\n")
    print("add to the server config:")
    print(f'[[auth.tokens]]\nname = "{args.name}"\nsha256 = "{digest}"\nscopes = {list(scopes)!r}'.replace("'", '"'))
    return 0


def cmd_grants(args: argparse.Namespace) -> int:
    import time

    from .auth.store import Store
    from .config import load_config

    cfg = load_config(args.config)
    if not cfg.auth.state_dir:
        print("no [auth] state_dir: OAuth is not configured", file=sys.stderr)
        return 2
    store = Store(cfg.auth.state_dir / "oauth.sqlite3")
    if args.revoke_all or args.revoke:
        ids = [g["grant_id"] for g in store.list_grants()] if args.revoke_all else args.revoke
        for grant_id in ids:
            print(f"{grant_id}: {'revoked' if store.revoke_grant(grant_id) else 'not found'}")
        return 0
    grants = store.list_grants()
    if not grants:
        print("no active OAuth grants")
    for g in grants:
        day = lambda t: time.strftime("%Y-%m-%d", time.localtime(t)) if t else "?"  # noqa: E731
        print(
            f"{g['grant_id']}  {g.get('client_name') or g['client_id']!r} for {g.get('subject')}  "
            f"[{' '.join(g.get('scopes', []))}]  since {day(g.get('created_at'))}, until {day(g.get('expires_at'))}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bc-mcp", description="Remote MCP server for a Beancount ledger")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the HTTP server")
    serve.add_argument("-c", "--config", help="TOML config file")
    serve.add_argument("--ledger", help="ledger directory (local testing without a config)")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--insecure-no-auth", action="store_true", help="allow serving without auth on loopback")
    serve.add_argument("--log-level", default="info")
    serve.set_defaults(fn=cmd_serve)

    check = sub.add_parser("check", help="validate the config, ledger and OIDC provider")
    check.add_argument("-c", "--config", required=True)
    check.set_defaults(fn=cmd_check)

    token = sub.add_parser("token", help="generate a static bearer token and its config entry")
    token.add_argument("--name", required=True, help="who uses it, e.g. claude-code")
    token.add_argument("--scopes", default="read,write", help="read or read,write (default)")
    token.set_defaults(fn=cmd_token)

    grants = sub.add_parser("grants", help="list or revoke OAuth grants (connected apps)")
    grants.add_argument("-c", "--config", required=True)
    grants.add_argument("--revoke", nargs="+", metavar="GRANT_ID", help="revoke these grants")
    grants.add_argument("--revoke-all", action="store_true", help="revoke every grant")
    grants.set_defaults(fn=cmd_grants)

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
