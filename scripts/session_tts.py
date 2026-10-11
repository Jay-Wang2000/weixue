"""Speech for the device session driver: Google Cloud Text-to-Speech (Chirp 3 HD) clips cached
as 24 kHz mono LINEAR16 wav files and played with afplay.

Credentials come from GOOGLE_APPLICATION_CREDENTIALS (scripts/audio_session.sh exports it from
GOOGLE_SA_FILE in the live server/.env).
"""
import hashlib
import io
import json
import os
import re
import wave

SAMPLE_RATE = 24000
PAD_MS = 250
DEFAULT_VOICE = "Fenrir"
CACHE_DIR = os.path.join(
    os.environ.get("WEIXUE_VENDOR", os.path.expanduser("~/Projects/weixue-vendor")), "session-clips")
CHIRP = "Chirp3-HD"
LANGUAGES = {"en": "en-US", "zh": "cmn-CN"}
CJK_RE = re.compile(r"[㐀-鿿]")
FULL_NAME_RE = re.compile(r"^[a-z]{2,3}-[A-Z]{2}-Chirp3-HD-(\w+)$")


def language_of(text):
    return "zh" if CJK_RE.search(text) else "en"


def voice_name(voice, text):
    """Returns (Google voice name, language code) for `text`. `voice` is a Chirp 3 HD speaker
    (`Fenrir`) or a full voice name (`en-US-Chirp3-HD-Fenrir`); the language comes from the text
    and the speaker is shared between languages."""
    m = FULL_NAME_RE.match(voice)
    speaker = m.group(1) if m else voice
    if not re.fullmatch(r"\w+", speaker):
        raise ValueError(f"not a Chirp 3 HD speaker or voice name: {voice!r}")
    code = LANGUAGES[language_of(text)]
    return f"{code}-{CHIRP}-{speaker}", code


def cache_key(name, text):
    blob = json.dumps([name, text, SAMPLE_RATE, PAD_MS], ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def clip_path(voice, text, cache_dir=None):
    name, _ = voice_name(voice, text)
    return os.path.join(cache_dir or CACHE_DIR, f"{name}-{cache_key(name, text)}.wav")


def pad_wav(wav_bytes, pad_ms=PAD_MS):
    """Returns `wav_bytes` (mono 16-bit wav) with pad_ms of silence before and after."""
    with wave.open(io.BytesIO(wav_bytes)) as r:
        params = r.getparams()
        pcm = r.readframes(r.getnframes())
    if (params.nchannels, params.sampwidth, params.framerate) != (1, 2, SAMPLE_RATE):
        raise ValueError(f"unexpected wav format {params[:3]}")
    pad = b"\0" * (SAMPLE_RATE * pad_ms // 1000 * 2)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pad + pcm + pad)
    return out.getvalue()


def synthesize(text, name, code):
    """One synthesize_speech call; returns the padded wav bytes."""
    from google.cloud import texttospeech  # noqa: PLC0415
    client = texttospeech.TextToSpeechClient()
    try:
        r = client.synthesize_speech(
            input=texttospeech.SynthesisInput(text=text),
            voice=texttospeech.VoiceSelectionParams(name=name, language_code=code),
            audio_config=texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.LINEAR16, sample_rate_hertz=SAMPLE_RATE),
            timeout=30,
        )
    finally:
        client.transport.close()
    return pad_wav(r.audio_content)


def ensure_clip(voice, text, cache_dir=None, synth=None):
    """Returns (path, synthesised): the cached clip for text, synthesising it first when missing."""
    cache_dir = cache_dir or CACHE_DIR
    path = clip_path(voice, text, cache_dir)
    if os.path.exists(path):
        return path, False
    name, code = voice_name(voice, text)
    data = (synth or synthesize)(text, name, code)
    os.makedirs(cache_dir, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return path, True


def afplay_command(path):
    return ["afplay", path]
