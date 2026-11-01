"""SQLite persistence for OAuth clients, pending logins, codes and tokens.

Secrets (codes and tokens) are stored only as SHA-256 hashes. Used codes and rotated refresh tokens
are kept as markers until they expire, so a replay can be recognised and its grant revoked.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY,
    info TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pending (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS pending_expiry ON pending (expires_at);
CREATE TABLE IF NOT EXISTS codes (
    hash TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    expires_at REAL NOT NULL,
    grant_id TEXT  -- set once the code has been exchanged
);
CREATE TABLE IF NOT EXISTS tokens (
    hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL,  -- access, refresh, or refresh_used (rotated away, kept to detect replay)
    grant_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    data TEXT NOT NULL,
    expires_at REAL
);
CREATE INDEX IF NOT EXISTS tokens_grant ON tokens (grant_id);
CREATE INDEX IF NOT EXISTS tokens_client ON tokens (client_id);
"""


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()
        path.chmod(0o600)

    def _exec(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _change(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            return self._db.execute(sql, args).rowcount

    def prune(self) -> None:
        now = time.time()
        with self._lock:
            self._db.execute("DELETE FROM pending WHERE expires_at < ?", (now,))
            self._db.execute("DELETE FROM codes WHERE expires_at < ?", (now,))
            self._db.execute("DELETE FROM tokens WHERE expires_at IS NOT NULL AND expires_at < ?", (now,))

    # clients

    def put_client(self, client_id: str, info: dict[str, Any]) -> None:
        self._exec(
            "INSERT OR REPLACE INTO clients (client_id, info, created_at) VALUES (?, ?, ?)",
            (client_id, json.dumps(info), time.time()),
        )

    def count_clients(self) -> int:
        return self._exec("SELECT count(*) FROM clients")[0][0]

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        rows = self._exec("SELECT info FROM clients WHERE client_id = ?", (client_id,))
        return json.loads(rows[0][0]) if rows else None

    def evict_clients(self, keep: int, created_before: float) -> int:
        """Drop the oldest clients that hold no live token, until at most `keep` remain."""
        self.prune()
        excess = self.count_clients() - keep
        if excess <= 0:
            return 0
        return self._change(
            """DELETE FROM clients WHERE client_id IN (
                 SELECT c.client_id FROM clients c
                 WHERE c.created_at < ? AND NOT EXISTS (
                   SELECT 1 FROM tokens t WHERE t.client_id = c.client_id AND t.kind != 'refresh_used')
                 ORDER BY c.created_at LIMIT ?)""",
            (created_before, excess),
        )

    # pending authorizations (between /authorize and the consent decision)

    def put_pending(self, pending_id: str, data: dict[str, Any], ttl: float) -> None:
        self._exec(
            "INSERT OR REPLACE INTO pending (id, data, expires_at) VALUES (?, ?, ?)",
            (pending_id, json.dumps(data), time.time() + ttl),
        )

    def count_pending(self) -> int:
        self.prune()
        return self._exec("SELECT count(*) FROM pending")[0][0]

    def get_pending(self, pending_id: str) -> dict[str, Any] | None:
        rows = self._exec("SELECT data, expires_at FROM pending WHERE id = ?", (pending_id,))
        if not rows or rows[0][1] < time.time():
            return None
        return json.loads(rows[0][0])

    def take_pending(self, pending_id: str) -> dict[str, Any] | None:
        data = self.get_pending(pending_id)
        self._exec("DELETE FROM pending WHERE id = ?", (pending_id,))
        return data

    # authorization codes

    def put_code(self, code: str, data: dict[str, Any], expires_at: float) -> None:
        self._exec(
            "INSERT INTO codes (hash, data, expires_at) VALUES (?, ?, ?)",
            (digest(code), json.dumps(data), expires_at),
        )

    def get_code(self, code: str) -> tuple[dict[str, Any], str | None] | None:
        """The code's data, and the grant it was exchanged for (None while unused)."""
        rows = self._exec("SELECT data, grant_id FROM codes WHERE hash = ?", (digest(code),))
        return (json.loads(rows[0][0]), rows[0][1]) if rows else None

    def use_code(self, code: str, grant_id: str) -> str | None:
        """Mark the code used by `grant_id`. Returns None on success, else the grant that already used it."""
        with self._lock:
            changed = self._db.execute(
                "UPDATE codes SET grant_id = ? WHERE hash = ? AND grant_id IS NULL", (grant_id, digest(code))
            ).rowcount
            if changed:
                return None
            rows = self._db.execute("SELECT grant_id FROM codes WHERE hash = ?", (digest(code),)).fetchall()
            return rows[0][0] if rows else ""

    # tokens

    def put_token(self, token: str, kind: str, grant_id: str, data: dict[str, Any], expires_at: float | None):
        self._exec(
            "INSERT INTO tokens (hash, kind, grant_id, client_id, data, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (digest(token), kind, grant_id, data["client_id"], json.dumps(data), expires_at),
        )

    def get_token(self, token: str, kind: str) -> tuple[str, dict[str, Any], float | None] | None:
        rows = self._exec(
            "SELECT grant_id, data, expires_at FROM tokens WHERE hash = ? AND kind = ?", (digest(token), kind)
        )
        if not rows:
            return None
        grant_id, data, expires_at = rows[0]
        if expires_at is not None and expires_at < time.time():
            return None
        return grant_id, json.loads(data), expires_at

    def retire_refresh(self, token: str) -> bool:
        """Turn a refresh token into a used marker. False if it was not a live refresh token."""
        return (
            self._change(
                "UPDATE tokens SET kind = 'refresh_used' WHERE hash = ? AND kind = 'refresh'", (digest(token),)
            )
            > 0
        )

    def rotate_grant(self, grant_id: str) -> None:
        """Drop a grant's live tokens, keeping used-refresh markers."""
        self._exec("DELETE FROM tokens WHERE grant_id = ? AND kind != 'refresh_used'", (grant_id,))

    def revoke_grant(self, grant_id: str) -> int:
        return self._change("DELETE FROM tokens WHERE grant_id = ?", (grant_id,))

    def list_grants(self) -> list[dict[str, Any]]:
        """Grants that still have a live refresh token."""
        self.prune()
        rows = self._exec("SELECT grant_id, data, expires_at FROM tokens WHERE kind = 'refresh' ORDER BY grant_id")
        return [{"grant_id": g, **json.loads(d), "refresh_expires_at": e} for g, d, e in rows]
