"""簡單的庫存模組：新增、扣除品項數量，以及計算庫存總值。

所有函式都回傳新的 dict，不修改傳入的 stock。
"""


def add_item(stock: dict, name: str, qty: int) -> dict:
    """把 qty 個 name 加進庫存；新品項從 0 開始。

    qty 必須大於 0，否則拋出 ValueError。
    """
    if qty <= 0:
        raise ValueError(f"數量必須大於 0：{qty}")
    new_stock = dict(stock)
    new_stock[name] = new_stock.get(name, 0) + qty
    return new_stock


def remove_item(stock: dict, name: str, qty: int) -> dict:
    """從庫存扣掉 qty 個 name；扣到 0 時把品項移除。

    品項不存在或數量不足時拋出 ValueError。
    """
    if name not in stock:
        raise ValueError(f"品項不存在：{name}")
    if stock[name] < qty:
        raise ValueError(f"數量不足：{name} 只有 {stock[name]}，要扣 {qty}")
    new_stock = dict(stock)
    new_stock[name] -= qty
    if new_stock[name] == 0:
        del new_stock[name]
    return new_stock


def total_value(stock: dict, prices: dict) -> float:
    """計算庫存總值：sum(數量 × 單價)，沒有單價的品項當作 0。"""
    return float(sum(qty * prices.get(name, 0) for name, qty in stock.items()))
