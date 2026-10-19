#!/bin/bash
# Native build only. The Mac package contains no Windows runtime or tax records.
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
MAC_BUILD_PYTHON="${1:-python3}"

"$MAC_BUILD_PYTHON" -c 'import platform,sys; assert sys.platform == "darwin" and platform.machine() == "arm64", "Build on a native Apple Silicon Mac"; assert sys.version_info[:2] == (3,12), "Python 3.12 is required"; assert int(platform.mac_ver()[0].split(".")[0]) >= 15, "macOS 15 or newer is required"'

mkdir -p build-work-mac build-reports-mac build-dist-mac
MAC_VENV_PYTHON="$PROJECT_ROOT/build-work-mac/venv/bin/python"
if [[ ! -x "$MAC_VENV_PYTHON" ]]; then
    "$MAC_BUILD_PYTHON" -m venv "$PROJECT_ROOT/build-work-mac/venv"
fi
"$MAC_VENV_PYTHON" -m pip install --disable-pip-version-check 'pyinstaller==6.22.3' -r requirements-bridge.txt
"$MAC_VENV_PYTHON" -m pip check
"$MAC_VENV_PYTHON" -m pip freeze > build-reports-mac/dependencies.txt

# These tests use fixtures/loopback only. They never contact a tax workstation.
"$MAC_VENV_PYTHON" - <<'PY'
from pathlib import Path
tests = Path("tests")
for name in ("test_bridge_protocol.py", "test_bridge_host.py"):
    if not (tests / name).is_file():
        raise RuntimeError("Missing required offline test: " + name)
if not list(tests.glob("test_mac*.py")):
    raise RuntimeError("Mac UI offline tests are missing")
PY
"$MAC_VENV_PYTHON" -m unittest discover -s tests -p 'test_bridge_protocol.py' -v 2>&1 | tee build-reports-mac/protocol-tests.txt
"$MAC_VENV_PYTHON" -m unittest discover -s tests -p 'test_bridge_host.py' -v 2>&1 | tee build-reports-mac/host-tests.txt
"$MAC_VENV_PYTHON" -m unittest discover -s tests -p 'test_mac*.py' -v 2>&1 | tee build-reports-mac/mac-tests.txt

"$MAC_VENV_PYTHON" - <<'PY'
import json
import os
import platform
import ssl
import sys
import tkinter
from datetime import datetime, timezone
from pathlib import Path
import PyInstaller

assert sys.platform == "darwin" and platform.machine() == "arm64"
assert sys.version_info[:2] == (3, 12)
assert PyInstaller.__version__ == "6.22.3"
window = tkinter.Tk()
window.withdraw()
window.update_idletasks()
tk_patchlevel = window.tk.call("info", "patchlevel")
window.destroy()
report = {
    "source_commit": os.environ.get("GITHUB_SHA", ""),
    "built_at_utc": datetime.now(timezone.utc).isoformat(),
    "platform": "macOS Apple Silicon",
    "architecture": platform.machine(),
    "tested_macos": platform.mac_ver()[0],
    "minimum_macos": "15.0",
    "python": platform.python_version(),
    "pyinstaller": PyInstaller.__version__,
    "openssl": ssl.OPENSSL_VERSION,
    "tk_patchlevel": tk_patchlevel,
    "runner_image": os.environ.get("ImageOS", ""),
    "runner_image_version": os.environ.get("ImageVersion", ""),
    "bundled_source_allowlist": ["mac_app.py", "bridge_protocol.py"],
    "signing": "ad-hoc",
    "apple_notarized": False,
    "real_tax_filing_verified": False,
}
Path("build-reports-mac/build-environment.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
PY

# Stage only the two Mac-side source files. Never copy the repository or data/.
MAC_SOURCE_STAGE="$PROJECT_ROOT/build-work-mac/source"
mkdir -p "$MAC_SOURCE_STAGE" "$PROJECT_ROOT/build-work-mac/specs"
"$MAC_VENV_PYTHON" - <<'PY'
import hashlib
import json
import shutil
from pathlib import Path
stage = Path("build-work-mac/source")
allowlist = {"mac_app.py", "bridge_protocol.py"}
for path in stage.iterdir():
    if path.name not in allowlist:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
for name in sorted(allowlist):
    source = Path(name)
    if source.is_symlink() or not source.is_file():
        raise RuntimeError("Missing regular Mac source file: " + name)
    shutil.copy2(source, stage / name)
report_path = Path("build-reports-mac/build-environment.json")
report = json.loads(report_path.read_text(encoding="utf-8"))
report["source_sha256"] = {
    name: hashlib.sha256((stage / name).read_bytes()).hexdigest()
    for name in sorted(allowlist)
}
report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

# Let PyInstaller apply its default ad-hoc signatures. Supplying a signing
# identity enables hardened runtime, which is inappropriate for ad-hoc builds.
# Do not enable argv emulation: it conflicts with Tk app event handling.
(
    cd "$MAC_SOURCE_STAGE"
    "$MAC_VENV_PYTHON" -m PyInstaller \
        --noconfirm --clean --onedir --windowed \
        --name TaxZeroMac --target-arch arm64 \
        --osx-bundle-identifier com.taxzero.maccompanion \
        --distpath "$PROJECT_ROOT/build-dist-mac" \
        --workpath "$PROJECT_ROOT/build-work-mac/pyinstaller" \
        --specpath "$PROJECT_ROOT/build-work-mac/specs" \
        --exclude-module cryptography \
        --exclude-module pywinauto --exclude-module playwright \
        --exclude-module onnxruntime --exclude-module rapidocr_onnxruntime \
        --exclude-module bridge_host --exclude-module app \
        --exclude-module wechat --exclude-module wxauto4 \
        --hidden-import tkinter --hidden-import tkinter.ttk \
        --hidden-import tkinter.messagebox --hidden-import tkinter.filedialog \
        --hidden-import ssl --hidden-import _ssl \
        mac_app.py
)

"$MAC_VENV_PYTHON" - <<'PY'
import plistlib
import shutil
from pathlib import Path
source = Path("build-dist-mac/TaxZeroMac.app")
target = Path("build-dist-mac/个税一键申报 Mac.app")
if target.exists():
    shutil.rmtree(target)
source.rename(target)
plist_path = target / "Contents/Info.plist"
with plist_path.open("rb") as stream:
    info = plistlib.load(stream)
info.update({
    "CFBundleName": "个税一键申报 Mac",
    "CFBundleDisplayName": "个税一键申报 Mac",
    "CFBundleShortVersionString": "1.0.0",
    "CFBundleVersion": "2026.10.19",
    "LSMinimumSystemVersion": "15.0",
    "NSHighResolutionCapable": True,
    "NSLocalNetworkUsageDescription": "连接您配对的 Windows 电脑，查看并启动其申报任务。",
})
with plist_path.open("wb") as stream:
    plistlib.dump(info, stream, sort_keys=True)
PY

# Info.plist changed after freezing, so reseal the bundle using ad-hoc signing.
/usr/bin/codesign --force --deep --sign - 'build-dist-mac/个税一键申报 Mac.app'
/usr/bin/codesign --verify --deep --strict --verbose=2 'build-dist-mac/个税一键申报 Mac.app'
"$MAC_VENV_PYTHON" tools/mac_smoke.py
echo 'Native Mac build and relocated offline UI self-test completed.'
