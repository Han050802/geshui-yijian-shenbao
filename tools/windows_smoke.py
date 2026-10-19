"""Validate copied frozen runtimes without opening business applications."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if sys.platform != "win32":
        raise RuntimeError("Frozen runtime smoke checks require Windows")
    report = {"platform": platform.platform(), "python": sys.version,
              "source_commit": os.environ.get("GITHUB_SHA", ""), "checks": {}}
    report_dir = ROOT / "build-reports"
    report_dir.mkdir(exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="geshui-") as tmp:
            base = Path(tmp) / "个税便携 自检"
            base.mkdir()
            for name in ("runtime", "runtime-wechat"):
                shutil.copytree(ROOT / name, base / name)
            (base / "wechat").mkdir()
            for source in (ROOT / "wechat").glob("*.py"):
                shutil.copy2(source, base / "wechat" / source.name)
            env = os.environ.copy()
            for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
                env.pop(key, None)
            # os.environ retains Windows' case-insensitive lookup; a dict copy does not.
            system = Path(os.environ["SystemRoot"])
            env["PATH"] = str(system / "System32") + os.pathsep + str(system)
            env["PYTHONNOUSERSITE"] = "1"
            env["PLAYWRIGHT_BROWSERS_PATH"] = "0"
            runs = (
                ("main", "runtime/TaxZero.exe", "self_test.json", ["--self-test"]),
                ("wechat", "runtime-wechat/WeChatReceipt.exe", "wechat_self_test.json", ["--self-test"]),
                ("bridge", "runtime/TaxZero.exe", "bridge_self_test.json", ["--bridge-self-test"]),
            )
            for name, relative, output, arguments in runs:
                executable = base / relative
                result_path = base / "data" / output
                if result_path.exists():
                    raise RuntimeError("Smoke check must produce a fresh report")
                if name == "bridge":
                    arguments += ["--report", str(result_path)]
                proc = subprocess.run([str(executable), *arguments], cwd=str(base),
                                      env=env, capture_output=True, timeout=240)
                result = json.loads(result_path.read_text(encoding="utf-8-sig")) if result_path.is_file() else {}
                report["checks"][name] = {
                    "exit_code": proc.returncode, "result": result,
                    "exe_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                }
                if name == "main":
                    passed = all(result.get(k) == "ok" for k in
                                 ("imports", "excel_mapping", "ocr_models", "tkinter", "bundled_browser"))
                elif name == "wechat":
                    passed = result.get("ok") is True
                else:
                    passed = (result.get("status") == "passed" and result.get("offline") is True
                              and result.get("real_business_executed") is False)
                if proc.returncode or not passed:
                    raise RuntimeError(name + " packaged self-test did not pass: " + json.dumps(result))
                print(name + ": packaged self-test passed")
            report["status"] = "passed"
        return 0
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        (report_dir / "smoke-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

