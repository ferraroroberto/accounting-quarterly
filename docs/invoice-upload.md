# Invoice Upload

The Invoice Upload tab supports uploading invoice PDFs to the accounting partner API (IntegraLOOP/BILOOP).

- **Invoices In** (`data/invoices/in/`): received invoices
- **Invoices Out** (`data/invoices/out/`): produced invoices
- Tracks which files have been uploaded to avoid duplicates

Enable it in `config.json`:

```json
{
  "accounting_api": {
    "company_id": "YOUR_COMPANY_ID",
    "enabled": true
  }
}
```

Then set the Accounting API credentials in `.env`:

```
ACCOUNTING_BASE_URL=https://api.example.com
ACCOUNTING_SUBSCRIPTION_KEY=your_subscription_key_here
ACCOUNTING_TOKEN=your_token_here
# OR (optional) user/pass to fetch a 2h token via /api-global/v1/token
ACCOUNTING_USER=your_user_here
ACCOUNTING_PASSWORD=your_password_here
```
