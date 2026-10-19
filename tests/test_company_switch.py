"""Company-switch regressions, with no real desktop or tax submissions."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tax_desktop import TaxDesktop, DesktopError, live_windows


class CompanySwitchTests(unittest.TestCase):
    def manager_adapter(self):
        adapter = self.adapter()
        adapter.root = SimpleNamespace(handle=100)
        adapter.find_company_dialog = Mock(return_value=None)
        adapter.footer_matches = Mock(return_value=False)
        adapter.find_login_dialog = Mock(return_value=None)
        # Popup capture/closing is outside these management retry tests.
        adapter.dismiss_declare_success = Mock(return_value=False)
        adapter.bring_client_forward = Mock()
        adapter.click_text = Mock()
        return adapter

    def test_manager_retry_reobserves_and_stops_after_open(self):
        adapter = self.manager_adapter()
        dialog = Mock()
        adapter.wait_company_dialog = Mock(side_effect=[False, dialog])
        self.assertIs(adapter.open_company_management(SimpleNamespace(name="甲公司")), dialog)
        self.assertEqual(adapter.click_text.call_count, 2)
        self.assertEqual(adapter.find_company_dialog.call_count, 2)

    def test_delayed_dialog_is_not_toggled_closed_on_retry(self):
        adapter = self.manager_adapter()
        dialog = Mock()
        adapter.find_company_dialog.side_effect = [None, dialog]
        adapter.wait_company_dialog = Mock(return_value=False)
        self.assertIs(adapter.open_company_management(SimpleNamespace(name="甲公司")), dialog)
        adapter.click_text.assert_called_once()

    def test_manager_no_response_is_bounded(self):
        adapter = self.manager_adapter()
        adapter.wait_company_dialog = Mock(return_value=False)
        with self.assertRaisesRegex(DesktopError, "已重新定位2次"):
            adapter.open_company_management(SimpleNamespace(name="甲公司"))
        self.assertEqual(adapter.click_text.call_count, 2)

    def test_manager_does_not_retry_a_blocked_click(self):
        adapter = self.manager_adapter()
        adapter.click_text.side_effect = DesktopError("遮挡")
        adapter.wait_company_dialog = Mock()
        with self.assertRaises(DesktopError):
            adapter.open_company_management(SimpleNamespace(name="甲公司"))
        adapter.click_text.assert_called_once()
        adapter.wait_company_dialog.assert_not_called()

    def test_click_rejects_same_process_popup_covering_target(self):
        adapter = self.adapter()
        adapter.root = Mock(handle=100)
        adapter.root.rectangle.return_value = SimpleNamespace(left=0, top=0,
                                                               width=lambda: 1010, height=lambda: 688)
        adapter.user32.GetForegroundWindow.return_value = 100
        adapter.user32.WindowFromPoint.return_value = 201
        adapter.user32.GetAncestor.return_value = 200
        with self.assertRaisesRegex(DesktopError, "遮挡"):
            adapter.validate_click_target(.85, .08)

    def test_click_allows_visible_button_when_another_window_is_foreground(self):
        adapter = self.adapter()
        adapter.root = Mock(handle=100)
        adapter.root.rectangle.return_value = SimpleNamespace(left=0, top=0,
                                                               width=lambda: 1010, height=lambda: 688)
        adapter.user32.GetForegroundWindow.return_value = 900
        adapter.user32.WindowFromPoint.return_value = 101
        adapter.user32.GetAncestor.return_value = 100
        adapter.focus_handle = Mock(return_value=False)
        adapter.validate_click_target(.85, .08)
        adapter.focus_handle.assert_called_once()

    def test_click_accepts_button_child_of_target_window(self):
        adapter = self.adapter()
        adapter.root = Mock(handle=100)
        adapter.root.rectangle.return_value = SimpleNamespace(left=0, top=0,
                                                               width=lambda: 1010, height=lambda: 688)
        adapter.user32.GetForegroundWindow.return_value = 100
        adapter.user32.WindowFromPoint.return_value = 101
        adapter.user32.GetAncestor.return_value = 100
        adapter.validate_click_target(.85, .08)

    def test_wait_manager_accepts_login_completed_homepage(self):
        adapter = self.adapter()
        adapter.find_company_dialog = Mock(return_value=None)
        adapter.footer_matches = Mock(side_effect=[False, True, True])
        ticks = iter(range(100))
        with patch("tax_desktop.time.sleep"), patch("tax_desktop.time.monotonic", side_effect=lambda: next(ticks)):
            self.assertIsNone(adapter.wait_company_dialog(company=SimpleNamespace(name="甲公司")))
        self.assertEqual(adapter.footer_matches.call_count, 3)

    def test_wait_manager_does_not_accept_wrong_company(self):
        adapter = self.adapter()
        adapter.find_company_dialog = Mock(return_value=None)
        adapter.footer_matches = Mock(return_value=False)
        ticks = iter(range(100))
        with patch("tax_desktop.time.sleep"), patch("tax_desktop.time.monotonic", side_effect=lambda: next(ticks)):
            with self.assertRaisesRegex(DesktopError, "未确认目标公司"):
                adapter.wait_company_dialog(timeout=3, company=SimpleNamespace(name="甲公司"))

    def test_open_manager_is_used_without_clicking_management_again(self):
        adapter = self.adapter()
        adapter.root = Mock()
        main = adapter.root
        dialog = Mock()
        adapter.footer_matches = Mock(return_value=False)
        adapter.find_company_dialog = Mock(return_value=dialog)
        adapter.click_text = Mock()
        adapter.search_company_in_dialog = Mock()
        adapter.scroll_result_to_end = Mock()
        adapter.remark_on_row = Mock(return_value="test-only")
        adapter.open_company_row = Mock(return_value=True)
        adapter.wait_logged_in = Mock()
        adapter._clear_clipboard = Mock()
        company = SimpleNamespace(name="甲公司")
        adapter.login_company(company)
        adapter.click_text.assert_not_called()
        adapter.search_company_in_dialog.assert_called_once_with(company)
        adapter.wait_logged_in.assert_called_once()
        self.assertIs(adapter.root, main)

    def test_evidence_never_initiates_login(self):
        adapter = self.adapter()
        adapter.root = Mock()
        adapter.last_image = Mock()
        adapter.footer_matches = Mock(return_value=False)
        adapter.identity = Mock()
        adapter.login_company = Mock()
        self.assertEqual(adapter.evidence(SimpleNamespace(name="甲公司"), "UNKNOWN"), "")
        adapter.identity.assert_not_called()
        adapter.login_company.assert_not_called()

    def test_closed_popup_during_enumeration_does_not_abort_main_window(self):
        from pywinauto.controls.hwndwrapper import InvalidWindowHandle
        native = Mock()
        def enumerate_handles(callback, unused):
            for hwnd in (11, 12, 13):
                callback(hwnd, 0)
            return True
        native.EnumWindows.side_effect = enumerate_handles
        native.IsWindow.side_effect = [False, True, True]
        main = Mock()
        with patch("tax_desktop.ctypes.windll.user32", native), patch(
                "pywinauto.controls.hwndwrapper.HwndWrapper",
                side_effect=[InvalidWindowHandle(12), main]):
            self.assertEqual(live_windows(), [main])

    def test_check_zero_rebinds_and_validates_before_clicking(self):
        adapter = self.adapter()
        calls = []
        adapter.attach = Mock(side_effect=lambda: calls.append("attach"))
        adapter.guard = Mock(side_effect=lambda company: calls.append("guard"))
        def stop_at_first_click(*args, **kwargs):
            calls.append("click")
            raise DesktopError("test stops before any real input")
        adapter.click_text = Mock(side_effect=stop_at_first_click)
        with self.assertRaises(DesktopError):
            adapter.check_zero(SimpleNamespace(name="甲公司"))
        self.assertEqual(calls, ["attach", "guard", "click"])

    def adapter(self):
        adapter = TaxDesktop.__new__(TaxDesktop)
        adapter.root = None
        adapter.log = Mock()
        adapter.cancelled = Mock()
        adapter.user32 = Mock()
        adapter.user32.IsWindowEnabled.return_value = True
        return adapter

    def test_waits_for_late_created_controls(self):
        adapter = self.adapter()
        edits = [(0.2, 0.2, 11), (0.6, 0.2, 12)]
        buttons = [(0.3, 0.27, 21), (0.7, 0.27, 22)]
        adapter.child_boxes = Mock(side_effect=[[], [], [edits[0]], buttons,
                                                edits, buttons, edits, buttons])
        with patch("tax_desktop.time.sleep"):
            self.assertEqual(adapter.wait_company_search_controls(), (edits[0], edits[1], buttons[0]))
        self.assertEqual(adapter.child_boxes.call_count, 8)

    def test_disabled_input_waits_then_becomes_ready(self):
        adapter = self.adapter()
        edits = [(0.2, 0.2, 11), (0.6, 0.2, 12)]
        buttons = [(0.3, 0.27, 21)]
        adapter.child_boxes = Mock(side_effect=lambda kind: edits if kind == "TITSEdit" else buttons)
        adapter.user32.IsWindowEnabled.side_effect = [False, True, True, True, True, True, True]
        with patch("tax_desktop.time.sleep"):
            self.assertEqual(adapter.wait_company_search_controls(), (edits[0], edits[1], buttons[0]))
        self.assertEqual(adapter.child_boxes.call_count, 6)

    def test_missing_controls_fail_with_diagnostics_without_clicking(self):
        adapter = self.adapter()
        adapter.root = Mock()
        adapter.root.class_name.return_value = "TFrmNewCompanyMgrView"
        adapter.child_boxes = Mock(return_value=[])
        adapter.click_control = Mock()
        ticks = iter(range(100))
        with patch("tax_desktop.time.sleep"), patch("tax_desktop.time.monotonic", side_effect=lambda: next(ticks)):
            with self.assertRaisesRegex(DesktopError, "未就绪"):
                adapter.wait_company_search_controls(timeout=3)
        adapter.click_control.assert_not_called()
        self.assertTrue(any("控件诊断" in c.args[0] for c in adapter.log.call_args_list))

    def test_visible_foreground_dialog_is_not_restored_or_refocused(self):
        adapter = self.adapter()
        adapter.root = SimpleNamespace(handle=100, process_id=lambda: 1234)
        adapter.user32 = Mock()
        adapter.user32.GetForegroundWindow.return_value = 100
        adapter.user32.IsIconic.return_value = False
        adapter.user32.IsWindowVisible.return_value = True
        adapter.focus_window = Mock()
        # Model an unobstructed desktop without enumerating real Windows windows.
        with patch("tax_desktop.live_windows", return_value=[]):
            adapter.bring_client_forward()
        adapter.user32.ShowWindow.assert_not_called()
        adapter.user32.AttachThreadInput.assert_not_called()
        adapter.user32.keybd_event.assert_not_called()
        adapter.focus_window.assert_not_called()

    def test_two_companies_query_once_each_without_reset(self):
        adapter = self.adapter()
        adapter.child_boxes = Mock(side_effect=lambda kind: (
            [(0.2, 0.2, 11), (0.6, 0.2, 12)] if kind == "TITSEdit"
            else [(0.3, 0.27, 21), (0.7, 0.27, 22)]))
        adapter.click_control = Mock()
        adapter.type_literal = Mock()
        adapter.wait_company_results = Mock()
        adapter.read_control_text = Mock(side_effect=["", "甲公司", "", "乙公司"])
        for name in ("甲公司", "乙公司"):
            adapter.search_company_in_dialog(SimpleNamespace(name=name))
        self.assertEqual([c.args[0] for c in adapter.click_control.call_args_list],
                         [12, 21, 12, 21])
        self.assertEqual(adapter.wait_company_results.call_count, 2)

    def test_partial_name_input_never_queries(self):
        adapter = self.adapter()
        adapter.child_boxes = Mock(return_value=[(0.2, 0.2, 11), (0.6, 0.2, 12)])
        adapter.click_control = Mock()
        adapter.type_literal = Mock()
        adapter.read_control_text = Mock(side_effect=["", "甲公司分公司"])
        with self.assertRaises(DesktopError):
            adapter.search_company_in_dialog(SimpleNamespace(name="甲公司"))
        adapter.click_control.assert_called_once_with(12)

    def test_waits_through_stale_and_busy_results(self):
        adapter = self.adapter()
        adapter.snap = Mock(side_effect=[
            [{"text": "旧公司"}],
            [{"text": "甲公司"}, {"text": "正在查询"}],
            [{"text": "甲公司"}], [{"text": "甲公司"}],
        ])
        ticks = iter(range(100))
        with patch("tax_desktop.time.sleep"), patch("tax_desktop.time.monotonic", side_effect=lambda: next(ticks)):
            adapter.wait_company_results(SimpleNamespace(name="甲公司"))
        self.assertEqual(adapter.snap.call_count, 4)

    def test_missing_result_times_out_without_another_click(self):
        adapter = self.adapter()
        adapter.snap = Mock(return_value=[{"text": "旧公司"}])
        adapter.click_control = Mock()
        ticks = iter(range(100))
        with patch("tax_desktop.time.sleep"), patch("tax_desktop.time.monotonic", side_effect=lambda: next(ticks)):
            with self.assertRaisesRegex(DesktopError, "不重复刷新查询"):
                adapter.wait_company_results(SimpleNamespace(name="甲公司"), timeout=3)
        adapter.click_control.assert_not_called()


if __name__ == "__main__":
    unittest.main()
