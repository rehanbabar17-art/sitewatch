import unittest

from price_tracker import availability_line, read_stock, stock_label


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


if __name__ == "__main__":
    unittest.main()
