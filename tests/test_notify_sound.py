"""完成提示音：选项收敛、播放降级、Windows 音序。"""
import os
import sys
import types

import pytest

import notify_sound


def teardown_function():
    notify_sound.set_sound("none")     # 复位全局单例，避免影响其他用例


def test_set_sound_coerces_unknown_to_none():
    assert notify_sound.set_sound("beep") == "beep"
    assert notify_sound.get_sound() == "beep"
    assert notify_sound.set_sound("loud") == "none"
    assert notify_sound.get_sound() == "none"


def test_play_is_noop_when_disabled():
    assert notify_sound.set_sound("none") == "none"
    assert notify_sound.play() is False


@pytest.mark.skipif(os.name != "nt", reason="winsound only exists on Windows")
def test_play_beep_uses_three_short_tones(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "winsound",
                        types.SimpleNamespace(Beep=lambda f, d: calls.append((f, d))))
    notify_sound.set_sound("beep")
    assert notify_sound.play() is True
    assert [f for f, _ in calls] == [880, 660, 880]


@pytest.mark.skipif(os.name != "nt", reason="winsound only exists on Windows")
def test_play_system_uses_os_alias_sound(monkeypatch):
    calls = []
    fake = types.SimpleNamespace(
        Beep=lambda f, d: calls.append(("beep", f)),
        PlaySound=lambda name, flags: calls.append(("play", name)),
        SND_ALIAS=0x10000,
    )
    monkeypatch.setitem(sys.modules, "winsound", fake)
    notify_sound.set_sound("system")
    assert notify_sound.play() is True
    assert calls == [("play", "SystemAsterisk")]


@pytest.mark.skipif(os.name != "nt", reason="winsound only exists on Windows")
def test_play_survives_broken_audio(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("no audio device")

    fake = types.SimpleNamespace(Beep=_boom, PlaySound=_boom, SND_ALIAS=0x10000)
    monkeypatch.setitem(sys.modules, "winsound", fake)
    notify_sound.set_sound("beep")
    assert notify_sound.play() is False     # 失败必须静默，不能影响下载
