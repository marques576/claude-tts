"""Warm TTS daemon: loads Sopro once, speaks queued text, supports barge-in."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

from . import cleaner, config as cfg, playback

MAX_BODY = 200_000


def log(message: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    if sys.stdout.isatty():
        print(line, flush=True)
    cfg.log_line(cfg.DAEMON_LOG, message)


class Engine:
    def __init__(self, settings: cfg.Settings) -> None:
        self.settings = settings
        self.lock = threading.RLock()
        self.ready = threading.Event()
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.model = None
        self.reference = None
        self.error: Optional[str] = None
        self.speaking = False
        self.pending: Optional[Tuple[str, int]] = None
        self.generation = 0
        self.cancel_event = threading.Event()
        self.player = None
        self.started = time.time()
        self.played = 0
        self.last_chars = 0
        self.last_timing: Dict[str, Any] = {}
        self.worker = threading.Thread(target=self._worker_loop, name="speaker", daemon=True)

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        self.worker.start()
        threading.Thread(target=self._load, name="model-loader", daemon=True).start()

    def _load(self) -> None:
        settings = self.settings
        voice = settings.voice_path
        try:
            log(f"loading model {settings.model} on {settings.device}{' (int8)' if settings.int8 else ''}")
            t0 = time.perf_counter()
            from sopro import SoproTTS

            self.model = SoproTTS.from_pretrained(
                settings.model,
                device=settings.device,
                quantization="int8" if settings.int8 else None,
            )
            log(f"model loaded in {time.perf_counter() - t0:.1f}s")
            if not voice.exists():
                raise FileNotFoundError(f"voice reference not found: {voice}")
            t1 = time.perf_counter()
            self.reference = self.model.prepare_reference(ref_audio_path=str(voice))
            log(f"voice reference ready ({voice.name}) in {time.perf_counter() - t1:.2f}s")
            self.error = None
            self.ready.set()
        except Exception as exc:  # noqa: BLE001 - surfaced through /status
            self.error = f"{type(exc).__name__}: {exc}"
            log("model load failed:\n" + traceback.format_exc())

    # -- public API ------------------------------------------------------
    def speak(self, text: str) -> Dict[str, Any]:
        text = (text or "").strip()
        if not text:
            return {"ok": True, "queued": 0}
        # Read the flag live so `claude-tts off` takes effect without a restart.
        try:
            enabled = cfg.load_settings().enabled
        except Exception:  # noqa: BLE001
            enabled = self.settings.enabled
        if not enabled:
            return {"ok": True, "queued": 0, "muted": True}
        with self.lock:
            self.generation += 1
            generation = self.generation
            self.pending = (text, generation)
            self.cancel_event.set()
            player = self.player
        if player is not None:
            player.cancel()
        self.wake.set()
        return {"ok": True, "queued": len(text), "generation": generation}

    def stop(self) -> Dict[str, Any]:
        with self.lock:
            self.generation += 1
            self.pending = None
            self.cancel_event.set()
            player = self.player
        if player is not None:
            player.cancel()
        return {"ok": True, "stopped": True}

    def status(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "app": cfg.APP,
                "ok": True,
                "pid": os.getpid(),
                "ready": self.ready.is_set(),
                "loading": not self.ready.is_set() and self.error is None,
                "error": self.error,
                "speaking": self.speaking,
                "queued": 1 if self.pending else 0,
                "voice": str(self.settings.voice_path),
                "model": self.settings.model,
                "device": self.settings.device,
                "int8": self.settings.int8,
                "played": self.played,
                "last_chars": self.last_chars,
                "last_timing": self.last_timing,
                "uptime": round(time.time() - self.started, 1),
                "port": self.settings.port,
            }

    def shutdown(self) -> None:
        self.stop()
        self.stop_event.set()
        self.wake.set()

    # -- worker ----------------------------------------------------------
    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            self.wake.wait(0.5)
            self.wake.clear()
            while not self.stop_event.is_set():
                with self.lock:
                    item = self.pending
                    self.pending = None
                if item is None:
                    break
                text, generation = item
                try:
                    self._utterance(text, generation)
                except Exception:  # noqa: BLE001 - daemon must stay alive
                    log("speech failed:\n" + traceback.format_exc())

    def _utterance(self, text: str, generation: int) -> None:
        # Model load (and first-run weight download) can take minutes; wait it
        # out while still reacting to a newer reply or a stop request.
        while not self.ready.is_set() and not self.stop_event.is_set():
            with self.lock:
                if generation != self.generation:
                    return
            time.sleep(0.2)
        if self.error or self.model is None or self.reference is None:
            log(f"cannot speak: {self.error or 'model not ready'}")
            return

        with self.lock:
            if generation != self.generation:
                return
            cancel = threading.Event()
            self.cancel_event = cancel
            self.speaking = True
            self.last_chars = len(text)

        # Volume and speed are safe to change on the fly; the model, voice and
        # precision still need a restart.
        try:
            live = cfg.load_settings()
            self.settings.volume = live.volume
            self.settings.speed = live.speed
        except Exception:  # noqa: BLE001
            pass
        player = playback.make_player(self.settings)
        with self.lock:
            self.player = player
        player.bind(cancel)
        begin = time.perf_counter()
        timing: Dict[str, Any] = {"player": player.name, "chars": len(text)}
        try:
            if player.can_stream:
                timing.update(self._speak_stream(player, text, generation, cancel))
            else:
                timing.update(self._speak_segments(player, text, generation, cancel))
        finally:
            player.close()
            cancelled = cancel.is_set() or generation != self.generation
            with self.lock:
                self.player = None
                self.speaking = False
                if not cancelled:
                    self.played += 1
                    timing["total_s"] = round(time.perf_counter() - begin, 2)
                    self.last_timing = timing
            elapsed = time.perf_counter() - begin
            if cancelled:
                log(f"cancelled {len(text)} chars after {elapsed:.2f}s ({timing.get('audio_s', 0.0)}s audio emitted)")
            else:
                log(
                    "spoke {chars} chars via {player}: {total:.2f}s wall, {audio:.1f}s audio, first audio {ttfa}s".format(
                        chars=len(text),
                        player=timing.get("player"),
                        total=timing.get("total_s", elapsed),
                        audio=timing.get("audio_s", 0.0),
                        ttfa=timing.get("ttfa_s"),
                    )
                )

    def _speak_stream(self, player, text: str, generation: int, cancel: threading.Event) -> Dict[str, Any]:
        model = self.model
        kwargs = {"ref": self.reference, "lang": self.settings.lang}
        if self.settings.steps:
            kwargs["steps"] = int(self.settings.steps)
        chunks = model.stream(text, **kwargs)
        sample_rate = int(model.sample_rate)
        audio_samples = 0
        first_at: Optional[float] = None
        start = time.perf_counter()

        def measured():
            nonlocal audio_samples, first_at
            try:
                for chunk in chunks:
                    if cancel.is_set() or generation != self.generation:
                        break
                    if first_at is None:
                        first_at = time.perf_counter() - start
                        log(f"time to first audio: {first_at:.2f}s")
                    audio_samples += int(chunk.shape[-1])
                    yield chunk
            finally:
                close = getattr(chunks, "close", None)
                if close:
                    close()

        player.stream(measured(), sample_rate)
        return {
            "ttfa_s": round(first_at, 3) if first_at is not None else None,
            "audio_s": round(audio_samples / sample_rate, 2),
        }

    def _speak_segments(self, player, text: str, generation: int, cancel: threading.Event) -> Dict[str, Any]:
        model = self.model
        kwargs = {"ref": self.reference, "lang": self.settings.lang}
        if self.settings.steps:
            kwargs["steps"] = int(self.settings.steps)
        segments = cleaner.split_for_segments(text, 260)
        sample_rate = int(model.sample_rate)
        audio_samples = 0
        first_at: Optional[float] = None
        start = time.perf_counter()
        for segment in segments:
            if cancel.is_set() or generation != self.generation:
                break
            wav = model.synthesize(segment, **kwargs)
            if cancel.is_set() or generation != self.generation:
                break
            if first_at is None:
                first_at = time.perf_counter() - start
            audio_samples += int(wav.shape[-1])
            player.play_wav(wav, sample_rate)
        return {
            "ttfa_s": round(first_at, 3) if first_at is not None else None,
            "audio_s": round(audio_samples / sample_rate, 2),
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "claude-tts/0.1"
    protocol_version = "HTTP/1.1"

    # quieter default logging (one line per request is too much)
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    def _send(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("body too large")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid json") from exc
        return data if isinstance(data, dict) else {}

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?")[0] in {"/status", "/"}:
            self._send(self.server.engine.status())
        else:
            self._send({"ok": False, "error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send({"ok": False, "error": str(exc)}, 400)
            return
        engine: Engine = self.server.engine
        if path == "/speak":
            self._send(engine.speak(str(payload.get("text") or "")))
        elif path == "/stop":
            self._send(engine.stop())
        elif path == "/shutdown":
            self._send({"ok": True, "bye": True})
            threading.Thread(target=self.server.stop_daemon, daemon=True).start()
        else:
            self._send({"ok": False, "error": "not found"}, 404)


class DaemonServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, engine: Engine) -> None:
        super().__init__(address, Handler)
        self.engine = engine
        self._stopping = threading.Event()

    def stop_daemon(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        self.engine.shutdown()
        self.shutdown()


def run(settings: cfg.Settings, port: Optional[int] = None) -> int:
    cfg.ensure_dirs()
    settings.port = int(port or settings.port)
    engine = Engine(settings)
    try:
        server = DaemonServer(("127.0.0.1", settings.port), engine)
    except OSError as exc:
        log(f"cannot bind 127.0.0.1:{settings.port}: {exc}")
        return 1

    cfg.write_json(
        cfg.DAEMON_INFO,
        {
            "app": cfg.APP,
            "pid": os.getpid(),
            "port": settings.port,
            "url": f"http://127.0.0.1:{settings.port}",
            "started": time.time(),
        },
    )

    def handle_signal(signum, frame) -> None:  # noqa: ARG001
        log(f"signal {signum}: shutting down")
        threading.Thread(target=server.stop_daemon, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except (ValueError, OSError):
            pass

    engine.start()
    log(f"listening on http://127.0.0.1:{settings.port} (pid {os.getpid()})")
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        info = cfg.read_json(cfg.DAEMON_INFO, {}) or {}
        if info.get("pid") == os.getpid():
            try:
                cfg.DAEMON_INFO.unlink()
            except OSError:
                pass
        log("daemon stopped")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="claude-tts daemon")
    parser.add_argument("--port", type=int)
    args = parser.parse_args(argv)
    settings = cfg.load_settings()
    return run(settings, port=args.port)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
