import pytest

import throttle


@pytest.fixture(autouse=True)
def _reset():
    throttle.set_rate(0)
    yield
    throttle.set_rate(0)


def test_zero_rate_means_unlimited_and_never_sleeps():
    sleeps = []
    throttle._sleep = sleeps.append
    throttle.set_rate(0)
    throttle.consume(10 * 1024 * 1024)
    assert sleeps == []
    assert throttle.get_rate() == 0


def test_set_rate_clamps_negative_and_returns_applied():
    assert throttle.set_rate(-5) == 0
    assert throttle.set_rate(1024) == 1024


def test_consume_sleeps_for_missing_tokens():
    clock = [0.0]
    sleeps = []
    throttle._monotonic = lambda: clock[0]
    throttle._sleep = sleeps.append
    throttle.set_rate(1000)              # 1000 B/s，初始桶空
    throttle.consume(500)                # 无余额 → 需要 0.5s
    assert sleeps and abs(sleeps[0] - 0.5) < 1e-6
    sleeps.clear()
    clock[0] += 1.0                      # 过 1 秒，攒到上限（0.5s 突发 = 500B）
    throttle.consume(400)                # 余额够 → 不 sleep
    assert sleeps == []
    throttle.consume(400)                # 余额不足 → 按比例补时
    assert sleeps and 0.2 < sleeps[0] <= 0.6


def test_burst_is_capped_so_idle_cannot_cheat():
    clock = [0.0]
    sleeps = []
    throttle._monotonic = lambda: clock[0]
    throttle._sleep = sleeps.append
    throttle.set_rate(1000)
    clock[0] += 3600                     # 挂机一小时
    throttle.consume(100000)             # 不能一次白拿一小时额度
    assert sleeps[0] > 90                # ≈ (100000 - 500) / 1000
    assert sleeps[0] <= 100


def test_rate_change_takes_effect_immediately():
    clock = [0.0]
    sleeps = []
    throttle._monotonic = lambda: clock[0]
    throttle._sleep = sleeps.append
    throttle.set_rate(1000)
    throttle.consume(1000)               # ≈1s
    sleeps.clear()
    throttle.set_rate(0)
    throttle.consume(5_000_000)          # 立刻不限速
    assert sleeps == []


def test_consume_ignores_zero_and_negative_sizes():
    sleeps = []
    throttle._sleep = sleeps.append
    throttle.set_rate(1)
    throttle.consume(0)
    throttle.consume(-10)
    assert sleeps == []


def test_settings_api_roundtrip_rate_limit():
    import app as appmod
    appmod.app.config["TESTING"] = True
    client = appmod.app.test_client()

    got = client.get("/api/settings").get_json()
    assert got["rate_limit"] == 0

    posted = client.post("/api/settings", json={"rate_limit": 512 * 1024}).get_json()
    assert posted["success"] is True and posted["rate_limit"] == 512 * 1024
    assert throttle.get_rate() == 512 * 1024

    client.post("/api/settings", json={"rate_limit": 0})
    assert throttle.get_rate() == 0


def test_write_granularity_is_half_second_budget():
    throttle.set_rate(65536)
    assert throttle.write_granularity() == 32768
    throttle.set_rate(1024)
    assert throttle.write_granularity() == 512
    throttle.set_rate(0)
    assert throttle.write_granularity() == 0
