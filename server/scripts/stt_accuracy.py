#!/usr/bin/env python3
"""STT accuracy check: 10 English and 10 Mandarin sentences synthesized with Google TTS at 16 kHz,
recognized by latest_short StreamingRecognize (audio fed in real time as 60 ms frames, half-closed
after the last frame) and by chirp_3 batch Recognize. Prints WER (English) / CER (Mandarin) per
model, the streaming final latency after the half-close and the latest_short confidence per
sentence; rows go to <work>/results.jsonl.

  (set -a; . server/.env; set +a; GOOGLE_APPLICATION_CREDENTIALS="$GOOGLE_SA_FILE" \
     .venv/bin/python server/scripts/stt_accuracy.py [--work DIR])
"""
import argparse
import json
import os
import re
import statistics
import time
import unicodedata
import wave

from google.api_core.client_options import ClientOptions
from google.cloud import texttospeech
from google.cloud.speech_v2 import SpeechClient
from google.cloud.speech_v2.types import cloud_speech
from google.oauth2 import service_account

RATE = 16000
FRAME_BYTES = 1920  # 60 ms
LEAD_S = 0.3
TRAIL_S = 0.2
LANGS = ["en-US", "cmn-Hans-CN"]

ENGLISH = [
    "Turn on the living room lights.",
    "What time is it in Dublin?",
    "Is the kitchen light on?",
    "Set the bedroom temperature to twenty degrees.",
    "Will it rain tomorrow?",
    "Turn off all the lights downstairs.",
    "Remind me to call my mother at six.",
    "What's the weather like this weekend?",
    "Dim the hallway lights to fifty percent.",
    "Lock the front door.",
]
MANDARIN = [
    "打开客厅的灯",
    "把卧室的空调调到二十六度",
    "现在几点了",
    "明天都柏林会下雨吗",
    "关掉厨房的灯",
    "客厅的灯开着吗",
    "帮我设一个十分钟的定时器",
    "把音量调小一点",
    "今天的天气怎么样",
    "晚上十点提醒我关窗户",
]
VOICES = {"en": ("en-US", ["en-US-Chirp3-HD-Charon", "en-US-Chirp3-HD-Aoede"]),
          "zh": ("cmn-CN", ["cmn-CN-Chirp3-HD-Charon", "cmn-CN-Chirp3-HD-Aoede"])}

_EN_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_EN_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
_ZH_DIGITS = "零一二三四五六七八九"


def en_number(n):
    if n < 20:
        return _EN_ONES[n]
    if n < 100:
        return _EN_TENS[n // 10] + ("" if n % 10 == 0 else " " + _EN_ONES[n % 10])
    return str(n)


def zh_number(n):
    if n < 10:
        return _ZH_DIGITS[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return ("" if tens == 1 else _ZH_DIGITS[tens]) + "十" + ("" if ones == 0 else _ZH_DIGITS[ones])
    return "".join(_ZH_DIGITS[int(d)] for d in str(n))


def en_tokens(text):
    text = text.lower().replace("%", " percent").replace("°", " degrees").replace("’", "'")
    text = re.sub(r"(\d+):00", r"\1", text)
    text = re.sub(r"\d+", lambda m: " " + en_number(int(m.group())) + " ", text)
    text = re.sub(r"[^a-z' ]", " ", text)
    return text.split()


def zh_chars(text):
    text = re.sub(r"(\d+):00", r"\1点", text)
    text = re.sub(r"\d+", lambda m: zh_number(int(m.group())), text)
    return [c for c in text if not (unicodedata.category(c)[0] in "PZS" or c.isspace())]


def edits(ref, hyp):
    row = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, row[0] = row[0], i
        for j, h in enumerate(hyp, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (r != h))
    return row[-1]


def synthesize(tts, work, lang, index, text):
    path = os.path.join(work, f"{lang}_{index:02d}.wav")
    if not os.path.exists(path):
        code, voices = VOICES[lang]
        response = tts.synthesize_speech(
            input=texttospeech.SynthesisInput(text=text),
            voice=texttospeech.VoiceSelectionParams(name=voices[index % len(voices)], language_code=code),
            audio_config=texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.LINEAR16, sample_rate_hertz=RATE
            ),
        )
        with open(path, "wb") as f:
            f.write(response.audio_content)
    with wave.open(path) as w:
        assert w.getframerate() == RATE and w.getnchannels() == 1 and w.getsampwidth() == 2
        return w.readframes(w.getnframes())


def recognition_config(model):
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


def batch(client, recognizer, pcm):
    start = time.monotonic()
    response = client.recognize(
        request=cloud_speech.RecognizeRequest(
            recognizer=recognizer, config=recognition_config("chirp_3"), content=pcm
        ),
        timeout=15,
        retry=None,
    )
    ms = (time.monotonic() - start) * 1000
    texts = [r.alternatives[0].transcript.strip() for r in response.results if r.alternatives]
    lang = next((r.language_code for r in response.results if r.language_code), None)
    return {"text": " ".join(t for t in texts if t), "language": lang, "latency_ms": round(ms)}


def stream(client, recognizer, pcm):
    audio = b"\0" * int(RATE * 2 * LEAD_S) + pcm + b"\0" * int(RATE * 2 * TRAIL_S)
    mark = {}

    def requests():
        yield cloud_speech.StreamingRecognizeRequest(
            recognizer=recognizer,
            streaming_config=cloud_speech.StreamingRecognitionConfig(
                config=recognition_config("latest_short"),
                streaming_features=cloud_speech.StreamingRecognitionFeatures(interim_results=True),
            ),
        )
        t0 = time.monotonic()
        for i in range(0, len(audio), FRAME_BYTES):
            delay = t0 + (i // FRAME_BYTES) * 0.06 - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            yield cloud_speech.StreamingRecognizeRequest(audio=audio[i : i + FRAME_BYTES])
        mark["half_close"] = time.monotonic()

    finals, confidence, lang, final_at, interims, end_event = [], None, None, None, 0, False
    for response in client.streaming_recognize(requests=requests(), timeout=30):
        if response.speech_event_type == cloud_speech.StreamingRecognizeResponse.SpeechEventType.END_OF_SINGLE_UTTERANCE:
            end_event = True
        for result in response.results:
            if not result.alternatives:
                continue
            if not result.is_final:
                interims += 1
                continue
            final_at = time.monotonic()
            finals.append(result.alternatives[0].transcript.strip())
            confidence = result.alternatives[0].confidence
            lang = result.language_code or lang
    end = time.monotonic()
    half_close = mark.get("half_close", end)
    return {
        "text": " ".join(t for t in finals if t),
        "language": lang,
        "confidence": None if confidence is None else round(confidence, 3),
        "final_after_half_close_ms": None if final_at is None else round((final_at - half_close) * 1000),
        "interims": interims,
        "end_of_single_utterance": end_event,
        "all_audio_sent": "half_close" in mark,
    }


def score(lang, ref, hyp):
    split = en_tokens if lang == "en" else zh_chars
    r, h = split(ref), split(hyp)
    return edits(r, h), len(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default=os.path.join(
        os.environ.get("WEIXUE_VENDOR", os.path.expanduser("~/Projects/weixue-vendor")), "spikes", "stt-accuracy"))
    ap.add_argument("--location", default="us")
    args = ap.parse_args()
    os.makedirs(args.work, exist_ok=True)
    creds = service_account.Credentials.from_service_account_file(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
    tts = texttospeech.TextToSpeechClient(credentials=creds)
    client = SpeechClient(credentials=creds, client_options=ClientOptions(api_endpoint=f"{args.location}-speech.googleapis.com"))
    recognizer = f"projects/{creds.project_id}/locations/{args.location}/recognizers/_"
    rows = []
    with open(os.path.join(args.work, "results.jsonl"), "a", encoding="utf-8") as out:
        for lang, sentences in (("en", ENGLISH), ("zh", MANDARIN)):
            batch(client, recognizer, synthesize(tts, args.work, lang, 0, sentences[0]))
            for i, ref in enumerate(sentences):
                pcm = synthesize(tts, args.work, lang, i, ref)
                for model, run in (("latest_short", stream), ("chirp_3", batch)):
                    result = run(client, recognizer, pcm)
                    errors, length = score(lang, ref, result["text"])
                    row = {"lang": lang, "index": i, "model": model, "ref": ref, **result,
                           "errors": errors, "ref_len": length, "audio_s": round(len(pcm) / 2 / RATE, 2)}
                    rows.append(row)
                    out.write(json.dumps(row, ensure_ascii=False) + "\n")
                    print(json.dumps(row, ensure_ascii=False), flush=True)
    print("\n| Model | Language | Error rate | Exact | Confidence min / median | Final after half-close med / max (ms) | Batch latency med (ms) |")
    print("|---|---|---|---|---|---|---|")
    for model in ("latest_short", "chirp_3"):
        for lang in ("en", "zh"):
            sel = [r for r in rows if r["model"] == model and r["lang"] == lang]
            rate = sum(r["errors"] for r in sel) / sum(r["ref_len"] for r in sel)
            exact = sum(r["errors"] == 0 for r in sel)
            conf = [r["confidence"] for r in sel if r.get("confidence") is not None]
            lat = [r["final_after_half_close_ms"] for r in sel if r.get("final_after_half_close_ms") is not None]
            blat = [r["latency_ms"] for r in sel if "latency_ms" in r]
            print(f"| {model} | {lang} {'WER' if lang == 'en' else 'CER'} | {rate:.1%} | {exact}/{len(sel)} | "
                  f"{(f'{min(conf):.3f} / {statistics.median(conf):.3f}') if conf else '-'} | "
                  f"{(f'{statistics.median(lat):.0f} / {max(lat)}') if lat else '-'} | "
                  f"{(f'{statistics.median(blat):.0f}') if blat else '-'} |")


if __name__ == "__main__":
    main()
