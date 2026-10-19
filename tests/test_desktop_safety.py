"""Offline regression tests for scope, OCR and feedback safety boundaries."""
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tax_desktop import DesktopError, TaxDesktop, ocr_items, require_zero


class DesktopSafetyTests(unittest.TestCase):
    def setUp(self):
        self.company = SimpleNamespace(name="目标公司", month="2026-09",
                                       expected_people=2, allow_missing_insurance=False,
                                       generate_zero=True)

    def adapter(self):
        adapter = TaxDesktop.__new__(TaxDesktop)
        adapter.log = Mock()
        adapter.cancelled = Mock()
        adapter.settings = {"feedback_timeout": 2}
        adapter.find_import_wizard = Mock(return_value=None)
        return adapter

    def boxes(self, prefix="1", confidence=.84):
        return [
            ([[0, 0], [10, 0], [10, 20], [0, 20]], prefix, confidence),
            ([[10, 0], [60, 0], [60, 20], [10, 20]], "0.00", .99),
        ]

    def test_low_confidence_leading_digit_cannot_become_zero(self):
        with self.assertRaisesRegex(DesktopError, "低置信度"):
            TaxDesktop.text(ocr_items(self.boxes(), 100, 100, strict=True))

    def test_uncertain_person_count_and_nan_confidence_stop(self):
        for confidence in (.84, float("nan")):
            with self.subTest(confidence=confidence), self.assertRaises(DesktopError):
                ocr_items(self.boxes("2人", confidence), 100, 100, strict=True)

    def test_clear_zero_is_still_accepted(self):
        result = ocr_items(self.boxes()[1:], 100, 100, strict=True)
        require_zero(TaxDesktop.text(result))

    def test_navigation_can_still_skip_uncertain_text(self):
        result = ocr_items(self.boxes(), 100, 100)
        self.assertEqual(TaxDesktop.text(result), "0.00")

    def test_critical_read_requests_strict_ocr(self):
        adapter = self.adapter()
        adapter.snap = Mock(return_value=[{"text": "0.00"}])
        region = (.3, .4, .5, .6)
        self.assertEqual(adapter.read_critical(region), "0.00")
        adapter.snap.assert_called_once_with(region, strict=True)

    def test_identity_mismatch_does_not_silently_switch_mid_operation(self):
        adapter = self.adapter()
        adapter.footer_matches = Mock(return_value=False)
        adapter.login_company = Mock()
        with self.assertRaises(DesktopError):
            adapter.identity(self.company)
        adapter.login_company.assert_not_called()

    def test_prepare_verifies_identity_before_any_business_dialog_action(self):
        adapter = self.adapter()
        adapter.attach = Mock()
        adapter.login_company = Mock()
        adapter.identity = Mock(side_effect=DesktopError("wrong company"))
        adapter.month = Mock()
        adapter.dismiss_zero_income_prompt = Mock()
        adapter.wait_until_idle = Mock()
        adapter.copy_previous_month = Mock()
        adapter.confirm_import_dialog = Mock()
        adapter.return_to_step_list = Mock()
        with self.assertRaisesRegex(DesktopError, "wrong company"):
            adapter.prepare(self.company)
        for method in (adapter.month, adapter.dismiss_zero_income_prompt,
                       adapter.wait_until_idle, adapter.copy_previous_month,
                       adapter.confirm_import_dialog, adapter.return_to_step_list):
            method.assert_not_called()

    def test_prepare_wrong_month_stops_before_navigation_or_import(self):
        adapter = self.adapter()
        adapter.attach = Mock()
        adapter.login_company = Mock()
        adapter.identity = Mock()
        adapter.month = Mock(side_effect=DesktopError("wrong month"))
        adapter.return_to_step_list = Mock()
        adapter.copy_previous_month = Mock()
        adapter.click_text = Mock()
        with self.assertRaisesRegex(DesktopError, "wrong month"):
            adapter.prepare(self.company)
        adapter.return_to_step_list.assert_not_called()
        adapter.copy_previous_month.assert_not_called()
        adapter.click_text.assert_not_called()

    def test_prepare_and_reconcile_stop_at_existing_import_wizard(self):
        for method in ("prepare", "reconcile"):
            with self.subTest(method=method):
                adapter = self.adapter()
                adapter.attach = Mock()
                adapter.find_import_wizard = Mock(return_value=object())
                adapter.login_company = Mock()
                adapter.copy_previous_month = Mock()
                adapter.confirm_import_dialog = Mock()
                with self.assertRaisesRegex(DesktopError, "未完成的工资导入向导"):
                    getattr(adapter, method)(self.company)
                adapter.login_company.assert_not_called()
                adapter.copy_previous_month.assert_not_called()
                adapter.confirm_import_dialog.assert_not_called()

    def test_generic_dialog_cleanup_never_imports(self):
        adapter = self.adapter()
        adapter.root = SimpleNamespace(class_name=lambda: "Tfrm_MainFrame")
        adapter.close_copy_result = Mock(return_value=False)
        adapter.find_zero_income_dialog = Mock(return_value=None)
        adapter.find_import_wizard = Mock(return_value=object())
        adapter.confirm_import_dialog = Mock(return_value=True)
        adapter.copy_previous_month = Mock()
        adapter.dismiss_zero_income_prompt()
        adapter.confirm_import_dialog.assert_not_called()
        adapter.copy_previous_month.assert_not_called()
        adapter.close_copy_result.assert_called_once_with(company=None, allow_calculation=False)

    def test_reconcile_only_opens_existing_results(self):
        adapter = self.adapter()
        adapter.attach = Mock()
        adapter.login_company = Mock()
        adapter.identity = Mock()
        adapter.month = Mock()
        adapter.return_to_step_list = Mock()
        adapter.click_text = Mock()
        adapter.wait_ready = Mock(return_value="READY")
        adapter.copy_previous_month = Mock()
        adapter.calculate = Mock()
        adapter.submit = Mock()
        adapter.check_zero = Mock()
        self.assertEqual(adapter.reconcile(self.company), "READY")
        self.assertEqual([call.args[0] for call in adapter.click_text.call_args_list],
                         ["综合所得申报", "申报表报送"])
        adapter.copy_previous_month.assert_not_called()
        adapter.calculate.assert_not_called()
        adapter.submit.assert_not_called()
        adapter.check_zero.assert_not_called()

    def test_zero_check_rejects_uncertain_people_before_import(self):
        adapter = self.adapter()
        adapter.attach = Mock()
        adapter.guard = Mock()
        adapter.click_text = Mock()
        adapter.wait_ready = Mock(return_value="IMPORT")
        adapter.read_critical = Mock(side_effect=DesktopError("低置信度"))
        adapter.confirm_import_dialog = Mock()
        adapter.copy_previous_month = Mock()
        with self.assertRaisesRegex(DesktopError, "低置信度"):
            adapter.check_zero(self.company)
        adapter.confirm_import_dialog.assert_not_called()
        adapter.copy_previous_month.assert_not_called()

    def test_empty_table_import_is_scoped_to_verified_company(self):
        adapter = self.adapter()
        events = []
        adapter.attach = Mock()
        adapter.guard = Mock(side_effect=lambda company: events.append(("guard", company)))
        adapter.click_text = Mock()
        adapter.wait_ready = Mock(return_value="READY")
        adapter.read_critical = Mock(side_effect=[
            "申报总人数0人收入总额0.00元免税收入合计0.00元",
            "申报总人数2人收入总额0.00元免税收入合计0.00元",
        ])
        adapter.confirm_import_dialog = Mock(side_effect=lambda company: events.append(("confirm", company)) or True)
        adapter.copy_previous_month = Mock(side_effect=lambda company: events.append(("copy", company)) or True)
        adapter.check_zero(self.company)
        self.assertEqual([name for name, _ in events], ["guard", "guard", "guard", "confirm", "copy", "guard"])
        self.assertTrue(all(company is self.company for _, company in events))

    def test_empty_table_import_disabled_never_confirms_or_copies(self):
        adapter = self.adapter()
        self.company.generate_zero = False
        adapter.attach = Mock()
        adapter.guard = Mock()
        adapter.click_text = Mock()
        adapter.wait_ready = Mock(return_value="IMPORT")
        adapter.read_critical = Mock(return_value="申报总人数0人收入总额0.00元免税收入合计0.00元")
        adapter.confirm_import_dialog = Mock()
        adapter.copy_previous_month = Mock()
        with self.assertRaisesRegex(DesktopError, "工资表为空，未允许复制上月数据"):
            adapter.check_zero(self.company)
        adapter.confirm_import_dialog.assert_not_called()
        adapter.copy_previous_month.assert_not_called()

    def test_submit_critical_amount_failure_prevents_send(self):
        adapter = self.adapter()
        adapter.wait_until_idle = Mock()
        adapter.guard = Mock()
        adapter.read = Mock(return_value="本期收入")
        adapter.read_critical = Mock(side_effect=DesktopError("低置信度"))
        adapter.click_text = Mock()
        with self.assertRaisesRegex(DesktopError, "低置信度"):
            adapter.submit(self.company)
        adapter.click_text.assert_not_called()

    def test_conflicting_payment_feedback_fails_even_with_zero_summary(self):
        adapter = self.adapter()
        adapter.wait_ready = Mock()
        adapter.guard = Mock()
        adapter.read_critical = Mock(side_effect=["申报成功，无需缴款", "申报成功，需缴款100.00元"])
        adapter.validate_summary = Mock()
        with self.assertRaisesRegex(DesktopError, "缴款"):
            adapter.verify(self.company)
        adapter.validate_summary.assert_not_called()

    def test_implicit_calculation_requires_explicit_company_scope(self):
        class Array:
            def __getitem__(self, key):
                return self
            def copy(self):
                return self
        class Image:
            size = (100, 100)
            def crop(self, region):
                return self
        numpy = ModuleType("numpy")
        numpy.array = lambda image: Array()
        box = [[0, 0], [10, 0], [10, 20], [0, 20]]
        buttons = [(box, "继续算税", .99),
                   ([[80, 0], [90, 0], [90, 20], [80, 20]], "暂不算税", .99)]
        for company, allowed, guard_error in (
                (None, False, False),
                (self.company, True, False),
                (SimpleNamespace(allow_missing_insurance=True), False, False),
                (SimpleNamespace(allow_missing_insurance=True), True, True)):
            with self.subTest(company=company, allowed=allowed, guard_error=guard_error):
                adapter = self.adapter()
                dialog = Mock()
                adapter.find_named_popup = Mock(return_value=dialog)
                adapter.capture_window = Mock(return_value=Image())
                adapter.ocr = Mock(side_effect=[(buttons, None)] * 5 + [
                    ([(box, "未填写三险一金", .99)], None)])
                adapter.guard = Mock(side_effect=DesktopError("wrong company") if guard_error else None)
                adapter.focus_handle = Mock()
                with patch.dict(sys.modules, {"numpy": numpy}), patch("tax_desktop.time.sleep"):
                    with self.assertRaises(DesktopError):
                        adapter.close_copy_result(company=company, allow_calculation=allowed)
                dialog.click_input.assert_not_called()

    def test_clear_success_still_requires_critical_summary_validation(self):
        adapter = self.adapter()
        adapter.wait_ready = Mock()
        adapter.guard = Mock()
        adapter.read_critical = Mock(side_effect=["申报成功，无需缴款", "申报成功", DesktopError("低置信度")])
        adapter.read = Mock(return_value="本期收入")
        with self.assertRaisesRegex(DesktopError, "低置信度"):
            adapter.verify(self.company)


if __name__ == "__main__":
    unittest.main()
