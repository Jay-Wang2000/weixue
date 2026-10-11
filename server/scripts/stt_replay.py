"""Replays 16 kHz utterance captures through latest_short streaming and chirp_3 batch.

Captures are the WAV + JSON pairs the server writes with WEIXUE_STT_CAPTURE=1 (data/captures/),
or any 16 kHz mono 16-bit WAV. A sidecar's `expected` text, when present, scores both providers.

  (set -a; . ./.env; set +a; ../.venv/bin/python scripts/stt_replay.py data/captures --pacing recorded)
  ../.venv/bin/python scripts/stt_replay.py --synthesize DIR      # TTS-made replay set (see --help)

Streaming sends 60 ms frames: `realtime` one per 60 ms; `burst` all at once; `recorded` the
sidecar's arrival_offsets_s (realtime without them). The stream is half-closed after the last
frame, as at the VAD's end of utterance. `stream+fallback` applies the provider's rules: chirp_3's
text when the stream returned no non-empty final (no_final) or ended the utterance more than
TRUNCATED_TAIL_S before the end of its audio (truncated), else the stream's text. Output: one JSON line per
capture and a summary line; GOOGLE_SA_FILE names the service account file.
"""
import argparse
import array
import json
import math
import os
import random
import re
import sys
import time
import wave
from pathlib import Path

RATE = 16000
FRAME = 1920
LANGS = ["en-US", "cmn-Hans-CN"]
LOCATION = "us"
STREAM_MODEL = "latest_short"
BATCH_MODEL = "chirp_3"
# Phrases of the 2026-10-11 06:55 session (docs/logs/session-2026-10-11-0655-rca.md).
SESSION_PHRASES = (
    ("one_breath", "Jarvis, what time is it?"),
    ("after_cue", "Yes."),
    ("long_reply", "Jarvis, tell me a long story about a lighthouse keeper."),
    ("barge_in", "Jarvis, tell me a long story about a dragon."),
    ("bedside", "Jarvis, switch the screen to bedside mode."),
    ("plain", "What time is it?"),
)
# google_stt_stream.TRUNCATED_TAIL_S
TRUNCATED_TAIL_S = 0.6
# Peak levels (dBFS) of the synthesized set; quiet captures exercise the models' level floor.
SYNTH_PEAKS = (-6, -26, -36, -42, -48)
SYNTH_NOISE_DBFS = -66


def _clients():
    from google.api_core.client_options import ClientOptions
    from google.cloud.speech_v2 import SpeechClient
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(os.environ["GOOGLE_SA_FILE"])
    client = SpeechClient(credentials=creds, client_options=ClientOptions(api_endpoint=f"{LOCATION}-speech.googleapis.com"))
    return creds, client, f"projects/{creds.project_id}/locations/{LOCATION}/recognizers/_"


def _config(model):
    from google.cloud.speech_v2.types import cloud_speech

    return cloud_speech.RecognitionConfig(
        explicit_decoding_config=cloud_speech.ExplicitDecodingConfig(
            encoding=cloud_speech.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=RATE,
            audio_channel_count=1,
        ),
        language_codes=LANGS,
        model=model,
        features=cloud_speech.RecognitionFeatures(enable_automatic_punctuation=True),
    )


def read_pcm(path):
    with wave.open(str(path)) as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, RATE):
            raise ValueError(f"{path}: not 16 kHz mono 16-bit")
        return w.readframes(w.getnframes())


def write_pcm(path, pcm):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)


def frames_of(pcm):
    pcm = pcm + b"\0" * ((-len(pcm)) % FRAME)
    return [pcm[i : i + FRAME] for i in range(0, len(pcm), FRAME)]


def schedule(n, pacing, meta):
    """Send offsets (s) for n frames."""
    offsets = meta.get("arrival_offsets_s") or []
    if pacing == "recorded" and len(offsets) == n:
        return [float(x) for x in offsets]
    if pacing == "burst":
        return [0.0] * n
    return [i * 0.06 for i in range(n)]


def stream(client, recognizer, pcm, offsets, model=STREAM_MODEL):
    from google.cloud.speech_v2.types import cloud_speech

    fr = frames_of(pcm)
    mark = {}

    def requests():
        yield cloud_speech.StreamingRecognizeRequest(
            recognizer=recognizer,
            streaming_config=cloud_speech.StreamingRecognitionConfig(
                config=_config(model),
                streaming_features=cloud_speech.StreamingRecognitionFeatures(interim_results=True),
            ),
        )
        t0 = mark["t0"] = time.monotonic()
        for frame, offset in zip(fr, offsets):
            delay = t0 + offset - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            yield cloud_speech.StreamingRecognizeRequest(audio=frame)
        mark["half_close"] = time.monotonic()

    finals, interims, first_interim, utterance_end = [], 0, None, None
    end_event = cloud_speech.StreamingRecognizeResponse.SpeechEventType.END_OF_SINGLE_UTTERANCE
    for response in client.streaming_recognize(requests=requests(), timeout=90):
        if response.speech_event_type == end_event and response.speech_event_offset:
            utterance_end = response.speech_event_offset.total_seconds() or None
        for result in response.results:
            if not result.alternatives:
                continue
            if result.is_final:
                finals.append(result.alternatives[0].transcript.strip())
            else:
                interims += 1
                if first_interim is None:
                    first_interim = time.monotonic() - mark["t0"]
    end = time.monotonic()
    text = " ".join(t for t in finals if t)
    return {
        "text": text,
        "interims": interims,
        "utterance_end_s": utterance_end,
        "truncated": utterance_end is not None and len(fr) * FRAME / 2 / RATE - utterance_end > TRUNCATED_TAIL_S,
        "first_interim_ms": None if first_interim is None else round(first_interim * 1000),
        "final_after_half_close_ms": round((end - mark.get("half_close", end)) * 1000),
    }


def batch(client, recognizer, pcm):
    from google.cloud.speech_v2.types import cloud_speech

    response = client.recognize(
        request=cloud_speech.RecognizeRequest(recognizer=recognizer, config=_config(BATCH_MODEL), content=pcm),
        timeout=30,
        retry=None,
    )
    return " ".join(r.alternatives[0].transcript.strip() for r in response.results if r.alternatives).strip()


def normalize(text):
    return re.sub(r"[^\w\s]", "", (text or "").casefold()).split()


def word_errors(expected, heard):
    """Word-level edit distance between expected and heard (punctuation and case ignored)."""
    a, b = normalize(expected), normalize(heard)
    row = list(range(len(b) + 1))
    for i, word in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, other in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (word != other))
    return row[len(b)]


def captures(paths):
    for path in paths:
        path = Path(path)
        for wav in sorted(path.glob("*.wav")) if path.is_dir() else [path]:
            side = wav.with_suffix(".json")
            meta = json.loads(side.read_text()) if side.exists() else {}
            yield wav, meta


def replay(paths, pacing, model=STREAM_MODEL):
    _, client, recognizer = _clients()
    totals = {"n": 0, "speech": 0, "stream_empty": 0, "batch_empty": 0, "fallback_empty": 0,
              "stream_errors": 0, "batch_errors": 0, "fallback_errors": 0, "words": 0}
    for wav, meta in captures(paths):
        if meta.get("kind") == "wake_only":
            continue
        pcm = read_pcm(wav)
        offsets = schedule(len(frames_of(pcm)), pacing, meta)
        s = stream(client, recognizer, pcm, offsets, model)
        b = batch(client, recognizer, pcm)
        fallback = b if s["truncated"] or not s["text"] else s["text"]
        row = {"capture": wav.name, "kind": meta.get("kind"), "peak_dbfs": meta.get("peak_dbfs"),
               "pacing": pacing, "stream_model": model, "stream": s, "batch": b, "stream+fallback": fallback}
        expected = meta.get("expected")
        totals["n"] += 1
        if expected:
            totals["speech"] += 1
            totals["words"] += len(normalize(expected))
            row["expected"] = expected
            for key, heard in (("stream", s["text"]), ("batch", b), ("fallback", fallback)):
                errors = word_errors(expected, heard)
                row[f"{key}_word_errors"] = errors
                totals[f"{key}_errors"] += errors
                totals[f"{key}_empty"] += not heard
        else:
            totals["stream_empty"] += not s["text"]
            totals["batch_empty"] += not b
            totals["fallback_empty"] += not fallback
        print(json.dumps(row, ensure_ascii=False), flush=True)
    if totals["words"]:
        for key in ("stream", "batch", "fallback"):
            totals[f"{key}_wer"] = round(totals[f"{key}_errors"] / totals["words"], 3)
    print(json.dumps({"summary": totals}), flush=True)


def _noise(seconds, dbfs, seed):
    rnd = random.Random(seed)
    amp = 32768 * 10 ** (dbfs / 20) * math.sqrt(3)
    return array.array("h", (int(rnd.uniform(-amp, amp)) for _ in range(int(seconds * RATE)))).tobytes()


def _scaled(pcm, peak_dbfs):
    samples = array.array("h", pcm)
    peak = max(1, max(abs(x) for x in samples))
    gain = 32768 * 10 ** (peak_dbfs / 20) / peak
    return array.array("h", (max(-32768, min(32767, int(x * gain))) for x in samples)).tobytes()


def _mix(a, b):
    x, y = array.array("h", a), array.array("h", b)
    y.extend([0] * max(0, len(x) - len(y)))
    return array.array("h", (max(-32768, min(32767, p + q)) for p, q in zip(x, y))).tobytes()


def synthesize(directory, voice):
    """Google TTS clips of SESSION_PHRASES at SYNTH_PEAKS over a noise floor: 0.6 s pre-roll, the
    phrase, 0.3 s tail, as an utterance the VAD hands over. Sidecars carry `expected`, the level
    and a `recorded` schedule with a 2.34 s leading burst (the device's wake-word audio plus held
    packets) for phrases that start with the wake word."""
    from google.cloud import texttospeech

    creds, _, _ = _clients()
    tts = texttospeech.TextToSpeechClient(credentials=creds)
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    for index, (tag, text) in enumerate(SESSION_PHRASES):
        response = tts.synthesize_speech(
            input=texttospeech.SynthesisInput(text=text),
            voice=texttospeech.VoiceSelectionParams(name=voice, language_code="en-US"),
            audio_config=texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.LINEAR16, sample_rate_hertz=RATE),
        )
        clean = response.audio_content[44:]
        for peak in SYNTH_PEAKS:
            speech = _mix(_scaled(clean, peak), _noise(len(clean) / 2 / RATE, SYNTH_NOISE_DBFS, index))
            pcm = _noise(0.6, SYNTH_NOISE_DBFS, index + 100) + speech + _noise(0.3, SYNTH_NOISE_DBFS, index + 200)
            n = len(frames_of(pcm))
            burst = min(n, 39) if text.startswith("Jarvis") else 0
            offsets = [0.0] * burst + [(i - burst + 1) * 0.06 for i in range(burst, n)]
            name = f"synth-{tag}-{voice.rsplit('-', 1)[-1].lower()}-{-peak:02d}db"
            write_pcm(out / f"{name}.wav", pcm)
            meta = {"kind": "wake" if burst else "follow", "expected": text, "peak_dbfs": peak,
                    "voice": voice, "arrival_offsets_s": [round(x, 3) for x in offsets], "synthetic": True}
            (out / f"{name}.json").write_text(json.dumps(meta, indent=1))
            print(out / f"{name}.wav")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="capture directories or WAV files")
    ap.add_argument("--pacing", choices=("realtime", "burst", "recorded"), default="recorded")
    ap.add_argument("--synthesize", metavar="DIR", help="write the TTS replay set to DIR and exit")
    ap.add_argument("--voice", default="en-US-Chirp3-HD-Charon")
    ap.add_argument("--stream-model", default=STREAM_MODEL, help="streaming model to compare with chirp_3 batch")
    a = ap.parse_args()
    if a.synthesize:
        synthesize(a.synthesize, a.voice)
        return
    if not a.paths:
        ap.error("give capture paths or --synthesize DIR")
    replay(a.paths, a.pacing, a.stream_model)


if __name__ == "__main__":
    sys.exit(main())
