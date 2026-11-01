# Bookkeeping policy for the test ledger

A made-up policy for bc-mcp's tests. bc-mcp serves this file to agents as its instructions.

## Recording expenses

- Card purchases go to the card's liability account, with the merchant as payee.
- Use the most specific expense account that exists; open a new one only when nothing fits.
- Run bean-check after every change.

## Reconciling

Transfers that make an account match a statement get a `^recon-YYYYMMDD` link and a comment above
them saying which statement they match.

## Monthly budget reporting

Monthly budgets per expense group are in budget.json. Report spending against them at month end.
