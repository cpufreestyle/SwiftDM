import os

import pytest

import downloader

SYS = {"http": "127.0.0.1:7897", "https": "127.0.0.1:7897"}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    downloader._restore_proxy_env()
    for k in downloader._PROXY_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(downloader, "_windows_system_proxy", lambda: SYS)
    yield
    downloader._restore_proxy_env()
    downloader._apply_proxy_mode("env")


def test_env_mode_exports_system_proxy_for_ytdlp():
    downloader._apply_proxy_mode("env")
    assert os.environ["HTTP_PROXY"] == "127.0.0.1:7897"
    assert os.environ["HTTPS_PROXY"] == "127.0.0.1:7897"
    assert downloader._SESSION.proxies.get("http") == "127.0.0.1:7897"


def test_env_mode_keeps_explicit_env_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://10.0.0.9:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.9:3128")
    downloader._apply_proxy_mode("env")
    assert os.environ["HTTP_PROXY"] == "http://10.0.0.9:3128"
    assert downloader._SESSION.proxies.get("http") is None   # 走 trust_env


def test_env_mode_without_any_proxy_clears_keys(monkeypatch):
    monkeypatch.setattr(downloader, "_windows_system_proxy", lambda: None)
    downloader._apply_proxy_mode("env")
    for k in downloader._PROXY_ENV_KEYS:
        assert k not in os.environ


def test_direct_mode_strips_proxy_env(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://10.0.0.9:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.9:3128")
    downloader._apply_proxy_mode("direct")
    for k in downloader._PROXY_ENV_KEYS:
        assert k not in os.environ
    assert downloader._SESSION.trust_env is False


def test_custom_mode_exports_proxy_env():
    downloader._apply_proxy_mode("http://127.0.0.1:7890")
    assert os.environ["HTTP_PROXY"] == "http://127.0.0.1:7890"
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7890"


def test_env_mode_restores_after_direct(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://10.0.0.9:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.9:3128")
    downloader._apply_proxy_mode("direct")
    assert "HTTP_PROXY" not in os.environ
    downloader._apply_proxy_mode("env")
    assert os.environ["HTTP_PROXY"] == "http://10.0.0.9:3128"
