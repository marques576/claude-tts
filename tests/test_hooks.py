import json
from pathlib import Path

import pytest

from claude_tts import config as cfg, hook, installer


def test_extract_from_payload():
    assert hook.extract_message({"last_assistant_message": "Hello there"}) == "Hello there"


def test_extract_from_transcript(tmp_path: Path):
    transcript = tmp_path / "session.jsonl"
    rows = [
        {"type": "user", "message": {"content": "hi"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "old"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "newest"}]}},
    ]
    transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert hook.message_from_transcript(str(transcript)) == "newest"


def test_hook_install_and_remove(tmp_path: Path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "model": "opus",
                "hooks": {
                    "Stop": [{"hooks": [{"type": "command", "command": "echo something-else"}]}],
                    "PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo x"}]}],
                },
            }
        )
    )
    monkeypatch.setattr(cfg, "claude_settings_path", lambda project=False: settings_file)

    installer.install_hooks("/usr/local/bin/claude-tts", project=True)
    installer.install_hooks("/usr/local/bin/claude-tts", project=True)  # idempotent

    data = json.loads(settings_file.read_text())
    assert data["model"] == "opus"
    stop = data["hooks"]["Stop"]
    ours = [h for g in stop for h in g["hooks"] if "claude-tts" in h["command"]]
    assert len(ours) == 1
    assert any("echo something-else" in h["command"] for g in stop for h in g["hooks"])
    assert data["hooks"]["PostToolUse"][0]["matcher"] == "Bash"
    assert any("claude-tts" in h["command"] for g in data["hooks"]["UserPromptSubmit"] for h in g["hooks"])

    installer.remove_hooks(project=True)
    data = json.loads(settings_file.read_text())
    # the pre-existing Stop hook survives, ours is gone
    assert [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]] == ["echo something-else"]
    assert any("echo x" in h["command"] for g in data["hooks"]["PostToolUse"] for h in g["hooks"])


def test_install_refuses_invalid_settings(tmp_path: Path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{not json")
    monkeypatch.setattr(cfg, "claude_settings_path", lambda project=False: settings_file)
    with pytest.raises(RuntimeError):
        installer.install_hooks("/usr/local/bin/claude-tts", project=True)


def test_is_ours_does_not_match_unrelated():
    assert installer._is_ours("/Users/me/.local/bin/claude-tts hook")
    assert installer._is_ours("claude-tts")
    assert not installer._is_ours("echo claude-tts is nice")
    assert not installer._is_ours("/opt/other/bin/claude-tts-speak hook")
