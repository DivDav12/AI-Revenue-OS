# Switching PayPal from sandbox to live

The PayPal integration (`src/revenue_os/paypal.py`) is read-only: it books
payments PayPal has already captured into the RevenueLedger. It cannot move
money. Switching to live only changes which PayPal account is read.

## PayPal side

1. Log in to developer.paypal.com with the real business account.
2. The account must be a **Business** account -- `paypal-sync` uses the
   Transaction Search API, which personal accounts do not have.
3. Apps & Credentials -> toggle **Live** -> select/create an app -> copy the
   live **Client ID** and **Secret** (different from the sandbox ones).
4. On the app, under **Features**, enable **Transaction Search** (needed for
   `paypal-sync`; not for `paypal-verify`). Allow a short activation delay.
5. On every live button / invoice / payment link, set `custom_id` (or the
   invoice custom field) to the exact candidate name -- same mapping rule as
   sandbox.

## Project side

1. Put the live values in `.env`:
   ```
   PAYPAL_CLIENT_ID=<live client id>
   PAYPAL_CLIENT_SECRET=<live secret>
   PAYPAL_ENV=live
   ```
2. Nothing loads `.env` automatically -- export the vars into the shell
   before running the CLI. PowerShell:
   ```powershell
   $env:PAYPAL_CLIENT_ID     = "<live client id>"
   $env:PAYPAL_CLIENT_SECRET = "<live secret>"
   $env:PAYPAL_ENV           = "live"
   ```
3. The package is not installed -- run with `pip install -e .` once, or set
   `$env:PYTHONPATH = "src"` per session.
4. `.env` stays gitignored. Never commit live credentials.

## Verify without a real payment

```
python -m revenue_os paypal-sync --dry-run --days 1
```

Read-only: fetches an OAuth token, lists transactions, books nothing.

- `would book 0 payment(s) ... 0 skipped` -> credentials valid, Transaction
  Search enabled. (Confirmed working on 2026-08-29.)
- `PayPal API 401` -> wrong client id/secret, or still sandbox values.
- `PayPal API 403` -> Transaction Search not activated yet.

When a real payment arrives, verify it singly first:

```
python -m revenue_os paypal-verify <candidate> <order-id>
```

then switch to `paypal-sync` (without `--dry-run`) for routine booking.

## Notes

- Live payments may be in EUR; `record_payment` books the returned currency
  as-is. Transaction Search lags up to ~3h; `--days` max is 31.
- The candidate must be `launched` or `earning`, or the payment is skipped.
