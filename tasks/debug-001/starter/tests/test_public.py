"""现有可见测试：覆盖主流程，不覆盖历史反馈中的边界场景。"""

import os
import sys
import tempfile
import unittest

START = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, START)

from ledgerq import export_csv, load_rows, summarize
from ledgerq.money import format_money

SAMPLE = os.path.join(START, "sample", "transactions.csv")


class PublicTests(unittest.TestCase):
    def test_cli_export(self):
        from ledgerq.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out.csv")
            code = main(["export", "--input", SAMPLE, "--output", out])
            self.assertEqual(code, 0)
            with open(out, encoding="utf-8") as handle:
                head = handle.readline().strip()
        self.assertEqual(head, "transaction_id,occurred_at,currency,amount,signed_amount,kind")

    def test_export_roundtrip(self):
        rows = load_rows(SAMPLE)
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out.csv")
            export_csv(rows, out)
            with open(out, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        self.assertEqual(lines[0], "transaction_id,occurred_at,currency,amount,signed_amount,kind")
        self.assertEqual(len(lines), 5)

    def test_load_sample(self):
        rows = load_rows(SAMPLE)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["transaction_id"], "T0001")
        self.assertEqual(format_money(rows[0]["amount"]), "120.00")

    def test_summary_single_currency(self):
        data = summarize(load_rows(SAMPLE))
        self.assertEqual(data["rows"], 4)
        self.assertEqual(data["months"][0]["month"], "2026-06")
        self.assertEqual(data["months"][0]["counts"], {"charge": 3, "refund": 1})


if __name__ == "__main__":
    unittest.main()
