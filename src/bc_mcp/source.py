"""Text-level view of Beancount files: directive spans, IDs, alignment and insertion.

The loader tells us what an entry means; this module tells us where its text lives,
so writes can edit files surgically and leave everything else byte-for-byte intact.
"""

from __future__ import annotations

import datetime
import hashlib
import re
from collections import Counter
from dataclasses import dataclass

DATED_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ \t]+(\S+)(?:[ \t]+(\S+))?")
INCLUDE_LINE = re.compile(r'^include[ \t]+"([^"]+)"')
KEYWORDS = {
    "open", "close", "commodity", "balance", "pad", "note",
    "document", "price", "event", "query", "custom",
}  # fmt: skip
# Posting line: indent, optional flag, account, gap, number expression, currency, rest.
POSTING_LINE = re.compile(
    r"^(?P<lead>[ \t]+(?:[*!&#?%PSTCURM][ \t]+)?[A-Z][^\s:]*(?::[^\s:]+)+)"
    r"(?P<gap>[ \t]+)"
    r"(?P<number>[-+(.0-9][-+*/(). 0-9,]*?)"
    r"(?P<rest>[ \t]+[A-Z][A-Z0-9'._-]*.*)$"
)
MODELINE = re.compile(r"^;+\s*-\*-.*-\*-")


@dataclass(frozen=True)
class Span:
    """One directive's source text: its header line plus indented continuation lines."""

    id: str
    file: str  # ledger-relative, posix separators
    start: int  # 1-based first line
    end: int  # 1-based last line, inclusive
    date: datetime.date
    kind: str  # "transaction" or a directive keyword such as "balance"
    text: str

    @property
    def account(self) -> str | None:
        m = DATED_LINE.match(self.text)
        return m.group(3) if m and self.kind in ("open", "close", "balance", "pad", "note", "document") else None


def split_lines(text: str) -> list[str]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def join_lines(lines: list[str]) -> str:
    return "\n".join(lines) + "\n" if lines else ""


def directive_kind(word: str) -> str:
    return word if word in KEYWORDS else "transaction"


def block_end(lines: list[str], start: int) -> int:
    """Index one past the last line of the directive that starts at `start` (0-based)."""
    j = start + 1
    while j < len(lines) and lines[j].strip() and lines[j][0] in " \t":
        j += 1
    return j


def scan_spans(file: str, text: str) -> list[Span]:
    lines = split_lines(text)
    spans: list[Span] = []
    seen: Counter[str] = Counter()
    i = 0
    while i < len(lines):
        m = DATED_LINE.match(lines[i])
        if not m:
            i += 1
            continue
        j = block_end(lines, i)
        try:
            day = datetime.date.fromisoformat(m.group(1))
        except ValueError:
            i = j
            continue
        block = "\n".join(lines[i:j])
        key = f"{file}\n{block}"
        # Identical entries in one file get distinct IDs by occurrence order.
        occurrence = seen[key]
        seen[key] += 1
        digest = hashlib.sha256(f"{key}\n#{occurrence}".encode()).hexdigest()[:12]
        spans.append(Span(digest, file, i + 1, j, day, directive_kind(m.group(2)), block))
        i = j
    return spans


def leading_comment_start(lines: list[str], start: int) -> int:
    """0-based index of the first column-0 comment line directly above line `start` (0-based)."""
    k = start
    while k > 0 and lines[k - 1].startswith(";") and not (k - 1 == 0 and MODELINE.match(lines[0])):
        k -= 1
    return k


# --- alignment ---------------------------------------------------------------------------


def amount_column(text: str, default: int) -> int:
    """The most common column at which posting numbers end in `text`."""
    ends: Counter[int] = Counter()
    for line in split_lines(text):
        m = POSTING_LINE.match(line)
        if m and re.fullmatch(r"-?[0-9][0-9,]*(\.[0-9]+)?", m.group("number").strip()):
            ends[len(m.group("lead")) + len(m.group("gap")) + len(m.group("number").rstrip())] += 1
    return ends.most_common(1)[0][0] if ends else default


def align_postings(block: str, column: int) -> str:
    """Right-align posting numbers to end at `column`, changing only whitespace."""
    out = []
    for line in block.split("\n"):
        m = POSTING_LINE.match(line)
        if m:
            lead, number = m.group("lead"), m.group("number").strip()
            gap = max(2, column - len(lead) - len(number))
            line = f"{lead}{' ' * gap}{number}{m.group('rest')}"
        out.append(line.rstrip())
    return "\n".join(out)


# --- insertion ---------------------------------------------------------------------------


def insert_lines(lines: list[str], index: int, block: list[str], tight: bool = False) -> tuple[list[str], int]:
    """Insert `block` before `lines[index]`, keeping one blank line around it unless `tight`.

    Returns the new lines and the 1-based line number where the block starts.
    """
    before, after = lines[:index], lines[index:]
    sep = [] if tight or not before or not before[-1].strip() else [""]
    trailer = [] if tight or not after or not after[0].strip() else [""]
    start = len(before) + len(sep) + 1
    return before + sep + block + trailer + after, start


def insert_by_date(lines: list[str], file: str, day: datetime.date, block: list[str]) -> tuple[list[str], int]:
    """Insert after the last directive (in file order) dated on or before `day`."""
    spans = scan_spans(file, join_lines(lines))
    earlier = [s for s in spans if s.date <= day]
    if earlier:
        return insert_lines(lines, earlier[-1].end, block)
    if spans:
        return insert_lines(lines, leading_comment_start(lines, spans[0].start - 1), block)
    return insert_lines(lines, len(lines), block)


def _shared_components(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a.split(":"), b.split(":")):
        if x != y:
            break
        n += 1
    return n


def insert_declaration(lines: list[str], file: str, kind: str, account: str | None, block: list[str]):
    """Place open/close/commodity next to its closest relative in an accounts file."""
    spans = scan_spans(file, join_lines(lines))
    anchor: Span | None = None
    if kind == "open" and account:
        best = 0
        for s in spans:
            if s.kind == "open" and s.account:
                score = _shared_components(s.account, account)
                if score >= best and score > 0:
                    best, anchor = score, s
    elif kind == "close" and account:
        anchor = next((s for s in spans if s.kind == "open" and s.account == account), None)
    elif kind == "commodity":
        anchor = next((s for s in reversed(spans) if s.kind == "commodity"), None)
    if anchor is None:
        return insert_lines(lines, len(lines), block)
    return insert_lines(lines, anchor.end, block, tight=True)


def add_include(lines: list[str], include_path: str) -> list[str]:
    """Add `include "include_path"` among the existing includes, in sorted position."""
    includes = [(i, m.group(1)) for i, line in enumerate(lines) if (m := INCLUDE_LINE.match(line))]
    if any(path == include_path for _, path in includes):
        return lines
    line = f'include "{include_path}"'
    lower = [i for i, path in includes if path < include_path]
    if lower:
        index = lower[-1] + 1
    elif includes:
        index = includes[0][0]
    else:
        return insert_lines(lines, len(lines), [line])[0]
    return lines[:index] + [line] + lines[index:]


def delete_lines(lines: list[str], start: int, end: int) -> list[str]:
    """Remove 0-based [start, end) and collapse the blank lines left behind."""
    out = lines[:start] + lines[end:]
    i = start
    if i < len(out) and not out[i].strip() and (i == 0 or not out[i - 1].strip()):
        del out[i]
    elif i == len(out) and i > 0 and not out[i - 1].strip():
        del out[i - 1]
    return out
