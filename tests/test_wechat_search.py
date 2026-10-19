"""Offline regressions for selecting and verifying a WeChat destination.

All platform, screenshot, click, clipboard-adjacent and send entry points are
mocked. These tests never connect to WeChat or send a real message.
"""

import ctypes
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "wechat" / "wechat_report.py"
SPEC = importlib.util.spec_from_file_location("wechat_report_search_tests", MODULE_PATH)
report = importlib.util.module_from_spec(SPEC)
# Importing the script normally adjusts the process DPI. Keep even that Windows
# call isolated, so importing this test module has no desktop side effects.
with patch.object(ctypes, "windll", create=True):
    SPEC.loader.exec_module(report)


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        for name in (
            "_capture_wechat", "_click_screen", "_press_ctrl", "_show_wechat",
            "dismiss_search_box", "send_text_and_image", "find_wechat_main_window",
            "capture_screen",
        ):
            guard = patch.object(
                report, name,
                side_effect=AssertionError(f"Unexpected external operation: {name}"),
            )
            guard.start()
            self.addCleanup(guard.stop)
        for guard in (
            patch.object(ctypes, "windll", create=True),
            patch.object(ctypes, "WinDLL", create=True),
            patch.object(report.ImageGrab, "grab", side_effect=AssertionError("No real screenshots")),
            patch.object(report.time, "sleep"),
            patch.object(report, "logger"),
        ):
            guard.start()
            self.addCleanup(guard.stop)


class SearchSelectionTests(OfflineTestCase):
    def assert_selection(self, rows, keyword, expected, *, exact=False, ambiguous=False):
        """Exercise the same search scene through controls and OCR."""
        controls = [
            SimpleNamespace(
                Name=text,
                ClassName="mmui::XTableCell" if kind == "section" else "mmui::SearchContentCellView",
            )
            for kind, text in rows
        ]
        search_list = Mock()
        search_list.Exists.return_value = True
        search_list.GetChildren.return_value = controls
        root = Mock()
        root.ListControl.return_value = search_list
        lines = [
            {"text": text, "x": 100, "right": 300, "y": 100 + i * 24, "bottom": 118 + i * 24}
            for i, (_, text) in enumerate(rows)
        ]
        with self.subTest(source="controls"):
            if ambiguous:
                with self.assertRaises(report.AmbiguousChatError):
                    report._search_result_to_click(root, keyword, exact=exact)
            else:
                actual = report._search_result_to_click(root, keyword, exact=exact)
                self.assertIs(actual, None if expected is None else controls[expected])
        with self.subTest(source="ocr"):
            # OCR providers need not return their lines in visual order.
            if ambiguous:
                with self.assertRaises(report.AmbiguousChatError):
                    report._result_to_click(list(reversed(lines)), keyword, 60, exact=exact)
            else:
                actual = report._result_to_click(list(reversed(lines)), keyword, 60, exact=exact)
                self.assertIs(actual, None if expected is None else lines[expected])

    def test_unique_group_keyword_opens_full_group_name(self):
        self.assert_selection([("section", "群聊"), ("result", "示例公司财务申报群")], "财务申报", 1)

    def test_full_group_name_wins_over_partial_and_contact(self):
        self.assert_selection([
            ("section", "联系人"), ("result", "财务群"),
            ("section", "群聊"), ("result", "财务群旧群"), ("result", "财务群"),
        ], "财务群", 4)

    def test_unique_group_keyword_wins_over_exact_contact(self):
        self.assert_selection([
            ("section", "联系人"), ("result", "财务"),
            ("section", "群聊"), ("result", "示例公司财务群"),
        ], "财务", 3)

    def test_two_keyword_groups_are_ambiguous(self):
        self.assert_selection([
            ("section", "群聊"), ("result", "甲公司财务群"), ("result", "乙公司财务群"),
        ], "财务", None, ambiguous=True)

    def test_ambiguous_groups_never_fall_back_to_exact_contact(self):
        self.assert_selection([
            ("section", "联系人"), ("result", "财务"),
            ("section", "群聊"), ("result", "甲公司财务群"), ("result", "乙公司财务群"),
        ], "财务", None, ambiguous=True)

    def test_duplicate_exact_group_names_are_ambiguous(self):
        self.assert_selection([
            ("section", "群聊"), ("result", "财务群"), ("result", "财务群"),
        ], "财务群", None, ambiguous=True)

    def test_fallback_exact_name_can_be_repeated_in_search_results(self):
        self.assert_selection([
            ("section", "联系人"), ("result", "王书涵"),
            ("result", "王书涵"), ("result", "王书涵"),
        ], "王书涵", 1, exact=True)

    def test_repeated_fallback_names_without_category_use_first_exact_hit(self):
        self.assert_selection([
            ("result", "王书涵"), ("result", "王书涵"), ("result", "王书涵"),
        ], "王书涵", 0, exact=True)

    def test_whitespace_and_member_count_allow_keyword_match(self):
        self.assert_selection([
            ("section", "群 聊"), ("result", "示例 公 司\u3000财务 群 (12)"),
        ], "公司\t财务", 1)

    def test_exact_mode_rejects_keyword_only_group(self):
        self.assert_selection([
            ("section", "群聊"), ("result", "示例公司财务群"),
        ], "财务", None, exact=True)

    def test_exact_mode_accepts_full_group_name(self):
        self.assert_selection([
            ("section", "群聊"), ("result", "示例 公司财务群"),
        ], "示例公司财务群", 1, exact=True)

    def test_other_chat_sections_require_full_name(self):
        for section in ("联系人", "功能", "最常使用", "最近使用"):
            with self.subTest(section=section):
                rows = [("section", section), ("result", "文件传输助手")]
                self.assert_selection(rows, "文件传输", None)
                self.assert_selection(rows, "文件传输助手", 1)

    def test_history_and_network_matches_are_never_destinations(self):
        for section in ("聊天记录", "搜索网络结果", "网络搜索", "公众号", "小程序"):
            with self.subTest(section=section):
                self.assert_selection([
                    ("section", section), ("result", "财务群"),
                ], "财务群", None)

    def test_unknown_or_absent_section_allows_only_full_name(self):
        for rows in (
            [("section", "未知栏目"), ("result", "财务群")],
            [("result", "财务群")],
        ):
            self.assert_selection(rows, "财务群", 1 if len(rows) == 2 else 0, exact=True)
            self.assert_selection(rows, "财务", None)

    def test_same_full_name_in_distinct_contact_sections_is_not_ambiguous(self):
        self.assert_selection([
            ("section", "最常使用"), ("result", "文件传输助手"),
            ("section", "联系人"), ("result", "文件传输助手"),
        ], "文件传输助手", 1, exact=True)

    def test_group_section_with_result_count_allows_keyword(self):
        for section in ("群聊(1)", "群聊（1）", "群聊1"):
            with self.subTest(section=section):
                self.assert_selection([
                    ("section", section), ("result", "示例公司财务群"),
                ], "财务", 1)

    def test_view_all_and_following_rows_are_not_destinations(self):
        self.assert_selection([
            ("section", "群聊"), ("section", "查看全部群聊"), ("result", "财务群"),
        ], "财务群", None)

    def test_blank_keyword_cannot_select_any_destination(self):
        self.assert_selection([("section", "群聊"), ("result", "财务群")], " \t\u3000", None)

    def test_ocr_search_field_text_is_excluded(self):
        lines = [
            {"text": "财务", "x": 100, "right": 160, "y": 40, "bottom": 60},
            {"text": "群聊", "x": 100, "right": 160, "y": 90, "bottom": 110},
            {"text": "示例财务群", "x": 100, "right": 260, "y": 120, "bottom": 140},
        ]
        self.assertIs(report._result_to_click(lines, "财务", 60), lines[2])

    def test_missing_control_search_list_returns_none(self):
        root = Mock()
        root.ListControl.return_value.Exists.return_value = False
        self.assertIsNone(report._search_result_to_click(root, "财务"))
        root.ListControl.return_value.GetChildren.assert_not_called()


class ChatTitleTests(OfflineTestCase):
    def assert_title(self, actual, selected, expected):
        wx = Mock()
        wx.ChatInfo.return_value = {"chat_name": actual}
        with patch.object(report, "_capture_wechat", return_value=(Mock(width=1000), 0, 0)) as capture, patch.object(
            report, "_ocr_lines", return_value=[{"text": actual}],
        ):
            self.assertEqual(report._chat_title_matches(wx, 123, selected), expected)
            if expected:
                capture.assert_not_called()
            else:
                capture.assert_called_once_with(123)

    def test_full_selected_group_title_matches(self):
        self.assert_title("示例公司财务群", "示例公司财务群", True)

    def test_title_whitespace_and_trailing_count_are_accepted(self):
        self.assert_title("示例 公司 财务群（ 12 ）", "示例公司财务群", True)

    def test_selected_name_suffix_is_preserved_as_part_of_destination(self):
        self.assert_title("示例公司财务群", "示例公司财务群(12)", False)
        self.assert_title("示例公司财务群(2)", "示例公司财务群(1)", False)

    def test_same_keyword_in_different_title_is_rejected(self):
        self.assert_title("另一公司财务群", "示例公司财务群", False)
        self.assert_title("示例公司财务群旧群", "示例公司财务群", False)

    def test_stale_chat_info_is_verified_against_screen_title(self):
        wx = Mock()
        wx.ChatInfo.return_value = {"chat_name": "另一公司财务群"}
        with patch.object(report, "_capture_wechat", return_value=(Mock(width=1000), 0, 0)) as capture, patch.object(
            report, "_ocr_lines", return_value=[{"text": "示例公司财务群"}],
        ) as ocr:
            self.assertTrue(report._chat_title_matches(wx, 123, "示例公司财务群"))
            capture.assert_called_once_with(123)
            ocr.assert_called_once()

    def test_empty_chat_info_uses_ocr_full_title(self):
        wx = Mock()
        wx.ChatInfo.return_value = {}
        image = Mock(width=1000)
        with patch.object(report, "_search_box_bounds", side_effect=AssertionError("Title verification must not inspect search field")) as search, patch.object(
            report, "_capture_wechat", return_value=(image, 0, 0),
        ), patch.object(
            report, "_ocr_lines", return_value=[{"text": "示例 公司 财务群（12）"}],
        ) as ocr:
            self.assertTrue(report._chat_title_matches(wx, 123, "示例公司财务群"))
            ocr.assert_called_once_with(image.crop.return_value)
            image.crop.assert_called_once_with((360, 28, 780, 92))
            search.assert_not_called()

    def test_ocr_split_title_and_member_count_are_joined(self):
        wx = Mock()
        wx.ChatInfo.return_value = {"chat_name": "先前会话"}
        with patch.object(report, "_capture_wechat", return_value=(Mock(width=1000), 0, 0)), patch.object(
            report, "_ocr_lines", return_value=[
                {"text": "示例公司"}, {"text": "财务群"}, {"text": "（12）"},
            ],
        ):
            self.assertTrue(report._chat_title_matches(wx, 123, "示例公司财务群"))

    def test_ocr_split_old_group_title_cannot_match_destination_substring(self):
        wx = Mock()
        wx.ChatInfo.return_value = {"chat_name": "先前会话"}
        for fragments in (
            ["示例公司", "财务群旧群", "（12）"],
            ["示例公司财务群", "旧群", "（12）"],
        ):
            with self.subTest(fragments=fragments), patch.object(
                report, "_capture_wechat", return_value=(Mock(width=1000), 0, 0),
            ), patch.object(report, "_ocr_lines", return_value=[{"text": text} for text in fragments]):
                self.assertFalse(report._chat_title_matches(wx, 123, "示例公司财务群"))

    def test_ocr_keyword_in_different_title_is_rejected(self):
        wx = Mock()
        wx.ChatInfo.return_value = {}
        with patch.object(report, "_search_box_bounds", return_value=(80, 30, 320, 60)), patch.object(
            report, "_capture_wechat", return_value=(Mock(width=1000), 0, 0),
        ), patch.object(
            report, "_ocr_lines", return_value=[{"text": "另一公司财务群"}],
        ):
            self.assertFalse(report._chat_title_matches(wx, 123, "示例公司财务群"))


class OpenGroupTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.wx = Mock()
        self.clipboard = Mock()
        self.clipboard.paste.return_value = "original clipboard"
        modules = patch.dict(sys.modules, {
            "pyperclip": self.clipboard,
            "wechat_a11y": SimpleNamespace(prime_wechat_accessibility=Mock()),
        })
        modules.start()
        self.addCleanup(modules.stop)
        for name, result in (
            ("find_wechat_main_window", (123,)),
            ("_show_wechat", True),
            ("dismiss_search_box", None),
            ("_open_group_by_controls", ""),
            ("_open_group_from_screen", ""),
        ):
            guard = patch.object(report, name, return_value=result)
            setattr(self, name, guard.start())
            self.addCleanup(guard.stop)
        ctypes.windll.user32.GetForegroundWindow.return_value = 123

    def test_control_match_returns_full_name_without_screen_fallback(self):
        self._open_group_by_controls.return_value = "示例公司财务群"
        self.assertEqual(report.open_group(self.wx, "财务"), "示例公司财务群")
        self._open_group_from_screen.assert_not_called()
        self.clipboard.copy.assert_has_calls([call("财务"), call("original clipboard")])

    def test_control_ambiguity_cannot_retry_with_less_complete_ocr_results(self):
        self._open_group_by_controls.side_effect = report.AmbiguousChatError("two groups")
        self._open_group_from_screen.return_value = "示例公司财务群"
        self.assertEqual(report.open_group(self.wx, "财务"), "")
        self._open_group_from_screen.assert_not_called()
        self.clipboard.copy.assert_called_with("original clipboard")

    def test_exact_mode_is_preserved_when_controls_need_ocr_fallback(self):
        self._open_group_from_screen.return_value = "文件传输助手"
        self.assertEqual(report.open_group(self.wx, "文件传输助手", exact=True), "文件传输助手")
        self._open_group_by_controls.assert_called_once_with(self.wx, 123, "文件传输助手", exact=True)
        self._open_group_from_screen.assert_called_once_with(self.wx, 123, "文件传输助手", exact=True)
        self.clipboard.copy.assert_called_with("original clipboard")


class DeliveryTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.wx = Mock()
        self.image = Path("offline-screenshot.png")
        for name in ("open_group", "send_text_and_image", "dismiss_search_box"):
            guard = patch.object(report, name)
            setattr(self, name, guard.start())
            self.addCleanup(guard.stop)

    def deliver(self):
        return report.deliver(self.wx, "本次说明", self.image, "财务", "文件传输助手")

    def test_target_success_returns_full_destination_and_sends_once(self):
        self.open_group.return_value = "示例公司财务群"
        self.assertEqual(self.deliver(), "示例公司财务群")
        self.open_group.assert_called_once_with(self.wx, "财务")
        self.send_text_and_image.assert_called_once_with(self.wx, "本次说明", self.image)
        self.dismiss_search_box.assert_not_called()

    def test_target_miss_opens_fallback_with_exact_name_only(self):
        self.open_group.side_effect = ["", "文件传输助手"]
        self.assertEqual(self.deliver(), "文件传输助手")
        self.assertEqual(self.open_group.call_args_list, [
            call(self.wx, "财务"), call(self.wx, "文件传输助手", exact=True),
        ])
        self.dismiss_search_box.assert_called_once_with()
        self.send_text_and_image.assert_called_once_with(self.wx, "本次说明", self.image)

    def test_target_search_exception_can_use_verified_fallback(self):
        self.open_group.side_effect = [RuntimeError("offline search failure"), "文件传输助手"]
        self.assertEqual(self.deliver(), "文件传输助手")
        self.open_group.assert_called_with(self.wx, "文件传输助手", exact=True)
        self.send_text_and_image.assert_called_once_with(self.wx, "本次说明", self.image)

    def test_no_target_and_no_fallback_never_sends(self):
        self.open_group.side_effect = ["", ""]
        with self.assertRaisesRegex(RuntimeError, "目标群与兜底群都无法打开"):
            self.deliver()
        self.send_text_and_image.assert_not_called()

    def test_fallback_search_exception_never_sends(self):
        self.open_group.side_effect = ["", RuntimeError("offline fallback failure")]
        with self.assertRaisesRegex(RuntimeError, "目标群与兜底群都无法打开"):
            self.deliver()
        self.send_text_and_image.assert_not_called()

    def test_send_failure_does_not_trigger_duplicate_fallback_delivery(self):
        self.open_group.return_value = "示例公司财务群"
        self.send_text_and_image.side_effect = RuntimeError("offline send failure")
        with self.assertRaisesRegex(RuntimeError, "offline send failure"):
            self.deliver()
        self.open_group.assert_called_once_with(self.wx, "财务")
        self.send_text_and_image.assert_called_once()


if __name__ == "__main__":
    unittest.main()
