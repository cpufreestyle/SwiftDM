import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


import pytest


@pytest.fixture(autouse=True)
def _isolate_shared_config(monkeypatch):
    """配置写入只落内存覆写层，绝不动用户真实的 ~/.swiftdm/config.json。

    Scheduler._persist() 会把整张定时任务表写回共享配置；测试进程里的调度器
    单例是空的，一次 pytest 就把用户真实的定时下载表冲成测试数据，
    重启后这些任务全部变成「已取消 · 重启后中断」。
    这里给 get/set 套一层内存覆写：读仍走真实配置，写只进覆写层，测试结束就丢。
    """
    import config

    overlay = {}
    real_get = config.get

    def fake_get(key, *args):
        # 真身 get(key) 只接一个参数，多余参数原样往后传，不改叫法
        return overlay[key] if key in overlay else real_get(key, *args)

    monkeypatch.setattr(config, "get", fake_get)
    monkeypatch.setattr(config, "set",
                        lambda key, value: overlay.__setitem__(key, value))
    return overlay
