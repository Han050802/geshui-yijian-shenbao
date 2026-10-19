"""Package only the verified native Mac operator app, then verify both archives."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from mac_smoke import (
    APP_NAME, ROOT, SOURCE_FILES, bundle_inventory, command_result,
    inventory_digest, run_relocated_self_test, sha256_file, validate_bundle,
)

PACKAGE_NAME = "个税一键申报 Mac操作端"
ARCHIVE_STEM = "个税一键申报_Mac操作端_AppleSilicon_2026-10-19"
REPORT_ALLOWLIST = (
    "build-environment.json", "dependencies.txt", "smoke-report.json",
    "app-self-test.json", "protocol-tests.txt", "host-tests.txt", "mac-tests.txt",
)


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_readme(environment: dict, source_commit: str) -> str:
    return f"""个税一键申报 — Mac 操作端

适用：Apple Silicon（M 系列）Mac，macOS 15.0 或更新版本。
本次实际构建和离线自检系统：macOS {environment['tested_macos']}，arm64。
本包自带 Python/Tk/SSL 运行组件，使用者无需安装 Python。

安装到 Mac
1. 打开 DMG，将“个税一键申报 Mac.app”拖入旁边的“Applications/应用程序”。
2. 在“应用程序”中双击打开。ZIP 版本请使用 Mac 自带“归档实用工具”解压，
   再将 .app 拖入“应用程序”；不要拆开 .app 内部文件。
3. 这是私人分发的 ad-hoc 签名程序，没有 Apple 开发者身份签名或 Apple 公证。
   首次打开可能被 macOS 阻止。确认文件来自您自己的仓库后，在
   “系统设置 → 隐私与安全性”中选择该应用对应的“仍要打开”，再确认打开。
   不需要关闭系统安全功能。Apple 官方说明：
   https://support.apple.com/guide/mac-help/mh40616/mac

使用方式：Mac 操作，Windows 执行
1. 在 Windows 电脑上解压“个税一键申报_Windows连接端_2026-10-19.zip”，
   保持文件夹完整，双击“启动Mac连接.cmd”，在界面点击“复制连接码”。
2. Mac 与 Windows 需处于彼此可连接的受信网络；在 Mac 应用中点击
   “粘贴连接码”并连接。连接码包含连接凭据，请只在自己的设备间传送，
   不要上传到 GitHub 或发给他人。
3. Windows 上打开自然人电子税务局扣缴端并人工核对所属月份和企业等信息。
   运行时保持 Windows 桌面解锁；税务登录、验证码等步骤仍在 Windows 处理。
4. 先在 Mac 选择准备/检查，核对 Windows 生成的草稿；核实业务条件后再正式申报。
   Mac 负责操作界面和接收状态，实际扣缴端自动化始终在 Windows 执行。
5. 对已发送但反馈不明的申报，请先核对税务端状态，不要直接重复发送。

本次验证范围
已实际验证：Mac 原生 arm64 Mach-O、.app 执行权限、ad-hoc 签名完整性；
将 .app 复制到包含中文和空格的临时目录后，在仅保留系统 PATH 的环境中，
实际运行打包后的离线 UI 自检。自检不读取用户配对、不连接真实 Windows、
不访问税务系统、不发送申报。具体证据见“验证报告”。
本包不包含原始税务数据、Windows runtime、真实连接码或任何已验证申报回执。
真实工作台与扣缴端完整链路仍需在您的 Windows 现场核对和联调。
已经通过离线自检，不等于已经验证真实申报成功。

核对版本
源提交：{source_commit}
构建时间及依赖见 build-info.json 与“验证报告/build-environment.json”。
外部 SHA256.txt 可核对 DMG、ZIP、说明和验证报告是否完整下载。
两个归档包含同一份 .app；通常使用 DMG 即可，不必重复安装两份。
"""


def verify_dmg(archive: Path, expected_digest: str) -> dict:
    verification = command_result(["/usr/bin/hdiutil", "verify", str(archive)])
    with tempfile.TemporaryDirectory(prefix="个税 DMG 复验 ") as temporary:
        mountpoint = Path(temporary) / "只读 挂载"
        mountpoint.mkdir()
        attached = False
        try:
            subprocess.run([
                "/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen",
                "-mountpoint", str(mountpoint), str(archive),
            ], check=True, capture_output=True, timeout=60)
            attached = True
            app_check = validate_bundle(mountpoint / APP_NAME)
            if app_check["bundle_sha256"] != expected_digest:
                raise RuntimeError("DMG changed the verified app bundle")
            applications = mountpoint / "Applications"
            if not applications.is_symlink() or os.readlink(applications) != "/Applications":
                raise RuntimeError("DMG lacks its Applications drag-install link")
            if not (mountpoint / "使用说明.txt").is_file():
                raise RuntimeError("DMG is missing its installation instructions")
            return {"status": "passed", "checksum_verification": verification,
                    "read_only_mount_verified": True,
                    "applications_link_verified": True,
                    "bundle_sha256": app_check["bundle_sha256"],
                    "codesign_verify": app_check["codesign_verify"]}
        finally:
            if attached:
                command_result(["/usr/bin/hdiutil", "detach", str(mountpoint)])


def verify_zip(archive: Path, expected_digest: str, reports: Path) -> dict:
    with zipfile.ZipFile(archive) as zipped:
        if zipped.testzip() is not None:
            raise RuntimeError("ZIP integrity verification failed")
    with tempfile.TemporaryDirectory(prefix="个税 ZIP 解压复验 ") as temporary:
        restored = Path(temporary)
        command_result(["/usr/bin/ditto", "-x", "-k", str(archive), str(restored)])
        app = restored / PACKAGE_NAME / APP_NAME
        actual_digest = inventory_digest(bundle_inventory(app))
        if actual_digest != expected_digest:
            raise RuntimeError("ZIP extraction changed app contents, links or permissions")
        check = run_relocated_self_test(app, reports / "zip-restored-self-test")
        if check["bundle_sha256"] != expected_digest:
            raise RuntimeError("ZIP self-test did not use the verified app bundle")
        return {"status": "passed", "integrity_verified": True,
                "mac_ditto_extraction_verified": True,
                "symlinks_and_permissions_preserved": True,
                "restored_app_self_test": check}


def main() -> int:
    if sys.platform != "darwin":
        raise RuntimeError("Mac artifacts must be assembled and verified on macOS")
    reports = ROOT / "build-reports-mac"
    environment = json.loads((reports / "build-environment.json").read_text(encoding="utf-8"))
    smoke = json.loads((reports / "smoke-report.json").read_text(encoding="utf-8"))
    source_commit = os.environ.get("GITHUB_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise RuntimeError("A verified GitHub source commit is required for packaging")
    if smoke.get("status") != "passed" or smoke.get("source_commit") != source_commit:
        raise RuntimeError("Do not package an unverified app or a different source commit")
    if environment.get("source_commit") != source_commit:
        raise RuntimeError("Build environment evidence belongs to a different source commit")
    for name in SOURCE_FILES:
        actual = sha256_file(ROOT / name)
        if smoke.get("source_sha256", {}).get(name) != actual:
            raise RuntimeError("Mac source changed after self-test: " + name)
        if environment.get("source_sha256", {}).get(name) != actual:
            raise RuntimeError("Mac source differs from the frozen input: " + name)
    app = ROOT / "build-dist-mac" / APP_NAME
    verified = validate_bundle(app)
    expected_digest = smoke.get("checks", {}).get("bundle_sha256")
    if not expected_digest or verified["bundle_sha256"] != expected_digest:
        raise RuntimeError("Mac app changed after successful self-test")

    output = ROOT / "dist-output-mac"
    output.mkdir(exist_ok=True)
    archive_zip = output / (ARCHIVE_STEM + ".zip")
    archive_dmg = output / (ARCHIVE_STEM + ".dmg")
    readme = make_readme(environment, source_commit)
    info = dict(environment, source_commit=source_commit, app_name=APP_NAME,
                bundle_sha256=expected_digest, offline_self_test="passed",
                packaged_at_utc=datetime.now(timezone.utc).isoformat(),
                includes_windows_runtime=False, includes_original_tax_data=False,
                includes_real_pairing=False)
    with tempfile.TemporaryDirectory(prefix="Mac 打包 ", dir=ROOT / "build-work-mac") as temporary:
        stage = Path(temporary) / PACKAGE_NAME
        stage.mkdir()
        command_result(["/usr/bin/ditto", str(app), str(stage / APP_NAME)])
        (stage / "Applications").symlink_to("/Applications", target_is_directory=True)
        (stage / "使用说明.txt").write_text(readme, encoding="utf-8")
        write_json(stage / "build-info.json", info)
        staged_reports = stage / "验证报告"
        staged_reports.mkdir()
        for name in REPORT_ALLOWLIST:
            source = reports / name
            if not source.is_file() or source.is_symlink():
                raise RuntimeError("Missing regular verification report: " + name)
            shutil.copy2(source, staged_reports / name)
        if inventory_digest(bundle_inventory(stage / APP_NAME)) != expected_digest:
            raise RuntimeError("Packaging stage changed the signed app")
        # ditto preserves the symlinks and executable bits used by PyInstaller 6.
        if archive_zip.exists():
            archive_zip.unlink()
        command_result([
            "/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
            str(stage), str(archive_zip),
        ])
        command_result([
            "/usr/bin/hdiutil", "create", "-ov", "-format", "UDZO", "-fs", "HFS+",
            "-volname", "个税一键申报 Mac", "-srcfolder", str(stage), str(archive_dmg),
        ])

    package_verification = {
        "status": "failed", "source_commit": source_commit,
        "bundle_sha256": expected_digest,
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "apple_notarized": False,
        "real_tax_filing_verified": False,
    }
    try:
        package_verification["dmg"] = verify_dmg(archive_dmg, expected_digest)
        package_verification["zip"] = verify_zip(archive_zip, expected_digest, reports)
        package_verification["status"] = "passed"
    finally:
        write_json(reports / "package-verification.json", package_verification)
    if package_verification["status"] != "passed":
        raise RuntimeError("Mac archive verification failed")

    (output / "使用说明.txt").write_text(readme, encoding="utf-8")
    write_json(output / "package-verification.json", package_verification)
    info["archives"] = [
        {"filename": path.name, "size_bytes": path.stat().st_size,
         "sha256": sha256_file(path)} for path in (archive_dmg, archive_zip)
    ]
    info["archive_verification"] = "passed"
    write_json(output / "package-info.json", info)
    deliverables = [archive_dmg, archive_zip, output / "使用说明.txt",
                    output / "package-verification.json", output / "package-info.json"]
    (output / "SHA256.txt").write_text(
        "".join(sha256_file(path) + "  " + path.name + "\n" for path in deliverables),
        encoding="utf-8",
    )
    print(json.dumps({"status": "passed", "archives": info["archives"],
                      "source_commit": source_commit}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
