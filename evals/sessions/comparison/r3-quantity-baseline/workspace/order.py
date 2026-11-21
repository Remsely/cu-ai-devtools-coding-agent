def calculate_line_total(unit_price: float, quantity: int) -> float:
    if unit_price < 0:
        raise ValueError("unit_price must be non-negative")
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise ValueError("quantity must be a non-negative integer")
    if quantity < 0:
        raise ValueError("quantity must be non-negative")
    return unit_price * quantity
