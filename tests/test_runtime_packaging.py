"""Mock-only packaging checks; no browser, desktop input or real filing data."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runtime_checks
from web_workbench import Workbench


class BrowserSelectionTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "url": "https://example.test/", "work_month": "2026-10",
            "tax_month": "2026-09",
        }
        self.workbench = Workbench(self.settings, ".")
        self.workbench.playwright = Mock()
        self.page = Mock(url=self.workbench._url())
        context = self.workbench.playwright.chromium.launch_persistent_context.return_value
        context.pages = [self.page]

    def test_source_mode_preserves_installed_chrome_and_browser_environment(self):
        with patch.object(sys, "frozen", False, create=True), patch.dict(
                os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "host-cache"}):
            self.workbench._open_target(self.workbench._url(), RuntimeError, None)
            self.assertEqual(os.environ["PLAYWRIGHT_BROWSERS_PATH"], "host-cache")
        options = self.workbench.playwright.chromium.launch_persistent_context.call_args.kwargs
        self.assertEqual(options["channel"], "chrome")
        self.assertFalse(options["headless"])

    def test_frozen_mode_uses_bundled_browser_and_overrides_host_cache(self):
        with patch.object(sys, "frozen", True, create=True), patch.dict(
                os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "host-cache"}):
            self.workbench._open_target(
                self.workbench._url(), RuntimeError, ["--no-proxy-server"],
            )
            self.assertEqual(os.environ["PLAYWRIGHT_BROWSERS_PATH"], "0")
        options = self.workbench.playwright.chromium.launch_persistent_context.call_args.kwargs
        self.assertNotIn("channel", options)
        self.assertFalse(options["headless"])
        self.assertEqual(options["args"], ["--no-proxy-server"])

    def test_frozen_environment_is_configured_before_driver_starts(self):
        api = ModuleType("playwright.sync_api")
        api.TimeoutError = RuntimeError
        api.sync_playwright = Mock()
        api.sync_playwright.return_value.start.side_effect = (
            lambda: self.assertEqual(os.environ["PLAYWRIGHT_BROWSERS_PATH"], "0")
        )
        with patch.dict(sys.modules, {"playwright.sync_api": api}), patch.object(
                sys, "frozen", True, create=True), patch.dict(
                os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "host-cache"}), patch.object(
                self.workbench, "_open_target"):
            self.workbench.open()
        api.sync_playwright.return_value.start.assert_called_once()


class RuntimeSelfTestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.pywinauto = ModuleType("pywinauto")
        self.ocr = ModuleType("rapidocr_onnxruntime")
        self.ocr.RapidOCR = Mock()
        self.tk = ModuleType("tkinter")
        self.tk.Tk = Mock()
        self.playwright = ModuleType("playwright")
        self.playwright.__file__ = str(self.base / "fake-package" / "__init__.py")
        self.api = ModuleType("playwright.sync_api")
        self.api.sync_playwright = Mock()
        self.manager = MagicMock()
        self.api.sync_playwright.return_value = self.manager
        self.engine = self.manager.__enter__.return_value
        self.executable = (
            self.base / "fake-package" / "driver" / "package" / ".local-browsers"
            / "chromium-test" / "chrome-win" / "chrome.exe"
        )
        self.executable.parent.mkdir(parents=True)
        self.executable.write_bytes(b"fake executable; never run")
        self.engine.chromium.executable_path = str(self.executable)
        self.context = self.engine.chromium.launch_persistent_context.return_value
        self.page = self.context.new_page.return_value
        self.page.url = "about:blank"
        self.page.title.return_value = runtime_checks.OFFLINE_TITLE
        self.page.locator.return_value.inner_text.return_value = "ok"
        modules = {
            "pywinauto": self.pywinauto,
            "rapidocr_onnxruntime": self.ocr,
            "tkinter": self.tk,
            "playwright": self.playwright,
            "playwright.sync_api": self.api,
        }
        for guard in (patch.dict(sys.modules, modules), patch.dict(os.environ)):
            guard.start()
            self.addCleanup(guard.stop)

    def report(self):
        return json.loads((self.base / "data" / "self_test.json").read_text(encoding="utf-8"))

    def test_success_uses_offline_temporary_profile_and_preserves_business_files(self):
        data = self.base / "data"
        data.mkdir()
        settings = self.base / "settings.json"
        state = data / "state.json"
        settings.write_text("test-only settings sentinel", encoding="utf-8")
        state.write_text("test-only state sentinel", encoding="utf-8")
        self.assertEqual(runtime_checks.run_self_test(self.base), 0)
        self.assertEqual(self.report(), {name: "ok" for name in runtime_checks.CHECK_NAMES})
        self.assertEqual(settings.read_text(encoding="utf-8"), "test-only settings sentinel")
        self.assertEqual(state.read_text(encoding="utf-8"), "test-only state sentinel")
        self.assertFalse((data / "browser_profile").exists())
        self.assertEqual(os.environ["PLAYWRIGHT_BROWSERS_PATH"], "0")
        call = self.engine.chromium.launch_persistent_context.call_args
        self.assertTrue(call.kwargs["headless"])
        self.assertTrue(call.kwargs["offline"])
        self.assertEqual(call.kwargs["service_workers"], "block")
        self.assertEqual(Path(call.kwargs["executable_path"]).resolve(), self.executable.resolve())
        self.assertNotIn("channel", call.kwargs)
        self.assertFalse(Path(call.args[0]).exists())
        self.assertNotIn(str(data), call.args[0])
        self.page.goto.assert_not_called()
        self.page.set_content.assert_called_once_with(
            runtime_checks.OFFLINE_HTML, wait_until="load", timeout=10000,
        )
        self.context.close.assert_called_once()
        self.tk.Tk.return_value.destroy.assert_called_once()
        route = Mock()
        self.context.route.call_args.args[1](route)
        route.abort.assert_called_once()

    def test_missing_bundled_browser_fails_before_launch(self):
        self.executable.unlink()
        self.assertEqual(runtime_checks.run_self_test(self.base), 1)
        self.assertEqual(self.report()["error"], "FileNotFoundError")
        self.assertEqual(self.report()["bundled_browser"], "failed")
        self.engine.chromium.launch_persistent_context.assert_not_called()

    def test_main_runtime_missing_excel_dependency_stops_before_desktop_checks(self):
        with patch.object(runtime_checks, "_check_excel_mapping", side_effect=ImportError("missing Excel dependency")):
            self.assertEqual(runtime_checks.run_self_test(self.base), 1)
        result = self.report()
        self.assertEqual(result["failed_check"], "excel_mapping")
        self.assertEqual(result["excel_mapping"], "failed")
        self.assertEqual(result["ocr_models"], "not_run")
        self.ocr.RapidOCR.assert_not_called()
        self.tk.Tk.assert_not_called()

    def test_existing_browser_outside_package_is_rejected(self):
        host_executable = self.base / "host-chrome.exe"
        host_executable.write_bytes(b"fake host browser")
        self.engine.chromium.executable_path = str(host_executable)
        self.assertEqual(runtime_checks.run_self_test(self.base), 1)
        self.assertEqual(self.report()["error"], "FileNotFoundError")
        self.engine.chromium.launch_persistent_context.assert_not_called()

    def test_browser_failure_closes_context_and_does_not_leak_exception_text(self):
        self.page.set_content.side_effect = RuntimeError("secret-path-or-token")
        self.assertEqual(runtime_checks.run_self_test(self.base), 1)
        result = self.report()
        self.assertEqual(result["error"], "RuntimeError")
        self.assertEqual(result["failed_check"], "bundled_browser")
        self.assertNotIn("secret-path-or-token", json.dumps(result))
        self.context.close.assert_called_once()
        profile = self.engine.chromium.launch_persistent_context.call_args.args[0]
        self.assertFalse(Path(profile).exists())

    def test_tk_failure_destroys_root_and_does_not_start_browser(self):
        self.tk.Tk.return_value.update_idletasks.side_effect = RuntimeError("broken Tcl")
        self.assertEqual(runtime_checks.run_self_test(self.base), 1)
        self.assertEqual(self.report()["tkinter"], "failed")
        self.assertEqual(self.report()["bundled_browser"], "not_run")
        self.tk.Tk.return_value.destroy.assert_called_once()
        self.api.sync_playwright.assert_not_called()

    def test_failed_rerun_replaces_a_previous_success_report(self):
        self.assertEqual(runtime_checks.run_self_test(self.base), 0)
        self.ocr.RapidOCR.side_effect = RuntimeError("bad model")
        self.assertEqual(runtime_checks.run_self_test(self.base), 1)
        self.assertEqual(self.report()["ocr_models"], "failed")
        self.assertEqual(self.report()["bundled_browser"], "not_run")
        self.assertEqual(sorted(path.name for path in (self.base / "data").iterdir()),
                         ["self_test.json"])

    def test_unwritable_report_is_not_success(self):
        with patch.object(runtime_checks, "_write_self_test_report", side_effect=PermissionError):
            self.assertEqual(runtime_checks.run_self_test(self.base), 1)


if __name__ == "__main__":
    unittest.main()

