"""Video-derived adapter for 自然人电子税务局（扣缴端）.

All coordinates are relative to the *tax client window*, not the desktop.
Every action is gated by fresh OCR, identity, focus and bounded waits. Unknown
dialogs fail closed. This adapter still requires validation against a logged-in
installation; the video is not a substitute for an end-to-end test.
"""
from __future__ import annotations

import ctypes
import re
import time
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


class DesktopError(RuntimeError):
    pass


def live_windows():
    """Wrap handles separately: a closing popup must not abort the entire scan."""
    from pywinauto.controls.hwndwrapper import HwndWrapper, InvalidWindowHandle
    user32 = ctypes.windll.user32
    handles = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def collect(hwnd, _):
        handles.append(hwnd)
        return True

    user32.EnumWindows.argtypes = [callback_type, ctypes.c_void_p]
    user32.IsWindow.argtypes = [ctypes.c_void_p]
    user32.EnumWindows(collect, 0)
    windows = []
    for hwnd in handles:
        if not user32.IsWindow(hwnd):
            continue
        try:
            windows.append(HwndWrapper(hwnd))
        except InvalidWindowHandle:
            continue
    return windows


def compact(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value)))


def require_zero(value):
    text = compact(value)
    if text.endswith("元"):
        text = text[:-1]
    if not re.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)\.\d{2}", text):
        raise DesktopError("金额未识别为明确的两位小数")
    text = text.replace(",", "")
    try:
        if Decimal(text) != 0:
            raise DesktopError("检测到非零金额")
    except InvalidOperation as exc:
        raise DesktopError("金额无法解析") from exc


def require_identity(text, company):
    text = compact(text)
    # Match the observed footer field rather than a company-name substring.
    # A branch name or duplicate footer must never pass for the requested unit.
    # OCR can omit the narrow closing bracket at the end of the footer.
    # Only accept that omission at end-of-text, after an exact 18-character ID.
    units = re.findall(r"当前操作单位[:：]([^\[\]]+)\[([A-Z0-9]{18})(?:\]|$)", text)
    expected_name = compact(company.name)
    tax_id = company.tax_id.strip()
    if not tax_id:
        if [name for name, _tax in units] != [expected_name]:
            raise DesktopError("扣缴端当前公司名称不匹配或不唯一")
        return
    identifiers = re.findall(r"(?<![A-Z0-9])[A-Z0-9]{18}(?![A-Z0-9])", text)
    if identifiers != [tax_id]:
        raise DesktopError("扣缴端当前税号不唯一")
    if units != [(expected_name, tax_id)]:
        raise DesktopError("扣缴端当前公司名称或税号不匹配或不唯一")


def require_month(text, company):
    found = re.findall(r"(\d{4})年(\d{1,2})月", compact(text))
    months = {f"{y}-{int(m):02d}" for y, m in found}
    if months != {company.month}:
        raise DesktopError("税款所属月份不匹配或无法识别；请在扣缴端选择目标月份")


def salary_totals(text):
    text = compact(text)
    labels = ("申报总人数", "收入总额", "免税收入合计")
    if any(text.count(label) != 1 for label in labels):
        raise DesktopError("工资表汇总标签缺失或不唯一")
    people = re.findall(r"申报总人数[:：]?(\d+)人", text)
    income = re.findall(r"收入总额[:：]?([+-]?[\d,]+\.\d{2})元", text)
    exempt = re.findall(r"免税收入合计[:：]?([+-]?[\d,]+\.\d{2})元", text)
    if not (len(people) == len(income) == len(exempt) == 1):
        raise DesktopError("未能完整读取工资表人数、收入和免税收入")
    require_zero(income[0])
    require_zero(exempt[0])
    return int(people[0])


def choose_name_edit(edits):
    """Middle edit on the top search row is 纳税人名称. Left edit is the tax id."""
    if not edits:
        return None
    top = min(edit[1] for edit in edits)
    row = [edit for edit in edits if abs(edit[1] - top) <= 0.06]
    row.sort()
    if len(row) < 2:
        return None
    return row[1]


def choose_search_button(buttons, which):
    """Search-row buttons sit just under the name edit. Left is 查询, right is 重置."""
    row = [button for button in buttons if 0.20 <= button[1] <= 0.33]
    if not row:
        return None
    row.sort()
    if which == "query":
        return row[0]
    if len(row) < 2:
        return None
    return row[-1]


def choose_password_edit(edits):
    """The 请输入申报密码 box is the edit on the lower-middle row of the login dialog."""
    row = [edit for edit in edits if 0.48 <= edit[1] <= 0.62]
    if len(row) != 1:
        return None
    return row[0]


def choose_keyboard_close(items):
    """Close the virtual keyboard. Do not treat the login or copy-result dialog as that keyboard."""
    texts = {compact(item.get("text", "")) for item in items}
    if "确定" in texts or "保存" in texts or "复制结果" in texts:
        return None
    return choose_exact_button(items, "关闭")


def choose_exact_button(items, label):
    matches = [item for item in items if compact(item.get("text", "")) == compact(label)]
    if len(matches) != 1:
        return None
    return matches[0]


def remark_is_complete(text):
    """The grid paints a cut-off remark with an ellipsis. That is not the value to paste."""
    value = str(text or "").strip().replace("\x00", "")
    if not value:
        return False
    return not value.endswith("...") and not value.endswith("…") and not value.endswith("..")


def choose_remark_point(items, company_name, y_tolerance=0.04):
    """Point at the 备注 column on the result row. The painted text may be cut off with an ellipsis."""
    headers = [item for item in items if compact(item.get("text", "")) == "备注"]
    if len(headers) != 1:
        return None
    header = headers[0]
    row = choose_row_action(items, company_name, y_tolerance)
    if row is None:
        enters = [item for item in items
                  if compact(item.get("text", "")) == "进入" and item.get("y", 0) > header.get("y", 0) + 0.02]
        if len(enters) != 1:
            return None
        row = enters[0]
    return {"x": header.get("x", 0), "y": row.get("y", 0)}


def choose_single_enter(items):
    """After a name search, one visible 进入 is the row to open."""
    enters = [item for item in items
              if compact(item.get("text", "")) == "进入" and item.get("y", 0) > 0.32]
    if len(enters) != 1:
        return None
    return enters[0]


def choose_row_action(items, company_name, y_tolerance=0.03):
    """Return the 进入 or 登录 control on the same result row as the company."""
    expected = compact(company_name)
    names = [item for item in items if compact(item.get("text", "")) == expected]
    if not names:
        names = [item for item in items
                 if len(compact(item.get("text", ""))) >= 6
                 and (compact(item.get("text", "")) in expected or expected in compact(item.get("text", "")))]
    if len(names) != 1:
        return None
    name = names[0]
    actions = [item for item in items
               if compact(item.get("text", "")) in ("进入", "登录")
               and abs(item.get("y", 0) - name.get("y", 0)) <= y_tolerance]
    enters = [item for item in actions if compact(item.get("text", "")) == "进入"]
    chosen = enters or actions
    if len(chosen) != 1:
        return None
    return chosen[0]


def choose_copy_option(items):
    """The salary wizard must use 复制上月当期数据, never the template or zero-wage row."""
    matches = []
    for item in items:
        text = compact(item.get("text", ""))
        if "复制上月当期数据" in text and "标准模板" not in text and "生成零" not in text:
            matches.append(item)
    if len(matches) != 1:
        return None
    return matches[0]


def choose_continue_declare(items):
    """Pick 继续申报. A dialog that also offers 作废, 更正, or payment is refused."""
    blocked = ("作废申报", "更正申报", "立即缴税", "暂不申报")
    texts = [compact(item.get("text", "")) for item in items]
    if any(any(word in text for word in blocked) for text in texts):
        return None
    matches = [item for item in items if compact(item.get("text", "")) == "继续申报"]
    if len(matches) != 1:
        return None
    return matches[0]


def choose_continue_tax_button(items):
    """继续算税 is the left button. 暂不算税 stays on the right."""
    goes = [item for item in items if compact(item.get("text", "")) == "继续算税"]
    stays = [item for item in items if compact(item.get("text", "")) == "暂不算税"]
    if len(goes) != 1:
        return None
    if stays and goes[0].get("x", 1) >= min(item.get("x", 0) for item in stays):
        return None
    return goes[0]


def choose_close_button(items):
    """Close the copy result. Never choose 保存."""
    closes = [item for item in items if compact(item.get("text", "")) == "关闭"]
    if len(closes) != 1:
        return None
    return closes[0]


def choose_copy_button(items):
    """After that option is selected the action button reads 立即复制数据."""
    matches = [item for item in items if compact(item.get("text", "")).startswith("立即复制")]
    if len(matches) != 1:
        return None
    return matches[0]


def choose_confirm_button(items):
    """Pick 确定 when it sits to the left of 取消."""
    confirms = [item for item in items if compact(item.get("text", "")) == "确定"]
    cancels = [item for item in items if compact(item.get("text", "")) == "取消"]
    if len(confirms) != 1:
        return None
    if cancels and confirms[0].get("x", 0) >= min(item.get("x", 1) for item in cancels):
        return None
    return confirms[0]


def choose_defer_button(items):
    """Pick 暂不跳转. Never pick the left-hand 立即跳转 button."""
    stays = [item for item in items if compact(item.get("text", "")) == "暂不跳转"]
    jumps = [item for item in items if compact(item.get("text", "")) == "立即跳转"]
    if len(stays) != 1:
        return None
    if jumps and stays[0].get("x", 0) <= min(item.get("x", 0) for item in jumps):
        return None
    return stays[0]


def choose_client_indexes(items):
    """Pick the visible 扣缴端 main window.

    The Delphi client also creates a zero-size TApplication window with the
    same title. Counting that window makes an already-open client look missing.
    """
    frames, others = [], []
    for index, (title, class_name, width, height) in enumerate(items):
        title = title or ""
        if "自然人电子税务局" not in title or "扣缴端" not in title:
            continue
        if class_name == "TApplication":
            continue
        if class_name == "Tfrm_MainFrame":
            frames.append(index)
        elif width >= 200 and height >= 200:
            others.append(index)
    return frames or others


def success_feedback(status, feedback):
    status, feedback = compact(status), compact(feedback)
    if any(t in status+feedback for t in ("申报失败", "处理失败")):
        raise DesktopError("反馈含失败结果，请人工核验，禁止重复发送")
    if "需缴款" in status and "无需缴款" not in status:
        raise DesktopError("反馈要求缴款，不能视为零申报成功")
    return "申报成功" in status and "无需缴款" in status and "申报成功" in feedback


class TaxDesktop:
    def __init__(self, settings, base_dir, password_provider, log=print, stop=None):
        from rapidocr_onnxruntime import RapidOCR
        self.ocr = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=2)
        self.settings = settings
        self.base = Path(base_dir)
        self.password_provider = password_provider
        self.log = log
        self.stop = stop or (lambda: False)
        self.root = None
        self.last_image = None
        self.last_error = ""
        self.user32 = ctypes.windll.user32

    def fail(self, message):
        self.last_error = message
        self.log(message)
        raise DesktopError(message)

    def cancelled(self):
        if self.stop() or self.user32.GetAsyncKeyState(0x1B) & 0x8000:
            self.fail("用户已停止；若已发送申报，下一次仅核验结果")

    def record_error(self, phase, exc):
        """Save frame locations only, never exception bodies, locals or passwords."""
        import traceback
        frames = traceback.extract_tb(exc.__traceback__)
        locations = " > ".join(f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in frames)
        folder = self.base / "data"
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "desktop_errors.log").open("a", encoding="utf-8") as stream:
            stream.write(f"{datetime.now().isoformat()} {phase} {type(exc).__name__} {locations}\n")

    def attach(self):
        from pywinauto import Desktop
        windows = live_windows()
        records = []
        for window in windows:
            try:
                rect = window.rectangle()
                width, height = rect.width(), rect.height()
                title, class_name = window.window_text(), window.class_name()
            except Exception:
                width = height = 0
                title = class_name = ""
            records.append((title, class_name, width, height))
        picked = choose_client_indexes(records)
        if not picked:
            self.fail("请打开一个自然人电子税务局（扣缴端）窗口")
        if len(picked) != 1:
            self.fail("检测到多个扣缴端窗口，请只保留一个主窗口")
        self.root = windows[picked[0]]
        if self.root.is_minimized():
            self.root.restore()
        self.focus_window()
        r = self.root.rectangle()
        if not (1.40 < r.width() / r.height() < 1.55):
            self.fail("请将扣缴端恢复为普通窗口（视频约1010×688），不要最大化")
        if r.width() < 950 or r.height() < 630:
            self.fail("扣缴端窗口过小，请恢复标准窗口大小")

    def focus_handle(self, window):
        """Bring a tax window forward without pywinauto's off-screen mouse move.

        Do not attach the withholding client's own thread or send Alt. Attaching
        that thread deadlocks the client when a dialog has disabled the main
        window. Joining only the current foreground thread lets Windows accept
        SetForegroundWindow, and the join is released immediately.
        """
        user32 = self.user32
        hwnd = window.handle
        user32.IsWindowEnabled.argtypes = [ctypes.c_void_p]
        user32.IsWindowEnabled.restype = ctypes.c_bool
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        user32.SetForegroundWindow.restype = ctypes.c_bool
        user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
        user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
        user32.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool]
        user32.AttachThreadInput.restype = ctypes.c_bool
        user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        if not user32.IsWindowEnabled(hwnd):
            return False
        if user32.GetForegroundWindow() == hwnd:
            return True
        kernel32 = ctypes.windll.kernel32
        current = kernel32.GetCurrentThreadId()
        foreground = user32.GetForegroundWindow()
        foreground_tid = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        attached = False
        if foreground_tid and foreground_tid != current:
            attached = bool(user32.AttachThreadInput(current, foreground_tid, True))
        try:
            user32.BringWindowToTop(hwnd)
            # Raise above the workbench or WeChat window left in front.
            user32.SetWindowPos(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x0003)
            user32.SetWindowPos(hwnd, ctypes.c_void_p(-2), 0, 0, 0, 0, 0x0003)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(current, foreground_tid, False)
        return user32.GetForegroundWindow() == hwnd

    def focus_window(self):
        self.focus_handle(self.root)

    def focus_guard(self):
        self.cancelled()
        if not self.root or not self.user32.IsWindow(self.root.handle):
            self.fail("扣缴端窗口已关闭")
        fg = self.user32.GetForegroundWindow()
        pid = ctypes.c_ulong()
        self.user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
        if pid.value != self.root.process_id():
            for _ in range(3):
                self.bring_client_forward()
                time.sleep(.25)
                fg = self.user32.GetForegroundWindow()
                self.user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
                if pid.value == self.root.process_id():
                    break
            else:
                self.fail("焦点已离开扣缴端，批次停止")

    def bring_client_forward(self):
        """Do not restore a visible dialog or couple our input queue to the client."""
        self.ensure_shown()
        self.park_covering_windows()
        if self.user32.GetForegroundWindow() != self.root.handle:
            self.focus_window()

    def park_covering_windows(self):
        """Send the workbench browser behind the client. Do not touch the client itself."""
        user32 = self.user32
        user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        client_pid = self.root.process_id()
        for window in live_windows():
            try:
                if not window.is_visible() or window.process_id() == client_pid:
                    continue
                title = window.window_text() or ""
                class_name = window.class_name() or ""
                if "Chrome" not in class_name and "工作台" not in title and "报税核对" not in title:
                    continue
                user32.SetWindowPos(window.handle, ctypes.c_void_p(1), 0, 0, 0, 0, 0x0013)
            except Exception:
                continue

    def ensure_shown(self):
        """The client can be hidden while its window still exists. Show that window, not the screen behind it."""
        hwnd = self.root.handle
        self.user32.IsIconic.argtypes = [ctypes.c_void_p]
        self.user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        self.user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        if self.user32.IsIconic(hwnd) or not self.user32.IsWindowVisible(hwnd):
            self.user32.ShowWindow(hwnd, 9)
            self.user32.ShowWindow(hwnd, 5)
            self.focus_window()
            time.sleep(.3)

    def capture_client(self):
        """Read pixels from the tax window itself. A desktop grab also copies whatever window is covering it."""
        return self.capture_window(self.root)

    def capture_window(self, window):
        from PIL import Image
        user32, gdi32 = self.user32, ctypes.windll.gdi32
        hwnd = window.handle
        rect = window.rectangle()
        width, height = rect.width(), rect.height()
        if width < 50 or height < 50:
            self.fail("扣缴端窗口没有可点击的画面")
        user32.GetWindowDC.argtypes = [ctypes.c_void_p]
        user32.GetWindowDC.restype = ctypes.c_void_p
        user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        user32.PrintWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
        user32.PrintWindow.restype = ctypes.c_bool
        gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
        gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
        gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
        gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        gdi32.SelectObject.restype = ctypes.c_void_p
        gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
        gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
        gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                                    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
        hdc = user32.GetWindowDC(hwnd)
        if not hdc:
            self.fail("无法读取扣缴端窗口画面")
        memory = gdi32.CreateCompatibleDC(hdc)
        bitmap = gdi32.CreateCompatibleBitmap(hdc, width, height)
        previous = gdi32.SelectObject(memory, bitmap)
        try:
            if not user32.PrintWindow(hwnd, memory, 2):
                self.fail("无法读取扣缴端窗口画面")
            class Header(ctypes.Structure):
                _fields_ = [("biSize", ctypes.c_uint), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                            ("biPlanes", ctypes.c_ushort), ("biBitCount", ctypes.c_ushort),
                            ("biCompression", ctypes.c_uint), ("biSizeImage", ctypes.c_uint),
                            ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
                            ("biClrUsed", ctypes.c_uint), ("biClrImportant", ctypes.c_uint)]
            header = Header()
            header.biSize = ctypes.sizeof(Header)
            header.biWidth = width
            header.biHeight = -height
            header.biPlanes = 1
            header.biBitCount = 32
            raw = ctypes.create_string_buffer(width * height * 4)
            gdi32.GetDIBits(memory, bitmap, 0, height, raw, ctypes.byref(header), 0)
            return Image.frombuffer("RGB", (width, height), raw, "raw", "BGRX", 0, 1)
        finally:
            if previous:
                gdi32.SelectObject(memory, previous)
            if bitmap:
                gdi32.DeleteObject(bitmap)
            if memory:
                gdi32.DeleteDC(memory)
            user32.ReleaseDC(hwnd, hdc)

    def snap(self, region=None):
        import numpy as np
        self.focus_guard()
        self.ensure_shown()
        image = self.capture_client()
        sample = np.array(image)[::8, ::8]
        if float(sample.mean()) < 12:
            self.ensure_shown()
            time.sleep(.4)
            image = self.capture_client()
        self.last_image = image
        w, h = image.size
        x0, y0, x1, y1 = region or (0, 0, 1, 1)
        crop = image.crop((int(w*x0), int(h*y0), int(w*x1), int(h*y1)))
        # RGB input is supported by RapidOCR; convert explicitly to BGR.
        results, _ = self.ocr(np.array(crop)[:, :, ::-1].copy())
        out = []
        for box, text, confidence in results or []:
            if confidence < .85:
                continue
            xs, ys = zip(*box)
            out.append({"text": text, "x": (sum(xs)/4 + w*x0)/w,
                        "y": (sum(ys)/4 + h*y0)/h,
                        "w": (max(xs)-min(xs))/w, "h": (max(ys)-min(ys))/h})
        return out

    @staticmethod
    def text(items):
        return "\n".join(i["text"] for i in items)

    def read(self, region):
        return self.text(self.snap(region))

    def find_zero_income_dialog(self):
        """The three-month zero-income notice is its own window, not a control on the main frame."""
        from pywinauto import Desktop
        if not self.root:
            return None
        pid = self.root.process_id()
        found = []
        for window in live_windows():
            try:
                if window.class_name() != "Tfrm_ZeroIncomeTip" or not window.is_visible():
                    continue
                if window.process_id() != pid:
                    continue
                rect = window.rectangle()
                if rect.width() < 200 or rect.height() < 200:
                    continue
                found.append(window)
            except Exception:
                continue
        if len(found) > 1:
            self.fail("出现多个零收入提示，已停止")
        return found[0] if found else None

    def find_message_dialog(self):
        from pywinauto import Desktop
        if not self.root:
            return None
        pid = self.root.process_id()
        found = []
        for window in live_windows():
            try:
                if window.class_name() != "Tfrm_MsgDlgRich" or not window.is_visible():
                    continue
                if window.process_id() != pid:
                    continue
                rect = window.rectangle()
                if rect.width() < 200 or rect.height() < 80:
                    continue
                found.append(window)
            except Exception:
                continue
        if len(found) > 1:
            self.fail("出现多个提示窗口，已停止")
        return found[0] if found else None

    def confirm_import_dialog(self):
        """Accept the empty-table import confirmation. Do not log the dialog body."""
        import numpy as np
        dialog = self.find_message_dialog()
        if dialog is None:
            return False
        image = self.capture_window(dialog)
        width, height = image.size
        body = image.crop((0, 0, width, int(height * .7)))
        body_results, _ = self.ocr(np.array(body)[:, :, ::-1].copy())
        text = compact("".join(item[1] for item in (body_results or [])))
        if any(word in text for word in ("离职", "作废", "更正", "缴税", "缴款")):
            self.fail("出现不能自动确认的提示，已停止")
        if not all(word in text for word in ("导入", "确认", "没有", "数据")):
            self.fail("出现未识别的提示窗口，已停止")
        crop = image.crop((0, int(height * .62), width, height))
        results, _ = self.ocr(np.array(crop)[:, :, ::-1].copy())
        items = []
        for box, label, confidence in results or []:
            if confidence < .85:
                continue
            xs, ys = zip(*box)
            items.append({"text": label, "x": (sum(xs) / 4) / width,
                          "y": (sum(ys) / 4 + height * .62) / height})
        button = choose_confirm_button(items)
        if button is None:
            self.fail("导入确认没有唯一的确定，已停止")
        self.log("工资表没有数据，导入确认点击确定后继续")
        self.focus_handle(dialog)
        dialog.click_input(coords=(round(button["x"] * width), round(button["y"] * height)))
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            self.cancelled()
            if self.find_message_dialog() is None:
                return True
            time.sleep(.3)
        self.fail("点击确定后导入确认仍在，已停止")

    def find_import_wizard(self):
        from pywinauto import Desktop
        if not self.root:
            return None
        pid = self.root.process_id()
        found = []
        for window in live_windows():
            try:
                if window.class_name() != "TFrm_ImportConfig" or not window.is_visible():
                    continue
                if window.process_id() != pid:
                    continue
                rect = window.rectangle()
                if rect.width() < 200 or rect.height() < 120:
                    continue
                found.append(window)
            except Exception:
                continue
        if len(found) > 1:
            self.fail("出现多个工资导入向导，已停止")
        return found[0] if found else None

    def wizard_items(self, dialog):
        import numpy as np
        image = self.capture_window(dialog)
        width, height = image.size
        results, _ = self.ocr(np.array(image)[:, :, ::-1].copy())
        items = []
        for box, label, confidence in results or []:
            if confidence < .85:
                continue
            xs, ys = zip(*box)
            items.append({"text": label, "x": (sum(xs) / 4) / width, "y": (sum(ys) / 4) / height})
        return items, width, height

    def copy_previous_month(self):
        """Choose 复制上月当期数据 and click 立即复制数据. Never click 导入 or 生成零工资."""
        deadline = time.monotonic() + 8
        dialog = None
        while time.monotonic() < deadline:
            self.cancelled()
            dialog = self.find_import_wizard()
            if dialog is not None:
                break
            time.sleep(.3)
        if dialog is None:
            return False
        items, width, height = self.wizard_items(dialog)
        option = choose_copy_option(items)
        if option is None:
            self.fail("工资导入向导里没有唯一的复制上月当期数据")
        self.log("选择复制上月当期数据")
        self.focus_handle(dialog)
        dialog.click_input(coords=(round(option["x"] * width), round(option["y"] * height)))
        button = None
        for _ in range(5):
            time.sleep(.35)
            dialog = self.find_import_wizard()
            if dialog is None:
                self.fail("选择复制上月后向导已关闭")
            items, width, height = self.wizard_items(dialog)
            button = choose_copy_button(items)
            if button is not None:
                break
        if button is None:
            self.fail("选择复制上月后没有出现立即复制")
        self.log("点击立即复制数据")
        self.focus_handle(dialog)
        dialog.click_input(coords=(round(button["x"] * width), round(button["y"] * height)))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.cancelled()
            if self.find_import_wizard() is None:
                self.wait_until_idle()
                return True
            time.sleep(.3)
        self.fail("点击立即复制后向导仍在，已停止")

    def find_named_popup(self, title):
        from pywinauto import Desktop
        if not self.root:
            return None
        pid = self.root.process_id()
        found = []
        for window in live_windows():
            try:
                if window.class_name() != "TfrmBase" or not window.is_visible():
                    continue
                if window.process_id() != pid or window.window_text() != title:
                    continue
                rect = window.rectangle()
                if rect.width() < 150 or rect.height() < 80:
                    continue
                found.append(window)
            except Exception:
                continue
        if len(found) > 1:
            self.fail("出现多个同类提示窗口，已停止")
        return found[0] if found else None

    def close_copy_result(self):
        """Close the copy-result window. Do not read its rows or click 保存."""
        import numpy as np
        dialog = self.find_named_popup("结果")
        if dialog is None:
            return False
        button = None
        action = "关闭结果"
        image = width = height = None
        for _ in range(5):
            dialog = self.find_named_popup("结果")
            if dialog is None:
                return False
            image = self.capture_window(dialog)
            width, height = image.size
            results, _ = self.ocr(np.array(image)[:, :, ::-1].copy())
            items = []
            for box, label, confidence in results or []:
                if confidence < .85 or len(label) > 8:
                    continue
                xs, ys = zip(*box)
                items.append({"text": label, "x": (sum(xs) / 4) / width, "y": (sum(ys) / 4) / height})
            button = choose_close_button(items)
            if button is not None:
                break
            time.sleep(.4)
        if button is None:
            body = image.crop((0, 0, width, int(height * .75)))
            body_results, _ = self.ocr(np.array(body)[:, :, ::-1].copy())
            body_text = compact("".join(item[1] for item in (body_results or [])))
            button = choose_continue_tax_button(items)
            if button is None or "三险" not in body_text or "未填写" not in body_text:
                self.fail("结果窗口按钮无法识别，已停止")
            if not self.settings.get("allow_missing_insurance"):
                self.fail("检测到未填写三险一金提示，请核对该企业配置")
            action = "继续算税"
        self.log("复制完成后关闭提示并继续" if action == "关闭结果" else "三险一金未填写，点击继续算税")
        self.focus_handle(dialog)
        dialog.click_input(coords=(round(button["x"] * width), round(button["y"] * height)))
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            self.cancelled()
            if self.find_named_popup("结果") is None:
                return True
            time.sleep(.3)
        self.fail("关闭复制结果后窗口仍在，已停止")

    def wait_until_idle(self, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.cancelled()
            self.close_copy_result()
            if self.find_named_popup("进度") is not None:
                time.sleep(.5)
                continue
            text = compact(self.read((.15, .12, .99, .9)))
            if not any(token in text for token in ("正在查询", "正在下载", "请稍候", "请稍后", "进度")):
                return
            time.sleep(.5)
        self.fail("复制上月数据后仍在查询，已停止")

    def dismiss_zero_income_prompt(self):
        """Click 暂不跳转 and continue. Do not read or log the names inside the notice."""
        if self.root is not None and self.root.class_name() in ("TFrmNewCompanyMgrView", "TFrmCompanyMgrView", "TfrmLoginAuth"):
            return
        self.close_copy_result()
        if self.confirm_import_dialog() or self.find_import_wizard():
            self.copy_previous_month()
        import numpy as np
        dialog = self.find_zero_income_dialog()
        if dialog is None:
            return
        image = self.capture_window(dialog)
        width, height = image.size
        crop = image.crop((0, int(height * .86), width, height))
        results, _ = self.ocr(np.array(crop)[:, :, ::-1].copy())
        items = []
        for box, text, confidence in results or []:
            if confidence < .85:
                continue
            xs, ys = zip(*box)
            items.append({"text": text, "x": (sum(xs) / 4) / width,
                          "y": (sum(ys) / 4 + height * .86) / height})
        button = choose_defer_button(items)
        if button is None:
            self.fail("零收入提示没有唯一的暂不跳转，已停止")
        self.log("出现连续零收入提示，点击暂不跳转，继续办理")
        self.focus_handle(dialog)
        dialog.click_input(coords=(round(button["x"] * width), round(button["y"] * height)))
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            self.cancelled()
            if self.find_zero_income_dialog() is None:
                return
            time.sleep(.3)
        self.fail("点击暂不跳转后提示仍在，已停止")

    def click_point(self, x, y, double=False):
        from pywinauto import mouse
        # Do not dismiss dialogs after OCR selected a point: doing so may
        # change the page and invalidate the coordinates we are about to use.
        self.focus_guard()
        self.bring_client_forward()
        self.validate_click_target(x, y)
        rect = self.root.rectangle()
        point = (rect.left + round(x * rect.width()), rect.top + round(y * rect.height()))
        if double:
            mouse.double_click(coords=point)
        else:
            mouse.click(coords=point)
        time.sleep(.25)

    def _screen_point_belongs_to_root(self, screen_x, screen_y):
        from ctypes import wintypes
        user32 = self.user32
        user32.WindowFromPoint.argtypes = [wintypes.POINT]
        user32.WindowFromPoint.restype = ctypes.c_void_p
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p
        hit = user32.WindowFromPoint(wintypes.POINT(int(screen_x), int(screen_y)))
        return bool(hit) and user32.GetAncestor(hit, 2) == self.root.handle

    def validate_click_target(self, x, y):
        """Click when the button itself belongs to the withholding client.

        Another program can remain the reported foreground window without
        covering the button. Refusing that case stops a visible client.
        """
        user32 = self.user32
        user32.IsWindowEnabled.argtypes = [ctypes.c_void_p]
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        if not user32.IsWindowEnabled(self.root.handle):
            self.fail("扣缴端当前窗口被弹窗禁用，请先处理遮挡的提示窗口")
        if not (0 <= x <= 1 and 0 <= y <= 1):
            self.fail("按钮坐标超出扣缴端窗口，已停止点击")
        rect = self.root.rectangle()
        screen_x = rect.left + round(x * rect.width())
        screen_y = rect.top + round(y * rect.height())
        if user32.GetForegroundWindow() != self.root.handle:
            self.focus_handle(self.root)
        if self._screen_point_belongs_to_root(screen_x, screen_y):
            return
        self.focus_handle(self.root)
        if self._screen_point_belongs_to_root(screen_x, screen_y):
            return
        self.fail("按钮位置被其他窗口遮挡，本次未点击，请将扣缴端完整置于前台")

    def click_text(self, label, region, partial=True, offset_x=0, double=False):
        self.dismiss_zero_income_prompt()
        items = self.snap(region)
        target = compact(label)
        matches = [i for i in items if (target in compact(i["text"]) if partial
                                       else target == compact(i["text"]))]
        if len(matches) != 1:
            self.fail(f"按钮“{label}”未唯一识别；请保留现场检查")
        item = matches[0]
        self.click_point(item["x"]+offset_x, item["y"], double)

    def type_literal(self, value, replace=False):
        from pywinauto.keyboard import send_keys
        self.focus_guard()
        if replace:
            send_keys("^a")
        escaped = "".join("{"+c+"}" if c in "+^%~(){}" else c for c in value)
        send_keys(escaped, with_spaces=True, pause=.01, vk_packet=True)

    def wait_ready(self, required=(), timeout=90, allow_login=False, allow_insurance=False):
        deadline = time.monotonic()+timeout
        ready_since, previous = None, None
        while time.monotonic() < deadline:
            self.cancelled()
            self.dismiss_zero_income_prompt()
            observed = self.snap((.19,.12,.99,.95))
            text = compact(self.text(observed))
            if "未填写三险一金" in text:
                if allow_insurance:
                    return "INSURANCE"
                self.fail("检测到未填写三险一金提示，请核对该企业配置")
            if "在线登录" in text or "请输入申报密码" in text:
                if allow_login:
                    return "LOGIN"
                self.fail("需要登录或身份验证，当前批次停止")
            if any(t in text for t in ("验证码", "密码错误", "登录失败", "申报失败", "连接失败", "异常", "错误")):
                self.fail("扣缴端出现验证、失败或异常提示，请人工查看当前页面")
            busy = any(token in text for token in ("进度", "请稍候", "请稍后", "正在查询", "正在下载"))
            unknown_dialog = any(t in text for t in ("是否继续", "确认提交")) or any(
                compact(i["text"]) == "提示" for i in observed)
            if unknown_dialog:
                self.fail("出现视频流程以外的确认提示，已停止")
            if not busy and all(compact(t) in text for t in required):
                if text != previous:
                    ready_since, previous = time.monotonic(), text
                elif ready_since is not None and time.monotonic()-ready_since >= 1.5:
                    return "READY"
            else:
                ready_since, previous = None, None
            time.sleep(.5)
        self.fail("等待扣缴端响应超时；如已发送申报，禁止再次发送")

    def identity(self, company):
        if self.footer_matches(company):
            return
        self.login_company(company)
        if not self.footer_matches(company):
            self.fail("扣缴端当前公司名称不匹配或不唯一")

    def month(self, company):
        try:
            # The period is the 2026年09月 title. On the list it sits under the step tabs;
            # after opening a form the same title moves up, so the band covers both rows.
            require_month(self.read((.18,.18,.70,.36)), company)
        except DesktopError as exc:
            self.fail(str(exc))

    def guard(self, company):
        self.identity(company)
        self.month(company)

    def fill_taxpayer_name(self, company):
        """Type the company name into 纳税人名称. A tax id is not required."""
        items = self.snap((.08, .18, .96, .40))
        exact = [i for i in items if compact(i["text"]) == "纳税人名称"]
        contains = [i for i in items if "纳税人名称" in compact(i["text"])]
        matches = exact or contains
        if len(matches) != 1:
            self.fail("未唯一识别纳税人名称输入位置")
        label = matches[0]
        x = label["x"] + label.get("w", 0) / 2 + 0.10
        if not 0 < x < 0.98:
            self.fail("纳税人名称输入位置超出窗口")
        self.click_point(x, label["y"])
        self.type_literal(company.name, replace=True)
        band = self.read((max(0, label["x"]), max(0, label["y"] - 0.03),
                          min(1, x + 0.16), min(1, label["y"] + 0.03)))
        if compact(company.name) not in compact(band):
            self.fail("单位管理公司名称输入核验失败")

    def footer_matches(self, company):
        try:
            require_identity(self.read((0, .955, .93, 1)), company)
            return True
        except DesktopError:
            return False

    def child_boxes(self, class_name):
        user32 = ctypes.windll.user32
        user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        user32.IsWindowEnabled.argtypes = [ctypes.c_void_p]
        user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.wintypes.RECT)]
        parent = self.root.rectangle()
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def visit(hwnd, _):
            name = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hwnd, name, 128)
            if name.value != class_name:
                return True
            # Disabled controls still define the row layout while the dialog
            # loads. Do not remove them and shift the name field's index.
            if not user32.IsWindowVisible(hwnd):
                return True
            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            width, height = rect.right - rect.left, rect.bottom - rect.top
            if width < 20 or height < 12 or parent.width() <= 0 or parent.height() <= 0:
                return True
            if rect.right <= parent.left or rect.left >= parent.right or rect.bottom <= parent.top or rect.top >= parent.bottom:
                return True
            found.append((
                (rect.left - parent.left + width / 2) / parent.width(),
                (rect.top - parent.top + height / 2) / parent.height(),
                int(hwnd),
            ))
            return True

        user32.EnumChildWindows.argtypes = [ctypes.c_void_p, type(visit), ctypes.c_void_p]
        user32.EnumChildWindows(self.root.handle, visit, 0)
        return found

    def find_company_dialog(self):
        client_pid = self.root.process_id()
        found = []
        for window in live_windows():
            try:
                if (window.class_name() in ("TFrmNewCompanyMgrView", "TFrmCompanyMgrView")
                        and window.process_id() == client_pid and window.is_visible()
                        and window.rectangle().width() > 200 and window.rectangle().height() > 100):
                    found.append(window)
            except Exception:
                continue
        if len(found) > 1:
            self.fail("检测到多个单位管理窗口，请保留一个后重试")
        return found[0] if found else None

    def wait_company_dialog(self, timeout=30, company=None, fail_on_timeout=True):
        deadline = time.monotonic() + timeout
        matched_since = None
        while time.monotonic() < deadline:
            self.cancelled()
            dialog = self.find_company_dialog()
            if dialog is not None:
                return dialog
            # Login may finish while the company-manager window is opening or
            # closing. A stable target footer is also a valid completion path.
            if company is not None and self.footer_matches(company):
                if matched_since is None:
                    matched_since = time.monotonic()
                elif time.monotonic() - matched_since >= 1.5:
                    self.log("已进入目标公司首页，继续后续操作")
                    return None
            else:
                matched_since = None
            time.sleep(.3)
        if fail_on_timeout:
            self.fail("等待单位管理超时，且未确认目标公司登录完成，请查看扣缴端当前页面")
        return False

    def dismiss_declare_success(self):
        """Close only a 申报成功 prompt. Leave every other dialog untouched."""
        dialog = self.find_message_dialog()
        if dialog is None:
            return False
        image = self.capture_window(dialog)
        width, height = image.size
        import numpy as np
        results, _ = self.ocr(np.array(image)[:, :, ::-1].copy())
        items = []
        for box, label, confidence in results or []:
            if confidence < .85:
                continue
            xs, ys = zip(*box)
            items.append({"text": label, "x": (sum(xs) / 4) / width, "y": (sum(ys) / 4) / height})
        text = compact(self.text(items))
        if "申报成功" not in text:
            return False
        if any(word in text for word in ("作废", "更正", "离职", "立即缴税")):
            return False
        button = choose_confirm_button(items)
        if button is None:
            return False
        self.focus_handle(dialog)
        dialog.click_input(coords=(round(button["x"] * width), round(button["y"] * height)))
        self.log("已关闭申报成功提示，准备打开单位管理")
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            self.cancelled()
            if self.find_message_dialog() is None:
                return True
            time.sleep(.3)
        return False

    def open_company_management(self, company):
        """Reobserve before a bounded retry; never toggle an already open dialog."""
        self.user32.IsWindowEnabled.argtypes = [ctypes.c_void_p]
        self.user32.IsWindowEnabled.restype = ctypes.c_bool
        for attempt in range(2):
            self.cancelled()
            dialog = self.find_company_dialog()
            if dialog is not None:
                return dialog
            if self.footer_matches(company):
                self.log("目标公司已登录，继续完整流程")
                return None
            if self.find_login_dialog() is not None:
                self.wait_logged_in(company)
                return None
            self.dismiss_declare_success()
            self.bring_client_forward()
            if not self.user32.IsWindowEnabled(self.root.handle):
                self.log("扣缴端主窗口仍被提示挡住，这一轮不点单位管理")
                time.sleep(1)
                continue
            self.log(f"打开单位管理：第{attempt + 1}次定位并点击")
            self.click_text("单位管理", (.55, .02, .99, .16), partial=False)
            self.log("单位管理按钮点击已发出，等待窗口打开")
            dialog = self.wait_company_dialog(timeout=8 if attempt == 0 else 20,
                                              company=company, fail_on_timeout=False)
            if dialog is not False:
                return dialog
            if attempt == 0:
                self.log("尚未打开单位管理，将重新检查遮挡和按钮位置后再试一次")
        self.fail("单位管理点击后仍未打开（已重新定位2次），请检查扣缴端是否有未处理提示或无响应")

    def read_control_text(self, hwnd):
        """TITSEdit answers WM_GETTEXT. GetWindowTextLength on that control stays 0."""
        user32 = ctypes.windll.user32
        user32.SendMessageTimeoutW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t,
                                             ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                                             ctypes.POINTER(ctypes.c_size_t)]
        user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
        buf = ctypes.create_unicode_buffer(512)
        result = ctypes.c_size_t()
        if not user32.SendMessageTimeoutW(hwnd, 0x000D, 512, buf, 0x0002, 2000,
                                         ctypes.byref(result)):
            self.fail("单位管理输入框响应超时，已停止查询，请检查扣缴端")
        return buf.value

    def wait_company_search_controls(self, timeout=20):
        """A top-level dialog can exist before its child controls are ready."""
        self.log("等待单位管理的查询输入框加载")
        deadline = time.monotonic() + timeout
        previous = None
        edits = []
        while time.monotonic() < deadline:
            self.cancelled()
            edits = self.child_boxes("TITSEdit")
            name_edit = choose_name_edit(edits)
            query = choose_search_button(self.child_boxes("TITSSpeedButton"), "query")
            if name_edit is not None and query is not None:
                top = min(edit[1] for edit in edits)
                tax_edit = sorted(edit for edit in edits if abs(edit[1] - top) <= .06)[0]
                controls = (tax_edit, name_edit, query)
                # Retain disabled controls for identification; wait until the
                # selected controls are usable instead of reporting them absent.
                ready = all(self.user32.IsWindowEnabled(box[2]) for box in controls)
                if ready and controls == previous:
                    return controls
                previous = controls if ready else None
            else:
                previous = None
            time.sleep(.3)
        positions = [(round(x, 3), round(y, 3)) for x, y, _ in edits]
        self.log(f"单位管理控件诊断：窗口类={self.root.class_name()}，可见TITSEdit位置={positions}")
        self.fail("单位管理查询控件20秒内未就绪（输入框或查询按钮未加载/不可用），已停止")

    def search_company_in_dialog(self, company):
        tax_edit, name_edit, query = self.wait_company_search_controls()
        # Reset reloads the entire company list. Clear only the tax-id filter,
        # then replace the name and issue a single filtered query.
        if self.read_control_text(tax_edit[2]).strip():
            from pywinauto.keyboard import send_keys
            self.click_control(tax_edit[2])
            self.focus_guard()
            send_keys("^a{BACKSPACE}")
            if self.read_control_text(tax_edit[2]).strip():
                self.fail("单位管理税号筛选清空失败")
        self.click_control(name_edit[2])
        self.type_literal(company.name, replace=True)
        typed = self.read_control_text(name_edit[2])
        if compact(company.name) != compact(typed):
            self.fail("单位管理公司名称输入核验失败")
        buttons = self.child_boxes("TITSSpeedButton")
        query = choose_search_button(buttons, "query")
        if query is None:
            self.fail("单位管理没有找到查询按钮")
        self.log("公司名称已核验，执行一次查询并等待结果")
        self.click_control(query[2])
        self.wait_company_results(company)

    def wait_company_results(self, company, timeout=30):
        deadline = time.monotonic() + timeout
        previous, stable_since = None, None
        while time.monotonic() < deadline:
            self.cancelled()
            items = self.snap((.02, .30, .99, .88))
            text = compact(self.text(items))
            matches = [i for i in items if compact(i["text"]) == compact(company.name)]
            if len(matches) > 1:
                self.fail("单位管理按公司名称搜索结果不唯一")
            busy = any(t in text for t in ("请稍候", "请稍后", "正在查询", "正在加载"))
            if len(matches) == 1 and not busy:
                if text != previous:
                    previous, stable_since = text, time.monotonic()
                elif stable_since is not None and time.monotonic() - stable_since >= 1.5:
                    self.log("目标公司查询结果已加载")
                    return
            else:
                previous, stable_since = None, None
            time.sleep(.5)
        self.fail("单位管理查询超时或未找到目标公司，已停止，不重复刷新查询")

    def click_control(self, hwnd):
        """Click a button by its screen center, without the focus call that freezes the mouse."""
        from pywinauto import mouse
        user32 = self.user32
        self.cancelled()
        self.bring_client_forward()
        if not user32.IsWindowVisible(hwnd) or not user32.IsWindowEnabled(hwnd):
            self.fail("目标控件不可用，已停止点击")
        user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.wintypes.RECT)]
        rect = ctypes.wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            self.fail("目标控件位置读取失败")
        center_x = (rect.left + rect.right) // 2
        center_y = (rect.top + rect.bottom) // 2
        if not self._screen_point_belongs_to_root(center_x, center_y):
            self.focus_handle(self.root)
        if not self._screen_point_belongs_to_root(center_x, center_y):
            self.fail("单位管理未获得前台焦点，请将扣缴端置于前台后重试")
        mouse.click(coords=(center_x, center_y))
        time.sleep(.25)

    def scroll_result_to_end(self):
        """Drag the result grid's horizontal bar to the far right so 备注 and 进入 are visible."""
        grids = [box for box in self.child_boxes("TVirtualStringTree") if box[1] > 0.35]
        if not grids:
            self.fail("单位管理没有结果列表")
        hwnd = grids[0][2]
        user32 = ctypes.windll.user32
        WM_HSCROLL = 0x0114
        SB_HORZ = 0
        SB_RIGHT = 7
        SIF_POS = 0x04
        SIF_RANGE = 0x01
        SIF_PAGE = 0x02

        class ScrollInfo(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("fMask", ctypes.c_uint),
                        ("nMin", ctypes.c_int), ("nMax", ctypes.c_int),
                        ("nPage", ctypes.c_uint), ("nPos", ctypes.c_int),
                        ("nTrackPos", ctypes.c_int)]

        user32.GetScrollInfo.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        user32.GetScrollInfo.restype = ctypes.c_bool
        user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        user32.SendMessageW.restype = ctypes.c_ssize_t
        previous = None
        for _ in range(40):
            info = ScrollInfo()
            info.cbSize = ctypes.sizeof(ScrollInfo)
            info.fMask = SIF_POS | SIF_RANGE | SIF_PAGE
            if not user32.GetScrollInfo(hwnd, SB_HORZ, ctypes.byref(info)):
                break
            if info.nPos + int(info.nPage) >= info.nMax or info.nPos == previous:
                return
            previous = info.nPos
            user32.SendMessageW(hwnd, WM_HSCROLL, SB_RIGHT, 0)
            time.sleep(.05)

    def _read_clipboard(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.OpenClipboard.restype = ctypes.c_bool
        user32.GetClipboardData.argtypes = [ctypes.c_uint]
        user32.GetClipboardData.restype = ctypes.c_void_p
        user32.CloseClipboard.restype = ctypes.c_bool
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        if not user32.OpenClipboard(None):
            return ""
        try:
            handle = user32.GetClipboardData(13)
            if not handle:
                return ""
            locked = kernel32.GlobalLock(handle)
            if not locked:
                return ""
            try:
                return ctypes.wstring_at(locked)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    def _copy_selection(self):
        from pywinauto.keyboard import send_keys
        self._clear_clipboard()
        self.focus_guard()
        send_keys("^c")
        time.sleep(.3)
        return self._read_clipboard().strip().replace("\x00", "")

    def _editor_text_near(self, x, y):
        """Read a cell editor sitting on the remark. Do not log the text."""
        for class_name in ("Edit", "TEdit", "TMemo", "TInplaceEdit", "TITSEdit"):
            for edit in self.child_boxes(class_name):
                if abs(edit[0] - x) > 0.12 or abs(edit[1] - y) > 0.08:
                    continue
                text = self.read_control_text(edit[2]).strip()
                if remark_is_complete(text):
                    return text
        focused = self._focused_control_text()
        if remark_is_complete(focused):
            return focused
        return ""

    def _focused_control_text(self):
        user32 = ctypes.windll.user32
        user32.GetFocus.restype = ctypes.c_void_p
        hwnd = user32.GetFocus()
        if not hwnd:
            return ""
        return self.read_control_text(hwnd).strip()

    def remark_on_row(self, company):
        """Copy the full 备注 cell. The grid may only paint an ellipsis. Do not log the text."""
        from pywinauto.keyboard import send_keys
        items = self.snap((.02, .28, .99, .92))
        point = choose_remark_point(items, company.name)
        if point is None:
            return ""
        self.click_point(point["x"], point["y"], double=True)
        time.sleep(.25)
        edited = self._editor_text_near(point["x"], point["y"])
        if edited:
            send_keys("{ESC}")
            return edited
        self.focus_guard()
        send_keys("^a")
        copied = self._copy_selection()
        send_keys("{ESC}")
        if remark_is_complete(copied):
            return copied
        self.click_point(point["x"], point["y"])
        copied = self._copy_selection()
        if remark_is_complete(copied):
            return copied
        return ""

    def _set_clipboard(self, text):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.OpenClipboard.restype = ctypes.c_bool
        user32.EmptyClipboard.restype = ctypes.c_bool
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p
        user32.CloseClipboard.restype = ctypes.c_bool
        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        data = str(text).encode("utf-16-le") + b"\x00\x00"
        handle = kernel32.GlobalAlloc(0x0002, len(data))
        if not handle:
            return False
        locked = kernel32.GlobalLock(handle)
        if not locked:
            return False
        ctypes.memmove(locked, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.OpenClipboard(None):
            return False
        try:
            user32.EmptyClipboard()
            if not user32.SetClipboardData(13, handle):
                return False
        finally:
            user32.CloseClipboard()
        return True

    def _clear_clipboard(self):
        user32 = ctypes.windll.user32
        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.OpenClipboard.restype = ctypes.c_bool
        user32.EmptyClipboard.restype = ctypes.c_bool
        user32.CloseClipboard.restype = ctypes.c_bool
        if user32.OpenClipboard(None):
            user32.EmptyClipboard()
            user32.CloseClipboard()

    def close_virtual_keyboard(self):
        """The password box opens its own keyboard. Close that keyboard before 确定."""
        from pywinauto import Desktop
        if self.root is None:
            return
        pid = self.root.process_id()
        for window in live_windows():
            try:
                if not window.is_visible() or window.process_id() != pid:
                    continue
                cls = window.class_name() or ""
                if cls in ("Tfrm_MainFrame", "TfrmLoginAuth", "TApplication", "TFormShadow", "SoPY_Status"):
                    continue
                if (window.window_text() or "") == "结果":
                    continue
                rect = window.rectangle()
                if rect.width() < 80 or rect.height() < 40 or rect.width() > 800:
                    continue
            except Exception:
                continue
            current = self.root
            self.root = window
            try:
                items = self.snap((0, 0, 1, 1))
            finally:
                self.root = current
            button = choose_keyboard_close(items)
            if button is None:
                continue
            self.focus_handle(window)
            window.click_input(coords=(round(button["x"] * rect.width()), round(button["y"] * rect.height())))
            self.log("已关闭虚拟键盘")
            time.sleep(.3)
            return

    def find_login_dialog(self):
        from pywinauto import Desktop
        if not self.root:
            return None
        pid = self.root.process_id()
        found = []
        for window in live_windows():
            try:
                if window.class_name() != "TfrmLoginAuth" or not window.is_visible():
                    continue
                if window.process_id() != pid:
                    continue
                rect = window.rectangle()
                if rect.width() < 200 or rect.height() < 200:
                    continue
                found.append(window)
            except Exception:
                continue
        if len(found) > 1:
            self.fail("出现多个申报密码窗口，已停止")
        return found[0] if found else None

    def paste_declare_password(self, secret):
        """Paste the row remark into 请输入申报密码. Never log or keep the value."""
        from pywinauto.keyboard import send_keys
        secret = (secret or "").strip()
        dialog = self.find_login_dialog()
        if dialog is None or not secret:
            self.log("请在扣缴端窗口完成该公司登录，完成后程序会继续。")
            return
        main = self.root
        self.root = dialog
        try:
            edits = self.child_boxes("TXPFamerEditEx")
            edit = choose_password_edit(edits)
            if edit is None:
                self.fail("没有找到申报密码输入框")
            rect = dialog.rectangle()
            self.focus_handle(dialog)
            dialog.click_input(coords=(round(edit[0] * rect.width()), round(edit[1] * rect.height())))
            if not self._set_clipboard(secret):
                self.fail("无法填入申报密码")
            send_keys("^v")
            time.sleep(.4)
            self.close_virtual_keyboard()
            self.root = dialog
            items = self.snap((0, 0, 1, 1))
            keyboard_close = choose_exact_button(items, "关闭")
            if keyboard_close is not None:
                dialog.click_input(coords=(round(keyboard_close["x"] * rect.width()),
                                           round(keyboard_close["y"] * rect.height())))
                self.log("已关闭虚拟键盘")
                time.sleep(.3)
            items = self.snap((.25, .85, .8, .99))
            button = choose_exact_button(items, "确定")
            if button is None:
                self.fail("申报密码窗口没有找到确定")
            dialog.click_input(coords=(round(button["x"] * rect.width()), round(button["y"] * rect.height())))
            self.log("已提交申报密码")
        finally:
            secret = ""
            self._clear_clipboard()
            self.root = main

    def login_company(self, company):
        """Later companies are entered from the 单位管理 dialog, then 进入."""
        if self.footer_matches(company):
            self.log(f"已登录目标公司：{company.name}")
            return
        self.log(f"当前公司不是目标公司，打开单位管理登录：{company.name}")
        self.dismiss_declare_success()
        dialog = self.find_company_dialog()
        if dialog is None and self.find_login_dialog() is not None:
            self.log("检测到正在登录，等待登录完成后继续")
            self.wait_logged_in(company)
            return
        if dialog is None:
            dialog = self.open_company_management(company)
        if dialog is None:
            return
        main = self.root
        self.root = dialog
        remark = ""
        try:
            self.log(f"单位管理已打开，正在登录：{company.name}")
            self.search_company_in_dialog(company)
            self.scroll_result_to_end()
            remark = self.remark_on_row(company)
            if not remark:
                self.fail("查询结果里没有读到该行备注")
            if not self.open_company_row(company):
                self.fail("单位管理中没有找到该公司唯一的「进入」或「登录」")
            self.log(f"已点击进入：{company.name}")
        finally:
            self.root = main
        try:
            self.wait_logged_in(company, declare_password=remark)
        finally:
            remark = ""
            self._clear_clipboard()

    def open_company_row(self, company):
        from pywinauto.keyboard import send_keys
        deadline = time.monotonic() + 20
        scrolls = 0
        while time.monotonic() < deadline:
            self.cancelled()
            items = self.snap((.02, .30, .99, .88))
            names = [item for item in items if compact(item["text"]) == compact(company.name)]
            if len(names) > 1:
                self.fail("单位管理按公司名称搜索结果不唯一")
            hit = choose_row_action(items, company.name) or choose_single_enter(items)
            if hit is not None:
                self.click_point(hit["x"], hit["y"])
                return True
            if len(names) == 1 and scrolls < 4:
                self.click_point(names[0]["x"], names[0]["y"])
                self.focus_guard()
                send_keys("{RIGHT 6}")
                scrolls += 1
                time.sleep(.4)
                continue
            time.sleep(.5)
        return False

    def submit_login_password(self, company):
        try:
            password = self.password_provider(company.name)
        except Exception:
            password = ""
        if not password:
            self.log("请在扣缴端窗口完成该公司登录，完成后程序会继续。")
            return
        modal = compact(self.read((.28, .24, .73, .82)))
        if compact(company.name) not in modal or (company.tax_id.strip() and company.tax_id.strip() not in modal):
            self.fail("登录框的公司信息不匹配")
        self.click_point(.48, .56)
        self.type_literal(password, replace=True)
        password = None
        self.click_text("确定", (.49, .77, .59, .83), partial=False)

    def wait_logged_in(self, company, timeout=120, declare_password=""):
        deadline = time.monotonic() + timeout
        prompted = False
        try:
            while time.monotonic() < deadline:
                self.cancelled()
                if not self.user32.IsWindow(self.root.handle):
                    self.attach()
                if self.footer_matches(company):
                    self.log(f"已登录：{company.name}")
                    return
                screen = compact(self.read((.15, .12, .99, .95)))
                if any(token in screen for token in ("验证码", "密码错误", "登录失败")):
                    self.fail("扣缴端登录未通过，请人工查看当前页面")
                if not prompted and (self.find_login_dialog() is not None or "请输入申报密码" in screen):
                    prompted = True
                    self.paste_declare_password(declare_password)
                    declare_password = ""
                time.sleep(.6)
        finally:
            declare_password = ""
            self._clear_clipboard()
        self.fail("单位管理登录后，当前操作单位仍不是目标公司")

    def prepare(self, company):
        self.last_error = ""
        self.attach()
        self.dismiss_zero_income_prompt()
        self.wait_until_idle()
        self.login_company(company)
        # Login can rebuild the main frame. Bind the current window before
        # verifying identity/period and entering the declaration workflow.
        self.attach()
        self.identity(company)
        # Never infer September from the workbench's October. The explicit
        # configured period must already be selected in the client.
        self.month(company)
        self.return_to_step_list()
        self.click_text("综合所得申报", (.03,.40,.18,.48), partial=False)
        self.wait_ready(("收入及减除填写", "税款计算"))
        self.guard(company)
        # Stay on 收入及减除填写. 申报表报送 is only used after 附表填写.
        return "READY"

    def return_to_step_list(self):
        """A detail page such as 正常工资薪金所得 hides the four step tabs behind 返回."""
        for _ in range(3):
            header = compact(self.read((.18, .12, .98, .40)))
            if "收入及减除填写" in header and "税款计算" in header:
                return
            if "返回" not in header:
                return
            self.log("从明细页返回综合所得步骤")
            self.click_text("返回", (.18, .24, .36, .34), partial=False)
            time.sleep(.5)

    def check_zero(self, company):
        self.attach()
        self.guard(company)
        self.click_text("收入及减除填写", (.22,.19,.40,.26))
        self.wait_ready(("正常工资薪金所得",))
        self.click_text("正常工资薪金所得", (.21,.37,.46,.44), partial=False)
        self.wait_ready(("申报总人数", "收入总额", "更多操作"))
        self.guard(company)
        try:
            count = salary_totals(self.read((.49,.20,.91,.25)))
        except DesktopError as exc:
            self.fail(str(exc))
        if count == 0:
            self.log("工资表为空，确认后复制上月当期数据")
            copied = self.copy_previous_month() if (self.confirm_import_dialog() or self.find_import_wizard()) else False
            if not copied:
                self.fail("工资表为空，未出现复制上月当期数据")
            self.wait_ready(("申报总人数", "收入总额", "更多操作"))
            self.guard(company)
            try:
                count = salary_totals(self.read((.49,.20,.91,.25)))
            except DesktopError as exc:
                self.fail(str(exc))
        if count != company.expected_people:
            self.fail(f"申报人数{count}与配置人数{company.expected_people}不一致")
        self.click_text("返回", (.21,.25,.28,.32), partial=False)
        self.wait_ready(("收入及减除填写", "正常工资薪金所得"))

    def calculate(self, company):
        self.guard(company)
        self.click_text("税款计算", (.41,.19,.59,.26))
        mode = self.wait_ready(("附表填写",), allow_insurance=company.allow_missing_insurance)
        if mode == "INSURANCE":
            self.click_text("继续算税", (.59,.69,.68,.75), partial=False)
            self.wait_ready(("附表填写",))
        self.guard(company)

    def calculation_is_ready(self):
        """The tax-calculation banner must show this period and only zero amounts."""
        text = compact(self.read((.18, .26, .98, .40)))
        if "税款计算" not in text or "应补退税额" not in text:
            return False
        amounts = re.findall(r"(?:本期收入总额|应补退税额)[:：]?([+-]?[\d,]+\.\d{2})", text)
        if len(amounts) < 2:
            self.fail("税款计算金额无法核验")
        for amount in amounts:
            require_zero(amount)
        return True

    def click_continue_declare(self):
        """After 发送申报, accept only the 继续申报 button."""
        from pywinauto import Desktop
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            self.cancelled()
            if self.find_named_popup("进度") is not None:
                time.sleep(.4)
                continue
            pid = self.root.process_id()
            for window in live_windows():
                try:
                    if not window.is_visible() or window.process_id() != pid:
                        continue
                    if window.class_name() == "Tfrm_MainFrame":
                        continue
                    rect = window.rectangle()
                    if rect.width() < 180 or rect.height() < 80:
                        continue
                    items, width, height = self.wizard_items(window)
                except Exception:
                    continue
                button = choose_continue_declare(items)
                if button is None:
                    continue
                self.log("点击继续申报")
                self.focus_handle(window)
                window.click_input(coords=(round(button["x"] * width), round(button["y"] * height)))
                time.sleep(.4)
                return
            time.sleep(.4)
        self.log("未出现继续申报，继续核验申报结果")

    def check_attachments(self, company):
        self.wait_until_idle()
        self.click_text("附表填写", (.61,.19,.79,.26))
        self.wait_ready(("其他附表", "减免事项附表"))
        self.guard(company)
        # The video has three attachment rows. Read each status cell separately.
        for region in ((.72,.39,.85,.435), (.72,.45,.85,.495), (.72,.51,.85,.555)):
            if compact(self.read(region)) != "无需填写":
                self.fail("附表不是视频中的三项无需填写状态，请人工处理")

    def check_summary(self, company):
        self.wait_until_idle()
        self.click_text("申报表报送", (.81,.19,.98,.26))
        self.wait_ready(("申报状态", "本期收入"))
        self.validate_summary(company)

    def validate_summary(self, company):
        self.guard(company)
        columns = [("本期收入",.345,.463), ("累计收入",.464,.581),
                   ("应纳税额",.582,.699), ("已缴税额",.701,.818),
                   ("税额",.82,.94)]
        for label,left,right in columns:
            header = compact(self.read((left,.42,right,.47)))
            if label not in header:
                self.fail("申报汇总列标题无法核验")
            try:
                require_zero(self.read((left,.48,right,.535)))
            except DesktopError as exc:
                self.fail(f"{label}：{exc}")

    def submit(self, company):
        # Re-read immediately before the one and only submission click.
        self.wait_until_idle()
        self.validate_summary(company)
        status = compact(self.read((.30,.365,.53,.41)))
        if status not in ("待申报", "未申报"):
            self.fail("提交前申报状态已变化，禁止再次发送")
        self.evidence(company, "BEFORE_SUBMIT")
        self.click_text("发送申报", (.86,.33,.98,.405), partial=False)
        self.log("已点击发送申报，正在核验税局反馈")
        self.click_continue_declare()

    def filing_status_text(self):
        return compact(self.read((.30, .365, .53, .41)))

    def verify(self, company):
        deadline = time.monotonic()+int(self.settings.get("feedback_timeout",180))
        last_refresh = 0
        while time.monotonic() < deadline:
            self.wait_ready(("申报状态",), timeout=90)
            self.guard(company)
            status = compact(self.read((.30,.365,.53,.41)))
            feedback = compact(self.read((.21,.74,.96,.94)))
            # Contradictory feedback is not success, even when an older status
            # cell still says success/no-payment.
            try:
                confirmed = success_feedback(status, feedback)
            except DesktopError as exc:
                self.fail(str(exc))
            if confirmed:
                self.validate_summary(company)
                return True
            buttons = compact(self.read((.55,.33,.98,.405)))
            if "获取反馈" in buttons and time.monotonic()-last_refresh > 5:
                self.click_text("获取反馈", (.55,.33,.98,.405), partial=False)
                last_refresh = time.monotonic()
            time.sleep(1)
        self.fail("未取得明确的申报成功且无需缴款反馈，禁止重复提交")

    def evidence(self, company, state):
        # Never capture login/password dialogs, including on failures.
        if self.last_image is None or self.root is None:
            return ""
        try:
            # Evidence collection is read-only. Never switch companies or log
            # in while handling a failure: it hides the original failure state.
            if not self.footer_matches(company):
                return ""
            screen = compact(self.read((.19,.12,.99,.95)))
            if any(t in screen for t in ("密码", "在线登录", "验证码")):
                return ""
            folder = self.base / "data" / "evidence"
            folder.mkdir(parents=True, exist_ok=True)
            stem = company.tax_id.strip() or company.name.strip()
            stem = re.sub(r'[<>:"/\\|?*]', "_", stem)[:80] or "company"
            path = folder / f"{stem}_{company.month}_{state}_{datetime.now():%Y%m%d_%H%M%S}.png"
            self.last_image.save(path)
            return str(path.resolve())
        except Exception:
            return ""
