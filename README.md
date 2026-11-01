# bc-mcp

A remote [MCP](https://modelcontextprotocol.io) server for a [Beancount](https://beancount.github.io)
ledger. Run it next to your ledger, and AI agents (Claude Code, claude.ai and ChatGPT connectors, SDK
agents, ...) can query and edit your books over Streamable HTTP.

- **Engine:** beancount v3 and beanquery, in-process.
- **Safe writes.** Each change is planned in memory and checked by loading the *whole* ledger from a
  temporary copy. Only a change that adds no errors is written, atomically. It is filed in the right
  dated file in date order, aligned like its neighbours, and committed to git.
- **Auth:** static bearer tokens for agents you configure, plus OAuth 2.1 with dynamic client
  registration for connectors. The OAuth login is delegated to an OpenID Connect provider; the
  examples use [Authelia](https://www.authelia.com).
- **Policy:** the ledger's `README.md` is served as the MCP server instructions, so every agent gets
  your bookkeeping rules.

## Tools

| Tool | What it does |
|---|---|
| `ledger_info` | Title, currencies, commodities, files, counts, date range, errors, git state, filing layout, policy |
| `list_accounts` | Open accounts with open date, currencies, posting count, balance |
| `balances` | Unit balances by account (regex), as of a date, optionally rolled up to N components |
| `find_entries` | Search by date range, kind, account regex, text, tag, link, flag, amount range, currency. Returns ids and exact source text |
| `get_entry` | One entry's text, location, comment lines and surrounding context |
| `query` | Read-only BQL (`SELECT`, `BALANCES`, `JOURNAL`, `PRINT`) |
| `validate` | Current ledger errors |
| `read_file` | Read a non-hidden text file inside the ledger directory |
| `add_transaction` | Structured transaction (payee, narration, postings, tags, links, metadata) |
| `add_entries` | Any dated directives in Beancount syntax (balance, pad, note, price, open/close, ...) |
| `replace_entry` | Correct an entry by id. In place if the date is unchanged, moved if the date changes |
| `delete_entry` | Delete an entry by id |

All write tools take `dry_run` to get the diff and validation result without writing. A rejected write
returns the Beancount errors and the proposed diff. Entry ids are content hashes, so a stale id (the
entry changed since it was read) is refused rather than editing the wrong thing.

Where writes go (configurable in `[ledger.layout]`; the defaults expect `books/YYYY/MM/MM.beancount`):

- Transactions, balances, notes and the like go in the dated file, after the last entry dated on or
  before them. A missing month file is created with the main file's modeline, and its `include` is
  added to the year index. A missing year index is created too and included from `main.beancount`.
- `open`, `close` and `commodity` go in `accounts.beancount`, next to the closest related account.
- `option`, `include` and `plugin` lines are refused, so agents can't load code into the ledger, and
  so are `custom "fava-..."` directives (Fava extensions and importer configs run Python).
- `document` paths must point at an existing, non-hidden file inside the ledger directory.

Git: each write is one commit (`ledger: add 2026-10-31 * "Corner Cafe" "Brunch"`, with the tool and
the client identity in the body). If a file being written already has uncommitted changes (a hand
edit, say), those are committed first in a separate snapshot commit, so every bc-mcp commit can be
reverted on its own. Nothing is pushed.

## Try it

```bash
uv sync
```

The tests use a small made-up ledger in [tests/fixtures/ledger](tests/fixtures/ledger). To try the
server, serve a git-tracked copy of it (writes are real):

```bash
cp -r tests/fixtures/ledger /tmp/demo-ledger && git -C /tmp/demo-ledger init -q && git -C /tmp/demo-ledger add -A && git -C /tmp/demo-ledger commit -qm init
```

```bash
uv run bc-mcp serve --ledger /tmp/demo-ledger --insecure-no-auth
```

The server listens on `http://127.0.0.1:8765/mcp`. `--insecure-no-auth` only binds to loopback.

## Configuration

Everything is in one TOML file; [config.example.toml](config.example.toml) documents every key. The
main parts:

- `[ledger]`: the ledger directory (a git repository) and its main file.
- `[server] public_url`: the public origin behind your reverse proxy. Required for OAuth.
- `[[auth.tokens]]`: static tokens. `bc-mcp token --name NAME --scopes read,write` prints a new token
  and its config entry; only the SHA-256 hash goes in the config.
- `[auth.oidc]`: the OpenID Connect provider used for the OAuth login, and `allowed_users` /
  `allowed_groups`.

`bc-mcp check -c CONFIG` validates the config, loads the ledger and fetches the provider's discovery
document. `bc-mcp grants -c CONFIG` lists connected OAuth apps; `--revoke GRANT_ID` (or
`--revoke-all`) disconnects them.

## Deploy

bc-mcp listens on loopback; put a reverse proxy with TLS in front of it. Don't add an auth proxy
(such as Authelia's `forward_auth`) to its site: MCP clients can't complete a browser login there, and
bc-mcp does its own auth.

Run it as a dedicated user that owns the ledger. Atomic writes replace files, so whoever writes owns
them.

### OpenID Connect provider

Register bc-mcp as a confidential client with redirect URI `https://<public_url>/oauth/callback`,
PKCE (S256) and the scopes `openid profile email groups`. For Authelia, see
[deploy/authelia-client.yml](deploy/authelia-client.yml). Give bc-mcp the client secret as a file
(`client_secret_file`) or in `BC_MCP_OIDC_CLIENT_SECRET`.

### NixOS

The flake provides the package (built from `uv.lock` with uv2nix) and a NixOS module. Add the input:

```nix
bc-mcp = {
  url = "github:hanwenguo/bc-mcp";
  inputs.nixpkgs.follows = "nixpkgs";
};
```

Then:

```nix
{ bc-mcp, ... }:
{
  imports = [ bc-mcp.nixosModules.default ];

  services.bc-mcp = {
    enable = true;
    ledgerDir = "/var/lib/ledger"; # a git repository owned by the service user
    settings = {
      server.public_url = "https://ledger.example.com";
      auth.oidc = {
        issuer = "https://auth.example.com";
        client_id = "bc-mcp";
        client_secret_file = "/run/secrets/bc-mcp-oidc-client-secret"; # readable by bc-mcp
        allowed_users = [ "alice" ];
      };
    };
  };

  services.caddy.virtualHosts."ledger.example.com".extraConfig = ''
    reverse_proxy 127.0.0.1:8765
  '';
}
```

`settings` is the TOML config, written to the Nix store, so give secrets as file paths. The module
runs bc-mcp as the `bc-mcp` user in a systemd sandbox, keeps OAuth state in `/var/lib/bc-mcp`, and
puts the config at `/etc/bc-mcp/config.toml` for `sudo -u bc-mcp bc-mcp grants -c
/etc/bc-mcp/config.toml`. Set `group` to a group shared with read-only readers such as Fava.

### Other Linux with systemd

1. Create the service user, and put the ledger repository in `/var/lib/ledger`, owned by it:

   ```bash
   sudo useradd --system --user-group --home-dir /var/lib/bc-mcp --shell /usr/sbin/nologin bc-mcp
   ```

   ```bash
   sudo chown -R bc-mcp:bc-mcp /var/lib/ledger
   ```

2. Install the code and its environment:

   ```bash
   sudo git clone https://github.com/hanwenguo/bc-mcp /opt/bc-mcp && cd /opt/bc-mcp && sudo UV_PYTHON_INSTALL_DIR=/opt/bc-mcp/python uv sync --frozen --no-dev
   ```

   `UV_PYTHON_INSTALL_DIR` keeps any Python that uv downloads under `/opt`. Otherwise it would land
   in `/root`, which the service user can't read.

3. Write `/etc/bc-mcp/config.toml` from [config.example.toml](config.example.toml), mode `0640`, group
   `bc-mcp`. Put the OIDC client secret in `/etc/bc-mcp/oidc-client-secret`, also `0640` and group
   `bc-mcp`.

4. Check the setup, then install and start the unit:

   ```bash
   sudo -u bc-mcp /opt/bc-mcp/.venv/bin/bc-mcp check -c /etc/bc-mcp/config.toml
   ```

   ```bash
   sudo cp /opt/bc-mcp/deploy/bc-mcp.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now bc-mcp
   ```

5. Put a reverse proxy with TLS in front; [deploy/Caddyfile](deploy/Caddyfile) is a Caddy example.

The unit ([deploy/bc-mcp.service](deploy/bc-mcp.service)) makes the filesystem read-only except the
ledger and `/var/lib/bc-mcp`, and hides `/home`. If the ledger lives elsewhere, change
`ReadWritePaths` to match. It needs systemd 247 or newer.

## Connect agents

Claude Code, with a static token:

```bash
claude mcp add --transport http ledger https://ledger.example.com/mcp --header "Authorization: Bearer bcm_..."
```

Claude Code with OAuth: leave out `--header`, then run `/mcp` and choose Authenticate.

claude.ai: Settings → Connectors → Add custom connector, with URL `https://ledger.example.com/mcp`.
You sign in with your OpenID Connect provider, then bc-mcp shows a consent page. It names the app and
the host it will redirect to, and lets you grant read-only or read+write.

ChatGPT: turn on developer mode (Settings → Apps → Advanced settings), then create an app with the
MCP server URL `https://ledger.example.com/mcp` and OAuth authentication. ChatGPT registers itself
(dynamic client registration), and the login and consent steps are the same as for claude.ai. Its
callbacks (`https://chatgpt.com/connector/oauth/...` and the older
`https://chatgpt.com/connector_platform_oauth_redirect`) are allowed by default.

Other agents and scripts: a Streamable HTTP MCP server at `https://ledger.example.com/mcp` with the
header `Authorization: Bearer <token>`.

## OAuth details

- bc-mcp is its own authorization server (`/.well-known/oauth-authorization-server`, `/register`,
  `/authorize`, `/token`, `/revoke`), and publishes protected-resource metadata for `/mcp`.
- `/authorize` sends the browser to the OpenID Connect provider (code flow with PKCE and a nonce).
  The id_token is checked against the provider's JWKS, then the user is checked against
  `allowed_users` / `allowed_groups`. Then comes bc-mcp's own consent page, which guards against a
  malicious dynamically registered client riding an existing login session.
- Scopes: `ledger:read` (required for any access) and `ledger:write`. Access tokens last 1 hour.
  Refresh tokens last 30 days, rotate on each use, and are single-use. Codes and tokens are stored
  only as SHA-256 hashes in `state_dir/oauth.sqlite3`.
- Static tokens are accepted alongside OAuth tokens and never expire. Remove one from the config and
  restart to revoke it.

## Security model

bc-mcp assumes anyone on the internet can reach it, and that an agent holding a valid token may be
confused or prompt-injected (say, by a malicious bank statement it was asked to import).

Getting a token:

- OAuth redirect URIs must be loopback or start with a prefix in `[auth] allowed_redirects`
  (claude.ai, claude.com and ChatGPT by default). A stranger can register a client and send you an
  `/authorize` link, but the code can only go back to an allowed app, so approving it by mistake
  gives them nothing.
- Login goes through the OpenID Connect provider (PKCE, nonce, id_token checked against its JWKS,
  `azp` checked), then `allowed_users` / `allowed_groups`, then bc-mcp's own consent page. That page
  shows the app's name and where it will redirect, and grants read-only unless you tick "Also allow
  changes". It can't be framed, and Approve only activates after a second, against double-click
  tricks.
- Refresh tokens rotate. If an old refresh token or a used authorization code is presented again,
  the whole grant is revoked, since that means a copy leaked. Grants end after `grant_ttl` (90 days)
  however often they are refreshed.
- `/register`, `/authorize`, `/token`, `/revoke` and the login pages are rate-limited per client IP.
  Pending logins, registered clients, and request bodies are all capped. Clients that never got a
  token are evicted to make room, so junk registrations can't lock you out.

Using a token:

- Every tool checks its scope; the write tools need `ledger:write`.
- `read_file` can't leave the ledger directory or read hidden files (`.git`, dotfiles), including
  through symlinks.
- BQL is limited to read statements. beanquery's `CREATE TABLE ... USING` can open arbitrary files,
  so it is refused.
- Structured fields (`add_transaction`) are validated so none of them can break out of their line.
  Text directives are refused if they contain `option`/`include`/`plugin`/`pushtag` lines.
- Every write is validated against the whole ledger, written atomically, and committed. It is
  refused if any ledger file changed on disk while it was being prepared. A bad write is one
  `git revert` away. Nothing is pushed, so back the ledger repository up yourself.
- Error messages show ledger-relative paths only.

Known limits:

- A token with `ledger:write` can add, change or delete any entry, as long as the ledger stays
  valid. Git history is the undo. Give agents that only need to read a read-only token.
- Anything running as the service user (or root) can read `/var/lib/bc-mcp/oauth.sqlite3`. It holds
  only hashes of codes and tokens, but also the registered clients. Don't share the service user
  with other software.
- Public OAuth clients can't use `/revoke`: MCP SDK 2.3 requires a `client_secret` field there. Use
  `bc-mcp grants --revoke` instead.

## Development

```bash
uv run pytest
```

```bash
uv run ruff check && uv run ruff format --check
```

`nix build .#dev` builds an environment with the test dependencies, to run the tests on the
Nix-built Python.

```
src/bc_mcp/
  config.py     TOML config
  ledger.py     loading + snapshot that maps entries to their source text
  source.py     text-level spans, ids, alignment, date-ordered insertion, includes
  writer.py     plan -> validate in a temp copy -> atomic write -> git commit
  queries.py    read operations
  render.py     structured transaction -> Beancount text
  tools.py      MCP tools, policy resource and instructions
  auth/         OAuth provider, OpenID Connect client, SQLite store
  ratelimit.py  per-IP limits on the public OAuth endpoints
  app.py, cli.py
tests/          run against a git-tracked copy of tests/fixtures/ledger (made-up data)
deploy/         systemd unit, Caddyfile, Authelia client example
nix/module.nix  NixOS module (flake.nix has the package)
```
