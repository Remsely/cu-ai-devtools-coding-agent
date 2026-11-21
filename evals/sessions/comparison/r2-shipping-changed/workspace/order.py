def calculate_shipping(subtotal: float, free_from: float = 100, fee: float = 10) -> float:
    return fee if subtotal <= free_from else 0
