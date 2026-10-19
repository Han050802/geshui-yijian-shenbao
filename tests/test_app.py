"""Integration control-flow tests, without live UI, threads or RPA input."""
import csv
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from core import Company, StateStore


class AppIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        base_patch = patch.object(app, "BASE", self.base)
        base_patch.start()
        self.addCleanup(base_patch.stop)
        self.companies = [Company("测试甲", "12345678901234567A", "2026-09", 1),
                          Company("测试乙", "12345678901234567B", "2026-09", 1)]
        self.events = []
        # Bypass __init__ so no worker thread is started.
        self.controller = app.Controller.__new__(app.Controller)
        self.controller.check_wechat_mappings = MagicMock()  # Excel preflight has separate integration tests.
        self.controller.emit = lambda kind, value: self.events.append((kind, value))
        self.controller.stop = threading.Event()
        self.controller.settings = {}
        self.controller.web = MagicMock()
        self.controller.web.read_companies.return_value = self.companies
        self.controller.web.mark_success.return_value = True
        self.controller.forward_wechat = MagicMock(return_value="记账群")
        self.controller.ensure_web = MagicMock()
        self.adapter = MagicMock()
        self.adapter.prepare.return_value = "READY"
        self.adapter.reconcile.return_value = "READY"
        self.adapter.filing_status_text.return_value = "待申报"
        self.adapter.calculation_is_ready.return_value = False
        self.adapter.verify.return_value = True
        self.adapter.evidence.return_value = "evidence.png"
        adapter_patch = patch("tax_desktop.TaxDesktop", return_value=self.adapter)
        adapter_patch.start()
        self.addCleanup(adapter_patch.stop)

    def run_controller(self, task="run", writeback=True):
        self.controller.execute(task, {"writeback": writeback})
        return [value for kind, value in self.events if kind == "result"][-1]

    def durable(self, company):
        return StateStore(self.base / "data" / "state.json").get(company)

    def test_prepare_mode_never_submits_or_updates_web(self):
        rows = self.run_controller("prepare")
        self.assertEqual([row["状态"] for row in rows], ["PREPARED", "PREPARED"])
        self.adapter.submit.assert_not_called()
        self.controller.web.mark_success.assert_not_called()
        self.assertIsNone(self.durable(self.companies[0]))
        self.assertEqual(self.adapter.check_summary.call_count, 2)

    def test_tax_success_writeback_false_still_files_the_next_company(self):
        self.controller.web.mark_success.return_value = False
        rows = self.run_controller()
        self.assertEqual([row["状态"] for row in rows], ["WRITEBACK_PENDING", "WRITEBACK_PENDING"])
        self.assertEqual(self.durable(self.companies[0])["state"], "SUCCESS")
        self.assertEqual(self.durable(self.companies[1])["state"], "SUCCESS")
        self.assertEqual(self.adapter.submit.call_count, 2)
        with (self.base / "data" / "last_results.csv").open(encoding="utf-8-sig", newline="") as stream:
            self.assertEqual([row["状态"] for row in csv.DictReader(stream)], ["WRITEBACK_PENDING", "WRITEBACK_PENDING"])

    def test_browser_exception_records_pending_and_continues(self):
        self.controller.web.mark_success.side_effect = RuntimeError("secret-password")
        rows = self.run_controller()
        self.assertEqual([row["状态"] for row in rows], ["WRITEBACK_PENDING", "WRITEBACK_PENDING"])
        self.assertEqual(self.durable(self.companies[0])["state"], "SUCCESS")
        self.assertEqual(self.durable(self.companies[1])["state"], "SUCCESS")
        self.assertEqual(self.adapter.submit.call_count, 2)
        self.assertNotIn("secret-password", str(self.events))

    def test_rerun_only_verifies_live_success(self):
        self.controller.web.read_companies.return_value = self.companies[:1]
        self.controller.web.mark_success.return_value = False
        self.run_controller()
        self.adapter.reset_mock()
        self.adapter.calculation_is_ready.return_value = False
        self.adapter.filing_status_text.return_value = "申报成功,无需缴款"
        self.controller.web.mark_success.return_value = True
        rows = self.run_controller()
        self.assertEqual(rows[0]["状态"], "SUCCESS")
        self.adapter.prepare.assert_not_called()
        self.adapter.check_zero.assert_not_called()
        self.adapter.calculate.assert_not_called()
        self.adapter.check_summary.assert_not_called()
        self.adapter.reconcile.assert_called_once_with(self.companies[0])
        self.adapter.verify.assert_called_once_with(self.companies[0])
        self.adapter.submit.assert_not_called()

    def test_stop_after_current_company_leaves_rest_skipped(self):
        def finish_current(company):
            self.controller.stop.set()
            return True
        self.adapter.verify.side_effect = finish_current
        rows = self.run_controller(writeback=False)
        self.assertEqual([row["状态"] for row in rows], ["SUCCESS", "SKIPPED"])
        self.assertEqual(self.adapter.submit.call_count, 1)

    def test_writeback_disabled_never_updates_workbench(self):
        rows = self.run_controller(writeback=False)
        self.assertEqual([row["状态"] for row in rows], ["SUCCESS", "SUCCESS"])
        self.controller.web.mark_success.assert_not_called()

    def test_test_button_prepares_only_named_company(self):
        target = Company(app.TEST_COMPANY_NAME, "", "2026-09", 1)
        self.controller.web.read_companies.return_value = [self.companies[0], target, self.companies[1]]
        rows = self.run_controller("test_company")
        self.assertEqual([row["公司名称"] for row in rows], [app.TEST_COMPANY_NAME])
        self.assertEqual(rows[0]["状态"], "SUCCESS")
        self.adapter.prepare.assert_called_once_with(target)
        self.adapter.submit.assert_called_once_with(target)
        self.adapter.verify.assert_called_once_with(target)
        shown = [value for kind, value in self.events if kind == "companies"][-1]
        self.assertEqual([company.name for company in shown], [app.TEST_COMPANY_NAME])

    def test_partial_zero_button_runs_three_companies_in_order(self):
        companies = [Company(f"部分公司{index}", "", "2026-09", 1) for index in range(1, 4)]
        self.controller.web.read_companies.return_value = companies
        self.controller.web.select_partial_zero.side_effect = lambda: setattr(self.controller.web, "partial_list", True)
        rows = self.run_controller("test_partial")
        self.controller.web.select_partial_zero.assert_called_once()
        self.assertEqual([row["公司名称"] for row in rows], [company.name for company in companies])
        self.assertEqual([row["状态"] for row in rows], ["SUCCESS", "SUCCESS", "SUCCESS"])
        self.assertEqual(self.adapter.submit.call_count, 3)

    def test_partial_zero_button_runs_however_many_companies_are_listed(self):
        companies = [Company("部分甲", "", "2026-09", 1), Company("部分乙", "", "2026-09", 1)]
        self.controller.web.read_companies.return_value = companies
        self.controller.web.select_partial_zero.side_effect = lambda: setattr(self.controller.web, "partial_list", True)
        rows = self.run_controller("test_partial")
        self.assertEqual([row["公司名称"] for row in rows], ["部分甲", "部分乙"])
        self.assertEqual(self.adapter.submit.call_count, 2)

    def test_full_run_does_not_open_wechat(self):
        self.run_controller("run")
        self.controller.forward_wechat.assert_not_called()
        self.assertEqual(self.controller.web.mark_success.call_count, 2)

    def test_partial_clicks_tax_only_after_wechat_finishes(self):
        order = []
        companies = [Company("部分甲", "", "2026-09", 1)]
        self.controller.web.read_companies.return_value = companies
        self.controller.web.select_partial_zero.side_effect = lambda: setattr(self.controller.web, "partial_list", True)

        def remember_wechat(company):
            order.append(("wechat", company.name))
            return "记账群"

        def remember_tax(company):
            order.append(("tax", company.name))
            return True

        self.controller.forward_wechat.side_effect = remember_wechat
        self.controller.web.mark_success.side_effect = remember_tax
        rows = self.run_controller("test_partial")
        self.assertEqual(order, [("wechat", "部分甲"), ("tax", "部分甲")])
        self.assertEqual(rows[0]["状态"], "SUCCESS")

    def test_partial_skips_tax_when_wechat_does_not_finish(self):
        companies = [Company("部分甲", "", "2026-09", 1), Company("部分乙", "", "2026-09", 1)]
        self.controller.web.read_companies.return_value = companies
        self.controller.web.select_partial_zero.side_effect = lambda: setattr(self.controller.web, "partial_list", True)
        self.controller.forward_wechat.return_value = ""
        rows = self.run_controller("test_partial")
        self.controller.web.mark_success.assert_not_called()
        self.assertEqual([row["状态"] for row in rows], ["WRITEBACK_PENDING", "SKIPPED"])
        self.assertEqual(self.adapter.submit.call_count, 1)
        self.assertEqual(self.durable(companies[0])["state"], "SUCCESS")

    def test_partial_missing_program_does_not_click_tax(self):
        del self.controller.forward_wechat
        companies = [Company("部分甲", "", "2026-09", 1)]
        self.controller.web.read_companies.return_value = companies
        self.controller.web.select_partial_zero.side_effect = lambda: setattr(self.controller.web, "partial_list", True)
        rows = self.run_controller("test_partial")
        self.controller.web.mark_success.assert_not_called()
        self.assertEqual(rows[0]["状态"], "WRITEBACK_PENDING")
        self.assertFalse((self.base / "wechat" / "app.py").exists())

    def test_forward_result_matches_company_and_month(self):
        path = self.base / "result.json"
        path.write_text('{"company":"部分甲","taxLabel":"2026-09","sentTo":"记账群"}', encoding="utf-8")
        self.assertEqual(app.read_forward_result(path, "部分甲", "2026-09"), "记账群")
        self.assertEqual(app.read_forward_result(path, "部分乙", "2026-09"), "")
        self.assertEqual(app.read_forward_result(path, "部分甲", "2026-08"), "")

    def test_partial_filter_does_not_stick_to_the_next_run(self):
        companies = [Company(f"部分公司{index}", "", "2026-09", 1) for index in range(1, 4)]
        self.controller.web.read_companies.return_value = companies
        self.controller.web.select_partial_zero.side_effect = lambda: setattr(self.controller.web, "partial_list", True)
        self.run_controller("test_partial")
        self.controller.web.read_companies.return_value = self.companies
        rows = self.run_controller("run")
        self.assertEqual(len(rows), 2)

    def test_test_button_stops_when_company_missing(self):
        with self.assertRaises(app.ValidationError):
            self.run_controller("test_company")
        self.adapter.prepare.assert_not_called()
        self.adapter.submit.assert_not_called()

    def test_test_button_reconciles_history_without_new_submission(self):
        target = Company(app.TEST_COMPANY_NAME, "", "2026-09", 1)
        self.controller.web.read_companies.return_value = [target]
        StateStore(self.base / "data" / "state.json").set(target, "UNKNOWN")
        self.adapter.filing_status_text.return_value = "待申报"
        rows = self.run_controller("test_company")
        self.assertEqual(rows[0]["状态"], "SUCCESS")
        self.adapter.reconcile.assert_called_once_with(target)
        self.adapter.check_zero.assert_not_called()
        self.adapter.submit.assert_not_called()
        self.adapter.check_summary.assert_not_called()
        self.adapter.verify.assert_called_once_with(target)
        self.assertEqual(self.durable(target)["state"], "SUCCESS")

    def test_prepare_unknown_state_fails_closed(self):
        self.adapter.prepare.return_value = "UNCERTAIN"
        rows = self.run_controller("prepare")
        self.assertEqual([row["状态"] for row in rows], ["FAILED", "SKIPPED"])
        self.adapter.check_zero.assert_not_called()
        self.adapter.submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()

