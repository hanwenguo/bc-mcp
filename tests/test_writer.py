import datetime

import pytest

from bc_mcp.render import Posting, render_transaction
from bc_mcp.writer import ValidationFailed, WriteError, parse_blocks

from .conftest import git

OCT = "books/2026/10/10.beancount"


def add(writer, text, dry_run=False):
    return writer.add(parse_blocks(text), tool="test", actor="tester", dry_run=dry_run)


def read(root, rel):
    return (root / rel).read_text()


def test_add_inserts_in_date_order_aligned_and_commits(writer, ledger_dir):
    before = git(ledger_dir, "rev-parse", "HEAD").strip()
    result = add(
        writer,
        '2026-10-27 * "Joe\'s" "Grocery"\n  Expenses:Food:Groceries 12.34 USD\n  Liabilities:CreditCard:Acme:Gold',
    )
    text = read(ledger_dir, OCT)
    lines = text.splitlines()
    i = lines.index('2026-10-27 * "Joe\'s" "Grocery"')
    assert lines[i + 1] == "  Expenses:Food:Groceries                 12.34 USD"
    # After the other 10-27 entries, before the first 10-28 one.
    assert lines[i - 4].startswith("2026-10-27") and lines[i + 4].startswith("2026-10-28")
    assert result["status"] == "written" and result["files"] == [OCT]
    assert result["entries"][0]["line"] == i + 1
    log = git(ledger_dir, "log", "-1", "--format=%s%n%b%n%an")
    assert log.startswith('ledger: add 2026-10-27 * "Joe\'s" "Grocery"')
    assert "Client: tester" in log and "bc-mcp" in log
    assert git(ledger_dir, "rev-parse", "HEAD~1").strip() == before
    assert git(ledger_dir, "status", "--porcelain") == ""


def test_new_month_creates_file_and_include(writer, ledger_dir):
    result = add(
        writer,
        '2026-11-01 * "Rent" "November rent"\n  Expenses:Housing:Rent 1500.00 USD\n  Assets:Bank:Acme:Checking',
    )
    new = read(ledger_dir, "books/2026/11/11.beancount")
    assert new.startswith(";; -*- mode: beancount -*-\n")
    assert read(ledger_dir, "books/2026/2026.beancount").endswith(
        'include "10/10.beancount"\ninclude "11/11.beancount"\n'
    )
    assert sorted(result["files"]) == ["books/2026/11/11.beancount", "books/2026/2026.beancount"]
    assert git(ledger_dir, "status", "--porcelain") == ""


def test_new_year_creates_year_index_and_main_include(writer, ledger_dir):
    add(writer, '2027-01-02 * "Coffee"\n  Expenses:Food:Snacks 4.00 USD\n  Liabilities:CreditCard:Acme:Gold')
    assert read(ledger_dir, "books/2027/2027.beancount").splitlines()[-1] == 'include "01/01.beancount"'
    main = read(ledger_dir, "main.beancount").splitlines()
    assert main[-2:] == ['include "books/2026/2026.beancount"', 'include "books/2027/2027.beancount"']
    assert writer.ledger.snapshot().errors == []


def test_invalid_change_is_rejected_and_nothing_is_written(writer, ledger_dir):
    head = git(ledger_dir, "rev-parse", "HEAD")
    original = read(ledger_dir, OCT)
    with pytest.raises(ValidationFailed) as exc:
        add(writer, '2026-10-30 * "Bad"\n  Expenses:DoesNotExist 1.00 USD\n  Assets:Bank:Acme:Checking')
    assert "unknown account 'Expenses:DoesNotExist'" in str(exc.value)
    assert exc.value.errors[0].file == OCT
    assert '+2026-10-30 * "Bad"' in exc.value.diff
    assert read(ledger_dir, OCT) == original
    assert git(ledger_dir, "rev-parse", "HEAD") == head


def test_failing_balance_assertion_is_rejected(writer):
    with pytest.raises(ValidationFailed, match="Balance failed"):
        add(writer, "2026-10-02 balance Assets:Bank:Acme:Savings 999.00 USD")


def test_unbalanced_transaction_is_rejected(writer):
    with pytest.raises(ValidationFailed, match="does not balance"):
        add(writer, '2026-10-30 * "x"\n  Expenses:Misc 1.00 USD\n  Assets:Bank:Acme:Checking -2.00 USD')


def test_syntax_errors_and_non_directives_are_refused(writer):
    with pytest.raises(WriteError, match="syntax"):
        add(writer, '2026-10-30 * "x"\n  Expenses:Misc 1..0 USD\n  Assets:Bank:Acme:Checking')
    with pytest.raises(WriteError, match="option, include, plugin"):
        add(writer, 'include "/etc/passwd"')


def test_dry_run_validates_without_writing(writer, ledger_dir):
    original = read(ledger_dir, OCT)
    result = add(writer, '2026-10-30 * "x"\n  Expenses:Misc 1.00 USD\n  Assets:Bank:Acme:Checking', dry_run=True)
    assert result["status"] == "dry_run" and result["commit"] is None
    assert '+2026-10-30 * "x"' in result["diff"]
    assert read(ledger_dir, OCT) == original


def test_open_lands_in_accounts_file_section(writer, ledger_dir):
    add(writer, "2026-11-01 open Expenses:Food:Coffee USD")
    lines = read(ledger_dir, "accounts.beancount").splitlines()
    i = lines.index("2026-11-01 open Expenses:Food:Coffee USD")
    assert lines[i - 1] == "2026-08-31 open Expenses:Food:Snacks USD"


def test_comments_stay_with_their_entry(writer, ledger_dir):
    add(writer, "; observed on the statement\n2026-10-31 balance Assets:Bank:Acme:Savings 137.50 USD")
    text = read(ledger_dir, OCT)
    assert "; observed on the statement\n2026-10-31 balance Assets:Bank:Acme:Savings 137.50 USD\n" in text


def test_replace_in_place_and_moving_date(writer, ledger, ledger_dir):
    span = next(s for s in ledger.snapshot().spans.values() if '"SearchCo"' in s.text and s.file == OCT)
    result = writer.replace(
        span.id,
        parse_blocks('2026-10-28 * "SearchCo" "SearchCo plan"\n  Expenses:Subscriptions 10.00 USD\n  Liabilities:CreditCard:Acme:Blue'),
        with_comments=False, tool="test", actor=None, dry_run=False,
    )  # fmt: skip
    assert result["removed"][0]["id"] == span.id
    lines = read(ledger_dir, OCT).splitlines()
    assert lines[span.start - 1] == '2026-10-28 * "SearchCo" "SearchCo plan"'
    new_id = result["entries"][0]["id"]

    result = writer.replace(
        new_id,
        parse_blocks('2026-11-02 * "SearchCo" "SearchCo plan"\n  Expenses:Subscriptions 10.00 USD\n  Liabilities:CreditCard:Acme:Blue'),
        with_comments=False, tool="test", actor=None, dry_run=False,
    )  # fmt: skip
    assert '"SearchCo" "SearchCo plan"' not in read(ledger_dir, OCT)
    assert '2026-11-02 * "SearchCo"' in read(ledger_dir, "books/2026/11/11.beancount")
    assert result["entries"][0]["file"] == "books/2026/11/11.beancount"
    assert ledger.snapshot().errors == []


def test_stale_ids_are_refused(writer, ledger):
    span = next(s for s in ledger.snapshot().spans.values() if '"SearchCo"' in s.text and s.file == OCT)
    writer.delete(span.id, with_comments=False, tool="test", actor=None, dry_run=False)
    with pytest.raises(WriteError, match="no entry with id"):
        writer.delete(span.id, with_comments=False, tool="test", actor=None, dry_run=False)


def test_delete_with_comments(writer, ledger, ledger_dir):
    span = next(s for s in ledger.snapshot().spans.values() if "^recon-20261023" in s.text)
    # Deleting the reconciliation alone would break the balance assertions? It has none after it,
    # so the ledger stays valid.
    writer.delete(span.id, with_comments=True, tool="test", actor=None, dry_run=False)
    text = read(ledger_dir, OCT)
    assert "recon-20261023" not in text
    assert "; Reconcile to balances observed on October 23" not in text
    assert "\n\n\n" not in text


def test_delete_that_breaks_a_balance_assertion_is_rejected(writer, ledger):
    span = next(s for s in ledger.snapshot().spans.values() if "^recon-20261001" in s.text)
    with pytest.raises(ValidationFailed, match="Balance failed"):
        writer.delete(span.id, with_comments=False, tool="test", actor=None, dry_run=False)


def test_uncommitted_changes_are_snapshotted_first(writer, ledger_dir):
    path = ledger_dir / OCT
    path.write_text(
        path.read_text() + '\n2026-10-31 * "Edited by hand"\n  Expenses:Misc 1.00 USD\n  Assets:Bank:Acme:Checking\n'
    )
    result = add(writer, '2026-10-31 * "Via MCP"\n  Expenses:Misc 2.00 USD\n  Assets:Bank:Acme:Checking')
    assert any("pre-existing" in w for w in result["warnings"])
    subjects = git(ledger_dir, "log", "-2", "--format=%s").splitlines()
    assert subjects[0].startswith('ledger: add 2026-10-31 * "Via MCP"')
    assert subjects[1] == "ledger: snapshot uncommitted changes before bc-mcp write"
    added = [line for line in git(ledger_dir, "show", "HEAD", "--", OCT).splitlines() if line.startswith("+")]
    assert not any("Edited by hand" in line for line in added)
    assert '+2026-10-31 * "Edited by hand"' in git(ledger_dir, "show", "HEAD~1", "--", OCT)


def test_concurrent_external_edit_is_detected(writer, ledger_dir, monkeypatch):
    original_validate = writer.validate

    def validate_then_edit(snap, changes, **kw):
        errors = original_validate(snap, changes, **kw)
        path = ledger_dir / OCT
        path.write_text(path.read_text() + "; someone else\n")
        return errors

    monkeypatch.setattr(writer, "validate", validate_then_edit)
    with pytest.raises(WriteError, match="changed on disk"):
        add(writer, '2026-10-31 * "x"\n  Expenses:Misc 1.00 USD\n  Assets:Bank:Acme:Checking')


def test_render_transaction_matches_ledger_style(writer, ledger_dir):
    text = render_transaction(
        datetime.date(2026, 10, 31),
        "Snacks",
        [
            Posting(account="Expenses:Food:Snacks", amount="7.98", currency="USD"),
            Posting(account="Liabilities:CreditCard:Acme:Gold", amount="-7.98", currency="USD"),
        ],
        payee='Corner "Store"',
        tags=["saving"],
        metadata={"saving_amount": "7.98", "source": True},
    )
    add(writer, text)
    assert (
        '2026-10-31 * "Corner \\"Store\\"" "Snacks" #saving\n'
        '  saving_amount: "7.98"\n'
        "  source: TRUE\n"
        "  Expenses:Food:Snacks                     7.98 USD\n"
        "  Liabilities:CreditCard:Acme:Gold        -7.98 USD\n"
    ) in read(ledger_dir, OCT)


def test_price_conversion_posting(writer, ledger_dir):
    text = render_transaction(
        datetime.date(2026, 10, 31),
        "Transfer to Jade",
        [
            Posting(
                account="Assets:Bank:Jade:Euro",
                amount="100.00",
                currency="EUR",
                price="108.00 USD",
                total_price=True,
            ),
            Posting(account="Liabilities:CreditCard:Acme:Blue", amount="-108.00", currency="USD"),
        ],
    )
    add(writer, text)
    assert "  Assets:Bank:Jade:Euro                  100.00 EUR @@ 108.00 USD\n" in read(ledger_dir, OCT)


def test_edit_to_another_file_during_a_write_is_detected(writer, ledger_dir, monkeypatch):
    # Validation used every included file, so a hand edit to accounts.beancount must stop the write too.
    original_validate = writer.validate

    def validate_then_edit(snap, changes, **kw):
        errors = original_validate(snap, changes, **kw)
        path = ledger_dir / "accounts.beancount"
        path.write_text(path.read_text() + "; someone else\n")
        return errors

    monkeypatch.setattr(writer, "validate", validate_then_edit)
    with pytest.raises(WriteError, match="changed on disk"):
        add(writer, '2026-10-31 * "x"\n  Expenses:Misc 1.00 USD\n  Assets:Bank:Acme:Checking')
    assert '2026-10-31 * "x"' not in read(ledger_dir, OCT)


SMUGGLED = '\n\n2026-10-27 * "Hidden"\n  Assets:Bank:Acme:Checking  -500.00 USD\n  Assets:Bank:Acme:Savings'


@pytest.mark.parametrize(
    "field",
    [
        {"account": "Liabilities:CreditCard:Acme:Gold" + SMUGGLED},
        {"account": "Expenses:Food:Groceries ; comment"},
        {"flag": '!\n2026-10-27 note Assets:Bank:Acme:Checking "x"'},
        {"flag": "P"},
        {"price": '1.00 USD\n2026-10-27 note Assets:Bank:Acme:Checking "x"'},
        {"price": "1.00 USD ; x"},
        {"cost": '{1.00 USD}\n2026-10-27 note Assets:Bank:Acme:Checking "x"\n{'},
        {"cost": "{1.00 USD} ; x {}"},
    ],
)
def test_posting_fields_cannot_leave_their_line(field):
    base = {"account": "Expenses:Food:Groceries", "amount": "1.00", "currency": "USD"}
    with pytest.raises(ValueError):
        render_transaction(
            datetime.date(2026, 10, 27), "Coffee",
            [Posting(**(base | field)), Posting(account="Liabilities:CreditCard:Acme:Gold")],
        )  # fmt: skip


def test_cost_and_price_forms_still_render():
    for cost in ("{100.00 USD}", "{}", "{{1000.00 USD}}", '{100.00 USD, 2026-01-02, "lot-1"}'):
        text = render_transaction(
            datetime.date(2026, 10, 27), "Buy",
            [Posting(account="Assets:Broker", amount="10", currency="VTI", cost=cost, price="101.5 USD"),
             Posting(account="Assets:Bank:Acme:Checking")],
        )  # fmt: skip
        assert len(parse_blocks(text)) == 1


def test_document_paths_stay_inside_the_ledger(writer, ledger_dir):
    (ledger_dir / "documents").mkdir()
    (ledger_dir / "documents" / "receipt.pdf").write_bytes(b"%PDF")
    (ledger_dir / "books" / "2026" / "10" / "scan.pdf").write_bytes(b"%PDF")
    for path in ("/etc/passwd", "/etc/nonexistent", "../../../../etc/passwd", "../../../.git/config", "../../../../x"):
        with pytest.raises(WriteError, match="inside the ledger directory"):
            add(writer, f'2026-10-27 document Assets:Bank:Acme:Checking "{path}"', dry_run=True)
    with pytest.raises(WriteError, match="does not exist"):
        add(writer, '2026-10-27 document Assets:Bank:Acme:Checking "nope.pdf"', dry_run=True)
    # Relative to the month file, like beancount resolves them.
    add(writer, '2026-10-27 document Assets:Bank:Acme:Checking "../../../documents/receipt.pdf"')
    add(writer, '2026-10-28 document Assets:Bank:Acme:Checking "scan.pdf"')
    assert '"../../../documents/receipt.pdf"' in read(ledger_dir, OCT) and '"scan.pdf"' in read(ledger_dir, OCT)


def test_fava_configuration_is_refused(writer):
    for text in (
        '2026-10-27 custom "fava-extension" "evil_module"',
        '2026-10-27 custom "fava-option" "import-config" "x.py"',
    ):
        with pytest.raises(WriteError, match="configure Fava"):
            add(writer, text)
