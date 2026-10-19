"""Small Windows launcher; usable directly or from Power Automate Desktop."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import Runner, StateStore, ValidationError, _atomic_write, write_results


def base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent.parent
    return Path(__file__).resolve().parent


BASE = base_dir()
TEST_COMPANY_NAME = "北京万瑞铭诚商贸有限公司"


def only_test_company(companies, name=TEST_COMPANY_NAME):
    """The test button keeps one named company. Other tasks keep the full list."""
    matched = [company for company in companies if company.name.strip() == name]
    if len(matched) != 1:
        raise ValidationError(f"待办名单里没有唯一找到：{name}，测试未运行其他公司")
    return matched


def defaults():
    now = datetime.now(timezone(timedelta(hours=8)))
    previous = now.replace(day=1)-timedelta(days=1)
    return {"url": "http://192.168.110.15:8765/", "work_month": now.strftime("%Y-%m"),
            "tax_month": previous.strftime("%Y-%m"), "expected_people": 1,
            "expected_people_by_tax_id": {}, "expected_people_by_name": {}, "generate_zero": False,
            "allow_missing_insurance": False, "writeback": True,
            "verify_url": "", "feedback_timeout": 180,
            "selectors": {}, "profile_note": "视频预设，需现场联调"}


def load_settings():
    result = defaults()
    path = BASE / "settings.json"
    if path.exists():
        result.update(json.loads(path.read_text(encoding="utf-8-sig")))
    return result


def save_settings(settings):
    from core import _atomic_write
    _atomic_write(BASE / "settings.json", json.dumps(settings, ensure_ascii=False, indent=2))


def visible_error(exc, validation_error, desktop_error):
    if isinstance(exc, (validation_error, desktop_error)):
        return str(exc)
    text = str(exc)
    if type(exc).__name__ == "TargetClosedError" or "TargetClosedError" in text:
        return "工作台浏览器被上次的窗口占用，已尝试重新打开。若仍失败，请先关掉工作台网页再试。"
    if "ERR_PROXY" in text:
        return "系统代理连不上，网页没有打开。请确认代理软件已启动。"
    if "net::ERR_" in text or (type(exc).__name__ == "Error" and "Page.goto" in text):
        return "网页没有打开，网络连接失败。请检查工作台地址是否还能访问。"
    return type(exc).__name__


class RunLock:
    """One tax desktop process at a time, even for copies in other folders."""
    def __enter__(self):
        from ctypes import wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateMutexW(None, False, "Local\\TaxZeroBatchRunner_v1")
        if not self.handle:
            raise RuntimeError("无法创建执行锁")
        if ctypes.get_last_error() == 183:
            self.kernel.CloseHandle(self.handle)
            raise RuntimeError("另一个申报程序正在运行，请先完成或关闭它")
        return self

    def __exit__(self, *_):
        self.kernel.CloseHandle(self.handle)


def find_pythonw():
    """微信自动转发是独立窗口，用 pythonw 打开，避免再占一个黑窗口。"""
    found = shutil.which("python")
    if found and "WindowsApps" not in found and Path(found).is_file():
        pythonw = Path(found).with_name("pythonw.exe")
        if pythonw.is_file():
            return str(pythonw)
        return found
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / "Python312"
    for name in ("pythonw.exe", "python.exe"):
        candidate = local / name
        if candidate.is_file():
            return str(candidate)
    return ""


def read_forward_result(path, company_name, tax_label):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    if not isinstance(data.get("company"), str) or data["company"] != company_name:
        return ""
    if not isinstance(data.get("taxLabel"), str) or data["taxLabel"] != tax_label:
        return ""
    destination = data.get("sentTo")
    return destination.strip() if isinstance(destination, str) else ""


def receipt_result_path(company):
    key = hashlib.sha256(company.key.encode("utf-8")).hexdigest()
    return BASE / "data" / "receipts" / f"{key}.json"


class Controller:
    """A single worker owns Playwright, desktop automation, and all mutations."""
    def __init__(self, emit):
        self.emit = emit
        self.jobs = queue.Queue()
        self.stop = threading.Event()
        self.web = None
        self.settings = None
        self.worker = threading.Thread(target=self.loop, daemon=True)
        self.worker.start()

    def log(self, message):
        self.emit("log", message)

    def loop(self):
        while True:
            task, settings = self.jobs.get()
            try:
                if task == "close":
                    if self.web:
                        self.web.close()
                    return
                self.stop.clear()
                with RunLock():
                    self.execute(task, settings)
            except Exception as exc:
                # Framework errors can contain input values/DOM snippets.
                # Only our concise validation errors are shown verbatim.
                from tax_desktop import DesktopError
                detail = visible_error(exc, ValidationError, DesktopError)
                self.log("已停止："+detail)
                self.emit("error", detail)
            finally:
                self.emit("idle", "")

    def ensure_web(self, settings):
        from web_workbench import Workbench
        if self.web is not None and settings != self.settings:
            self.web.close()
            self.web = None
        if self.web is None:
            self.web = Workbench(settings, BASE, self.log)
            self.web.stop = self.stop.is_set
            try:
                self.web.open()
            except Exception:
                self.web = None
                raise
            self.settings = dict(settings)

    def _write_back(self, company, row, lead, store):
        self.log(lead)
        try:
            written = self.web.mark_success(company)
            store.set_step(company, "workbench", "DONE" if written is True else "PENDING")
        except Exception as exc:
            self.log("工作台回填异常：" + type(exc).__name__)
            written = False
        if written is not True:
            row["状态"] = "WRITEBACK_PENDING"
            row["原因"] = "税局申报已成功；工作台回填未核验，请检查。禁止重新申报。"
            self.log("本家回填未完成，继续办理下一家")
        else:
            self.log("工作台回填已重新读取核验")

    def _forward_once(self, company, store):
        """Persist each receipt attempt before launch; never replay an uncertain send."""
        step = store.step(company, "wechat")
        if step and step["state"] == "DONE":
            self.log("本公司本月微信回执已记录完成，跳过重复发送")
            return step["destination"]
        # A child may have written its receipt before the parent crashed.
        for path in (receipt_result_path(company), BASE / "data" / "wechat-forward-result.json"):
            recovered = read_forward_result(path, company.name, company.month)
            if recovered:
                store.set_step(company, "wechat", "DONE", destination=recovered,
                               reason="从已保存的回执结果恢复")
                self.log("已恢复本公司本月的微信成功记录，不再发送")
                return recovered
        if step is None or step["state"] in ("IN_PROGRESS", "UNKNOWN"):
            store.set_step(company, "wechat", "UNKNOWN", reason="旧版记录或上次发送结果缺少明确回执")
            self.log("本公司本月微信发送结果待核实，请到微信核对；程序不会自动重发")
            return ""
        store.set_step(company, "wechat", "IN_PROGRESS", reason="启动发送前已持久化")
        try:
            sent = self.forward_wechat(company)
        except Exception as exc:
            self.log("微信流程中断，发送结果待核实：" + type(exc).__name__)
            sent = ""
        store.set_step(company, "wechat", "DONE" if sent else "UNKNOWN", destination=sent,
                       reason="微信返回明确成功结果" if sent else "可能已部分发送，请人工核对后处理")
        return sent

    def forward_wechat(self, company):
        """申报成功已核验之后打开微信自动转发，并等到它自己关掉。"""
        app_path = BASE / "wechat" / "app.py"
        if not app_path.is_file():
            self.log("没有找到微信自动转发程序，工作台先不点击个税。")
            return ""
        portable_wechat = BASE / "runtime-wechat" / "WeChatReceipt.exe"
        if portable_wechat.is_file():
            command = [str(portable_wechat)]
        else:
            if getattr(sys, "frozen", False):
                self.log("没有找到微信运行组件 runtime-wechat\\WeChatReceipt.exe。请完整解压程序。申报已经成功，请不要再次发送申报。")
                return ""
            pythonw = find_pythonw()
            if not pythonw:
                self.log("没有找到 Python，微信自动转发无法启动。申报已经成功，请不要再次发送申报。")
                return ""
            command = [pythonw, "-X", "utf8", str(app_path)]
        data_dir = BASE / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        company_file = data_dir / "current-company.json"
        result_file = receipt_result_path(company)
        result_file.parent.mkdir(parents=True, exist_ok=True)
        if result_file.exists():
            # Preserve durable evidence. An incomplete old result needs review.
            return read_forward_result(result_file, company.name, company.month)
        payload = {
            "company": company.name,
            "taxLabel": company.month,
            "updatedAt": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
        }
        _atomic_write(company_file, json.dumps(payload, ensure_ascii=False))
        self.log("正在打开微信自动转发。窗口关闭前，不点击工作台个税。")
        try:
            proc = subprocess.Popen(
                [*command, "--from-geshui",
                 "--company-file", str(company_file), "--result-file", str(result_file)],
                cwd=str(app_path.parent),
            )
        except OSError:
            self.log("微信自动转发窗口没有打开。申报已经成功，请不要再次发送申报。")
            return ""
        waited = 0
        stop_noted = False
        while proc.poll() is None:
            time.sleep(2)
            waited += 2
            if self.stop.is_set() and not stop_noted:
                self.log("已请求停止。仍等待微信自动转发结束，结束后才决定是否点击个税。")
                stop_noted = True
            elif waited % 20 == 0:
                self.log("微信自动转发窗口仍在运行，工作台先不点击个税。")
        sent = read_forward_result(result_file, company.name, company.month)
        if not sent:
            self.log("微信自动转发没有完成。申报已经成功，请不要再次发送申报，也先不点击工作台个税。")
            return ""
        self.log(f"微信自动转发已运行完毕，已发送到 {sent}。")
        return sent

    def execute(self, task, settings):
        if task == "check_client":
            from tax_desktop import TaxDesktop
            adapter = TaxDesktop(settings, BASE, lambda _: "", self.log, self.stop.is_set)
            adapter.attach()
            text = adapter.read((0,.955,.93,1))
            if "当前操作单位" not in text:
                raise ValidationError("未识别到扣缴端当前操作单位，请先登录并恢复普通窗口")
            self.log("已读到扣缴端当前单位。此检查不代表全流程已通过联调。")
            return
        self.ensure_web(settings)
        partial_run = task == "test_partial"
        send_test = task == "test_company"
        if getattr(self.web, "partial_list", False) is True:
            self.web.partial_list = False
        if task == "test_partial":
            self.log("切换到部分0申报")
            self.web.select_partial_zero()
            task = "run"
        self.log("读取工作台待办零申报名单")
        companies = self.web.read_companies()
        if task == "run" and getattr(self.web, "partial_list", False) is True:
            self.log(f"部分0申报读到{len(companies)}家，按顺序办理")
        if task == "test_company":
            companies = only_test_company(companies)
            self.log(f"测试只运行：{TEST_COMPANY_NAME}，金额为0后发送申报")
            task = "run"
        self.emit("companies", companies)
        if not companies:
            self.log("当前工作台页面没有待处理个税公司")
            self.emit("result", [])
            return
        if task == "read":
            self.log(f"已读取 {len(companies)} 家企业，请核对月份与预期人数")
            return
        from tax_desktop import TaxDesktop
        adapter = TaxDesktop(settings, BASE, self.web.get_password, self.log, self.stop.is_set)
        data = BASE / "data"
        data.mkdir(parents=True, exist_ok=True)
        store = StateStore(data / "state.json")
        results = []
        report = data / "last_results.csv"
        stopped = False
        for company in companies:
            if stopped or self.stop.is_set():
                results.append(Runner._row(company, "SKIPPED", "批次已停止", ""))
                continue
            if task == "prepare":
                if store.get(company):
                    row = Runner(adapter, store, self.log).run([company])[0]
                    if row["状态"] == "SUCCESS":
                        row["状态"] = "VERIFIED"
                        row["原因"] = "已有申报核验成功；未回填"
                    else:
                        stopped = True
                    results.append(row)
                    write_results(report, results)
                    continue
                try:
                    self.log(f"准备并检查：{company.name}")
                    ready = adapter.prepare(company)
                    if ready not in ("READY", "ALREADY_SUCCESS"):
                        raise ValidationError("准备阶段未取得明确申报状态")
                    if ready == "ALREADY_SUCCESS":
                        if adapter.verify(company) is not True:
                            raise ValidationError("已有提交记录未核验成功")
                        store.set(company, "SUCCESS", "已有申报核验成功", adapter.evidence(company,"VERIFIED"))
                        results.append(Runner._row(company, "VERIFIED", "已有申报核验成功；未回填", adapter.evidence(company,"VERIFIED")))
                    else:
                        self.log("完整流程：检查工资表与零金额")
                        adapter.check_zero(company)
                        self.log("完整流程：税款计算")
                        adapter.calculate(company)
                        adapter.check_attachments(company)
                        adapter.check_summary(company)
                        results.append(Runner._row(company, "PREPARED", "已到提交前；尚未发送申报", adapter.evidence(company,"PREPARED")))
                        self.log("准备完成，本模式不会发送申报")
                except Exception as exc:
                    try:
                        diagnostic = getattr(adapter, "record_error", None)
                        if diagnostic:
                            diagnostic("PREPARE_CHECK", exc)
                    except Exception:
                        pass
                    if isinstance(exc, ValidationError) or type(exc).__name__ == "RuntimeError":
                        self.log(str(exc).splitlines()[0])
                    results.append(Runner._row(company, "FAILED", "准备检查失败："+type(exc).__name__, adapter.evidence(company,"FAILED")))
                    stopped = True
            else:
                row = Runner(adapter, store, self.log).run([company])[0]
                results.append(row)
                # Persist the success record/report before attempting workbench
                # writes. A browser error must never cause tax resubmission.
                write_results(report, results)
                if row["状态"] != "SUCCESS":
                    stopped = True
                elif partial_run:
                    self.log(f"{company.name}：申报成功已核验，打开微信自动转发")
                    try:
                        sent = self._forward_once(company, store)
                    except Exception as exc:
                        self.log("微信阶段记录未能保存，已停止自动发送：" + type(exc).__name__)
                        sent = ""
                    if not sent:
                        stopped = True
                        row["状态"] = "WRITEBACK_PENDING"
                        row["原因"] = "税局申报已成功；微信结果待核实，未点击工作台个税。禁止重新申报或自动重发回执。"
                        self.log("微信结果待核实，批次已停止；请检查仍打开的微信回执窗口，工作台先不点击个税")
                    elif settings.get("writeback", True):
                        self._write_back(company, row, "微信自动转发已运行完毕，正在核验工作台个税", store)
                    else:
                        self.log("微信自动转发已结束。当前未勾选回填，不点击工作台个税。")
                elif not send_test and settings.get("writeback", True):
                    self._write_back(company, row, "税局已成功，核验工作台个税完成状态", store)
            write_results(report, results)
        write_results(report, results)
        stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S")
        write_results(data / f"results_{stamp}.csv", results)
        self.emit("result", results)
        self.log("本批次结束，结果已保存至 data/last_results.csv")


def cli_run():
    log_path = BASE / "data" / "run.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    exit_status = {"code": 2}

    def emit(kind, value):
        if kind == "log":
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(f"{datetime.now(timezone(timedelta(hours=8))).isoformat()} {value}\n")
            if sys.stdout:
                print(value, flush=True)
        elif kind == "result":
            exit_status["code"] = 0 if all(r["状态"] == "SUCCESS" for r in value) else 2

    runner = Controller.__new__(Controller)
    runner.emit, runner.stop, runner.web, runner.settings = emit, threading.Event(), None, None
    try:
        with RunLock():
            runner.execute("run", load_settings())
    except Exception as exc:
        emit("log", "执行失败："+type(exc).__name__)
        exit_status["code"] = 2
    finally:
        if runner.web:
            runner.web.close()
    return exit_status["code"]


def gui():
    import tkinter as tk
    from tkinter import ttk, messagebox
    root = tk.Tk()
    root.title("个税一键申报 · Power Automate")
    root.geometry("1180x670")
    root.minsize(1080,620)
    root.configure(bg="#f4f6f8")
    style = ttk.Style()
    style.theme_use("vista")
    settings = load_settings()
    events = queue.Queue()
    controller = Controller(lambda k,v: events.put((k,v)))
    frame = ttk.Frame(root, padding=18)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="批量零申报", font=("Microsoft YaHei UI",19,"bold")).pack(anchor="w")
    ttk.Label(frame, text="网页取名单 → 扣缴端申报 → 获取成功反馈 → 回填工作台").pack(anchor="w",pady=(4,10))
    ttk.Label(frame, text="当前为视频预设版，首次请先准备并检查；真实界面仍需联调。",foreground="#9a5c00").pack(anchor="w",pady=(0,10))
    fields = ttk.Frame(frame)
    fields.pack(fill="x")
    variables = {}
    for i,(key,title,width) in enumerate((("url","工作台地址",60),("work_month","网页工作月份",10),
                                        ("tax_month","税款所属月份",10),("expected_people","每家预期人数",10))):
        ttk.Label(fields,text=title).grid(row=i,column=0,sticky="w",pady=3)
        var=tk.StringVar(value=str(settings[key]))
        variables[key]=var
        ttk.Entry(fields,textvariable=var,width=width).grid(row=i,column=1,sticky="w",padx=10,pady=3)
    checks=ttk.Frame(frame)
    checks.pack(fill="x",pady=10)
    for key,title in (("generate_zero","空工资表时允许复制上月数据"),("allow_missing_insurance","允许三险一金为空"),("writeback","成功后回填工作台")):
        var=tk.BooleanVar(value=settings[key]);variables[key]=var
        ttk.Checkbutton(checks,text=title,variable=var).pack(side="left",padx=(0,16))
    buttons=ttk.Frame(frame);buttons.pack(fill="x",pady=(0,10))
    action_buttons=[]
    busy={"value":False}

    def action(task):
        if busy["value"]:
            return
        try:
            new=dict(settings)
            new.update({k:v.get().strip() if isinstance(v,tk.StringVar) else v.get() for k,v in variables.items()})
            new["expected_people"]=int(new["expected_people"])
            if new["expected_people"]<1:
                raise ValueError()
            import re
            if any(not re.fullmatch(r"[1-9]\d{3}-(0[1-9]|1[0-2])",new[k]) for k in ("work_month","tax_month")):
                raise ValueError()
            save_settings(new)
            settings.update(new)
            busy["value"]=True
            for button in action_buttons:
                button.configure(state="disabled")
            controller.jobs.put((task,new))
        except ValueError:
            messagebox.showerror("设置需要修正","月份请填写 YYYY-MM，人数请填写正整数。",parent=root)

    for text,task in (("读取网页名单","read"),("检查扣缴端","check_client"),("准备并检查","prepare"),("一键申报","run"),("测试万瑞铭诚","test_company"),("测试部分0申报","test_partial")):
        button=ttk.Button(buttons,text=text,command=lambda t=task:action(t));button.pack(side="left",padx=(0,8));action_buttons.append(button)
    ttk.Button(buttons,text="停止（Esc）",command=controller.stop.set).pack(side="right")
    root.bind("<Escape>",lambda _:controller.stop.set())
    tree=ttk.Treeview(frame,columns=("company","month","people","status"),show="headings",height=6)
    for key,title,width in (("company","公司",360),("month","所属月",110),("people","人数",70),("status","结果",150)):
        tree.heading(key,text=title);tree.column(key,width=width)
    tree.pack(fill="both",expand=True)
    console=tk.Text(frame,height=7,font=("Microsoft YaHei UI",9),bg="#ffffff",relief="flat",state="disabled")
    console.pack(fill="both",expand=True,pady=10)
    bottom=ttk.Frame(frame);bottom.pack(fill="x")
    ttk.Label(bottom,text="一键申报与测试走同一套流程。准备并检查不发送。",foreground="#666666").pack(side="left")
    ttk.Button(bottom,text="打开结果文件夹",command=lambda:open_data()).pack(side="right")
    def open_data():
        (BASE/"data").mkdir(exist_ok=True)
        os.startfile(BASE/"data")
    def poll():
        while True:
            try:kind,value=events.get_nowait()
            except queue.Empty:break
            if kind=="log":
                console.configure(state="normal");console.insert("end",value+"\n");console.see("end");console.configure(state="disabled")
            elif kind=="companies":
                tree.delete(*tree.get_children())
                for c in value:tree.insert("", "end",iid=c.key,values=(c.name,c.month,c.expected_people,"待处理"))
            elif kind=="result":
                for r in value:
                    key=r["公司名称"]+":"+r["税款所属月"]
                    if tree.exists(key):tree.set(key,"status",r["状态"])
            elif kind=="idle":
                busy["value"]=False
                for button in action_buttons:button.configure(state="normal")
        root.after(150,poll)
    def close():
        controller.stop.set()
        if busy["value"]:
            messagebox.showinfo("正在停止","已请求停止。请待当前步骤结束后再关闭；发送过的申报会保留待核验记录。",parent=root)
            return
        controller.jobs.put(("close",None));root.destroy()
    root.protocol("WM_DELETE_WINDOW",close)
    root.after(150,poll)
    root.mainloop()


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--run",action="store_true",help="Run saved configuration from Power Automate")
    parser.add_argument("--self-test",action="store_true",help="Offline import check; no browser or desktop input")
    parser.add_argument("--bridge",action="store_true",help="Open the authenticated Mac connection window on Windows")
    parser.add_argument("--bridge-self-test",action="store_true",help="Offline bridge TLS/Tk check using a temporary fake controller")
    parser.add_argument("--report",type=Path,help="JSON report path for --bridge-self-test")
    args=parser.parse_args()
    if args.bridge_self_test:
        if args.report is None:
            parser.error("--bridge-self-test requires --report PATH")
        from bridge_host import run_self_test
        sys.exit(run_self_test(args.report))
    if args.report is not None:
        parser.error("--report is only used with --bridge-self-test")
    if args.self_test:
        from runtime_checks import run_self_test
        sys.exit(run_self_test(BASE))
    if args.bridge:
        from bridge_host import main
        sys.exit(main(BASE))
    if args.run:
        sys.exit(cli_run())
    gui()

