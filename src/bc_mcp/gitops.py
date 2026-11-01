"""Committing ledger writes to the ledger's git repository."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from .config import GitConfig

log = logging.getLogger(__name__)


class GitError(Exception):
    pass


class GitRepo:
    def __init__(self, root: Path, cfg: GitConfig):
        self.root = root
        self.cfg = cfg
        self.available = cfg.enabled and self._detect()

    def _detect(self) -> bool:
        try:
            out = self._git("rev-parse", "--is-inside-work-tree", check=False)
        except (OSError, GitError):
            return False
        return out.returncode == 0 and out.stdout.strip() == "true"

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
        cmd = [
            "git",
            "-c", f"user.name={self.cfg.author_name}",
            "-c", f"user.email={self.cfg.author_email}",
            "-c", "commit.gpgsign=false",
            *args,
        ]  # fmt: skip
        try:
            proc = subprocess.run(cmd, cwd=self.root, env=env, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {args[0]} timed out") from exc
        if check and proc.returncode != 0:
            raise GitError(f"git {args[0]} failed: {(proc.stderr or proc.stdout).strip()}")
        return proc

    def head(self) -> str | None:
        if not self.available:
            return None
        proc = self._git("rev-parse", "--short", "HEAD", check=False)
        return proc.stdout.strip() if proc.returncode == 0 else None

    def dirty(self, paths: list[str] | None = None) -> list[str]:
        """Paths (relative to the ledger root) with uncommitted changes."""
        if not self.available:
            return []
        args = ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *(paths or ["."])]
        out = self._git(*args).stdout
        prefix = self._git("rev-parse", "--show-prefix").stdout.strip()
        result = []
        records = iter(out.split("\0"))
        for record in records:
            if not record:
                continue
            if record[0] in "RC":
                next(records, None)  # -z puts a rename's source path in its own record
            path = record[3:]
            result.append(path[len(prefix) :] if prefix and path.startswith(prefix) else path)
        return result

    def commit(self, paths: list[str], message: str) -> str | None:
        """Commit exactly `paths`, leaving any other staged or unstaged changes alone."""
        if not self.available or not paths:
            return None
        self._git("add", "--", *paths)
        if self._git("diff", "--cached", "--quiet", "--", *paths, check=False).returncode == 0:
            return None
        self._git("commit", "--only", "--quiet", "-m", message, "--", *paths)
        return self.head()
