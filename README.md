# 🛒 Sitewatch — Automated E-Commerce Price & Stock Tracker

**Sitewatch** is a headless, serverless price and inventory tracking engine powered by **GitHub Actions**, **Playwright (Chromium)**, and **ntfy.sh**.

It automatically monitors prices, struck-through MRPs, stock availability, and all-time-low records across multi-vendor e-commerce stores (including Shopify stores, Daraz marketplace, and custom brand websites) while keeping monitored product lists, URLs, and pricing data private.

---

## ⚡ Key Highlights & Architecture

- 🔒 **Private B2 Persistence**: Product configurations (`products.json`) and per-item historical price records (`*.csv`) are stored in the private `sitewatch/` folder in Backblaze B2. No sensitive URLs or private product lists are committed to git.
- 🛍️ **Dual-Engine Scraping**:
  - **Shopify API Engine**: Lightweight direct `.json` endpoint extraction + `cart/add.js` inventory validation for high-speed checks on Shopify stores without triggering bot-detection.
  - **Playwright Headless Browser Engine**: Full headless Chromium rendering with client-side price extraction and stock status detection for dynamic Single Page Applications (SPAs) and marketplaces like Daraz.
- 🔔 **Instant Multi-Device Alerts**: Real-time push notifications via [ntfy.sh](https://ntfy.sh) for price drops, price increases, restocks, out-of-stock events, and all-time lows.
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
| **`NTFY_TOPIC`** | GitHub Repository Secret | Secret topic channel for encrypted push notifications. |

### Backblaze B2 setup

The shared private bucket is `GithubRepoSecretRB17`. Sitewatch uses its own folder:

```text
sitewatch/products.json
sitewatch/<product-history>.csv
```

The repository requires these encrypted Actions secrets: `B2_KEY_ID`, `B2_APPLICATION_KEY`, `B2_BUCKET`, `B2_ENDPOINT`, and `NTFY_TOPIC`. The first B2-enabled run can bootstrap from the old Actions cache; subsequent runs read and write B2 directly. The old cache is no longer saved after migration.

To start from a clean history, run **Actions → Track Prices → Run workflow**, enable **Clear all cached CSV price history before this run**, and run it twice. The first run records the current prices and may send initial change/restock alerts; the second run confirms the saved B2 history prevents duplicate alerts.

---

## 🔔 Alert Triggers

- 📉 **Price Drop**: Fired when the active selling price drops below the previous recorded price.
- 📈 **Price Increase**: Fired when a price increases.
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
