"""The Claude Code ``Stop`` hook: read the finished reply and speak it."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import cleaner, client, config as cfg

TRANSCRIPT_TAIL_BYTES = 512 * 1024
DEDUPE_SECONDS = 300


def read_payload(timeout: float = 2.0) -> Dict[str, Any]:
    """Read the JSON Claude Code writes to stdin (empty when run by hand)."""

    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        import select

        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return {}
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001 - Windows pipes cannot be select()ed
        try:
            raw = sys.stdin.read()
        except (OSError, ValueError, UnicodeDecodeError):
            return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "\n".join(parts)
    return ""


def message_from_transcript(path: Optional[str]) -> str:
    if not path:
        return ""
    file = Path(os.path.expanduser(str(path)))
    try:
        with file.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - TRANSCRIPT_TAIL_BYTES))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    lines = tail.splitlines()
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        message = record.get("message") or {}
        text = _text_from_content(message.get("content"))
        if text.strip():
            return text
    return ""


def extract_message(payload: Dict[str, Any]) -> str:
    last = payload.get("last_assistant_message")
    if isinstance(last, str) and last.strip():
        return last
    if isinstance(last, dict):
        text = _text_from_content(last.get("content")) or str(last.get("text") or "")
        if text.strip():
            return text
    return message_from_transcript(payload.get("transcript_path"))


def _transcript_size(payload: Dict[str, Any]) -> int:
    path = payload.get("transcript_path")
    if not path:
        return 0
    try:
        return os.path.getsize(os.path.expanduser(str(path)))
    except OSError:
        return 0


def _already_spoken(text: str, session: str, size: int = 0) -> bool:
    """Claude Code can fire Stop more than once for one turn; speak once.

    The transcript size is part of the key so two *different* turns that happen
    to end with the same words ("Done.") are both spoken.
    """

    digest = hashlib.sha256(f"{session}\0{size}\0{text}".encode("utf-8")).hexdigest()[:32]
    state = cfg.read_json(cfg.STATE_PATH, {}) or {}
    last = state.get("hook") or {}
    fresh = time.time() - float(last.get("ts") or 0) < DEDUPE_SECONDS
    if fresh and last.get("hash") == digest:
        return True
    cfg.write_json(cfg.STATE_PATH, {**state, "hook": {"hash": digest, "ts": time.time()}})
    return False


def run() -> int:
    payload = read_payload()
    settings = cfg.load_settings()
    if not settings.enabled or os.environ.get("CLAUDE_TTS_DISABLE", "").strip() in {"1", "true", "yes"}:
        return 0

    raw = extract_message(payload)
    if not raw.strip():
        cfg.log_line(cfg.HOOK_LOG, "no assistant message in hook payload; nothing to say")
        return 0

    text = cleaner.clean_for_speech(raw, settings.max_chars)
    if len(text) < 3:
        cfg.log_line(cfg.HOOK_LOG, "reply had no speakable prose (code/tool output only)")
        return 0

    session = str(payload.get("session_id") or payload.get("transcript_path") or "unknown")
    if _already_spoken(text, session, _transcript_size(payload)):
        cfg.log_line(cfg.HOOK_LOG, "duplicate Stop event; skipping")
        return 0

    started = time.perf_counter()
    ok = client.speak(text)
    elapsed = time.perf_counter() - started
    if not ok:
        cfg.log_line(cfg.HOOK_LOG, f"daemon unreachable; dropped {len(text)} chars ({elapsed:.2f}s)")
    else:
        cfg.log_line(cfg.HOOK_LOG, f"queued {len(text)} chars in {elapsed:.2f}s")
    return 0


def main() -> int:
    try:
        return run()
    except Exception as exc:  # noqa: BLE001 - a hook must never break Claude
        cfg.log_line(cfg.HOOK_LOG, f"hook error: {type(exc).__name__}: {exc}")
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
