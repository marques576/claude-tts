"""Install / uninstall: stable CLI, Claude Code hooks, voice, model warm-up."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import client, config as cfg

PACKAGE = "claude-sopro-tts"
MARKER = "claude-tts"
HOOK_TIMEOUT = 15


# ---------------------------------------------------------------------------
# source / executable resolution
# ---------------------------------------------------------------------------


def _source_checkout() -> Optional[Path]:
    here = Path(__file__).resolve()
    for parent in list(here.parents)[:6]:
        pyproject = parent / "pyproject.toml"
        if pyproject.is_file():
            try:
                text = pyproject.read_text("utf-8")
            except OSError:
                continue
            if f'name = "{PACKAGE}"' in text:
                return parent
    return None


def installed_source() -> Optional[str]:
    """Best guess at "where this package came from", for ``uv tool install``."""

    checkout = _source_checkout()
    if checkout is not None:
        return str(checkout)
    try:
        from importlib.metadata import distribution

        dist = distribution(PACKAGE)
        raw = dist.read_text("direct_url.json")
        if raw:
            data = json.loads(raw)
            url = str(data.get("url") or "")
            if url.startswith("file://"):
                path = Path(url[7:])
                if path.exists():
                    return str(path)
            if url:
                vcs = data.get("vcs_info") or {}
                if vcs.get("vcs") == "git":
                    return url if url.startswith("git+") else f"git+{url}"
                return url
        return f"{PACKAGE}=={dist.version}"
    except Exception:  # noqa: BLE001 - package metadata is best effort
        return None


def _uv() -> Optional[str]:
    return shutil.which("uv")


def _tool_has_package() -> bool:
    uv = _uv()
    if not uv:
        return False
    try:
        result = subprocess.run([uv, "tool", "list"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return PACKAGE in result.stdout


def install_tool(source: Optional[str], force: bool = False) -> Optional[str]:
    """Make ``claude-tts`` a stable executable; return its absolute path."""

    uv = _uv()
    if uv and (force or not _tool_has_package()):
        if not source:
            source = installed_source()
        if not source:
            raise RuntimeError(
                "cannot work out where this package came from; pass --source "
                "(a git URL, PyPI name, or local checkout path)"
            )
        print(f"  installing {PACKAGE} as a uv tool from {source}")
        cmd = [uv, "tool", "install", "--force"]
        if force:
            # uv caches the wheel built from a local directory even with
            # --refresh; --no-cache is what actually picks up local edits.
            cmd += ["--refresh", "--no-cache"]
        cmd.append(source)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            sys.stderr.write(result.stdout)
            sys.stderr.write(result.stderr)
            raise RuntimeError("uv tool install failed")

    # Prefer the uv tool installation: when `install` itself runs under
    # `uvx`, the ephemeral interpreter is first on PATH and must not be baked
    # into the hooks (that directory disappears).
    if uv:
        candidate = _uv_tool_binary(uv)
        if candidate:
            return str(candidate)
    exe = shutil.which("claude-tts")
    if exe:
        return str(Path(exe).resolve())
    return _write_shim()


def _uv_tool_binary(uv: str) -> Optional[Path]:
    try:
        result = subprocess.run([uv, "tool", "dir", "--bin"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    candidate = Path(result.stdout.strip().splitlines()[-1]) / "claude-tts"
    if candidate.exists():
        return candidate.resolve()
    return None


def _write_shim() -> Optional[str]:
    """Fallback when uv is unavailable: wrapper in ~/.local/bin."""

    exe = Path.home() / ".local" / "bin" / "claude-tts"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text(
        "#!/bin/sh\nexec {} -m claude_tts \"$@\"\n".format(shlex.quote(sys.executable)),
        "utf-8",
    )
    exe.chmod(0o755)
    return str(exe)


# ---------------------------------------------------------------------------
# Claude Code settings
# ---------------------------------------------------------------------------


def _load_settings_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path} is not valid JSON ({exc}); refusing to edit it") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} does not contain a JSON object; refusing to edit it")
    return data


def _save_settings_file(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_suffix(path.suffix + ".claude-tts.bak")
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
    tmp.replace(path)


def _is_ours(command: str) -> bool:
    """True for hook commands this package wrote (and only those)."""

    try:
        parts = shlex.split(str(command))
    except ValueError:
        return False
    if not parts:
        return False
    if Path(parts[0]).name not in {"claude-tts", "claude-tts.exe"}:
        return False
    return len(parts) == 1 or parts[1] in {"hook", "stop", "start", "warm"}


def _drop_our_hooks(data: Dict[str, Any]) -> int:
    removed = 0
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    for event in list(hooks):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                kept_groups.append(group)
                continue
            kept = [h for h in handlers if not (isinstance(h, dict) and _is_ours(h.get("command", "")))]
            removed += len(handlers) - len(kept)
            if kept:
                group = {**group, "hooks": kept}
                kept_groups.append(group)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            hooks.pop(event)
    if not hooks:
        data.pop("hooks", None)
    return removed


def _our_hooks(exe: str) -> Dict[str, List[Dict[str, Any]]]:
    exe_q = shlex.quote(exe)
    return {
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": f"{exe_q} hook",
                        "timeout": HOOK_TIMEOUT,
                    }
                ]
            }
        ],
        "UserPromptSubmit": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": f"{exe_q} stop",
                        "timeout": 5,
                    }
                ]
            }
        ],
        "SessionStart": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": f"{exe_q} start",
                        "timeout": 10,
                    }
                ]
            }
        ],
        "SessionEnd": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": f"{exe_q} stop",
                        "timeout": 5,
                    }
                ]
            }
        ],
    }


def install_hooks(exe: str, project: bool = False) -> Tuple[Path, int]:
    path = cfg.claude_settings_path(project=project)
    data = _load_settings_file(path)
    _drop_our_hooks(data)
    existing_hooks = data.get("hooks")
    if existing_hooks is not None and not isinstance(existing_hooks, dict):
        raise RuntimeError(f"{path}: 'hooks' is not a JSON object; refusing to edit it")
    hooks = data.setdefault("hooks", {})
    for event, groups in _our_hooks(exe).items():
        existing = hooks.get(event)
        if not isinstance(existing, list):
            existing = []
            hooks[event] = existing
        existing.extend(groups)
    _save_settings_file(path, data)
    return path, len(_our_hooks(exe))


def remove_hooks(project: bool = False) -> List[Path]:
    touched = []
    paths = [cfg.claude_settings_path(project=project)]
    for path in paths:
        if not path.exists():
            continue
        data = _load_settings_file(path)
        removed = _drop_our_hooks(data)
        if removed:
            _save_settings_file(path, data)
            touched.append(path)
    return touched


# ---------------------------------------------------------------------------
# voice reference
# ---------------------------------------------------------------------------


def _audio_info(path: Path) -> Tuple[float, int]:
    try:
        import soundfile as sf

        info = sf.info(str(path))
        return float(info.duration), int(info.samplerate)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"cannot read audio file {path}: {exc}") from exc


def set_voice(source: Path) -> cfg.Settings:
    source = source.expanduser().resolve()
    if not source.exists():
        raise RuntimeError(f"no such file: {source}")
    duration, rate = _audio_info(source)
    if duration < 3:
        raise RuntimeError(f"reference clip is only {duration:.1f}s; Sopro needs 5-20s of clean speech")
    cfg.ensure_dirs()
    suffix = source.suffix.lower() or ".wav"
    target = cfg.VOICE_PATH.with_suffix(suffix)
    for old in cfg.CONFIG_DIR.glob("voice.*"):
        try:
            old.unlink()
        except OSError:
            pass
    shutil.copy2(source, target)
    settings = cfg.load_settings()
    settings.voice = str(target)
    cfg.save_settings(settings)
    return settings


# ---------------------------------------------------------------------------
# warm-up
# ---------------------------------------------------------------------------


def warm(wait_seconds: float = 900.0, quiet: bool = False, exe: Optional[str] = None) -> int:
    if not client.ensure_daemon(exe=exe):
        print("  could not start the daemon; see", cfg.DAEMON_LOG)
        return 1
    current = client.status()
    if current and current.get("ready"):
        if not quiet:
            print("  model already loaded")
        return 0

    if not quiet:
        print("  loading Sopro on CPU (first run downloads ~640 MB of weights)...")
    deadline = time.time() + max(1.0, wait_seconds)
    last_note = 0.0
    while time.time() < deadline:
        current = client.status()
        if current is None:
            # A busy loader can starve the HTTP thread for a couple of
            # seconds; only give up if the process is really gone.
            if not client._daemon_pid_alive():
                print("  daemon exited; see", cfg.DAEMON_LOG)
                return 1
            time.sleep(0.5)
            continue
        if current.get("ready"):
            if not quiet:
                print("  model ready")
            return 0
        if current.get("error"):
            print("  model failed to load:", current["error"])
            print("  see", cfg.DAEMON_LOG)
            return 1
        if not quiet and time.time() - last_note > 10:
            print("  still loading...")
            last_note = time.time()
        time.sleep(0.5)
    print("  model is still loading in the background; speech will start once it is ready")
    return 0


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def do_install(
    source: Optional[str] = None,
    ref: Optional[Path] = None,
    project: bool = False,
    int8: bool = False,
    no_warm: bool = False,
    port: Optional[int] = None,
    reinstall: bool = False,
) -> int:
    print("claude-tts: installing")
    settings = cfg.load_settings()
    if int8:
        settings.int8 = True
    if port:
        settings.port = int(port)
    cfg.save_settings(settings)

    if ref is not None:
        settings = set_voice(ref)
        print(f"  voice reference: {settings.voice}")

    exe = install_tool(source, force=reinstall)
    if not exe:
        print("  could not find or create a stable claude-tts executable")
        return 1
    print(f"  executable: {exe}")

    path, count = install_hooks(exe, project=project)
    print(f"  hooks written to {path} ({count} events)")

    if not shutil.which("claude"):
        print("  note: the `claude` CLI is not on PATH; hooks apply once Claude Code runs")

    if not no_warm:
        code = warm(exe=exe)
        if code != 0:
            return code

    print()
    print("Done. Claude's replies will be read aloud from the next turn on.")
    print(f"  speed                 # {settings.speed:g}x (claude-tts speed 1.0 for faithful pace)")
    print("  claude-tts status     # daemon / model state")
    print("  claude-tts off | on   # mute / unmute")
    print("  claude-tts voice FILE # use your own 5-20s reference clip")
    print("  claude-tts speak 'hi' # test without Claude")
    return 0


def do_uninstall(purge: bool = False) -> int:
    client.kill_daemon()
    for path in remove_hooks():
        print(f"  removed claude-tts hooks from {path}")
    if purge:
        for directory in (cfg.CONFIG_DIR, cfg.CACHE_DIR):
            shutil.rmtree(directory, ignore_errors=True)
        uv = _uv()
        if uv:
            subprocess.run([uv, "tool", "uninstall", PACKAGE], capture_output=True, text=True)
        print("  removed config, cache and the uv tool install")
    else:
        print("  config kept in", cfg.CONFIG_DIR, "(use --purge to remove)")
    print("Done.")
    return 0


def do_doctor() -> int:
    settings = cfg.load_settings()
    checks: List[Tuple[str, bool, str]] = []

    checks.append(("uv", bool(_uv()), _uv() or "not found (install from https://docs.astral.sh/uv/)"))
    claude = shutil.which("claude")
    checks.append(("claude CLI", bool(claude), claude or "not on PATH"))
    exe = shutil.which("claude-tts")
    checks.append(("claude-tts", bool(exe), exe or "not on PATH (run claude-tts install)"))

    settings_path = cfg.claude_settings_path()
    hook_ok = False
    if settings_path.exists():
        try:
            data = _load_settings_file(settings_path)
            hooks = data.get("hooks") or {}
            stop = hooks.get("Stop") or []
            hook_ok = any(
                isinstance(h, dict) and _is_ours(h.get("command", ""))
                for group in stop
                if isinstance(group, dict)
                for h in (group.get("hooks") or [])
            )
        except RuntimeError:
            hook_ok = False
    checks.append(("Stop hook", hook_ok, str(settings_path)))

    voice = settings.voice_path
    try:
        duration, rate = _audio_info(voice)
        voice_note = f"{voice} ({duration:.1f}s @ {rate} Hz)"
        voice_ok = duration >= 3
    except RuntimeError as exc:
        voice_ok, voice_note = False, str(exc)
    checks.append(("voice reference", voice_ok, voice_note))

    try:
        import sounddevice as sd

        outs = [d for d in sd.query_devices() if int(d.get("max_output_channels", 0)) > 0]
        note = f"{len(outs)} output device(s)" if outs else "no output device"
        checks.append(("sounddevice", bool(outs), note))
    except Exception as exc:  # noqa: BLE001
        fallback = shutil.which("afplay") or shutil.which("paplay") or shutil.which("aplay")
        checks.append(("sound output", bool(fallback), f"sounddevice unavailable ({exc}); fallback {fallback}"))

    state = client.status()
    if state:
        note = "ready" if state.get("ready") else ("error: " + str(state.get("error")) if state.get("error") else "loading")
        checks.append(("daemon", state.get("ready", False), f"pid {state.get('pid')} on port {state.get('port')} - {note}"))
    else:
        checks.append(("daemon", False, "not running (will start on first speech)"))

    width = max(len(name) for name, _, _ in checks)
    for name, ok, note in checks:
        mark = "ok  " if ok else "..  "
        print(f"  [{mark}] {name.ljust(width)}  {note}")
    return 0 if all(ok for _, ok, _ in checks) else 1


def do_config(key: Optional[str], value: Optional[str]) -> int:
    if key is None:
        print(json.dumps(cfg.load_settings().to_dict(), indent=2))
        return 0
    try:
        settings = cfg.set_setting(key, value) if value is not None else None
    except KeyError:
        print(f"unknown setting: {key}", file=sys.stderr)
        return 2
    if settings is None:
        current = getattr(cfg.load_settings(), key, None)
        print(json.dumps(current) if not isinstance(current, str) else current)
        return 0
    if key == "voice" and getattr(settings, "voice", None) is None:
        for old in cfg.CONFIG_DIR.glob("voice.*"):
            try:
                old.unlink()
            except OSError:
                pass
    print(f"{key} = {getattr(settings, key)!r}")
    print("restart the daemon for model/voice/device changes: claude-tts restart")
    return 0
