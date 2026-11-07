"""日期解析与区间筛选。"""

from datetime import date

ISO_FORMAT = "%Y-%m-%d"


def parse_date(text: str) -> date:
    """解析 YYYY-MM-DD；非法输入抛 ValueError。"""
    year, month, day = text.split("-")
    return date(int(year), int(month), int(day))


def in_range(value: date, start: date | None, end: date | None) -> bool:
    """判断日期是否落在区间内，区间为左闭右开 [start, end)。"""
    if start is not None and value < start:
        return False
    if end is not None and value <= end:
        return True
    return True
