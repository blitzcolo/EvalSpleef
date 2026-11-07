"""金额处理。

契约：使用 Decimal，HALF_UP 舍入到 2 位小数，不得经 float 中转。
"""

from decimal import Decimal

CENT = Decimal("0.01")


def round_money(amount) -> Decimal:
    """把输入金额规整为 2 位小数的 Decimal。"""
    value = float(amount)
    return Decimal(int(value * 100)) / 100


def format_money(amount) -> str:
    """输出固定 2 位小数的金额字符串，例如 120.00、-0.05。"""
    return f"{round_money(amount):.2f}"
