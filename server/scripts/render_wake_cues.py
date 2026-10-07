"""Renders the wake cue clips with the configured Google TTS voice.

Runs inside the server container (needs its config and credentials):
  docker exec -i xiaozhi-esp32-server python - < server/scripts/render_wake_cues.py
  docker cp xiaozhi-esp32-server:/tmp/wake_cues/. server/overlay/config/assets/wake_cues/
"""
import asyncio
import io
import os

from google.cloud import texttospeech
from pydub import AudioSegment, silence

from config.config_loader import load_config

CUES = {"hmm": "Hmm?", "mm-hm": "Mm-hm?", "yes": "Yes?"}
OUT_DIR = "/tmp/wake_cues"

cfg = asyncio.run(load_config())
tts = cfg["TTS"][cfg["selected_module"]["TTS"]]
voice = tts.get("private_voice") or tts.get("voice_en") or f"en-US-Chirp3-HD-{tts.get('speaker') or 'Charon'}"
client = texttospeech.TextToSpeechClient.from_service_account_file(tts["credentials_file"])
os.makedirs(OUT_DIR, exist_ok=True)
for name, text in CUES.items():
    response = client.synthesize_speech(
        input=texttospeech.SynthesisInput(text=text),
        voice=texttospeech.VoiceSelectionParams(name=voice, language_code="en-US"),
        audio_config=texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.LINEAR16,
            sample_rate_hertz=24000,
            speaking_rate=float(tts.get("speaking_rate") or 1.0),
        ),
    )
    clip = AudioSegment.from_file(io.BytesIO(response.audio_content), format="wav")
    spans = silence.detect_nonsilent(clip, min_silence_len=50, silence_thresh=clip.dBFS - 25)
    if spans:
        clip = clip[max(0, spans[0][0] - 20) : min(len(clip), spans[-1][1] + 60)]
    clip.set_channels(1).fade_out(30).export(f"{OUT_DIR}/{name}.wav", format="wav")
    print(name, f"{len(clip)} ms")
