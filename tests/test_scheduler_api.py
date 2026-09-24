import pytest

import app as appmod


class FakeScheduler:
    """只实现 /api/settings 与取消类路由用到的方法。"""

    def __init__(self):
        self.action = "none"
        self.unscheduled = []

    def get_finish_action(self):
        return self.action

    def set_finish_action(self, action):
        self.action = action if action in ("none", "shutdown", "suspend", "beep") else "none"
        return self.action

    def cancel_finish_action(self):
        old, self.action = self.action, "none"
        return {"action": old, "cancelled": True}

    def status(self, now=None):
        return {"finish_action": self.action, "fire_at": None, "remaining": 42,
                "scheduled": [{"task_id": "t_9", "start_at": 1234.0}]}

    def unschedule(self, task_id):
        self.unscheduled.append(task_id)
        return True

    def pending_at(self, task_id):
        return 1234.0 if task_id == "t_9" else None


@pytest.fixture
def fake(monkeypatch):
    f = FakeScheduler()
    monkeypatch.setattr(appmod, "scheduler", f)
    monkeypatch.setattr(appmod, "ffmpeg_status", lambda: {"available": False, "path": None})
    monkeypatch.setattr(appmod, "ytdlp_available", lambda: True)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client(), f


def test_settings_reports_scheduler_state_and_capabilities(fake):
    client, _ = fake
    got = client.get("/api/settings").get_json()
    assert got["finish_action"] == "none"
    assert got["finish_countdown"] == 42
    assert got["scheduled"] == [{"task_id": "t_9", "start_at": 1234.0}]
    assert got["capabilities"] == {"ffmpeg": {"available": False, "path": None},
                                   "ytdlp": True}


def test_settings_post_finish_action_roundtrip(fake):
    client, f = fake
    posted = client.post("/api/settings", json={"finish_action": "shutdown"}).get_json()
    assert posted["success"] is True and posted["finish_action"] == "shutdown"
    assert f.action == "shutdown"
    client.post("/api/settings", json={"finish_action": "reboot"})
    assert f.action == "none"


def test_cancel_endpoint_clears_action(fake):
    client, f = fake
    f.action = "shutdown"
    res = client.post("/api/finish_action/cancel").get_json()
    assert res["success"] is True and res["cancelled"] is True and res["action"] == "shutdown"
    assert f.action == "none"


def test_cancel_and_remove_routes_unschedule_the_task(fake):
    client, f = fake
    client.post("/api/cancel/t_9")
    client.delete("/api/remove/t_9")
    assert f.unscheduled == ["t_9", "t_9"]


def test_stream_payload_carries_schedule_and_finish_state(fake):
    _client, f = fake
    f.action = "shutdown"
    payload = appmod._stream_payload()
    assert payload["finish"] == {"action": "shutdown", "remaining": 42}
    assert isinstance(payload["tasks"], list) and "stats" in payload
