"""Local HTTP client used by hooks and the CLI to reach the warm daemon."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from . import config as cfg

USER_AGENT = "claude-tts"
SPAWN_STALE_SECONDS = 30.0


def base_url(port: Optional[int] = None) -> str:
    port = port or cfg.load_settings().port
    return f"http://127.0.0.1:{int(port)}"


def _request(
    path: str,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
    timeout: float = 1.5,
) -> Optional[Dict[str, Any]]:
    data = None
    headers = {"User-Agent": USER_AGENT}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base_url() + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
        return json.loads(body) if body else {}
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, json.JSONDecodeError):
        return None


def status(timeout: float = 2.0) -> Optional[Dict[str, Any]]:
    result = _request("/status", timeout=timeout)
    if result and result.get("app") == cfg.APP:
        return result
    return None


def is_running() -> bool:
    return status() is not None


def speak(text: str, timeout: float = 3.0) -> bool:
    if not text.strip():
        return False
    if status() is None and not ensure_daemon():
        # Last resort: the daemon may exist but be too busy to answer /status
        # (weight loading holds the GIL). The /speak request queues either way.
        pass
    # The daemon can be busy loading weights; retry briefly instead of dropping
    # the reply on one slow request.
    for attempt in range(3):
        result = _request("/speak", "POST", {"text": text}, timeout=timeout)
        if result and result.get("ok"):
            return True
        if attempt < 2:
            time.sleep(0.2)
    return False


def stop() -> bool:
    # No /status pre-check: this runs on every prompt submit and must never
    # wait on a busy daemon. A refused connection returns immediately anyway.
    result = _request("/stop", "POST", {}, timeout=1.0)
    return bool(result and result.get("ok"))


def shutdown() -> bool:
    result = _request("/shutdown", "POST", {}, timeout=2.0)
    return bool(result and result.get("ok"))


def daemon_command(exe: str | None = None) -> list[str]:
    """Command that starts a foreground daemon in this installation."""

    exe = exe or shutil.which("claude-tts")
    if exe:
        return [exe, "daemon"]
    return [sys.executable, "-m", "claude_tts", "daemon"]


def ensure_daemon(wait_seconds: float = 10.0, exe: str | None = None) -> bool:
    """Start the daemon detached if it is not running; wait for HTTP only."""

    if status() is not None:
        return True

    cfg.ensure_dirs()
    # A recorded pid that is still alive means the daemon exists but its HTTP
    # thread is momentarily busy (e.g. loading weights): just wait for it.
    if _daemon_pid_alive():
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if status(timeout=1.0) is not None:
                return True
            time.sleep(0.1)
        return False

    lock = _take_spawn_lock()
    if lock is None:
        # Someone else is starting it right now: wait for their daemon.
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if status(timeout=1.0) is not None:
                return True
            time.sleep(0.1)
        return False

    try:
        if status(timeout=1.0) is not None:
            return True
        log = cfg.DAEMON_LOG.open("a", encoding="utf-8")
        log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} starting daemon ===\n")
        log.flush()
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        subprocess.Popen(
            daemon_command(exe),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            cwd=str(Path.home()),
            env=env,
        )
        log.close()

        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if status(timeout=1.0) is not None:
                return True
            time.sleep(0.1)
        return False
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def _daemon_pid_alive() -> bool:
    info = cfg.read_json(cfg.DAEMON_INFO, {}) or {}
    if info.get("app") != cfg.APP:
        return False
    return _pid_alive(info.get("pid"))


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _take_spawn_lock() -> Optional[Path]:
    try:
        cfg.SPAWN_LOCK.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(cfg.SPAWN_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        try:
            age = time.time() - cfg.SPAWN_LOCK.stat().st_mtime
        except OSError:
            return None
        if age > SPAWN_STALE_SECONDS:
            try:
                cfg.SPAWN_LOCK.unlink()
            except OSError:
                return None
            return _take_spawn_lock()
        return None
    except OSError:
        return None
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return cfg.SPAWN_LOCK


def kill_daemon() -> bool:
    """Stop the daemon and wait for it to actually exit (frees the port)."""

    info = cfg.read_json(cfg.DAEMON_INFO, {}) or {}
    pid = info.get("pid")
    requested = shutdown()
    if not _pid_alive(pid):
        return requested
    for _ in range(50):
        time.sleep(0.1)
        if not _pid_alive(pid):
            return True
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return not _pid_alive(pid)
    for _ in range(30):
        time.sleep(0.1)
        if not _pid_alive(pid):
            return True
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    time.sleep(0.2)
    return not _pid_alive(pid)
