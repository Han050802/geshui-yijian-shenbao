"""Verify the real frozen Mac app after relocation, without real connections."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "个税一键申报 Mac.app"
BINARY_NAME = "TaxZeroMac"
SOURCE_FILES = ("mac_app.py", "bridge_protocol.py")
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bundle_inventory(app: Path) -> list[dict]:
    """Hash contents, symlink targets and permissions without following links."""
    app = app.resolve(strict=True)
    entries = []
    for directory, directories, files in os.walk(app, followlinks=False):
        for name in sorted(directories + files):
            path = Path(directory) / name
            relative = path.relative_to(app).as_posix()
            mode = stat.S_IMODE(path.lstat().st_mode)
            if path.is_symlink():
                target = os.readlink(path)
                if Path(target).is_absolute():
                    raise RuntimeError("Absolute symlink inside app: " + relative)
                if not path.resolve(strict=True).is_relative_to(app):
                    raise RuntimeError("Symlink escapes app: " + relative)
                entry = {"path": relative, "type": "symlink", "target": target}
            elif path.is_dir():
                entry = {"path": relative, "type": "directory", "mode": mode}
            elif path.is_file():
                entry = {"path": relative, "type": "file", "mode": mode,
                         "size": path.stat().st_size, "sha256": sha256_file(path)}
            else:
                raise RuntimeError("Unsupported app bundle entry: " + relative)
            entries.append(entry)
    return sorted(entries, key=lambda entry: entry["path"])


def inventory_digest(entries: list[dict]) -> str:
    payload = json.dumps(entries, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def command_result(args: list[str], *, check: bool = True) -> dict:
    completed = subprocess.run(args, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=60)
    result = {"exit_code": completed.returncode,
              "stdout": completed.stdout.strip(), "stderr": completed.stderr.strip()}
    if check and completed.returncode:
        raise RuntimeError(f"{Path(args[0]).name} failed: {result['stderr'] or result['stdout']}")
    return result


def validate_bundle(app: Path) -> dict:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise RuntimeError("This validation must run on a native Apple Silicon Mac")
    with (app / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    if info.get("CFBundleExecutable") != BINARY_NAME:
        raise RuntimeError("Unexpected CFBundleExecutable")
    if info.get("LSMinimumSystemVersion") != "15.0":
        raise RuntimeError("Minimum supported macOS must be declared as 15.0")
    executable = app / "Contents/MacOS" / BINARY_NAME
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError("The app executable is missing or not executable")
    entries = bundle_inventory(app)
    binaries = []
    for entry in entries:
        if entry["type"] != "file":
            continue
        path = app / entry["path"]
        with path.open("rb") as stream:
            magic = stream.read(4)
        if magic in MACHO_MAGICS:
            arches = command_result(["/usr/bin/lipo", "-archs", str(path)])["stdout"].split()
            if arches != ["arm64"]:
                raise RuntimeError("Non-arm64 Mach-O bundled: " + entry["path"])
            binaries.append({"path": entry["path"], "architectures": arches})
    if not any(binary["path"] == f"Contents/MacOS/{BINARY_NAME}" for binary in binaries):
        raise RuntimeError("The main executable is not a Mach-O binary")
    verification = command_result([
        "/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)
    ])
    signing = command_result(["/usr/bin/codesign", "--display", "--verbose=4", str(app)])
    if "Signature=adhoc" not in signing["stderr"]:
        raise RuntimeError("Expected explicit ad-hoc signing evidence")
    # Developer ID/Gatekeeper approval is separate from signature integrity.
    # A rejection is expected for this unnotarized, ad-hoc signed private build.
    gatekeeper = command_result([
        "/usr/sbin/spctl", "--assess", "--type", "execute", "--verbose=2", str(app)
    ], check=False)
    return {
        "bundle_sha256": inventory_digest(entries),
        "bundle_entry_count": len(entries),
        "executable_sha256": sha256_file(executable),
        "executable_mode": oct(stat.S_IMODE(executable.stat().st_mode)),
        "executable_is_executable": True,
        "mach_o_files": binaries,
        "minimum_macos": info["LSMinimumSystemVersion"],
        "codesign_verify": verification,
        "codesign_details": signing,
        "signing": "ad-hoc",
        "apple_notarized": False,
        "gatekeeper_assessment": gatekeeper,
    }


def clean_app_environment() -> tuple[dict[str, str], list[str]]:
    environment = os.environ.copy()
    removed = []
    exact = {"VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "__PYVENV_LAUNCHER__",
             "TCL_LIBRARY", "TK_LIBRARY", "DYLD_LIBRARY_PATH",
             "DYLD_FALLBACK_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH",
             "DYLD_FALLBACK_FRAMEWORK_PATH", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    prefixes = ("PYTHON", "PYENV_", "CONDA_")
    for key in list(environment):
        if key in exact or key.startswith(prefixes):
            removed.append(key)
            del environment[key]
    environment["PATH"] = SYSTEM_PATH
    if environment.get("HOME") != os.environ.get("HOME"):
        raise RuntimeError("Self-test must preserve the real HOME environment")
    return environment, sorted(removed)


def run_relocated_self_test(app: Path, reports: Path) -> dict:
    """Launch the frozen executable with no source cwd or Python dependencies."""
    reports.mkdir(parents=True, exist_ok=True)
    original = validate_bundle(app)
    with tempfile.TemporaryDirectory(prefix="个税 Mac 自检 ") as temporary:
        temporary_path = Path(temporary)
        relocated = temporary_path / APP_NAME
        command_result(["/usr/bin/ditto", str(app), str(relocated)])
        copied = validate_bundle(relocated)
        if copied["bundle_sha256"] != original["bundle_sha256"]:
            raise RuntimeError("Relocation changed bundle contents or permissions")
        application_report = temporary_path / "离线 UI 自检结果.json"
        executable = relocated / "Contents/MacOS" / BINARY_NAME
        environment, removed = clean_app_environment()
        completed = subprocess.run(
            [str(executable), "--self-test", "--report", str(application_report)],
            cwd=temporary_path, env=environment, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=90,
        )
        (reports / "app-self-test-stdout.txt").write_text(completed.stdout, encoding="utf-8")
        (reports / "app-self-test-stderr.txt").write_text(completed.stderr, encoding="utf-8")
        if application_report.is_file():
            raw_report = application_report.read_text(encoding="utf-8")
            (reports / "app-self-test.json").write_text(raw_report, encoding="utf-8")
        else:
            raise RuntimeError("Frozen app did not produce its self-test report")
        app_report = json.loads(raw_report)
        if completed.returncode != 0 or app_report.get("status") != "passed":
            raise RuntimeError(f"Frozen Mac self-test failed with exit code {completed.returncode}")
        for field, expected in (("offline", True), ("ui_created", True),
                                ("user_pairing_accessed", False)):
            if app_report.get(field) is not expected:
                raise RuntimeError("Self-test report does not confirm: " + field)
        if not isinstance(app_report.get("checks"), (dict, list)) or not app_report["checks"]:
            raise RuntimeError("Self-test report is missing actual UI checks")
        after = inventory_digest(bundle_inventory(relocated))
        if after != copied["bundle_sha256"]:
            raise RuntimeError("Self-test modified the signed application bundle")
        return dict(
            original,
            exit_code=completed.returncode,
            relocated_to_chinese_path_with_spaces=True,
            relocated_bundle_sha256=copied["bundle_sha256"],
            offline=True,
            ui_created=True,
            user_pairing_accessed=False,
            path=SYSTEM_PATH,
            removed_environment_variable_names=removed,
            home_preserved=True,
            app_self_test=app_report,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, default=ROOT / "build-dist-mac" / APP_NAME)
    parser.add_argument("--reports", type=Path, default=ROOT / "build-reports-mac")
    args = parser.parse_args()
    reports = args.reports.resolve()
    reports.mkdir(parents=True, exist_ok=True)
    report = {
        "status": "failed",
        "source_commit": os.environ.get("GITHUB_SHA", ""),
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "tested_macos": platform.mac_ver()[0],
        "architecture": platform.machine(),
        "real_tax_filing_verified": False,
        "real_windows_connection_verified": False,
    }
    try:
        report["source_sha256"] = {name: sha256_file(ROOT / name) for name in SOURCE_FILES}
        environment = json.loads((ROOT / "build-reports-mac/build-environment.json").read_text(encoding="utf-8"))
        if environment.get("source_sha256") != report["source_sha256"]:
            raise RuntimeError("Source changed after staging the frozen build")
        report["checks"] = run_relocated_self_test(args.app.resolve(), reports)
        report["status"] = "passed"
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    (reports / "smoke-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": report["status"], "report": str(reports / "smoke-report.json"),
                      "error": report.get("error", "")}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
