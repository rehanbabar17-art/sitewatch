# 🛒 Sitewatch — Automated E-Commerce Price & Stock Tracker

**Sitewatch** is a headless, serverless price and inventory tracking engine powered by **GitHub Actions**, **Playwright (Chromium)**, and **ntfy.sh**.

It automatically monitors prices, struck-through MRPs, stock availability, and all-time-low records across multi-vendor e-commerce stores (including Shopify stores, Daraz marketplace, and custom brand websites) while keeping monitored product lists, URLs, and pricing data private.

---

## ⚡ Key Highlights & Architecture

- 🔒 **Private B2 Persistence**: Product configurations (`products.json`), per-item historical price records (`*.csv`), and daily-audit state (`audit_state.json`) are stored in the private `sitewatch/` folder in Backblaze B2. No private product lists are committed to git.
- 🛍️ **Dual-Engine Scraping**:
  - **Shopify API Engine**: Lightweight read-only `.json` endpoint extraction for price/title data, combined with rendered product-page availability checks for Shopify stock.
  - **Playwright Headless Browser Engine**: Full headless Chromium rendering with client-side price extraction and stock status detection for dynamic Single Page Applications (SPAs) and marketplaces like Daraz.
- 🔔 **Instant Multi-Device Alerts**: Real-time push notifications via [ntfy.sh](https://ntfy.sh) for price drops, price increases, restocks, out-of-stock events, and all-time lows.
- 🧾 **Availability on Every Price Alert**: Price-change and all-time-low notifications include the current purchase status — **In stock**, **Out of stock**, or **Unknown** — so a low price is not mistaken for an item that can be purchased.
- 🛡️ **Privacy-Hardened Logs**: Actions console output and job step summaries are anonymized (`Item #1`, `Item #2`, etc.) to protect tracked product names in public repository runs.

---

## 🚀 How It Works

```
[cron-job.org / Webhook / Dispatch]
               │
               ▼
   [GitHub Actions Workflow]
               │
  ┌────────────┴────────────┐
  │  1. Restore B2          │ ◄── Restores products.json & *.csv history
  │  2. Execute Scraper     │
  │     ├─ Shopify API      │ ◄── Instant JSON + cart probe
  │     └─ Playwright       │ ◄── Full Chromium browser rendering
  │  3. Change Detection    │ ◄── Compares vs last reliable price & history
  │  4. Alert Dispatch      │ ──► Sends push notification via ntfy.sh
  │  5. Upload B2           │ ──► Persists updated products.json & *.csv
  └─────────────────────────┘
```

---

## 📦 Data & Caching Model

| Component | Storage Location | Purpose |
| :--- | :--- | :--- |
| **`sitewatch/products.json`** | Private Backblaze B2 object | List of tracked items, target URLs, baseline prices, and store flags. |
| **`sitewatch/*.csv` History Files** | Private Backblaze B2 objects | Timestamped log of recorded prices, stock flags, and change events. |
| **`sitewatch/audit_state.json`** | Private Backblaze B2 object | Date of the last completed daily audit, preventing repeated same-day audits. |
| **`NTFY_TOPIC`** | GitHub Repository Secret | Secret topic channel for encrypted push notifications. |

### Backblaze B2 setup

The shared private bucket is `GithubRepoSecretRB17`. Sitewatch uses its own folder:

```text
sitewatch/products.json
sitewatch/<product-history>.csv
```

The repository requires these encrypted Actions secrets: `B2_KEY_ID`, `B2_APPLICATION_KEY`, `B2_BUCKET`, `B2_ENDPOINT`, and `NTFY_TOPIC`. The first B2-enabled run can bootstrap from the old Actions cache; subsequent runs read and write B2 directly. The old cache is no longer saved after migration.

To start from a clean history, run **Actions → Track Prices → Run workflow**, enable **Clear all cached CSV price history before this run**, and run it twice. The first run records the current prices and may send initial change/restock alerts; the second run confirms the saved B2 history prevents duplicate alerts.

### Verified B2 migration and tests

- The existing Actions cache was bootstrapped into the private `sitewatch/` B2 folder.
- The first clean-history test restored B2 data, cleared **16** CSV history files, checked **16 products**, accepted **27** ntfy alerts, and uploaded 16 CSV histories back to B2.
- The second test restored the same B2 data, checked 16 products, accepted **0** duplicate alerts, and uploaded the updated 16 CSV histories successfully.
- The old GitHub Actions cache is no longer saved; B2 is now the authoritative persistence layer.

### URL-only additions and daily audit

Run **Actions → Track Prices → Run workflow** and enter a product URL by itself
in **Optional product URL**. Sitewatch discovers the product name, current
selling price, compare-at/list price (when provided), and stock from the page;
JSON product input remains supported for advanced use. The first reliable price
becomes the baseline, so adding a link alone does not cause a false price-rise
alert.

The workflow also runs at **09:05 Asia/Karachi**. The first successful tracker
run at or after 09:00 local time performs the daily metadata audit for every
product. It refreshes the page title, price, sale/list price, and stock, applies
confident corrections to the private product record, and retries the audit on
later runs if any product could not be verified. The completion date is kept in
the private B2 `sitewatch/audit_state.json` object.

Shopify stock checks use read-only product JSON and rendered product-page
availability. Sitewatch no longer posts to `/cart/add.js` to infer stock.

### Stock-aware alert behavior

Sitewatch records `in_stock` with every reliable price observation in each
product's CSV history. The value is represented as `1` (in stock), `0` (out of
stock), or `-1` (unknown). Price-change and all-time-low notifications append
the current availability status. Unknown availability is never silently
treated as available, and an out-of-stock history value of `0` is preserved
when comparing stock transitions.

---

## 🔔 Alert Triggers

- 📉 **Price Drop**: Fired when the active selling price drops below the previous recorded price.
- 📈 **Price Increase**: Fired when a price increases; the alert includes the current availability status.
- 🧾 **Price Alert Availability**: Price drops and all-time lows include `Availability: In stock`, `Availability: Out of stock`, or `Availability: Unknown`.
- 🛒 **Back in Stock (Restock)**: Fired when an out-of-stock item becomes available.
- ⚠️ **Out of Stock**: Fired when an in-stock item sells out.
- 🎉 **All-Time Low**: Distinctive high-priority alert when a price reaches the lowest price ever recorded in its CSV history.

---

## 🛠️ Repository Structure

```
├── .github/workflows/
│   └── track-price.yml    # GitHub Actions workflow runner (hourly/on-demand)
├── price_tracker.py       # Core scraping, comparison, alerting & cache engine
├── products.example.json  # Reference schema template for products
├── requirements.txt       # Python dependencies (Playwright)
└── README.md              # Documentation
```
