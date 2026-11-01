import json

import pytest
from mcp import Client

from bc_mcp.app import build_server

pytestmark = pytest.mark.anyio


async def call(client, name, **args):
    result = await client.call_tool(name, args)
    text = result.content[0].text
    if result.is_error:
        raise AssertionError(text)
    return json.loads(text)


async def call_error(client, name, **args) -> str:
    result = await client.call_tool(name, args)
    assert result.is_error, result
    return result.content[0].text


@pytest.fixture
def server(config):
    return build_server(config).server


async def test_tools_and_instructions(server):
    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert names == {
            "ledger_info", "list_accounts", "balances", "find_entries", "get_entry", "query", "validate",
            "read_file", "add_transaction", "add_entries", "replace_entry", "delete_entry",
        }  # fmt: skip
        instructions = client.instructions
        assert "Bookkeeping policy for the test ledger" in instructions
        assert "run Beancount commands" in instructions
        policy = await client.read_resource("ledger://policy")
        assert "Monthly budget reporting" in policy.contents[0].text


async def test_ledger_info_and_accounts(server):
    async with Client(server) as client:
        info = await call(client, "ledger_info")
        assert info["operating_currencies"] == ["USD", "EUR"]
        assert info["errors"] == 0 and info["git"]["uncommitted"] == []
        assert "README.md" in info["other_files"] and "budget.json" in info["other_files"]
        accounts = await call(client, "list_accounts", account="^Liabilities")
        names = [a["account"] for a in accounts["accounts"]]
        assert "Liabilities:CreditCard:Acme:Gold" in names
        gold = next(a for a in accounts["accounts"] if a["account"].endswith(":Gold"))
        assert gold["balance"] and gold["balance"][0].endswith("USD")


async def test_balances_match_bql(server):
    async with Client(server) as client:
        bal = await call(client, "balances", account="^Assets:Bank:Acme:Savings$", as_of="2026-09-30")
        rows = await call(
            client, "query",
            bql="SELECT sum(number) WHERE account = 'Assets:Bank:Acme:Savings' AND date <= 2026-09-30",
        )  # fmt: skip
        assert bal["accounts"][0]["balance"] == [f"{rows['rows'][0][0]} USD"] == ["112.50 USD"]
        rolled = await call(client, "balances", account="^Assets:Bank", depth=2)
        assert [a["account"] for a in rolled["accounts"]] == ["Assets:Bank"]


async def test_find_entries_for_duplicate_check(server):
    async with Client(server) as client:
        found = await call(
            client, "find_entries", start_date="2026-10-31", end_date="2026-10-31", min_amount="9", max_amount="9"
        )
        assert found["total"] == 1
        entry = found["entries"][0]
        assert entry["text"].startswith('2026-10-31 * "Corner Cafe" "Brunch"')
        assert entry["file"] == "books/2026/10/10.beancount"
        detail = await call(client, "get_entry", id=entry["id"], context_lines=2)
        assert detail["text"] == entry["text"] and detail["before"]
        tagged = await call(client, "find_entries", link="recon-20261023")
        assert tagged["total"] == 1
        assert "Error" in await call_error(client, "find_entries", account="(")


async def test_add_transaction_then_correct_it(server, ledger_dir):
    async with Client(server) as client:
        added = await call(
            client, "add_transaction",
            date="2026-10-31", payee="Corner Store", narration="Snacks",
            postings=[
                {"account": "Expenses:Food:Snacks", "amount": "3.25", "currency": "USD"},
                {"account": "Liabilities:CreditCard:Acme:Gold"},
            ],
        )  # fmt: skip
        assert added["status"] == "written" and added["commit"]
        entry_id = added["entries"][0]["id"]
        fixed = await call(
            client, "replace_entry", id=entry_id,
            text='2026-10-31 * "Corner Store" "Snacks"\n  Expenses:Food:Snacks  3.52 USD\n  Liabilities:CreditCard:Acme:Gold',
        )  # fmt: skip
        assert "-  Expenses:Food:Snacks                     3.25 USD" in fixed["diff"]
        assert "+  Expenses:Food:Snacks                     3.52 USD" in fixed["diff"]
        await call(client, "delete_entry", id=fixed["entries"][0]["id"])
        assert (
            'Corner Store" "Snacks"\n  Expenses:Food:Snacks                     3.'
            not in (ledger_dir / "books/2026/10/10.beancount").read_text().split('2026-10-31 * "PhoneCo"')[1]
        )


async def test_write_errors_reach_the_model(server):
    async with Client(server) as client:
        message = await call_error(
            client, "add_transaction", date="2026-10-31", narration="x",
            postings=[{"account": "Expenses:Nope", "amount": "1", "currency": "USD"}, {"account": "Assets:Bank:Acme:Checking"}],
        )  # fmt: skip
        assert "Rejected" in message and "Expenses:Nope" in message
        assert "amount and currency" in await call_error(
            client, "add_transaction", date="2026-10-31", narration="x",
            postings=[{"account": "Expenses:Misc", "amount": "1"}, {"account": "Assets:Bank:Acme:Checking"}],
        )  # fmt: skip
        assert "YYYY-MM-DD" in await call_error(client, "balances", as_of="10/31")


async def test_read_file_is_confined(server):
    async with Client(server) as client:
        main = await call(client, "read_file", path="main.beancount", start_line=1, end_line=3)
        assert main["text"].startswith(";; -*- mode: beancount")
        for path in ("../../etc/passwd", ".git/config", "/etc/hosts"):
            assert "outside the ledger" in await call_error(client, "read_file", path=path)


async def test_add_transaction_writes_exactly_one_transaction(server, ledger_dir):
    async with Client(server) as client:
        smuggled = 'Liabilities:CreditCard:Acme:Gold\n\n2026-10-27 note Assets:Bank:Acme:Checking "x"'
        message = await call_error(
            client, "add_transaction", date="2026-10-27", narration="Coffee",
            postings=[{"account": "Expenses:Food:Snacks", "amount": "3", "currency": "USD"}, {"account": smuggled}],
        )  # fmt: skip
        assert "invalid account name" in message
        assert '"x"' not in (ledger_dir / "books/2026/10/10.beancount").read_text()


async def test_query_cannot_open_files(server, tmp_path):
    secret = tmp_path / "secret.csv"
    secret.write_text("a,b\n1,2\n")
    async with Client(server) as client:
        for bql in (f"CREATE TABLE t USING 'csv://{secret}'", "CREATE TABLE t USING 'csv:///nonexistent'"):
            message = await call_error(client, "query", bql=bql)
            assert "only SELECT" in message and "No such file" not in message
        # Regexes with {n,m} quantifiers work (no str.format on the query).
        rows = await call(client, "query", bql="SELECT DISTINCT account WHERE account ~ '^Assets:Ba.{2}:Acme'")
        assert rows["rows"] and all(r[0].startswith("Assets:Bank:Acme") for r in rows["rows"])
        assert (await call(client, "query", bql="BALANCES WHERE account ~ '^Assets'"))["rows"]
