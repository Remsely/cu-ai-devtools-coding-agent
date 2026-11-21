import unittest

from order import calculate_shipping


class OrderTest(unittest.TestCase):
    def test_below_threshold(self):
        self.assertEqual(calculate_shipping(99), 10)

    def test_threshold_is_free(self):
        self.assertEqual(calculate_shipping(100), 0)

    def test_custom_threshold(self):
        self.assertEqual(calculate_shipping(50, free_from=50, fee=7), 0)

    def test_negative_values(self):
        for args in ((-1,), (10, -1), (10, 100, -1)):
            with self.assertRaises(ValueError):
                calculate_shipping(*args)


if __name__ == "__main__":
    unittest.main()
