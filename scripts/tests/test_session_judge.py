"""Host tests for scripts/session_judge.py and the items of scripts/audio_session.py (stdlib
unittest). Fixtures under fixtures/ are excerpts of recorded session logs (docs/logs/session-*):
`<name>-serial.log` (diag_inject.py log format) and `<name>-server.log` (redacted docker logs,
server clock UTC+1). Driver tests replay a serial fixture through Session with afplay, docker logs
and the clock mocked."""
import datetime
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
FIX = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parent / "firmware" / "tools"))
import session_judge as j  # noqa: E402

os.environ["WEIXUE_SERVER_SSH"] = ""  # docker stays local whatever the live server/.env names

try:
    import audio_session
except ImportError:  # no pyserial
    audio_session = None

SERIAL_RE = re.compile(r"\[\s*([\d.]+) ([\d:]+)Z\] (.*)$")
ACK_RE = re.compile(r"DiagConsole: (ok|error) (.*?): (.*)$")
DAY = datetime.date(2026, 10, 11)
LIGHTHOUSE = re.compile(r"lighthouse", re.I)


def epoch(stamp, hours=0):
    t = datetime.datetime.combine(DAY, datetime.time.fromisoformat(stamp), datetime.timezone.utc)
    return (t - datetime.timedelta(hours=hours)).timestamp()


def server_time(line):
    return epoch(line[7:15], hours=1)


def load(name):
    """(lines, plays, wall clock of each log line number, server lines) of a fixture."""
    raw = (FIX / f"{name}-serial.log").read_text(encoding="utf-8")
    lines, plays = j.parse_serial_log(raw)
    wall = {}
    for n, r in enumerate(raw.splitlines(), 1):
        m = SERIAL_RE.match(r)
        if m:
            wall[n] = epoch(m.group(2))
    path = FIX / f"{name}-server.log"
    server = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return lines, plays, wall, server


def trials(name, starts):
    """[(lines, plays, server)] split at each play whose text is in starts."""
    lines, plays, wall, server = load(name)
    marks = [p for p in plays if p.text in starts]
    out = []
    for i, p in enumerate(marks):
        nxt = marks[i + 1] if i + 1 < len(marks) else None
        within = lambda n: n >= p.n and (nxt is None or n < nxt.n)  # noqa: E731
        t0, t1 = wall[p.n], wall[nxt.n] if nxt else float("inf")
        out.append(([l for l in lines if within(l.n)], [q for q in plays if within(q.n)],
                    [s for s in server if t0 <= server_time(s) < t1]))
    return out


def classes(verdicts):
    return [v.cls for v in verdicts]


class WakeCueTest(unittest.TestCase):
    def test_0748_preroll_turns_are_their_own_class(self):
        got = [j.judge_wake_cue(*t) for t in trials("wake_cue-0748", {"Jarvis"})]
        self.assertEqual(classes(got), ["preroll_turn"] * 4 + ["pass"] + ["preroll_turn"] * 2 + ["cue_late"] +
                         ["preroll_turn"] * 4)
        self.assertIn("pre-roll became a turn: 'service'", got[0].reason)
        self.assertIn("'Yes.' -> 'Yes'", got[0].reason)
        self.assertIn("'Jervis'", got[2].reason)
        self.assertIn("cue `Mm-hm?`", got[4].reason)
        self.assertIn("'Yes.' not played (cue 14 s after the wake clip)", got[7].reason)
        self.assertNotIn("cue missing", " ".join(v.reason for v in got))

    def test_0655_cue_played_but_yes_unanswered_is_not_cue_missing(self):
        got = [j.judge_wake_cue(*t) for t in trials("wake_cue-0655", {"Jarvis"})]
        self.assertEqual(classes(got).count("no_answer"), 8)
        self.assertEqual(classes(got).count("preroll_turn"), 3)
        self.assertEqual(classes(got)[10], "no_wake")
        self.assertNotIn("cue_missing", classes(got))
        self.assertIn("'Yes.' got no transcript", got[1].reason)
        self.assertEqual({re.search(r"'(.+?)' L", v.reason).group(1) for v in got if v.cls == "preroll_turn"},
                         {"charters", "Chavez", "下载"})

    def test_0917_yes_dropped_during_the_preroll_reply(self):
        (lines, plays, server), = trials("wake_cue-0917", {"Jarvis"})
        v = j.judge_wake_cue(lines, plays, server)
        self.assertEqual(v.cls, "preroll_turn")
        self.assertRegex(v.reason, r"^cue `Yes\?` L\d+; pre-roll became a turn: 'service' L\d+; 'Yes.' got no "
                         r"transcript; server dropped 'Yes.' during playback$")

    def test_0719_three_cues_answered(self):
        got = [j.judge_wake_cue(*t) for t in trials("wake_cue-0719", {"Jarvis"})]
        passed = {j.cue_name(lines, server) for (lines, _, server), v in
                  zip(trials("wake_cue-0719", {"Jarvis"}), got) if v.ok}
        self.assertEqual(passed, set(j.CUES))

    def test_cue_missing_only_without_device_and_server_cue(self):
        lines, plays, server = trials("wake_cue-0748", {"Jarvis"})[0]
        no_dev = [l for l in lines if not j.CUE_RE.search(l.text)]
        self.assertEqual(j.cue_name(no_dev, server), "Yes?")
        self.assertNotEqual(j.judge_wake_cue(no_dev, plays, server).cls, "cue_missing")
        no_srv = [s for s in server if not j.SERVER_CUE_RE.search(s)]
        v = j.judge_wake_cue(no_dev, plays, no_srv)
        self.assertEqual(v.cls, "cue_missing")
        self.assertIn("no cue on device or server", v.reason)
        v = j.judge_wake_cue(no_dev, plays, ["... [weixue_patches]-INFO-wake cue skipped: user speaking, abort or newer turn"])
        self.assertIn("server: skipped, user speaking", v.reason)
        self.assertIn("server log unavailable", j.judge_wake_cue(no_dev, plays, None).reason)


class DoubleTapTest(unittest.TestCase):
    def test_0655_no_reply_trials_fail_with_a_reason(self):
        asks = {"tell me a long story about a lighthouse keeper.", "Jarvis, tell me a long story about a lighthouse keeper."}
        got = [j.judge_double_tap(lines, LIGHTHOUSE) for lines, _, _ in trials("double_tap-0655", asks)]
        self.assertEqual(len(got), 10)
        self.assertEqual(sum(bool(v.ok) for v in got), 3)
        for v in got:
            if not v.ok:
                self.assertEqual(v.cls, "no_transcript")
                self.assertIn("tap not sent", v.reason)
        self.assertIn("(not the request)", got[0].reason)
        for v in got:
            if v.ok:
                self.assertRegex(v.reason, r"reply L\d+; tap L\d+, abort L\d+, speaking -> listening L\d+$")

    def test_tap_without_abort_and_reply_ended(self):
        asks = {"Jarvis, tell me a long story about a lighthouse keeper."}
        lines = next(lines for lines, _, _ in trials("double_tap-0655", asks)
                     if j.judge_double_tap(lines, LIGHTHOUSE).ok)
        no_abort = [l for l in lines if not j.ABORT_RE.search(l.text)]
        self.assertEqual(j.judge_double_tap(no_abort, LIGHTHOUSE).cls, "no_abort")
        reply = j.first(lines, j.REPLY_RE)
        tap = j.first(lines, j.injected_re("double_tap"))
        cut = [l for l in lines if l.n < tap.n] + [j.Line(tap.n, tap.t, "I (1) StateMachine: State: speaking -> listening")]
        v = j.judge_double_tap(cut, LIGHTHOUSE)
        self.assertEqual(v.cls, "reply_ended")
        self.assertIn("tap not sent", v.reason)
        self.assertGreater(tap.n, reply.n)


class LongReplyTest(unittest.TestCase):
    def test_no_transcript_is_not_zero_seconds(self):
        lines, _, _, server = load("long_reply-0655")
        v = j.judge_long_reply(lines, server, LIGHTHOUSE)
        self.assertEqual((v.ok, v.cls), (False, "no_transcript"))
        self.assertRegex(v.reason, r"^wake L\d+, no transcript$")

    def test_no_wake(self):
        lines, plays, _, server = load("long_reply-0748")
        ask = next(p for p in plays if "lighthouse" in p.text)
        v = j.judge_long_reply([l for l in lines if l.n > ask.n], server, LIGHTHOUSE)
        self.assertEqual(v.reason, "no `Wake word detected`, no transcript")

    def test_duration_from_tts_start_to_stop(self):
        lines, plays, _, server = load("long_reply-0748")
        v = j.judge_long_reply(lines, server, LIGHTHOUSE, min_s=5)
        self.assertEqual(v.cls, "aborted")
        self.assertRegex(v.reason, r"TTS start L\d+ -> stop L\d+ \(listening\) 10 s; device aborts 0, server aborts 1")
        v = j.judge_long_reply(lines, [], LIGHTHOUSE, min_s=5)
        self.assertEqual(v.cls, "pass")
        self.assertEqual(j.judge_long_reply(lines, [], LIGHTHOUSE).cls, "short")
        self.assertEqual(j.judge_long_reply(lines, None, LIGHTHOUSE, min_s=5).cls, "no_server_log")
        reply = j.first(lines, j.REPLY_RE, j.first(lines, j.STT_RE))
        v = j.judge_long_reply([l for l in lines if l.n <= reply.n], [], LIGHTHOUSE)
        self.assertEqual(v.cls, "no_tts_stop")

    def test_server_abort_counts_once_per_abort(self):
        _, _, _, server = load("long_reply-0748")
        self.assertEqual(sum("Abort message received" in s for s in server), 2)
        self.assertEqual(j.server_count(server, j.SERVER_ABORT_RE), 1)


class BargeInTest(unittest.TestCase):
    ARGS = (re.compile("dragon", re.I), "Stop. What is two plus two?", re.compile("two plus two", re.I),
            re.compile(r"\bfour\b", re.I))

    def window(self):
        lines, plays, wall, server = load("barge_in-0748")
        barge = next(p for p in plays if p.text == self.ARGS[1])
        return lines, plays, [s for s in server if server_time(s) >= wall[barge.n] - 1]

    def test_flag_0_expects_no_abort(self):
        lines, plays, server = self.window()
        v = j.judge_barge_in(lines, plays, server, 0, *self.ARGS)
        self.assertEqual(v.cls, "aborted")
        self.assertIn("VOICE_BARGE_IN=0", v.reason)
        self.assertIn("abort device none, server 1 (expected none)", v.reason)

    def test_flag_0_reply_played_out(self):
        lines, plays, server = self.window()
        quiet = [s for s in server if not j.SERVER_ABORT_RE.search(s)]
        drop = "x [weixue_patches]-INFO-barge-in disabled: dropped utterance during playback: 'Stop.'"
        barge = next(p for p in plays if p.text == self.ARGS[1])
        playing = [l for l in lines if not (l.n > barge.n and j.STT_RE.search(l.text))]
        v = j.judge_barge_in(playing, plays, quiet + [drop], 0, *self.ARGS)
        self.assertEqual(v.cls, "pass")
        self.assertRegex(v.reason, r"played out: TTS stop L\d+ \d+ s later; server dropped 'Stop.'")
        end = j.first(playing, j.SPEAK_END_RE, barge)
        turn = j.Line(end.n - 0.5, end.t, "I (1) Application: >> Stop. What is two plus two?")
        v = j.judge_barge_in(sorted(playing + [turn]), plays, quiet, 0, *self.ARGS)
        self.assertEqual(v.cls, "turn_during_playback")
        self.assertIn("'Stop. What is two plus two?'", v.reason)

    def test_flag_1_judges_abort_and_new_turn(self):
        lines, plays, server = self.window()
        v = j.judge_barge_in(lines, plays, server, 1, *self.ARGS)
        self.assertEqual(v.cls, "wrong_transcript")
        self.assertIn("server 1; heard 'der ist die'", v.reason)
        quiet = [s for s in server if not j.SERVER_ABORT_RE.search(s)]
        self.assertEqual(j.judge_barge_in(lines, plays, quiet, 1, *self.ARGS).cls, "no_abort")
        fixed = [l._replace(text=l.text.replace("der ist die", "Stop. What is two plus two?")
                            .replace("Sorry, I didn't catch that", "Two plus two is four")) for l in lines]
        self.assertEqual(j.judge_barge_in(fixed, plays, server, 1, *self.ARGS).cls, "pass")

    def test_no_reply_and_phrase_not_played(self):
        lines, plays, server = self.window()
        stt = j.first(lines, j.STT_RE)
        v = j.judge_barge_in([l for l in lines if l.n <= stt.n], plays, server, 0, *self.ARGS)
        self.assertEqual(v.cls, "no_reply")
        self.assertIn("nothing to interrupt", v.reason)
        v = j.judge_barge_in(lines, [p for p in plays if p.text != self.ARGS[1]], server, 0, *self.ARGS)
        self.assertEqual(v.cls, "reply_ended")


class PickUpTest(unittest.TestCase):
    def test_injected_pick_up_after_dim(self):
        lines, plays, _, _ = load("pick_up-0655")
        first_trial = [l for l in lines if l.n < j.first(lines, j.DIM_RE, j.first(lines, j.UNDIM_RE)).n]
        v = j.judge_pick_up(first_trial, plays)
        self.assertEqual(v.cls, "pass")
        self.assertRegex(v.reason, r"gesture L\d+, brightness 75 L\d+")
        self.assertEqual(j.unprompted_pick_ups(lines, plays), [])
        self.assertEqual(j.judge_pick_up([], [], dimmed=False).cls, "no_dim")

    def test_speaker_vibration_is_not_counted(self):
        lines, plays, _, _ = load("wake_cue-0719")
        found = j.unprompted_pick_ups(lines, plays)
        self.assertEqual([d is not None for _, d in found], [True, False, False])
        vib = found[0][0]
        v = j.judge_pick_up([l for l in lines if vib.n - 20 < l.n <= vib.n], plays)
        self.assertEqual((v.ok, v.cls), (None, "speaker_vibration"))
        self.assertIn("1.0 s after a playback start; not counted", v.reason)
        other = found[1][0]
        v = j.judge_pick_up([l for l in lines if vib.n < l.n <= other.n], plays)
        self.assertEqual((v.ok, v.cls), (False, "false_positive"))
        self.assertEqual(j.vibration_summary(lines, plays),
                         f"unprompted pick_up in the session: 3 (speaker_vibration 1: L{vib.n}; "
                         f"other 2: L{other.n} L{found[2][0].n})")

    def test_mac_clip_start_counts_as_playback(self):
        line = j.Line(10, 100.4, "I (1) Board: IMU gesture: pick_up (count 1)")
        self.assertAlmostEqual(j.unprompted_pick_ups([line], [j.Play(9, 100.0, "Jarvis")])[0][1], 0.4)
        self.assertIsNone(j.unprompted_pick_ups([line], [j.Play(9, 99.0, "Jarvis")])[0][1])


class BedsideTest(unittest.TestCase):
    NIGHT, AUTO = "switch the screen to bedside mode.", "set the bedside screen back to automatic."

    def test_0655_no_transcript(self):
        lines, plays, _, _ = load("bedside_voice-0655")
        v = j.judge_bedside(lines, plays, self.NIGHT, self.AUTO, re.compile("bedside"), re.compile("auto"))
        self.assertEqual(v.cls, "no_transcript")
        self.assertEqual(v.reason, "night: no transcript, no tool call (no hint), no bedside page; "
                         "auto: no transcript, no tool call (no hint)")

    def test_events_in_order(self):
        lines, plays, _, _ = load("bedside_voice-0655")
        night = next(p for p in plays if p.text.endswith(self.NIGHT))
        auto = next(p for p in plays if p.text.endswith(self.AUTO))
        add = [j.Line(night.n + 0.1, night.t + 2, "I (1) Application: >> Switch the screen to bedside mode"),
               j.Line(night.n + 0.2, night.t + 4, "I (1) FaceDisplay: bedside hint=night local=23:10"),
               j.Line(auto.n - 0.3, auto.t - 3, "I (1) FaceDisplay: bedside=on reason=hint local=23:10 hint=1"),
               j.Line(auto.n - 0.2, auto.t - 2, "I (1) FaceDisplay: bedside=off reason=touch local=23:10 hint=1"),
               j.Line(auto.n + 0.1, auto.t + 2, "I (1) Application: >> Set the bedside screen back to automatic"),
               j.Line(auto.n + 0.2, auto.t + 4, "I (1) FaceDisplay: bedside hint=auto local=23:11")]
        v = j.judge_bedside(sorted(lines + add, key=lambda l: l.n), plays, self.NIGHT, self.AUTO,
                            re.compile("bedside"), re.compile("auto"))
        self.assertEqual(v.cls, "pass")
        self.assertIn("hint=night", v.reason)
        self.assertIn("tap off", v.reason)


def same_note(recorded, text):
    """A driver note matches the recorded one with or without the wake word before the request."""
    def bare(note):
        return note.replace("say: Jarvis, ", "say: ", 1)
    return recorded == text or bare(recorded) == bare(text)


class Replay:
    """A recorded serial fixture played back to Session: a clip or diag send moves the cursor to the
    recorded note of the same text; a wait returns the next matching line within its timeout and
    never runs past the recorded driver's next action; sleep advances the clock. `written` keeps
    every note the driver wrote."""

    def __init__(self, name):
        self.lines, self.notes, self.written = [], [], []
        self.base = None
        for n, raw in enumerate((FIX / f"{name}-serial.log").read_text(encoding="utf-8").splitlines(), 1):
            m = SERIAL_RE.match(raw)
            t, body = float(m.group(1)), m.group(3)
            if self.base is None:
                self.base = epoch(m.group(2)) - t
            if body.startswith("### "):
                if body.startswith(("### say: ", "### send ")):
                    self.notes.append((len(self.lines), t, n, body[4:]))
            else:
                self.lines.append((t, n, body))
        self.cursor, self.now, self.next_note = 0, self.lines[0][0], 0

    def _cap(self):
        if self.next_note < len(self.notes):
            idx, t, _, _ = self.notes[self.next_note]
            return idx, t
        return len(self.lines), float("inf")

    def _run_to(self, deadline):
        cap_idx, _ = self._cap()
        while self.cursor < cap_idx and self.lines[self.cursor][0] <= deadline:
            self.cursor += 1
        self.now = max(self.now, deadline)

    def mark(self):
        return self.cursor

    def note(self, text):
        self.written.append(text)
        for k in range(self.next_note, len(self.notes)):
            idx, t, n, body = self.notes[k]
            if same_note(body, text):
                self.next_note = k + 1
                self.cursor, self.now = max(self.cursor, idx), max(self.now, t)
                return n
        return self.lines[self.cursor - 1][1] + 0.5 if self.cursor else 0.5

    def send(self, line, timeout=3.0):
        n = self.note(f"send {line}")
        hit = self.wait_for(rf"DiagConsole: (ok|error) {re.escape(line)}: (.*)$", timeout, self.cursor)
        return (hit[2].group(1) == "ok", hit[2].group(2), hit[0]) if hit else (False, "no answer", n)

    def wait_for(self, pattern, timeout, since=None):
        rx = re.compile(pattern) if isinstance(pattern, str) else pattern
        cap_idx, cap_t = self._cap()
        deadline = min(self.now + timeout, cap_t)
        i = self.cursor if since is None else since
        while i < min(len(self.lines), cap_idx):
            t, n, text = self.lines[i]
            if i >= self.cursor and t > deadline:
                break
            m = rx.search(text)
            if m:
                self.cursor, self.now = max(self.cursor, i + 1), max(self.now, t)
                return n, text, m, t, i + 1
            i += 1
        self._run_to(deadline)
        return None

    def sleep(self, seconds):
        self._run_to(min(self.now + seconds, self._cap()[1]))

    def find_all(self, pattern, since=0, until=None):
        rx = re.compile(pattern) if isinstance(pattern, str) else pattern
        return [(n, text) for _, n, text in self.lines[since:self.cursor if until is None else until] if rx.search(text)]

    def snapshot(self, since=0, until=None):
        return list(self.lines[since:self.cursor if until is None else until])


def replay_item(name, item, barge_in=0, count=10, idle_wait=True, agent=None, **extra):
    """Runs one Session item against Replay(name) with afplay, docker logs and the clock mocked;
    returns (session, replay, [(command, replay clock at the call)]). agent replaces the
    agent-service client and sees the replay clock."""
    replay = Replay(name)
    path = FIX / f"{name}-server.log"
    server = [(server_time(s), s) for s in (path.read_text(encoding="utf-8").splitlines() if path.exists() else [])]
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, replay.now))
        if cmd[0] == "docker":
            since, until = float(cmd[cmd.index("--since") + 1]), float(cmd[cmd.index("--until") + 1])
            out = "\n".join(s for t, s in server if since <= t <= until)
            return subprocess.CompletedProcess(cmd, 0, out, "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    clock = SimpleNamespace(sleep=replay.sleep, monotonic=lambda: replay.now,
                            time=lambda: replay.base + replay.now)
    ns = SimpleNamespace(voice="Fenrir", no_audio=False, count=count, barge_in=barge_in, **extra)
    session = audio_session.Session(ns, replay)
    if agent is not None:
        agent.replay = replay
        session.agent = agent
    if not idle_wait:
        session.wait_ready = lambda *a, **k: None
    with mock.patch.object(audio_session, "time", clock), \
            mock.patch.object(audio_session.subprocess, "run", run), \
            mock.patch.object(audio_session.session_tts, "ensure_clip", lambda v, t: ("/c/x.wav", False)):
        getattr(session, f"item_{item}")()
    return session, replay, calls


@unittest.skipIf(audio_session is None, "pyserial missing")
class DriverReplayTest(unittest.TestCase):
    def run_item(self, name, item, barge_in=0, count=10, idle_wait=True, **extra):
        """The item's table row; idle_wait=False replays a log recorded before the driver waited for
        idle (requests said into the open listening window)."""
        session, _, calls = replay_item(name, item, barge_in=barge_in, count=count, idle_wait=idle_wait, **extra)
        self.assertEqual(any(cmd[0] == "afplay" for cmd, _ in calls), item != "pick_up")
        return session.rows[-1]

    def test_wake_cue_0748(self):
        item, count, verdict, _, reason, _ = self.run_item("wake_cue-0748", "wake_cue")
        self.assertEqual((item, count, verdict), ("wake_cue", "1/3", "FAIL"))
        self.assertTrue(reason.startswith("preroll_turn "), reason)
        self.assertIn("t1 preroll_turn: [Yes?] cue `Yes?`", reason)
        self.assertIn("Hmm? not answered; Mm-hm? ok; Yes? not answered (12 wakes)", reason)
        self.assertNotIn("missing", reason)

    def test_wake_cue_0655(self):
        _, count, verdict, _, reason, _ = self.run_item("wake_cue-0655", "wake_cue")
        self.assertEqual((count, verdict), ("0/3", "FAIL"))
        self.assertIn("preroll_turn 3, no_answer 8, no_wake 1", reason)
        self.assertIn("no_wake 1", reason)
        self.assertNotIn("cue_missing", reason)

    def test_double_tap_0655(self):
        _, count, verdict, _, reason, _ = self.run_item("double_tap-0655", "double_tap", idle_wait=False)
        self.assertEqual((count, verdict), ("3/10", "FAIL"))
        self.assertRegex(reason, r"^(pass|no_transcript) \d+, (pass|no_transcript) \d+; t1 ")
        self.assertIn("no_transcript 7", reason)
        self.assertEqual(reason.count("tap not sent"), 7)

    def test_long_reply_wake_then_request(self):
        session, replay, calls = replay_item("long_reply-twostep", "long_reply")
        _, count, verdict, _, reason, _ = session.rows[-1]
        said = [w[len("say: "):] for w in replay.written if w.startswith("say: ")]
        self.assertEqual(said, ["Jarvis", audio_session.ASKS["long_reply"]])
        self.assertEqual((count, verdict), ("1/1", "pass"), reason)
        self.assertRegex(reason, r"^pass: heard 'Tell me a story about a lighthouse keeper that t...' L\d+; "
                         r"TTS start L\d+ -> stop L\d+ \(listening\) 58 s; device aborts 0, server aborts 0$")

    def test_barge_in_0748(self):
        _, _, verdict, result, reason, _ = self.run_item("barge_in-0748", "barge_in")
        self.assertEqual(verdict, "FAIL")
        self.assertIn("VOICE_BARGE_IN=0: phrase during the reply -> no abort, reply plays out", result)
        self.assertIn("aborted: VOICE_BARGE_IN=0", reason)
        _, _, _, result, reason, _ = self.run_item("barge_in-0748", "barge_in", barge_in=1)
        self.assertIn("VOICE_BARGE_IN=1: phrase during the reply -> abort, new turn answered", result)
        self.assertIn("wrong_transcript: VOICE_BARGE_IN=1", reason)
        self.assertIn("server 1; heard 'der ist die'", reason)

    def test_barge_in_0917_flag_0_played_out(self):
        _, count, verdict, _, reason, _ = self.run_item("barge_in-0917", "barge_in")
        self.assertEqual((count, verdict), ("1/1", "pass"))
        self.assertRegex(reason, r"^pass: VOICE_BARGE_IN=0; heard 'Jarvis, tell me a long story about a dragon' L\d+, "
                         r"reply L\d+; phrase L\d+; abort device none, server 0; played out: TTS stop L\d+ \d+ s later; "
                         r"server dropped \"I don't know.\", '100'$")

    def test_double_tap_0917(self):
        _, count, verdict, _, reason, _ = self.run_item("double_tap-0917", "double_tap", count=3, idle_wait=False)
        self.assertEqual((count, verdict), ("2/3", "FAIL"))
        self.assertIn("t1 no_transcript: no `Wake word detected`, no transcript; tap not sent", reason)

    def test_pick_up_0655(self):
        item, count, verdict, _, reason, _ = self.run_item("pick_up-0655", "pick_up", count=2)
        self.assertEqual((item, count, verdict), ("pick_up", "2/2", "pass"))
        self.assertRegex(reason, r"^pass 2; t1 pass: gesture L\d+, brightness 75 L\d+; t2 pass: ")
        self.assertIn("unprompted pick_up in the session: 0", reason)

    def test_report_has_a_reason_column(self):
        import tempfile
        session = SimpleNamespace(rows=[("x", "1/1", "pass", "r | s", "t1 pass: a | b", "1, 2")], barge_in=0)
        args = SimpleNamespace(no_audio=False, voice="Fenrir", build_info="abc diag")
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "s.md"
            audio_session.write_report(str(out), args, session, datetime.datetime(2026, 10, 11),
                                       str(Path(d) / "s-serial.log"), None)
            text = out.read_text()
        self.assertIn("| # | Item | Count | Verdict | Result | Reason | Evidence |", text)
        self.assertIn("| 1 | x | 1/1 | pass | r \\| s | t1 pass: a \\| b | 1, 2 |", text)
        self.assertIn("VOICE_BARGE_IN=0", text)

@unittest.skipIf(audio_session is None, "pyserial missing")
class IdleWaitTest(unittest.TestCase):
    """Wake clips start only on an idle device, SETTLE_S after its last state change (fixtures
    idle_wait-*: synthetic serial logs)."""

    def clips(self, calls):
        return [t for cmd, t in calls if cmd[0] == "afplay"]

    def idle_line(self, replay, after=0.0):
        return next(t for t, _, text in replay.lines if t >= after and audio_session.IDLE_RE.search(text))

    def test_idle_reached_then_settle(self):
        session, replay, calls = replay_item("idle_wait-reached", "one_breath", count=1)
        idle = self.idle_line(replay)
        self.assertEqual(len(self.clips(calls)), 1)
        self.assertGreaterEqual(self.clips(calls)[0], idle + audio_session.SETTLE_S)
        self.assertTrue(any(w.startswith("idle wait: idle L") for w in replay.written), replay.written)
        self.assertEqual(session.rows[-1][1:3], ("1/1", "pass"))

    def test_settle_restarts_when_the_device_leaves_idle(self):
        session, replay, calls = replay_item("idle_wait-settle", "one_breath", count=1)
        second_idle = self.idle_line(replay, after=6.0)
        self.assertEqual(len(self.clips(calls)), 1)
        self.assertGreaterEqual(self.clips(calls)[0], second_idle + audio_session.SETTLE_S)
        self.assertEqual(session.rows[-1][2], "pass")

    def test_settle_restarts_on_pose_change_and_screen_snapshot(self):
        session, replay, calls = replay_item("idle_wait-pose", "one_breath", count=1)
        snapshot = next(t for t, _, text in replay.lines if "diag lit=" in text)
        self.assertGreaterEqual(self.clips(calls)[0], snapshot + audio_session.SETTLE_S)
        self.assertEqual(session.rows[-1][2], "pass")

    def test_timeout_counts_per_state(self):
        session, replay, calls = replay_item("idle_wait-long", "one_breath", count=1)
        self.assertGreaterEqual(self.clips(calls)[0], 45.0 + audio_session.SETTLE_S)
        self.assertEqual(session.rows[-1][2], "pass")

    def test_timeout_records_not_idle_without_a_clip(self):
        session, replay, calls = replay_item("idle_wait-timeout", "long_reply")
        item, count, verdict, _, reason, _ = session.rows[-1]
        self.assertEqual((item, count, verdict), ("long_reply", "0/1", "FAIL"))
        self.assertRegex(reason, r"^not_idle: device listening since L2, not idle after 30 s; wake clip not played$")
        self.assertEqual(self.clips(calls), [])
        self.assertIn("idle wait: not idle after 30 s (state listening)", replay.written)

    def test_every_voice_item_waits(self):
        for item in ("one_breath", "wake_cue", "long_reply", "barge_in", "bedside_voice", "double_tap"):
            session, _, calls = replay_item("idle_wait-timeout", item, count=1)
            self.assertEqual(self.clips(calls), [], item)
            self.assertIn("not_idle", session.rows[-1][4], item)

    def test_unknown_state_uses_the_dump(self):
        session, replay, calls = replay_item("idle_wait-dump", "one_breath", count=1)
        dump = next(t for t, _, text in replay.lines if "ok diag dump" in text)
        self.assertGreaterEqual(self.clips(calls)[0], dump + audio_session.SETTLE_S)
        self.assertEqual(session.rows[-1][2], "pass")


class FakeAgent:
    """agent-service API for the earcons item: the hold becomes approvable at replay time `ready`
    (the task already reports done after 20 s, its hold still open); the trace reports the approved
    call with `status`. done_at: the hold is gone at that time, its call ended unknown."""

    TASK, CHAT = "tsk_01M4TESTTASK0000000000000", "cht_01M4TESTCHAT0000000000000"

    def __init__(self, ready=32.0, status="ok", decision="approve", done_at=None):
        self.ready, self.status, self.decision, self.done_at = ready, status, decision, done_at
        self.replay = None
        self.requests = []

    def request(self, method, path, body=None, idempotent=False):
        self.requests.append((method, path, body, idempotent))
        if (method, path) == ("POST", "/v1/chats"):
            return 201, {"chat_id": self.CHAT, "duplicate": False}
        if (method, path) == ("POST", f"/v1/chats/{self.CHAT}/messages"):
            return 201, {"chat_id": self.CHAT, "task_id": self.TASK, "routed": "new_task"}
        if path == f"/v1/tasks/{self.TASK}":
            if self.done_at is not None and self.replay.now >= self.done_at:
                return 200, {"task_id": self.TASK, "state": "done", "pending": []}
            state = "pending" if self.replay.now >= self.ready else "uncorrelated"
            return 200, {"task_id": self.TASK, "state": "done" if self.replay.now > 20 else "needs_approval",
                         "pending": [{"kind": "approval", "state": state, "interaction_id": "int_x"}]}
        if path == f"/v1/tasks/{self.TASK}/trace" and self.done_at is not None:
            return 200, {"tool_calls": [{"kind": "ha", "tool": "ha_call_service", "status": "unknown",
                                         "approval_decision": None, "result_summary": "result unknown (ReadTimeout)"}]}
        if path == f"/v1/tasks/{self.TASK}/trace":
            return 200, {"tool_calls": [{"kind": "ha", "tool": "ha_call_service", "status": self.status,
                                         "approval_decision": self.decision}]}
        return 404, None


@unittest.skipIf(audio_session is None, "pyserial missing")
class EarconsTest(unittest.TestCase):
    def test_skipped_without_claude_agent(self):
        agent = FakeAgent()
        session, _, calls = replay_item("earcons-accepted", "earcons", agent=agent, voice_llm="anthropic",
                                        fixture_device="aa:bb:cc:dd:ee:ff")
        item, count, verdict, result, reason, _ = session.rows[-1]
        self.assertEqual((item, count, verdict), ("earcons", "-", "skipped"))
        self.assertEqual(reason, "skipped: needs VOICE_LLM=claude_agent (server/.env: anthropic)")
        self.assertEqual(agent.requests, [])
        self.assertEqual(calls, [])

    def test_real_hold_approved_by_tap(self):
        agent = FakeAgent()
        session, replay, _ = replay_item("earcons-accepted", "earcons", agent=agent, voice_llm="claude_agent",
                                         fixture_device="AA:BB:CC:DD:EE:FF")
        item, count, verdict, _, reason, _ = session.rows[-1]
        self.assertEqual((item, count, verdict), ("earcons", "1/1", "pass"), reason)
        self.assertRegex(reason, r"^pass: task tsk_\w+: earcon=needs_you L\d+, card open L\d+, 2 tap\(s\), "
                         r"receipt accepted L\d+, earcon=approved L\d+, service: decision approve, ha_call_service ok$")
        create, message = agent.requests[0], agent.requests[1]
        self.assertEqual(create, ("POST", "/v1/chats", {"device_id": "aa:bb:cc:dd:ee:ff", "switch": False}, True))
        self.assertEqual(message[2]["text"], audio_session.HELD_ASK)
        self.assertIn("input_boolean.weixue_session_probe", message[2]["text"])
        self.assertEqual(sum(w == "send diag card approve" for w in replay.written), 2)

    def test_stale_receipts_fail_not_accepted(self):
        agent = FakeAgent()
        session, replay, _ = replay_item("earcons-stale", "earcons", agent=agent, voice_llm="claude_agent",
                                         fixture_device="aa:bb:cc:dd:ee:ff")
        _, count, verdict, _, reason, _ = session.rows[-1]
        self.assertEqual((count, verdict), ("0/1", "FAIL"))
        self.assertIn("not_accepted: ", reason)
        self.assertIn("receipts stale L", reason)
        self.assertEqual(sum(w == "send diag card approve" for w in replay.written), audio_session.APPROVE_TAPS)

    def test_no_approvable_hold(self):
        agent = FakeAgent(ready=float("inf"))
        session, _, _ = replay_item("earcons-accepted", "earcons", agent=agent, voice_llm="claude_agent",
                                    fixture_device="aa:bb:cc:dd:ee:ff")
        self.assertRegex(session.rows[-1][4], r"^no_hold: task tsk_\w+: no approvable hold within 90 s "
                         r"\(hold uncorrelated\); ha_call_service ok: ")

    def test_hold_lost_before_it_became_approvable(self):
        agent = FakeAgent(ready=float("inf"), done_at=16.0)
        session, _, _ = replay_item("earcons-accepted", "earcons", agent=agent, voice_llm="claude_agent",
                                    fixture_device="aa:bb:cc:dd:ee:ff")
        self.assertEqual(session.rows[-1][4], f"no_hold: task {FakeAgent.TASK}: task done without an approvable hold "
                         "(hold uncorrelated); ha_call_service unknown: result unknown (ReadTimeout)")

    def test_judge_needs_a_replayed_call(self):
        lines, _ = j.parse_serial_log((FIX / "earcons-accepted-serial.log").read_text())
        run = {"task_id": FakeAgent.TASK, "actionable": True, "taps": 2, "decision": "approve", "status": "pending",
               "tool": "ha_call_service"}
        self.assertEqual(j.judge_earcons(lines, run).cls, "not_replayed")
        self.assertEqual(j.judge_earcons(lines, dict(run, status="error")).cls, "pass")
        self.assertEqual(j.judge_earcons(lines, {"task_id": None, "error": "chat create answered 0"}).cls, "agent_error")


class AgentClientTest(unittest.TestCase):
    @unittest.skipIf(audio_session is None, "pyserial missing")
    def test_request_runs_inside_the_container(self):
        seen = {}

        def run(cmd, input=None, **kw):
            seen.update(cmd=cmd, input=json.loads(input))
            return subprocess.CompletedProcess(cmd, 0, 'warning\n{"status": 201, "body": {"chat_id": "c"}}\n', "")

        with mock.patch.object(audio_session.subprocess, "run", run):
            status, body = audio_session.AgentClient().request("POST", "/v1/chats", {"device_id": "d"}, idempotent=True)
        self.assertEqual((status, body), (201, {"chat_id": "c"}))
        self.assertEqual(seen["cmd"][:6], ["docker", "exec", "-i", "agent-service", "python", "-c"])
        self.assertEqual(seen["input"]["path"], "/v1/chats")
        self.assertRegex(seen["input"]["headers"]["Idempotency-Key"], r"^[0-9a-f-]{36}$")
        self.assertNotIn("AGENT_SERVICE_TOKEN=", " ".join(seen["cmd"]))
        failed = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "No such container")  # noqa: E731
        with mock.patch.object(audio_session.subprocess, "run", failed):
            self.assertEqual(audio_session.AgentClient().request("GET", "/v1/tasks/t"), (0, None))


class ShellEnvTest(unittest.TestCase):
    def mode_args(self, extra_env=""):
        import os
        import tempfile
        root = SCRIPTS.parent
        with tempfile.TemporaryDirectory() as d:
            env_file = Path(d) / ".env"
            env_file.write_text("LAN_IP=10.9.9.9\nWEIXUE_PROACTIVE_TOKEN=\"tok-secret-123\"\nBIND_IP=0.0.0.0\n"
                                "ALLOWED_DEVICES=aa:bb:cc:dd:ee:ff,11:22:33:44:55:66\nVOICE_BARGE_IN=1\n"
                                "VOICE_LLM=claude_agent\n" + extra_env)
            stub = Path(d) / "probe.sh"
            src = (root / "scripts" / "audio_session.sh").read_text()
            body = src[src.index("mode_args=()"):src.index("set +e")]
            stub.write_text("set -euo pipefail\nlive_env=\"$1\"\nno_audio=1\nexport WEIXUE_LIVE_ENV=\"$1\"\n"
                            f". {root / 'scripts' / 'lib' / 'remote_docker.sh'}\n" +
                            src[src.index("env_value() {"):src.index("\n}\n", src.index("env_value() {")) + 3] +
                            body + 'printf "%s\\n" "${mode_args[@]}"\n'
                            '[ "$WEIXUE_PROACTIVE_TOKEN" = tok-secret-123 ] && echo token-exported\n')
            env = {k: v for k, v in os.environ.items() if not k.startswith("WEIXUE_SERVER_")}
            out = subprocess.run(["bash", str(stub), str(env_file)], capture_output=True, text=True,
                                 env={**env, "WEIXUE_PROACTIVE_TOKEN": ""})
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.splitlines()

    def test_reads_token_fixture_and_flag_from_env_without_printing(self):
        self.assertEqual(self.mode_args(), ["--no-audio", "--fixture-base", "http://127.0.0.1:8003",
                                            "--fixture-device", "aa:bb:cc:dd:ee:ff", "--barge-in", "1",
                                            "--voice-llm", "claude_agent", "token-exported"])

    def test_remote_stack_sets_the_fixture_base_to_the_server(self):
        self.assertEqual(self.mode_args("WEIXUE_SERVER_SSH=jay@192.0.2.5\n")[1:3],
                         ["--fixture-base", "http://192.0.2.5:8003"])


if __name__ == "__main__":
    unittest.main()
