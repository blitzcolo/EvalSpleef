"""debug-001 隐藏回归测试：只依赖 README 行为契约，不依赖内部实现细节。

运行方式（由 hidden-tests/run.sh 调用）：
    LEDGER_CANDIDATE=<候选工程根目录> python3 -m unittest discover -s hidden-tests/tests
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal

CANDIDATE = os.environ.get("LEDGER_CANDIDATE") or os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "starter")
)
if CANDIDATE not in sys.path:
    sys.path.insert(0, CANDIDATE)

import ledgerq
from ledgerq import LedgerError, export_csv, load_rows, summarize
from ledgerq.money import format_money

FIXTURES = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "fixtures"))


def fixture(name: str) -> str:
    return os.path.join(FIXTURES, name)


def run_cli(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = CANDIDATE
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "ledgerq", *args],
        cwd=CANDIDATE,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class TestMoney(unittest.TestCase):
    """D1：金额必须 HALF_UP，不得经 float 截断。"""

    def test_half_up_positive(self):
        self.assertEqual(format_money(Decimal("2.675")), "2.68")

    def test_half_up_negative_refund(self):
        self.assertEqual(format_money(Decimal("-1.005")), "-1.01")

    def test_summary_uses_half_up(self):
        data = summarize(load_rows(fixture("half_up.csv")))
        self.assertEqual(data["months"][0]["totals"], {"CNY": "2.68"})


class TestDateRange(unittest.TestCase):
    """D2：区间 [start, end) 左闭右开。"""

    def test_end_is_exclusive(self):
        rows = load_rows(fixture("mixed.csv"))
        dates = [row["occurred_at"].isoformat() for row in ledgerq.select_range(
            rows, ledgerq.dates.parse_date("2026-06-01"), ledgerq.dates.parse_date("2026-07-01")
        )]
        self.assertEqual(dates, ["2026-06-01", "2026-06-02", "2026-06-30"])

    def test_start_is_inclusive(self):
        rows = load_rows(fixture("mixed.csv"))
        selected = ledgerq.select_range(
            rows, ledgerq.dates.parse_date("2026-06-30"), None
        )
        self.assertEqual([row["transaction_id"] for row in selected], ["M004", "M005"])

    def test_cli_range_excludes_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "s.json")
            proc = run_cli(
                "summary",
                "--input", fixture("mixed.csv"),
                "--output", out,
                "--start", "2026-06-01",
                "--end", "2026-07-01",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(out, encoding="utf-8") as handle:
                data = json.load(handle)
        self.assertEqual([month["month"] for month in data["months"]], ["2026-06"])


class TestValidation(unittest.TestCase):
    """D3：非法输入必须抛 LedgerError 且带行号，不得静默跳过。"""

    def _assert_raises_with_line(self, name: str, line: int):
        with self.assertRaises(LedgerError) as ctx:
            load_rows(fixture(name))
        self.assertEqual(ctx.exception.line, line, str(ctx.exception))
        self.assertIn(f"line {line}", str(ctx.exception))

    def test_invalid_date(self):
        self._assert_raises_with_line("invalid_date.csv", 4)

    def test_invalid_amount(self):
        self._assert_raises_with_line("invalid_amount.csv", 3)

    def test_unknown_kind(self):
        self._assert_raises_with_line("bad_kind.csv", 3)

    def test_duplicate_transaction_id(self):
        self._assert_raises_with_line("duplicate.csv", 3)

    def test_bad_header(self):
        with self.assertRaises(LedgerError):
            load_rows(fixture("bad_header.csv"))

    def test_cli_invalid_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "s.json")
            proc = run_cli("summary", "--input", fixture("invalid_date.csv"), "--output", out)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("ERROR:", proc.stderr)
        self.assertIn("line 4", proc.stderr)
        self.assertFalse(os.path.exists(out))


class TestAggregation(unittest.TestCase):
    """D4：汇总不得跨调用共享可变状态；D5：币种必须分开汇总。"""

    def test_currency_totals_are_separate(self):
        data = summarize(load_rows(fixture("mixed.csv")))
        june = [month for month in data["months"] if month["month"] == "2026-06"][0]
        self.assertEqual(june["totals"], {"CNY": "-5.00", "USD": "22.50"})
        self.assertEqual(set(june["totals"]), {"CNY", "USD"})

    def test_months_sorted_and_counts(self):
        data = summarize(load_rows(fixture("mixed.csv")))
        self.assertEqual([m["month"] for m in data["months"]], ["2026-05", "2026-06", "2026-07"])
        self.assertEqual(data["months"][0]["counts"], {"charge": 1, "refund": 0})
        self.assertEqual(data["rows"], 5)

    def test_summary_is_deterministic(self):
        rows = load_rows(fixture("mixed.csv"))
        first = summarize(rows)
        second = summarize(rows)
        self.assertEqual(first, second)

    def test_no_state_leak_between_datasets(self):
        summarize(load_rows(fixture("mixed.csv")))
        data = summarize(load_rows(fixture("june_only.csv")))
        self.assertEqual([m["month"] for m in data["months"]], ["2026-06"])
        self.assertEqual(data["months"][0]["totals"], {"CNY": "7.00"})
        self.assertEqual(data["rows"], 1)

    def test_empty_input(self):
        self.assertEqual(summarize([]), {"months": [], "rows": 0})


class TestExport(unittest.TestCase):
    """导出格式与排序。"""

    def test_export_sorted_by_date_then_id(self):
        rows = load_rows(fixture("unsorted.csv"))
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out.csv")
            export_csv(rows, out)
            with open(out, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        self.assertEqual(lines[0], "transaction_id,occurred_at,currency,amount,signed_amount,kind")
        self.assertEqual([line.split(",")[0] for line in lines[1:]], ["U001", "U002", "U003"])
        self.assertEqual(lines[2].split(",")[4], "-3.00")

    def test_export_utf8_newlines(self):
        rows = load_rows(fixture("mixed.csv"))
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out.csv")
            export_csv(rows, out)
            with open(out, "rb") as handle:
                raw = handle.read()
        self.assertNotIn(b"\r\n", raw)
        self.assertTrue(raw.endswith(b"\n"))


class TestEndToEnd(unittest.TestCase):
    """完整 CLI：隐藏数据集上的汇总结果必须精确匹配。"""

    def test_cli_summary_full_dataset(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "s.json")
            proc = run_cli("summary", "--input", fixture("hidden_e2e.csv"), "--output", out)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(out, encoding="utf-8") as handle:
                data = json.load(handle)
        expected = {
            "months": [
                {
                    "month": "2026-05",
                    "totals": {"CNY": "100.00", "USD": "30.00"},
                    "counts": {"charge": 2, "refund": 0},
                },
                {
                    "month": "2026-06",
                    "totals": {"CNY": "11.34", "USD": "0.01"},
                    "counts": {"charge": 2, "refund": 1},
                },
            ],
            "rows": 5,
        }
        self.assertEqual(data, expected)

    def test_cli_summary_with_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "s.json")
            proc = run_cli(
                "summary",
                "--input", fixture("hidden_e2e.csv"),
                "--output", out,
                "--start", "2026-06-01",
                "--end", "2026-07-01",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(out, encoding="utf-8") as handle:
                data = json.load(handle)
        self.assertEqual(data["rows"], 3)
        self.assertEqual([m["month"] for m in data["months"]], ["2026-06"])
        self.assertEqual(data["months"][0]["totals"], {"CNY": "11.34", "USD": "0.01"})

    def test_cli_export_hidden_dataset(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out.csv")
            proc = run_cli("export", "--input", fixture("hidden_e2e.csv"), "--output", out)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(out, encoding="utf-8") as handle:
                body = handle.read().splitlines()
        self.assertEqual(len(body), 6)
        self.assertEqual(body[1].split(",")[0], "E001")
        self.assertEqual(body[-1].split(",")[0], "E005")


if __name__ == "__main__":
    unittest.main()
