import json
import threading
import urllib.request
from pathlib import Path

import pytest

from claude_tts import config as cfg, daemon


@pytest.fixture()
def server(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "DAEMON_LOG", tmp_path / "daemon.log")
    monkeypatch.setenv("CLAUDE_TTS_NULL_AUDIO", "1")
    settings = cfg.Settings(enabled=True)
    settings.port = 0
    engine = daemon.Engine(settings)
    monkeypatch.setattr(engine, "_load", lambda: None)  # no model download in unit tests
    httpd = daemon.DaemonServer(("127.0.0.1", 0), engine)
    engine.start()
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    yield engine, port
    httpd.stop_daemon()
    thread.join(timeout=5)


def _call(port, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def test_status_and_speak_queue(server, tmp_path, monkeypatch):
    engine, port = server
    status = _call(port, "/status")
    assert status["app"] == "claude-tts"
    assert status["pid"] > 0
    assert status["loading"] is True

    reply = _call(port, "/speak", {"text": "Hello from the test suite."})
    assert reply["ok"] is True
    assert reply["queued"] == len("Hello from the test suite.")

    stop = _call(port, "/stop", {})
    assert stop["ok"] is True
    assert engine.pending is None


def test_disabled_engine_ignores_speak(server, monkeypatch):
    engine, port = server
    engine.settings.enabled = False
    monkeypatch.setattr(cfg, "load_settings", lambda: cfg.Settings(enabled=False))
    reply = _call(port, "/speak", {"text": "should not be queued"})
    assert reply.get("muted") is True
    assert engine.pending is None


def test_dedupe_hook(monkeypatch, tmp_path):
    from claude_tts import hook

    monkeypatch.setattr(cfg, "STATE_PATH", tmp_path / "state.json")
    assert hook._already_spoken("same text", "session", 100) is False
    assert hook._already_spoken("same text", "session", 100) is True
    assert hook._already_spoken("same text", "session", 200) is False  # a new turn
    assert hook._already_spoken("same text", "other session", 200) is False
