"""MCP tools, resources and instructions for one ledger."""

from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Callable, TypeVar

import anyio
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import queries
from .config import READ_SCOPE, WRITE_SCOPE, Config
from .gitops import GitRepo
from .ledger import Ledger
from .queries import QueryError
from .render import MetaValue, Posting, RenderError, render_transaction
from .writer import WriteError, Writer, parse_blocks

T = TypeVar("T")

PREAMBLE = """\
This server manages a Beancount ledger (plain-text double-entry bookkeeping) hosted on a server.

How to work with it:
- Start with ledger_info (layout, files, bookkeeping policy) and list_accounts (exact account names, balances).
- Read with find_entries, get_entry, balances and query (BQL). Reads never change anything.
- Write with add_transaction (expenses, income, transfers), add_entries (any other directive, in Beancount
  syntax), and replace_entry / delete_entry for corrections, using an id from find_entries or get_entry.
- Every write is put in the right file in date order and checked against the whole ledger. If it would make
  the ledger invalid it is rejected with the errors and nothing is written. Accepted writes are committed to
  git. Pass dry_run=true to preview the diff without writing.
- Before adding, look for a likely duplicate with find_entries (date range plus amount or text).
"""

POLICY_NOTE = """\
The ledger owner's bookkeeping policy follows. If it was written for agents with shell access, use this
server's tools wherever it says to run Beancount commands (check, query, format) or to edit, create or
include files. Validation, formatting, file placement and includes are handled by the server.
"""

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
ADD = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
CHANGE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)

DryRun = Annotated[bool, Field(description="Validate and return the diff without writing anything")]


def load_policy(cfg: Config) -> str | None:
    if not cfg.ledger.instructions:
        return None
    path = cfg.ledger.root / cfg.ledger.instructions
    return path.read_text() if path.is_file() else None


def build_instructions(policy: str | None) -> str:
    if not policy:
        return PREAMBLE
    return f"{PREAMBLE}\n{POLICY_NOTE}\n--- Bookkeeping policy ---\n\n{policy.strip()}\n"


def _date(value: str | None, name: str) -> datetime.date | None:
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise ToolError(f"{name} must be YYYY-MM-DD, got {value!r}") from exc


def _decimal(value: str | None, name: str) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return abs(Decimal(str(value).replace(",", "")))
    except InvalidOperation as exc:
        raise ToolError(f"{name} must be a number, got {value!r}") from exc


def register_tools(server: MCPServer, cfg: Config, ledger: Ledger, git: GitRepo | None) -> None:
    writer = Writer(ledger, git)
    auth_enabled = cfg.auth.enabled

    def require(scope: str) -> None:
        if not auth_enabled:
            return
        token = get_access_token()
        if token is None or scope not in token.scopes:
            raise ToolError(f"this connection's token lacks the {scope} scope")

    def actor() -> str | None:
        token = get_access_token()
        if token is None:
            return None
        name = (token.claims or {}).get("client_name") or token.client_id
        who = f"{name} ({token.subject})" if token.subject and token.subject != name else name
        # client_name comes from open client registration; keep it to one line in commit messages.
        return " ".join("".join(c if c.isprintable() else " " for c in who).split())[:120]

    async def run(fn: Callable[[], T]) -> T:
        try:
            return await anyio.to_thread.run_sync(fn)
        except (QueryError, WriteError, RenderError) as exc:
            raise ToolError(str(exc)) from exc

    async def read(fn: Callable[[queries.Snapshot], T]) -> T:
        require(READ_SCOPE)
        return await run(lambda: fn(ledger.snapshot()))

    # -- reads -------------------------------------------------------------------------------

    @server.tool(annotations=READ)
    async def ledger_info() -> dict[str, Any]:
        """Overview of the ledger: title, operating currencies, commodities, files, entry counts,
        date range, error count, git state, where new entries are filed, and the bookkeeping policy.
        Call this first."""
        policy = load_policy(cfg)
        return await read(lambda snap: queries.ledger_info(ledger, snap, git, policy))

    @server.tool(annotations=READ)
    async def list_accounts(
        account: Annotated[
            str | None, Field(description="Regex searched in account names, e.g. '^Expenses:Food'")
        ] = None,
        include_closed: bool = False,
        with_balances: Annotated[bool, Field(description="Include each account's current balance")] = True,
    ) -> dict[str, Any]:
        """List open accounts with open date, allowed currencies, posting count and balance.
        Use the exact names from here when writing entries."""
        return await read(lambda snap: queries.list_accounts(snap, account, include_closed, with_balances))

    @server.tool(annotations=READ)
    async def balances(
        account: Annotated[
            str | None, Field(description="Regex searched in account names, e.g. '^Liabilities'")
        ] = None,
        as_of: Annotated[str | None, Field(description="YYYY-MM-DD; balances after all entries on that date")] = None,
        depth: Annotated[int | None, Field(description="Roll accounts up to this many components, e.g. 2")] = None,
    ) -> dict[str, Any]:
        """Account balances in units (no currency conversion), with a total per currency."""
        day = _date(as_of, "as_of")
        return await read(lambda snap: queries.balances(snap, account, day, depth))

    @server.tool(annotations=READ)
    async def find_entries(
        start_date: Annotated[str | None, Field(description="YYYY-MM-DD, inclusive")] = None,
        end_date: Annotated[str | None, Field(description="YYYY-MM-DD, inclusive")] = None,
        kind: Annotated[
            str | None,
            Field(description="transaction, balance, open, close, pad, note, price, document, event, custom"),
        ] = None,
        account: Annotated[str | None, Field(description="Regex searched in posting/directive accounts")] = None,
        text: Annotated[str | None, Field(description="Case-insensitive substring of the entry's source text")] = None,
        tag: str | None = None,
        link: str | None = None,
        flag: Annotated[str | None, Field(description="'!' finds pending transactions")] = None,
        min_amount: Annotated[str | None, Field(description="Minimum absolute amount of any posting")] = None,
        max_amount: Annotated[str | None, Field(description="Maximum absolute amount of any posting")] = None,
        currency: Annotated[str | None, Field(description="Only match amounts in this currency")] = None,
        limit: Annotated[int, Field(description="Maximum entries to return (1-500)")] = 50,
        oldest_first: bool = False,
    ) -> dict[str, Any]:
        """Search entries. Returns each entry's id, file, line and exact source text, newest first.
        Use it to check for duplicates before adding and to find the id of an entry to correct."""
        lo, hi = _decimal(min_amount, "min_amount"), _decimal(max_amount, "max_amount")
        start, end = _date(start_date, "start_date"), _date(end_date, "end_date")
        return await read(
            lambda snap: queries.find_entries(
                snap, start_date=start, end_date=end, kind=kind, account=account, text=text, tag=tag,
                link=link, flag=flag, min_amount=lo, max_amount=hi, currency=currency, limit=limit,
                oldest_first=oldest_first,
            )
        )  # fmt: skip

    @server.tool(annotations=READ)
    async def get_entry(
        id: Annotated[str, Field(description="Entry id from find_entries")],
        context_lines: Annotated[int, Field(description="Lines of surrounding file text to include (0-50)")] = 0,
    ) -> dict[str, Any]:
        """One entry's source text and location, its comment lines, and optionally the text around it."""
        return await read(lambda snap: queries.get_entry(ledger, snap, id, context_lines))

    @server.tool(annotations=READ)
    async def query(
        bql: Annotated[
            str,
            Field(
                description="Beancount Query Language, e.g. \"SELECT account, sum(position) WHERE account ~ '^Expenses' AND month = 10 GROUP BY account\""
            ),
        ],
        limit: Annotated[int, Field(description="Maximum rows to return (1-5000)")] = 200,
    ) -> dict[str, Any]:
        """Run a read-only BQL query (beanquery) over the whole ledger."""
        return await read(lambda snap: queries.run_query(snap, bql, limit))

    @server.tool(annotations=READ)
    async def validate() -> dict[str, Any]:
        """Load the whole ledger and report any errors (balance failures, unknown accounts, ...)."""
        return await read(queries.validate)

    @server.tool(annotations=READ)
    async def read_file(
        path: Annotated[str, Field(description="Path relative to the ledger directory, e.g. 'accounts.beancount'")],
        start_line: int = 1,
        end_line: int | None = None,
    ) -> dict[str, Any]:
        """Read a text file in the ledger directory (ledger files, README, configs). Hidden files are off limits."""
        require(READ_SCOPE)
        return await run(lambda: queries.read_file(ledger, path, start_line, end_line))

    # -- writes ------------------------------------------------------------------------------

    @server.tool(annotations=ADD)
    async def add_transaction(
        date: Annotated[str, Field(description="YYYY-MM-DD")],
        narration: Annotated[str, Field(description="What it was, e.g. 'Grocery'")],
        postings: Annotated[list[Posting], Field(description="At least two postings that balance")],
        payee: Annotated[str | None, Field(description='Merchant or counterparty, e.g. "Smith\'s"')] = None,
        flag: Annotated[str, Field(description="'*' cleared or '!' pending")] = "*",
        tags: list[str] | None = None,
        links: list[str] | None = None,
        metadata: dict[str, MetaValue] | None = None,
        dry_run: DryRun = False,
    ) -> dict[str, Any]:
        """Add one transaction. The server files it by date, aligns it like the surrounding entries,
        validates the whole ledger and commits. Returns the new entry's id and the diff."""
        require(WRITE_SCOPE)
        day = _date(date, "date")
        who = actor()

        def work():
            text = render_transaction(day, narration, postings, payee, flag, tags, links, metadata)
            blocks = parse_blocks(text)
            if len(blocks) != 1 or blocks[0].kind != "transaction":  # rendering must not leak into new directives
                raise RenderError("the fields did not render as exactly one transaction")
            return writer.add(blocks, tool="add_transaction", actor=who, dry_run=dry_run)

        return await run(work)

    @server.tool(annotations=ADD)
    async def add_entries(
        text: Annotated[
            str,
            Field(
                description="One or more dated Beancount directives; ';' comment lines directly above a directive are kept with it"
            ),
        ],
        dry_run: DryRun = False,
    ) -> dict[str, Any]:
        """Add any dated directives written in Beancount syntax: transactions, balance assertions, pad, note,
        price, event, document, custom, open/close (filed in the accounts file next to related accounts),
        commodity. option, include and plugin lines are refused."""
        require(WRITE_SCOPE)
        who = actor()
        return await run(lambda: writer.add(parse_blocks(text), tool="add_entries", actor=who, dry_run=dry_run))

    @server.tool(annotations=CHANGE)
    async def replace_entry(
        id: Annotated[str, Field(description="Entry id from find_entries or get_entry")],
        text: Annotated[str, Field(description="Corrected entry in Beancount syntax (one or more directives)")],
        include_comments: Annotated[
            bool, Field(description="Also replace the ';' comment lines directly above it")
        ] = False,
        dry_run: DryRun = False,
    ) -> dict[str, Any]:
        """Correct an existing entry. With the same date it is edited in place; with a new date it moves to
        the right file and position. Fails if the entry changed since its id was read."""
        require(WRITE_SCOPE)
        who = actor()
        return await run(
            lambda: writer.replace(
                id, parse_blocks(text), with_comments=include_comments, tool="replace_entry", actor=who,
                dry_run=dry_run,
            )
        )  # fmt: skip

    @server.tool(annotations=CHANGE)
    async def delete_entry(
        id: Annotated[str, Field(description="Entry id from find_entries or get_entry")],
        include_comments: Annotated[
            bool, Field(description="Also delete the ';' comment lines directly above it")
        ] = False,
        dry_run: DryRun = False,
    ) -> dict[str, Any]:
        """Delete an existing entry, if the ledger stays valid without it."""
        require(WRITE_SCOPE)
        who = actor()
        return await run(
            lambda: writer.delete(id, with_comments=include_comments, tool="delete_entry", actor=who, dry_run=dry_run)
        )

    # -- resources ---------------------------------------------------------------------------

    if cfg.ledger.instructions:

        @server.resource("ledger://policy", name="policy", mime_type="text/markdown")
        def policy_resource() -> str:
            """The ledger owner's bookkeeping policy."""
            require(READ_SCOPE)
            return load_policy(cfg) or ""
