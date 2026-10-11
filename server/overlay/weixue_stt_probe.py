"""Per-segment ASR input probe: frame arrival record, audio level, and the env-gated capture of
each utterance's 16 kHz PCM with a JSON sidecar (WEIXUE_STT_CAPTURE=1)."""
import json
import math
import os
import threading
import time
import wave
from collections import deque
from datetime import datetime, timezone

import numpy as np

SAMPLE_RATE_HZ = 16000
# Arrival times kept per connection: 66 s of 60 ms frames, more than the 59 s ASR limit.
FRAME_TIMES_KEPT = 1100
# Frames handed to ASR within BURST_GAP_S of the previous one belong to a burst.
BURST_GAP_S = 0.02
CAPTURE_ENV = "WEIXUE_STT_CAPTURE"
CAPTURE_DIR_ENV = "WEIXUE_STT_CAPTURE_DIR"
CAPTURE_MAX_ENV = "WEIXUE_STT_CAPTURE_MAX"
DEFAULT_CAPTURE_DIR = "data/captures"
DEFAULT_CAPTURE_MAX = 500

_lock = threading.Lock()
_seq = 0


def note_frame(conn, pcm_frame):
    """Records the frame's arrival at ASR (monotonic time) and the session's frame and byte counts."""
    times = conn.__dict__.get("weixue_frame_times")
    if times is None:
        times = conn.weixue_frame_times = deque(maxlen=FRAME_TIMES_KEPT)
    times.append(time.monotonic())
    conn.weixue_frames_in = getattr(conn, "weixue_frames_in", 0) + 1
    conn.weixue_bytes_in = getattr(conn, "weixue_bytes_in", 0) + len(pcm_frame)


def segment_arrivals(conn, count):
    """Arrival times of the last `count` frames (conn.asr_audio is always a suffix of the frames received)."""
    times = list(conn.__dict__.get("weixue_frame_times") or ())
    return times[-count:] if count else []


def leading_burst(arrivals):
    """Number of frames at the start of the segment handed over within BURST_GAP_S of each other."""
    n = 1 if arrivals else 0
    while n < len(arrivals) and arrivals[n] - arrivals[n - 1] < BURST_GAP_S:
        n += 1
    return n


def level_dbfs(pcm: bytes):
    """(peak, rms) of 16-bit PCM in dBFS, -inf for digital silence."""
    samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16).astype(np.float64)
    if samples.size == 0:
        return -math.inf, -math.inf

    def db(value):
        return 20 * math.log10(value / 32768) if value > 0 else -math.inf

    return db(float(np.max(np.abs(samples)))), db(float(np.sqrt(np.mean(samples * samples))))


def capture_enabled():
    return os.environ.get(CAPTURE_ENV, "").lower() in ("1", "true", "yes")


def _capture_dir():
    return os.environ.get(CAPTURE_DIR_ENV) or DEFAULT_CAPTURE_DIR


def _prune(directory, keep):
    wavs = sorted(name for name in os.listdir(directory) if name.endswith(".wav"))
    for name in wavs[: max(len(wavs) - keep, 0)]:
        for path in (name, name[:-4] + ".json"):
            try:
                os.remove(os.path.join(directory, path))
            except FileNotFoundError:
                pass


def write_capture(pcm: bytes, meta: dict):
    """Writes <UTC time>-<seq>.wav and its .json sidecar under the capture directory, keeps the
    newest WEIXUE_STT_CAPTURE_MAX pairs and returns the base name. File names carry no device or
    session identity."""
    global _seq
    directory = _capture_dir()
    os.makedirs(directory, exist_ok=True)
    with _lock:
        _seq += 1
        name = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%f')[:-3]}Z-{_seq:04d}"
    with wave.open(os.path.join(directory, name + ".wav"), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE_HZ)
        wav.writeframes(pcm)
    update_capture(name, meta)
    try:
        keep = int(os.environ.get(CAPTURE_MAX_ENV) or DEFAULT_CAPTURE_MAX)
    except ValueError:
        keep = DEFAULT_CAPTURE_MAX
    _prune(directory, keep)
    return name


def update_capture(name, meta: dict):
    path = os.path.join(_capture_dir(), name + ".json")
    try:
        with open(path) as f:
            current = json.load(f)
    except (FileNotFoundError, ValueError):
        current = {}
    current.update(meta)
    with open(path + ".tmp", "w") as f:
        json.dump(current, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(path + ".tmp", path)
