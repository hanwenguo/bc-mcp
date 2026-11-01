"""Validated, atomic writes to the ledger.

Every write is planned in memory, checked by loading the whole ledger from a temporary
copy, and only then written to disk (atomically, file by file) and committed to git.
"""

from __future__ import annotations

import datetime
import difflib
import logging
import os
import posixpath
import re
import shutil
import tempfile
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from beancount import loader
from beancount.core import data
from beancount.parser import parser

from .gitops import GitError, GitRepo
from .ledger import Ledger, LedgerError, Snapshot
from .source import (
    DATED_LINE,
    MODELINE,
    Span,
    add_include,
    align_postings,
    amount_column,
    block_end,
    delete_lines,
    directive_kind,
    insert_by_date,
    insert_declaration,
    join_lines,
    leading_comment_start,
    scan_spans,
    split_lines,
)

log = logging.getLogger(__name__)

# The path of a document directive, as written (beancount resolves it against the file's directory).
DOCUMENT_PATH = re.compile(r'\d{4}-\d{2}-\d{2}\s+document\s+\S+\s+"((?:[^"\\]|\\.)*)"')

DECLARATIONS = {"open", "close", "commodity"}


class WriteError(Exception):
    """A write was refused; the message is meant for the agent."""


class ValidationFailed(WriteError):
    def __init__(self, errors: list[LedgerError], diff: str):
        self.errors = errors
        self.diff = diff
        lines = "\n".join(f"- {e}" for e in errors[:20])
        more = f"\n- ... and {len(errors) - 20} more" if len(errors) > 20 else ""
        super().__init__(
            f"Rejected: the change would make the ledger invalid. Nothing was written.\n{lines}{more}"
            f"\n\nProposed diff:\n{diff}"
        )


@dataclass
class Block:
    date: datetime.date
    kind: str
    account: str | None
    comments: list[str]
    lines: list[str]

    @property
    def header(self) -> str:
        return " ".join(self.lines[0].split())


def parse_blocks(text: str) -> list[Block]:
    """Split Beancount text into directives (with their leading comments) and syntax-check it."""
    lines = [line.rstrip() for line in text.strip("\n").split("\n")]
    blocks: list[Block] = []
    comments: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif line.startswith(";"):
            comments.append(line)
            i += 1
        elif m := DATED_LINE.match(line):
            j = block_end(lines, i)
            kind = directive_kind(m.group(2))
            account = m.group(3) if kind in ("open", "close") else None
            blocks.append(Block(datetime.date.fromisoformat(m.group(1)), kind, account, comments, lines[i:j]))
            comments = []
            i = j
        else:
            raise WriteError(
                f"line {i + 1}: expected a dated directive or a ';' comment, got {line!r}. "
                "option, include, plugin and pushtag lines are not accepted."
            )
    if comments:
        raise WriteError("comments must come before the directive they describe")
    if not blocks:
        raise WriteError("no directives found in the text")
    entries, errors, _ = parser.parse_string("\n".join(lines))
    if errors:
        details = "\n".join(f"- line {e.source.get('lineno')}: {e.message}" for e in errors)
        raise WriteError(f"Beancount syntax errors:\n{details}")
    if len(entries) != len(blocks):
        raise WriteError(f"parsed {len(entries)} directives but found {len(blocks)} directive blocks")
    for entry in entries:
        # Fava reads these: "fava-extension" imports a Python module, "fava-option" can name an importer
        # script it runs. Configure Fava by hand, not through an agent.
        if isinstance(entry, data.Custom) and entry.type.startswith("fava-"):
            raise WriteError(f'custom "{entry.type}" directives configure Fava and are not accepted')
    return blocks


class Plan:
    """In-memory edits to ledger files, built against one snapshot."""

    def __init__(self, ledger: Ledger, snap: Snapshot):
        self.ledger = ledger
        self.cfg = ledger.cfg
        self.snap = snap
        self.work: dict[str, list[str]] = {}
        self.original: dict[str, str | None] = {}
        self.included: set[str] = set(snap.files)
        self.placed: list[tuple[str, Block]] = []
        self.removed: list[Span] = []

    # -- file access

    def lines(self, rel: str) -> list[str] | None:
        if rel not in self.work:
            if rel in self.snap.texts:
                text = self.snap.texts[rel]
            else:
                path = self.ledger.root / rel
                text = path.read_text() if path.is_file() else None
            self.original[rel] = text
            if text is None:
                return None
            self.work[rel] = split_lines(text)
        return self.work[rel]

    def _modeline(self) -> list[str]:
        main = split_lines(self.snap.texts.get(self.cfg.main, ""))
        return [main[0]] if main and MODELINE.match(main[0]) else []

    def _new_file(self, rel: str) -> list[str]:
        safe = self.ledger.rel(self.ledger.root / rel)
        if safe != rel:
            raise WriteError(f"refusing to create {rel!r} outside the ledger directory")
        self.work[rel] = self._modeline()
        return self.work[rel]

    def _ensure_included(self, rel: str, day: datetime.date) -> None:
        if rel in self.included:
            return
        layout = self.cfg.layout
        parent = self.cfg.main
        if layout.year_index and rel == layout.dated_file.format(year=day.year, month=day.month, day=day.day):
            parent = layout.year_index.format(year=day.year, month=day.month, day=day.day)
        lines = self.lines(parent)
        if lines is None:
            lines = self._new_file(parent)
        include = posixpath.relpath(rel, posixpath.dirname(parent) or ".")
        self.work[parent] = add_include(lines, include)
        self.included.add(rel)
        self._ensure_included(parent, day)

    # -- edits

    def target_for(self, block: Block) -> str:
        layout = self.cfg.layout
        if block.kind in DECLARATIONS and layout.accounts_file:
            return layout.accounts_file
        d = block.date
        return layout.dated_file.format(year=d.year, month=d.month, day=d.day)

    def _render(self, block: Block, rel: str) -> list[str]:
        text = self.original.get(rel) or join_lines(self.work.get(rel, []))
        column = amount_column(text, self.cfg.layout.amount_column)
        return block.comments + align_postings("\n".join(block.lines), column).split("\n")

    def place(self, block: Block, target: str | None = None) -> None:
        rel = target or self.target_for(block)
        lines = self.lines(rel)
        if lines is None:
            lines = self._new_file(rel)
        rendered = self._render(block, rel)
        if block.kind in DECLARATIONS and rel == self.cfg.layout.accounts_file:
            self.work[rel], _ = insert_declaration(lines, rel, block.kind, block.account, rendered)
        else:
            self.work[rel], _ = insert_by_date(lines, rel, block.date, rendered)
        self.placed.append((rel, block))
        self._ensure_included(rel, block.date)

    def _locate(self, span: Span, with_comments: bool) -> tuple[list[str], int]:
        lines = self.lines(span.file)
        if lines is None or "\n".join(lines[span.start - 1 : span.end]) != span.text:
            raise WriteError(f"entry {span.id} changed on disk; look it up again")
        start = leading_comment_start(lines, span.start - 1) if with_comments else span.start - 1
        return lines, start

    def remove(self, span: Span, with_comments: bool = False) -> None:
        lines, start = self._locate(span, with_comments)
        self.work[span.file] = delete_lines(lines, start, span.end)
        self.removed.append(span)

    def replace_in_place(self, span: Span, blocks: list[Block], with_comments: bool = False) -> None:
        lines, start = self._locate(span, with_comments)
        new: list[str] = []
        for block in blocks:
            new += ([""] if new else []) + self._render(block, span.file)
            self.placed.append((span.file, block))
        self.work[span.file] = lines[:start] + new + lines[span.end :]
        self.removed.append(span)

    # -- results

    def changes(self) -> dict[str, tuple[str | None, str]]:
        out = {}
        for rel, lines in self.work.items():
            new = join_lines(lines)
            if new != self.original.get(rel):
                out[rel] = (self.original.get(rel), new)
        return out

    def new_entries(self, changes: dict[str, tuple[str | None, str]]) -> list[dict[str, Any]]:
        result = []
        claimed: set[str] = set()
        for rel, block in self.placed:
            old_ids = {s.id for s in scan_spans(rel, changes.get(rel, (None, ""))[0] or "")}
            text_lines = self._render(block, rel)[len(block.comments) :]
            text = "\n".join(text_lines)
            new_text = changes[rel][1] if rel in changes else self.snap.texts.get(rel, "")
            for span in scan_spans(rel, new_text):
                if span.text == text and span.id not in claimed and span.id not in old_ids:
                    claimed.add(span.id)
                    result.append({"id": span.id, "file": rel, "line": span.start, "text": span.text})
                    break
        return result


def unified_diff(changes: dict[str, tuple[str | None, str]]) -> str:
    parts = []
    for rel, (old, new) in sorted(changes.items()):
        parts.extend(
            difflib.unified_diff(
                (old or "").splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile="/dev/null" if old is None else f"a/{rel}",
                tofile=f"b/{rel}",
                n=2,
            )
        )
    return "".join(parts)


class Writer:
    def __init__(self, ledger: Ledger, git: GitRepo | None):
        self.ledger = ledger
        self.git = git
        self._lock = threading.Lock()

    # -- public operations (each runs under the write lock)

    def add(self, blocks: list[Block], *, tool: str, actor: str | None, dry_run: bool) -> dict[str, Any]:
        with self._lock:
            plan = Plan(self.ledger, self.ledger.snapshot())
            for block in blocks:
                plan.place(block)
            verb = "add"
            return self._finish(plan, blocks, verb, tool=tool, actor=actor, dry_run=dry_run)

    def replace(
        self, entry_id: str, blocks: list[Block], *, with_comments: bool, tool: str, actor: str | None, dry_run: bool
    ) -> dict[str, Any]:
        with self._lock:
            snap = self.ledger.snapshot()
            span = self._span(snap, entry_id)
            plan = Plan(self.ledger, snap)
            if all(b.date == span.date for b in blocks) or (
                span.file == self.ledger.cfg.layout.accounts_file and all(b.kind in DECLARATIONS for b in blocks)
            ):
                plan.replace_in_place(span, blocks, with_comments)
            else:
                plan.remove(span, with_comments)
                for block in blocks:
                    plan.place(block)
            return self._finish(plan, blocks, "edit", tool=tool, actor=actor, dry_run=dry_run)

    def delete(
        self, entry_id: str, *, with_comments: bool, tool: str, actor: str | None, dry_run: bool
    ) -> dict[str, Any]:
        with self._lock:
            snap = self.ledger.snapshot()
            span = self._span(snap, entry_id)
            plan = Plan(self.ledger, snap)
            plan.remove(span, with_comments)
            header = " ".join(span.text.split("\n")[0].split())
            return self._finish(plan, [], f"delete {header}", tool=tool, actor=actor, dry_run=dry_run)

    # -- internals

    @staticmethod
    def _span(snap: Snapshot, entry_id: str) -> Span:
        span = snap.spans.get(entry_id)
        if span is None:
            raise WriteError(
                f"no entry with id {entry_id!r}. IDs change when an entry's text changes; "
                "look the entry up again with find_entries."
            )
        return span

    def _document_paths(self, plan: Plan) -> list[str]:
        """Paths (as beancount will resolve them, ledger-relative) of new document directives.

        Beancount checks that each document exists, so an unchecked path would tell the caller whether
        any file on the server exists, and tools like Fava serve the files documents point to. Only
        existing, non-hidden files inside the ledger directory are accepted."""
        paths = []
        for rel, block in plan.placed:
            if block.kind != "document":
                continue
            m = DOCUMENT_PATH.match(block.lines[0])
            raw = re.sub(r"\\(.)", r"\1", m.group(1)) if m else ""
            target = posixpath.normpath(posixpath.join(posixpath.dirname(rel), raw)) if raw else ""
            real = None
            if raw and not raw.startswith("/") and target != ".." and not target.startswith("../"):
                real = self.ledger.rel(self.ledger.root / target)
            if real is None or any(part.startswith(".") for part in PurePosixPath(real).parts):
                raise WriteError(
                    f"document path {raw!r} must be relative to {rel} and point inside the ledger directory"
                )
            if not (self.ledger.root / real).is_file():
                raise WriteError(f"document {real} does not exist in the ledger directory")
            paths.append(target)
        return paths

    def _inside(self, filename: str) -> str | None:
        """`filename` relative to the ledger root, without resolving symlinks, or None if outside."""
        for base in (self.ledger.root, self.ledger.cfg.root):
            try:
                return PurePosixPath(posixpath.normpath(filename)).relative_to(base.as_posix()).as_posix()
            except ValueError:
                continue
        return None

    def validate(
        self, snap: Snapshot, changes: dict[str, tuple[str | None, str]], documents: list[str] = ()
    ) -> list[LedgerError]:
        """Errors the change would introduce, found by loading the whole ledger from a temp copy."""
        with tempfile.TemporaryDirectory(prefix="bc-mcp-") as tmp:
            root = Path(tmp)
            for rel in set(snap.files) | set(changes):
                text = changes[rel][1] if rel in changes else snap.texts.get(rel, "")
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            # Beancount checks that document files exist. Give it stand-ins for the ones that exist in
            # the real ledger: those already in it, and the new ones (checked by _document_paths).
            existing = [self._inside(e.filename) for e in snap.entries if isinstance(e, data.Document)]
            existing = [rel for rel in existing if rel and (self.ledger.root / rel).exists()]
            for rel in [*existing, *documents]:
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).touch()
            _, errors, _ = loader.load_file(str(root / self.ledger.cfg.main))
            converted = [self.ledger.convert_error(e, root=root) for e in errors]
        baseline = Counter((e.file, e.message) for e in snap.errors)
        new = []
        for error in converted:
            key = (error.file, error.message)
            if baseline[key]:
                baseline[key] -= 1
            else:
                new.append(error)
        return new

    def _finish(
        self, plan: Plan, blocks: list[Block], verb: str, *, tool: str, actor: str | None, dry_run: bool
    ) -> dict[str, Any]:
        changes = plan.changes()
        if not changes:
            raise WriteError("the change leaves the ledger exactly as it is")
        diff = unified_diff(changes)
        documents = self._document_paths(plan)
        errors = self.validate(plan.snap, changes, documents=documents)
        if errors:
            raise ValidationFailed(errors, diff)
        result: dict[str, Any] = {
            "status": "dry_run" if dry_run else "written",
            "entries": plan.new_entries(changes),
            "removed": [{"id": s.id, "file": s.file, "line": s.start} for s in plan.removed],
            "files": sorted(changes),
            "diff": diff,
            "validation": "ok: the whole ledger loads without new errors",
            "commit": None,
            "warnings": [],
        }
        if plan.snap.errors:
            result["warnings"].append(f"the ledger already had {len(plan.snap.errors)} error(s) before this change")
        if dry_run:
            return result

        if self.ledger.stamp(plan.snap.files) != plan.snap.stamp:  # validation used these files' contents
            raise WriteError("ledger files changed on disk while this write was being prepared; retry")
        for rel, (old, _) in changes.items():
            path = self.ledger.root / rel
            current = path.read_text() if path.is_file() else None
            if current != old:
                raise WriteError(f"{rel} changed on disk while this write was being prepared; retry")

        if self.git and self.git.available:
            try:
                dirty = self.git.dirty([rel for rel, (old, _) in changes.items() if old is not None])
                if dirty:
                    self.git.commit(dirty, "ledger: snapshot uncommitted changes before bc-mcp write")
                    result["warnings"].append(f"committed pre-existing uncommitted changes first: {', '.join(dirty)}")
            except GitError as exc:
                result["warnings"].append(f"git snapshot failed: {exc}")

        for rel, (_, new) in changes.items():
            _atomic_write(self.ledger.root / rel, new, mode_from=self.ledger.cfg.main_path)

        if self.git and self.git.available:
            if blocks:
                subject = (
                    f"ledger: {verb} {blocks[0].header}"
                    if len(blocks) == 1
                    else (f"ledger: {verb} {len(blocks)} entries ({blocks[0].header} ...)")
                )
            else:
                subject = f"ledger: {verb}"
            if len(subject) > 100:
                subject = subject[:97] + "..."
            body = f"Tool: {tool}" + (f"\nClient: {actor}" if actor else "")
            try:
                result["commit"] = self.git.commit(sorted(changes), f"{subject}\n\n{body}")
            except GitError as exc:
                result["warnings"].append(f"written, but git commit failed: {exc}")
        return result


def _atomic_write(path: Path, text: str, mode_from: Path) -> None:
    """Replace `path` atomically; new files copy their permissions from `mode_from`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        shutil.copymode(path if path.exists() else mode_from, tmp)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
