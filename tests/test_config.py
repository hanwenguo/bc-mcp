import time
from pathlib import Path

import pytest

from bc_mcp.auth.store import Store
from bc_mcp.cli import main
from bc_mcp.config import ALL_SCOPES, AuthConfig, ConfigError, load_config, redirect_allowed
from bc_mcp.ratelimit import RateLimit

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.toml"


def example(tmp_path: Path, ledger_dir: Path, **replace: str) -> Path:
    (tmp_path / "secret").write_text("idp-secret\n")
    text = EXAMPLE.read_text()
    text = text.replace("/var/lib/ledger", str(ledger_dir))
    text = text.replace("/etc/bc-mcp/oidc-client-secret", str(tmp_path / "secret"))
    text = text.replace("/var/lib/bc-mcp", str(tmp_path / "state"))
    for old, new in replace.items():
        text = text.replace(old, new)
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_example_config_loads(tmp_path, ledger_dir):
    cfg = load_config(example(tmp_path, ledger_dir))
    assert cfg.ledger.root == ledger_dir.resolve()
    assert cfg.ledger.layout.dated_file == "books/{year}/{month:02d}/{month:02d}.beancount"
    assert cfg.auth.oidc.client_secret == "idp-secret"
    assert cfg.auth.tokens[0].name == "claude-code" and cfg.auth.tokens[0].scopes == ALL_SCOPES
    assert cfg.server.resource_url == "https://ledger.example.com/mcp"
    assert cfg.auth.allowed_redirects == AuthConfig.allowed_redirects


def test_oidc_requires_an_allow_list(tmp_path, ledger_dir):
    path = example(tmp_path, ledger_dir, **{'allowed_users = ["alice"]': "allowed_users = []"})
    with pytest.raises(ConfigError, match="allowed_users or allowed_groups"):
        load_config(path)


def test_bad_token_hash_is_rejected(tmp_path, ledger_dir):
    path = example(tmp_path, ledger_dir, **{"0" * 64: "abc"})
    with pytest.raises(ConfigError, match="64-hex sha256"):
        load_config(path)


def test_bad_redirect_prefix_is_rejected(tmp_path, ledger_dir):
    path = example(tmp_path, ledger_dir, **{'"https://claude.ai/",': '"https://claude.ai",'})
    with pytest.raises(ConfigError, match="URL prefix"):
        load_config(path)


def test_redirect_allowed():
    prefixes = ("https://claude.ai/", "cursor://anysphere.cursor-mcp/")
    assert redirect_allowed("https://claude.ai/api/mcp/auth_callback", prefixes)
    assert redirect_allowed("cursor://anysphere.cursor-mcp/oauth/callback", prefixes)
    assert redirect_allowed("http://127.0.0.1:4242/cb", prefixes)
    for uri in (
        "https://claude.ai.evil.com/",
        "https://claude.ai@evil.com/",
        "https://evil.com/?https://claude.ai/",
        "http://localhost@evil.com/",
        "https://claude.ai/cb#frag",
    ):
        assert not redirect_allowed(uri, prefixes), uri


def test_store_evicts_only_clients_without_live_tokens(tmp_path):
    store = Store(tmp_path / "oauth.sqlite3")
    for i in range(4):
        store.put_client(f"c{i}", {})
    store.put_token("t", "refresh", "g", {"client_id": "c0"}, time.time() + 60)
    assert store.evict_clients(2, created_before=time.time() + 1) == 2
    assert [c for c in ("c0", "c1", "c2", "c3") if store.get_client(c) is not None] == ["c0", "c3"]


def test_rate_limit_is_per_client_and_window():
    limit = RateLimit(app=None, limits={"/register": (2, 60)})
    assert limit.allow("a", "/register", 0) and limit.allow("a", "/register", 1)
    assert not limit.allow("a", "/register", 2)
    assert limit.allow("b", "/register", 2)
    assert limit.allow("a", "/register", 61)


def test_grants_command_lists_and_revokes(tmp_path, ledger_dir, capsys):
    path = example(tmp_path, ledger_dir)
    store = Store(tmp_path / "state" / "oauth.sqlite3")
    grant = {"client_id": "c1", "client_name": "Claude", "subject": "alice", "scopes": list(ALL_SCOPES),
             "created_at": time.time(), "expires_at": time.time() + 86400}  # fmt: skip
    store.put_token("refresh", "refresh", "g1", grant, time.time() + 3600)
    store.put_token("access", "access", "g1", grant, time.time() + 3600)
    assert main(["grants", "-c", str(path)]) == 0
    assert "g1  'Claude' for alice  [ledger:read ledger:write]" in capsys.readouterr().out
    assert main(["grants", "-c", str(path), "--revoke", "g1"]) == 0
    assert store.get_token("access", "access") is None and store.list_grants() == []
