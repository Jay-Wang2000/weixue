"""server/sim entrypoint: Google TTS client replaced by a local tone generator (text containing
SIMFAIL raises InvalidArgument, SIMBREAK raises ServiceUnavailable after its first chunk), then
weixue_main."""
import math
import runpy
import struct
from types import SimpleNamespace

from google.api_core import exceptions as gexc
from google.cloud import texttospeech


def _tone(seconds, rate):
    return b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(int(seconds * rate)))


class ToneClient:
    def __init__(self, *args, **kwargs):
        self.transport = SimpleNamespace(close=lambda: None)

    @classmethod
    def from_service_account_file(cls, *args, **kwargs):
        return cls()

    def streaming_synthesize(self, requests, timeout=None):
        reqs = list(requests)
        rate = reqs[0].streaming_config.streaming_audio_config.sample_rate_hertz
        text = reqs[1].input.text
        if "SIMFAIL" in text:
            raise gexc.InvalidArgument("simulated synthesis failure")
        seconds = min(4.0, 0.06 * len(text))
        pcm = _tone(seconds, rate)
        step = rate * 2 // 5
        chunks = [SimpleNamespace(audio_content=pcm[i:i + step]) for i in range(0, len(pcm), step)]
        if "SIMBREAK" in text:
            return _break_after(chunks[:1])
        return iter(chunks)


def _break_after(chunks):
    yield from chunks
    raise gexc.ServiceUnavailable("simulated stream failure")


texttospeech.TextToSpeechClient = ToneClient
runpy.run_path("weixue_main.py", run_name="__main__")
