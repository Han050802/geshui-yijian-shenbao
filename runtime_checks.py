"""Offline release checks, isolated from tax filing and WeChat entry points."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


CHECK_NAMES = ("imports", "ocr_models", "tkinter", "bundled_browser")
OFFLINE_TITLE = "TaxZero offline self-test"
OFFLINE_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    f"<title>{OFFLINE_TITLE}</title></head>"
    "<body><p id='runtime-check'>ok</p></body></html>"
)


def configure_browser_environment() -> bool:
    """Frozen apps must use their bundled browser, never a host browser cache."""
    frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
    return frozen


def _check_bundled_browser(sync_playwright, playwright_package) -> None:
    """Start a temporary, offline browser and render only inline test content."""
    bundled_root = (
        Path(playwright_package.__file__).resolve().parent
        / "driver" / "package" / ".local-browsers"
    )
    with sync_playwright() as engine:
        executable = Path(engine.chromium.executable_path).resolve()
        if not executable.is_relative_to(bundled_root) or not executable.is_file():
            raise FileNotFoundError("Bundled Chromium is missing")

        # This profile is always fresh and is removed after the browser exits.
        # It is unrelated to BASE/data/browser_profile or the user's Chrome.
        with tempfile.TemporaryDirectory(prefix="taxzero-self-test-") as profile:
            context = engine.chromium.launch_persistent_context(
                profile,
                executable_path=str(executable),
                headless=True,
                offline=True,
                service_workers="block",
                accept_downloads=False,
                timeout=30000,
            )
            try:
                context.route("**/*", lambda route: route.abort())
                page = context.new_page()
                page.set_content(OFFLINE_HTML, wait_until="load", timeout=10000)
                if page.url != "about:blank" or page.title() != OFFLINE_TITLE:
                    raise RuntimeError("Offline browser page check failed")
                if page.locator("#runtime-check").inner_text(timeout=5000) != "ok":
                    raise RuntimeError("Offline browser rendering check failed")
            finally:
                context.close()


def _write_self_test_report(base: Path, result: dict) -> None:
    """Replace only the self-test report; never open settings or filing state."""
    directory = base / "data"
    directory.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory,
            prefix="self_test-", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, directory / "self_test.json")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run_self_test(base: str | Path) -> int:
    """Return 0 only when the packaged dependencies, GUI and browser all work.

    No business settings, tax records, existing browser profile or WeChat
    modules are loaded. Exceptions are reduced to their type in the report.
    """
    result = {name: "not_run" for name in CHECK_NAMES}
    current = "imports"
    exit_code = 1
    # Also require a hermetic browser when invoked from the source tree.
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
    try:
        import pywinauto  # noqa: F401 -- verify packaged Windows dependencies
        import rapidocr_onnxruntime
        import tkinter as tk
        import playwright
        from playwright.sync_api import sync_playwright

        result["imports"] = "ok"
        current = "ocr_models"
        engine = rapidocr_onnxruntime.RapidOCR(
            intra_op_num_threads=2, inter_op_num_threads=2,
        )
        del engine
        result["ocr_models"] = "ok"

        current = "tkinter"
        root = tk.Tk()
        try:
            root.withdraw()
            root.update_idletasks()
        finally:
            root.destroy()
        result["tkinter"] = "ok"

        current = "bundled_browser"
        _check_bundled_browser(sync_playwright, playwright)
        result["bundled_browser"] = "ok"
        exit_code = 0
    except Exception as exc:
        result[current] = "failed"
        result["error"] = type(exc).__name__
        result["failed_check"] = current

    try:
        _write_self_test_report(Path(base), result)
    except OSError:
        # A missing/unwritable report cannot be reported as a successful check.
        return 1
    return exit_code
