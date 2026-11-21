def calculate_line_total(unit_price: float, quantity: int) -> float:
    if unit_price < 0 or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
        raise ValueError("Invalid inputs")
    return unit_price * quantity
