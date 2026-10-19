# -*- coding: utf-8 -*-
"""无人值守任务完成后截屏，并把说明文字与截图发到指定微信群。

检索不到目标群时熔断，自动改发到兜底群，正文仍只发原来的说明。

依赖安装：
    pip install -r requirements.txt

运行前请先登录并打开微信电脑版。目标支持群名关键词，兜底请填写完整名称：
    python wechat_report.py --target "记账核对群" --fallback "文件传输助手"

也可不传参数，直接修改下方 TARGET_GROUP / FALLBACK_GROUP。
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import re
import sys
import tempfile
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

from PIL import ImageGrab



def _enable_per_monitor_dpi() -> None:
    """截图和点击都要用屏幕真实像素，否则搜索框会点偏。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


_enable_per_monitor_dpi()


# ---------------------------------------------------------------------------
# 运行配置：目标支持唯一的群名关键词；兜底使用完整会话名称
# ---------------------------------------------------------------------------
TARGET_GROUP = os.environ.get("WECHAT_TARGET_GROUP", "请修改为目标微信群名称")
FALLBACK_GROUP = os.environ.get("WECHAT_FALLBACK_GROUP", "请修改为兜底备用微信群名称")

BASE_DIR = Path(__file__).resolve().parent

# DirectUI 渲染与输入法切换的等待，避免窗口还没出来就发送
IME_SETTLE_SECONDS = 0.3
SEARCH_RENDER_SECONDS = 0.5
BEFORE_SEND_SECONDS = 0.4
BETWEEN_SEND_SECONDS = 0.5

WM_INPUTLANGCHANGEREQUEST = 0x0050
KLF_ACTIVATE = 0x00000001
VK_ESCAPE = 0x1B
KEYEVENTF_KEYUP = 0x0002
ENGLISH_LAYOUT_ID = "00000409"

logger = logging.getLogger("wechat_report")




def load_wechat_class():
    """优先使用适配微信 4.x 的 wxauto4，没有时再退回旧版 wxauto。"""
    try:
        from wxauto4 import WeChat
    except ImportError:
        try:
            from wxauto import WeChat
        except ImportError as exc:
            raise RuntimeError(
                "未安装微信自动化依赖。请先双击「微信截图回执.bat」，或执行：pip install -r requirements.txt"
            ) from exc
    return WeChat


def configure_wxauto() -> None:
    """放宽搜索等待，避免会话列表还没出来就被判定为找不到。"""
    wx_param = None
    for module_name in ("wxauto4", "wxauto4.param", "wxauto", "wxauto.param"):
        try:
            module = __import__(module_name, fromlist=["WxParam"])
        except ImportError:
            continue
        wx_param = getattr(module, "WxParam", None)
        if wx_param is not None:
            break
    if wx_param is not None and hasattr(wx_param, "SEARCH_CHAT_TIMEOUT"):
        wx_param.SEARCH_CHAT_TIMEOUT = max(int(wx_param.SEARCH_CHAT_TIMEOUT), 5)


def setup_logging() -> None:
    # Windows 控制台默认不是 UTF-8，不切换的话中文日志会乱码
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except Exception:
                pass
    handlers: list[logging.Handler] = [
        logging.FileHandler(BASE_DIR / "app.log", encoding="utf-8")
    ]
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )
    # wxauto 导入时会清除根日志处理器。把本模块的文件日志挂在自身 logger
    # 上，保留每次搜索、标题确认和失败原因，便于现场排查。
    file_handler = handlers[0]
    logging.getLogger().removeHandler(file_handler)
    for previous in list(logger.handlers):
        if isinstance(previous, logging.FileHandler) and previous.baseFilename == file_handler.baseFilename:
            logger.removeHandler(previous)
            previous.close()
    logger.addHandler(file_handler)
    logger.setLevel(logging.INFO)


def dismiss_search_box() -> None:
    """关掉可能残留的搜索下拉，避免下一次检索点到上一次的结果。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.keybd_event(VK_ESCAPE, 0, 0, 0)
    user32.keybd_event(VK_ESCAPE, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(IME_SETTLE_SECONDS)


def _withholding_window_corners(hwnd: int) -> tuple[int, int, int, int]:
    """返回扣缴端窗口可见区域的左、上、右、下，也就是四个角点。"""
    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)
        time.sleep(0.4)
    rect = wintypes.RECT()
    hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect))
    if hr != 0 or rect.right <= rect.left or rect.bottom <= rect.top:
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)


def find_withholding_window() -> int | None:
    """定位自然人电子税务局（扣缴端）主窗口，避开 0 尺寸的程序占位窗。"""
    user32 = ctypes.windll.user32
    found: list[tuple[int, int, int]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        if "扣缴端" not in _window_text(int(hwnd)):
            return True
        if _window_class(int(hwnd)) == "TApplication":
            return True
        left, top, right, bottom = _withholding_window_corners(int(hwnd))
        area = max(0, right - left) * max(0, bottom - top)
        if area <= 0:
            return True
        preferred = 0 if _window_class(int(hwnd)) == "Tfrm_MainFrame" else 1
        found.append((preferred, -area, int(hwnd)))
        return True

    user32.EnumWindows(callback, 0)
    if not found:
        return None
    found.sort()
    return found[0][2]


def capture_screen() -> Path:
    """只截扣缴端窗口。先取四个角点，再按这个矩形截图。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    hwnd = find_withholding_window()
    if hwnd is None:
        raise RuntimeError("没有找到「自然人电子税务局（扣缴端）」窗口。请先打开扣缴端，并让它显示在屏幕上。")
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, 9)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)
    left, top, right, bottom = _withholding_window_corners(hwnd)
    logger.info(
        "扣缴端四角：左上(%s,%s) 右上(%s,%s) 右下(%s,%s) 左下(%s,%s)",
        left,
        top,
        right,
        top,
        right,
        bottom,
        left,
        bottom,
    )
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    image_path = (Path(tempfile.gettempdir()) / f"wechat_report_{stamp}.png").resolve()
    ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True).save(image_path, "PNG")
    logger.info("已按扣缴端窗口截图，尺寸 %s×%s", right - left, bottom - top)
    return image_path


def delete_screenshot(image_path: Path | None) -> None:
    """微信读完截图后删除临时文件，不留在本地文件夹。"""
    if image_path is None:
        return
    time.sleep(1.0)
    try:
        image_path.unlink(missing_ok=True)
        logger.info("临时截图已删除")
    except OSError:
        logger.exception("删除临时截图失败")


def _status_text(result: object) -> str:
    if isinstance(result, dict):
        return str(result.get("status", "")).strip()
    return str(getattr(result, "status", "") or "").strip()


def _is_explicit_failure(result: object) -> bool:
    """明确失败才返回 True。None 表示接口无返回，留给标题核对，不在这里直接判失败。"""
    if result is None or result is True:
        return False
    if result is False:
        return True

    status = _status_text(result)
    # wxauto4 的 WxResponse 失败状态是「失败」或「错误」
    if status in {"失败", "错误", "failed", "fail", "error"}:
        return True
    if isinstance(result, dict) and "success" in result and not result["success"]:
        return True

    is_success = getattr(result, "is_success", None)
    if is_success is False:
        return True

    # 假值 WxResponse / False：检索失败。字符串是旧版返回的会话名，不能按假值处理
    if not isinstance(result, str):
        try:
            if not result:
                return True
        except Exception:
            return False
    return False


def _is_explicit_success(result: object) -> bool:
    if result is True:
        return True
    if _status_text(result) == "成功":
        return True
    return getattr(result, "is_success", None) is True


def judge_open_result(group_name: str, chat_result: object, chat_name: str) -> bool:
    """判断是否已经打开指定群。返回 False 时调用方必须熔断，不能对当前窗口发送。

    关键分支：
    - 返回 False，或 WxResponse 状态为失败/错误：没找到目标群。
    - 抛出异常：由 open_group / deliver 捕获，同样按没找到处理。
    - 无返回（None）且读不到会话标题：无法证明已进入目标群，判失败。
    - 无返回但当前标题已经等于目标群：检索实际成功，允许发送。
    - 标题是别的会话：即使返回值看起来成功，也判失败，避免发错群。
    """
    expected = group_name.strip()
    actual = (chat_name or "").strip()

    if _is_explicit_failure(chat_result):
        return False

    # 旧版 ChatWith 成功时返回匹配到的会话名；名字不同说明打开错了
    if isinstance(chat_result, str) and chat_result.strip() not in {"", expected}:
        return False

    if actual:
        return actual == expected

    # 标题为空时，只有接口明确成功才放行；完全无返回则熔断
    if chat_result is None:
        return False
    if isinstance(chat_result, str) and chat_result.strip() == expected:
        return True
    return _is_explicit_success(chat_result)


def read_chat_name(wx) -> str:
    info_fn = getattr(wx, "ChatInfo", None)
    if not callable(info_fn):
        return ""
    info = info_fn()
    if isinstance(info, dict):
        return str(info.get("chat_name") or info.get("name") or "").strip()
    return str(getattr(info, "chat_name", "") or "").strip()


def chat_with(wx, group_name: str) -> object:
    """调用检索。exact=True 避免模糊命中其他群；旧版接口没有 exact 参数时自动回退。"""
    try:
        return wx.ChatWith(group_name, exact=True)
    except TypeError as exc:
        # 只有旧版签名不认识 exact 时才降级调用，其它 TypeError 继续抛出
        if "exact" not in str(exc):
            raise
        return wx.ChatWith(group_name)


def _press_ctrl(key_code: int) -> None:
    user32 = ctypes.windll.user32
    user32.keybd_event(0x11, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(key_code, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(key_code, 0, 2, 0)
    time.sleep(0.05)
    user32.keybd_event(0x11, 0, 2, 0)



def _clean_chat_title(name: str) -> str:
    title = (name or "").strip()
    for suffix in ("按住鼠标 语音输入文字", "按住鼠标语音输入文字"):
        if title.endswith(suffix):
            title = title[: -len(suffix)].strip()
    return title


def _current_chat_title(root) -> str:
    field = root.EditControl(AutomationId="chat_input_field", searchDepth=40)
    if field.Exists(0.4):
        return _clean_chat_title(field.Name or "")
    return ""







def _show_wechat(hwnd: int) -> bool:
    """把微信放到最前。点屏幕前必须确认它真的在最前，避免点到别的窗口。"""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.ShowWindow(hwnd, 9)
    current_tid = kernel32.GetCurrentThreadId()
    foreground = user32.GetForegroundWindow()
    foreground_tid = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    target_tid = user32.GetWindowThreadProcessId(hwnd, None)
    if foreground_tid and foreground_tid != current_tid:
        user32.AttachThreadInput(current_tid, foreground_tid, True)
    if target_tid and target_tid != current_tid:
        user32.AttachThreadInput(current_tid, target_tid, True)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        # 先短暂置顶再取消，才能从别的程序下面抬起来。
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002)
        user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0001 | 0x0002)
    finally:
        if target_tid and target_tid != current_tid:
            user32.AttachThreadInput(current_tid, target_tid, False)
        if foreground_tid and foreground_tid != current_tid:
            user32.AttachThreadInput(current_tid, foreground_tid, False)
    time.sleep(0.2)
    return user32.GetForegroundWindow() == hwnd



def _click_screen(x: int, y: int) -> None:
    user32 = ctypes.windll.user32
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.04)
    user32.mouse_event(0x0004, 0, 0, 0, 0)


def _capture_wechat(hwnd: int):
    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    image = ImageGrab.grab(
        bbox=(int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)),
        all_screens=True,
    )
    return image, int(rect.left), int(rect.top)


def _search_box_bounds(image) -> tuple[int, int, int, int] | None:
    """在左侧会话栏里找白色搜索框，保留实际左右边界。"""
    rgb = image.convert("RGB")
    width, height = rgb.size
    limit_x = min(width - 8, 430)
    rows: list[tuple[int, int, int]] = []
    for y in range(24, min(120, height)):
        start = None
        best_len = 0
        best_span = (0, 0)
        for x in range(48, limit_x):
            red, green, blue = rgb.getpixel((x, y))
            bright = red >= 246 and green >= 246 and blue >= 246
            if bright and start is None:
                start = x
            elif not bright and start is not None:
                length = x - start
                if length > best_len:
                    best_len = length
                    best_span = (start, x)
                start = None
        if start is not None and limit_x - start > best_len:
            best_len = limit_x - start
            best_span = (start, limit_x)
        if best_len >= 140:
            rows.append((y, best_span[0], best_span[1]))
    if not rows:
        return None
    bands: list[list[tuple[int, int, int]]] = []
    current = [rows[0]]
    for row in rows[1:]:
        if row[0] <= current[-1][0] + 2:
            current.append(row)
        else:
            bands.append(current)
            current = [row]
    bands.append(current)
    # 搜索框是一条细白条。下拉面板和聊天区也是白色，不能把它们算进去。
    thin = [band for band in bands if 12 <= len(band) <= 42]
    best_band = thin[0] if thin else None
    if best_band is None:
        return None
    top = best_band[0][0]
    bottom = best_band[-1][0]
    left = min(item[1] for item in best_band)
    right = max(item[2] for item in best_band)
    return left, top, right, bottom


def _search_box_center(image) -> tuple[int, int, int] | None:
    box = _search_box_bounds(image)
    if box is None:
        return None
    left, top, right, bottom = box
    return (left + right) // 2, (top + bottom) // 2, bottom


def _ocr_lines(image) -> list[dict]:
    import winocr

    result = winocr.recognize_pil_sync(image, lang="zh-Hans")
    lines: list[dict] = []
    for line in result.get("lines") or []:
        words = line.get("words") or []
        if not words:
            continue
        text = "".join(str(word.get("text") or "") for word in words).replace(" ", "")
        if not text:
            continue
        left = min(word["bounding_rect"]["x"] for word in words)
        top = min(word["bounding_rect"]["y"] for word in words)
        right = max(word["bounding_rect"]["x"] + word["bounding_rect"]["width"] for word in words)
        bottom = max(word["bounding_rect"]["y"] + word["bounding_rect"]["height"] for word in words)
        lines.append({"text": text, "x": left, "y": top, "right": right, "bottom": bottom})
    return lines













_CHAT_SECTIONS = {"群聊", "联系人", "功能", "最常使用", "最近使用"}
_SKIP_SECTIONS = {"聊天记录", "搜索网络结果", "网络搜索", "公众号", "小程序"}


class AmbiguousChatError(RuntimeError):
    """已经看到多个候选时，不能用另一识别通道漏读后的结果继续猜。"""


def _normalized_name(name: str) -> str:
    """忽略微信/OCR 插入的空白，不丢弃群名中的文字和标点。"""
    return re.sub(r"\s+", "", name or "")


def _search_section(name: str) -> str:
    name = _normalized_name(name)
    for section in _CHAT_SECTIONS | _SKIP_SECTIONS:
        if name == section or re.fullmatch(re.escape(section) + r"[（(]?\d+[）)]?", name):
            return section
    return name


def _choose_chat_result(candidates, group_name: str, *, exact: bool = False):
    """完整群名优先；关键词只在群聊内匹配，多个候选时不猜测。"""
    keyword = _normalized_name(group_name)
    if not keyword:
        return None
    # 微信部分版本不暴露分类标题，OCR 也可能漏读它。完整同名结果继续沿用
    # 原有检索行为；只有关键词包含匹配才必须明确位于「群聊」分类。
    valid = [entry for entry in candidates
             if entry[0] not in _SKIP_SECTIONS and not entry[0].startswith("查看全部")]
    groups = [entry for entry in valid if entry[0] == "群聊"]
    matches = [entry for entry in groups if _normalized_name(entry[1]) == keyword]
    if not matches and not exact:
        matches = [entry for entry in groups if keyword in _normalized_name(entry[1])]
    if not matches:
        # 联系人和文件传输助手等仍可用完整名称打开，不能用关键词模糊命中。
        matches = [entry for entry in valid if _normalized_name(entry[1]) == keyword]
    if len(matches) > 1:
        # 兜底已指定完整名称。微信搜索和 OCR 可能把同名会话重复展示/识别，
        # 沿用原版取首个完整同名结果，再由点击后的标题核验决定是否放行。
        if exact:
            return matches[0][2]
        # 同一联系人可能同时出现在常用/最近使用与联系人分类中。
        # 不同完整群名、同一分类下的多个同名群仍然必须停止。
        if (all(entry[0] != "群聊" for entry in matches)
                and len({entry[0] for entry in matches}) == len(matches)):
            return matches[0][2]
        names = " / ".join(entry[1] for entry in matches)
        raise AmbiguousChatError(f"关键词 {group_name} 命中多个会话，请填写更完整的群名：{names}")
    if not matches:
        logger.info("未命中 %s；已识别搜索项：%s", group_name,
                    " / ".join(f"[{entry[0]}]{entry[1]}" for entry in candidates[:15]))
    return matches[0][2] if matches else None


def _search_result_to_click(root, group_name: str, *, exact: bool = False):
    """从控件搜索结果中选择唯一群聊，兜底可限定完整名称。"""
    search_list = root.ListControl(AutomationId="search_list", searchDepth=40)
    if not search_list.Exists(1.2):
        return None
    section = ""
    candidates = []
    for item in search_list.GetChildren():
        name = (item.Name or "").strip()
        class_name = item.ClassName or ""
        if class_name == "mmui::XTableCell":
            section = _search_section(name)
            continue
        if class_name != "mmui::SearchContentCellView":
            continue
        candidates.append((section, name, item))
    return _choose_chat_result(candidates, group_name, exact=exact)


def _result_to_click(lines: list[dict], group_name: str, search_bottom: int,
                     *, exact: bool = False, search_right: int | None = None) -> dict | None:
    """只从左侧搜索结果的会话分类中选取名称，不匹配聊天记录。"""
    section = ""
    candidates = []
    for line in sorted(lines, key=lambda item: item["y"]):
        if line["bottom"] <= search_bottom + 8:
            continue
        if search_right is not None and line["x"] >= search_right:
            continue
        text = _search_section(line["text"])
        if text in _CHAT_SECTIONS or text in _SKIP_SECTIONS or text.startswith("查看全部"):
            section = "聊天记录" if text.startswith("查看全部") else text
            continue
        candidates.append((section, line["text"], line))
    return _choose_chat_result(candidates, group_name, exact=exact)


def _same_chat_title(actual: str, expected: str) -> bool:
    actual = _normalized_name(_clean_chat_title(actual))
    expected = _normalized_name(_clean_chat_title(expected))
    if not actual or not expected:
        return False
    if actual == expected:
        return True
    # 群聊标题可能追加人数；不允许把「旧群」等真实名称后缀当成装饰。
    return re.sub(r"[（(]\d+[）)]$", "", actual) == expected


def _chat_title_matches(wx, hwnd: int, group_name: str) -> bool:
    actual = _safe_chat_name(wx)
    if _same_chat_title(actual, group_name):
        return True
    # ChatInfo 可能仍缓存着点击前的会话名；名称不符时继续核对屏幕标题，
    # 不能因此把已经打开的目标和兜底会话一并判为失败。
    image, _left, _top = _capture_wechat(hwnd)
    # 保留原先可用的标题区域。搜索框最右端并不等于聊天区标题起点。
    header = image.crop((int(image.width * 0.36), 28, int(image.width * 0.78), 92))
    lines = _ocr_lines(header)
    screen_title = "".join(line["text"] for line in lines)
    matched = _same_chat_title(screen_title, group_name)
    if not matched:
        logger.debug("会话标题尚未匹配：缓存=%s，屏幕=%s，期望=%s", actual, screen_title, group_name)
    return matched


def _wait_for_chat_title(wx, hwnd: int, selected_name: str) -> bool:
    deadline = time.monotonic() + 3.0
    while True:
        if _chat_title_matches(wx, hwnd, selected_name):
            return True
        if time.monotonic() >= deadline:
            logger.warning("点击后未确认进入会话：%s", selected_name)
            return False
        time.sleep(0.25)


def _open_group_by_controls(wx, hwnd: int, group_name: str, *, exact: bool = False) -> str:
    """控件树完整时搜索关键词，点击唯一结果并核对完整会话名称。"""
    try:
        from wxauto4.uia import uiautomation as auto
    except ImportError:
        return ""
    root = auto.ControlFromHandle(hwnd)
    search_edit = root.EditControl(ClassName="mmui::XValidatorTextEdit", Name="搜索", searchDepth=30)
    if not search_edit.Exists(0.25):
        return ""
    search_edit.Click()
    time.sleep(0.2)
    _press_ctrl(0x41)
    time.sleep(0.05)
    _press_ctrl(0x56)
    time.sleep(0.7)
    root = auto.ControlFromHandle(hwnd)
    typed = ""
    search_edit = root.EditControl(ClassName="mmui::XValidatorTextEdit", searchDepth=30)
    if search_edit.Exists(0.2):
        try:
            typed = (search_edit.GetValuePattern().Value or "").strip()
        except Exception:
            typed = ""
    if _normalized_name(typed) != _normalized_name(group_name):
        return ""
    result = _search_result_to_click(root, group_name, exact=exact)
    if result is None:
        return ""
    selected_name = (result.Name or "").strip()
    if not _show_wechat(hwnd):
        return ""
    result.Click()
    time.sleep(0.45)
    opened = _wait_for_chat_title(wx, hwnd, selected_name)
    logger.info("搜索关键词 %s -> 命中 %s -> 打开=%s", group_name, selected_name, opened)
    return selected_name if opened else ""


def _open_group_from_screen(wx, hwnd: int, group_name: str, *, exact: bool = False) -> str:
    """控件树为空时，从搜索栏下方识别唯一群名并点击。"""
    if not _show_wechat(hwnd):
        logger.info("微信没有回到前台，取消点击搜索栏")
        return ""
    image, origin_x, origin_y = _capture_wechat(hwnd)
    box = _search_box_bounds(image)
    if box is None:
        logger.info("没有找到微信搜索栏")
        return ""
    search_left, search_top, search_right, search_bottom = box
    center_x = (search_left + search_right) // 2
    center_y = (search_top + search_bottom) // 2
    _click_screen(origin_x + center_x, origin_y + center_y)
    time.sleep(0.2)
    _press_ctrl(0x41)
    time.sleep(0.05)
    _press_ctrl(0x56)
    time.sleep(0.85)

    if not _show_wechat(hwnd):
        logger.info("微信没有回到前台，取消点击搜索结果")
        dismiss_search_box()
        return ""
    image, origin_x, origin_y = _capture_wechat(hwnd)
    # 按真实搜索栏边界过滤文字起点，不截断延伸到右边的长群名。
    result = _result_to_click(_ocr_lines(image), group_name, search_bottom,
                             exact=exact, search_right=search_right)
    if result is None:
        logger.info("搜索结果里没有可唯一确定的会话：%s", group_name)
        dismiss_search_box()
        return ""
    selected_name = result["text"].strip()
    click_x = origin_x + int((result["x"] + result["right"]) / 2)
    click_y = origin_y + int((result["y"] + result["bottom"]) / 2)
    _click_screen(click_x, click_y)
    time.sleep(0.55)
    opened = _wait_for_chat_title(wx, hwnd, selected_name)
    logger.info("搜索关键词 %s -> 命中 %s -> 打开=%s", group_name, selected_name, opened)
    if not opened:
        dismiss_search_box()
    return selected_name if opened else ""


def open_group(wx, group_name: str, *, exact: bool = False) -> str:
    """搜索并核对会话，成功返回完整名称，失败返回空字符串。"""
    found = find_wechat_main_window()
    if found is None:
        return ""
    hwnd = found[0]
    group_name = group_name.strip()
    if not group_name:
        return ""
    try:
        from wechat_a11y import prime_wechat_accessibility

        prime_wechat_accessibility(hwnd)
    except Exception:
        logger.exception("准备微信界面识别失败")
    _show_wechat(hwnd)
    if ctypes.windll.user32.GetForegroundWindow() != hwnd:
        logger.info("微信没有回到前台，取消点击搜索栏")
        return ""
    dismiss_search_box()

    import pyperclip

    previous_clipboard = None
    try:
        previous_clipboard = pyperclip.paste()
    except Exception:
        previous_clipboard = None
    try:
        pyperclip.copy(group_name)
        time.sleep(0.05)
        opened = _open_group_by_controls(wx, hwnd, group_name, exact=exact)
        if opened:
            logger.info("已通过控件打开 %s", opened)
            return opened
        dismiss_search_box()
        return _open_group_from_screen(wx, hwnd, group_name, exact=exact)
    except AmbiguousChatError as exc:
        logger.warning("%s", exc)
        dismiss_search_box()
        return ""
    except Exception:
        logger.exception("通过搜索栏打开群聊失败")
        dismiss_search_box()
        return ""
    finally:
        if previous_clipboard is not None:
            try:
                pyperclip.copy(previous_clipboard)
            except Exception:
                pass


def deliver(wx, text: str, image_path: Path, target_group: str, fallback_group: str) -> str:
    """先检索目标群；失败或异常则熔断，改发兜底群。返回实际发送到的群名。"""
    opened = ""
    try:
        opened = open_group(wx, target_group)
    except Exception:
        logger.exception("检索目标群时发生异常，准备降级")
        opened = ""

    if opened:
        send_text_and_image(wx, text, image_path)
        return opened

    logger.warning("未检索到目标群 %s，切换兜底群 %s", target_group, fallback_group)
    dismiss_search_box()

    try:
        fallback_opened = open_group(wx, fallback_group, exact=True)
    except Exception as exc:
        logger.exception("检索兜底群时发生异常")
        raise RuntimeError(f"目标群与兜底群都无法打开: {target_group} / {fallback_group}") from exc

    if not fallback_opened:
        raise RuntimeError(f"目标群与兜底群都无法打开: {target_group} / {fallback_group}")

    send_text_and_image(wx, text, image_path)
    return fallback_opened



def _safe_chat_name(wx) -> str:
    try:
        return read_chat_name(wx)
    except Exception:
        logger.exception("读取当前会话标题失败")
        return ""


def build_message(summary: str) -> str:
    """The WeChat text is only the note itself. The screenshot is sent as the attachment."""
    return summary.strip()


def send_text_and_image(wx, text: str, image_path: Path) -> None:
    """先发文字，再发本地截图。两次发送之间留出渲染间隔。"""
    image_path = image_path.resolve()
    if not image_path.is_file():
        raise FileNotFoundError(f"截图不存在: {image_path}")

    time.sleep(BEFORE_SEND_SECONDS)
    if text.strip():
        msg_result = wx.SendMsg(text.strip())
        if _is_explicit_failure(msg_result):
            raise RuntimeError(f"文字发送失败: {msg_result}")
        logger.info("文字已发送")
        time.sleep(BETWEEN_SEND_SECONDS)
    file_result = wx.SendFiles(str(image_path))
    if _is_explicit_failure(file_result):
        raise RuntimeError(f"截图发送失败: {file_result}")
    logger.info("截图已发送")



def run_work() -> str:
    """无人值守任务入口。把真实业务替换到这里，返回需要发到群里的说明。"""
    logger.info("开始执行无人值守任务")
    # 示例任务：实际使用时在这里调用业务脚本，完成后再截屏
    time.sleep(0.3)
    return "无人值守任务已执行完毕。"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="任务完成后截屏并发送到微信群，失败时降级到兜底群")
    parser.add_argument("--target", default=TARGET_GROUP, help="目标微信群完整名称或唯一关键词")
    parser.add_argument("--fallback", default=FALLBACK_GROUP, help="检索失败时的兜底会话完整名称")
    parser.add_argument("--message", default="", help="附加说明；为空时使用任务返回的说明")
    return parser.parse_args()


def ensure_group_names(target_group: str, fallback_group: str) -> None:
    invalid = [name for name in (target_group, fallback_group) if (not name.strip()) or name.startswith("请修改为")]
    if invalid:
        raise ValueError("请填写目标群的完整名称或唯一关键词，并填写兜底会话的完整名称。")
    if target_group.strip() == fallback_group.strip():
        raise ValueError("目标群和兜底群不能是同一个名称，否则无法区分降级。")


class WeChatClientError(RuntimeError):
    """微信主窗口在，但界面控件还不能被自动化识别。"""

    def __init__(self, message: str, can_restart: bool = False) -> None:
        super().__init__(message)
        self.can_restart = can_restart


def _window_text(hwnd: int) -> str:
    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _window_class(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def _process_image(pid: int) -> str:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return ""
    buffer = ctypes.create_unicode_buffer(1024)
    size = ctypes.c_ulong(1024)
    ok = kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size))
    kernel32.CloseHandle(handle)
    return buffer.value if ok else ""


def find_wechat_main_window() -> tuple[int, str] | None:
    """返回微信主窗口句柄和程序路径。窗口被藏起来时也能找到，调用方再把它显示出来。"""
    user32 = ctypes.windll.user32
    found: list[tuple[int, int, int, str]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd, _lparam):
        if _window_text(hwnd) != "微信":
            return True
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        image = _process_image(pid.value)
        name = Path(image).name.lower()
        if name not in {"weixin.exe", "wechat.exe"}:
            return True
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        area = max(0, int(rect.right) - int(rect.left)) * max(0, int(rect.bottom) - int(rect.top))
        if area <= 0:
            return True
        hidden = 0 if user32.IsWindowVisible(hwnd) else 1
        found.append((hidden, -area, int(hwnd), image))
        return True

    user32.EnumWindows(callback, 0)
    if not found:
        return None
    found.sort()
    return found[0][2], found[0][3]


def wechat_version(exe_path: str) -> str:
    root = Path(exe_path).parent
    versions: list[tuple[tuple[int, ...], str]] = []
    if not root.is_dir():
        return ""
    for child in root.iterdir():
        if not child.is_dir():
            continue
        parts: list[int] = []
        for piece in child.name.split("."):
            if not piece.isdigit():
                parts = []
                break
            parts.append(int(piece))
        if parts:
            versions.append((tuple(parts), child.name))
    if not versions:
        return ""
    return max(versions)[1]


def enable_wechat_accessibility() -> None:
    """让系统声明有屏幕阅读器。微信只在下次启动时读取这个状态。"""
    user32 = ctypes.windll.user32
    user32.SystemParametersInfoW(0x0047, 1, None, 2)


def focus_wechat_window(hwnd: int) -> None:
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, 9)
    user32.SendMessageW(hwnd, 0x003D, 0, ctypes.c_long(0xFFFFFFFC).value)
    time.sleep(0.3)


def wechat_tree_ready(hwnd: int) -> bool:
    """搜索栏或会话列表出现，才说明可以搜索群名。"""
    try:
        from wxauto4.uia import uiautomation as auto
    except ImportError:
        return False
    try:
        control = auto.ControlFromHandle(hwnd)
    except Exception:
        return False
    if control is None:
        return False
    search = control.EditControl(ClassName="mmui::XValidatorTextEdit", searchDepth=40)
    if search.Exists(0.2):
        return True
    session = control.ListControl(AutomationId="session_list", searchDepth=40)
    return bool(session.Exists(0.2))


def ensure_wechat_controls(hwnd: int) -> bool:
    """窗口在但控件树为空时，打开微信自带的界面识别开关。"""
    if wechat_tree_ready(hwnd):
        return True
    enable_wechat_accessibility()
    focus_wechat_window(hwnd)
    try:
        from wechat_a11y import activate_wechat_controls
    except Exception:
        logger.exception("加载界面识别模块失败")
        return False
    try:
        return bool(activate_wechat_controls(hwnd))
    except Exception:
        logger.exception("打开微信界面控件失败")
        return False


def describe_blocked_wechat(version: str) -> str:
    version_text = f"（{version}）" if version else ""
    return (
        "未找到已登录的客户端主窗口。\n\n"
        f"微信{version_text}已经打开，但聊天界面没有暴露给系统。"
        "程序只能看到空的 Qt 窗口，找不到会话列表和输入框。\n\n"
        "请在任务栏右下角的微信图标上右键，选择退出，不要只点关闭。"
        "重新打开并登录后，让主窗口保持显示，再点一次发送。\n\n"
        "如果重新打开后仍然是这句话，说明这个账号在这台电脑上不提供界面控件，"
        "当前发送方式无法继续。"
    )


def probe_wechat() -> str:
    found = find_wechat_main_window()
    if found is None:
        return "没有找到已打开的微信主窗口。请先登录微信，并让主窗口保持显示。"
    hwnd, exe_path = found
    version = wechat_version(exe_path)
    ensure_wechat_controls(hwnd)
    if wechat_tree_ready(hwnd):
        label = f"微信 {version} 可以被识别，可以发送。" if version else "微信可以被识别，可以发送。"
        return label
    return describe_blocked_wechat(version)


def restart_wechat() -> str:
    """退出当前微信并重新启动。调用前需要用户确认。"""
    import subprocess

    found = find_wechat_main_window()
    if found is None:
        raise WeChatClientError("没有找到正在运行的微信，请手动打开。")
    hwnd, exe_path = found
    if not exe_path or not Path(exe_path).is_file():
        raise WeChatClientError("找到了微信窗口，但没有定位到程序文件，请手动退出后重新打开。")

    enable_wechat_accessibility()
    ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)
    image_name = Path(exe_path).name
    deadline = time.time() + 8
    while time.time() < deadline and find_wechat_main_window() is not None:
        time.sleep(0.4)
    if find_wechat_main_window() is not None:
        subprocess.run(
            ["taskkill", "/IM", image_name, "/T"],
            check=False,
            capture_output=True,
            text=True,
        )
        time.sleep(1)
    if find_wechat_main_window() is not None:
        subprocess.run(
            ["taskkill", "/F", "/IM", image_name, "/T"],
            check=False,
            capture_output=True,
            text=True,
        )
        time.sleep(1)

    subprocess.Popen([exe_path])
    deadline = time.time() + 25
    while time.time() < deadline:
        time.sleep(1)
        again = find_wechat_main_window()
        if again is None:
            continue
        focus_wechat_window(again[0])
        ensure_wechat_controls(again[0])
        if wechat_tree_ready(again[0]):
            return "微信已重新打开，界面可以识别了。请再点一次「截图并发送到微信」。"
        if time.time() + 8 < deadline:
            continue
        return describe_blocked_wechat(wechat_version(exe_path))
    return "已启动微信。请等待登录完成并让主窗口显示出来，然后再点发送。"


def execute_report(
    target_group: str,
    fallback_group: str,
    summary: str,
    screenshot_dir: Path | None = None,
    status: str = "成功",
) -> str:
    """截屏并发送。临时图片在发送结束后删除。返回实际发送到的群名。"""
    del screenshot_dir, status
    target_group = target_group.strip()
    fallback_group = fallback_group.strip()
    ensure_group_names(target_group, fallback_group)
    image_path = capture_screen()
    try:
        text = build_message(summary)
        configure_wxauto()
        found = find_wechat_main_window()
        if found is not None:
            ensure_wechat_controls(found[0])
        try:
            wx = load_wechat_class()()
        except Exception as exc:
            # wxauto 把“控件树为空”也报成找不到主窗口。这里换成可操作的说明。
            if "未找到已登录的客户端主窗口" in str(exc):
                version = wechat_version(found[1]) if found else ""
                raise WeChatClientError(describe_blocked_wechat(version), can_restart=found is not None) from exc
            raise
        time.sleep(SEARCH_RENDER_SECONDS)
        sent_to = deliver(wx, text, image_path, target_group, fallback_group)
        logger.info("已发送到：%s", sent_to)
        return sent_to
    finally:
        delete_screenshot(image_path)


def main() -> int:
    setup_logging()
    args = parse_args()
    target_group = args.target.strip()
    fallback_group = args.fallback.strip()
    ensure_group_names(target_group, fallback_group)

    status = "成功"
    try:
        summary = run_work()
    except Exception as exc:
        # 任务失败也截取当前屏幕，方便在群里看到现场
        status = "失败"
        summary = f"任务执行异常：{exc}"
        logger.exception("无人值守任务失败，仍将截屏并尝试通知")

    if args.message.strip():
        summary = f"{summary}\n{args.message.strip()}" if summary else args.message.strip()

    try:
        execute_report(target_group, fallback_group, summary, status=status)
    except ValueError as exc:
        logger.error("%s", exc)
        return 1
    except Exception:
        logger.exception("发送到微信失败")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
