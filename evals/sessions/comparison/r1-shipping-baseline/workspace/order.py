def calculate_shipping(subtotal: float, free_from: float = 100, fee: float = 10) -> float:
    if subtotal < 0 or free_from < 0 or fee < 0:
        raise ValueError("Values must be non-negative")
    return fee if subtotal < free_from else 0
