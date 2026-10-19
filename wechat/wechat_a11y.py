# -*- coding: utf-8 -*-
"""Turn on WeChat's built-in accessibility switch so the chat UI can be seen.

WeChat 4.1 keeps that switch off until a screen reader asks for it. The main
window then stays an empty Qt shell, and wxauto reports that no logged-in
window exists. This module sets the switch in the running WeChat process and
checks that the mmui controls actually appear. A wrong address is restored.
"""

from __future__ import annotations

import ctypes
import logging
import struct
import time
from ctypes import wintypes
from pathlib import Path

logger = logging.getLogger("wechat_report")

PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_QUERY_INFORMATION = 0x0400
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
IMAGE_SCN_MEM_EXECUTE = 0x20000000
IMAGE_SCN_MEM_WRITE = 0x80000000
WM_GETOBJECT = 0x003D
OBJID_CLIENT = 0xFFFFFFFC

# Verified switch locations. A newer build is discovered by scanning Weixin.dll.
KNOWN_GATE_RVA = {
    "4.1.11.22": 0x0A1E7DB8,
    "4.1.13.65": 0x0AE2B0C8,
    "4.1.15.13": 0x0B135C38,
}
GATE_PATTERN = (
    b"\x48\x85\xc9\x0f\x84....\x80\x3d(?P<disp>....)\x00\x0f\x84"
)


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("th32ModuleID", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("GlblcntUsage", ctypes.c_ulong),
        ("ProccntUsage", ctypes.c_ulong),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_ubyte)),
        ("modBaseSize", ctypes.c_ulong),
        ("hModule", ctypes.c_void_p),
        ("szModule", ctypes.c_wchar * 256),
        ("szExePath", ctypes.c_wchar * 260),
    ]


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.ReadProcessMemory.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.ReadProcessMemory.restype = wintypes.BOOL
    kernel32.WriteProcessMemory.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.WriteProcessMemory.restype = wintypes.BOOL
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Module32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MODULEENTRY32W)]
    kernel32.Module32FirstW.restype = wintypes.BOOL
    kernel32.Module32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MODULEENTRY32W)]
    kernel32.Module32NextW.restype = wintypes.BOOL
    return kernel32


def _pid_from_hwnd(hwnd: int) -> int:
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _weixin_dll(pid: int) -> tuple[int, str] | None:
    kernel32 = _kernel32()
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return None
    entry = MODULEENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        if not kernel32.Module32FirstW(snapshot, ctypes.byref(entry)):
            return None
        while True:
            if entry.szModule.lower() == "weixin.dll":
                base = ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value or 0
                return int(base), entry.szExePath
            if not kernel32.Module32NextW(snapshot, ctypes.byref(entry)):
                return None
    finally:
        kernel32.CloseHandle(snapshot)


def _read_byte(handle, address: int) -> int | None:
    kernel32 = _kernel32()
    buffer = (ctypes.c_ubyte * 1)()
    read = ctypes.c_size_t()
    ok = kernel32.ReadProcessMemory(handle, address, buffer, 1, ctypes.byref(read))
    if not ok or read.value != 1:
        return None
    return int(buffer[0])


def _write_byte(handle, address: int, value: int) -> bool:
    kernel32 = _kernel32()
    buffer = (ctypes.c_ubyte * 1)(value & 0xFF)
    written = ctypes.c_size_t()
    ok = kernel32.WriteProcessMemory(handle, address, buffer, 1, ctypes.byref(written))
    return bool(ok and written.value == 1)


def _wake_window(hwnd: int) -> None:
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, 9)
    user32.SendMessageW(hwnd, WM_GETOBJECT, 0, ctypes.c_long(OBJID_CLIENT).value)


def _mmui_ready(hwnd: int) -> bool:
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


def _wait_until_ready(hwnd: int, timeout: float = 6.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        _wake_window(hwnd)
        if _mmui_ready(hwnd):
            return True
        time.sleep(0.25)
    return _mmui_ready(hwnd)


def _pe_sections(data: bytes) -> list[dict]:
    try:
        pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe_offset:pe_offset + 4] != b"PE\0\0":
            return []
        coff = pe_offset + 4
        count = struct.unpack_from("<H", data, coff + 2)[0]
        optional_size = struct.unpack_from("<H", data, coff + 16)[0]
        section_offset = coff + 20 + optional_size
        sections = []
        for index in range(count):
            offset = section_offset + index * 40
            virtual_size, virtual_address, raw_size, raw_pointer = struct.unpack_from("<IIII", data, offset + 8)
            characteristics = struct.unpack_from("<I", data, offset + 36)[0]
            sections.append(
                {
                    "rva": virtual_address,
                    "vsize": virtual_size,
                    "raw_size": raw_size,
                    "raw_ptr": raw_pointer,
                    "chars": characteristics,
                }
            )
        return sections
    except Exception:
        return []


def _section_for_rva(sections: list[dict], rva: int) -> dict | None:
    for section in sections:
        size = max(section["vsize"], section["raw_size"])
        if section["rva"] <= rva < section["rva"] + size:
            return section
    return None


def _offset_to_rva(sections: list[dict], offset: int) -> int | None:
    for section in sections:
        if section["raw_ptr"] <= offset < section["raw_ptr"] + section["raw_size"]:
            return section["rva"] + offset - section["raw_ptr"]
    return None


def _scan_gate_rvas(dll_path: str) -> list[int]:
    import re

    try:
        data = Path(dll_path).read_bytes()
    except OSError:
        return []
    sections = _pe_sections(data)
    if not sections:
        return []
    found: list[int] = []
    pattern = re.compile(GATE_PATTERN, re.DOTALL)
    for match in pattern.finditer(data):
        match_rva = _offset_to_rva(sections, match.start())
        disp_rva = _offset_to_rva(sections, match.start("disp"))
        if match_rva is None or disp_rva is None:
            continue
        match_section = _section_for_rva(sections, match_rva)
        if not match_section or not (match_section["chars"] & IMAGE_SCN_MEM_EXECUTE):
            continue
        compare_rva = disp_rva - 2
        displacement = struct.unpack("<i", match.group("disp"))[0]
        target_rva = compare_rva + 7 + displacement
        target_section = _section_for_rva(sections, target_rva)
        if not target_section or not (target_section["chars"] & IMAGE_SCN_MEM_WRITE):
            continue
        if target_rva not in found:
            found.append(target_rva)
    return found[:4]


def _candidate_rvas(dll_path: str) -> list[int]:
    version = Path(dll_path).parent.name
    candidates: list[int] = []
    known = KNOWN_GATE_RVA.get(version)
    if known is not None:
        candidates.append(known)
    if candidates:
        return candidates
    for rva in _scan_gate_rvas(dll_path):
        if rva not in candidates:
            candidates.append(rva)
    return candidates


def _try_candidates(hwnd: int, pid: int, base: int, rvas: list[int]) -> bool:
    if not rvas:
        return False
    kernel32 = _kernel32()
    access = PROCESS_QUERY_INFORMATION | PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION
    handle = kernel32.OpenProcess(access, False, pid)
    if not handle:
        logger.warning("无法读取微信进程，请不要用管理员身份单独打开微信或本程序。")
        return False
    try:
        for rva in rvas:
            address = base + int(rva)
            current = _read_byte(handle, address)
            if current not in {0, 1}:
                continue
            wrote = False
            if current != 1:
                if not _write_byte(handle, address, 1):
                    continue
                wrote = True
                logger.info("已打开微信界面识别开关：Weixin.dll+0x%x", rva)
            if _wait_until_ready(hwnd):
                return True
            # 已知地址写上之后要留着。微信还在登录时控件会晚几秒出现，立刻改回 0 会把开关关掉。
            if wrote and int(rva) not in KNOWN_GATE_RVA.values():
                _write_byte(handle, address, current)
    finally:
        kernel32.CloseHandle(handle)
    return False


def prime_wechat_accessibility(hwnd: int) -> None:
    """Turn the known switch on. Do not wait, and do not touch any other address."""
    pid = _pid_from_hwnd(hwnd)
    if not pid:
        return
    module = _weixin_dll(pid)
    if module is None:
        return
    base, dll_path = module
    rva = KNOWN_GATE_RVA.get(Path(dll_path).parent.name)
    if rva is None:
        return
    kernel32 = _kernel32()
    access = PROCESS_QUERY_INFORMATION | PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION
    handle = kernel32.OpenProcess(access, False, pid)
    if not handle:
        return
    try:
        address = base + int(rva)
        current = _read_byte(handle, address)
        if current == 0 and _write_byte(handle, address, 1):
            logger.info("已打开微信界面识别开关：Weixin.dll+0x%x", rva)
            _wake_window(hwnd)
    finally:
        kernel32.CloseHandle(handle)


def activate_wechat_controls(hwnd: int) -> bool:
    """Make the logged-in WeChat window expose its chat controls. Return whether it worked."""
    if _mmui_ready(hwnd):
        return True
    pid = _pid_from_hwnd(hwnd)
    if not pid:
        return False
    module = _weixin_dll(pid)
    if module is None:
        logger.warning("微信进程里没有找到 Weixin.dll。")
        return False
    base, dll_path = module
    candidates = _candidate_rvas(dll_path)
    if _try_candidates(hwnd, pid, base, candidates):
        return True
    logger.warning("微信界面控件仍未出现。")
    return False
