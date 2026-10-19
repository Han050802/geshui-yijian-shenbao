"""Offline regressions for selecting and verifying a WeChat destination.

All platform, screenshot, click, clipboard-adjacent and send entry points are
mocked. These tests never connect to WeChat or send a real message.
"""

import ctypes
import importlib.util
from pathlib import Path
import sys
import tempfile
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
REAL_SEND_TEXT_AND_IMAGE = report.send_text_and_image


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
    def assert_selection(self, rows, keyword, expected, *, exact=False, group_only=False, ambiguous=False):
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
                    report._search_result_to_click(root, keyword, exact=exact, group_only=group_only)
            else:
                actual = report._search_result_to_click(root, keyword, exact=exact, group_only=group_only)
                self.assertIs(actual, None if expected is None else controls[expected])
        with self.subTest(source="ocr"):
            # OCR providers need not return their lines in visual order.
            if ambiguous:
                with self.assertRaises(report.AmbiguousChatError):
                    report._result_to_click(list(reversed(lines)), keyword, 60, exact=exact, group_only=group_only)
            else:
                actual = report._result_to_click(list(reversed(lines)), keyword, 60, exact=exact, group_only=group_only)
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

    def test_fallback_repeated_contact_name_is_ambiguous(self):
        self.assert_selection([
            ("section", "联系人"), ("result", "王书涵"),
            ("result", "王书涵"), ("result", "王书涵"),
        ], "王书涵", None, exact=True, ambiguous=True)

    def test_repeated_fallback_names_without_category_are_ambiguous(self):
        self.assert_selection([
            ("result", "王书涵"), ("result", "王书涵"), ("result", "王书涵"),
        ], "王书涵", None, exact=True, ambiguous=True)

    def test_fallback_repeated_full_group_name_is_ambiguous(self):
        self.assert_selection([
            ("section", "群聊"), ("result", "财务兜底群"), ("result", "财务兜底群"),
        ], "财务兜底群", None, exact=True, ambiguous=True)

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

    def test_group_only_rejects_same_name_contact_and_uncategorized_result(self):
        for section in ("联系人", "功能", "最常使用", "最近使用", "未知栏目"):
            with self.subTest(section=section):
                self.assert_selection([
                    ("section", section), ("result", "示例公司财务群"),
                ], "示例公司财务群", None, exact=True, group_only=True)
        self.assert_selection([("result", "示例公司财务群")],
                              "示例公司财务群", None, exact=True, group_only=True)

    def test_group_only_exact_name_does_not_use_company_keyword(self):
        self.assert_selection([
            ("section", "群聊"), ("result", "示例公司财务群"),
        ], "示例公司", None, exact=True, group_only=True)

    def test_group_only_accepts_unique_full_group_even_with_same_name_contact(self):
        self.assert_selection([
            ("section", "联系人"), ("result", "示例公司财务群"),
            ("section", "群聊"), ("result", "示例公司财务群"),
        ], "示例公司财务群", 3, exact=True, group_only=True)

    def test_group_only_rejects_duplicate_full_group_names(self):
        self.assert_selection([
            ("section", "群聊"), ("result", "示例公司财务群"), ("result", "示例公司财务群"),
        ], "示例公司财务群", None, exact=True, group_only=True, ambiguous=True)

    def test_group_only_refuses_matching_row_when_more_groups_are_collapsed(self):
        for kind in ("section", "result"):
            for more in ("查看全部群聊", "查看更多", "更多群聊"):
                with self.subTest(kind=kind, more=more):
                    self.assert_selection([
                        ("section", "群聊"), ("result", "示例公司财务群"), (kind, more),
                    ], "示例公司财务群", None, exact=True, group_only=True, ambiguous=True)

    def test_group_only_refuses_incomplete_declared_group_results(self):
        for section in ("群聊(3)", "群聊（3）", "群聊3"):
            with self.subTest(section=section):
                self.assert_selection([
                    ("section", section), ("result", "示例公司财务群"),
                    ("result", "示例公司财务群旧群"),
                ], "示例公司财务群", None, exact=True, group_only=True, ambiguous=True)

    def test_group_only_accepts_complete_count_and_ignores_hidden_contacts(self):
        self.assert_selection([
            ("section", "联系人"), ("result", "示例公司财务群"),
            ("section", "查看更多联系人"),
            ("section", "群聊（2）"), ("result", "示例公司财务群"),
            ("result", "示例公司财务群旧群"),
        ], "示例公司财务群", 4, exact=True, group_only=True)

    def test_group_only_preserves_internal_spaces_and_punctuation(self):
        for expected, actual in (("A B", "AB"), ("AB", "A B"),
                                 ("A B", "A  B"), ("A-B", "A—B")):
            with self.subTest(expected=expected, actual=actual):
                self.assert_selection([
                    ("section", "群聊"), ("result", actual),
                ], expected, None, exact=True, group_only=True)
        self.assert_selection([
            ("section", "群聊"), ("result", "A B"),
        ], " A B ", 1, exact=True, group_only=True)

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

    def test_same_full_name_in_distinct_sections_needs_stable_identity(self):
        for exact in (False, True):
            with self.subTest(exact=exact):
                self.assert_selection([
                    ("section", "最常使用"), ("result", "文件传输助手"),
                    ("section", "联系人"), ("result", "文件传输助手"),
                ], "文件传输助手", None, exact=exact, ambiguous=True)

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
    def test_strict_ocr_cannot_treat_literal_numeric_suffix_as_member_decoration(self):
        for actual, expected in (("财务群(2)", "财务群"),
                                 ("财务群（ 2 ）", "财务群"),
                                 ("财务群(2)", "财务群(2)"),
                                 ("财务群(2)(5)", "财务群(2)")):
            with self.subTest(actual=actual, expected=expected), patch.object(
                report, "_read_control_chat_title", return_value=""
            ), patch.object(report, "_read_screen_chat_title", return_value=actual):
                self.assertFalse(report._chat_title_matches(Mock(), 123, expected, strict=True))

    def test_strict_ocr_full_title_without_ambiguous_numeric_suffix_matches(self):
        with patch.object(report, "_read_control_chat_title", return_value=""), patch.object(
            report, "_read_screen_chat_title", return_value="A B 财务群"
        ):
            self.assertTrue(report._chat_title_matches(Mock(), 123, "A B 财务群", strict=True))

    def test_strict_uia_title_keeps_numeric_suffix_as_part_of_real_group_name(self):
        field = Mock(Name="财务群(2)")
        field.Exists.return_value = True
        root = Mock()
        root.EditControl.return_value = field
        auto = Mock()
        auto.ControlFromHandle.return_value = root
        with patch.dict(sys.modules, {"wxauto4.uia": SimpleNamespace(uiautomation=auto)}):
            self.assertFalse(report._chat_title_matches(Mock(), 123, "财务群", strict=True))
            self.assertTrue(report._chat_title_matches(Mock(), 123, "财务群(2)", strict=True))

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

    def test_strict_title_preserves_spaces_and_allows_member_count(self):
        for actual, expected, matches in (
            ("A B", "AB", False), ("AB", "A B", False),
            ("A  B", "A B", False), ("A B（ 12 ）", "A B", True),
            ("A B旧群（12）", "A B", False), (" A B ", "A B", True),
        ):
            with self.subTest(actual=actual, expected=expected):
                self.assertEqual(report._same_chat_title(actual, expected, strict=True), matches)

    def test_strict_verification_reads_live_title_instead_of_cached_match(self):
        wx = Mock()
        wx.ChatInfo.return_value = {"chat_name": "A B"}
        with patch.object(report, "_read_control_chat_title", return_value="AB") as read:
            self.assertFalse(report._chat_title_matches(wx, 123, "A B", strict=True))
            read.assert_called_once_with(123)
            wx.ChatInfo.assert_not_called()

    def test_strict_live_title_ocr_keeps_internal_spaces(self):
        image = Mock(width=1000)
        with patch.dict(sys.modules, {"wxauto4.uia": SimpleNamespace(uiautomation=Mock())}), patch.object(
            report, "_current_chat_title", return_value=""
        ), patch.object(report, "_capture_wechat", return_value=(image, 0, 0)), patch.object(
            report, "_ocr_lines", return_value=[{"text": "A B"}]
        ) as ocr:
            self.assertEqual(report._read_live_chat_title(123, strict=True), "A B")
            ocr.assert_called_once_with(image.crop.return_value, preserve_spacing=True)


class OCRSpacingTests(OfflineTestCase):
    def test_strict_ocr_uses_original_line_spacing_and_legacy_keeps_behavior(self):
        recognized = {
            "lines": [{"text": " A B ", "words": [
                {"text": "A", "bounding_rect": {"x": 1, "y": 2, "width": 3, "height": 4}},
                {"text": "B", "bounding_rect": {"x": 10, "y": 2, "width": 3, "height": 4}},
            ]}],
        }
        fake_ocr = SimpleNamespace(recognize_pil_sync=Mock(return_value=recognized))
        with patch.dict(sys.modules, {"winocr": fake_ocr}):
            self.assertEqual(report._ocr_lines(Mock(), preserve_spacing=True)[0]["text"], "A B")
            self.assertEqual(report._ocr_lines(Mock())[0]["text"], "AB")

    def test_strict_ocr_missing_original_line_does_not_invent_word_spacing(self):
        fake_ocr = SimpleNamespace(recognize_pil_sync=Mock(return_value={
            "lines": [{"words": [{"text": "AB", "bounding_rect": {
                "x": 1, "y": 2, "width": 3, "height": 4,
            }}]}],
        }))
        with patch.dict(sys.modules, {"winocr": fake_ocr}):
            self.assertEqual(report._ocr_lines(Mock(), preserve_spacing=True), [])


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
        self.assertEqual(report.open_group(self.wx, "示例公司财务群", exact=True, group_only=True), "示例公司财务群")
        self._open_group_from_screen.assert_not_called()
        self.clipboard.copy.assert_has_calls([call("示例公司财务群"), call("original clipboard")])
        self._open_group_by_controls.assert_called_once_with(
            self.wx, 123, "示例公司财务群", exact=True, group_only=True
        )

    def test_control_ambiguity_cannot_retry_with_less_complete_ocr_results(self):
        self._open_group_by_controls.side_effect = report.AmbiguousChatError("two groups")
        self._open_group_from_screen.return_value = "示例公司财务群"
        self.assertEqual(report.open_group(self.wx, "示例公司财务群", exact=True, group_only=True), "")
        self._open_group_from_screen.assert_not_called()
        self.clipboard.copy.assert_called_with("original clipboard")

    def test_exact_mode_is_preserved_when_controls_need_ocr_fallback(self):
        self._open_group_from_screen.return_value = "文件传输助手"
        self.assertEqual(report.open_group(self.wx, "文件传输助手", exact=True), "文件传输助手")
        self._open_group_by_controls.assert_called_once_with(self.wx, 123, "文件传输助手", exact=True, group_only=False)
        self._open_group_from_screen.assert_called_once_with(self.wx, 123, "文件传输助手", exact=True, group_only=False)
        self.clipboard.copy.assert_called_with("original clipboard")

    def test_exact_group_only_is_preserved_when_controls_need_ocr_fallback(self):
        self._open_group_from_screen.return_value = "示例公司财务群"
        self.assertEqual(report.open_group(
            self.wx, "示例公司财务群", exact=True, group_only=True
        ), "示例公司财务群")
        for channel in (self._open_group_by_controls, self._open_group_from_screen):
            channel.assert_called_once_with(
                self.wx, 123, "示例公司财务群", exact=True, group_only=True
            )


class SearchOnlyTests(OfflineTestCase):
    def test_search_only_opens_exact_group_without_sending_or_capturing_receipt(self):
        wx = Mock()
        with patch.object(report, "_create_wechat_client", return_value=wx), patch.object(
            report, "open_group", return_value="示例公司财务群"
        ) as open_chat:
            self.assertEqual(report.search_group(" 示例公司财务群 "), "示例公司财务群")
            open_chat.assert_called_once_with(wx, "示例公司财务群", exact=True, group_only=True)

    def test_invalid_search_name_never_connects_to_wechat(self):
        with patch.object(report, "_create_wechat_client") as create:
            for value in ("", "  ", None, "请修改为目标微信群名称"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    report.search_group(value)
            create.assert_not_called()


class DeliveryTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.wx = Mock()
        self.image = Path("offline-screenshot.png")
        for name in ("open_group", "send_text_and_image", "dismiss_search_box"):
            guard = patch.object(report, name)
            setattr(self, name, guard.start())
            self.addCleanup(guard.stop)
        self.send_text_and_image.side_effect = self.simulated_send

    @staticmethod
    def simulated_send(*_args, before_send=None, **_kwargs):
        if before_send:
            before_send()

    def deliver(self):
        return report.deliver(self.wx, "本次说明", self.image, "示例公司财务群", "文件传输助手")

    def test_target_success_returns_full_destination_and_sends_once(self):
        self.open_group.return_value = "示例公司财务群"
        self.assertEqual(self.deliver(), "示例公司财务群")
        self.open_group.assert_called_once_with(self.wx, "示例公司财务群", exact=True, group_only=True)
        self.send_text_and_image.assert_called_once_with(
            self.wx, "本次说明", self.image, destination="示例公司财务群",
            group_only=True, before_send=None,
        )
        self.dismiss_search_box.assert_not_called()

    def test_target_miss_opens_fallback_with_exact_name_only(self):
        self.open_group.side_effect = ["", "文件传输助手"]
        self.assertEqual(self.deliver(), "文件传输助手")
        self.assertEqual(self.open_group.call_args_list, [
            call(self.wx, "示例公司财务群", exact=True, group_only=True), call(self.wx, "文件传输助手", exact=True),
        ])
        self.dismiss_search_box.assert_called_once_with()
        self.send_text_and_image.assert_called_once_with(
            self.wx, "本次说明", self.image, destination="文件传输助手", before_send=None,
        )

    def test_target_search_exception_can_use_verified_fallback(self):
        self.open_group.side_effect = [RuntimeError("offline search failure"), "文件传输助手"]
        self.assertEqual(self.deliver(), "文件传输助手")
        self.open_group.assert_called_with(self.wx, "文件传输助手", exact=True)
        self.send_text_and_image.assert_called_once_with(
            self.wx, "本次说明", self.image, destination="文件传输助手", before_send=None,
        )

    def test_no_target_and_no_fallback_never_sends(self):
        self.open_group.side_effect = ["", ""]
        with self.assertRaisesRegex(RuntimeError, "目标群与兜底群都无法打开"):
            self.deliver()
        self.send_text_and_image.assert_not_called()

    def test_excel_target_miss_stops_without_optional_fallback(self):
        self.open_group.return_value = ""
        with self.assertRaisesRegex(RuntimeError, "已停止发送"):
            report.deliver(self.wx, "本次说明", self.image, "示例公司财务群", "")
        self.open_group.assert_called_once_with(
            self.wx, "示例公司财务群", exact=True, group_only=True
        )
        self.send_text_and_image.assert_not_called()

    def test_optional_empty_fallback_is_valid(self):
        report.ensure_group_names("示例公司财务群", "")

    def test_explicit_fallback_must_be_valid_and_distinct(self):
        for fallback in ("示例公司财务群", "请修改为兜底群", None):
            with self.subTest(fallback=fallback), self.assertRaises(ValueError):
                report.ensure_group_names("示例公司财务群", fallback)

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
        self.open_group.assert_called_once_with(self.wx, "示例公司财务群", exact=True, group_only=True)
        self.send_text_and_image.assert_called_once()

    def test_before_send_runs_once_immediately_before_first_send(self):
        self.open_group.return_value = "示例公司财务群"
        sequence = []
        def send(*args, **kwargs):
            self.simulated_send(*args, **kwargs)
            sequence.append("send")
        self.send_text_and_image.side_effect = send
        report.deliver(self.wx, "说明", self.image, "示例公司财务群", "",
                       before_send=lambda: sequence.append("attempted"))
        self.assertEqual(sequence, ["attempted", "send"])

    def test_target_miss_never_marks_delivery_attempted(self):
        self.open_group.return_value = ""
        before = Mock()
        with self.assertRaises(RuntimeError):
            report.deliver(self.wx, "说明", self.image, "A B", "", before_send=before)
        before.assert_not_called()
        self.send_text_and_image.assert_not_called()

    def test_similar_name_differing_only_by_space_never_reaches_send(self):
        before = Mock()
        self.open_group.side_effect = lambda wx, expected, **kwargs: report._choose_chat_result(
            [("群聊", "AB", "AB")], expected, **kwargs
        ) or ""
        with self.assertRaisesRegex(RuntimeError, "已停止发送"):
            report.deliver(self.wx, "说明", self.image, "A B", "", before_send=before)
        before.assert_not_called()
        self.send_text_and_image.assert_not_called()

    def test_optional_fallback_invokes_before_send_only_once(self):
        self.open_group.side_effect = ["", "文件传输助手"]
        before = Mock()
        report.deliver(self.wx, "说明", self.image, "示例公司财务群", "文件传输助手",
                       before_send=before)
        before.assert_called_once_with()
        self.send_text_and_image.assert_called_once()

    def test_callback_failure_prevents_send(self):
        self.open_group.return_value = "示例公司财务群"
        before = Mock(side_effect=RuntimeError("record failed"))
        with self.assertRaisesRegex(RuntimeError, "record failed"):
            report.deliver(self.wx, "说明", self.image, "示例公司财务群", "", before_send=before)
        before.assert_called_once_with()

    def test_sending_failure_keeps_attempt_marked_and_cannot_fallback(self):
        self.open_group.return_value = "示例公司财务群"
        def partial_send(*args, **kwargs):
            self.simulated_send(*args, **kwargs)
            raise RuntimeError("partial send")
        self.send_text_and_image.side_effect = partial_send
        before = Mock()
        with self.assertRaisesRegex(RuntimeError, "partial send"):
            report.deliver(self.wx, "说明", self.image, "示例公司财务群", "文件传输助手",
                           before_send=before)
        before.assert_called_once_with()
        self.open_group.assert_called_once()


class ExecuteBeforeSendTests(OfflineTestCase):
    def test_capture_failure_does_not_mark_delivery_attempted(self):
        before = Mock()
        with patch.object(report, "capture_screen", side_effect=RuntimeError("no tax window")):
            with self.assertRaisesRegex(RuntimeError, "no tax window"):
                report.execute_report("A B", "", "说明", before_send=before)
        before.assert_not_called()

    def test_client_connection_failure_does_not_mark_delivery_attempted(self):
        before = Mock()
        with patch.object(report, "capture_screen", return_value=Path("offline.png")), patch.object(
            report, "_create_wechat_client", side_effect=RuntimeError("not logged in")
        ), patch.object(report, "delete_screenshot"):
            with self.assertRaisesRegex(RuntimeError, "not logged in"):
                report.execute_report("A B", "", "说明", before_send=before)
        before.assert_not_called()

    def test_execute_passes_callback_to_delivery(self):
        before, wx, image = Mock(), Mock(), Path("offline.png")
        with patch.object(report, "capture_screen", return_value=image), patch.object(
            report, "_create_wechat_client", return_value=wx
        ), patch.object(report, "delete_screenshot"), patch.object(
            report, "deliver", return_value="A B"
        ) as deliver:
            self.assertEqual(report.execute_report("A B", "", "说明", before_send=before), "A B")
        deliver.assert_called_once_with(wx, "说明", image, "A B", "", before_send=before)


class DeliveryDestinationGuardTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.image = Path(directory.name) / "offline.png"
        self.image.write_bytes(b"offline image placeholder")
        self.wx = Mock(spec=["SendMsg", "SendFiles"])
        self.wx.SendMsg.return_value = None
        self.wx.SendFiles.return_value = None
        self.group = "甲公司财务群"
        self.current_title = self.group
        self.before_send = Mock()
        ctypes.windll.user32.GetForegroundWindow.return_value = 123
        for name, kwargs in (
            ("send_text_and_image", {"side_effect": REAL_SEND_TEXT_AND_IMAGE}),
            ("open_group", {"return_value": self.group}),
            ("find_wechat_main_window", {"return_value": (123, "wechat.exe")}),
            ("_chat_title_matches", {"side_effect": lambda _wx, _hwnd, expected, **_kwargs:
                self.current_title == expected}),
        ):
            guard = patch.object(report, name, **kwargs)
            guard.start()
            self.addCleanup(guard.stop)

    def deliver(self, text="本次申报说明"):
        return report.deliver(self.wx, text, self.image, self.group, "",
                              before_send=self.before_send)

    def test_switch_during_pre_send_delay_stops_before_first_message(self):
        def switch(_seconds):
            self.current_title = "乙公司财务群"
        report.time.sleep.side_effect = switch
        with self.assertRaisesRegex(RuntimeError, "会话.*变化"):
            self.deliver()
        self.wx.SendMsg.assert_not_called()
        self.wx.SendFiles.assert_not_called()
        self.before_send.assert_not_called()

    def test_switch_between_text_and_image_stops_image_without_resending_text(self):
        def switch(seconds):
            if seconds == report.BETWEEN_SEND_SECONDS:
                self.current_title = "乙公司财务群"
        report.time.sleep.side_effect = switch
        with self.assertRaisesRegex(RuntimeError, "会话.*变化"):
            self.deliver()
        self.wx.SendMsg.assert_called_once_with("本次申报说明")
        self.wx.SendFiles.assert_not_called()
        self.before_send.assert_called_once_with()

    def test_lost_foreground_before_image_only_send_stops_without_attempt_marker(self):
        def lose_focus(_seconds):
            ctypes.windll.user32.GetForegroundWindow.return_value = 999
        report.time.sleep.side_effect = lose_focus
        with self.assertRaisesRegex(RuntimeError, "微信.*前台"):
            self.deliver(text="")
        self.wx.SendMsg.assert_not_called()
        self.wx.SendFiles.assert_not_called()
        self.before_send.assert_not_called()

    def test_valid_destination_sends_text_and_image_and_marks_attempt_once(self):
        self.assertEqual(self.deliver(), self.group)
        self.wx.SendMsg.assert_called_once_with("本次申报说明")
        self.wx.SendFiles.assert_called_once_with(str(self.image.resolve()))
        self.before_send.assert_called_once_with()

    def test_switch_while_before_send_callback_reads_excel_stops_actual_send(self):
        def switch():
            self.current_title = "乙公司财务群"
        self.before_send.side_effect = switch
        with self.assertRaisesRegex(RuntimeError, "会话.*变化"):
            self.deliver()
        self.wx.SendMsg.assert_not_called()
        self.wx.SendFiles.assert_not_called()
        self.before_send.assert_called_once_with()

    def test_callback_failure_never_enters_wechat_send_api(self):
        self.before_send.side_effect = RuntimeError("Excel 已变化")
        with self.assertRaisesRegex(RuntimeError, "Excel 已变化"):
            self.deliver()
        self.wx.SendMsg.assert_not_called()
        self.wx.SendFiles.assert_not_called()


if __name__ == "__main__":
    unittest.main()

