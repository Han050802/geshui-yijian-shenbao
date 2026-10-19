"""Offline navigation regressions; never access or send to a real WeChat UI."""

import ctypes
import importlib.util
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "wechat" / "wechat_report.py"
SPEC = importlib.util.spec_from_file_location("wechat_report_navigation_tests", MODULE_PATH)
report = importlib.util.module_from_spec(SPEC)
with patch.object(ctypes, "windll", create=True):
    SPEC.loader.exec_module(report)


def ocr_line(text, y, *, x=100, right=290):
    return {"text": text, "x": x, "y": y, "right": right, "bottom": y + 20}


class NavigationBase(unittest.TestCase):
    def setUp(self):
        # Keep every Windows/input/capture/send boundary inert, including future
        # calls accidentally added to the navigation implementation.
        for name in (
            "_capture_wechat", "_click_screen", "_press_ctrl", "_show_wechat",
            "dismiss_search_box", "send_text_and_image", "capture_screen",
            "find_wechat_main_window", "_ocr_lines", "_chat_title_matches",
        ):
            self.mock_boundary(
                name, side_effect=AssertionError(f"Unexpected external operation: {name}")
            )
        for guard in (
            patch.object(ctypes, "windll", create=True),
            patch.object(ctypes, "WinDLL", create=True),
            patch.object(report.ImageGrab, "grab", side_effect=AssertionError("No screenshots")),
            patch.object(report.time, "sleep"),
            patch.object(report, "logger"),
        ):
            guard.start()
            self.addCleanup(guard.stop)
        self.wx = Mock(spec=["ChatInfo", "SendMsg", "SendFiles"])
        self.wx.SendMsg.side_effect = AssertionError("No messages")
        self.wx.SendFiles.side_effect = AssertionError("No attachments")
        self.hwnd = 42

    def mock_boundary(self, name, **kwargs):
        guard = patch.object(report, name, **kwargs)
        mocked = guard.start()
        self.addCleanup(guard.stop)
        return mocked

    def screen_scene(self, lines, *, foreground=True, verified=True):
        # A fake image avoids even constructing or reading a desktop screenshot.
        self.image = Mock(name="fake_wechat_image")
        self.capture = self.mock_boundary(
            "_capture_wechat", return_value=(self.image, 400, 200)
        )
        self.bounds = self.mock_boundary(
            "_search_box_bounds", return_value=(80, 36, 300, 64)
        )
        self.click = self.mock_boundary("_click_screen")
        self.press = self.mock_boundary("_press_ctrl")
        self.show = self.mock_boundary("_show_wechat", return_value=foreground)
        self.ocr = self.mock_boundary("_ocr_lines", return_value=lines)
        self.dismiss = self.mock_boundary("dismiss_search_box")
        self.wait_title = self.mock_boundary("_wait_for_chat_title", return_value=verified)


class NavigationTests(NavigationBase):
    def test_unique_keyword_clicks_result_and_verifies_full_group_name(self):
        full_name = "华东客户个税申报工作群"
        self.screen_scene([ocr_line("群聊", 90), ocr_line(full_name, 120)])

        opened = report._open_group_from_screen(self.wx, self.hwnd, "个税")

        self.assertEqual(opened, full_name)
        self.assertEqual(self.click.call_args_list, [call(590, 250), call(595, 330)])
        self.wait_title.assert_called_once_with(self.wx, self.hwnd, full_name)
        self.dismiss.assert_not_called()

    def test_multiple_matching_groups_do_not_click_a_result(self):
        self.screen_scene([
            ocr_line("群聊", 90),
            ocr_line("甲公司个税申报群", 120),
            ocr_line("乙公司个税申报群", 160),
        ])

        with self.assertRaises(report.AmbiguousChatError):
            report._open_group_from_screen(self.wx, self.hwnd, "个税")

        self.click.assert_called_once_with(590, 250)
        self.wait_title.assert_not_called()

    def test_exact_search_rejects_partial_group_name(self):
        self.screen_scene([ocr_line("群聊", 90), ocr_line("华东个税工作群", 120)])

        opened = report._open_group_from_screen(self.wx, self.hwnd, "个税", exact=True)

        self.assertEqual(opened, "")
        self.click.assert_called_once_with(590, 250)
        self.wait_title.assert_not_called()
        self.dismiss.assert_called_once_with()

    def test_exact_group_search_does_not_click_same_name_contact(self):
        full_name = "华东个税工作群"
        self.screen_scene([ocr_line("联系人", 90), ocr_line(full_name, 120)])

        opened = report._open_group_from_screen(
            self.wx, self.hwnd, full_name, exact=True, group_only=True
        )

        self.assertEqual(opened, "")
        self.click.assert_called_once_with(590, 250)
        self.wait_title.assert_not_called()

    def test_exact_group_search_opens_and_verifies_full_title(self):
        full_name = "华东个税工作群"
        self.screen_scene([ocr_line("群聊", 90), ocr_line(full_name, 120)])

        opened = report._open_group_from_screen(
            self.wx, self.hwnd, full_name, exact=True, group_only=True
        )

        self.assertEqual(opened, full_name)
        self.assertEqual(self.click.call_count, 2)
        self.wait_title.assert_called_once_with(self.wx, self.hwnd, full_name, strict=True)
        self.ocr.assert_called_once_with(self.image, preserve_spacing=True)

    def test_strict_ocr_group_search_never_merges_internal_spaces(self):
        self.screen_scene([ocr_line("群聊", 90), ocr_line("AB", 120)])

        opened = report._open_group_from_screen(
            self.wx, self.hwnd, "A B", exact=True, group_only=True
        )

        self.assertEqual(opened, "")
        self.click.assert_called_once_with(590, 250)
        self.wait_title.assert_not_called()
        self.ocr.assert_called_once_with(self.image, preserve_spacing=True)

    def test_lost_foreground_prevents_result_click(self):
        self.screen_scene([ocr_line("群聊", 90), ocr_line("华东个税工作群", 120)])
        self.show.side_effect = [True, False]

        opened = report._open_group_from_screen(self.wx, self.hwnd, "个税")

        self.assertEqual(opened, "")
        self.click.assert_called_once_with(590, 250)
        self.capture.assert_called_once_with(self.hwnd)
        self.ocr.assert_not_called()
        self.wait_title.assert_not_called()
        self.dismiss.assert_called_once_with()

    def test_unconfirmed_title_returns_empty_after_click(self):
        full_name = "华东个税工作群"
        self.screen_scene(
            [ocr_line("群聊", 90), ocr_line(full_name, 120)], verified=False
        )

        opened = report._open_group_from_screen(self.wx, self.hwnd, "个税")

        self.assertEqual(opened, "")
        self.assertEqual(self.click.call_count, 2)
        self.wait_title.assert_called_once_with(self.wx, self.hwnd, full_name)
        self.dismiss.assert_called_once_with()

    def test_ocr_preserves_long_names_but_ignores_right_chat_pane(self):
        full_name = "华东客户个税申报沟通与资料核对工作群"
        self.screen_scene([
            ocr_line("群聊", 90),
            # The name begins in the results column but extends past its edge.
            ocr_line(full_name, 120, right=390),
            # Right-pane section/message must not alter the search classification.
            ocr_line("聊天记录", 110, x=500, right=600),
            ocr_line("另一家公司的个税工作群", 140, x=500, right=800),
        ])

        opened = report._open_group_from_screen(self.wx, self.hwnd, "个税")

        self.assertEqual(opened, full_name)
        self.ocr.assert_called_once_with(self.image)
        self.image.crop.assert_not_called()
        self.wait_title.assert_called_once_with(self.wx, self.hwnd, full_name)

    def test_control_navigation_verifies_full_name_without_importing_real_uia(self):
        full_name = "华东客户个税申报工作群"
        search_edit = Mock()
        search_edit.Exists.return_value = True
        search_edit.GetValuePattern.return_value.Value = "个税"
        result = Mock()
        result.Name = full_name
        result.ClassName = "mmui::SearchContentCellView"
        search_list = Mock()
        search_list.Exists.return_value = True
        search_list.GetChildren.return_value = [
            SimpleNamespace(Name="群聊", ClassName="mmui::XTableCell"), result
        ]
        root = Mock()
        root.EditControl.return_value = search_edit
        root.ListControl.return_value = search_list
        auto = Mock()
        auto.ControlFromHandle.return_value = root
        fake_package = ModuleType("wxauto4")
        fake_package.__path__ = []
        fake_uia = ModuleType("wxauto4.uia")
        fake_uia.uiautomation = auto
        fake_package.uia = fake_uia
        self.mock_boundary("_press_ctrl")
        self.mock_boundary("_show_wechat", return_value=True)
        wait_title = self.mock_boundary("_wait_for_chat_title", return_value=True)

        with patch.dict(sys.modules, {"wxauto4": fake_package, "wxauto4.uia": fake_uia}):
            opened = report._open_group_by_controls(self.wx, self.hwnd, "个税")

        self.assertEqual(opened, full_name)
        search_edit.Click.assert_called_once_with()
        result.Click.assert_called_once_with()
        wait_title.assert_called_once_with(self.wx, self.hwnd, full_name)

    def test_strict_control_chain_preserves_full_name_from_input_through_title(self):
        for typed, candidate, expected in (("A B", "A B", "A B"),
                                            ("AB", "A B", ""),
                                            ("A B", "AB", "")):
            with self.subTest(typed=typed, candidate=candidate):
                search_edit = Mock()
                search_edit.Exists.return_value = True
                search_edit.GetValuePattern.return_value.Value = typed
                result = Mock(Name=candidate, ClassName="mmui::SearchContentCellView")
                search_list = Mock()
                search_list.Exists.return_value = True
                search_list.GetChildren.return_value = [
                    SimpleNamespace(Name="群聊", ClassName="mmui::XTableCell"), result,
                ]
                root = Mock()
                root.EditControl.return_value = search_edit
                root.ListControl.return_value = search_list
                auto = Mock()
                auto.ControlFromHandle.return_value = root
                fake_uia = ModuleType("wxauto4.uia")
                fake_uia.uiautomation = auto
                with patch.dict(sys.modules, {"wxauto4.uia": fake_uia}), patch.object(
                    report, "_press_ctrl"
                ), patch.object(report, "_show_wechat", return_value=True), patch.object(
                    report, "_wait_for_chat_title", return_value=True
                ) as title:
                    opened = report._open_group_by_controls(
                        self.wx, self.hwnd, "A B", exact=True, group_only=True
                    )
                self.assertEqual(opened, expected)
                if expected:
                    result.Click.assert_called_once_with()
                    title.assert_called_once_with(self.wx, self.hwnd, "A B", strict=True)
                else:
                    result.Click.assert_not_called()
                    title.assert_not_called()

    def test_strict_title_wait_propagates_strictness(self):
        matches = self.mock_boundary("_chat_title_matches", return_value=True)
        self.assertTrue(report._wait_for_chat_title(self.wx, self.hwnd, "A B", strict=True))
        matches.assert_called_once_with(self.wx, self.hwnd, "A B", strict=True)

    def test_title_wait_allows_render_delay_before_success(self):
        full_name = "华东个税工作群"
        matches = self.mock_boundary("_chat_title_matches", side_effect=[False, False, True])
        with patch.object(report.time, "monotonic", side_effect=[10.0, 10.1, 10.4]):
            opened = report._wait_for_chat_title(self.wx, self.hwnd, full_name)

        self.assertTrue(opened)
        self.assertEqual(matches.call_args_list, [call(self.wx, self.hwnd, full_name)] * 3)
        self.assertEqual(report.time.sleep.call_args_list, [call(0.25), call(0.25)])


class WaitForGroupTests(NavigationBase):
    def setUp(self):
        super().setUp()
        self.group = "华东个税工作群"
        self.stop_event = threading.Event()
        self.clock = 100.0
        guard = patch.object(report.time, "monotonic", side_effect=lambda: self.clock)
        guard.start()
        self.addCleanup(guard.stop)
        self.found = self.mock_boundary("find_wechat_main_window", return_value=(42, "wechat.exe"))
        self.read_title = self.mock_boundary("_read_live_chat_title", return_value="尚未命名的群")
        self.search = self.mock_boundary("search_group", return_value=self.group)
        ctypes.windll.user32.GetForegroundWindow.return_value = self.hwnd
        self.event_wait = Mock(side_effect=self.advance_clock)
        self.stop_event.wait = self.event_wait

    def advance_clock(self, seconds):
        self.clock += seconds
        return self.stop_event.is_set()

    def test_wait_observes_until_title_matches_then_verifies_group_once(self):
        self.read_title.side_effect = ["尚未命名的群", "华东个税工作群（12）"]
        statuses = []
        self.assertEqual(report.wait_for_group(
            self.group, self.stop_event, statuses.append, timeout=2
        ), self.group)
        self.search.assert_called_once_with(self.group)
        self.assertEqual(self.read_title.call_count, 2)
        self.event_wait.assert_called_once_with(0.5)
        self.assertEqual(len(statuses), 2)

    def test_cancelled_wait_does_not_read_or_search(self):
        self.stop_event.set()
        with self.assertRaises(report.GroupWaitCancelled):
            report.wait_for_group(self.group, self.stop_event)
        self.read_title.assert_not_called()
        self.search.assert_not_called()

    def test_cancel_during_read_prevents_search(self):
        def read(_hwnd, **_kwargs):
            self.stop_event.set()
            return self.group
        self.read_title.side_effect = read
        with self.assertRaises(report.GroupWaitCancelled):
            report.wait_for_group(self.group, self.stop_event)
        self.search.assert_not_called()

    def test_cancel_during_verification_does_not_return_group_for_registration(self):
        self.read_title.return_value = self.group
        def search(_group):
            self.stop_event.set()
            return self.group
        self.search.side_effect = search
        with self.assertRaises(report.GroupWaitCancelled):
            report.wait_for_group(self.group, self.stop_event)

    def test_unmatched_title_times_out_without_searching(self):
        with self.assertRaisesRegex(TimeoutError, "未登记到 Excel"):
            report.wait_for_group(self.group, self.stop_event, timeout=1)
        self.search.assert_not_called()
        self.assertEqual(self.event_wait.call_count, 2)

    def test_background_wechat_is_not_focused_or_read(self):
        ctypes.windll.user32.GetForegroundWindow.return_value = 999
        with self.assertRaises(TimeoutError):
            report.wait_for_group(self.group, self.stop_event, timeout=1)
        self.read_title.assert_not_called()
        self.search.assert_not_called()

    def test_missing_wechat_keeps_waiting_without_searching(self):
        self.found.return_value = None
        with self.assertRaises(TimeoutError):
            report.wait_for_group(self.group, self.stop_event, timeout=1)
        self.read_title.assert_not_called()
        self.search.assert_not_called()

    def test_unreadable_title_keeps_waiting_without_searching(self):
        self.read_title.side_effect = RuntimeError("暂时不可读取")
        with self.assertRaises(TimeoutError):
            report.wait_for_group(self.group, self.stop_event, timeout=1)
        self.search.assert_not_called()

    def test_title_match_without_unique_group_is_never_registered(self):
        self.read_title.return_value = self.group
        self.search.return_value = ""
        with self.assertRaisesRegex(RuntimeError, "未能唯一确认"):
            report.wait_for_group(self.group, self.stop_event)
        self.search.assert_called_once_with(self.group)
        self.event_wait.assert_not_called()

    def test_invalid_timeout_is_rejected_without_reading(self):
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                report.wait_for_group(self.group, self.stop_event, timeout=timeout)
        self.read_title.assert_not_called()
        self.search.assert_not_called()


if __name__ == "__main__":
    unittest.main()

