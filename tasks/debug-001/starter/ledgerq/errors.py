"""统一的业务异常。"""


class LedgerError(Exception):
    """输入数据或运行期的业务错误。

    line 为 CSV 物理行号（表头是第 1 行）；与文件无关的错误为 None。
    """

    def __init__(self, message: str, line: int | None = None) -> None:
        self.line = line
        super().__init__(f"line {line}: {message}" if line is not None else message)
