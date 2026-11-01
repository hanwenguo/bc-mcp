import datetime

from bc_mcp.source import (
    add_include,
    align_postings,
    amount_column,
    delete_lines,
    insert_by_date,
    insert_declaration,
    scan_spans,
    split_lines,
)

TEXT = """\
;; -*- mode: beancount -*-
; balances first
2026-10-01 balance Assets:Cash 10.00 USD
2026-10-02 balance Assets:Cash 5.00 USD

2026-10-01 * "A" "first"
  Expenses:Food                            1.00 USD
  Assets:Cash

; note about b
2026-10-03 * "B" "third"
  ; inline comment
  Expenses:Food                            2.00 USD
  Assets:Cash
"""


def test_scan_spans_finds_directives_with_their_continuation_lines():
    spans = scan_spans("f.beancount", TEXT)
    assert [(s.kind, s.start, s.end) for s in spans] == [
        ("balance", 3, 3),
        ("balance", 4, 4),
        ("transaction", 6, 8),
        ("transaction", 11, 14),
    ]
    assert spans[3].text.splitlines()[1] == "  ; inline comment"
    assert spans[0].account == "Assets:Cash"


def test_identical_entries_get_distinct_stable_ids():
    text = '2026-10-01 * "x"\n  A:B  1 USD\n  A:C\n\n2026-10-01 * "x"\n  A:B  1 USD\n  A:C\n'
    first, second = scan_spans("f", text)
    assert first.id != second.id
    assert [s.id for s in scan_spans("f", text)] == [first.id, second.id]
    assert scan_spans("g", text)[0].id != first.id  # the file is part of the identity


def test_insert_by_date_goes_after_last_entry_on_or_before_the_date():
    lines = split_lines(TEXT)
    out, start = insert_by_date(lines, "f", datetime.date(2026, 10, 2), ['2026-10-02 * "new"'])
    assert out[start - 1] == '2026-10-02 * "new"'
    # The 10-02 balance comes before the 10-01 transaction in file order; the last
    # entry on or before 10-02 in file order is the 10-01 transaction.
    assert out[start - 3] == "  Assets:Cash" and out[start - 2] == ""
    assert out[start] == "" and out[start + 1] == "; note about b"


def test_insert_before_everything_skips_the_modeline():
    out, start = insert_by_date(split_lines(TEXT), "f", datetime.date(2026, 9, 1), ['2026-09-01 * "early"'])
    assert out[0].startswith(";; -*-")
    assert out[start - 1] == '2026-09-01 * "early"'
    assert out[start + 1] == "; balances first"


def test_amount_column_and_alignment_follow_the_file():
    assert amount_column(TEXT, 99) == 47
    aligned = align_postings('2026-10-05 * "x"\n  Expenses:Food 3.5 USD\n  Assets:Cash  -3.5 USD @ 1 EUR', 47)
    lines = aligned.splitlines()
    assert lines[1].index("3.5") + 3 == 47
    assert lines[2].endswith("-3.5 USD @ 1 EUR") and lines[2].index("-3.5") + 4 == 47


def test_alignment_keeps_arithmetic_and_elided_postings():
    block = '2026-10-05 * "x"\n  Assets:Cash 1.00 + 2.00 USD\n  Equity:Opening-Balances'
    out = align_postings(block, 47).splitlines()
    assert out[1].endswith("1.00 + 2.00 USD") and len(out[1]) == 47 + 4
    assert out[2] == "  Equity:Opening-Balances"


def test_open_goes_next_to_its_closest_relative():
    lines = [
        "2026-08-31 open Assets:Cash USD",
        "2026-08-31 open Expenses:Food:Dining USD",
        "2026-08-31 open Expenses:Food:Snack USD",
        "",
        "2026-08-31 open Expenses:Phone USD",
    ]
    out, start = insert_declaration(
        lines, "a", "open", "Expenses:Food:Coffee", ["2026-11-01 open Expenses:Food:Coffee"]
    )
    assert start == 4 and out[3] == "2026-11-01 open Expenses:Food:Coffee" and out[4] == ""
    out, start = insert_declaration(lines, "a", "open", "Liabilities:Card", ["2026-11-01 open Liabilities:Card"])
    assert out[-1] == "2026-11-01 open Liabilities:Card"


def test_add_include_keeps_sorted_order():
    lines = ['include "08/08.beancount"', 'include "10/10.beancount"']
    assert add_include(lines, "09/09.beancount")[1] == 'include "09/09.beancount"'
    assert add_include(lines, "11/11.beancount")[-1] == 'include "11/11.beancount"'
    assert add_include(lines, "07/07.beancount")[0] == 'include "07/07.beancount"'
    assert add_include(lines, "10/10.beancount") == lines


def test_delete_lines_collapses_blank_lines():
    lines = ["a", "", "b", "b2", "", "c"]
    assert delete_lines(lines, 2, 4) == ["a", "", "c"]
    assert delete_lines(["a", "", "b"], 2, 3) == ["a"]
