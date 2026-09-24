import shutil

import pytest

import media


@pytest.mark.parametrize("url,expected", [
    ("https://c/v/index.m3u8?sign=1", "hls"),
    ("https://c/v/INDEX.MPD", "dash"),
    ("https://c/v/movie.mp4", "http"),
    ("https://c/v/blob-ish", "http"),
    ("magnet:?xt=urn:btih:abc", "http"),          # 磁力由 DownloadManager 先拦截，classify 不报错即可
])
def test_classify_kind_auto_by_url_feature(url, expected):
    assert media.classify_kind(url, "auto") == expected


def test_classify_kind_auto_never_guesses_video_page():
    # 任意网页地址不能自动当解析页——必须显式 kind=video_page
    assert media.classify_kind("https://site/watch?v=1", "auto") == "http"


@pytest.mark.parametrize("explicit", ["hls", "dash", "video_page", "http"])
def test_explicit_kind_wins(explicit):
    assert media.classify_kind("https://site/whatever", explicit) == explicit


def test_unknown_kind_raises_invalid_url():
    with pytest.raises(media.MediaError) as e:
        media.classify_kind("https://site/a", "flash")
    assert e.value.reason == "invalid_kind"


def test_ffmpeg_status_uses_path_and_env(monkeypatch, tmp_path):
    fake = tmp_path / "ffmpeg.exe"
    fake.write_bytes(b"x")
    monkeypatch.delenv("SWIFTDM_FFMPEG", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: str(fake) if name == "ffmpeg" else None)
    assert media.ffmpeg_status() == {"available": True, "path": str(fake)}
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert media.ffmpeg_status() == {"available": False, "path": None}
    monkeypatch.setenv("SWIFTDM_FFMPEG", str(fake))
    assert media.ffmpeg_status()["available"] is True
    monkeypatch.setenv("SWIFTDM_FFMPEG", str(tmp_path / "missing.exe"))
    monkeypatch.setattr(shutil, "which", lambda name: str(fake))
    assert media.ffmpeg_status() == {"available": True, "path": str(fake)}   # 覆盖路径无效时回落 PATH


def test_ytdlp_missing_raises_media_error(monkeypatch):
    media.set_ytdlp_module(None)
    if media.ytdlp_available():
        pytest.skip("本机已安装 yt-dlp，无法验证缺失分支")
    with pytest.raises(media.MediaError) as e:
        media._ytdlp()
    assert e.value.reason == "needs_ytdlp"


def test_injected_module_is_used(monkeypatch):
    sentinel = object()
    media.set_ytdlp_module(sentinel)
    assert media._ytdlp() is sentinel
    assert media.ytdlp_available() is True
    media.set_ytdlp_module(None)
