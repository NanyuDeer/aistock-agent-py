"""历史工具展示去重的纯标准库测试。"""
import unittest

from aistock_agent.utils.hot_burst_history import deduplicate_history


class HistoryDedupTest(unittest.TestCase):
    def test_duplicates_do_not_consume_display_slots(self):
        rows = [{"symbol": "300308", "id": 100 - i} for i in range(30)]
        rows.append({"symbol": "300124", "id": 1})
        result = deduplicate_history(rows)[:20]
        self.assertEqual([r["symbol"] for r in result], ["300308", "300124"])
        self.assertEqual(result[0]["id"], 100)

    def test_empty_invalid_and_code_alias(self):
        self.assertEqual(deduplicate_history([]), [])
        self.assertEqual(deduplicate_history([None, "invalid"]), [])
        rows = [{"stock_code": "300308"}, {"symbol": " 300308 "}]
        self.assertEqual(len(deduplicate_history(rows)), 1)
