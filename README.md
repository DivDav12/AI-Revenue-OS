# AI-Revenue-OS
Autonomous multi-agent AI revenue ecosystem

## PayPal

Read-only integration (`src/revenue_os/paypal.py`): it books payments PayPal
has already captured into the RevenueLedger. It cannot move money, change an
account, or create an obligation.

Setup: copy `.env.example` to `.env` and fill in `PAYPAL_CLIENT_ID`,
`PAYPAL_CLIENT_SECRET`, `PAYPAL_ENV` (`sandbox` or `live`). Nothing loads
`.env` automatically -- export the vars into the shell before running.

Commands:

- `revenue_os paypal-verify <candidate> <order-id>` -- verify one order and
  book it.
- `revenue_os paypal-sync [--days N] [--dry-run]` -- book recent payments,
  matched to candidates by the order's `custom_id`. Needs the "Transaction
  Search" feature enabled on the PayPal app.

Payments are tied to a candidate by setting the order's `custom_id` to the
candidate name. Booking still requires the candidate to be `launched` or
`earning`.

Moving from sandbox to live: see [docs/paypal-live-switch.md](docs/paypal-live-switch.md).
