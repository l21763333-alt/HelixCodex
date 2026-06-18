from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from mine_badcases import mine_badcases  # noqa: E402


class MineBadcasesTest(unittest.TestCase):
    def test_extreme_badcases_use_ape_and_absolute_error_thresholds(self) -> None:
        rows = [
            {"sku": "A", "date": "2026-01-01", "prediction": 70, "actual": 100},
            {"sku": "B", "date": "2026-01-01", "prediction": 125, "actual": 100},
            {"sku": "C", "date": "2026-01-01", "prediction": 108, "actual": 100},
            {"sku": "D", "date": "2026-01-01", "prediction": 1, "actual": 5},
        ]

        result = mine_badcases(rows, top_n=10)

        extreme = {row["sku"]: row for row in result if row["badcase_type"].startswith("extreme_")}
        self.assertEqual({"A", "B"}, set(extreme))
        self.assertEqual("extreme_underestimate", extreme["A"]["badcase_type"])
        self.assertEqual("extreme_overestimate", extreme["B"]["badcase_type"])

    def test_consecutive_directional_bias_is_grouped_by_key_and_date(self) -> None:
        rows = [
            {"sku": "A", "store": "S1", "date": "2026-01-01", "prediction": 80, "actual": 100},
            {"sku": "A", "store": "S1", "date": "2026-01-02", "prediction": 81, "actual": 100},
            {"sku": "A", "store": "S1", "date": "2026-01-03", "prediction": 82, "actual": 100},
            {"sku": "A", "store": "S1", "date": "2026-01-04", "prediction": 120, "actual": 100},
            {"sku": "B", "store": "S1", "date": "2026-01-01", "prediction": 121, "actual": 100},
            {"sku": "B", "store": "S1", "date": "2026-01-02", "prediction": 122, "actual": 100},
            {"sku": "B", "store": "S1", "date": "2026-01-03", "prediction": 123, "actual": 100},
        ]

        result = mine_badcases(rows, top_n=10)

        streaks = {
            (row["badcase_type"], row["sku"]): row
            for row in result
            if row["badcase_type"].startswith("consecutive_")
        }
        self.assertIn(("consecutive_underestimate", "A"), streaks)
        self.assertIn(("consecutive_overestimate", "B"), streaks)
        self.assertEqual(3, streaks[("consecutive_underestimate", "A")]["streak_length"])
        self.assertEqual("2026-01-01", streaks[("consecutive_underestimate", "A")]["streak_start_date"])
        self.assertEqual("2026-01-03", streaks[("consecutive_underestimate", "A")]["streak_end_date"])


if __name__ == "__main__":
    unittest.main()
