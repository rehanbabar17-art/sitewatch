#!/usr/bin/env python3
"""
Sitewatch — Price & Stock Tracker
Uses Playwright + headless Chromium to render product pages and extract
the actual selling price and in-stock status. For Shopify-based sites
(products marked with "shopify": true) it falls back to the lightweight
.json API + cart/add.js availability check to avoid bot-detection blocks.
Logs changes to CSV and pushes ntfy alerts for price changes and restocks.
Products are supplied via the PRODUCTS_JSON env var (JSON array) and the ntfy
topic via NTFY_TOPIC, so no personal data lives in this repository.
"""

import csv, os, re, asyncio, http.client, urllib.request, urllib.parse, urllib.error, json as _json
from datetime import datetime, timezone

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")

# Lines in rendered body text that mention promo/voucher/charge info must be
# skipped when falling back to body-text price extraction, to avoid picking up
# a discounted-off amount or delivery charge instead of the selling price.
PROMO_KEYWORDS = r"(?:off|voucher|discount|deal|promo|save|min\.?\s*spend|delivery|shipping|cashback|coupon|banks|free|%|off flat|extra)"

PRODUCTS = _json.loads(os.getenv("PRODUCTS_JSON", "[]"))




def fmt_price(price):
    """Format a price for display, handling None safely."""
    if price is None:
        return "N/A"
    return f"Rs. {price:,}"

def fetch_shopify_api(product: dict) -> dict:
    """Fetch price + stock via Shopify .json + cart/add.js (no rendering).
    Works from CI/datacenter IPs where the store's HTML page is blocked."""
    base_url = product["url"].rstrip("/")
    handle = base_url.split("/")[-1]
    scheme = urllib.parse.urlparse(base_url).scheme or "https"
    netloc = urllib.parse.urlparse(base_url).netloc
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    # 1) Price from the product .json endpoint
    prod_url = f"{scheme}://{netloc}/products/{handle}.json"
    try:
        req = urllib.request.Request(prod_url, headers=headers)
        data = _json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
        variants = data["product"]["variants"]
    except Exception:
        return {"price": None, "in_stock": -1, "valid": False}

    prices = [float(v["price"]) for v in variants if v.get("price")]
    price = int(min(prices)) if prices else None

    # 2) Stock: attempt a cart/add.js on the first variant id. Success => in
    #    stock; failure (~=unavailable) => out of stock.
    in_stock = -1
    if variants:
        vid = variants[0]["id"]
        add_url = f"{scheme}://{netloc}/cart/add.js"
        body = urllib.parse.urlencode({"id": vid, "quantity": 1}).encode()
        try:
            areq = urllib.request.Request(add_url, data=body, headers={
                "User-Agent": headers["User-Agent"],
                "Content-Type": "application/x-www-form-urlencoded",
            })
            urllib.request.urlopen(areq, timeout=30)
            in_stock = 1
        except urllib.error.HTTPError as e:
            in_stock = 0 if e.code in (400, 404, 422) else -1
        except Exception:
            in_stock = -1

    valid = price is not None
    return {"price": price, "in_stock": in_stock, "valid": valid}


async def fetch_product(page, product: dict) -> dict:
    """Render one product page and return current price + stock status."""
    # Retry a couple of times — Daraz can be slow to respond
    last_err = None
    for attempt in range(3):
        try:
            await page.goto(product["url"], wait_until="load", timeout=90_000)
            try:
                await page.wait_for_selector(
                    "text=/(Rs)|(PKR)\\s*[0-9,]+/", timeout=25_000,
                )
            except Exception:
                pass
            await page.wait_for_timeout(5_000)
            break
        except Exception as e:
            last_err = e
            await page.wait_for_timeout(5_000)
    else:
        raise last_err

    body = await page.inner_text("body")
    html = await page.content()

    # ---- Price extraction ----
    # 1) Shopify embedded JSON (works even if the page body isn't fully
    #    rendered in CI). Two Shopify price formats exist:
    #    - New:  "price":{"amount":23600.0,"currencyCode":"PKR"}  (direct PKR)
    #    - Old:  "price":2360000, "compare_at_price":2950000      (cents)
    #    We take the lowest available-variant price as the selling price,
    #    and reject clearly-wrong low values (bot/placeholder pages).
    price = None
    # --- New Shopify format (amount in PKR) ---
    new_prices = re.findall(
        r'"price":\{"amount":([0-9.]+),"currencyCode":"PKR"\}', html
    )
    if new_prices:
        vals = sorted(set(float(p) for p in new_prices))
        # the lowest non-trivial price is the selling price
        price_candidates = [int(v) for v in vals if v >= 500]
        if price_candidates:
            price = price_candidates[0]
    # --- Fallback: old Shopify cents format ---
    if price is None:
        cents_list = [int(m) for m in re.findall(r'"price":([0-9]{5,})', html) if int(m) > 50000]
        if cents_list:
            price = min(cents_list) // 100

    # 2) Rendered-body fallback: collect selling-price values.
    #    On Daraz, the first standalone "Rs. X" line is the selling price.
    #    We take the first plausible price (>= 500 PKR), NOT the minimum,
    #    to avoid picking up voucher text, delivery charges, or "Min. spend" lines.
    if price is None:
        for line in body.split("\n"):
            line = line.strip()
            if not line or re.search(PROMO_KEYWORDS, line, re.IGNORECASE):
                continue
            for m in re.finditer(r'(?:Rs\.?|PKR)\s*\s?([0-9,]+)', line):
                val = int(m.group(1).replace(",", ""))
                if val >= 500:
                    price = val
                    break
            if price is not None:
                break

    # 3) Baseline sanity guard: reject prices that deviate wildly from the
    #    product's known baseline (>50%). Realistic e-commerce discounts
    #    rarely exceed ~50%; a value far outside that window is almost always
    #    a wrong pickup (voucher amount, alternate SKU, or a bot page).
    base = product.get("baseline_price")
    if price is not None and base and not (base * 0.5 <= price <= base * 1.5):
        price = None  # mark unreliable so no change/restock alert fires

    # ---- Stock ----
    # On Daraz: stock status text appears as standalone lines like
    # "Out of stock" or "Add to cart" in the product detail area.
    # We look for these only in the body (rendered text), not the full
    # HTML+body concatenation (which would catch unrelated JSON content).
    body_lower = body.lower()
    out_of_stock = bool(re.search(
        r'(?:^|\n)\s*(?:out of stock|sold out)\s*(?:\n|$)',
        body_lower, re.MULTILINE,
    ))
    add_to_cart = bool(re.search(r'(?:add to cart|add to basket)', body_lower))
    if out_of_stock:
        stock_status = 0
    elif add_to_cart:
        stock_status = 1
    else:
        # No explicit stock marker: do NOT assume in-stock. A missing marker
        # usually means the page didn't fully render or is a bot-detection page,
        # so treat it as unknown to avoid false restock/out-of-stock alerts.
        stock_status = -1

    valid = price is not None

    return {"price": price, "in_stock": stock_status, "valid": valid}


def load_history(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def save_history(path: str, history: list[dict]):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["timestamp", "price", "in_stock", "event"]
        )
        writer.writeheader()
        writer.writerows(history)


def notify(title: str, message: str, tags: str):
    """Push a UTF-8 notification to ntfy.sh."""
    conn = http.client.HTTPSConnection("ntfy.sh", timeout=30)
    try:
        conn.request(
            "POST",
            f"/{NTFY_TOPIC}",
            body=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Tags": tags,
                "Content-Type": "text/plain; charset=utf-8",
            },
        )
        resp = conn.getresponse()
        print(f"  🔔 ntfy sent ({resp.status}): {title}")
    except Exception as e:
        print(f"  ⚠ ntfy failed: {e}")
    finally:
        conn.close()


async def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"[{now}] Checking {len(PRODUCTS)} product(s)")

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        page = await browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
            )
        )

        summary_lines = []
        output_lines = []
        index = 0
        for product in PRODUCTS:
            index += 1
            events = []

            try:
                if product.get("shopify"):
                    # Shopify .json API + cart/add.js (works from CI IPs)
                    data = fetch_shopify_api(product)
                else:
                    data = await fetch_product(page, product)
            except Exception as e:
                print(f"  ❌ {product['name']}: fetch failed ({e})")
                summary_lines.append(f"- ⚠️ **{product['name']}** — fetch failed")
                continue

            price = data["price"]
            in_stock = data["in_stock"]
            valid = data["valid"]
            stock_label = {1: "In stock", 0: "Out of stock", -1: "Unknown"}[in_stock]
            print(f"  • {product['name']}: "
                  f"{fmt_price(price)} · {stock_label}")

            history = load_history(product["history_file"])
            last_reliable = None
            for row in reversed(history):
                if row.get("price", "").strip():
                    last_reliable = row
                    break
            last = last_reliable if last_reliable else {}
            last_price = int(last.get("price") or product["baseline_price"])
            last_stock = int(last.get("in_stock") or 1)

            event = ""

            # If the read is unreliable, skip change logic entirely (no alerts).
            if not valid:
                history.append({
                    "timestamp": now,
                    "price": "",
                    "in_stock": in_stock,
                    "event": "unreliable",
                })
                save_history(product["history_file"], history)
                summary_lines.append(
                    f"- ⚠️ **{product['name']}** — unreliable read (page did not render); "
                    f"no alert fired."
                )
                output_lines.append(f"product{index}_price=")
                output_lines.append(f"product{index}_stock=unknown")
                output_lines.append(f"product{index}_changed=false")
                print(f"    ⚠ unreliable read — skipped change detection")
                continue

            # Price change (any direction)
            if price is not None and price != last_price:
                direction = "dropped" if price < last_price else "increased"
                events.append(f"Price {direction}: Rs. {last_price:,} to Rs. {price:,}")
                event += "price_change;"
                notify(
                    f"Sitewatch: Price change - {product['name']}",
                    f"{product['name']}\nRs. {last_price:,} -> Rs. {price:,}",
                    "pricechart,warning",
                )

            # Stock change: restock (was out, now in) — always alert
            if in_stock == 1 and last_stock == 0:
                events.append("Back in stock")
                event += "restock;"
                notify(
                    f"Sitewatch: Restocked - {product['name']}",
                    f"{product['name']} is back in stock!\nCurrent price: {fmt_price(price)}",
                    "white_check_mark,shopping_cart",
                )
            elif in_stock == 0 and last_stock == 1:
                events.append("Out of stock")
                event += "out_of_stock;"
                notify(
                    f"Sitewatch: Out of stock - {product['name']}",
                    f"{product['name']} is no longer available.",
                    "warning",
                )

            # All-time-low detection (all products): distinctive alert when
            # the current price is the lowest ever recorded for this product.
            if price is not None:
                seen_prices = [int(r["price"]) for r in history if r.get("price", "").strip()]
                seen_min = min(seen_prices) if seen_prices else product["baseline_price"]
                if price < seen_min:
                    events.append(f"ALL-TIME LOW: Rs. {price:,} (prev low Rs. {seen_min:,})")
                    event += "all_time_low;"
                    notify(
                        f"Sitewatch: ALL-TIME LOW - {product['name']}",
                        f"{product['name']}\nNew lowest price: Rs. {price:,}\nPrevious low: Rs. {seen_min:,}",
                        "chart_with_downwards_trend,partying_face",
                    )

            status = "; ".join(events) if events else "No change"
            if events:
                print(f"    {status}")

            history.append({
                "timestamp": now,
                "price": price if price is not None else "",
                "in_stock": in_stock,
                "event": event.rstrip(";"),
            })
            save_history(product["history_file"], history)

            summary_lines.append(
                f"- **{product['name']}**: {fmt_price(price)} · "
                f"{'In stock' if in_stock else 'Out of stock'} — {status}"
            )
            output_lines.append(
                f"product{index}_price={price if price is not None else ''}"
            )
            output_lines.append(
                f"product{index}_stock={'in' if in_stock else 'out'}"
            )
            output_lines.append(
                f"product{index}_changed={'true' if events else 'false'}"
            )

        await browser.close()

        summary = os.getenv("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a") as f:
                f.write(f"### {now}\n")
                f.writelines(line + "\n" for line in summary_lines)
                f.write("\n")

        output_file = os.getenv("GITHUB_OUTPUT")
        if output_file:
            with open(output_file, "a") as f:
                f.write("\n".join(output_lines) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
