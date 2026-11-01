"""Loading the ledger and keeping a snapshot that maps loaded entries to their source text."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from beancount import loader

from .config import LedgerConfig
from .source import Span, scan_spans

# Never write .picklecache files into the ledger directory.
loader.initialize(use_cache=False)


@dataclass(frozen=True)
class LedgerError:
    file: str | None
    line: int | None
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"file": self.file, "line": self.line, "message": self.message}

    def __str__(self) -> str:
        where = f"{self.file}:{self.line}: " if self.file else ""
        return f"{where}{self.message}"


@dataclass
class Snapshot:
    entries: list
    errors: list[LedgerError]
    options: dict
    files: list[str]  # ledger-relative files loaded via include, main first
    texts: dict[str, str]
    spans: dict[str, Span] = field(default_factory=dict)
    by_location: dict[tuple[str, int], Span] = field(default_factory=dict)
    stamp: tuple = ()
    rel_names: dict[str, str | None] = field(default_factory=dict)  # loader filename -> relative

    def entry_file(self, entry) -> str | None:
        meta = getattr(entry, "meta", None) or {}
        return self.rel_names.get(meta.get("filename"))

    def span_for(self, entry) -> Span | None:
        meta = getattr(entry, "meta", None) or {}
        return self.by_location.get((self.entry_file(entry), meta.get("lineno")))


class Ledger:
    def __init__(self, cfg: LedgerConfig):
        self.cfg = cfg
        self.root = cfg.root.resolve()
        self._lock = threading.Lock()
        self._snapshot: Snapshot | None = None

    def rel(self, path: str | Path) -> str | None:
        """Ledger-relative posix path, or None for paths outside the ledger root."""
        try:
            return Path(path).resolve().relative_to(self.root).as_posix()
        except (ValueError, OSError):
            return None

    def stamp(self, files: list[str]) -> tuple:
        stamp = []
        for rel in files:
            try:
                st = (self.root / rel).stat()
                stamp.append((rel, st.st_mtime_ns, st.st_size))
            except FileNotFoundError:
                stamp.append((rel, None, None))
        return tuple(stamp)

    def snapshot(self) -> Snapshot:
        with self._lock:
            current = self._snapshot
            if current and self.stamp(current.files) == current.stamp:
                return current
            for _ in range(3):
                snap = self._load()
                if self.stamp(snap.files) == snap.stamp:
                    break
            self._snapshot = snap
            return snap

    def _load(self) -> Snapshot:
        main = self.cfg.main_path
        entries, errors, options = loader.load_file(str(main))
        files = [self.cfg.main]
        for path in options.get("include", []):
            rel = self.rel(path)
            if rel and rel not in files:
                files.append(rel)
        stamp = self.stamp(files)
        texts = {}
        for rel in files:
            try:
                texts[rel] = (self.root / rel).read_text()
            except FileNotFoundError:
                texts[rel] = ""
        snap = Snapshot(entries, [], options, files, texts, stamp=stamp)
        for rel, text in texts.items():
            for span in scan_spans(rel, text):
                snap.spans[span.id] = span
                snap.by_location[(rel, span.start)] = span
        for entry in entries:
            filename = (getattr(entry, "meta", None) or {}).get("filename")
            if filename and filename not in snap.rel_names:
                snap.rel_names[filename] = None if filename.startswith("<") else self.rel(filename)
        snap.errors = [self.convert_error(e) for e in errors]
        return snap

    def convert_error(self, error, root: Path | None = None) -> LedgerError:
        """Turn a beancount error into a LedgerError with a ledger-relative location."""
        source = getattr(error, "source", None) or {}
        filename, line = source.get("filename"), source.get("lineno")
        message = str(getattr(error, "message", error))
        base = (root or self.root).resolve()
        rel = None
        if filename and not str(filename).startswith("<"):
            try:
                rel = Path(filename).resolve().relative_to(base).as_posix()
            except ValueError:
                rel = str(filename)
        # Paths in messages are made ledger-relative, so the server's directory layout isn't revealed.
        prefixes = {str(base), str(root or self.root), str(self.root), str(self.cfg.root)}
        for prefix in sorted(prefixes, key=len, reverse=True):  # /private/var/x before /var/x
            message = message.replace(prefix + "/", "")
        return LedgerError(rel, line, message)
