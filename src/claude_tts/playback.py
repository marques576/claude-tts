"""Audio output: real-time streaming when sounddevice works, OS players otherwise."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Iterable, Iterator, Optional

import numpy as np

from . import config as cfg

_EXTERNAL_PLAYERS = {
    "darwin": ["afplay"],
    "linux": ["paplay", "aplay", "ffplay", "mpv"],
    "win32": ["powershell"],
}


def null_audio() -> bool:
    value = os.environ.get("CLAUDE_TTS_NULL_AUDIO", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _sounddevice_ok() -> bool:
    if null_audio():
        return False
    try:
        import sounddevice as sd  # noqa: F401

        devices = sd.query_devices()
        return any(int(d.get("max_output_channels", 0)) > 0 for d in devices)
    except Exception:
        return False


def resolve_external_player(setting: str = "auto") -> Optional[list[str]]:
    if setting and setting.strip().lower() not in {"auto", "external"}:
        return shlex.split(setting)
    names = _EXTERNAL_PLAYERS.get(sys.platform, ["ffplay"])
    for name in names:
        if name == "powershell":
            if shutil.which("powershell") or shutil.which("pwsh"):
                return ["powershell", "-NoProfile", "-Command"]
            continue
        if shutil.which(name):
            return [name]
    return None


def make_player(settings) -> "Player":
    explicit = settings.player.strip().lower() if settings.player else "auto"
    if explicit == "null" or null_audio():
        return NullPlayer(settings)
    if explicit not in {"auto", "external"}:
        return FilePlayer(settings)
    if _sounddevice_ok():
        return DevicePlayer(settings)
    return FilePlayer(settings)


def clamp_speed(speed) -> float:
    try:
        value = float(speed)
    except (TypeError, ValueError):
        return 1.0
    if value != value:  # NaN
        return 1.0
    return max(0.5, min(2.0, value))


class TimeStretch:
    """Streaming WSOLA time-stretch: changes pace, keeps pitch and timbre.

    Audio is fed in chunks with :meth:`push`; each call returns whatever
    stretched audio is ready.  :meth:`flush` emits the tail at the end of an
    utterance.  WSOLA aligns each output frame to the most similar nearby input
    frame, which avoids both the chipmunk effect of resampling and the phasiness
    of a plain phase vocoder.
    """

    def __init__(self, speed: float, sample_rate: int) -> None:
        self.speed = clamp_speed(speed)
        self.sample_rate = int(sample_rate)
        frame = int(round(self.sample_rate * 0.042)) // 2 * 2
        self.frame = max(256, frame)
        self.hop_out = self.frame // 2
        # speed is a playback multiplier: 1.5 consumes 1.5x more input per
        # output frame, so the result is 1.5x shorter.
        self.hop_in = max(1, int(round(self.hop_out * self.speed)))
        self.search = max(8, self.frame // 4)
        self.window = np.hanning(self.frame).astype(np.float32)
        self.input = np.zeros(0, np.float32)
        self.out = np.zeros(0, np.float32)
        self.k = 0
        self.emitted = 0
        self.out_base = 0
        self.prev_start: Optional[int] = None

    def push(self, samples) -> np.ndarray:
        samples = np.asarray(samples, np.float32).reshape(-1)
        if samples.size:
            self.input = np.concatenate([self.input, samples]) if self.input.size else samples.copy()
            self._produce(final=False)
        out, self.out = self.out, np.zeros(0, np.float32)
        self.emitted += out.size
        self.out_base = self.emitted
        return out

    def flush(self) -> np.ndarray:
        self._produce(final=True)
        # push() already emitted some frames; only the remainder belongs here.
        expected_total = int(round(self.input.size / self.speed))
        remaining = max(0, expected_total - self.emitted)
        out = self.out[:remaining] if remaining < self.out.size else self.out
        self.emitted += out.size
        self.out_base = self.emitted
        if out.size:
            out = np.array(out, copy=True)
            fade = min(out.size, max(1, int(0.01 * self.sample_rate)))
            out[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)
        self.out = np.zeros(0, np.float32)
        return out

    def _produce(self, final: bool) -> None:
        n, hop_out, hop_in = self.frame, self.hop_out, self.hop_in
        while True:
            t = self.k * hop_in
            if final:
                if t >= self.input.size:
                    break
            elif t + n + self.search > self.input.size:
                break
            start = self._pick_start(t)
            pos = self.k * hop_out - self.out_base
            end = pos + n
            if pos < 0:  # already emitted (cannot happen, but never corrupt audio)
                self.k += 1
                continue
            if self.out.size < end:
                self.out = np.pad(self.out, (0, end - self.out.size))
            seg = self.input[start : start + n]
            if seg.size < n:
                seg = np.pad(seg, (0, n - seg.size))
            self.out[pos:end] += seg * self.window
            self.prev_start = start
            self.k += 1

    def _pick_start(self, t: int) -> int:
        n, half = self.frame, self.frame // 2
        max_start = max(0, self.input.size - n)
        t = min(max(0, t), max_start)
        lo = max(0, t - self.search)
        hi = min(max_start, t + self.search)
        if self.prev_start is None or hi <= lo:
            return t
        natural = self.prev_start + self.hop_out
        if natural + half > self.input.size:
            return t
        ref = self.input[natural : natural + half]
        ref_norm = float(np.sqrt(np.dot(ref, ref))) + 1e-9
        best_score = -2.0
        best = t
        for offset in range(lo, hi + 1, 4):
            cand = self.input[offset : offset + half]
            cand_norm = float(np.sqrt(np.dot(cand, cand))) + 1e-9
            score = float(np.dot(cand, ref)) / (cand_norm * ref_norm)
            if score > best_score:
                best_score = score
                best = offset
        return best


def stretch_audio(samples, speed: float, sample_rate: int) -> np.ndarray:
    """One-shot helper for whole WAV segments (the external-player fallback)."""

    speed = clamp_speed(speed)
    data = np.asarray(samples, np.float32).reshape(-1)
    if speed == 1.0 or data.size == 0:
        return data
    stretcher = TimeStretch(speed, sample_rate)
    return np.concatenate([stretcher.push(data), stretcher.flush()])


class Player:
    """One utterance's worth of output."""

    can_stream = False
    name = "null"

    def __init__(self, settings) -> None:
        self.settings = settings
        self.volume = max(0.0, min(4.0, float(settings.volume or 1.0)))
        self.speed = clamp_speed(getattr(settings, "speed", 1.0))
        self._cancelled: Optional[threading.Event] = None
        self._proc: Optional[subprocess.Popen] = None
        self._stream = None
        self._lock = threading.Lock()

    def bind(self, cancelled: threading.Event) -> None:
        self._cancelled = cancelled

    # -- helpers ---------------------------------------------------------
    def _is_cancelled(self) -> bool:
        return bool(self._cancelled and self._cancelled.is_set())

    def _flat(self, chunk) -> np.ndarray:
        array = chunk
        if hasattr(array, "detach"):
            array = array.detach().cpu().numpy()
        return np.asarray(array, dtype=np.float32).reshape(-1)

    def _shape(self, chunk) -> np.ndarray:
        array = self._flat(chunk)
        if self.volume != 1.0:
            array = np.clip(array * self.volume, -1.0, 1.0)
        return np.ascontiguousarray(array)

    def _stretch(self, samples, sample_rate: int) -> np.ndarray:
        return stretch_audio(samples, self.speed, sample_rate)

    # -- API -------------------------------------------------------------
    def stream(self, chunks: Iterable, sample_rate: int) -> None:  # pragma: no cover - abstract
        for _ in chunks:
            if self._is_cancelled():
                return
        raise NotImplementedError

    def play_wav(self, wav, sample_rate: int) -> None:  # pragma: no cover - abstract
        if self._is_cancelled():
            return
        raise NotImplementedError

    def cancel(self) -> None:
        """Safe to call from another thread: stops playback as soon as possible.

        The engine's cancel event stops the synthesis/consumer loops at the
        next chunk boundary; here we only need to kill an external player.
        Calling ``stream.abort()`` from another thread deadlocks CoreAudio, so
        a device stream is stopped by its own consumer thread instead.
        """

        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass


class NullPlayer(Player):
    name = "null"
    can_stream = True

    def stream(self, chunks: Iterable, sample_rate: int) -> None:
        stretcher = TimeStretch(self.speed, sample_rate) if self.speed != 1.0 else None
        for chunk in chunks:
            if self._is_cancelled():
                return
            data = self._shape(chunk)
            if stretcher is not None:
                stretcher.push(data)
        if stretcher is not None:
            stretcher.flush()

    def play_wav(self, wav, sample_rate: int) -> None:
        self._shape(wav)


class DevicePlayer(Player):
    """sounddevice/PortAudio streaming playback.

    Synthesis and playback run in two threads.  Sopro generates audio ~2.5x
    faster than real time, so a small prefetch queue keeps the device fed
    while the next chunk is being computed (a single thread would serialize
    generation and playback, doubling the wall clock).
    """

    name = "sounddevice"
    can_stream = True
    _SENTINEL = object()

    def _open(self, sample_rate: int):
        import sounddevice as sd

        stream = sd.OutputStream(
            samplerate=int(sample_rate),
            channels=1,
            dtype="float32",
            blocksize=0,
            latency="low",
        )
        stream.start()
        return stream

    def stream(self, chunks: Iterable, sample_rate: int) -> None:
        import queue

        items: "queue.Queue" = queue.Queue(maxsize=12)

        def enqueue(data: np.ndarray) -> bool:
            while True:
                if self._is_cancelled():
                    return False
                try:
                    items.put(data, timeout=0.1)
                    return True
                except queue.Full:
                    continue

        def produce() -> None:
            stretcher = TimeStretch(self.speed, sample_rate) if self.speed != 1.0 else None
            try:
                for chunk in chunks:
                    if self._is_cancelled():
                        break
                    data = self._shape(chunk)
                    if stretcher is not None:
                        data = stretcher.push(data)
                        if data.size == 0:
                            continue
                    if not enqueue(data):
                        return
                if stretcher is not None:
                    tail = stretcher.flush()
                    if tail.size:
                        enqueue(tail)
            except Exception:  # noqa: BLE001 - surfaced by the consumer's stream
                pass
            finally:
                while True:
                    try:
                        items.put(self._SENTINEL, timeout=0.1)
                        break
                    except queue.Full:
                        if self._is_cancelled():
                            break

        producer = threading.Thread(target=produce, name="synth", daemon=True)
        producer.start()
        stream = None
        try:
            while True:
                item = items.get()
                if item is self._SENTINEL or self._is_cancelled():
                    break
                if stream is None:
                    stream = self._open(sample_rate)
                    with self._lock:
                        self._stream = stream
                try:
                    step = max(1, int(sample_rate * 0.15))
                    for offset in range(0, len(item), step):
                        if self._is_cancelled():
                            break
                        stream.write(item[offset : offset + step])
                except Exception:
                    if self._is_cancelled():
                        break
                    raise
                if self._is_cancelled():
                    break
        finally:
            with self._lock:
                self._stream = None
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
            producer.join(timeout=1.0)

    def play_wav(self, wav, sample_rate: int) -> None:
        self.stream([wav], sample_rate)


class FilePlayer(Player):
    """Write a WAV and hand it to afplay / paplay / ffplay."""

    name = "external"

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.command = resolve_external_player(settings.player)

    def _temp_path(self) -> Path:
        cfg.ensure_dirs()
        fd, name = tempfile.mkstemp(prefix="segment-", suffix=".wav", dir=str(cfg.AUDIO_DIR))
        os.close(fd)
        return Path(name)

    def _spawn(self, path: Path) -> Optional[subprocess.Popen]:
        if not self.command:
            return None
        cmd = list(self.command)
        if cmd[0] in {"powershell", "pwsh"}:
            cmd += [f"(New-Object Media.SoundPlayer '{path}').PlaySync();"]
        elif cmd[0] == "ffplay":
            cmd += ["-nodisp", "-autoexit", "-loglevel", "quiet", str(path)]
        elif cmd[0] == "mpv":
            cmd += ["--no-video", "--really-quiet", str(path)]
        else:
            cmd += [str(path)]
        try:
            return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return None

    def play_wav(self, wav, sample_rate: int) -> None:
        if self._is_cancelled():
            return
        import soundfile as sf

        data = self._stretch(self._shape(wav), sample_rate)
        path = self._temp_path()
        try:
            sf.write(str(path), data, int(sample_rate), subtype="PCM_16")
            proc = self._spawn(path)
            if proc is None:
                return
            with self._lock:
                self._proc = proc
            while proc.poll() is None:
                if self._is_cancelled():
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                    break
                try:
                    proc.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            with self._lock:
                self._proc = None
            try:
                path.unlink()
            except OSError:
                pass

    def stream(self, chunks: Iterable, sample_rate: int) -> None:
        # No file player can play a live stream; accumulate then play once.
        import soundfile as sf

        parts = []
        for chunk in chunks:
            if self._is_cancelled():
                return
            parts.append(self._flat(chunk))
        if not parts:
            return
        data = np.concatenate(parts) if len(parts) > 1 else parts[0]
        self.play_wav(data, sample_rate)
