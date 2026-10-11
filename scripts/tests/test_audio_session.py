"""Host tests for the idle wait of scripts/audio_session.py (stdlib unittest, no board)."""
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parent / "firmware" / "tools"))

try:
    import audio_session
except ImportError:  # no pyserial
    audio_session = None


class FakeDiag:
    """Console holding one state line; wait_for records its timeout and sees no further state change."""

    def __init__(self, state, age_s=0.0):
        self.lines = [(time.monotonic() - age_s, 41, f"StateMachine: State: idle -> {state}")]
        self.waits = []
        self.notes = []

    def mark(self):
        return len(self.lines)

    def snapshot(self, since=0, until=None):
        return list(self.lines[since:until])

    def find_all(self, pattern, since=0, until=None):
        return [(n, text) for _, n, text in self.lines[since:until] if pattern.search(text)]

    def wait_for(self, pattern, timeout, since=None):
        self.waits.append(timeout)
        return None

    def note(self, text):
        self.notes.append(text)


@unittest.skipIf(audio_session is None, "pyserial missing")
class WaitReadyTest(unittest.TestCase):
    def wait(self, state, age_s=0.0):
        diag = FakeDiag(state, age_s)
        session = audio_session.Session(SimpleNamespace(voice="Fenrir", no_audio=True), diag)
        return session.wait_ready(), diag

    def test_speaking_gets_90_s(self):
        verdict, diag = self.wait("speaking")
        self.assertEqual(audio_session.SPEAKING_WAIT_S, 90)
        self.assertAlmostEqual(diag.waits[0], 90, delta=0.5)
        self.assertEqual(verdict.cls, "not_idle")

    def test_other_states_get_30_s(self):
        for state in ("listening", "connecting"):
            verdict, diag = self.wait(state)
            self.assertEqual(audio_session.IDLE_WAIT_S, 30)
            self.assertAlmostEqual(diag.waits[0], 30, delta=0.5)
            self.assertEqual(verdict.cls, "not_idle")

    def test_idle_device_is_ready(self):
        verdict, diag = self.wait("idle", age_s=5)
        self.assertIsNone(verdict)
        self.assertEqual(diag.waits, [])


if __name__ == "__main__":
    unittest.main()
