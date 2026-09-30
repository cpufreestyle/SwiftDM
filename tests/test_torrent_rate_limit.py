"""BT 任务的全局限速：设置里填的限速必须真的落到 libtorrent 的共享 session 上。

`torrent.py::_apply_session_settings` 此前没有 `download_rate_limit`，用户设置的限速对 BT
完全无效（HTTP / 流媒体都走 throttle 令牌桶）。这里把两条路径都钉住：session 懒创建时读
当前值，创建之后 `set_rate` 变更即时下发。
"""
import pytest

torrent = pytest.importorskip("torrent")


def test_apply_session_settings_carries_the_current_rate(monkeypatch):
    """建 session 时把当前限速写进 settings（先改限速、后建 session 的顺序也不能漏）。"""
    import throttle
    captured = {}

    class FakeSession:
        def apply_settings(self, settings):
            captured.update(settings)

    throttle.set_rate(256 * 1024)
    try:
        torrent._apply_session_settings(FakeSession())
        assert captured["download_rate_limit"] == 256 * 1024
    finally:
        throttle.set_rate(0)


def test_session_rate_limit_normalises_junk_to_unlimited():
    """libtorrent 的 0 = 不限速；None / 负数 / 非数字都归零，别把怪值传进去。"""
    assert torrent._session_rate_limit(None) == 0
    assert torrent._session_rate_limit(-5) == 0
    assert torrent._session_rate_limit("x") == 0
    assert torrent._session_rate_limit(1024) == 1024


def test_rate_limit_change_reaches_a_live_session(monkeypatch):
    """已存在的 session 要跟着 set_rate 变：改设置时 BT 不能继续裸奔。"""
    import throttle
    applied = []

    class FakeSession:
        def apply_settings(self, settings):
            applied.append(settings["download_rate_limit"])

    monkeypatch.setattr(torrent, "_session", FakeSession())
    throttle.set_rate(128 * 1024)
    try:
        assert applied == [128 * 1024]
        throttle.set_rate(0)
        assert applied[-1] == 0
    finally:
        throttle.set_rate(0)


def test_real_session_accepts_the_limit():
    """真 session 端到端：libtorrent 确实收下这个设置，不是只写在 dict 里。"""
    import throttle
    s = torrent._get_session()
    throttle.set_rate(64 * 1024)
    try:
        assert s.get_settings()["download_rate_limit"] == 64 * 1024
    finally:
        throttle.set_rate(0)
        s.apply_settings({"download_rate_limit": 0})
