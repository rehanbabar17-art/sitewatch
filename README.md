# Sitewatch — Price & Stock Tracker

Tracks the **actual selling price and stock status** of products you configure
and alerts via [ntfy.sh](https://ntfy.sh) whenever a price changes, an item
restocks or goes out of stock, or a product hits its lowest-ever recorded price.

- Prices are rendered client-side on many stores (e.g. Daraz), so each run uses
  **Playwright + headless Chromium** on GitHub Actions runners.
- Shopify-based stores can be read without rendering via the store's `.json`
  API by marking the product with `"shopify": true`.
- Everything is run from GitHub Actions, triggered on demand (e.g. hourly via
  cron-job.org using a workflow-dispatch request).

## Privacy & secrets

This repository contains **no products, no ntfy topic, and no history**.
All personal configuration lives in GitHub Actions **repository secrets**:

| Secret | Purpose |
|---|---|
| `PRODUCTS_JSON` | JSON array of products to track (see `products.example.json`) |
| `NTFY_TOPIC` | ntfy.sh topic to push alerts to |

Price-history CSV files are kept **out of the repository** as well: they are
persisted between runs in the private GitHub Actions cache (key `price-history-*`),
never committed to git.

## Products format

Each product in `PRODUCTS_JSON` (a JSON array) looks like:

```json
{
  "name": "Example Shopify Product",
  "url": "https://shop.example.com/products/example-product",
  "history_file": "example_shopify_history.csv",
  "baseline_price": 2500,
  "shopify": true
}
```

- `name` — display name used in alerts.
- `url` — product page. For Shopify stores this must be the
  `https://<store>/products/<handle>` URL (used to build the `.json` API call).
- `history_file` — local CSV filename for this product's history (cached).
- `baseline_price` — expected price used as a sanity check: values deviating
  more than 50% from it are treated as unreliable reads and never fire alerts.
- `shopify` — optional; set `true` to use the lightweight `.json` + `cart/add.js`
  API instead of rendering the page with Playwright.

## Alerts

Alerts fire when:

- **Price changes** (up or down)
- **Item comes back in stock** (restock)
- **Item goes out of stock**
- **Item hits its all-time lowest price** (distinctive alert)

Reads that are unreliable (page did not render, price outside the sane range)
are recorded as `unreliable` and never trigger alerts.

## cron-job.org setup

1. Create a **GitHub fine-grained PAT** with `Actions: Read & Write` permission
   on the target repository.
2. Create a cron-job.org job pointing at:

   ```
   https://api.github.com/repos/<owner>/<repo>/actions/workflows/track-price.yml/dispatches
   ```

   - Method: `POST`
   - Schedule: e.g. every 1 hour
   - Request body: `{"ref":"main"}`
   - Headers:
     - `Authorization: Bearer <PAT>`
     - `Accept: application/vnd.github+json`
     - `Content-Type: application/json`

## Local run

```bash
pip install -r requirements.txt
python -m playwright install --with-deps chromium

PRODUCTS_JSON='[{"name":"...","url":"...","history_file":"x.csv","baseline_price":100}]' \
NTFY_TOPIC=your-ntfy-topic \
python3 price_tracker.py
```

## Files

| File | Purpose |
|---|---|
| `price_tracker.py` | Main tracker (fetch, detect, alert). |
| `products.example.json` | Example `PRODUCTS_JSON` structure. |
| `.github/workflows/track-price.yml` | GitHub Actions workflow (manual dispatch). |
