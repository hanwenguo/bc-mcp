"""Rendering structured input as Beancount text."""

from __future__ import annotations

import datetime
import re
from decimal import Decimal, InvalidOperation

from beancount.core import account as account_mod
from pydantic import BaseModel, Field

TAG = re.compile(r"[A-Za-z0-9\-_/.]+")
META_KEY = re.compile(r"[a-z][a-zA-Z0-9_-]+")
CURRENCY = re.compile(r"[A-Z][A-Z0-9'._-]{0,22}[A-Z0-9]|[A-Z]")
_CUR = CURRENCY.pattern
# Price "21.69 USD"; cost "{100.00 USD}", "{}", "{100.00 USD, 2026-01-02}", "{100.00 # 5 USD, "lot"}". These end
# up verbatim in the posting line, so they may not contain anything that ends the line or starts a comment.
PRICE = re.compile(rf"-?(?:\d[\d,]*(?:\.\d*)?|\.\d+) (?:{_CUR})")
COST = re.compile(r'\{\{?[^{}\n\r;"]*(?:"[^"\n\r\\]*"[^{}\n\r;"]*)?\}?\}')

MetaValue = str | bool | int | float


class RenderError(ValueError):
    pass


class Posting(BaseModel):
    account: str = Field(description="Full account name, e.g. 'Expenses:Food:Groceries'")
    amount: str | None = Field(
        default=None,
        description="Number such as '35.53' or '-35.53'. Omit amount and currency on at most one "
        "posting to let Beancount balance it.",
    )
    currency: str | None = Field(default=None, description="Commodity, e.g. 'USD'")
    cost: str | None = Field(default=None, description="Cost in Beancount syntax, e.g. '{100.00 USD}'")
    price: str | None = Field(default=None, description="Price amount, e.g. '21.69 USD'")
    total_price: bool = Field(default=False, description="true writes '@@' (total price) instead of '@' (per unit)")
    flag: str | None = Field(default=None, description="Optional posting flag, '!' or '*'")
    metadata: dict[str, MetaValue] | None = None


def quote(text: str) -> str:
    text = " ".join(text.split())
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def meta_lines(meta: dict[str, MetaValue] | None, indent: str) -> list[str]:
    lines = []
    for key, value in (meta or {}).items():
        if not META_KEY.fullmatch(key):
            raise RenderError(f"metadata key {key!r} must start lowercase and be at least 2 characters")
        if isinstance(value, bool):
            rendered = "TRUE" if value else "FALSE"
        elif isinstance(value, (int, float)):
            rendered = str(Decimal(str(value)))
        else:
            rendered = quote(str(value))
        lines.append(f"{indent}{key}: {rendered}")
    return lines


def _number(text: str) -> str:
    cleaned = text.strip().replace(",", "")
    try:
        Decimal(cleaned)
    except InvalidOperation as exc:
        raise RenderError(f"amount {text!r} is not a number") from exc
    return cleaned


def render_transaction(
    date: datetime.date,
    narration: str,
    postings: list[Posting],
    payee: str | None = None,
    flag: str = "*",
    tags: list[str] | None = None,
    links: list[str] | None = None,
    metadata: dict[str, MetaValue] | None = None,
) -> str:
    if flag not in ("*", "!"):
        raise RenderError("flag must be '*' (cleared) or '!' (pending)")
    if len(postings) < 1:
        raise RenderError("a transaction needs postings")
    header = [date.isoformat(), flag]
    if payee:
        header.append(quote(payee))
    header.append(quote(narration))
    for tag in tags or []:
        tag = tag.lstrip("#")
        if not TAG.fullmatch(tag):
            raise RenderError(f"invalid tag {tag!r}")
        header.append(f"#{tag}")
    for link in links or []:
        link = link.lstrip("^")
        if not TAG.fullmatch(link):
            raise RenderError(f"invalid link {link!r}")
        header.append(f"^{link}")
    lines = [" ".join(header), *meta_lines(metadata, "  ")]
    for p in postings:
        if not account_mod.is_valid(p.account):
            raise RenderError(f"invalid account name {p.account!r}; use the exact names from list_accounts")
        if p.flag not in (None, "*", "!"):
            raise RenderError(f"posting flag must be '*' or '!', got {p.flag!r}")
        if (p.amount is None) != (p.currency is None):
            raise RenderError(f"posting {p.account}: give both amount and currency, or neither")
        if p.currency and not CURRENCY.fullmatch(p.currency):
            raise RenderError(f"invalid currency {p.currency!r}")
        parts = [f"{p.flag} " if p.flag else "", p.account]
        if p.amount is not None:
            parts.append(f"  {_number(p.amount)} {p.currency}")
            if p.cost:
                cost = p.cost.strip()
                if not COST.fullmatch(cost):
                    raise RenderError(f"cost {p.cost!r} must look like '{{100.00 USD}}' on one line")
                parts.append(f" {cost}")
            if p.price:
                price = " ".join(p.price.split())
                if not PRICE.fullmatch(price):
                    raise RenderError(f"price {p.price!r} must look like '21.69 USD'")
                parts.append(f" {'@@' if p.total_price else '@'} {price}")
        elif p.cost or p.price:
            raise RenderError(f"posting {p.account}: cost and price need an amount")
        lines.append("  " + "".join(parts))
        lines.extend(meta_lines(p.metadata, "    "))
    return "\n".join(lines)
