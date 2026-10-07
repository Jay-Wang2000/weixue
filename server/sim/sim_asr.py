"""ASR for the server/sim stack: utterance n returns line n (the last line after
that) of data/sim_transcripts.txt; rewriting the file restarts at line 1."""
from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType

PATH = "data/sim_transcripts.txt"


class ASRProvider(ASRProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__()
        self.interface_type = InterfaceType.LOCAL
        self.output_dir = config.get("output_dir", "tmp/")
        self.delete_audio_file = delete_audio_file
        self.content = None
        self.count = 0

    async def speech_to_text(self, opus_data, session_id, artifacts=None):
        content = open(PATH, encoding="utf-8").read()
        if content != self.content:
            self.content, self.count = content, 0
        lines = [l for l in content.splitlines() if l.strip()] or ["Yes."]
        text = lines[min(self.count, len(lines) - 1)]
        self.count += 1
        return text, None
