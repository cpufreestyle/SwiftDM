#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 extension/ 校验、修图标并打包成 Chrome 扩展 CRX。

用法：
    python build_crx.py --check      # 只校验，不打包（不需要 Chrome）
    python build_crx.py              # 校验 + 重画图标 + 打包
    python build_crx.py --no-icons   # 跳过重画图标
    python build_crx.py --out dist   # 输出目录（默认 dist）

产物（都在 gitignore 的 dist/ 下，.pem 不要提交）：
    dist/swiftdm-extension.crx       扩展包
    dist/swiftdm-extension.pem       签名私钥——换 key 就等于换扩展 ID

两个事实决定了这个脚本的做法：
1. CRX3 的签名覆盖范围是 Chrome 内部约定，自己拼字节无法保证被 Chrome 接受，
   所以直接调 Chrome 自带的打包器（--pack-extension / --pack-extension-key）。
   实测：同 key 同内容逐字节可复现，产物可以稳定分发。
2. 拖拽安装 CRX 与命令行 --load-extension 在现代 Chrome 上均已失效
   （Chrome 154 实测：连只有 manifest.json 的极简扩展都不加载）。
   所以本脚本不承诺「免手动加载」；CRX 的用途是企业策略安装
   （ExtensionInstallForcelist + update_url）与分发。

另外：extension/icons/*.png 曾损坏——文件头 12 字节是字面文本 "\x89PNG\r\n\x1a\n"。
     Chrome 能容忍，但打包前统一重画成真 PNG，并让扩展图标与 EXE 图标同源。
"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
EXT_DIR = os.path.join(HERE, "extension")
DEFAULT_OUT = os.path.join(HERE, "dist")
CRX_NAME = "swiftdm-extension.crx"
KEY_NAME = "swiftdm-extension.pem"
ICON_SIZES = (16, 48, 128)

CRX_MAGIC = b"Cr24"
CRX_VERSION = 3
# DigestInfo 前缀：SHA-256 with RSA Encryption（OID 1.2.840.113549.1.1.11）
SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")

# 固定时间戳：git checkout 的时间每台机器都不一样，原样打进 zip 会让产物不可复现。
STAMP = 1704067200  # 2024-01-01 00:00:00 UTC

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]

# --------------------------------------------------------------------------- 图标
def draw_app_icon(size):
    """画应用图标：圆角蓝底 + 白色下载箭头。

    固定按 256 画再缩到目标尺寸——build_exe.py 的 icon.ico 用的就是这张，
    两处图标从此同源，扩展工具栏上看到的和任务栏里是同一个样子。
    """
    from PIL import Image, ImageDraw

    s = 256
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([16, 16, s - 16, s - 16], radius=48, fill=(45, 120, 245, 255))
    white = (255, 255, 255, 255)
    cx = s // 2
    d.rectangle([cx - 14, 70, cx + 14, 150], fill=white)
    d.polygon([(cx, 196), (cx - 52, 132), (cx + 52, 132)], fill=white)
    d.rounded_rectangle([cx - 64, 206, cx + 64, 226], radius=10, fill=white)
    if size != s:
        img = img.resize((size, size), Image.LANCZOS)
    return img


def icon_png_bytes(size):
    """返回 icon<size>.png 的字节。"""
    with io.BytesIO() as buf:
        draw_app_icon(size).save(buf, format="PNG")
        return buf.getvalue()


def fix_icons():
    """重画扩展三枚图标；幂等——写什么只取决于尺寸。"""
    written = {}
    for size in ICON_SIZES:
        path = os.path.join(EXT_DIR, "icons", "icon%d.png" % size)
        with io.open(path, "wb") as f:
            f.write(icon_png_bytes(size))
        written[size] = path
    return written

HTML_REF = re.compile(r'(?:src|href)="([^"#?]+)"')


def html_references():
    """HTML 里通过 src/href 引用的本地文件（manifest 管不到这亚）。

    popup.js 就是只有 popup.html 引用它——不扫 HTML 会把它误判成多余文件。
    """
    out = []
    for rel, path in extension_files().items():
        if not rel.endswith(".html"):
            continue
        with io.open(path, encoding="utf-8", errors="replace") as f:
            for m in HTML_REF.finditer(f.read()):
                out.append(m.group(1).lstrip("./"))
    return out


# --------------------------------------------------------------------------- 校验
def png_size(data):
    """是真 PNG 就返回 (宽, 高），否则 None。"""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


def extension_files(root=None):
    """扩展目录里所有文件的 {相对路径: 绝对路径}（按路径排序，稳定输出）。"""
    root = root or EXT_DIR
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            abspath = os.path.join(dirpath, name)
            rel = os.path.relpath(abspath, root).replace(os.sep, "/")
            out[rel] = abspath
    return dict(sorted(out.items()))


def manifest_references(manifest):
    """manifest 里被引用的相对路径列表。"""
    refs = []
    for rel in ((manifest.get("icons") or {})).values():
        refs.append(rel)
    worker = (manifest.get("background") or {}).get("service_worker")
    if worker:
        refs.append(worker)
    if (manifest.get("action") or {}).get("default_popup"):
        refs.append(manifest["action"]["default_popup"])
    for cs in manifest.get("content_scripts") or []:
        refs.extend(cs.get("js") or [])
        refs.extend(cs.get("css") or [])
    return [r for r in refs if r]


def validate_extension():
    """返回问题列表；空列表表示通过。"""
    problems = []
    manifest_path = os.path.join(EXT_DIR, "manifest.json")
    if not os.path.isfile(manifest_path):
        return ["缺少 manifest.json"]
    try:
        with io.open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception as exc:
        return ["manifest.json 解析失败: %s" % exc]

    for field in ("manifest_version", "name", "version"):
        if not manifest.get(field):
            problems.append("manifest 缺少 %s" % field)

    for rel in manifest_references(manifest):
        if not os.path.isfile(os.path.join(EXT_DIR, rel)):
            problems.append("manifest 引用的文件不存在: %s" % rel)

    for size, rel in (manifest.get("icons") or {}).items():
        path = os.path.join(EXT_DIR, rel)
        if not os.path.isfile(path):
            continue  # 上面已经记过
        with io.open(path, "rb") as f:
            data = f.read()
        real = png_size(data)
        if real is None:
            problems.append("%s 不是有效 PNG" % rel)
        elif real != (int(size), int(size)):
            problems.append("%s 声明 %s px，实际 %dx%d" % (rel, size, real[0], real[1]))

    refs = set(manifest_references(manifest))
    for rel in html_references():
        refs.add(rel)
    for rel in extension_files():
        if rel in ("manifest.json",) or rel in refs or rel.startswith("icons/"):
            continue
        problems.append("目录里有 manifest/HTML 都没引用的文件: %s" % rel)
    return problems

# ------------------------------------------------------------------------ CRX 解析
def read_varint(buf, pos):
    """读 protobuf varint，返回 (值, 新位置)。"""
    shift = 0
    value = 0
    while True:
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def read_crx(path):
    """解析 CRX3，返回结构 dict；结构对不上直接 ValueError。

    布局（Chrome 打包器实测）：
        "Cr24" | version(3) | header_len | header | zip
        header = CrxFileHeader{ key_proofs=[{public_key, signature}] } + 尾部 SignedData
    尾部那 22 字节里的 crx_id 就是扩展 ID：sha256(公钥 DER) 的前 16 字节。
    """
    with io.open(path, "rb") as f:
        raw = f.read()
    if raw[:4] != CRX_MAGIC:
        raise ValueError("不是 CRX：缺少 Cr24 magic")
    version, header_len = struct.unpack("<II", raw[4:12])
    if version != CRX_VERSION:
        raise ValueError("只支持 CRX %d，实际 %d" % (CRX_VERSION, version))
    header = raw[12:12 + header_len]
    zip_bytes = raw[12 + header_len:]
    if zip_bytes[:4] != b"PK\x03\x04":
        raise ValueError("header 之后不是 zip 数据")

    if header[0] != 0x12:
        raise ValueError("header 不是 CrxFileHeader（field 2）")
    proof_len, pos = read_varint(header, 1)
    proof = header[pos:pos + proof_len]
    if len(proof) != proof_len:
        raise ValueError("key_proofs 长度越界")
    if proof[0] != 0x0A:
        raise ValueError("key_proofs 里没有 public_key（field 1）")
    key_len, p = read_varint(proof, 1)
    spki = proof[p:p + key_len]
    if len(spki) != key_len:
        raise ValueError("公钥长度越界")
    p += key_len
    if proof[p] != 0x12:
        raise ValueError("key_proofs 里没有 signature（field 2）")
    sig_len, p = read_varint(proof, p + 1)
    signature = proof[p:p + sig_len]
    if len(signature) != sig_len:
        raise ValueError("签名长度越界")

    crx_id = hashlib.sha256(spki).digest()[:16]
    if crx_id not in header:
        raise ValueError("header 尾部找不到 crx_id（扩展 ID 与公钥不匹配？）")
    return {
        "version": version,
        "header_len": header_len,
        "spki": spki,
        "signature": signature,
        "crx_id": crx_id,
        "id": crx_id.hex(),
        "zip": zip_bytes,
        "size": len(raw),
    }


def signature_block_is_wellformed(info):
    """签名块是否是「该公钥 + PKCS#1 v1.5 + SHA-256」签出来的（只看结构）。

    签名覆盖的具体字节是 Chrome 内部约定，这里不验消息，只验解开后的
    DigestInfo 前缀与长度——足够拦住「塞了个假签名」的包。
    """
    from cryptography.hazmat.primitives import serialization

    try:
        pub = serialization.load_der_public_key(info["spki"])
    except Exception:
        return False
    numbers = pub.public_numbers()
    k = (numbers.n.bit_length() + 7) // 8
    if k != len(info["signature"]):
        return False
    try:
        value = int.from_bytes(info["signature"], "big")
        if value >= numbers.n:
            return False
        block = pow(value, numbers.e, numbers.n).to_bytes(k, "big")
    except Exception:
        return False
    if not block.startswith(b"\x00\x01"):
        return False
    pad_end = block.find(b"\x00", 2)
    if pad_end < 0 or block[2:pad_end] != b"\xff" * (pad_end - 2):
        return False
    digest_info = block[pad_end + 1:]
    return (len(digest_info) == 51
            and digest_info.startswith(SHA256_DIGEST_INFO))


def zip_entries(zip_bytes):
    """zip 里的 {相对路径: 字节}（跳过目录项）。"""
    out = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            out[name] = zf.read(name)
    return out


def verify_crx(crx_path, ext_dir=None):
    """打包后自检：结构、签名块、zip 内容与扩展目录一致。返回问题列表。"""
    ext_dir = ext_dir or EXT_DIR
    problems = []
    info = read_crx(crx_path)
    if not signature_block_is_wellformed(info):
        problems.append("签名块不是该公钥的 PKCS#1 v1.5 / SHA-256 签名")
    packed = zip_entries(info["zip"])
    on_disk = {}
    for rel, path in extension_files(ext_dir).items():
        with io.open(path, "rb") as f:
            on_disk[rel] = f.read()
    for rel in sorted(set(packed) | set(on_disk)):
        if rel not in packed:
            problems.append("扩展目录里有、包里没有: %s" % rel)
        elif rel not in on_disk:
            problems.append("包里有、扩展目录里没有: %s" % rel)
        elif packed[rel] != on_disk[rel]:
            problems.append("内容不一致: %s" % rel)
    return problems, info

# --------------------------------------------------------------------------- 打包
def find_chrome():
    """返回 Chrome 可执行路径；没装返回 None。"""
    for candidate in CHROME_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    found = shutil.which("google-chrome") or shutil.which("chrome")
    return found


def ensure_key(key_path):
    """私钥不存在就生成一把（2048 位 RSA，PKCS#8 PEM）。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    if os.path.isfile(key_path):
        return False
    os.makedirs(os.path.dirname(key_path), exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption())
    with io.open(key_path, "wb") as f:
        f.write(pem)
    return True


def _stamp_tree(root):
    """把整检项的时间戳同一到 STAMP，zip 里才没有本机信息。"""
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            os.utime(os.path.join(dirpath, name), (STAMP, STAMP))


def pack_with_chrome(key_path, out_path):
    """把扩展目录暂存一份交给 Chrome 打包器，再把 crx 搬到 out_path。

    暂存是必须的：私钥不能落在扩展目录里（否则会被打进包），
    Chrome 也会把 <目录>.crx / <目录>.pem 写在源目录旁边。
    """
    chrome = find_chrome()
    if chrome is None:
        raise RuntimeError("找不到 Chrome；打包 CRX 需要本机安装 Chrome。")
    tmp = tempfile.mkdtemp(prefix="swiftdm-crx-")
    try:
        stage = os.path.join(tmp, "swiftdm-ext")
        shutil.copytree(EXT_DIR, stage)
        _stamp_tree(stage)
        # 私钥若被谁误放进扩展目录，这里挡一道：绝不让它进包。
        for bad in (KEY_NAME, "extension.pem"):
            stray = os.path.join(stage, bad)
            if os.path.isfile(stray):
                os.remove(stray)
        subprocess.run(
            [chrome, "--pack-extension=" + stage, "--pack-extension-key=" + key_path,
             "--no-sandbox", "--user-data-dir=" + os.path.join(tmp, "profile")],
            capture_output=True, timeout=180)
        produced = stage + ".crx"
        if not os.path.isfile(produced):
            raise RuntimeError("Chrome 没有产出 CRX（退出码 %d）" % 0)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        shutil.copyfile(produced, out_path)
        return out_path
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="校验并打包 SwiftDM 浏览器扩展")
    parser.add_argument("--check", action="store_true",
                        help="只校验，不改扩展目录也不打包（不需要 Chrome）")
    parser.add_argument("--no-icons", action="store_true", help="不重画图标")
    parser.add_argument("--out", default=DEFAULT_OUT, help="输出目录（默认 dist）")
    args = parser.parse_args(argv)

    if args.check:
        # 只读：图标坏了就报错，让 CI 能只靠它报红。
        problems = validate_extension()
        for problem in problems:
            print("    - %s" % problem)
        print("校验%s：%s" % ("未通过" if problems else "通过", EXT_DIR))
        return 1 if problems else 0

    # 构建：图标先重画再校验——旧图标夹头损坏过，不重画校验永远红。
    if not args.no_icons:
        for size, path in sorted(fix_icons().items()):
            print("图标已重画：%s (%dpx)" % (os.path.relpath(path, HERE), size))

    problems = validate_extension()
    if problems:
        print(">>> 扩展校验未通过：")
        for problem in problems:
            print("    - %s" % problem)
        return 1
    print("校验通过：%s" % EXT_DIR)

    key_path = os.path.join(args.out, KEY_NAME)
    ensure_key(key_path)
    crx_path = os.path.join(args.out, CRX_NAME)
    try:
        pack_with_chrome(key_path, crx_path)
    except RuntimeError as exc:
        print(">>> %s" % exc)
        print(">>> 也可以手动：chrome --pack-extension=%s --pack-extension-key=%s"
              % (EXT_DIR, key_path))
        return 2

    problems, info = verify_crx(crx_path)
    if problems:
        print(">>> 打包自检失败：")
        for problem in problems:
            print("    - %s" % problem)
        return 1
    print("已打包：%s" % crx_path)
    print("    扩展 ID %s（由 %s 里的私钥决定，别提交该文件）"
          % (info["id"], key_path))
    print("    %d 字节，zip 内 %d 个文件" % (info["size"], len(zip_entries(info["zip"]))))
    return 0


if __name__ == "__main__":
    sys.exit(main())