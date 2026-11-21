import unittest

from order import calculate_total


class OrderTest(unittest.TestCase):
    def test_discount(self):
        self.assertAlmostEqual(calculate_total([100, 50], 10), 135)

    def test_discount_edges(self):
        self.assertAlmostEqual(calculate_total([100], 0), 100)
        self.assertAlmostEqual(calculate_total([100], 100), 0)

    def test_invalid_discount(self):
        for discount in (-1, 101):
            with self.assertRaises(ValueError):
                calculate_total([100], discount)


if __name__ == "__main__":
    unittest.main()
