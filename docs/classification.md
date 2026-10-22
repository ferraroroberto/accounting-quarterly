# Classification Logic

Classification rules are defined in `classification_rules.json` and can be edited directly or through the Configuration tab in the dashboard.

## Activity type

Rules are evaluated in priority order; the first match wins.

| Priority | Match Type | Activity |
|----------|-----------|----------|
| 1 | Empty / null description | COACHING |
| 2 | Luma `registration` payment type | COACHING |
| 3 | Description contains illustration keywords | ILLUSTRATIONS |
| 4 | Description contains newsletter keywords | NEWSLETTER |
| 5 | Description contains coaching keywords | COACHING |
| - | No pattern matched | UNKNOWN |

## Geographic region

**EUR charges** — unchanged, currency alone decides the branch:

| Priority | Condition | Default region |
|----------|-----------|----------------|
| 1 | EUR + explicit name/email override | Per override |
| 2 | EUR + activity is **NEWSLETTER** | EU_NOT_SPAIN |
| 3 | EUR + any other activity | SPAIN |

**Non-EUR charges** — classified by the *charge country* first (card issuing country → Stripe billing address → Stripe customer address, first one set wins), currency only as a fallback when no country is known at all (a non-EUR currency alone is not evidence of being outside the EU: DKK, SEK, PLN, CZK, HUF, RON and BGN are EU member-state currencies):

| Priority | Condition | Region |
|----------|-----------|--------|
| 1 | Charge country known, `ES` | SPAIN |
| 2 | Charge country known, other EU member state | EU_NOT_SPAIN |
| 3 | Charge country known, non-EU | OUTSIDE_EU |
| 4 | No charge country, currency is an EU non-euro currency (DKK/SEK/PLN/CZK/HUF/RON/BGN) | EU_NOT_SPAIN, flagged for review (`non_eur_currency_eu_review` rule — weaker signal than a known country) |
| 5 | No charge country, any other non-EUR currency | `non_eur_default` (OUTSIDE_EU unless changed) |

The default region for each EUR condition, and for non-EUR row 5 (`non_eur_default`), is configurable in the Geographic Rules section of the Configuration tab. Name/email overrides apply only to EUR charges; the charge-country rule cannot be overridden by name/email today.

**Foreign-customer warning.** A EUR charge that falls through to the `eur_default` rule is flagged with ⚠ when the customer looks non-Spanish: a card or billing-address country other than `ES`, or an email (Stripe customer email, billing email, or one written in the description) on a country-code domain other than `.es` (generic-use ccTLDs such as `.io`, `.co`, `.me` are ignored). The flag shows in `close_quarter.py stripe-fetch`, the Transaction Browser (**Review** column) and the Quarter Report. Fix it with a geographic override.

## Customer VAT ID overrides (EU B2B vs. B2C)

For an EU (non-Spain) sale, whether it is booked as B2B (reverse charge) or B2C (Spanish 21% / OSS) depends on whether the customer's VAT id is known — not on the activity (see [VAT treatment classification](tax-obligations.md#vat-treatment-classification)). The VAT id is looked up the same way as a geographic override, in `classification_rules.json`'s `customer_vat_ids`:

```json
"customer_vat_ids": {
  "email_vat_ids": {
    "client@example.com": "DE123456789"
  },
  "name_vat_ids": {
    "client name as it appears in description": "DE123456789"
  }
}
```

Editable in the same **Configuration → Geographic Rules** tab as the geographic overrides, under **Customer VAT IDs (B2B)**. An email match wins, then a name/description match. As a read-only fallback, a Stripe `customer.tax_ids` entry already present in the stored raw charge is used if the override lookup finds nothing (currently a no-op — the Stripe fetch does not request `tax_ids`). VIES validity is **not** checked; verify the id yourself before relying on the reverse charge.

## Card issuing country

The card issuing country (`charge.payment_method_details.card.country`) is extracted from the Stripe API automatically. It is the first signal used to classify a non-EUR charge's geographic region (see above), and also improves the foreign-customer warning on EUR charges.

## Stripe API

Set `STRIPE_API_KEY` in a `.env` file at the project root:

```
STRIPE_API_KEY=sk_live_...
```

Required permissions for restricted keys (`rk_live_...`):
- **Read charges** — transaction data, amounts, descriptions, card country
- **Read balance transactions** — fee details

The dashboard includes a connection tester and permission checker under **Configuration → Stripe API**.
