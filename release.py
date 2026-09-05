#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""发布 SwiftDM：创建/复用 GitHub Release 并上传二进制构建产物。

幂等：若 tag 已存在则复用；若同名资源已存在则先删除再上传。

跨平台：根据当前系统选择对应的构建产物与资产名。

用法：
    python release.py                 # 上传当前平台的产物
    python release.py --target win    # 上传 Windows 产物 (.exe)
    python release.py --target mac    # 上传 macOS 产物 (.app 打包为 zip)
    python release.py --target linux  # 上传 Linux 产物 (ELF 可执行)
    python release.py --body-only     # 仅同步 Release 说明文案，不上传产物
"""
import argparse
import os
import platform
import re
import shutil
import sys
import tempfile
import zipfile

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = "cpufreestyle/SwiftDM"
TAG = "v0.1.0"


def _detect_target():
    s = platform.system().lower()
    if s == "windows":
        return "win"
    if s == "darwin":
        return "mac"
    return "linux"


def _asset_for(target):
    """返回 (资产名, 本地产物路径)。macOS 的 .app 会打包为 zip。"""
    dist = os.path.join(HERE, "dist")
    if target == "win":
        name = "SwiftDM-v0.1.0-windows.exe"
        path = os.path.join(dist, "SwiftDM.exe")
        return name, path, False
    if target == "mac":
        name = "SwiftDM-v0.1.0-macos.zip"
        path = os.path.join(dist, "SwiftDM.app")
        return name, path, True
    # linux
    name = "SwiftDM-v0.1.0-linux"
    path = os.path.join(dist, "SwiftDM")
    return name, path, False


def _prepare_asset(name, path, is_app):
    """若是 .app 目录，则打包为 zip 并返回 zip 路径；否则原样返回。"""
    if is_app and os.path.isdir(path):
        tmp = os.path.join(tempfile.gettempdir(), name)
        if os.path.isfile(tmp):
            os.remove(tmp)
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            # 仅打包 SwiftDM.app 顶层，保持包结构
            root = os.path.dirname(path)
            base = os.path.basename(path)
            for dirpath, _, filenames in os.walk(path):
                for f in filenames:
                    full = os.path.join(dirpath, f)
                    arc = os.path.relpath(full, root)
                    z.write(full, arc)
        return tmp
    return path


CRED_PATH = os.path.join(os.path.expanduser("~"), ".git-credentials")

RELEASE_BODY = """\
# SwiftDM v0.1.0

IDM 风格的高速多线程下载管理器（桌面端 + Web 端），支持 **Windows / macOS / Linux**。

## 下载

| 平台 | 资源 |
| --- | --- |
| Windows | `SwiftDM-v0.1.0-windows.exe` |
| macOS | `SwiftDM-v0.1.0-macos.zip` |
| Linux | `SwiftDM-v0.1.0-linux` |

下载后直接运行即可，无需安装 Python 环境。

## 功能

- **多线程分段下载**：默认 8 线程并行，可自定义 1-32 线程（服务器不支持 Range 时
  自动降级为单线程，避免文件损坏）
- **BT / PT 下载**：支持磁力链接（`magnet:`）与 `.torrent` 种子；任务卡片实时显示
  做种数 / peer 数，下载完成后自动做种；私有种子自动禁用 DHT / PEX / LSD，符合 PT 站点规则
- **断点续传**：随时暂停 / 继续，恢复时从断点续下；分段断流自动重试
- **完整性校验**：合并前校验每个分段与最终文件大小，杜绝静默截断
- **浏览器扩展**：Chrome 扩展自动捕获浏览器中的下载
- **剪贴板监听**：复制下载链接自动检测
- **原生桌面 UI**：基于 PyQt6 的暗色主题界面，IDM 风格任务卡片
- **实时进度 / 完成通知 / 批量管理**

## 其它

- 关闭窗口会最小化到系统托盘（macOS 为菜单栏），下载任务继续在后台运行。
- 无界面 / 服务器环境可使用纯 Web 模式：`SwiftDM --web-only`（或 `./start.sh --web`）。
- 浏览器扩展位于源码 `extension/` 目录，需在 `chrome://extensions/` 中
  「加载已解压的扩展程序」手动安装。
"""


def get_token():
    if not os.path.isfile(CRED_PATH):
        print(f">>> 未找到凭据文件：{CRED_PATH}", file=sys.stderr)
        sys.exit(1)
    with open(CRED_PATH, "r", encoding="utf-8") as f:
        text = f.read()
    # 凭据形如 https://<token>@github.com
    m = re.search(r"([A-Za-z0-9_-]+)@github\.com", text)
    if not m:
        print(">>> 未能从凭据中解析出 GitHub token", file=sys.stderr)
        sys.exit(1)
    return m.group(1)


def _sync_body(release, headers, proxies):
    """说明文案会随功能迭代变化：与远端不一致时 PATCH 更新。

    Release 一旦创建，后续再发布只会「复用」它，正文便停留在创建时的旧文案；
    这里补上同步，避免新增功能后 Release 页面仍写着过时的说明。
    只提交 body 字段，不动 tag_name / name / draft / prerelease。
    """
    if release.get("body", "") == RELEASE_BODY:
        print(">>> 说明文案已是最新，跳过更新")
        return release

    print(">>> 更新 release 说明文案 ...")
    pr = requests.patch(
        f"https://api.github.com/repos/{REPO}/releases/{release['id']}",
        headers=headers,
        proxies=proxies,
        timeout=30,
        json={"body": RELEASE_BODY},
    )
    if pr.status_code != 200:
        print(f">>> 更新说明文案失败: {pr.status_code} {pr.text}", file=sys.stderr)
        return release
    print(">>> 说明文案已更新")
    return pr.json()


def main():
    ap = argparse.ArgumentParser(description="发布 SwiftDM 到 GitHub Release")
    ap.add_argument("--target", default=None, choices=("win", "mac", "linux"),
                    help="指定上传的目标平台产物（默认按当前平台）")
    ap.add_argument("--body-only", action="store_true",
                    help="仅同步 Release 说明文案，不上传二进制产物")
    args = ap.parse_args()
    target = args.target or _detect_target()

    ASSET_NAME, ASSET_SRC, IS_APP = _asset_for(target)

    token = get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    # 直连 GitHub，不经过本地代理
    proxies = {"http": None, "https": None}

    # 1) 获取或创建 release
    url = f"https://api.github.com/repos/{REPO}/releases/tags/{TAG}"
    r = requests.get(url, headers=headers, proxies=proxies, timeout=30)
    if r.status_code == 200:
        release = r.json()
        print(f">>> 复用已存在的 release: {release['html_url']}")
        release = _sync_body(release, headers, proxies)
    elif r.status_code == 404:
        print(">>> 创建新 release ...")
        cr = requests.post(
            f"https://api.github.com/repos/{REPO}/releases",
            headers=headers,
            proxies=proxies,
            timeout=30,
            json={
                "tag_name": TAG,
                "name": f"SwiftDM {TAG}",
                "body": RELEASE_BODY,
                "draft": False,
                "prerelease": False,
            },
        )
        if cr.status_code not in (200, 201):
            print(f">>> 创建 release 失败: {cr.status_code} {cr.text}", file=sys.stderr)
            sys.exit(1)
        release = cr.json()
        print(f">>> 已创建 release: {release['html_url']}")
    else:
        print(f">>> 查询 release 失败: {r.status_code} {r.text}", file=sys.stderr)
        sys.exit(1)

    if args.body_only:
        print(">>> --body-only：已同步说明文案，跳过二进制上传")
        return

    if not os.path.exists(ASSET_SRC):
        print(f">>> 未找到二进制产物：{ASSET_SRC}，请先执行 python build_exe.py --target {target}",
              file=sys.stderr)
        sys.exit(1)
    ASSET_PATH = _prepare_asset(ASSET_NAME, ASSET_SRC, IS_APP)

    upload_url = release["upload_url"].replace("{?name,label}", "")
    asset_url = release["assets_url"]

    # 2) 删除同名旧资源
    existing = requests.get(asset_url, headers=headers, proxies=proxies, timeout=30).json()
    for a in existing:
        if a["name"] == ASSET_NAME:
            print(f">>> 删除旧资源: {ASSET_NAME}")
            requests.delete(a["url"], headers=headers, proxies=proxies, timeout=30)

    # 3) 上传新资源
    size_mb = os.path.getsize(ASSET_PATH) / (1024 * 1024)
    print(f">>> 上传二进制产物 {ASSET_NAME} ({size_mb:.1f} MB) ...")
    with open(ASSET_PATH, "rb") as f:
        ur = requests.post(
            f"{upload_url}?name={ASSET_NAME}",
            headers={**headers, "Content-Type": "application/octet-stream"},
            proxies=proxies,
            data=f,
            timeout=600,
        )
    if ur.status_code not in (200, 201):
        print(f">>> 上传失败: {ur.status_code} {ur.text}", file=sys.stderr)
        sys.exit(1)
    print(f">>> 上传成功: {ur.json()['browser_download_url']}")


if __name__ == "__main__":
    main()
