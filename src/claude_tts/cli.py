"""``claude-tts`` command line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import cleaner, client, config as cfg, daemon, hook, installer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-tts",
        description="Read Claude Code replies aloud with Sopro TTS, locally on your CPU.",
    )
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("install", help="install the hook, warm the model, pick a voice")
    p.add_argument("--source", help="git URL / PyPI name / path to (re)install the uv tool from")
    p.add_argument("--ref", type=Path, help="5-20s reference clip for voice cloning")
    p.add_argument("--project", action="store_true", help="write .claude/settings.json in this repo instead of ~/.claude")
    p.add_argument("--int8", action="store_true", help="faster, smaller model on CPU (slight quality trade-off)")
    p.add_argument("--port", type=int, help="daemon port (default 45733)")
    p.add_argument("--no-warm", action="store_true", help="do not wait for the model to download/load now")
    p.add_argument("--reinstall", action="store_true", help="force a fresh uv tool install")

    p = sub.add_parser("uninstall", help="remove hooks (and optionally everything else)")
    p.add_argument("--purge", action="store_true", help="also delete config, cache and the uv tool install")
    p.add_argument("--project", action="store_true", help="remove from .claude/settings.json in this repo")

    sub.add_parser("start", help="start the background daemon if needed (returns immediately)")
    sub.add_parser("restart", help="restart the daemon (reload voice/model settings)")
    p = sub.add_parser("warm", help="start the daemon and wait until the model is loaded")
    p.add_argument("--wait", type=float, default=900.0, help="seconds to wait (default 900)")
    sub.add_parser("status", help="show daemon and configuration state")
    sub.add_parser("stop", help="stop the current speech")
    sub.add_parser("kill", help="stop the background daemon (frees ~1 GB of RAM)")
    sub.add_parser("off", help="mute all speech")
    sub.add_parser("on", help="unmute")
    sub.add_parser("doctor", help="check the installation end to end")

    p = sub.add_parser("speak", help="speak text right now (no Claude needed)")
    p.add_argument("text", nargs="*", help="text to speak; omit to read stdin")
    p.add_argument("--raw", action="store_true", help="do not strip markdown / code")
    p.add_argument("--no-cap", action="store_true", help="ignore the max_chars limit")

    p = sub.add_parser("speed", help="show or set playback speed (0.5-2.0, pitch preserved)")
    p.add_argument("value", nargs="?", type=float, help="0.5 to 2.0; omit to show")

    p = sub.add_parser("voice", help="show or set the voice reference clip")
    p.add_argument("path", nargs="?", type=Path, help="5-20s audio file; omit to show the current voice")

    p = sub.add_parser("config", help="show or change settings")
    p.add_argument("key", nargs="?", help="setting name")
    p.add_argument("value", nargs="?", help="new value")

    p = sub.add_parser("daemon", help="run the daemon in the foreground (internal)")
    p.add_argument("--port", type=int)

    p = sub.add_parser("hook", help="Claude Code Stop hook (internal)")
    p.add_argument("--verbose", action="store_true")
    return parser


def _read_speak_text(args) -> str:
    if args.text:
        return " ".join(args.text)
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def _status_human() -> int:
    settings = cfg.load_settings()
    state = client.status()
    print("claude-tts")
    print(f"  enabled     {settings.enabled}")
    print(f"  voice       {settings.voice_path}")
    print(f"  model       {settings.model} ({settings.device}{', int8' if settings.int8 else ''})")
    print(f"  max chars   {settings.max_chars}")
    print(f"  speed       {settings.speed:g}x    volume {settings.volume:g}")
    print(f"  port        {settings.port}")
    print(f"  hooks       {cfg.claude_settings_path()}")
    if state is None:
        print("  daemon      not running (it starts on the first reply or `claude-tts start`)")
        return 0
    print(f"  daemon      pid {state.get('pid')}, up {state.get('uptime')}s")
    if state.get("error"):
        print(f"  model       error: {state['error']}")
    elif state.get("ready"):
        print("  model       ready")
    else:
        print("  model       loading...")
    print(f"  speaking    {state.get('speaking')}")
    if state.get("last_timing"):
        timing = state["last_timing"]
        print(
            "  last reply  {chars} chars, {audio}s audio, first audio {ttfa}s, {player}".format(
                chars=timing.get("chars"),
                audio=timing.get("audio_s"),
                ttfa=timing.get("ttfa_s"),
                player=timing.get("player"),
            )
        )
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "status"

    if command == "install":
        source = args.source
        if args.reinstall and not source:
            source = installer.installed_source()
        if source:
            args.source = source
        return installer.do_install(
            source=args.source,
            ref=args.ref,
            project=args.project,
            int8=args.int8,
            no_warm=args.no_warm,
            port=args.port,
            reinstall=args.reinstall,
        )

    if command == "uninstall":
        return installer.do_uninstall(purge=args.purge)

    if command == "hook":
        return hook.main()

    if command == "stop":
        client.stop()
        return 0

    if command == "start":
        return 0 if client.ensure_daemon() else 1

    if command == "kill":
        ok = client.kill_daemon()
        print("claude-tts: daemon stopped" if ok else "claude-tts: daemon was not running")
        return 0

    if command == "restart":
        client.kill_daemon()
        return 0 if client.ensure_daemon() else 1

    if command == "warm":
        return installer.warm(wait_seconds=args.wait)

    if command == "status":
        return _status_human()

    if command == "on":
        settings = cfg.load_settings()
        settings.enabled = True
        cfg.save_settings(settings)
        client.ensure_daemon()
        print("claude-tts: speech enabled")
        return 0

    if command == "off":
        settings = cfg.load_settings()
        settings.enabled = False
        cfg.save_settings(settings)
        client.stop()
        print("claude-tts: speech muted (claude-tts on to resume)")
        return 0

    if command == "speak":
        text = _read_speak_text(args)
        if not text.strip():
            parser.error("nothing to speak (pass text or pipe it in)")
        settings = cfg.load_settings()
        if not args.raw:
            text = cleaner.clean_for_speech(text, settings.max_chars if not args.no_cap else 10**9)
        if not text.strip():
            print("nothing speakable in that text", file=sys.stderr)
            return 1
        if not settings.enabled:
            print("speech is muted; run `claude-tts on` first", file=sys.stderr)
            return 1
        if not client.speak(text):
            print(f"could not reach the daemon; see {cfg.DAEMON_LOG}", file=sys.stderr)
            return 1
        state = client.status() or {}
        if not state.get("ready"):
            print("model is still loading; your text will be spoken as soon as it is ready")
        return 0

    if command == "speed":
        settings = cfg.load_settings()
        if args.value is None:
            print(f"speed: {settings.speed:g}x")
            return 0
        if not 0.5 <= args.value <= 2.0:
            parser.error("speed must be between 0.5 and 2.0")
        settings.speed = round(args.value, 2)
        cfg.save_settings(settings)
        print(f"speed: {settings.speed:g}x (takes effect on the next reply)")
        return 0

    if command == "voice":
        if args.path is None:
            settings = cfg.load_settings()
            path = settings.voice_path
            print(path)
            try:
                duration, rate = installer._audio_info(path)
                print(f"{duration:.1f}s @ {rate} Hz")
            except RuntimeError as exc:
                print(exc, file=sys.stderr)
                return 1
            return 0
        settings = installer.set_voice(args.path)
        print(f"voice reference: {settings.voice}")
        if client.is_running():
            client.kill_daemon()
            client.ensure_daemon()
            print("daemon restarting to load the new voice")
        return 0

    if command == "config":
        return installer.do_config(args.key, args.value)

    if command == "doctor":
        return installer.do_doctor()

    if command == "daemon":
        settings = cfg.load_settings()
        return daemon.run(settings, port=args.port)

    parser.error(f"unknown command {command}")
    return 2
