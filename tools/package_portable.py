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
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode("utf-8").split("\0")
    files = {}
    for relative in tracked:
        if relative:
            path = PurePosixPath(relative)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError("Invalid tracked path")
            files[relative] = ROOT / relative
    for directory in ("runtime", "runtime-wechat", "build-reports"):
        for path in (ROOT / directory).rglob("*"):
            if path.is_file():
                files[path.relative_to(ROOT).as_posix()] = path
    for required in ("runtime/TaxZero.exe", "runtime-wechat/WeChatReceipt.exe", "一键启动.cmd"):
        if required not in files:
            raise RuntimeError("Missing runtime entry point: " + required)
    checksums = [hashlib.sha256(path.read_bytes()).hexdigest() + "  " + relative
                 for relative, path in sorted(files.items())]
    build_info = {"source_commit": os.environ.get("GITHUB_SHA", ""),
                  "built_at_utc": datetime.now(timezone.utc).isoformat(),
                  "platform": "Windows x64", "smoke_test": "passed",
                  "source_files_and_runtime_files": len(files)}
    destination = ROOT / "dist-output"
    destination.mkdir(exist_ok=True)
    archive = destination / "个税一键申报_Windows完整运行包_2026-10-19.zip"
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
