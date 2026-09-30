# -*- coding: utf-8 -*-
"""build_crx.py 的守护：扩展目录、图标、CRX3 结构与打包可复现性。

打包那几组需要本机装了 Chrome（build_crx 是直接调 Chrome 自带打包器的），
没装就 skip——校验与图标部分不依赖 Chrome，任何时候都该是绿的。
"""
import io
import json
import os
import struct
import subprocess
import sys

import pytest

import build_crx

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

CHROME = build_crx.find_chrome()
needs_chrome = pytest.mark.skipif(CHROME is None, reason="本机没有 Chrome，跳过打包组")


def _icon(size):
    with io.open(os.path.join(build_crx.EXT_DIR, "icons", "icon%d.png" % size), "rb") as f:
        return f.read()


# --------------------------------------------------------------------- 扩展目录
def test_extension_icons_are_real_pngs_of_the_declared_size():
    """回归：这三个 png 的文件头曾是字面文本 "\x89PNG\r\n\x1a\n"，Chrome 能忍、包里不能有。"""
    for size in (16, 48, 128):
        data = _icon(size)
        assert build_crx.png_size(data) == (size, size), "icon%d.png" % size


def test_on_disk_icons_are_the_ones_the_code_draws():
    """磁盘图标必须就是代码画的那张，否则扩展和 EXE 的图标会慢慢长岔。"""
    for size in (16, 48, 128):
        assert _icon(size) == build_crx.icon_png_bytes(size), "icon%d.png" % size


def test_icon_bytes_are_deterministic():
    """同一尺寸两次画的字节一致——打包可复现的前提之一。"""
    assert build_crx.icon_png_bytes(64) == build_crx.icon_png_bytes(64)


def test_draw_app_icon_draws_a_transparent_canvas_with_a_white_arrow():
    from PIL import Image

    img = build_crx.draw_app_icon(48)
    assert img.size == (48, 48) and img.mode == "RGBA"
    assert img.getpixel((2, 2))[3] == 0            # 四角透明（圆角留出来的）
    px = img.getpixel((24, 24))                    # 中心是箭头的白杆
    assert px[3] == 255 and min(px[:3]) >= 240    # 抗锯齿后不会正好 255


def test_validate_extension_accepts_the_committed_tree():
    assert build_crx.validate_extension() == []


def test_manifest_and_html_explain_every_file_in_the_tree():
    """目录里不该有谁都引不到的文件，否则会被默默打进包里。"""
    with io.open(os.path.join(build_crx.EXT_DIR, "manifest.json"), encoding="utf-8") as f:
        refs = set(build_crx.manifest_references(json.load(f)))
    refs |= set(build_crx.html_references())
    for rel in build_crx.extension_files():
        assert rel in refs or rel == "manifest.json" or rel.startswith("icons/"), rel


def test_validate_extension_flags_a_missing_referenced_file(tmp_path):
    """manifest 引用的文件没了必须报错（而不是悄悄打个缺文件的包）。"""
    manifest = {"manifest_version": 3, "name": "x", "version": "1.0",
                "background": {"service_worker": "background.js"}}
    staged = tmp_path / "staged"
    staged.mkdir()
    with io.open(staged / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f)
    # validate_extension 走 EXT_DIR，换成临时副本再跑
    saved = build_crx.EXT_DIR
    try:
        build_crx.EXT_DIR = str(staged)
        problems = build_crx.validate_extension()
    finally:
        build_crx.EXT_DIR = saved
    assert problems == ["manifest 引用的文件不存在: background.js"]


def test_png_size_rejects_the_old_corrupt_icons():
    """把当年那个坏文件头喂进去：必须判负。"""
    assert build_crx.png_size(b'\\x89PNG\\r\\n\\x1a\\n' + b"\\x00" * 40) is None
    assert build_crx.png_size(b"not a png at all!!") is None

# ------------------------------------------------------------------------ 打包
def _pack(tmp_path, name, key_name="k.pem"):
    key = str(tmp_path / key_name)
    out = str(tmp_path / name)
    build_crx.ensure_key(key)
    build_crx.pack_with_chrome(key, out)
    return key, out


@needs_chrome
def test_ensure_key_reuses_an_existing_key(tmp_path):
    """第二次调用不换 key——换了就等于换了扩展 ID。"""
    key = str(tmp_path / "k.pem")
    assert build_crx.ensure_key(key) is True
    first = io.open(key, "rb").read()
    assert build_crx.ensure_key(key) is False
    assert io.open(key, "rb").read() == first


@needs_chrome
def test_read_crx_parses_a_crx_that_chrome_itself_produced(tmp_path):
    """解析器要能读 Chrome  Official 打包器的产物（这才算数）。"""
    _key, out = _pack(tmp_path, "chrome.crx")
    info = build_crx.read_crx(out)
    assert info["version"] == build_crx.CRX_VERSION
    assert info["zip"][:4] == b"PK\x03\x04"
    assert len(info["crx_id"]) == 16
    assert struct.unpack("<I", io.open(out, "rb").read()[4:8])[0] == build_crx.CRX_VERSION
    assert build_crx.signature_block_is_wellformed(info), "签名块不像 RSA/SHA-256"


@needs_chrome
def test_pack_is_byte_for_byte_reproducible(tmp_path):
    """同 key 同内容产物必须逐字节一致，否则没法稳定分发。"""
    _k1, a = _pack(tmp_path, "a.crx", "k1.pem")
    _k2, b = _pack(tmp_path, "b.crx", "k1.pem")
    assert io.open(a, "rb").read() == io.open(b, "rb").read()
    assert _k1 == _k2


@needs_chrome
def test_crx_contains_exactly_the_extension_tree(tmp_path):
    """包里必须正好是扩展目录那 9 个文件，一个不多一个不少。"""
    _key, out = _pack(tmp_path, "tree.crx")
    entries = build_crx.zip_entries(build_crx.read_crx(out)["zip"])
    assert set(entries) == set(build_crx.extension_files())
    for rel, data in entries.items():
        with io.open(build_crx.extension_files()[rel], "rb") as f:
            assert data == f.read(), rel


@needs_chrome
def test_private_key_never_lands_inside_the_crx(tmp_path):
    """私钥进了包就等于把扩展 ID 送给所有人。"""
    key, out = _pack(tmp_path, "safe.crx")
    entries = build_crx.zip_entries(build_crx.read_crx(out)["zip"])
    assert not [n for n in entries if n.endswith(".pem")]
    for name, data in entries.items():
        assert b"PRIVATE KEY" not in data, name


@needs_chrome
def test_verify_crx_passes_on_a_fresh_pack(tmp_path):
    _key, out = _pack(tmp_path, "verify.crx")
    problems, info = build_crx.verify_crx(out)
    assert problems == []
    assert info["size"] > 0


def test_read_crx_rejects_non_crx_bytes(tmp_path):
    junk = tmp_path / "junk.crx"
    junk.write_bytes(b"NOTCRX" + b"\x03\x00\x00\x00" + b"\x00" * 32)
    with pytest.raises(ValueError):
        build_crx.read_crx(str(junk))


def test_read_crx_rejects_old_versions(tmp_path):
    body = struct.pack("<II", 2, 8) + b"\x12\x06" + b"\x00" * 6 + b"PK\x03\x04"
    old = tmp_path / "v2.crx"
    old.write_bytes(b"Cr24" + body)
    with pytest.raises(ValueError, match="只支持 CRX 3"):
        build_crx.read_crx(str(old))


def test_cli_check_is_read_only_and_passes():
    """--check 不许改扩展目录（否则 CI 拿它当守卫时会顺带动工作区）。"""
    before = {rel: io.open(p, "rb").read()
              for rel, p in build_crx.extension_files().items()}
    stamps = {rel: os.stat(p).st_mtime for rel, p in build_crx.extension_files().items()}
    proc = subprocess.run([sys.executable, os.path.join(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))), "build_crx.py"), "--check"],
        capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    for rel, data in before.items():
        with io.open(build_crx.extension_files()[rel], "rb") as f:
            assert f.read() == data, rel
    assert {rel: os.stat(p).st_mtime
            for rel, p in build_crx.extension_files().items()} == stamps