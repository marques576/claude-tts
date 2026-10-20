import numpy as np

from claude_tts.playback import TimeStretch, clamp_speed, stretch_audio

SR = 24000


def tone(seconds: float = 2.0, freq: float = 220.0) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _dominant_freq(samples: np.ndarray) -> float:
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(samples.size)))
    return float(np.fft.rfftfreq(samples.size, 1 / SR)[int(np.argmax(spectrum))])


def test_speed_one_is_passthrough():
    data = tone(1.0)
    assert np.array_equal(stretch_audio(data, 1.0, SR), data)


def test_faster_is_shorter_and_slower_is_longer():
    data = tone(2.0)
    fast = stretch_audio(data, 1.5, SR)
    slow = stretch_audio(data, 0.75, SR)
    assert abs(len(fast) / len(data) - 1 / 1.5) < 0.02
    assert abs(len(slow) / len(data) - 1 / 0.75) < 0.02


def test_pitch_is_preserved():
    data = tone(2.0, 220.0)
    for speed in (0.75, 1.25, 1.5, 2.0):
        out = stretch_audio(data, speed, SR)
        assert abs(_dominant_freq(out) - 220.0) < 5.0, speed


def test_streaming_matches_whole_buffer_length():
    data = tone(2.0)
    whole = stretch_audio(data, 1.3, SR)
    stretcher = TimeStretch(1.3, SR)
    parts = [stretcher.push(data[i : i + 1024]) for i in range(0, data.size, 1024)]
    parts.append(stretcher.flush())
    streamed = np.concatenate(parts)
    assert abs(streamed.size - whole.size) <= 0.02 * whole.size
    assert np.isfinite(streamed).all()
    assert float(np.abs(streamed).max()) <= 0.35


def test_clamp():
    assert clamp_speed(0.1) == 0.5
    assert clamp_speed(5) == 2.0
    assert clamp_speed("nope") == 1.0
    assert clamp_speed(float("nan")) == 1.0
