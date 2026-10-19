# -*- coding: utf-8 -*-
"""Desktop window for screenshot capture and WeChat delivery."""

from __future__ import annotations

import ctypes
from contextlib import nullcontext
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from group_mapping import GroupMappingStore


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
APP_ROOT = report.BASE_DIR.parent
DEFAULT_MAPPING_FILE = "data/微信群配对.xlsx"
logger = logging.getLogger("wechat_report")


def mapping_path(value: str = "") -> Path:
    path = Path(value.strip() or DEFAULT_MAPPING_FILE).expanduser()
    return path if path.is_absolute() else APP_ROOT / path


def mapping_setting(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(APP_ROOT.resolve()))
    except ValueError:
        return str(path.resolve())


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


class DesktopTaskLock:
    """Share the tax runner's desktop lock for standalone WeChat actions."""

    def __enter__(self):
        from ctypes import wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateMutexW(None, False, "Local\\TaxZeroBatchRunner_v1")
        if not self.handle:
            raise RuntimeError("无法创建桌面操作锁。")
        if ctypes.get_last_error() == 183:
            self.kernel.CloseHandle(self.handle)
            raise RuntimeError("另一个申报或微信群配对任务正在使用桌面，请等它完成后再试。")
        return self

    def __exit__(self, *_args):
        self.kernel.CloseHandle(self.handle)


class ReportApp(tk.Tk):
    def __init__(
        self,
        from_geshui: bool = False,
        company_file: Path | None = None,
        result_file: Path | None = None,
        offline_self_test: bool = False,
    ) -> None:
        super().__init__()
        self.from_geshui = from_geshui
        self.company_file = company_file
        self.result_file = result_file
        self.company_name = ""
        self.tax_label = ""
        self.exit_code = 1 if from_geshui else 0
        self._last_sent = ""
        self._delivery_attempted = False
        self._delivery_completed = False
        self._closing = False
        self._close_when_idle = False
        self._job_kind = ""
        self.stop_event = threading.Event()
        self.running = False
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.ui_queue: queue.Queue = queue.Queue()
        self._install_log_handler()
        self.title("微信群与 Excel 配对 · 自动回执" if from_geshui else "微信群与 Excel 配对")
        self.geometry("900x820")
        self.minsize(800, 720)
        self.configure(bg="#f4f5f7")

        config = {} if offline_self_test else load_config()
        self.mapping_var = tk.StringVar(value=str(mapping_path(str(config.get("mapping_file", "")))))
        self.company_var = tk.StringVar(value="" if from_geshui else str(config.get("company_name", "")))
        self.target_var = tk.StringVar(value="")
        self.new_group_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="选择公司后，按 Excel 中的微信群完整名称搜索。")
        self._busy_widgets: list = []
        self._build()
        saved_message = str(config.get("message", "")).strip()
        if saved_message:
            self.message_box.insert("1.0", saved_message)
        self.company_var.trace_add("write", self._company_changed)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if not offline_self_test:
            self.after(120, self._drain_logs)
            self.after(200, self.lift)
            self._refresh_mapping(show_error=False)
            if from_geshui:
                self.after(300, self._start_from_geshui)

    def _install_log_handler(self) -> None:
        report.setup_logging()
        handler = QueueLogHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        logger.addHandler(handler)
        self._log_handler = handler

    def _button(self, parent, text: str, command, **kwargs):
        button = ttk.Button(parent, text=text, command=command, **kwargs)
        self._busy_widgets.append(button)
        return button

    def _build(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Hint.TLabel", font=("Microsoft YaHei UI", 9), foreground="#596273")
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(8, 5))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(10, 6))
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        ttk.Label(outer, text="微信群与 Excel 配对", style="Title.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        ttk.Label(
            outer,
            text="公司名称只用于查表。搜索时复制 Excel 中的完整群名；未配对、未找到或同名多个群时停止。",
            style="Hint.TLabel", wraplength=840,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 10))

        ttk.Label(outer, text="Excel 配对表").grid(row=2, column=0, sticky="w")
        ttk.Entry(outer, textvariable=self.mapping_var, state="readonly").grid(
            row=2, column=1, columnspan=2, sticky="ew", pady=4
        )
        files = ttk.Frame(outer)
        files.grid(row=3, column=1, columnspan=2, sticky="w", pady=(0, 8))
        for text, command in (
            ("选择 Excel", self._choose_mapping), ("新建 Excel", self._create_mapping),
            ("打开表格", self._open_mapping), ("刷新配对", self._refresh_clicked),
        ):
            self._button(files, text, command).pack(side="left", padx=(0, 6))

        ttk.Label(outer, text="公司名称").grid(row=4, column=0, sticky="w")
        self.company_box = ttk.Combobox(outer, textvariable=self.company_var,
                                       state="readonly" if self.from_geshui else "normal")
        self.company_box.grid(row=4, column=1, columnspan=2, sticky="ew", pady=4)
        ttk.Label(outer, text="已配对群全名").grid(row=5, column=0, sticky="w")
        self.target_entry = ttk.Entry(outer, textvariable=self.target_var, state="readonly")
        self.target_entry.grid(row=5, column=1, sticky="ew", pady=4)
        self.search_button = self._button(outer, "仅搜索此群", self._search_clicked)
        self.search_button.grid(row=5, column=2, padx=(8, 0))

        pairing = ttk.LabelFrame(outer, text="登记或更新公司与微信群配对", padding=10)
        pairing.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(10, 8))
        pairing.columnconfigure(1, weight=1)
        ttk.Label(pairing, text="微信群完整名称").grid(row=0, column=0, sticky="w")
        self.new_group_entry = ttk.Entry(pairing, textvariable=self.new_group_var)
        self.new_group_entry.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(8, 0))
        ttk.Label(
            pairing, text="先填公司和群名，再点等待，然后切到微信自行建群并命名。确认群名后自动写入 Excel。邀请成员在微信中完成。",
            style="Hint.TLabel", wraplength=800,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=6)
        pair_actions = ttk.Frame(pairing)
        pair_actions.grid(row=2, column=0, columnspan=3, sticky="w")
        self.wait_group_button = self._button(pair_actions, "等待微信群出现并登记", self._wait_group_clicked)
        self.wait_group_button.pack(side="left")
        self.pair_existing_button = self._button(pair_actions, "验证已有群并保存配对", self._pair_existing_clicked)
        self.pair_existing_button.pack(side="left", padx=8)
        self.cancel_button = ttk.Button(pair_actions, text="取消等待", command=self._cancel_wait, state="disabled")
        self.cancel_button.pack(side="left")

        ttk.Label(outer, text="回执说明").grid(row=7, column=0, sticky="nw", pady=4)
        self.message_box = scrolledtext.ScrolledText(outer, height=3, font=("Microsoft YaHei UI", 10),
                                                    wrap="word", relief="solid", borderwidth=1)
        self.message_box.grid(row=7, column=1, columnspan=2, sticky="ew", pady=4)
        actions = ttk.Frame(outer)
        actions.grid(row=8, column=0, columnspan=3, sticky="ew", pady=8)
        self.save_button = self._button(actions, "保存设置", self._save_clicked)
        self.check_button = self._button(actions, "检查微信", self._check_clicked)
        self.send_button = self._button(actions, "截图并发送到已配对群", self._send_clicked, style="Primary.TButton")
        self.save_button.pack(side="left")
        self.check_button.pack(side="left", padx=8)
        self.send_button.pack(side="left")
        ttk.Label(outer, text="运行日志").grid(row=9, column=0, columnspan=3, sticky="w", pady=(4, 4))
        self.log_box = scrolledtext.ScrolledText(outer, height=8, font=("Consolas", 10), wrap="word",
                                               state="disabled", relief="solid", borderwidth=1)
        self.log_box.grid(row=10, column=0, columnspan=3, sticky="nsew")
        outer.rowconfigure(10, weight=1)
        ttk.Label(outer, textvariable=self.status_var, style="Hint.TLabel", wraplength=840).grid(
            row=11, column=0, columnspan=3, sticky="w", pady=(8, 0)
        )

    def _append_log(self, line: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _drain_logs(self) -> None:
        if self._closing:
            return
        while True:
            try:
                self.ui_queue.get_nowait()()
            except queue.Empty:
                break
        if self._closing:
            return
        while True:
            try:
                self._append_log(self.log_queue.get_nowait())
            except queue.Empty:
                break
        self.after(120, self._drain_logs)

    def _settings(self) -> dict:
        config = load_config()
        config.pop("target_group", None)
        config.pop("fallback_group", None)
        config.update({
            "mapping_file": mapping_setting(mapping_path(self.mapping_var.get())),
            "company_name": self.company_var.get().strip(),
            "message": self.message_box.get("1.0", "end").strip(),
        })
        return config

    def _form(self) -> dict:
        # Always reread the workbook; a stale display or old target_group must
        # never become a sending destination.
        company = self.company_name if self.from_geshui else self.company_var.get().strip()
        if not company:
            raise ValueError("请先选择或输入公司名称。")
        path = mapping_path(self.mapping_var.get())
        group_name = GroupMappingStore(path).resolve(company)
        self.target_var.set(group_name)
        return {
            "company_name": company, "mapping_file": str(path),
            "target_group": group_name, "fallback_group": "",
            "message": self.message_box.get("1.0", "end").strip(),
        }

    def _save_clicked(self) -> None:
        if self.running:
            return
        try:
            save_config(self._settings())
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)
            return
        self.status_var.set("设置已保存。")
        self._append_log("设置已保存。")

    def _company_changed(self, *_args) -> None:
        if not self.running:
            self._show_pairing()

    def _show_pairing(self) -> None:
        self.target_var.set("")
        company = self.company_name if self.from_geshui else self.company_var.get().strip()
        if not company:
            return
        try:
            group_name = GroupMappingStore(mapping_path(self.mapping_var.get())).resolve(company)
            self.target_var.set(group_name)
            self.status_var.set(f"Excel 配对：{company} → {group_name}")
        except Exception as exc:
            self.status_var.set(f"尚不能发送：{exc}")

    def _refresh_mapping(self, show_error: bool = True) -> bool:
        try:
            entries = GroupMappingStore(mapping_path(self.mapping_var.get())).list_entries()
            companies = [entry.company_name for entry in entries]
            if self.from_geshui:
                companies = [self.company_name] if self.company_name else []
            self.company_box.configure(values=companies)
            self._show_pairing()
            return True
        except Exception as exc:
            self.target_var.set("")
            self.company_box.configure(values=[self.company_name] if self.company_name else [])
            self.status_var.set(f"请先选择或新建 Excel 配对表：{exc}")
            if show_error:
                messagebox.showerror("Excel 配对表", str(exc), parent=self)
            return False

    def _refresh_clicked(self) -> None:
        if not self.running and self._refresh_mapping():
            self._append_log("已重新读取 Excel 配对表。")

    def _select_mapping_path(self, path: Path) -> None:
        self.mapping_var.set(str(path))
        self.target_var.set("")
        save_config(self._settings())
        self._refresh_mapping()

    def _choose_mapping(self) -> None:
        if self.running:
            return
        value = filedialog.askopenfilename(parent=self, title="选择公司与微信群配对表",
                                          filetypes=[("Excel 工作簿", "*.xlsx")])
        if value:
            try:
                self._select_mapping_path(Path(value))
            except Exception as exc:
                messagebox.showerror("选择失败", str(exc), parent=self)

    def _create_mapping(self) -> None:
        if self.running:
            return
        default = mapping_path(self.mapping_var.get())
        value = filedialog.asksaveasfilename(parent=self, title="新建公司与微信群配对表",
                                            initialdir=str(default.parent), initialfile=default.name,
                                            defaultextension=".xlsx", filetypes=[("Excel 工作簿", "*.xlsx")])
        if value:
            try:
                path = GroupMappingStore(Path(value)).create()
                self._select_mapping_path(path)
                self.status_var.set("配对表已创建。可以验证已有群，或等待新群出现后登记。")
            except Exception as exc:
                messagebox.showerror("新建失败", str(exc), parent=self)

    def _open_mapping(self) -> None:
        if self.running:
            return
        path = mapping_path(self.mapping_var.get())
        if not path.is_file():
            messagebox.showwarning("Excel 配对表", "文件不存在，请先新建或选择配对表。", parent=self)
            return
        try:
            if sys.platform == "win32":
                os.startfile(str(path))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            messagebox.showerror("打开失败", str(exc), parent=self)

    def _set_busy(self, busy: bool) -> None:
        for button in self._busy_widgets:
            button.configure(state="disabled" if busy else "normal")
        self.company_box.configure(state="disabled" if busy else ("readonly" if self.from_geshui else "normal"))
        self.new_group_entry.configure(state="disabled" if busy else "normal")
        self.message_box.configure(state="disabled" if busy else "normal")
        self.cancel_button.configure(state="normal" if busy and self._job_kind == "wait" else "disabled")
        if self.from_geshui and self._delivery_attempted:
            self.send_button.configure(state="disabled")

    def _launch_job(self, kind: str, status: str, work, data: dict, hide: bool = False) -> bool:
        if self.running or self._closing or self._delivery_completed:
            return False
        self.running = True
        self._job_kind = kind
        self._last_sent = ""
        self.stop_event.clear()
        self._set_busy(True)
        self.status_var.set(status)
        self._append_log(status)
        self.attributes("-topmost", False)
        if hide:
            self.withdraw()
            self.update_idletasks()
        self._start_worker(work, dict(data))
        return True

    def _check_clicked(self) -> None:
        self._launch_job("check", "正在检查微信窗口。", self._check_wechat, {})

    def _check_wechat(self, _data: dict) -> None:
        message = report.probe_wechat()
        ready = "可以发送" in message or "可以被识别" in message
        self._finish(ready, message, offer_restart=not ready and "没有找到已打开的微信" not in message)

    def _start_from_geshui(self) -> None:
        try:
            company, tax_label = load_company_file(self.company_file)
            if self.result_file is None:
                raise RuntimeError("没有收到回写结果的位置。")
            self.company_name = company
            self.tax_label = tax_label
            self.company_var.set(company)
            self._refresh_mapping(show_error=False)
            data = self._form()
        except Exception as exc:
            self.exit_code = 1
            self.target_var.set("")
            message = f"自动转发已暂停：{exc}\n请在此窗口完成 Excel 配对，再点击“截图并发送到已配对群”。"
            self.status_var.set(message)
            self._append_log(message)
            return
        self._append_log(f"Excel 配对：{company} → {data['target_group']}")
        self.status_var.set(f"将发送到已配对群：{data['target_group']}")
        self.after(800, self._send_clicked)

    def _read_form(self) -> dict | None:
        try:
            return self._form()
        except Exception as exc:
            self.target_var.set("")
            self.status_var.set(f"尚不能发送：{exc}")
            messagebox.showwarning("请先完成 Excel 配对", str(exc), parent=self)
            return None

    def _search_clicked(self) -> None:
        if self.running:
            return
        data = self._read_form()
        if data:
            self._launch_job("search", f"正在按 Excel 完整群名搜索：{data['target_group']}（不发送）。",
                             self._search_only, data, hide=True)

    def _search_only(self, data: dict) -> None:
        found = report.search_group(data["target_group"])
        if found != data["target_group"]:
            raise RuntimeError("没有找到 Excel 配对的完整群名；没有发送。请核对群名或重复同名群。")
        self._finish(True, f"已找到群：{found}。未发送消息。")

    def _pairing_form(self) -> dict:
        company = self.company_name if self.from_geshui else self.company_var.get().strip()
        group_name = self.new_group_var.get().strip()
        if not company or not group_name:
            raise ValueError("请填写公司名称和微信群完整名称。")
        path = mapping_path(self.mapping_var.get())
        store = GroupMappingStore(path)
        entries = store.list_entries()
        existing = next((entry for entry in entries if entry.company_name == company), None)
        if existing and existing.group_name != group_name:
            if not messagebox.askyesno("更新配对", f"将“{company}”的群名从“{existing.group_name}”改为“{group_name}”？", parent=self):
                return {}
        return {"company_name": company, "group_name": group_name, "mapping_file": str(path),
                "expected_group": existing.group_name if existing else None}

    def _begin_pairing(self, wait: bool) -> None:
        if self.running:
            return
        try:
            data = self._pairing_form()
            if not data:
                return
            save_config(self._settings())
        except Exception as exc:
            messagebox.showwarning("无法登记配对", str(exc), parent=self)
            return
        if wait:
            status = "请切到微信自行建群并设置完整群名；检测到该群后自动验证并登记 Excel。最长等待 10 分钟。"
            self._launch_job("wait", status, self._pair_group_worker, dict(data, wait=True))
        else:
            self._launch_job("pair", "正在验证完整群名，验证成功后写入 Excel（不发送）。",
                             self._pair_group_worker, dict(data, wait=False), hide=True)

    def _wait_group_clicked(self) -> None:
        self._begin_pairing(wait=True)

    def _pair_existing_clicked(self) -> None:
        self._begin_pairing(wait=False)

    def _pair_group_worker(self, data: dict) -> None:
        if data["wait"]:
            found = report.wait_for_group(data["group_name"], self.stop_event, on_status=self.log_queue.put)
        else:
            found = report.search_group(data["group_name"])
        if self.stop_event.is_set():
            self._finish(False, "已取消等待，未写入 Excel。", cancelled=True)
            return
        if found != data["group_name"]:
            raise RuntimeError("没有找到唯一且完整同名的微信群。Excel 未新增配对。")
        # Final cancellation and workbook commit are serialized on Tk's event
        # thread. A cancel cannot be acknowledged halfway through saving.
        self.ui_queue.put(lambda: self._commit_pairing(data, found))

    def _commit_pairing(self, data: dict, found: str) -> None:
        if self.stop_event.is_set() or self._closing:
            self._finish(False, "已取消等待，未写入 Excel。", cancelled=True)
            return
        self.cancel_button.configure(state="disabled")
        self.status_var.set("微信群已验证，正在保存 Excel 配对。")
        try:
            GroupMappingStore(Path(data["mapping_file"])).upsert(
                data["company_name"], found, expected_group=data["expected_group"],
            )
        except Exception as exc:
            self._finish(False, f"群名已验证，但 Excel 未保存：{exc}")
            return
        self._finish(True, f"已验证并保存 Excel 配对：{data['company_name']} → {found}", refresh=True)

    def _cancel_wait(self) -> None:
        if self.running and self._job_kind == "wait":
            self.stop_event.set()
            self.cancel_button.configure(state="disabled")
            self.status_var.set("正在取消等待。")

    def _send_clicked(self) -> None:
        if self.running:
            return
        if self.from_geshui and self._delivery_attempted:
            messagebox.showwarning("请核对发送记录", "本次回执已尝试发送，请核对微信和发送记录，避免重复发送。", parent=self)
            return
        data = self._read_form()
        if data is None:
            return
        try:
            save_config(self._settings())
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)
            return
        self._launch_job("send", f"正在截图并发送到已配对群：{data['target_group']}。请不要操作鼠标和键盘。",
                         self._capture_and_send, data, hide=True)

    def _start_worker(self, work, data: dict) -> None:
        threading.Thread(target=self._run_worker, args=(work, data), daemon=True).start()

    def _run_worker(self, work, data: dict) -> None:
        initialized = False
        try:
            # The automatic helper is a child of the already-locked tax
            # runner; taking the same mutex there would reject its own parent.
            with nullcontext() if self.from_geshui else DesktopTaskLock():
                ctypes.windll.ole32.CoInitialize(None)
                initialized = True
                work(data)
        except report.GroupWaitCancelled:
            self._finish(False, "已取消等待，未写入 Excel。", cancelled=True)
        except report.WeChatClientError as exc:
            logger.error("%s", exc)
            self._finish(False, str(exc), offer_restart=exc.can_restart)
        except Exception as exc:
            logger.exception("执行失败")
            self._finish(False, str(exc))
        finally:
            if initialized:
                ctypes.windll.ole32.CoUninitialize()

    def _capture_and_send(self, data: dict) -> None:
        # Resolve once more immediately before sending, and reject a mapping
        # changed while the user was launching the action.
        current = GroupMappingStore(Path(data["mapping_file"])).resolve(data["company_name"])
        if current != data["target_group"]:
            raise RuntimeError("Excel 配对已发生变化，请刷新并核对后重试。没有发送。")
        sent_to = report.execute_report(current, "", data["message"], before_send=self._mark_delivery_attempted)
        if sent_to != current:
            raise RuntimeError("微信回执目标与 Excel 配对不一致，请核对发送结果。")
        self._last_sent = sent_to
        self._finish(True, f"已发送到：{sent_to}")

    def _mark_delivery_attempted(self) -> None:
        self._delivery_attempted = True

    def _finish(self, ok: bool, message: str, offer_restart: bool = False,
                cancelled: bool = False, refresh: bool = False) -> None:
        # Workers only enqueue Python callbacks. Tk is touched by _drain_logs
        # on the main thread, including logging, completion and error dialogs.
        job_kind = self._job_kind
        sent_to = self._last_sent if job_kind == "send" else ""

        def apply() -> None:
            if self._closing:
                return
            self.deiconify()
            self.lift()
            self.running = False
            self._job_kind = ""
            self._set_busy(False)
            if refresh:
                self._refresh_mapping(show_error=False)
            self.status_var.set(" ".join(message.split()))
            self._append_log(message)
            if self._close_when_idle:
                self._close_after_send()
                return
            if self.from_geshui and ok and job_kind == "send" and sent_to and self.result_file is not None:
                try:
                    write_forward_result(self.result_file, self.company_name, self.tax_label, sent_to)
                except OSError as exc:
                    self.exit_code = 1
                    self.send_button.configure(state="disabled")
                    messagebox.showerror("回执保存失败", f"消息已发送，但发送记录保存失败：{exc}\n请人工核对，不要重复发送。", parent=self)
                    return
                self.exit_code = 0
                self._delivery_completed = True
                self._set_busy(True)
                self.after(1200, self._close_after_send)
                return
            if self.from_geshui and not ok:
                self.exit_code = 1
            if cancelled:
                return
            if ok:
                if job_kind != "check":
                    messagebox.showinfo("完成", message, parent=self)
                return
            if offer_restart:
                if messagebox.askyesno("微信界面无法识别", message + "\n\n是否现在退出并重新打开微信？", parent=self):
                    self._launch_job("restart", "正在重新打开微信，请稍候。", self._restart_wechat, {})
                return
            messagebox.showerror("执行未完成", message, parent=self)

        self.ui_queue.put(apply)

    def _restart_wechat(self, _data: dict) -> None:
        message = report.restart_wechat()
        self._finish("可以识别" in message, message)

    def _close_after_send(self) -> None:
        self._closing = True
        logger.removeHandler(self._log_handler)
        self.destroy()

    def _on_close(self) -> None:
        if self._closing:
            return
        if self.running:
            if self._job_kind == "wait":
                self._close_when_idle = True
                self._cancel_wait()
                return
            self.deiconify()
            messagebox.showwarning("请稍候", "正在执行，请等待结束再关闭。", parent=self)
            return
        try:
            save_config(self._settings())
        except OSError:
            pass
        self._close_after_send()


def main() -> None:
    from_geshui, company_file, result_file = parse_launch_args(sys.argv)
    enable_dpi()
    app = ReportApp(from_geshui, company_file, result_file)
    app.mainloop()
    if from_geshui:
        raise SystemExit(app.exit_code)


if __name__ == "__main__":
    main()

