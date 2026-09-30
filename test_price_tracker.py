import json
import unittest
from datetime import datetime, timezone
from price_tracker import (
    availability_line,
    discounted_compare_price,
    infer_stock_status,
    is_daily_audit_due,
    price_is_plausible,
    product_from_link_or_json,
    read_stock,
    stock_label,
)


class StockNotificationTests(unittest.TestCase):
    def test_read_stock_preserves_out_of_stock_zero(self):
        self.assertEqual(read_stock({"in_stock": "0"}), 0)
        self.assertEqual(read_stock({"in_stock": 0}), 0)

    def test_read_stock_handles_unknown_or_missing_values(self):
        self.assertEqual(read_stock({"in_stock": "-1"}), -1)
        self.assertEqual(read_stock({"in_stock": ""}), -1)
        self.assertEqual(read_stock({}), -1)
        self.assertEqual(read_stock({"in_stock": "unexpected"}), -1)

    def test_stock_labels_are_unambiguous(self):
        self.assertEqual(stock_label(1), "In stock")
        self.assertEqual(stock_label(0), "Out of stock")
        self.assertEqual(stock_label(-1), "Unknown")

    def test_availability_line_is_ready_for_price_alerts(self):
        self.assertEqual(availability_line(1), "\nAvailability: In stock")
        self.assertEqual(availability_line(0), "\nAvailability: Out of stock")
        self.assertEqual(availability_line(-1), "\nAvailability: Unknown")

    def test_sold_out_page_signal_overrides_add_to_cart_from_related_items(self):
        self.assertEqual(infer_stock_status("Out of Dust\nSold Out\nRs.5,990"), 0)

    def test_enabled_add_to_bag_is_positive_stock_evidence(self):
        self.assertEqual(infer_stock_status("Product", form_signals=[{"text": "Add to Bag", "disabled": False}]), 1)

    def test_unknown_page_is_not_assumed_in_stock(self):
        self.assertEqual(infer_stock_status("Product details and price"), -1)


class ProductInputTests(unittest.TestCase):
    def test_url_only_input_creates_a_discoverable_product(self):
        product = product_from_link_or_json("https://saeedghani.pk/products/out-of-dust")
        self.assertEqual(product["url"], "https://saeedghani.pk/products/out-of-dust")
        self.assertEqual(product["name"], "Out Of Dust")
        self.assertTrue(product["history_file"].endswith("_history.csv"))
        self.assertIsNone(product["baseline_price"])
        self.assertTrue(product["discovered_from_url"])

    def test_json_product_input_without_metadata_is_supported(self):
        product = product_from_link_or_json(json.dumps({"url": "https://shop.example/products/item"}))
        self.assertEqual(product["url"], "https://shop.example/products/item")
        self.assertIsNone(product["baseline_price"])

    def test_invalid_product_input_is_rejected(self):
        with self.assertRaises(ValueError):
            product_from_link_or_json("not a URL")


class SalePriceTests(unittest.TestCase):
    def test_reads_daraz_discount_line_as_compare_at_price(self):
        text = "Product\nRs. 999\nRs. 1,999-50%\nQuantity\nDelivery Options"
        self.assertEqual(discounted_compare_price(text, 999), 1999)

    def test_reads_daraz_discount_line_with_spaced_discount(self):
        text = "Derma Roller\nRs. 188\nRs. 400 -53%\nQuantity"
        self.assertEqual(discounted_compare_price(text, 188), 400)

    def test_allows_large_discount_when_list_price_matches_baseline(self):
        self.assertTrue(price_is_plausible(999, 1999, 1999))

    def test_rejects_large_drop_without_list_price_evidence(self):
        self.assertFalse(price_is_plausible(999, 1999, None))


class DailyAuditTests(unittest.TestCase):
    def test_audit_is_due_after_nine_in_pakistan(self):
        at_905_pk = datetime(2026, 9, 30, 4, 5, tzinfo=timezone.utc)
        self.assertTrue(is_daily_audit_due(at_905_pk, {}))

    def test_audit_is_not_due_before_nine_in_pakistan(self):
        at_855_pk = datetime(2026, 9, 30, 3, 55, tzinfo=timezone.utc)
        self.assertFalse(is_daily_audit_due(at_855_pk, {}))

    def test_audit_runs_only_once_after_completion_for_the_day(self):
        at_1015_pk = datetime(2026, 9, 30, 5, 15, tzinfo=timezone.utc)
        self.assertFalse(is_daily_audit_due(at_1015_pk, {"last_completed_local_date": "2026-09-30"}))


if __name__ == "__main__":
    unittest.main()
