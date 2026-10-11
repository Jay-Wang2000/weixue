#!/usr/bin/env python3
"""Runs the scripted voice and physical-interaction session on a connected diag build and writes
a pass/fail table (docs/logs/session-<date>.md) with the redacted serial and server logs.

Called by scripts/audio_session.sh, which holds the device lock, flashes the diag build before
and the release build after, and sets the Mac output volume. Speech is Google Cloud TTS (Chirp 3 HD)
clips cached under ~/Projects/weixue-vendor/session-clips and played with afplay (scripts/session_tts.py);
gestures and touches go through the diag console (firmware/tools/diag_inject.py). --no-audio runs only
the items that need no speech; --synth-only synthesises every clip and exits (no board, no playback).
Verdicts come from scripts/session_judge.py; each table row carries the judged events (Reason).
"""
import argparse
import datetime
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "firmware", "tools"))
from diag_inject import DiagSerial, redact  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import session_judge as judge  # noqa: E402
import session_tts  # noqa: E402

SERVER_CONTAINER = "xiaozhi-esp32-server"
CUES = judge.CUES
STATE_RE = re.compile(r"StateMachine: State: (\w+) -> (\w+)")
STT_RE = judge.STT_RE
REPLY_RE = judge.REPLY_RE
CUE_RE = judge.CUE_RE
YES_STT_RE = re.compile(r"Application: >> .*\byes\b", re.I)
WAKE_CUE_TRIES = 12

AUDIO_ITEMS = ("boot", "one_breath", "wake_cue", "long_reply", "barge_in", "double_tap",
               "face_down", "press_boot", "panel", "card_tap", "bedside_voice", "pick_up")
SILENT_ITEMS = ("boot", "face_down", "double_tap", "press_boot", "panel", "card_tap", "pick_up")
NEEDS_AUDIO = {"one_breath", "wake_cue", "long_reply", "barge_in", "bedside_voice"}

WAKE = "Jarvis"
QUESTION = "what time is it?"
ASKS = {
    "double_tap": "tell me a long story about a lighthouse keeper.",
    "long_reply": "tell me a story about a lighthouse keeper that takes about one minute to tell.",
    "barge_in": "tell me a long story about a dragon.",
    "bedside_night": "switch the screen to bedside mode.",
    "bedside_auto": "set the bedside screen back to automatic.",
}
PLAIN = {
    "wake": WAKE,
    "wake_question": f"{WAKE}, {QUESTION}",
    "yes": "Yes.",
    "question": QUESTION.capitalize(),
    "barge": "Stop. What is two plus two?",
}
MATCH = {
    "question": re.compile(r"what time is it", re.I),
    "double_tap": re.compile(r"lighthouse|light house", re.I),
    "long_reply": re.compile(r"lighthouse|light house", re.I),
    "barge_in": re.compile(r"dragon", re.I),
    "barge_question": re.compile(r"two plus two|2 \+ 2|2\+2", re.I),
    "barge_answer": re.compile(r"\bfour\b|\b4\b", re.I),
    "bedside_night": re.compile(r"bedside", re.I),
    "bedside_auto": re.compile(r"auto", re.I),
}


def events(pairs):
    """`label L<n>` for each found event, `no <label>` for each missing one."""
    return ", ".join(f"{label} L{hit[0]}" if isinstance(hit, tuple) else
                     (label if hit else f"no {label}") for label, hit in pairs)


def cell(text):
    return text.replace("|", "\\|").replace("\n", " ")


def all_utterances():
    """Every text the session can speak, with and without the wake word prefix."""
    texts = list(PLAIN.values())
    for text in ASKS.values():
        texts += [text, f"{WAKE}, {text}"]
    return list(dict.fromkeys(texts))


class Session:
    def __init__(self, args, diag):
        self.args = args
        self.diag = diag
        self.audio = not args.no_audio
        self.barge_in = int(getattr(args, "barge_in", 0) or 0)
        self.rows = []
        self.plays = []  # (line index when the clip started, judge.Play)

    # Device and speech helpers.

    def say(self, text):
        path, _ = session_tts.ensure_clip(self.args.voice, text)
        n = self.diag.note(f"say: {text}")
        self.plays.append((self.diag.mark(), judge.Play(n, time.monotonic(), text)))
        subprocess.run(session_tts.afplay_command(path), check=True)

    def window(self, since):
        """Console lines from line index `since` until now, as judge.Line."""
        return [judge.Line(n, t, text) for t, n, text in self.diag.snapshot(since)]

    def plays_since(self, since):
        return [p for i, p in self.plays if i >= since]

    def inject(self, line):
        ok, result, n = self.diag.send(line)
        if not ok:
            self.diag.note(f"inject failed: {line}: {result}")
        return ok, result, n

    def state(self):
        hits = self.diag.find_all(STATE_RE)
        return STATE_RE.search(hits[-1][1]).group(2) if hits else "unknown"

    def wait(self, pattern, timeout, since=None):
        return self.diag.wait_for(pattern, timeout, since)

    def wait_idle(self, timeout=45):
        if self.state() == "idle":
            return True
        return self.wait(r"State: \w+ -> idle", timeout) is not None

    def ask(self, text):
        """Says text, with the wake word first when the device is idle; returns the line index
        before the speech."""
        since = self.diag.mark()
        self.say(f"{WAKE}, {text}" if self.state() == "idle" else text)
        return since

    def wait_reply(self, since, stt_timeout=15, reply_timeout=40):
        stt = self.wait(STT_RE, stt_timeout, since)
        if stt is None:
            return None, None
        return stt, self.wait(REPLY_RE, reply_timeout, stt[4])

    def server_lines(self, t0, t1):
        try:
            out = subprocess.run(["docker", "logs", "--since", str(int(t0)), "--until", str(int(t1) + 1),
                                  SERVER_CONTAINER], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if out.returncode != 0:
            return None
        return [redact(l) for l in (out.stdout + out.stderr).splitlines()]

    def record(self, item, passed, total, need, result, evidence, skipped=False, reason="", count=None):
        verdict = "skipped" if skipped else ("pass" if total and passed >= need else "FAIL")
        ev = ", ".join(str(e) for e in evidence[:12])
        if len(evidence) > 12:
            ev += f", ... ({len(evidence)} lines)"
        count = count or (f"{passed}/{total}" if total else "-")
        self.rows.append((item, count, verdict, result, reason or "-", ev))
        self.diag.note(f"result {item}: {verdict} {count} {reason or result}")

    def record_trials(self, item, verdicts, result, need=None, extra="", score=None):
        """Row from judge Verdicts: trials with ok None are listed but not counted. score
        (passed, total) replaces the per-trial count."""
        counted = [v for v in verdicts if v.ok is not None]
        passed, total = score or (sum(v.ok is True for v in counted), len(counted))
        excluded = len(verdicts) - len(counted)
        count = f"{passed}/{total}" + (f" (+{excluded} not counted)" if excluded and not score else "")
        tally = {}
        for v in verdicts:
            tally[v.cls] = tally.get(v.cls, 0) + 1
        trials = [f"{v.cls}: {v.reason}" if len(verdicts) == 1 else f"t{i} {v.cls}: {v.reason}"
                  for i, v in enumerate(verdicts, 1)]
        head = ", ".join(f"{k} {n}" for k, n in tally.items())
        reason = "; ".join(([head] if len(verdicts) > 1 else []) + trials + ([extra] if extra else []))
        self.record(item, passed, total, total if need is None else need, result,
                    [n for v in verdicts for n in v.evidence], reason=reason, count=count)

    def skip(self, item, why):
        self.record(item, 0, 0, 0, why, [], skipped=True)

    # Items.

    def item_boot(self):
        hit = self.wait(r"State: \w+ -> idle", 120, since=0)
        console = self.diag.find_all(r"DiagConsole: listening")
        ok, result, n = self.inject("diag dump")
        ev = ([hit[0]] if hit else []) + [c[0] for c in console] + ([n] if n else [])
        good = bool(hit) and bool(console) and ok
        self.record("boot", int(good), 1, 1, "idle, diag console up, dump answered", ev,
                    reason=events([("idle", hit), ("diag console", console[0] if console else None),
                                   ("dump", (n,) if ok and n else None)]))
        time.sleep(5)

    def item_face_down(self):
        n_ok, ev, missing = 0, [], []
        for i in range(self.args.count):
            since = self.diag.mark()
            ok_down, _, n1 = self.inject("diag gesture face_down")
            g = self.wait(r"IMU gesture: face_down \(count \d+\)", 3, since)
            m = self.wait(r"Application: Microphone muted$", 3, since)
            quiet = True
            if self.audio and i == 0:
                self.say(PLAIN["wake_question"])
                quiet = self.wait(r"Wake word detected|idle -> connecting", 6, since) is None
            ok_dump, dump, _ = self.inject("diag dump")
            muted = ok_dump and '"mic_muted":true' in dump
            since = self.diag.mark()
            ok_up, _, n2 = self.inject("diag gesture face_up")
            u = self.wait(r"IMU gesture: face_up \(count \d+\)", 3, since)
            um = self.wait(r"Application: Microphone unmuted$", 3, since)
            good = ok_down and ok_up and all((g, m, u, um)) and muted and quiet
            n_ok += good
            ev += [h[0] for h in (g, m, u, um) if h]
            if not good:
                missing.append(f"t{i + 1} " + events([("face_down", g), ("muted", m), ("dump mic_muted", muted),
                                                       ("quiet", quiet), ("face_up", u), ("unmuted", um)]))
            time.sleep(1.5)
        extra = ", a wake word while face-down started nothing" if self.audio else ""
        self.record("face_down", n_ok, self.args.count, self.args.count,
                    f"face_down -> `Microphone muted`, dump mic_muted, face_up -> `Microphone unmuted`{extra}", ev,
                    reason="; ".join(missing) or "every trial: face_down, muted, dump mic_muted, face_up, unmuted")

    def item_double_tap(self):
        verdicts = []
        for _ in range(self.args.count):
            if not self.audio:
                since = self.diag.mark()
                ok, _, _ = self.inject("diag gesture double_tap")
                g = self.wait(judge.gesture_re("double_tap"), 3, since)
                verdicts.append(judge.Verdict(bool(ok and g), "pass" if ok and g else "no_gesture",
                                              events([("gesture", g)]), [g[0]] if g else []))
                time.sleep(1)
                continue
            since = self.ask(ASKS["double_tap"])
            stt, reply = self.wait_reply(since)
            if reply is not None:
                time.sleep(2.5)
                if self.state() == "speaking":
                    tap = self.diag.mark()
                    self.inject("diag gesture double_tap")
                    self.wait(judge.TO_LISTENING_RE, 3, tap)
            verdicts.append(judge.judge_double_tap(self.window(since), MATCH["double_tap"]))
            self.diag.note(f"double_tap: {verdicts[-1].cls}: {verdicts[-1].reason}")
            time.sleep(1)
        if self.audio:
            self.record_trials("double_tap", verdicts,
                               "double-tap during the reply -> `Abort speaking`, speaking -> listening")
        else:
            self.record_trials("double_tap", verdicts, "gesture handled while idle (no reply to stop without audio)")

    def item_press_boot(self):
        if not self.audio:
            since = self.diag.mark()
            self.inject("diag gesture face_down")
            m = self.wait(r"Application: Microphone muted$", 3, since)
            ok, _, _ = self.inject("diag press_boot")
            b = self.wait(r"Button diag: injected boot click", 3, since)
            r = self.wait(r"Microphone muted, not starting a conversation", 3, since)
            self.inject("diag gesture face_up")
            u = self.wait(r"Application: Microphone unmuted$", 3, since)
            good = ok and all((m, b, r, u))
            self.record("press_boot", int(good), 1, 1,
                        "BOOT click while muted -> `Microphone muted, not starting a conversation`",
                        [h[0] for h in (m, b, r, u) if h],
                        reason=events([("muted", m), ("boot click", b), ("refused", r), ("unmuted", u)]))
            return
        self.wait_idle()
        since = self.diag.mark()
        ok, _, _ = self.inject("diag press_boot")
        c = self.wait(r"State: idle -> connecting", 3, since)
        lst = self.wait(r"State: connecting -> listening", 8, since)
        if lst:
            self.say(PLAIN["question"])
        stt, reply = self.wait_reply(since)
        good = ok and all((c, lst, stt, reply))
        self.record("press_boot", int(good), 1, 1, "BOOT click from idle opens a conversation that answers",
                    [h[0] for h in (c, lst, stt, reply) if h],
                    reason=events([("connecting", c), ("listening", lst), ("transcript", stt), ("reply", reply)]))
        self.wait_idle()

    def item_panel(self):
        if self.audio:
            self.wait_idle()
        steps = [
            ("diag swipe left", r"MonitorPanel: page=panel"),
            ("diag tap 233 430", r"MonitorPanel: page=2"),
            ("diag swipe down", r"MonitorPanel: page=1"),
            ("diag swipe up", r"MonitorPanel: page=2"),
            ("diag swipe right", r"MonitorPanel: page=face"),
        ]
        n_ok, ev, seen = 0, [], []
        for line, want in steps:
            since = self.diag.mark()
            ok, _, _ = self.inject(line)
            hit = self.wait(want, 3, since)
            n_ok += bool(ok and hit)
            ev += [hit[0]] if hit else []
            seen.append((want.split(": ")[-1], hit if ok else None))
            time.sleep(0.8)
        ok, dump, _ = self.inject("diag dump")
        closed = ok and '"panel":false' in dump
        n_ok += closed
        self.record("panel", n_ok, len(steps) + 1, len(steps) + 1,
                    "swipe left opens page 1, tap Home -> page 2, swipe down/up, swipe right closes", ev,
                    reason=events(seen + [("dump panel:false", closed)]))

    def item_card_tap(self):
        a = self.args
        token = os.environ.get("WEIXUE_PROACTIVE_TOKEN", "")
        if not (a.fixture_base and a.fixture_device and token):
            self.skip("card_tap", "needs --fixture-base, --fixture-device and WEIXUE_PROACTIVE_TOKEN "
                      "(audio_session.sh sets them from server/.env)")
            self.skip("earcons", "needs the card fixture")
            return
        self.wait_idle()
        since = self.diag.mark()
        fixture = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "server", "scripts", "agent_msg_fixture.py"), "--base", a.fixture_base,
             "--device", a.fixture_device, "--wait", "60", "--log", a.fixture_log, "approval"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        card = self.wait(r"agent ui: card open task=", 30, since)
        if card:
            time.sleep(1.0)
            self.inject("diag card approve")
        sent = self.wait(r"Device event sent|agent ui: card closed", 15, since)
        try:
            fixture.wait(75)
        except subprocess.TimeoutExpired:
            fixture.kill()
        responses = 0
        try:
            with open(a.fixture_log, encoding="utf-8") as f:
                responses = sum('"approve"' in l and "response" in l for l in f)
        except OSError:
            pass
        good = bool(card and sent and responses)
        self.record("card_tap", int(good), 1, 1, "approval card opened, tap on Approve reached the server",
                    [h[0] for h in (card, sent) if h],
                    reason=events([("card open", card), ("sent", sent), ("fixture approve response", responses)]))
        earcons = self.diag.find_all(r"FaceDisplay: earcon=", since)
        names = [re.search(r"earcon=(\S+)", t).group(1) for _, t in earcons]
        good = "needs_you" in names and "approved" in names
        self.record("earcons", int(good), 1, 1, "earcons needs_you and approved", [n for n, _ in earcons],
                    reason=", ".join(f"earcon={name} L{n}" for name, (n, _) in zip(names, earcons)) or "no earcon line")

    def item_one_breath(self):
        verdicts = []
        for _ in range(self.args.count):
            self.wait_idle()
            time.sleep(2)
            since = self.diag.mark()
            self.say(PLAIN["wake_question"])
            self.wait_reply(since)
            verdicts.append(judge.judge_turn(self.window(since), MATCH["question"]))
        self.record_trials("one_breath", verdicts, "first transcript holds `what time is it`, reply played",
                           need=(self.args.count * 9 + 9) // 10)

    def item_wake_cue(self):
        done, verdicts = {}, []
        while not all(done.get(c) for c in CUES) and len(verdicts) < WAKE_CUE_TRIES:
            self.wait_idle()
            time.sleep(2)
            t0 = time.time()
            since = self.diag.mark()
            self.say(PLAIN["wake"])
            cue = self.wait(CUE_RE, 15, since)
            if cue is not None:
                self.wait(r"State: speaking -> listening", 5, cue[4])
                after = self.diag.mark()
                self.say(PLAIN["yes"])
                yes = self.wait(YES_STT_RE, 15, after)
                if yes is not None:
                    self.wait(REPLY_RE, 40, yes[4])
            server = self.server_lines(t0, time.time())
            lines = self.window(since)
            v = judge.judge_wake_cue(lines, self.plays_since(since), server)
            name = judge.cue_name(lines, server) or "no cue"
            verdicts.append(v._replace(reason=f"[{name}] {v.reason}"))
            self.diag.note(f"wake_cue: {v.cls}: {v.reason}")
            if name in CUES:
                done[name] = done.get(name) or v.ok
        n_ok = sum(bool(done.get(c)) for c in CUES)
        summary = "; ".join(f"{c} {'ok' if done.get(c) else 'not answered' if c in done else 'not played'}"
                            for c in CUES)
        self.record_trials("wake_cue", verdicts, "each cue played and its follow-up \"Yes.\" answered",
                           extra=f"{summary} ({len(verdicts)} wakes)", score=(n_ok, len(CUES)))

    def item_long_reply(self):
        self.wait_idle()
        t0 = time.time()
        since = self.ask(ASKS["long_reply"])
        stt, first = self.wait_reply(since)
        if first is not None:
            self.wait(r"State: speaking -> (listening|idle)", 180, first[4])
        server = self.server_lines(t0, time.time())
        v = judge.judge_long_reply(self.window(since), server, MATCH["long_reply"])
        self.record_trials("long_reply", [v], f"TTS start to stop >= {judge.LONG_REPLY_S} s, "
                           "0 device and server aborts")
        self.wait_idle()

    def item_barge_in(self):
        self.wait_idle()
        since = self.ask(ASKS["barge_in"])
        stt, first = self.wait_reply(since)
        server = None
        if first is not None:
            time.sleep(5)
            if self.state() == "speaking":
                t0 = time.time()
                after = self.diag.mark()
                self.say(PLAIN["barge"])
                if self.barge_in:
                    self.wait_reply(after, reply_timeout=30)
                else:
                    self.wait(judge.SPEAK_END_RE, 180, after)
                server = self.server_lines(t0 - 1, time.time())
        v = judge.judge_barge_in(self.window(since), self.plays_since(since), server, self.barge_in,
                                 MATCH["barge_in"], PLAIN["barge"], MATCH["barge_question"], MATCH["barge_answer"])
        expect = ("phrase during the reply -> abort, new turn answered" if self.barge_in else
                  "phrase during the reply -> no abort, reply plays out")
        self.record_trials("barge_in", [v], f"VOICE_BARGE_IN={self.barge_in}: {expect}")
        self.wait_idle()

    def item_bedside_voice(self):
        self.wait_idle()
        start = self.ask(ASKS["bedside_night"])
        self.wait(r"FaceDisplay: bedside hint=night", 30, start)
        self.wait_idle()
        on = self.wait(r"FaceDisplay: bedside=on", 25, start)
        if on:
            s2 = self.diag.mark()
            self.inject("diag tap 233 60")
            self.wait(r"FaceDisplay: bedside=off reason=touch", 3, s2)
        self.wait_idle()
        s3 = self.ask(ASKS["bedside_auto"])
        self.wait(r"FaceDisplay: bedside hint=auto", 30, s3)
        v = judge.judge_bedside(self.window(start), self.plays_since(start), ASKS["bedside_night"],
                                ASKS["bedside_auto"], MATCH["bedside_night"], MATCH["bedside_auto"])
        self.record_trials("bedside_voice", [v], "voice night hint -> page 15 s after idle, tap leaves it, voice auto hint")
        self.wait_idle()

    def item_pick_up(self):
        verdicts = []
        for _ in range(self.args.count):
            since = self.diag.mark()
            ok, dump, _ = self.inject("diag dump")
            dimmed = ok and '"brightness":20,' in dump
            if not dimmed:
                dimmed = self.wait(judge.DIM_RE, 90) is not None
            if dimmed:
                time.sleep(1)
                tap = self.diag.mark()
                self.inject("diag gesture pick_up")
                self.wait(judge.UNDIM_RE, 3, tap)
            verdicts.append(judge.judge_pick_up(self.window(since), self.plays_since(since), dimmed))
        self.record_trials("pick_up", verdicts, "pick_up after the 60 s dim restores the brightness",
                           extra=judge.vibration_summary(self.window(0), self.plays_since(0)))

    def run(self, items):
        for item in items:
            if item in NEEDS_AUDIO and not self.audio:
                self.skip(item, "needs audio (--no-audio)")
                continue
            self.diag.note(f"item {item}")
            getattr(self, f"item_{item}")()


def write_report(path, args, session, started, serial_log, server_log):
    rows = session.rows
    counted = [r for r in rows if r[2] != "skipped"]
    passed = sum(r[2] == "pass" for r in counted)
    rel = lambda p: os.path.relpath(p, os.path.dirname(path))  # noqa: E731
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Device session, {started:%Y-%m-%d %H:%M} UTC\n\n")
        f.write(f"Mode: {'no audio (gestures and touches only)' if args.no_audio else 'audio, Google TTS voice ' + args.voice}. "
                f"Build: {args.build_info or 'diag'}. Serial log: `{rel(serial_log)}`"
                f"{', server log: `' + rel(server_log) + '`' if server_log else ''}. "
                f"Server VOICE_BARGE_IN={session.barge_in} (server/.env). "
                f"Evidence numbers and `L<n>` in Reason are serial log lines. Injections use the diag console "
                f"(`DiagConsole: ok ...` lines).\n\n")
        f.write("| # | Item | Count | Verdict | Result | Reason | Evidence |\n|---|---|---|---|---|---|---|\n")
        for i, (item, count, verdict, result, reason, ev) in enumerate(rows, 1):
            f.write(f"| {i} | {item} | {count} | {verdict} | {cell(result)} | {cell(reason)} | {ev} |\n")
        f.write(f"\n{passed}/{len(counted)} items pass, {len(rows) - len(counted)} skipped.\n")
    return passed == len(counted)


def synth_only(voice):
    texts = all_utterances()
    made = 0
    for text in texts:
        path, new = session_tts.ensure_clip(voice, text)
        made += new
        print(f"{'synthesised' if new else 'cached'} {os.path.basename(path)}: {text}")
    print(f"{len(texts)} clips, {made} synthesised, in {session_tts.CACHE_DIR}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port (required unless --synth-only)")
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--voice", default=session_tts.DEFAULT_VOICE,
                    help="Chirp 3 HD speaker (en-US and cmn-CN voices share it) or a full voice name")
    ap.add_argument("--synth-only", action="store_true",
                    help="synthesise and cache every clip, then exit (no board, no playback)")
    ap.add_argument("--count", type=int, default=10, help="repetitions of the repeated items")
    ap.add_argument("--items", help="comma-separated subset, in order")
    ap.add_argument("--out", help="results table (default docs/logs/session-<date>.md)")
    ap.add_argument("--build-info", default="")
    ap.add_argument("--no-reset", action="store_true", help="do not reset the board at the start")
    ap.add_argument("--fixture-base", help="gateway base URL for the approval card (agent_msg_fixture.py)")
    ap.add_argument("--fixture-device", help="device MAC as on allowed_devices")
    ap.add_argument("--barge-in", type=int, choices=(0, 1), default=0,
                    help="VOICE_BARGE_IN in effect on the server (audio_session.sh reads it from server/.env)")
    args = ap.parse_args()
    if args.synth_only:
        return synth_only(args.voice)
    if not args.port:
        ap.error("--port is required unless --synth-only")

    started = datetime.datetime.now(datetime.timezone.utc)
    stamp = started.strftime("%Y-%m-%d-%H%M")
    out = args.out or os.path.join(ROOT, "docs", "logs", f"session-{stamp}.md")
    base = out[:-3] if out.endswith(".md") else out
    serial_log, server_log = base + "-serial.log", base + "-server.log"
    args.fixture_log = base + "-fixture.jsonl"
    items = args.items.split(",") if args.items else list(SILENT_ITEMS if args.no_audio else AUDIO_ITEMS)
    unknown = [i for i in items if not hasattr(Session, f"item_{i}")]
    if unknown:
        raise SystemExit(f"unknown items: {', '.join(unknown)}")

    diag = DiagSerial(args.port, serial_log, reset=not args.no_reset)
    session = Session(args, diag)
    t0 = time.time()
    try:
        session.run(items)
    finally:
        diag.close()
    lines = session.server_lines(t0 - 5, time.time())
    if lines is not None:
        with open(server_log, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    else:
        server_log = None
    ok = write_report(out, args, session, started, serial_log, server_log)
    print(f"results: {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
