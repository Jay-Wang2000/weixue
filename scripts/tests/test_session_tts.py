"""Host tests for scripts/session_tts.py and the speech path of scripts/audio_session.py (stdlib
unittest). The smoke test synthesises one clip and is skipped without GOOGLE_APPLICATION_CREDENTIALS
and google-cloud-texttospeech."""
import io
import os
import re
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parent / "firmware" / "tools"))
import session_tts as st  # noqa: E402

try:
    import audio_session
except ImportError:  # no pyserial
    audio_session = None


def make_wav(frames=2400, rate=st.SAMPLE_RATE, channels=1):
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x01\x02" * frames * channels)
    return out.getvalue()


class VoiceMappingTest(unittest.TestCase):
    def test_speaker_maps_to_language_voices(self):
        self.assertEqual(st.voice_name("Fenrir", "What time is it?"), ("en-US-Chirp3-HD-Fenrir", "en-US"))
        self.assertEqual(st.voice_name("Fenrir", "现在几点了"), ("cmn-CN-Chirp3-HD-Fenrir", "cmn-CN"))

    def test_full_voice_name_keeps_its_speaker(self):
        self.assertEqual(st.voice_name("en-US-Chirp3-HD-Aoede", "Jarvis"), ("en-US-Chirp3-HD-Aoede", "en-US"))
        self.assertEqual(st.voice_name("en-US-Chirp3-HD-Aoede", "你好"), ("cmn-CN-Chirp3-HD-Aoede", "cmn-CN"))

    def test_rejects_a_say_voice_with_spaces_or_dashes(self):
        for bad in ("Daniel (Enhanced)", "en-US-Standard-A", ""):
            with self.assertRaises(ValueError):
                st.voice_name(bad, "Jarvis")

    def test_default_voice_is_a_chirp_speaker(self):
        self.assertEqual(st.voice_name(st.DEFAULT_VOICE, "Jarvis")[0], f"en-US-Chirp3-HD-{st.DEFAULT_VOICE}")


class CacheTest(unittest.TestCase):
    def test_key_is_stable_and_depends_on_voice_and_text(self):
        a = st.cache_key("en-US-Chirp3-HD-Fenrir", "Jarvis")
        self.assertEqual(a, st.cache_key("en-US-Chirp3-HD-Fenrir", "Jarvis"))
        self.assertNotEqual(a, st.cache_key("en-US-Chirp3-HD-Aoede", "Jarvis"))
        self.assertNotEqual(a, st.cache_key("en-US-Chirp3-HD-Fenrir", "Jarvis."))
        self.assertRegex(a, r"^[0-9a-f]{24}$")

    def test_key_depends_on_rate_and_pad(self):
        a = st.cache_key("v", "t")
        with mock.patch.object(st, "PAD_MS", 100):
            self.assertNotEqual(a, st.cache_key("v", "t"))
        with mock.patch.object(st, "SAMPLE_RATE", 16000):
            self.assertNotEqual(a, st.cache_key("v", "t"))

    def test_path_names_voice_and_key(self):
        path = st.clip_path("Fenrir", "Jarvis", "/c")
        name = "en-US-Chirp3-HD-Fenrir"
        self.assertEqual(path, f"/c/{name}-{st.cache_key(name, 'Jarvis')}.wav")

    def test_ensure_clip_synthesises_once(self):
        calls = []

        def synth(text, name, code):
            calls.append((text, name, code))
            return make_wav()

        with tempfile.TemporaryDirectory() as d:
            path, new = st.ensure_clip("Fenrir", "Jarvis", d, synth)
            self.assertTrue(new)
            path2, new2 = st.ensure_clip("Fenrir", "Jarvis", d, synth)
            self.assertEqual((path, False), (path2, new2))
            self.assertEqual(calls, [("Jarvis", "en-US-Chirp3-HD-Fenrir", "en-US")])
            self.assertEqual(os.listdir(d), [os.path.basename(path)])


class PadTest(unittest.TestCase):
    def test_silence_before_and_after(self):
        padded = st.pad_wav(make_wav(frames=2400), 250)
        with wave.open(io.BytesIO(padded)) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate()), (1, 2, 24000))
            pad = 24000 * 250 // 1000
            self.assertEqual(w.getnframes(), 2400 + 2 * pad)
            pcm = w.readframes(w.getnframes())
        self.assertEqual(pcm[: pad * 2], b"\0" * pad * 2)
        self.assertEqual(pcm[-pad * 2:], b"\0" * pad * 2)
        self.assertEqual(pcm[pad * 2: pad * 2 + 2], b"\x01\x02")

    def test_rejects_other_formats(self):
        with self.assertRaises(ValueError):
            st.pad_wav(make_wav(rate=16000))
        with self.assertRaises(ValueError):
            st.pad_wav(make_wav(channels=2))


class PlaybackTest(unittest.TestCase):
    def test_afplay_command(self):
        self.assertEqual(st.afplay_command("/c/x.wav"), ["afplay", "/c/x.wav"])

    @unittest.skipIf(audio_session is None, "pyserial missing")
    def test_say_plays_the_cached_clip_with_afplay(self):
        notes = []
        session = audio_session.Session(
            SimpleNamespace(voice="Fenrir", no_audio=False), SimpleNamespace(note=notes.append))
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(st, "CACHE_DIR", d), \
                mock.patch.object(st, "synthesize", lambda text, name, code: make_wav()), \
                mock.patch.object(audio_session.subprocess, "run") as run:
            session.say("Jarvis, what time is it?")
            session.say("Jarvis, what time is it?")
            want = os.path.join(d, os.listdir(d)[0])
            self.assertEqual(len(os.listdir(d)), 1)
        self.assertEqual(run.call_args_list, [mock.call(["afplay", want], check=True)] * 2)
        self.assertEqual(notes, ["say: Jarvis, what time is it?"] * 2)

    @unittest.skipIf(audio_session is None, "pyserial missing")
    def test_no_audio_session_skips_voice_items_without_synthesis(self):
        session = audio_session.Session(
            SimpleNamespace(voice="Fenrir", no_audio=True, count=1), SimpleNamespace(note=lambda t: None))
        with mock.patch.object(st, "ensure_clip") as ensure, mock.patch.object(audio_session.subprocess, "run") as run:
            session.run(["one_breath", "wake_cue", "long_reply", "barge_in"])
        ensure.assert_not_called()
        run.assert_not_called()
        self.assertEqual([r[2] for r in session.rows], ["skipped"] * 4)


@unittest.skipIf(audio_session is None, "pyserial missing")
class UtteranceTableTest(unittest.TestCase):
    def test_every_spoken_text_is_in_the_warm_list(self):
        texts = audio_session.all_utterances()
        self.assertEqual(len(texts), len(set(texts)))
        for text in audio_session.ASKS.values():
            self.assertIn(text, texts)
            self.assertIn(f"Jarvis, {text}", texts)
        for text in audio_session.PLAIN.values():
            self.assertIn(text, texts)

    def test_no_literal_utterances_in_items(self):
        src = (SCRIPTS / "audio_session.py").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"self\.(say|ask)\(\s*[\"']", src))


@unittest.skipUnless(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"), "no GOOGLE_APPLICATION_CREDENTIALS")
class SmokeTest(unittest.TestCase):
    def test_synthesises_one_short_clip(self):
        try:
            import google.cloud.texttospeech  # noqa: F401
        except ImportError:
            self.skipTest("google-cloud-texttospeech missing")
        with tempfile.TemporaryDirectory() as d:
            path, new = st.ensure_clip(st.DEFAULT_VOICE, "Jarvis", d)
            self.assertTrue(new)
            with wave.open(path) as w:
                self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate()), (1, 2, 24000))
                pad = 24000 * st.PAD_MS // 1000
                self.assertGreater(w.getnframes(), 2 * pad + 24000 // 4)
                pcm = w.readframes(w.getnframes())
            self.assertEqual(pcm[: pad * 2], b"\0" * pad * 2)
            self.assertNotEqual(pcm[pad * 2: pad * 2 + 4800], b"\0" * 4800)


if __name__ == "__main__":
    unittest.main()
