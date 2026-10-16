"""浏览器监控的下载链接判定回归测试。

背景：旧规则是「URL 路径里出现 download 子串就算下载链接」。复制一个
https://example.com/downloads 这样的普通页面分享链接，剪贴板监听就会凭空
建出一个下载任务；而真实下载站常用的 ?file=report.pdf 形式反而漏判。
修复见 browser_monitor.py _is_download_url：改成末段扩展名、query 文件名参数、
末段动作词三条规则，末段判定避免把 /downloads/page/2 当下载。
"""
import pytest

from browser_monitor import BrowserMonitor


@pytest.fixture()
def monitor():
    return BrowserMonitor.__new__(BrowserMonitor)


@pytest.mark.parametrize("url", [
    "https://cdn.example.com/files/app-2.1.0.zip",
    "https://mirrors.aliyun.com/ubuntu/24.04/ubuntu-24.04.iso",
    "https://example.com/get?file=report.pdf",
    "https://example.com/attachment/download?id=99",
    "https://example.com/download",
    "https://example.com/dl",
])
def test_real_download_urls_are_caught(monitor, url):
    assert monitor._is_download_url(url), url


@pytest.mark.parametrize("url", [
    # 普通页面：路径里带 download 字样但不是下载
    "https://example.com/downloads",
    "https://example.com/downloads/page/2",
    "https://app.example.com/download-app",
    "https://example.com/blog/how-to-download-files",
    "https://example.com/user/downloads/history",
    "https://example.com/download.html",
    # 与下载无关的普通页面
    "https://example.com/",
    "https://example.com/about",
    "https://example.com/search?q=download+manager",
])
def test_plain_pages_are_not_caught(monitor, url):
    assert not monitor._is_download_url(url), url


def test_query_filename_extension_counts(monitor):
    """扩展名出现在 query 里时也要认，这是旧规则漏掉的一类。"""
    assert monitor._is_download_url("https://example.com/get?file=report.pdf")
    assert monitor._is_download_url(
        "https://example.com/out?filename=setup-1.2.exe")
    # query 里没有下载扩展名时不能仅因为参数名叫 file 就判下载
    assert not monitor._is_download_url("https://example.com/get?file=12345")

