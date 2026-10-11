#!/usr/bin/env python3
"""Whisper spike (spike-whisper-voice): proxy-whisper clips, device trials and offline analysis.

  clips                      synthesise the utterances (Google TTS Chirp 3 HD Charon) and their
                             LPC-whispered versions into $WEIXUE_VENDOR/session-clips/whisper-proxy/
  trials --port P --tag T --gain DB [--wakenet T] --plan voiced:55,35 whisper:35
                             one trial per clip and Mac volume on a diag build built with
                             WEIXUE_AUDIO_DEBUG_UDP=<mac>:9050; caller holds the device lock
  silence --port P --tag T --minutes N [--gain DB] [--wakenet T]
                             counts wake detections with nothing played
  analyze --tags T1,T2 ...   level, offline Silero VAD and Google STT on the captured microphone
  table                      markdown tables from the analysis

Raw microphone + reference frames from the device's audio debugger are captured by a UDP receiver
on port 9050 into <out>/<tag>.udp; host markers sent to the same port align them.
Needs the spike venv (numpy, scipy, onnxruntime, pyserial, google-cloud-speech/texttospeech) and
GOOGLE_APPLICATION_CREDENTIALS.
"""
import argparse
import io
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import wave

import numpy as np

ROOT = os.environ.get("WEIXUE_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "firmware", "tools"))
VENDOR = os.environ.get("WEIXUE_VENDOR", os.path.expanduser("~/Projects/weixue-vendor"))
CLIPS = os.path.join(VENDOR, "session-clips", "whisper-proxy")
OUT = os.path.join(VENDOR, "spikes", "whisper", "runs")
SILERO = os.path.join(VENDOR, "xiaozhi-esp32-server", "main", "xiaozhi-server", "models",
                      "snakers4_silero-vad", "src", "silero_vad", "data", "silero_vad.onnx")
UDP_PORT = 9050
# The receiver runs on a Python the macOS application firewall lets accept incoming datagrams.
RX_PYTHON = os.environ.get("WEIXUE_RX_PYTHON", "/opt/homebrew/opt/python@3.14/bin/python3.14")
SERVER = "xiaozhi-esp32-server"
VOICE = "en-US-Chirp3-HD-Charon"
RATE = 24000
MIC_RATE = 16000

UTTERANCES = [
    ("Jarvis, what time is it?", ["time"]),
    ("Jarvis, what day is it today?", ["day"]),
    ("Jarvis, what is seven times eight?", ["seven|7", "eight|8"]),
    ("Jarvis, how many days are in March?", ["days", "march"]),
    ("Jarvis, spell the word necessary.", ["spell", "necessary"]),
    ("Jarvis, what is the capital of Portugal?", ["capital", "portugal"]),
    ("Jarvis, tell me a short joke.", ["joke"]),
    ("Jarvis, how do you say thank you in French?", ["thank", "french"]),
    ("Jarvis, what is twenty percent of fifty?", ["twenty|20", "fifty|50"]),
    ("Jarvis, how far away is the moon?", ["far", "moon"]),
]
WAKE_RE = r"Application: Wake word detected"
STT_RE = r"Application: >> (.+)"
STATE_RE = re.compile(r"StateMachine: State: (\w+) -> (\w+)")


# Clips.

def lpc_whisper(x, sr, order=24, frame_ms=25, hop_ms=10, seed=7):
    """Replaces the excitation of x with white noise (frame-wise LPC), keeping the spectral
    envelope and the RMS of x: an unvoiced, whisper-like version of the same utterance."""
    from scipy.linalg import solve_toeplitz
    from scipy.signal import lfilter
    rng = np.random.default_rng(seed)
    x = x.astype(np.float64)
    pre = lfilter([1, -0.95], [1], x)
    n, h = int(sr * frame_ms / 1000), int(sr * hop_ms / 1000)
    win = np.hanning(n)
    y = np.zeros(len(x) + n)
    norm = np.zeros(len(x) + n)
    for start in range(0, len(x) - n, h):
        f = pre[start:start + n] * win
        r = np.correlate(f, f, "full")[n - 1:n + order]
        if r[0] < 1e-6:
            continue
        a = solve_toeplitz(r[:order], -r[1:order + 1])
        err = max(r[0] + np.dot(a, r[1:order + 1]), 1e-9)
        exc = rng.standard_normal(n) * np.sqrt(err / n)
        y[start:start + n] += lfilter([1], np.concatenate(([1], a)), exc) * win
        norm[start:start + n] += win ** 2
    y = y[:len(x)] / np.maximum(norm[:len(x)], 1e-3)
    y = lfilter([1], [1, -0.95], y)
    y *= np.sqrt(np.mean(x ** 2) / max(np.mean(y ** 2), 1e-12))
    return np.clip(y, -32768, 32767).astype(np.int16)


def write_wav(path, pcm, sr):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.astype(np.int16).tobytes())


def read_wav(path):
    with wave.open(path) as r:
        return np.frombuffer(r.readframes(r.getnframes()), np.int16), r.getframerate()


def cmd_clips(args):
    from google.cloud import texttospeech
    os.makedirs(CLIPS, exist_ok=True)
    client = texttospeech.TextToSpeechClient()
    manifest = []
    for i, (text, keys) in enumerate(UTTERANCES):
        voiced = os.path.join(CLIPS, f"u{i:02d}-voiced.wav")
        whisper = os.path.join(CLIPS, f"u{i:02d}-whisper.wav")
        if not os.path.exists(voiced):
            r = client.synthesize_speech(
                input=texttospeech.SynthesisInput(text=text),
                voice=texttospeech.VoiceSelectionParams(name=VOICE, language_code="en-US"),
                audio_config=texttospeech.AudioConfig(
                    audio_encoding=texttospeech.AudioEncoding.LINEAR16, sample_rate_hertz=RATE),
                timeout=30)
            with wave.open(io.BytesIO(r.audio_content)) as w:
                pcm = np.frombuffer(w.readframes(w.getnframes()), np.int16)
            pad = np.zeros(RATE // 4, np.int16)
            write_wav(voiced, np.concatenate([pad, pcm, pad]), RATE)
        if not os.path.exists(whisper):
            pcm, sr = read_wav(voiced)
            write_wav(whisper, lpc_whisper(pcm, sr), sr)
        manifest.append({"id": i, "text": text, "keys": keys, "voiced": voiced, "whisper": whisper})
    with open(os.path.join(CLIPS, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    print(f"{len(manifest)} utterances in {CLIPS}")


# Capture.

RX_SCRIPT = r"""
import signal, socket, struct, sys, time
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
s.bind(("0.0.0.0", 9050))
f = open(sys.argv[1], "ab")
signal.signal(signal.SIGTERM, lambda *a: (f.flush(), sys.exit(0)))
n = 0
while True:
    d, _ = s.recvfrom(65535)
    f.write(struct.pack("<dI", time.time(), len(d)) + d)
    n += 1
    if n % 32 == 0:
        f.flush()
"""


class Capture:
    def __init__(self, out_dir, tag):
        self.path = os.path.join(out_dir, f"{tag}.udp")
        rx = os.path.join(out_dir, "rx.py")
        with open(rx, "w") as f:
            f.write(RX_SCRIPT)
        self.proc = subprocess.Popen([RX_PYTHON, rx, self.path])
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        time.sleep(1.0)

    def mark(self, label):
        payload = b"MARK" + json.dumps({"t": time.time(), "label": label}).encode()
        self.sock.sendto(payload, ("127.0.0.1", UDP_PORT))

    def close(self):
        self.proc.terminate()
        self.proc.wait(5)


def load_capture(path):
    """Returns (mic int16 array, sample host times array, markers {label: host time})."""
    data = open(path, "rb").read()
    i, chunks, times, marks, offsets = 0, [], [], {}, []
    while i + 12 <= len(data):
        t, n = struct.unpack_from("<dI", data, i)
        payload = data[i + 12:i + 12 + n]
        i += 12 + n
        if payload.startswith(b"MARK"):
            m = json.loads(payload[4:])
            marks[m["label"]] = m["t"]
            offsets.append(m["t"] - t)
            continue
        frame = np.frombuffer(payload[:len(payload) // 4 * 4], np.int16).reshape(-1, 2)[:, 0]
        chunks.append(frame)
        times.append((t, len(frame)))
    off = float(np.median(offsets)) if offsets else 0.0
    mic = np.concatenate(chunks) if chunks else np.zeros(0, np.int16)
    stamps = np.empty(len(mic))
    pos = 0
    for t, n in times:
        stamps[pos:pos + n] = t + off - (n - np.arange(n)) / MIC_RATE
        pos += n
    return mic, stamps, marks


# Device trials.

def osa_volume(level=None):
    if level is None:
        v = subprocess.run(["osascript", "-e", "output volume of (get volume settings)"],
                           capture_output=True, text=True).stdout.strip()
        m = subprocess.run(["osascript", "-e", "output muted of (get volume settings)"],
                           capture_output=True, text=True).stdout.strip()
        return v, m
    subprocess.run(["osascript", "-e", f"set volume output volume {level}",
                    "-e", "set volume output muted false"], check=True)


def device_state(diag):
    hits = diag.find_all(STATE_RE)
    if hits:
        return STATE_RE.search(hits[-1][1]).group(2)
    ok, result, _ = diag.send("diag dump")
    m = re.search(r'"device_state":"(\w+)"', result or "") if ok else None
    return m.group(1) if m else "unknown"


def to_idle(diag, tries=4):
    for _ in range(tries):
        if device_state(diag) == "idle":
            return True
        diag.send("diag press_boot")
        diag.wait_for(r"State: \w+ -> idle", 2.5)
    return device_state(diag) == "idle"


def setup_device(diag, gain, wakenet):
    for line in ("diag volume 0", f"diag audio gain {gain}",
                 f"diag audio wakenet {wakenet if wakenet else 'default'}"):
        ok, result, _ = diag.send(line)
        diag.note(f"{line}: {'ok' if ok else 'error'} {result}")
        if not ok:
            raise SystemExit(f"{line} failed: {result}")


def open_diag(args, tag):
    from diag_inject import DiagSerial
    os.makedirs(args.out, exist_ok=True)
    return DiagSerial(args.port, os.path.join(args.out, f"{tag}-serial.log"))


def server_log(args, tag, t0):
    out = subprocess.run(["docker", "logs", "--timestamps", "--since", str(int(t0) - 2), SERVER],
                         capture_output=True, text=True, timeout=60)
    from diag_inject import redact
    with open(os.path.join(args.out, f"{tag}-server.log"), "w") as f:
        f.write("\n".join(redact(l) for l in (out.stdout + out.stderr).splitlines()) + "\n")


def cmd_trials(args):
    manifest = json.load(open(os.path.join(CLIPS, "manifest.json")))
    plan = []
    for item in args.plan:
        kind, levels = item.split(":")
        plan += [(kind, int(v)) for v in levels.split(",")]
    old = osa_volume()
    diag = open_diag(args, args.tag)
    cap = Capture(args.out, args.tag)
    rows_path = os.path.join(args.out, f"{args.tag}.jsonl")
    t_start = time.time()
    try:
        to_idle(diag)
        setup_device(diag, args.gain, args.wakenet)
        for kind, level in plan:
            osa_volume(level)
            for u in manifest:
                if not to_idle(diag):
                    diag.note("device did not return to idle")
                time.sleep(1.0)
                label = f"{kind}{level}-u{u['id']:02d}"
                since = diag.mark()
                cap.mark(label + ":start")
                t0 = time.monotonic()
                t0_host = time.time()
                subprocess.run(["afplay", u[kind]], check=True)
                t1_host = time.time()
                cap.mark(label + ":end")
                wake = diag.wait_for(WAKE_RE, 1.5, since)
                stt = diag.wait_for(STT_RE, 8.0, since) if wake else None
                row = {"tag": args.tag, "gain": args.gain, "wakenet": args.wakenet or "default",
                       "kind": kind, "level": level, "utt": u["id"], "label": label,
                       "t0": t0_host, "t1": t1_host, "wake": bool(wake),
                       "wake_s": round(wake[3] - t0, 2) if wake else None,
                       "stt": stt[2].group(1) if stt else None}
                diag.note(f"trial {json.dumps(row)}")
                with open(rows_path, "a") as f:
                    f.write(json.dumps(row) + "\n")
        to_idle(diag)
    finally:
        osa_volume(int(old[0]) if old[0].isdigit() else 25)
        if old[1] == "true":
            subprocess.run(["osascript", "-e", "set volume output muted true"])
        cap.close()
        diag.close()
        server_log(args, args.tag, t_start)


def cmd_silence(args):
    diag = open_diag(args, args.tag)
    cap = Capture(args.out, args.tag)
    t_start = time.time()
    try:
        to_idle(diag)
        setup_device(diag, args.gain, args.wakenet)
        since = diag.mark()
        diag.note(f"silence {args.minutes} min")
        time.sleep(args.minutes * 60)
        wakes = diag.find_all(WAKE_RE, since)
        diag.note(f"silence wakes={len(wakes)}")
        with open(os.path.join(args.out, f"{args.tag}.json"), "w") as f:
            json.dump({"tag": args.tag, "gain": args.gain, "wakenet": args.wakenet or "default",
                       "minutes": args.minutes, "wakes": len(wakes), "lines": [n for n, _ in wakes]}, f)
        to_idle(diag)
    finally:
        cap.close()
        diag.close()
        server_log(args, args.tag, t_start)


# Analysis.

def active_level(x):
    """(active RMS dBFS over the loudest 30 % of 20 ms frames, peak dBFS)."""
    x = x.astype(np.float64) / 32768.0
    n = MIC_RATE // 50
    frames = x[:len(x) // n * n].reshape(-1, n)
    if not len(frames):
        return None, None
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    top = np.sort(rms)[-max(1, int(len(rms) * 0.3)):]
    return round(20 * np.log10(np.sqrt(np.mean(top ** 2)) + 1e-9), 1), \
        round(20 * np.log10(np.max(np.abs(x)) + 1e-9), 1)


class Silero:
    def __init__(self):
        import onnxruntime
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = opts.intra_op_num_threads = 1
        self.s = onnxruntime.InferenceSession(SILERO, providers=["CPUExecutionProvider"], sess_options=opts)

    def probs(self, x):
        state = np.zeros((2, 1, 128), np.float32)
        ctx = np.zeros((1, 64), np.float32)
        out = []
        f = x.astype(np.float32) / 32768.0
        for i in range(0, len(f) - 511, 512):
            inp = np.concatenate([ctx, f[i:i + 512].reshape(1, -1)], axis=1).astype(np.float32)
            p, state = self.s.run(None, {"input": inp, "state": state, "sr": np.array(16000, np.int64)})
            ctx = inp[:, -64:]
            out.append(float(p.item()))
        return out


def vad_opens(probs, hi, lo=None, window=5, need=3):
    """The server's rule: hysteresis hi/lo, voice when >= need of the last `window` frames."""
    lo = hi - 0.2 if lo is None else lo
    last, win, voiced = False, [], 0
    for p in probs:
        last = True if p >= hi else (False if p <= lo else last)
        win = (win + [last])[-window:]
        voiced += win.count(True) >= need
    return voiced > 0, round(voiced * 0.032, 2)


def stt_batch(client, recognizer, pcm, model="latest_short"):
    from google.cloud.speech_v2.types import cloud_speech
    cfg = cloud_speech.RecognitionConfig(
        explicit_decoding_config=cloud_speech.ExplicitDecodingConfig(
            encoding=cloud_speech.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=MIC_RATE, audio_channel_count=1),
        language_codes=["en-US"], model=model,
        features=cloud_speech.RecognitionFeatures(enable_automatic_punctuation=True))
    r = client.recognize(request=cloud_speech.RecognizeRequest(
        recognizer=recognizer, config=cfg, content=pcm.astype(np.int16).tobytes()), timeout=30)
    return " ".join(res.alternatives[0].transcript for res in r.results if res.alternatives).strip()


def keys_hit(text, keys):
    t = (text or "").lower()
    return all(any(re.search(rf"\b{k}\b", t) for k in alt.split("|")) for alt in keys)


def cmd_analyze(args):
    manifest = {u["id"]: u for u in json.load(open(os.path.join(CLIPS, "manifest.json")))}
    silero = Silero()
    client = recognizer = None
    if args.stt:
        from google.api_core.client_options import ClientOptions
        from google.cloud.speech_v2 import SpeechClient
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_file(
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
        client = SpeechClient(credentials=creds, client_options=ClientOptions(
            api_endpoint="us-speech.googleapis.com"))
        recognizer = f"projects/{creds.project_id}/locations/us/recognizers/_"
    results = []
    for tag in args.tags.split(","):
        mic, stamps, marks = load_capture(os.path.join(args.out, f"{tag}.udp"))
        for line in open(os.path.join(args.out, f"{tag}.jsonl")):
            row = json.loads(line)
            t0 = marks.get(row["label"] + ":start", row["t0"])
            t1 = marks.get(row["label"] + ":end", row["t1"]) + 0.3
            seg = mic[(stamps >= t0) & (stamps <= t1)]
            row["captured_s"] = round(len(seg) / MIC_RATE, 2)
            row["level_dbfs"], row["peak_dbfs"] = active_level(seg)
            probs = silero.probs(seg)
            row["silero_max"] = round(max(probs), 3) if probs else None
            for hi in (0.5, 0.35, 0.2):
                row[f"vad_{hi}"], row[f"vad_{hi}_s"] = vad_opens(probs, hi, max(hi - 0.2, 0.05))
            row["device_ok"] = keys_hit(row["stt"], manifest[row["utt"]]["keys"])
            if client is not None and len(seg) > MIC_RATE // 2:
                for model in args.stt_models:
                    for gain_db in args.stt_gains:
                        pcm = np.clip(seg.astype(np.float64) * 10 ** (gain_db / 20), -32768, 32767)
                        try:
                            text = stt_batch(client, recognizer, pcm, model)
                        except Exception as e:  # noqa: BLE001
                            text = f"<error {type(e).__name__}>"
                        row[f"stt_{model}_{gain_db:g}"] = text
                        row[f"ok_{model}_{gain_db:g}"] = keys_hit(text, manifest[row["utt"]]["keys"])
            results.append(row)
            print(json.dumps(row), flush=True)
    with open(os.path.join(args.out, "analysis.jsonl"), "w") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")


def cmd_table(args):
    """Markdown tables from analysis.jsonl: one row per (tag, kind, level)."""
    rows = [json.loads(l) for l in open(os.path.join(args.out, "analysis.jsonl"))]
    groups = {}
    for r in rows:
        groups.setdefault((r["tag"], r["gain"], r["wakenet"], r["kind"], r["level"]), []).append(r)
    gains = sorted({k for r in rows for k in r if re.match(r"ok_\w+_-?[\d.]+$", k)})
    head = ["setting", "set", "Mac vol", "mic dBFS (median)", "wake", "device STT ok", "VAD 0.5", "VAD 0.35",
            "VAD 0.2"] + [f"offline {k[3:].rsplit('_', 1)[0]} +{k.rsplit('_', 1)[1]} dB" for k in gains]
    print("| " + " | ".join(head) + " |")
    print("|" + "---|" * len(head))
    for (tag, gain, wn, kind, level), g in groups.items():
        n = len(g)
        lv = [r["level_dbfs"] for r in g if r.get("level_dbfs") is not None]
        cells = [f"{tag} (gain {gain:g} dB, WakeNet {wn})", kind, str(level),
                 f"{np.median(lv):.1f}" if lv else "-",
                 f"{sum(r['wake'] for r in g)}/{n}",
                 f"{sum(bool(r['device_ok']) for r in g)}/{n}"]
        cells += [f"{sum(bool(r.get(f'vad_{t}')) for r in g)}/{n}" for t in (0.5, 0.35, 0.2)]
        cells += [f"{sum(bool(r.get(k)) for r in g)}/{n}" for k in gains]
        print("| " + " | ".join(cells) + " |")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("clips")
    for name in ("trials", "silence"):
        p = sub.add_parser(name)
        p.add_argument("--port", required=True)
        p.add_argument("--tag", required=True)
        p.add_argument("--gain", type=float, default=30.0)
        p.add_argument("--wakenet", type=float, default=0.0)
        p.add_argument("--out", default=OUT)
        if name == "trials":
            p.add_argument("--plan", nargs="+", required=True)
        else:
            p.add_argument("--minutes", type=float, default=10)
    p = sub.add_parser("analyze")
    p.add_argument("--tags", required=True)
    p.add_argument("--out", default=OUT)
    p.add_argument("--stt", action="store_true")
    p.add_argument("--stt-gains", type=float, nargs="+", default=[0.0])
    p.add_argument("--stt-models", nargs="+", default=["latest_short", "chirp_3"])
    p = sub.add_parser("table")
    p.add_argument("--out", default=OUT)
    args = ap.parse_args()
    {"clips": cmd_clips, "trials": cmd_trials, "silence": cmd_silence, "analyze": cmd_analyze, "table": cmd_table}[args.cmd](args)


if __name__ == "__main__":
    main()
