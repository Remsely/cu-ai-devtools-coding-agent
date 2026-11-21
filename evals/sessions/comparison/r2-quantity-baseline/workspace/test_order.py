import unittest

from order import calculate_line_total


class OrderTest(unittest.TestCase):
    def test_multiplication(self):
        self.assertAlmostEqual(calculate_line_total(12.5, 3), 37.5)

    def test_zero_quantity(self):
        self.assertEqual(calculate_line_total(12.5, 0), 0)

    def test_invalid_inputs(self):
        for args in ((-1, 2), (10, -1), (10, 1.5), (10, True)):
            with self.assertRaises(ValueError):
                calculate_line_total(*args)


if __name__ == "__main__":
    unittest.main()
