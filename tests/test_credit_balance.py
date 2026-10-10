from __future__ import annotations

import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.i18n import t
from quota import check_codex_quota as quota
from quota.codex_quota_sampler import compact_account
from ui.panels import account_lines, account_summary_body, merged_account_reset_body
from ui.terminal_text import strip_ansi


class CreditBalanceTests(unittest.TestCase):
    def test_balance_survives_query_snapshot_and_rendering(self):
        from rich.console import Console

        for raw, expected in (
            ("1234.500", "1,234.5"), (0.125, "0.125"), (12, "12"),
            ("-2", "-2"), ("0", None), (0, None), (None, None),
            ("", None), ("invalid", None), ("NaN", None),
            ("Infinity", None), (True, None),
        ):
            with self.subTest(balance=raw):
                def response(url, _token):
                    if url == quota.USAGE_URL:
                        return {"credits": {"has_credits": True, "balance": raw, "unlimited": False}}
                    return {"credits": [], "available_count": 0}

                with patch.object(quota, "load_auth_credentials", return_value=("test", "test")), patch.object(
                    quota, "get_json", side_effect=response
                ):
                    report = quota.collect_quota(Path("unused"), "UTC")
                report["index"] = "openai-1"
                self.assertEqual(quota.credit_balance_text(report), expected)
                compact = compact_account(report, 1_000)
                self.assertEqual(compact["credits"], report["credits"])

                output = StringIO()
                with redirect_stdout(output):
                    quota.print_text(report)
                rich_output = StringIO()
                Console(file=rich_output, width=60).print(quota.make_account_panel(report, 60))
                renders = [output.getvalue(), rich_output.getvalue()]
                for account in (report, compact):
                    renders.extend([
                        "\n".join(account_summary_body(account, 58)),
                        "\n".join(account_lines(account, [], 60, 40, "all", "openai-1", "line")),
                        "\n".join(merged_account_reset_body([account], "openai-1", 58)),
                    ])
                for rendered in renders:
                    rendered = strip_ansi(rendered)
                    self.assertEqual(t("quota.credit_balance") in rendered, expected is not None)
                    if expected is not None:
                        self.assertIn(expected, rendered)

    def test_missing_credits_and_zero_refresh_do_not_retain_balance(self):
        from ui.history import inherit_snapshot_account_identity

        for usage in ({}, {"credits": None}, {"credits": []}):
            report = quota.normalize_quota_report(usage, {}, "UTC")
            self.assertIsNone(quota.credit_balance_text(report))
        previous = {"a": [{"i": "openai-1", "credits": {"balance": "42"}}]}
        for credits in (None, {"balance": "0"}):
            record = {"a": [{"i": "openai-1", "credits": credits}]}
            refreshed = inherit_snapshot_account_identity(record, previous)
            self.assertIsNone(quota.credit_balance_text(refreshed["a"][0]))


if __name__ == "__main__":
    unittest.main()
