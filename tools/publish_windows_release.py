"""Publish the already-built Windows 1.1.1 package, without rebuilding it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile


REPOSITORY = "Han050802/geshui-yijian-shenbao"
SOURCE_COMMIT = "4d74103a24b2bda57edcbb40acf53bdda5ca3441"
BUILD_RUN = 39244977202
ARTIFACT_ID = 12182628924
TAG = "v1.1.1"
ORIGINAL_NAME = "个税一键申报_Windows完整运行包_Excel联动版1.1.1_2026-10-19.zip"
ASSET_NAME = "TaxZero-Windows-Full-1.1.1.zip"
PACKAGE_SIZE = 458556083
PACKAGE_SHA256 = "7b7e42d4fa4e81e09423dc78600870a12c0decf8e73fbd993a0174e53b3b213d"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def verify_package(directory: Path) -> Path:
    package = directory / ORIGINAL_NAME
    if package.stat().st_size != PACKAGE_SIZE or digest(package) != PACKAGE_SHA256:
        raise RuntimeError("Package does not match the delivered Windows ZIP")
    info = json.loads((directory / "package-info.json").read_text(encoding="utf-8"))
    if (info.get("source_commit") != SOURCE_COMMIT or info.get("filename") != ORIGINAL_NAME
            or info.get("sha256") != PACKAGE_SHA256 or info.get("size_bytes") != PACKAGE_SIZE):
        raise RuntimeError("Package metadata does not match the pinned build")
    with zipfile.ZipFile(package) as archive:
        prefix = "个税一键申报/"
        build = json.loads(archive.read(prefix + "build-info.json"))
        smoke = json.loads(archive.read(prefix + "build-reports/smoke-report.json"))
        if (build.get("source_commit") != SOURCE_COMMIT or smoke.get("source_commit") != SOURCE_COMMIT
                or smoke.get("status") != "passed"):
            raise RuntimeError("Missing matching successful runtime verification")
        for name in ("runtime/TaxZero.exe", "runtime-wechat/WeChatReceipt.exe", "一键启动.cmd"):
            if archive.getinfo(prefix + name).file_size <= 0:
                raise RuntimeError("Missing runtime entry point: " + name)
    return package


def api(endpoint: str, *, method: str = "GET", data=None, allow_missing=False):
    request = Request(
        f"https://api.github.com/repos/{REPOSITORY}/{endpoint}",
        data=None if data is None else json.dumps(data).encode("utf-8"),
        headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"],
                 "Accept": "application/vnd.github+json", "Content-Type": "application/json",
                 "User-Agent": "TaxZero-verified-release", "X-GitHub-Api-Version": "2022-11-28"},
        method=method,
    )
    try:
        with urlopen(request, timeout=60) as response:
            return json.load(response)
    except HTTPError as exc:
        if allow_missing and exc.code == 404:
            return None
        raise RuntimeError(f"GitHub {method} {endpoint} returned HTTP {exc.code}") from None


def verify_tag(*, required: bool = False) -> None:
    ref = api(f"git/ref/tags/{TAG}", allow_missing=True)
    if ref is None:
        if required:
            raise RuntimeError("Published release is missing its tag")
        return
    target = ref["object"]
    for _ in range(8):
        if target["type"] == "commit":
            if target["sha"] != SOURCE_COMMIT:
                raise RuntimeError("Existing release tag points to different source; not changing it")
            return
        if target["type"] != "tag":
            break
        target = api("git/tags/" + target["sha"])["object"]
    raise RuntimeError("Cannot verify release tag target")


def verify_asset(asset: dict, path: Path) -> None:
    if asset.get("state") != "uploaded" or asset.get("size") != path.stat().st_size:
        raise RuntimeError("Existing asset is incomplete or has different size; not overwriting it")
    expected = "sha256:" + digest(path)
    if asset.get("digest"):
        if asset["digest"] != expected:
            raise RuntimeError("Existing asset has different bytes; not overwriting it")
        return
    # Older GitHub responses can omit digest. Verify a downloaded copy instead.
    with tempfile.TemporaryDirectory(prefix="release-check-") as temporary:
        subprocess.run(["gh", "release", "download", TAG, "--repo", REPOSITORY,
                        "--pattern", path.name, "--dir", temporary], check=True)
        if digest(Path(temporary) / path.name) != digest(path):
            raise RuntimeError("Downloaded release asset differs from the verified package")


def publish(package: Path, staging: Path) -> dict:
    verify_tag()
    notes = (
        "Windows x64 完整运行包 1.1.1，内含 runtime/TaxZero.exe 和 "
        "runtime-wechat/WeChatReceipt.exe，无需另外安装 Python。\n\n"
        f"请下载 **{ASSET_NAME}**（约459 MB），完整解压后进入“个税一键申报”文件夹，"
        "双击“一键启动.cmd”。GitHub 自动生成的 Source code 附件不含运行组件。\n\n"
        "此附件与已交付的中文文件名压缩包字节完全一致，仅下载文件名改为英文。"
        "升级前请备份并迁移原目录最新的 data、settings.json、wechat/config.json；"
        "不要用旧 runtime 或 wechat 源码覆盖新版。\n\n"
        "本版本修复微信与Excel配对、并发登记、发送目标核验及自动发送计时器等问题。"
        "Windows 436项回归和主程序、微信组件、连接服务的打包启动检查通过；"
        "未验证真实微信或税务现场流程。\n\n"
        f"源提交：`{SOURCE_COMMIT}`\n\n"
        f"[构建验证](https://github.com/{REPOSITORY}/actions/runs/{BUILD_RUN})\n\n"
        f"SHA256：`{PACKAGE_SHA256}`\n"
    )
    release = api(f"releases/tags/{TAG}", allow_missing=True)
    if release is None:
        # Draft releases may not be returned by the tag lookup on every server.
        drafts = [r for r in api("releases?per_page=100") if r["tag_name"] == TAG]
        if len(drafts) > 1:
            raise RuntimeError("Multiple releases use the target tag")
        release = drafts[0] if drafts else None
    if release is None:
        release = api("releases", method="POST", data={
            "tag_name": TAG, "target_commitish": SOURCE_COMMIT,
            "name": "1.1.1 — Windows完整运行包（含runtime）",
            "body": notes, "draft": True, "prerelease": False,
        })
    if release["tag_name"] != TAG or release["target_commitish"] != SOURCE_COMMIT:
        raise RuntimeError("Existing release targets different source; not modifying it")

    staged = staging / ASSET_NAME
    shutil.copyfile(package, staged)
    checksum = staging / "SHA256SUMS-Windows-1.1.1.txt"
    checksum.write_text(f"{PACKAGE_SHA256}  {ASSET_NAME}\n", encoding="utf-8")
    for path in (staged, checksum):
        current = api(f"releases/{release['id']}")
        assets = [asset for asset in current["assets"] if asset["name"] == path.name]
        if len(assets) > 1:
            raise RuntimeError("Duplicate release asset names")
        if not assets:
            label = "Windows完整运行包（含runtime）" if path == staged else "Windows文件校验值"
            subprocess.run(["gh", "release", "upload", TAG, str(path) + "#" + label,
                            "--repo", REPOSITORY], check=True)
            assets = [asset for asset in api(f"releases/{release['id']}")["assets"]
                      if asset["name"] == path.name]
        if len(assets) != 1:
            raise RuntimeError("Release upload did not produce one expected asset")
        verify_asset(assets[0], path)
    verify_tag()
    if release["draft"]:
        api(f"releases/{release['id']}", method="PATCH", data={"draft": False})
    published = api(f"releases/{release['id']}")
    if published["draft"]:
        raise RuntimeError("Release was not published")
    verify_tag(required=True)
    return published


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", type=Path, help="Verify local artifact bytes without network or publication")
    arguments = parser.parse_args()
    if arguments.verify_only:
        package = verify_package(arguments.verify_only)
        print(json.dumps({"verified": True, "size": package.stat().st_size, "sha256": digest(package)}))
        return
    run = api(f"actions/runs/{BUILD_RUN}")
    artifact = api(f"actions/artifacts/{ARTIFACT_ID}")
    if (run["head_sha"] != SOURCE_COMMIT or run["status"] != "completed" or run["conclusion"] != "success"
            or artifact["id"] != ARTIFACT_ID or artifact["name"] != "windows-companion" or artifact["expired"]
            or artifact["workflow_run"]["id"] != BUILD_RUN or artifact["workflow_run"]["head_sha"] != SOURCE_COMMIT):
        raise RuntimeError("Pinned successful build artifact is unavailable or changed")
    with tempfile.TemporaryDirectory(prefix="taxzero-publish-") as temporary:
        root = Path(temporary)
        source = root / "source"
        subprocess.run(["gh", "run", "download", str(BUILD_RUN), "--repo", REPOSITORY,
                        "--name", "windows-companion", "--dir", str(source)], check=True)
        package = verify_package(source)
        release = publish(package, root)
        print(json.dumps({"status": "published", "url": release["html_url"], "assets": [
            {key: asset.get(key) for key in ("name", "size", "digest", "browser_download_url")}
            for asset in release["assets"]]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
