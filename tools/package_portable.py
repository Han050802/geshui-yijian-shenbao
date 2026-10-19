"""Package tracked files and verified Windows runtimes into one ZIP."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    smoke = json.loads((ROOT / "build-reports/smoke-report.json").read_text(encoding="utf-8"))
    if smoke.get("status") != "passed":
        raise RuntimeError("Do not package unverified runtimes")
    source_commit = os.environ.get("GITHUB_SHA", "")
    if not source_commit or smoke.get("source_commit") != source_commit:
        raise RuntimeError("Smoke report does not match the current source commit")
    smoke_checks = smoke.get("checks")
    if not isinstance(smoke_checks, dict):
        raise RuntimeError("Smoke report is missing executable verification")
    for name, relative in (
        ("main", "runtime/TaxZero.exe"),
        ("wechat", "runtime-wechat/WeChatReceipt.exe"),
        ("bridge", "runtime/TaxZero.exe"),
    ):
        check = smoke_checks.get(name)
        if not isinstance(check, dict) or check.get("exit_code") != 0:
            raise RuntimeError("Missing successful smoke check: " + name)
        executable = ROOT / relative
        if not executable.is_file():
            raise RuntimeError("Missing verified executable: " + relative)
        current_digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        if check.get("exe_sha256") != current_digest:
            raise RuntimeError("Executable changed after its smoke check: " + relative)
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode("utf-8").split("\0")
    files = {}
    for relative in tracked:
        if relative:
            path = PurePosixPath(relative)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError("Invalid tracked path")
            files[relative] = ROOT / relative
    # Refuse to ship changed business records or line-ending conversions.
    # Source bytes in the ZIP must be the exact committed Git blob bytes.
    index = subprocess.check_output(["git", "ls-files", "-s", "-z"], cwd=ROOT).decode("utf-8")
    for record in index.split("\0"):
        if not record:
            continue
        metadata, relative = record.split("\t", 1)
        mode, expected, stage = metadata.split()
        if mode not in ("100644", "100755") or stage != "0":
            raise RuntimeError("Unsupported tracked file mode or merge state: " + relative)
        content = files[relative].read_bytes()
        actual = hashlib.sha1(b"blob " + str(len(content)).encode("ascii") + b"\0" + content).hexdigest()
        if actual != expected:
            raise RuntimeError("Tracked file bytes changed before packaging: " + relative)
    for directory in ("runtime", "runtime-wechat", "build-reports"):
        for path in (ROOT / directory).rglob("*"):
            if path.is_file():
                files[path.relative_to(ROOT).as_posix()] = path
    for required in ("runtime/TaxZero.exe", "runtime-wechat/WeChatReceipt.exe", "一键启动.cmd", "启动Mac连接.cmd"):
        if required not in files:
            raise RuntimeError("Missing runtime entry point: " + required)
    checksums = [hashlib.sha256(path.read_bytes()).hexdigest() + "  " + relative
                 for relative, path in sorted(files.items())]
    build_info = {"source_commit": source_commit,
                  "built_at_utc": datetime.now(timezone.utc).isoformat(),
                  "platform": "Windows x64", "smoke_test": "passed",
                  "companion_protocol": 1,
                  "bridge_smoke_test": "passed",
                  "source_files_and_runtime_files": len(files)}
    destination = ROOT / "dist-output"
    destination.mkdir(exist_ok=True)
    archive = destination / "个税一键申报_Windows完整运行包_Excel联动版1.1.1_2026-10-19.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for relative, path in sorted(files.items()):
            z.write(path, "个税一键申报/" + relative)
        z.writestr("个税一键申报/SHA256SUMS.txt", "\n".join(checksums) + "\n")
        z.writestr("个税一键申报/build-info.json", json.dumps(build_info, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None:
            raise RuntimeError("ZIP integrity check failed")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (destination / "SHA256.txt").write_text(digest + "  " + archive.name + "\n", encoding="utf-8")
    info = dict(build_info, filename=archive.name, size_bytes=archive.stat().st_size, sha256=digest,
                zip_entry_count=len(files) + 2)
    (destination / "package-info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(info, ensure_ascii=True))


if __name__ == "__main__":
    main()

