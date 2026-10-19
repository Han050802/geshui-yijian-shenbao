# -*- coding: utf-8 -*-
"""Desktop window for screenshot capture and WeChat delivery."""

from __future__ import annotations

import ctypes
import json
import logging
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk


def enable_dpi() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


try:
    import wechat_report as report
except Exception as exc:
    enable_dpi()
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(
        "无法启动",
        f"{exc}\n\n请先双击「微信截图回执.bat」安装依赖。",
    )
    raise SystemExit(1)


CONFIG_PATH = report.BASE_DIR / "config.json"
logger = logging.getLogger("wechat_report")


def load_config() -> dict:
    if not CONFIG_PATH.is_file():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(data: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_launch_args(argv: list[str]) -> tuple[bool, Path | None, Path | None]:
    """个税零申报传入 --from-geshui 时，仍打开本程序窗口，不走隐藏进程。"""
    company_file = None
    result_file = None
    for index, item in enumerate(argv):
        if item == "--company-file" and index + 1 < len(argv):
            company_file = Path(argv[index + 1])
        elif item == "--result-file" and index + 1 < len(argv):
            result_file = Path(argv[index + 1])
    return "--from-geshui" in argv, company_file, result_file


def load_company_file(path: Path | None) -> tuple[str, str]:
    if path is None or not path.is_file():
        raise RuntimeError("没有找到个税零申报正在办理的公司。")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("个税零申报的当前公司记录无法读取。") from exc
    if not isinstance(data, dict):
        raise RuntimeError("个税零申报的当前公司记录无法读取。")
    company = str(data.get("company") or "").strip()
    tax_label = str(data.get("taxLabel") or "").strip()
    if not company:
        raise RuntimeError("个税零申报当前公司名称为空。")
    return company, tax_label


def write_forward_result(path: Path, company: str, tax_label: str, sent_to: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "company": company,
        "taxLabel": tax_label,
        "sentTo": sent_to,
        "finishedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class QueueLogHandler(logging.Handler):
    def __init__(self, log_queue: queue.Queue[str]) -> None:
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.log_queue.put(self.format(record))
        except Exception:
            pass


class ReportApp(tk.Tk):
    def __init__(
        self,
        from_geshui: bool = False,
        company_file: Path | None = None,
        result_file: Path | None = None,
    ) -> None:
        super().__init__()
        self.from_geshui = from_geshui
        self.company_file = company_file
        self.result_file = result_file
        self.company_name = ""
        self.tax_label = ""
        self.exit_code = 1 if from_geshui else 0
        self._last_sent = ""
        self._closing = False
        self.title("微信自动转发" if from_geshui else "微信截图回执")
        self.geometry("760x640")
        self.minsize(680, 560)
        self.configure(bg="#f4f5f7")

        self.running = False
        self.log_queue: queue.Queue[str] = queue.Queue()
        self._install_log_handler()

        config = load_config()
        self.target_var = tk.StringVar(value=str(config.get("target_group", "")))
        self.fallback_var = tk.StringVar(value=str(config.get("fallback_group", "")))
        self.status_var = tk.StringVar(value="就绪。请先登录微信电脑版。")

        self._build()
        saved_message = str(config.get("message", "")).strip()
        if saved_message:
            self.message_box.insert("1.0", saved_message)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(120, self._drain_logs)
        self.after(200, self.lift)
        if from_geshui:
            self.attributes("-topmost", True)
            self.after(300, self._start_from_geshui)
        else:
            self.after(600, self._probe_quietly)

    def _install_log_handler(self) -> None:
        report.setup_logging()
        handler = QueueLogHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        logger.addHandler(handler)

    def _build(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Hint.TLabel", font=("Microsoft YaHei UI", 9), foreground="#596273")
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(12, 6))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(14, 7))

        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)

        ttk.Label(outer, text="微信截图回执", style="Title.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        ttk.Label(
            outer,
            text="目标群可填完整群名或关键词，完整同名优先；关键词只匹配唯一群聊。兜底填写完整名称。找不到或匹配多个群时改发兜底。只截取扣缴端窗口，发送后删除截图。",
            style="Hint.TLabel",
            wraplength=700,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 14))

        ttk.Label(outer, text="目标群/关键词").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(outer, textvariable=self.target_var).grid(
            row=2, column=1, columnspan=2, sticky="ew", pady=4
        )
        ttk.Label(outer, text="兜底会话全名").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(outer, textvariable=self.fallback_var).grid(
            row=3, column=1, columnspan=2, sticky="ew", pady=4
        )

        ttk.Label(outer, text="说明").grid(row=4, column=0, sticky="nw", pady=4)
        self.message_box = scrolledtext.ScrolledText(
            outer,
            height=5,
            font=("Microsoft YaHei UI", 10),
            wrap="word",
            relief="solid",
            borderwidth=1,
        )
        self.message_box.grid(row=4, column=1, columnspan=2, sticky="ew", pady=4)

        actions = ttk.Frame(outer)
        actions.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(12, 8))
        self.save_button = ttk.Button(actions, text="保存设置", command=self._save_clicked)
        self.check_button = ttk.Button(actions, text="检查微信", command=self._check_clicked)
        self.send_button = ttk.Button(
            actions, text="截图并发送到微信", style="Primary.TButton", command=self._send_clicked
        )
        self.save_button.pack(side="left")
        self.check_button.pack(side="left", padx=8)
        self.send_button.pack(side="left")

        ttk.Label(outer, text="运行日志").grid(row=6, column=0, columnspan=3, sticky="w", pady=(8, 4))
        self.log_box = scrolledtext.ScrolledText(
            outer,
            height=12,
            font=("Consolas", 10),
            wrap="word",
            state="disabled",
            relief="solid",
            borderwidth=1,
        )
        self.log_box.grid(row=7, column=0, columnspan=3, sticky="nsew")
        outer.rowconfigure(7, weight=1)

        ttk.Label(outer, textvariable=self.status_var, style="Hint.TLabel").grid(
            row=8, column=0, columnspan=3, sticky="w", pady=(8, 0)
        )

    def _append_log(self, line: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _drain_logs(self) -> None:
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self._append_log(line)
        self.after(120, self._drain_logs)

    def _form(self) -> dict:
        return {
            "target_group": self.target_var.get().strip(),
            "fallback_group": self.fallback_var.get().strip(),
            "message": self.message_box.get("1.0", "end").strip(),
        }

    def _save_clicked(self) -> None:
        try:
            save_config(self._form())
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)
            return
        self.status_var.set("设置已保存。")
        self._append_log("设置已保存。")

    def _validate(self, data: dict) -> bool:
        try:
            report.ensure_group_names(data["target_group"], data["fallback_group"])
        except ValueError as exc:
            messagebox.showwarning("请检查群名", str(exc), parent=self)
            return False
        return True

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for button in (self.save_button, self.check_button, self.send_button):
            button.configure(state=state)

    def _probe_quietly(self) -> None:
        if self.running:
            return
        threading.Thread(target=self._quiet_probe_worker, daemon=True).start()

    def _quiet_probe_worker(self) -> None:
        ctypes.windll.ole32.CoInitialize(None)
        try:
            message = report.probe_wechat()
        except Exception as exc:
            message = f"检查微信失败：{exc}"
        finally:
            ctypes.windll.ole32.CoUninitialize()

        def apply() -> None:
            if self.running:
                return
            self.status_var.set(" ".join(message.split()))
            self._append_log(message)
            if "没有暴露给系统" in message:
                messagebox.showwarning("微信界面无法识别", message, parent=self)

        self.after(0, apply)

    def _check_clicked(self) -> None:
        if self.running:
            return
        self.running = True
        self._set_busy(True)
        self.status_var.set("正在检查微信窗口。")
        self._start_worker(self._check_wechat, {})

    def _check_wechat(self, _data: dict) -> None:
        message = report.probe_wechat()
        ready = "可以发送" in message or "可以被识别" in message
        self._finish(ready, message, offer_restart=not ready and "没有找到已打开的微信" not in message)

    def _start_from_geshui(self) -> None:
        try:
            company, tax_label = load_company_file(self.company_file)
        except Exception as exc:
            self.exit_code = 1
            self.status_var.set(str(exc))
            self._append_log(str(exc))
            messagebox.showerror("微信自动转发", str(exc), parent=self)
            return
        if self.result_file is None:
            self.exit_code = 1
            messagebox.showerror("微信自动转发", "没有收到回写结果的位置。", parent=self)
            return
        self.company_name = company
        self.tax_label = tax_label
        self.target_var.set(company)
        self._append_log(f"已把当前公司名称粘贴到目标群：{company}")
        self.status_var.set(f"目标群：{company}")
        self.lift()
        self.attributes("-topmost", True)
        self.update_idletasks()
        self.after(800, self._send_clicked)

    def _send_clicked(self) -> None:
        data = self._form()
        if not self._validate(data):
            return
        if not self.from_geshui:
            save_config(data)
        self._begin("正在截图并发送。请不要操作鼠标和键盘。")
        delay = 1000 if self.from_geshui else 350
        self.after(delay, lambda: self._start_worker(self._capture_and_send, data))

    def _begin(self, status: str) -> None:
        self.running = True
        self._set_busy(True)
        self.status_var.set(status)
        self._append_log(status)
        self.lift()
        self.attributes("-topmost", True)
        self.update_idletasks()
        if self.from_geshui:
            self.after(700, self._hide_for_capture)
        else:
            self.withdraw()
            self.update_idletasks()

    def _hide_for_capture(self) -> None:
        try:
            self.withdraw()
            self.update_idletasks()
        except tk.TclError:
            return

    def _start_worker(self, work, data: dict) -> None:
        threading.Thread(target=self._run_worker, args=(work, data), daemon=True).start()

    def _run_worker(self, work, data: dict) -> None:
        ctypes.windll.ole32.CoInitialize(None)
        try:
            work(data)
        except report.WeChatClientError as exc:
            logger.error("%s", exc)
            self._finish(False, str(exc), offer_restart=exc.can_restart)
        except Exception as exc:
            logger.exception("执行失败")
            self._finish(False, str(exc))
        finally:
            ctypes.windll.ole32.CoUninitialize()

    def _capture_and_send(self, data: dict) -> None:
        sent_to = report.execute_report(
            data["target_group"],
            data["fallback_group"],
            data["message"],
        )
        self._last_sent = sent_to
        prefix = "已降级发送到" if sent_to == data["fallback_group"] else "已发送到"
        self._finish(True, f"{prefix}：{sent_to}")

    def _finish(
        self,
        ok: bool,
        message: str,
        offer_restart: bool = False,
    ) -> None:
        def apply() -> None:
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
            self.running = False
            self._set_busy(False)
            self.status_var.set(" ".join(message.split()))
            self._append_log(message)
            if self.from_geshui and ok and self._last_sent and self.result_file is not None:
                try:
                    write_forward_result(
                        self.result_file,
                        self.company_name,
                        self.tax_label,
                        self._last_sent,
                    )
                except OSError as exc:
                    self.exit_code = 1
                    messagebox.showerror("微信自动转发", str(exc), parent=self)
                    return
                self.exit_code = 0
                self.after(1200, self._close_after_send)
                return
            if self.from_geshui and not ok:
                self.exit_code = 1
            if ok:
                if "可以发送" not in message and "可以被识别" not in message:
                    messagebox.showinfo("完成", message, parent=self)
                return
            if offer_restart:
                if messagebox.askyesno(
                    "微信界面无法识别",
                    message + "\n\n是否现在退出并重新打开微信？",
                    parent=self,
                ):
                    self._begin("正在重新打开微信，请稍候。")
                    self.after(200, lambda: self._start_worker(self._restart_wechat, {}))
                return
            messagebox.showerror("发送失败", message, parent=self)

        self.after(0, apply)

    def _restart_wechat(self, _data: dict) -> None:
        message = report.restart_wechat()
        ready = "可以识别" in message
        self._finish(ready, message)

    def _close_after_send(self) -> None:
        self._closing = True
        self.destroy()

    def _on_close(self) -> None:
        if self._closing:
            return
        if self.running:
            self.deiconify()
            messagebox.showwarning("请稍候", "正在执行，请等待结束再关闭。", parent=self)
            return
        if not self.from_geshui:
            try:
                save_config(self._form())
            except OSError:
                pass
        self.destroy()


def main() -> None:
    from_geshui, company_file, result_file = parse_launch_args(sys.argv)
    enable_dpi()
    app = ReportApp(from_geshui, company_file, result_file)
    app.mainloop()
    if from_geshui:
        raise SystemExit(app.exit_code)


if __name__ == "__main__":
    main()
