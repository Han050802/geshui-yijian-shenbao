"""Portable runtime for the editable scripts in the external wechat folder."""
from __future__ import annotations

from contextlib import ExitStack
import importlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import runpy
import socket
import subprocess
import sys
import tempfile
from unittest.mock import patch


# These imports are checked without creating a WeChat client or an OCR request.
# PyInstaller must collect the modules and native extensions listed here.
DEPENDENCY_MODULES = (
    "tkinter", "tkinter.ttk", "tkinter.messagebox", "tkinter.scrolledtext",
    "PIL", "PIL.Image", "PIL.ImageGrab", "pyperclip",
    "pythoncom", "pywintypes", "win32api", "win32con", "win32gui",
    "win32ui", "win32process", "win32clipboard", "win32com.client",
    "comtypes", "comtypes.client", "comtypes.stream",
    "colorama", "httpx", "psutil", "tenacity",
    "openpyxl", "et_xmlfile",
    "winrt.windows.foundation", "winrt.windows.foundation.collections",
    "winrt.windows.globalization", "winrt.windows.graphics.imaging",
    "winrt.windows.media.ocr", "winrt.windows.storage.streams", "winocr",
    "wxauto4", "wxauto4.param", "wxauto4.uia.uiautomation",
)


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent.parent
    return Path(__file__).resolve().parent


def configure_source_path(base: Path) -> Path:
    source = Path(base).resolve() / "wechat"
    for name in ("app.py", "wechat_report.py", "wechat_a11y.py", "group_mapping.py"):
        if not (source / name).is_file():
            raise FileNotFoundError(f"微信程序文件缺失：wechat/{name}。请完整解压程序。")
    source_text = str(source)
    sys.path[:] = [source_text, *[item for item in sys.path if item != source_text]]
    importlib.invalidate_caches()
    return source


def _load_external_module(name: str, source: Path):
    """Always use the editable source, even if a frozen copy also exists."""
    path = source / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载微信程序文件：{path.name}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
        raise
    return module


def _blocked_operation(*args, **kwargs):
    raise RuntimeError("离线自检禁止网络连接、启动其他程序、截图或剪贴板操作")


def _empty_tk_check():
    import tkinter as tk

    root = tk.Tk()
    try:
        root.withdraw()
        root.update_idletasks()
        return {"tcl": str(root.tk.call("info", "patchlevel")), "tk": str(tk.TkVersion)}
    finally:
        root.destroy()


def _excel_mapping_check(source):
    """Exercise the bundled Excel engine without reading business mappings."""
    module = _load_external_module("group_mapping", source)
    with tempfile.TemporaryDirectory(prefix="TaxZero-Excel-selftest-") as temporary:
        store = module.GroupMappingStore(Path(temporary) / "配对自检.xlsx")
        store.create()
        store.upsert("离线测试公司", "离线测试财务服务群")
        if store.resolve("离线测试公司") != "离线测试财务服务群":
            raise RuntimeError("Excel配对写入读取不一致")
        return {"create_write_read": "ok", "business_files_accessed": False}


def _wechat_ui_check(source):
    """Create the complete pairing window with all business activity disabled."""
    with patch("tkinter.messagebox.showerror", side_effect=RuntimeError("微信配对界面模块无法加载")):
        module = _load_external_module("app", source)
    with patch.object(module.ReportApp, "_install_log_handler", lambda self: None):
        window = module.ReportApp(offline_self_test=True)
        try:
            window.withdraw()
            window.update_idletasks()
            checks = {
                "excel_search_button": bool(window.search_button.winfo_exists()),
                "group_registration_button": bool(window.wait_group_button.winfo_exists()),
                "existing_group_pairing_button": bool(window.pair_existing_button.winfo_exists()),
                "mapped_group_readonly": str(window.target_entry.cget("state")) == "readonly",
                "no_company_or_destination_loaded": not window.company_var.get() and not window.target_var.get(),
                "no_worker_started": window.running is False,
            }
            if not all(checks.values()):
                raise RuntimeError("微信Excel配对界面离线检查失败")
            return checks
        finally:
            window.destroy()


def _write_self_test_result(base: Path, result: dict) -> int:
    try:
        destination = Path(base) / "data" / "wechat_self_test.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return 1
    return 0 if result["ok"] else 1


def run_self_test(base: Path) -> int:
    """Check dependencies and source paths; never enter the business window."""
    base = Path(base).resolve()
    result = {"ok": False, "mode": "offline", "checks": {}}
    checks = result["checks"]

    def check(name, operation):
        try:
            detail = operation()
            checks[name] = {"ok": True}
            if detail is not None:
                checks[name]["detail"] = detail
            return True
        except Exception as exc:
            checks[name] = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
            return False

    source = None
    try:
        source = configure_source_path(base)
        checks["external_source"] = {"ok": True}
    except Exception as exc:
        checks["external_source"] = {"ok": False, "error": type(exc).__name__, "message": str(exc)}

    if sys.platform != "win32":
        checks["windows"] = {"ok": False, "message": "此运行组件需要 Windows。"}
    else:
        checks["windows"] = {"ok": True}
        original_cwd = Path.cwd()
        # The dependency creates a log on import. Keep it away from user data.
        with tempfile.TemporaryDirectory(prefix="TaxZero-WeChat-selftest-") as temporary:
            try:
                os.chdir(temporary)
                with ExitStack() as guards:
                    for target, attribute in (
                        (socket, "create_connection"), (socket.socket, "connect"),
                        (socket.socket, "connect_ex"), (subprocess, "Popen"),
                    ):
                        guards.enter_context(patch.object(target, attribute, _blocked_operation))
                    # Install capture/clipboard guards before wxauto4 is imported.
                    for module_name, attributes in (
                        ("PIL.ImageGrab", ("grab", "grabclipboard")),
                        ("pyperclip", ("copy", "paste")),
                        ("win32clipboard", ("OpenClipboard",)),
                    ):
                        try:
                            module = importlib.import_module(module_name)
                            for attribute in attributes:
                                guards.enter_context(patch.object(module, attribute, _blocked_operation))
                        except Exception as exc:
                            checks[f"guard:{module_name}"] = {
                                "ok": False, "error": type(exc).__name__, "message": str(exc),
                            }
                    for name in DEPENDENCY_MODULES:
                        def import_dependency(module_name=name):
                            importlib.import_module(module_name)
                        check(f"import:{name}", import_dependency)
                    check("tk_window", _empty_tk_check)
                    if source is not None:
                        for name in ("wechat_report", "wechat_a11y", "group_mapping"):
                            def import_source(module_name=name):
                                module = _load_external_module(module_name, source)
                                if Path(module.__file__).resolve() != source / f"{module_name}.py":
                                    raise RuntimeError("微信模块未从外部源码目录加载")
                            check(f"source:{name}", import_source)
                        check("excel_mapping", lambda: _excel_mapping_check(source))
                        check("excel_pairing_window", lambda: _wechat_ui_check(source))
            finally:
                # wxauto4's import-time log must be closed before temp cleanup on Windows.
                logging.shutdown()
                os.chdir(original_cwd)

    result["ok"] = bool(checks) and all(item["ok"] for item in checks.values())
    return _write_self_test_result(base, result)


def main(argv=None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    base = base_dir()
    # This branch must remain before any loading of the business GUI script.
    if "--self-test" in arguments:
        try:
            return run_self_test(base)
        except Exception as exc:
            return _write_self_test_result(base, {
                "ok": False, "mode": "offline", "checks": {
                    "self_test_runtime": {"ok": False, "error": type(exc).__name__, "message": str(exc)},
                },
            })
    source = configure_source_path(base)
    for name in ("wechat_report", "wechat_a11y"):
        _load_external_module(name, source)
    previous_argv = sys.argv
    previous_cwd = Path.cwd()
    try:
        os.chdir(source)
        sys.argv = [str(source / "app.py"), *arguments]
        runpy.run_path(str(source / "app.py"), run_name="__main__")
    finally:
        sys.argv = previous_argv
        os.chdir(previous_cwd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

