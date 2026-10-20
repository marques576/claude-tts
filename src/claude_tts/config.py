"""Paths, user settings and small filesystem helpers."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Dict, Optional

APP = "claude-tts"
DEFAULT_PORT = 45733
DEFAULT_MAX_CHARS = 1500
DEFAULT_MODEL = "samuel-vitorino/sopro-v2-turbo"

BUNDLED_VOICE = Path(__file__).with_name("assets") / "reference-voice.wav"


def _xdg(env: str, fallback: str) -> Path:
    value = os.environ.get(env)
    base = Path(value).expanduser() if value else Path.home() / fallback
    return base / APP


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config")
CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache")
CONFIG_PATH = CONFIG_DIR / "config.json"
VOICE_PATH = CONFIG_DIR / "voice.wav"
DAEMON_INFO = CACHE_DIR / "daemon.json"
DAEMON_LOG = CACHE_DIR / "daemon.log"
HOOK_LOG = CACHE_DIR / "hook.log"
STATE_PATH = CACHE_DIR / "state.json"
SPAWN_LOCK = CACHE_DIR / "daemon.spawn.lock"
AUDIO_DIR = CACHE_DIR / "audio"

DEFAULT_CLAUDE_DIR = Path.home() / ".claude"


def claude_settings_path(project: bool = False) -> Path:
    if project:
        return Path.cwd() / ".claude" / "settings.json"
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base).expanduser() if base else DEFAULT_CLAUDE_DIR) / "settings.json"


def ensure_dirs() -> None:
    for path in (CONFIG_DIR, CACHE_DIR, AUDIO_DIR):
        path.mkdir(parents=True, exist_ok=True)


@dataclass
class Settings:
    """User-facing configuration (``~/.config/claude-tts/config.json``)."""

    enabled: bool = True
    voice: Optional[str] = None
    model: str = DEFAULT_MODEL
    device: str = "cpu"
    int8: bool = False
    steps: Optional[int] = None
    max_chars: int = DEFAULT_MAX_CHARS
    lang: Optional[str] = None
    port: int = DEFAULT_PORT
    volume: float = 1.0
    speed: float = 1.25
    player: str = "auto"

    @property
    def voice_path(self) -> Path:
        if self.voice:
            return Path(self.voice).expanduser()
        return BUNDLED_VOICE

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def load_settings() -> Settings:
    data: Dict[str, Any] = {}
    try:
        raw = json.loads(CONFIG_PATH.read_text("utf-8"))
        if isinstance(raw, dict):
            data = raw
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    allowed = {f.name for f in fields(Settings)}
    return Settings(**{k: v for k, v in data.items() if k in allowed})


def save_settings(settings: Settings) -> None:
    ensure_dirs()
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings.to_dict(), indent=2) + "\n", "utf-8")
    tmp.replace(CONFIG_PATH)


def set_setting(key: str, value: str) -> Settings:
    settings = load_settings()
    if key not in {f.name for f in fields(Settings)}:
        raise KeyError(key)
    current = getattr(settings, key)
    empty = value.strip().lower() in {"", "none", "null", "off"}
    optional = key in {"voice", "lang", "steps"}
    if isinstance(current, bool):
        parsed: Any = value.strip().lower() in {"1", "true", "yes", "on"}
    elif isinstance(current, int) and not isinstance(current, bool):
        parsed = int(value)
    elif isinstance(current, float):
        parsed = float(value)
    elif optional and empty:
        parsed = None
    else:
        parsed = value
    setattr(settings, key, parsed)
    save_settings(settings)
    return settings


# ---------------------------------------------------------------------------
# tiny persistence helpers
# ---------------------------------------------------------------------------


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, data: Any) -> None:
    ensure_dirs()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
    tmp.replace(path)


def log_line(path: Path, message: str) -> None:
    try:
        ensure_dirs()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


def platform_name() -> str:
    if sys.platform == "darwin":
        return "macOS"
    if sys.platform.startswith("win"):
        return "Windows"
    return "Linux"
