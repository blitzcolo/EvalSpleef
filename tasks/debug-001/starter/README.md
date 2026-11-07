# ledgerq · 台账汇总小工具

把一份交易流水 CSV 汇总成月度报表，并导出规范化的 CSV。零第三方依赖，Python 3.11+。

## 快速开始

    python3 -m ledgerq summary --input sample/transactions.csv --output /tmp/summary.json
    python3 -m ledgerq export  --input sample/transactions.csv --output /tmp/transactions.csv
    python3 -m unittest discover -s tests -v

## 输入格式

CSV 表头固定为：

    transaction_id,occurred_at,currency,amount,kind
    T0001,2026-06-03,CNY,120.00,charge
    T0002,2026-06-15,CNY,45.00,refund

- **transaction_id**：全局唯一，重复即数据错误。
- **occurred_at**：YYYY-MM-DD 日期（无时区，按 UTC 日历日理解）。
- **currency**：3 位大写币种代码，如 CNY、USD。
- **amount**：非负十进制金额字符串。
- **kind**：charge（收款）或 refund（退款）。

## 行为契约（对外承诺，改动需同步 README）

1. **金额舍入**：一律 HALF_UP 保留 2 位小数；不得经 float 中转。
2. **日期区间**：[start, end) 左闭右开；结束日当天不计入。
3. **输入校验**：任意一行不合法（日期、金额、币种、kind、列数、重复 ID）必须抛
   LedgerError，异常信息含 line <行号>（行号从 2 开始，与文件物理行一致）；
   **不得静默跳过**。cli 捕获后打印 ERROR: <消息> 到 stderr 并返回退出码 2。
4. **分组**：按 occurred_at 的日历月分组；月份按升序输出。
5. **币种**：不同币种分开汇总，不做汇率换算；totals 以币种代码为键，键按字母升序。
6. **退款**：refund 对金额的贡献为负（signed_amount = -amount）；charge 为正。
7. **导出 CSV**：列固定为 transaction_id,occurred_at,currency,amount,signed_amount,kind；
   按 (occurred_at, transaction_id) 升序；UTF-8 编码、\n 行尾。
8. **库 API（保持兼容）**：ledgerq.load_rows(path) -> list[Row]、
   ledgerq.summarize(rows, start=None, end=None) -> dict、
   ledgerq.export_csv(rows, path) -> None、ledgerq.LedgerError。
   Row 至少含 transaction_id / occurred_at(date) / currency / amount(Decimal) /
   kind / line 字段。

## summary 输出（JSON）

    {
      "months": [
        {
          "month": "2026-06",
          "totals": {"CNY": "75.00", "USD": "10.50"},
          "counts": {"charge": 2, "refund": 1}
        }
      ],
      "rows": 3
    }

rows 是参与汇总的行数（已按日期区间过滤后）。totals 里的数字是 2 位小数字符串，
可能带负号。空输入输出 {"months": [], "rows": 0}。

## CLI

    python3 -m ledgerq summary --input IN.csv --output OUT.json [--start YYYY-MM-DD] [--end YYYY-MM-DD]
    python3 -m ledgerq export  --input IN.csv --output OUT.csv

## 已知反馈

- 财务用 6 月账单核对时，发现退款那几笔的合计金额不对。
- 7 月 1 日拉的报表里混进了 7 月 1 日当天的交易，但导出区间写的是 6 月。
- 有一次输入文件里有一行日期写错，工具什么都没说就出了报表。
- 同一进程里连续生成 6 月和 7 月两份报表时，第二份里出现了第一份的数据。
- 台账里同时有 CNY 和 USD 的月份，总金额被加在了一起。
