#!/usr/bin/env python3
"""Sitewatch — price, sale-price, and stock tracker.

Product URLs may be supplied directly (or as JSON) through ADD_PRODUCT_JSON.
The daily metadata audit runs once on the first successful run at/after 09:00
Asia/Karachi. Shopify product/cart endpoints are read-only: this script never
adds an item to a customer's cart to infer stock.
"""

import asyncio
import csv
import html
import http.client
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
PRODUCTS_PATH = Path("products.json")
AUDIT_STATE_PATH = Path("audit_state.json")
KARACHI = ZoneInfo("Asia/Karachi")
PROMO_KEYWORDS = r"(?:off|voucher|discount|deal|promo|save|min\.?\s*spend|delivery|shipping|cashback|coupon|banks|free|%|extra)"


class PageMetadata(HTMLParser):
    """Collect Open Graph/product metadata and JSON-LD blocks from page HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.jsonld = []
        self._in_jsonld = False
        self._script_parts = []

    def handle_starttag(self, tag, attrs):
        attrs = {key.lower(): value for key, value in attrs if key}
        if tag.lower() == "meta":
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            if key and attrs.get("content"):
                self.meta[key] = attrs["content"].strip()
        elif tag.lower() == "script" and "ld+json" in (attrs.get("type") or "").lower():
            self._in_jsonld = True
            self._script_parts = []

    def handle_data(self, data):
        if self._in_jsonld:
            self._script_parts.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self._in_jsonld:
            raw = "".join(self._script_parts).strip()
            if raw:
                try:
                    self.jsonld.append(json.loads(raw))
                except (TypeError, ValueError):
                    pass
            self._in_jsonld = False
            self._script_parts = []


def _read_products():
    products = []
    if PRODUCTS_PATH.exists():
        try:
            data = json.loads(PRODUCTS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                products = data
        except Exception as exc:
            print(f"  ⚠ Failed to read cached products.json: {exc}")
    if not products and os.getenv("PRODUCTS_JSON"):
        try:
            data = json.loads(os.getenv("PRODUCTS_JSON", "[]"))
            if isinstance(data, list):
                products = data
        except Exception as exc:
            print(f"  ⚠ Failed to parse PRODUCTS_JSON: {exc}")
    return products


def _safe_history_stem(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    source = f"{parsed.netloc}_{parsed.path.strip('/')}" or parsed.netloc or "product"
    source = urllib.parse.unquote(source)
    stem = re.sub(r"[^a-zA-Z0-9]+", "_", source).strip("_").lower()
    return (stem[:100] or "product")


def product_from_link_or_json(raw: str) -> dict:
    """Normalize a URL-only input or a JSON product object into a product row."""
    raw = raw.strip()
    if not raw:
        raise ValueError("No product URL or JSON was provided")
    if raw.startswith(("https://", "http://")):
        item = {"url": raw}
    else:
        item = json.loads(raw)
        if isinstance(item, str):
            item = {"url": item}
        if not isinstance(item, dict):
            raise ValueError("Product input must be a URL or JSON object")
    url = str(item.get("url", "")).strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Product input must include a valid http(s) URL")
    item["url"] = url
    item.setdefault("name", Path(parsed.path.rstrip("/")).name.replace("-", " ").title() or parsed.netloc)
    item.setdefault("history_file", f"{_safe_history_stem(url)}_history.csv")
    item.setdefault("baseline_price", None)
    item["discovered_from_url"] = not bool(item.get("name") and item.get("baseline_price"))
    return item


PRODUCTS = _read_products()
_new_product_raw = os.getenv("ADD_PRODUCT_JSON", "").strip()
if _new_product_raw:
    try:
        _new_product = product_from_link_or_json(_new_product_raw)
        existing_urls = {p.get("url", "").rstrip("/") for p in PRODUCTS}
        if _new_product["url"].rstrip("/") not in existing_urls:
            PRODUCTS.append(_new_product)
            print("  ✨ Added URL to the tracking queue; product details will be discovered from the page")
        else:
            print("  ℹ Product URL is already tracked")
    except Exception as exc:
        print(f"  ⚠ Could not add product input: {exc}")


def fmt_price(price):
    if price is None or price == "":
        return "N/A"
    return f"Rs. {int(price):,}"


def stock_label(in_stock: int) -> str:
    return {1: "In stock", 0: "Out of stock", -1: "Unknown"}.get(in_stock, "Unknown")


def read_stock(row: dict, default: int = -1) -> int:
    value = row.get("in_stock")
    if value is None or str(value).strip() == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed in (-1, 0, 1) else default


def availability_line(in_stock: int) -> str:
    return f"\nAvailability: {stock_label(in_stock)}"


def discounted_compare_price(text: str, selling_price: int | None) -> int | None:
    """Read a crossed-out/list price from compact sale lines such as Rs. 1,999-50%."""
    if selling_price is None:
        return None
    for line in _normalise_stock_context(text).splitlines():
        line = line.strip()
        if re.match(r"^(?:quantity|delivery options|return\s*&\s*warranty)\b", line, re.I):
            break
        if not re.search(r"(?:-\s*\d{1,3}\s*%|\d{1,3}\s*%\s*(?:off|discount))", line, re.I):
            continue
        amounts = re.findall(r"(?:Rs\.?|PKR)\s*([0-9][0-9,]*(?:\.\d{1,2})?)", line, re.I)
        values = [_price(value) for value in amounts]
        values = [value for value in values if value and value > selling_price]
        if values:
            return max(values)
    return None


def price_is_plausible(price: int | None, baseline: int | None, compare_at: int | None) -> bool:
    """Allow genuine large discounts when the displayed list price confirms them."""
    if price is None or baseline is None:
        return True
    if baseline * 0.5 <= price <= baseline * 1.5:
        return True
    return bool(
        compare_at is not None
        and compare_at > price
        and baseline * 0.75 <= compare_at <= baseline * 1.25
    )


def infer_stock_status(text: str, *, structured=None, form_signals=None) -> int:
    """Infer availability only from explicit product-page evidence."""
    structured = (structured or "").lower()
    if "outofstock" in structured or "soldout" in structured:
        return 0
    if "instock" in structured or "limitedavailability" in structured:
        return 1

    lines = [line.strip().lower() for line in (text or "").splitlines()]
    if any(re.fullmatch(r"(?:sold\s*out|out\s*of\s*stock|currently\s*unavailable)", line) for line in lines):
        return 0

    for signal in (form_signals or []):
        label = str(signal.get("text", "")).strip().lower()
        if re.search(r"sold\s*out|out\s*of\s*stock|unavailable", label):
            return 0
        if signal.get("disabled") and re.search(r"add\s+to\s+(?:cart|bag|basket)", label):
            return 0
    if any(re.search(r"add\s+to\s+(?:cart|bag|basket)", str(s.get("text", "")), re.I) and not s.get("disabled") for s in (form_signals or [])):
        return 1

    # For pages with a simple product-specific Add to Cart label in rendered text.
    if re.search(r"(?:^|\n)\s*add to (?:cart|bag|basket)\s*(?:\n|$)", text or "", re.I):
        return 1
    return -1


def _price(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if amount <= 0 or amount > 100_000_000:
        return None
    return int(round(amount))


def _walk_jsonld(obj):
    if isinstance(obj, list):
        for item in obj:
            yield from _walk_jsonld(item)
    elif isinstance(obj, dict):
        yield obj
        for value in obj.values():
            if isinstance(value, (dict, list)):
                yield from _walk_jsonld(value)


def _jsonld_product_fields(scripts):
    name = price = compare_at = availability = None
    for root in scripts:
        for item in _walk_jsonld(root):
            types = item.get("@type", [])
            types = [types] if isinstance(types, str) else types
            if "Product" not in types:
                continue
            name = name or item.get("name")
            offers = item.get("offers") or []
            if isinstance(offers, dict):
                offers = [offers]
            for offer in offers:
                if not isinstance(offer, dict):
                    continue
                price = price or _price(offer.get("price") or offer.get("lowPrice"))
                availability = availability or offer.get("availability")
                spec = offer.get("priceSpecification") or item.get("priceSpecification") or []
                if isinstance(spec, dict):
                    spec = [spec]
                for entry in spec:
                    if isinstance(entry, dict):
                        candidate = _price(entry.get("price"))
                        kind = str(entry.get("priceType", "") or entry.get("name", "")).lower()
                        if candidate and ("list" in kind or "regular" in kind or "strike" in kind):
                            compare_at = candidate
    return name, price, compare_at, availability


def _meta_and_html_prices(html_text: str, metadata: PageMetadata, jsonld_price=None):
    meta = metadata.meta
    price = None
    compare_at = None
    source = ""
    for key in ("product:price:amount", "og:price:amount"):
        if meta.get(key):
            price = _price(meta[key])
            if price:
                source = "meta"
                break
    if price is None and jsonld_price:
        price, source = jsonld_price, "json-ld"

    if price is None:
        for key in ("product:original_price:amount", "og:price:standard_amount"):
            if meta.get(key):
                compare_at = _price(meta[key])
                break
        for line in html.unescape(re.sub(r"<[^>]+>", " ", html_text)).splitlines():
            line = line.strip()
            if not line or re.search(PROMO_KEYWORDS, line, re.I):
                continue
            found = re.findall(r"(?:Rs\.?|PKR)\s*([0-9][0-9,]*(?:\.\d{1,2})?)", line, re.I)
            values = [_price(value) for value in found]
            values = [value for value in values if value and value >= 20]
            if values:
                price, source = values[0], "rendered-text"
                if len(values) > 1:
                    compare_at = max(values)
                break

    if compare_at is None:
        for key in ("product:original_price:amount", "og:price:standard_amount"):
            if meta.get(key):
                compare_at = _price(meta[key])
                if compare_at:
                    break
    if compare_at is None:
        raw_compare = re.findall(r'"compare_at_price"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)', html_text)
        values = []
        for raw in raw_compare:
            value = _price(raw)
            if value and "." not in raw and int(raw) > 100_000:
                value //= 100
            values.append(value)
        values = [v for v in values if v]
        if values:
            # Shopify API price fields are decimal PKR; legacy embedded integer values may be cents.
            compare_at = min(values)
    return price, compare_at, source


def _title_from_html(html_text: str, metadata: PageMetadata, h1: str = ""):
    title = html.unescape(h1 or "").strip()
    if title:
        return title, "h1"
    for key in ("og:title", "twitter:title"):
        if metadata.meta.get(key):
            return html.unescape(metadata.meta[key]).strip(), "meta"
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.I | re.S)
    if title_match:
        return html.unescape(re.sub(r"\s+", " ", title_match.group(1))).strip(), "title"
    return None, ""


def _normalise_stock_context(text: str) -> str:
    # Product recommendations often contain other sold-out products; don't let
    # those change the availability of the page's primary item.
    stop = re.search(r"\b(?:frequently bought together|you may also like|related products|recommended products)\b", text or "", re.I)
    return (text or "")[:stop.start()] if stop else (text or "")


async def fetch_product(page, product: dict) -> dict:
    """Render a product page and extract title, active/list prices, and stock."""
    last_error = None
    for _ in range(3):
        try:
            await page.goto(product["url"], wait_until="domcontentloaded", timeout=90_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:
                pass
            await page.wait_for_timeout(1_000)
            break
        except Exception as exc:
            last_error = exc
            await page.wait_for_timeout(2_000)
    else:
        raise last_error

    body = await page.inner_text("body")
    try:
        main_body = await page.locator("main").first.inner_text(timeout=2_000)
        if main_body.strip():
            body = main_body
    except Exception:
        pass
    html_text = await page.content()
    metadata = PageMetadata()
    metadata.feed(html_text)
    json_name, json_price, json_compare, structured_availability = _jsonld_product_fields(metadata.jsonld)
    try:
        h1 = await page.locator("h1").first.inner_text(timeout=1_500)
    except Exception:
        h1 = ""
    title, title_source = _title_from_html(html_text, metadata, h1) if not json_name else (json_name, "json-ld")

    price, compare_at, price_source = _meta_and_html_prices(html_text, metadata, json_price)
    if compare_at is None:
        compare_at = json_compare
    if price is None:
        for line in body.splitlines():
            line = line.strip()
            if not line or re.search(PROMO_KEYWORDS, line, re.I):
                continue
            match = re.search(r"(?:Rs\.?|PKR)\s*([0-9][0-9,]*(?:\.\d{1,2})?)", line, re.I)
            if match:
                price = _price(match.group(1))
                if price:
                    price_source = "rendered-text"
                    break
    if compare_at is None:
        compare_at = discounted_compare_price(body, price)

    form_signals = []
    try:
        form_signals = await page.locator(
            'form[action*="/cart/add"] button, form[action*="/cart/add"] input[type="submit"], '
            'button[name="add"], .product-form__submit, [data-add-to-cart]'
        ).evaluate_all("els => els.map(e => ({text:(e.innerText || e.value || e.getAttribute('aria-label') || '').trim(), disabled:!!e.disabled}))")
    except Exception:
        pass
    product_text = _normalise_stock_context(body)
    availability = structured_availability or ""
    if not availability:
        availability_matches = re.findall(r"https?://schema\.org/(?:InStock|OutOfStock|LimitedAvailability)", html_text, re.I)
        availability = availability_matches[0] if availability_matches else ""
    in_stock = infer_stock_status(product_text, structured=availability, form_signals=form_signals)

    # A baseline sanity check guards body-text false positives, but structured
    # product pricing remains authoritative even when a genuine sale is large.
    baseline = _price(product.get("baseline_price"))
    if price is not None and baseline and price_source == "rendered-text" and not price_is_plausible(price, baseline, compare_at):
        price = None

    return {
        "name": title,
        "name_source": title_source,
        "price": price,
        "compare_at_price": compare_at,
        "price_source": price_source,
        "in_stock": in_stock,
        "valid": price is not None,
        "shopify_detected": bool(re.search(r"cdn/shop|Shopify\.theme|shopify-section", html_text, re.I)),
    }


def fetch_shopify_api(product: dict) -> dict:
    """Read Shopify product JSON only; never POST to cart/add.js."""
    parsed = urllib.parse.urlparse(product["url"])
    path_parts = [part for part in parsed.path.split("/") if part]
    if "products" not in path_parts:
        return {"valid": False}
    index = path_parts.index("products")
    if index + 1 >= len(path_parts):
        return {"valid": False}
    handle = path_parts[index + 1]
    api_url = f"{parsed.scheme or 'https'}://{parsed.netloc}/products/{urllib.parse.quote(handle)}.json"
    request = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0 Sitewatch/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        item = payload["product"]
        variants = item.get("variants", [])
        if not variants:
            return {"valid": False}
        available_variants = [variant for variant in variants if variant.get("available") is True]
        chosen = available_variants or variants
        prices = [_price(variant.get("price")) for variant in chosen]
        prices = [value for value in prices if value is not None]
        compare_values = [_price(variant.get("compare_at_price")) for variant in chosen]
        compare_values = [value for value in compare_values if value is not None]
        product_available = item.get("available")
        if product_available is None and any(isinstance(v.get("available"), bool) for v in variants):
            product_available = any(v.get("available") is True for v in variants)
        return {
            "name": item.get("title"),
            "price": min(prices) if prices else None,
            "compare_at_price": min(compare_values) if compare_values else None,
            "in_stock": (1 if product_available else 0) if isinstance(product_available, bool) else None,
            "valid": bool(prices),
            "price_source": "shopify-product-json",
            "shopify_detected": True,
        }
    except Exception:
        return {"valid": False}


def load_history(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def save_history(path: str, history: list[dict]):
    fields = ["timestamp", "price", "compare_at_price", "in_stock", "event"]
    with open(path, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(history)


def save_products():
    PRODUCTS_PATH.write_text(json.dumps(PRODUCTS, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_audit_state():
    try:
        data = json.loads(AUDIT_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def is_daily_audit_due(now=None, state=None) -> bool:
    """Return true once the local calendar day reaches 09:00 Asia/Karachi."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local_now = now.astimezone(KARACHI)
    if (local_now.hour, local_now.minute) < (9, 0):
        return False
    return (state or {}).get("last_completed_local_date") != local_now.date().isoformat()


def _update_product_metadata(product, data, timestamp):
    corrected = []
    live_name = (data.get("name") or "").strip()
    existing_name = str(product.get("name", "")).strip()
    if live_name and data.get("name_source") in ("h1", "json-ld", "meta", "shopify-json"):
        # Keep a descriptive stored name when the page's shorter H1 is already
        # contained in it (e.g., “Brand Product (store.example)”). Otherwise
        # repair an actual name mismatch using the page title.
        if not existing_name or product.get("discovered_from_url") or live_name.casefold() not in existing_name.casefold():
            if live_name != existing_name:
                product["name"] = live_name
                corrected.append("name")
        product["page_title"] = live_name
    price = data.get("price")
    if price is not None:
        if product.get("current_price") != price:
            corrected.append("price")
        product["current_price"] = price
        product["last_checked_at"] = timestamp
    compare_at = data.get("compare_at_price")
    if compare_at is not None:
        if product.get("compare_at_price") != compare_at:
            corrected.append("compare_at_price")
        product["compare_at_price"] = compare_at
        product["sale_price"] = price if price is not None and compare_at > price else None
    elif price is not None:
        product["compare_at_price"] = None
        product["sale_price"] = None
    in_stock = data.get("in_stock", -1)
    if in_stock in (0, 1):
        if product.get("in_stock") != in_stock:
            corrected.append("stock")
        product["in_stock"] = in_stock
    if data.get("shopify_detected"):
        product["shopify"] = True
    product.pop("discovered_from_url", None)
    return corrected


def notify(title, message, tags):
    if not NTFY_TOPIC:
        return
    conn = http.client.HTTPSConnection("ntfy.sh", timeout=30)
    try:
        conn.request("POST", f"/{NTFY_TOPIC}", body=message.encode("utf-8"), headers={
            "Title": title.encode("utf-8"),
            "Tags": tags,
            "Content-Type": "text/plain; charset=utf-8",
        })
        response = conn.getresponse()
        print(f"  🔔 ntfy alert dispatched ({response.status})")
    except Exception as exc:
        print(f"  ⚠ ntfy failed: {exc}")
    finally:
        conn.close()


async def _fetch_data(page, product):
    page_data = await fetch_product(page, product)
    if product.get("shopify") or page_data.get("shopify_detected") or product.get("discovered_from_url"):
        api_data = fetch_shopify_api(product)
        if api_data.get("valid"):
            # Shopify's product JSON is authoritative for title and prices.
            # Availability falls back to the rendered product form/page, never a cart mutation.
            api_data["in_stock"] = api_data.get("in_stock") if api_data.get("in_stock") is not None else page_data.get("in_stock", -1)
            api_data["name_source"] = "shopify-json" if api_data.get("name") else page_data.get("name_source")
            api_data["shopify_detected"] = True
            if api_data.get("compare_at_price") is None:
                api_data["compare_at_price"] = page_data.get("compare_at_price")
            return api_data
    return page_data


async def main():
    now_dt = datetime.now(timezone.utc)
    timestamp = now_dt.strftime("%Y-%m-%d %H:%M UTC")
    state = load_audit_state()
    audit_due = is_daily_audit_due(now_dt, state)
    print(f"[{timestamp}] Checking {len(PRODUCTS)} product(s)")
    if audit_due:
        print("  🔎 Daily metadata audit due (Asia/Karachi, 09:00+)")

    from playwright.async_api import async_playwright

    audit_complete = bool(PRODUCTS)
    completed = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(args=["--no-sandbox", "--disable-setuid-sandbox"])
        page = await browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120 Safari/537.36"
        ))
        summary_lines = []
        output_lines = []

        for index, product in enumerate(PRODUCTS, start=1):
            events = []
            try:
                try:
                    data = await _fetch_data(page, product)
                except Exception:
                    # If a browser-rendered URL fails, still try its read-only
                    # Shopify JSON endpoint when the URL/metadata suggests Shopify.
                    if product.get("shopify") or product.get("discovered_from_url"):
                        data = fetch_shopify_api(product)
                        if not data.get("valid"):
                            raise
                        data["in_stock"] = -1 if data.get("in_stock") is None else data["in_stock"]
                    else:
                        raise
            except Exception as exc:
                print(f"  ❌ Item #{index}: fetch failed ({exc})")
                summary_lines.append(f"- ⚠️ **Item #{index}** — fetch failed")
                audit_complete = False
                continue

            price = data.get("price")
            compare_at = data.get("compare_at_price")
            in_stock = data.get("in_stock", -1)
            valid = bool(data.get("valid")) and price is not None
            product_name = product.get("name") or f"Item #{index}"
            print(f"  • Item #{index}: Checked · {stock_label(in_stock)}")

            history_file = product.get("history_file") or f"{_safe_history_stem(product.get('url','item'))}_history.csv"
            product["history_file"] = history_file
            history = load_history(history_file)
            last_reliable = next((row for row in reversed(history) if str(row.get("price", "")).strip()), None)
            baseline = _price(product.get("baseline_price"))
            last_price = _price(last_reliable.get("price")) if last_reliable else baseline
            last_stock = read_stock(last_reliable or {}, default=-1)

            corrections = _update_product_metadata(product, data, timestamp)
            if audit_due and corrections:
                print(f"    🔧 Audit corrected tracked metadata: {', '.join(sorted(set(corrections)))}")

            if not valid:
                history.append({
                    "timestamp": timestamp,
                    "price": "",
                    "compare_at_price": "",
                    "in_stock": in_stock,
                    "event": "unreliable",
                })
                save_history(history_file, history)
                summary_lines.append(f"- ⚠️ **Item #{index}** — unreliable price read; availability: {stock_label(in_stock)}")
                output_lines.extend([f"product{index}_price=", f"product{index}_compare_at=", f"product{index}_stock=unknown", f"product{index}_changed=false"])
                audit_complete = False
                continue

            # The first reliable observation establishes a baseline without a
            # spurious price-rise or all-time-low alert.
            if last_price is None:
                product["baseline_price"] = price
                last_price = price

            if price != last_price:
                direction = "dropped" if price < last_price else "increased"
                events.append(f"Price {direction}: Rs. {last_price:,} to Rs. {price:,}")
                sale_line = f"\nSale/list price: Rs. {compare_at:,}" if compare_at and compare_at > price else ""
                notify(
                    f"Sitewatch: Price change - {product_name}",
                    f"{product_name}\nRs. {last_price:,} -> Rs. {price:,}{sale_line}{availability_line(in_stock)}",
                    "pricechart,warning",
                )

            if in_stock == 1 and last_stock == 0:
                events.append("Back in stock")
                notify(f"Sitewatch: Restocked - {product_name}", f"{product_name} is back in stock!\nCurrent price: {fmt_price(price)}", "white_check_mark,shopping_cart")
            elif in_stock == 0 and last_stock == 1:
                events.append("Out of stock")
                notify(f"Sitewatch: Out of stock - {product_name}", f"{product_name} is no longer available. Current price: {fmt_price(price)}", "warning")

            seen_prices = [_price(row.get("price")) for row in history if str(row.get("price", "")).strip()]
            seen_prices = [value for value in seen_prices if value is not None]
            if seen_prices and price < min(seen_prices):
                events.append(f"ALL-TIME LOW: Rs. {price:,} (prev low Rs. {min(seen_prices):,})")
                notify(
                    f"Sitewatch: ALL-TIME LOW - {product_name}",
                    f"{product_name}\nNew lowest price: Rs. {price:,}\nPrevious low: Rs. {min(seen_prices):,}{availability_line(in_stock)}",
                    "chart_with_downwards_trend,partying_face",
                )

            event = ""
            if any(item.startswith("Price ") for item in events):
                event += "price_change;"
            if "Back in stock" in events:
                event += "restock;"
            if "Out of stock" in events:
                event += "out_of_stock;"
            if any(item.startswith("ALL-TIME LOW:") for item in events):
                event += "all_time_low;"
            if audit_due and corrections:
                event += "daily_audit_correction;"

            history.append({
                "timestamp": timestamp,
                "price": price,
                "compare_at_price": compare_at or "",
                "in_stock": in_stock,
                "event": event.rstrip(";"),
            })
            save_history(history_file, history)
            if price is not None:
                old_low = _price(product.get("lowest_price"))
                product["lowest_price"] = min(old_low, price) if old_low is not None else price
            reliable_name = data.get("name") and data.get("name_source") in ("h1", "json-ld", "meta", "shopify-json")
            if audit_due and (not reliable_name or in_stock not in (0, 1)):
                audit_complete = False
            elif in_stock not in (0, 1):
                audit_complete = False
            status = "; ".join(events) if events else "No change"
            summary_lines.append(f"- **Item #{index}**: {stock_label(in_stock)} — {status}" + (" · daily audit" if audit_due else ""))
            output_lines.extend([
                f"product{index}_price={price}",
                f"product{index}_compare_at={compare_at or ''}",
                f"product{index}_stock={'in' if in_stock == 1 else 'out' if in_stock == 0 else 'unknown'}",
                f"product{index}_changed={'true' if events else 'false'}",
            ])
            completed.append(index)

        await browser.close()

    save_products()
    if audit_due and audit_complete and len(completed) == len(PRODUCTS):
        local_date = now_dt.astimezone(KARACHI).date().isoformat()
        state = {"last_completed_local_date": local_date, "last_completed_at": timestamp}
        AUDIT_STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        print(f"  ✅ Daily audit completed for {local_date} (Asia/Karachi)")
    elif audit_due:
        print("  ⚠ Daily audit incomplete; it will retry on the next run after 09:00 Asia/Karachi")

    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(f"### {timestamp}\n")
            summary.writelines(line + "\n" for line in summary_lines)
            summary.write("\n")
    output_path = os.getenv("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as output:
            output.write("\n".join(output_lines) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
