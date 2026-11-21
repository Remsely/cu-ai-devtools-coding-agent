def calculate_line_total(unit_price: float, quantity: int) -> float:
    if unit_price < 0:
        raise ValueError("unit_price must be non-negative")
    if isinstance(quantity, bool):
        raise ValueError("quantity must be a non-negative integer")
    if not isinstance(quantity, int) or quantity < 0:
        raise ValueError("quantity must be a non-negative integer")
    return unit_price * quantity
