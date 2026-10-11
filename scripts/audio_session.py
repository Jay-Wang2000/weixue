#!/usr/bin/env python3
"""Runs the scripted voice and physical-interaction session on a connected diag build and writes
a pass/fail table (docs/logs/session-<date>.md) with the redacted serial and server logs.

Called by scripts/audio_session.sh, which holds the device lock, flashes the diag build before
and the release build after, and sets the Mac output volume. Speech is macOS `say`; gestures and
touches go through the diag console (firmware/tools/diag_inject.py). --no-audio runs only the
items that need no speech.
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

SERVER_CONTAINER = "xiaozhi-esp32-server"
CUES = ("Hmm?", "Mm-hm?", "Yes?")
STATE_RE = re.compile(r"StateMachine: State: (\w+) -> (\w+)")
STT_RE = re.compile(r"Application: >> (.+)")
TTS_RE = re.compile(r"Application: << (.+)")
REPLY_RE = re.compile(r"Application: << (?!(?:Hmm|Mm-hm|Yes)\?$)(.+)")
CUE_RE = re.compile(r"Application: << (Hmm\?|Mm-hm\?|Yes\?)$")
SERVER_ABORT_RE = re.compile(r"Abort message received|收到abort")
DIM_RE = re.compile(r"Backlight: Set brightness to 20$")
UNDIM_RE = re.compile(r"Backlight: Set brightness to (?!20$)\d+$")

AUDIO_ITEMS = ("boot", "one_breath", "wake_cue", "long_reply", "barge_in", "double_tap",
               "face_down", "press_boot", "panel", "card_tap", "bedside_voice", "pick_up_dim")
SILENT_ITEMS = ("boot", "face_down", "double_tap", "press_boot", "panel", "card_tap", "pick_up_dim")
NEEDS_AUDIO = {"one_breath", "wake_cue", "long_reply", "barge_in", "bedside_voice"}


class Session:
    def __init__(self, args, diag):
        self.args = args
        self.diag = diag
        self.audio = not args.no_audio
        self.rows = []

    # Device and speech helpers.

    def say(self, text):
        self.diag.note(f"say: {text}")
        subprocess.run(["say", "-v", self.args.voice, text], check=True)

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
        self.say(("Jarvis, " + text) if self.state() == "idle" else text)
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

    def record(self, item, passed, total, need, result, evidence, skipped=False):
        verdict = "skipped" if skipped else ("pass" if passed >= need else "FAIL")
        ev = ", ".join(str(e) for e in evidence[:12])
        if len(evidence) > 12:
            ev += f", ... ({len(evidence)} lines)"
        self.rows.append((item, f"{passed}/{total}" if total else "-", verdict, result, ev))
        self.diag.note(f"result {item}: {verdict} {passed}/{total} {result}")

    def skip(self, item, why):
        self.record(item, 0, 0, 0, why, [], skipped=True)

    # Items.

    def item_boot(self):
        hit = self.wait(r"State: \w+ -> idle", 120, since=0)
        console = self.diag.find_all(r"DiagConsole: listening")
        ok, result, n = self.inject("diag dump")
        ev = ([hit[0]] if hit else []) + [c[0] for c in console] + ([n] if n else [])
        good = bool(hit) and bool(console) and ok
        self.record("boot", int(good), 1, 1, "idle, diag console up, dump answered" if good else
                    f"idle={bool(hit)} console={bool(console)} dump={ok}", ev)
        time.sleep(5)

    def item_face_down(self):
        n_ok, ev = 0, []
        for i in range(self.args.count):
            since = self.diag.mark()
            ok_down, _, n1 = self.inject("diag gesture face_down")
            g = self.wait(r"IMU gesture: face_down \(count \d+\)", 3, since)
            m = self.wait(r"Application: Microphone muted$", 3, since)
            quiet = True
            if self.audio and i == 0:
                self.say("Jarvis, what time is it?")
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
            time.sleep(1.5)
        extra = ", a wake word while face-down started nothing" if self.audio else ""
        self.record("face_down", n_ok, self.args.count, self.args.count,
                    f"face_down -> `Microphone muted`, dump mic_muted, face_up -> `Microphone unmuted`{extra}", ev)

    def item_double_tap(self):
        n_ok, ev = 0, []
        for _ in range(self.args.count):
            if not self.audio:
                since = self.diag.mark()
                ok, _, _ = self.inject("diag gesture double_tap")
                g = self.wait(r"IMU gesture: double_tap \(count \d+\)", 3, since)
                n_ok += bool(ok and g)
                ev += [g[0]] if g else []
                time.sleep(1)
                continue
            since = self.ask("tell me a long story about a lighthouse keeper.")
            stt, reply = self.wait_reply(since)
            if reply is None:
                continue
            time.sleep(2.5)
            if self.state() != "speaking":
                self.diag.note("double_tap: reply ended before the tap")
                continue
            since = self.diag.mark()
            self.inject("diag gesture double_tap")
            g = self.wait(r"IMU gesture: double_tap \(count \d+\)", 2, since)
            a = self.wait(r"Application: Abort speaking", 2, since)
            s = self.wait(r"State: speaking -> listening", 3, since)
            good = all((g, a, s))
            n_ok += good
            ev += [h[0] for h in (stt, reply, g, a, s) if h]
            time.sleep(1)
        if self.audio:
            self.record("double_tap", n_ok, self.args.count, self.args.count,
                        "double-tap during the reply -> `Abort speaking`, speaking -> listening", ev)
        else:
            self.record("double_tap", n_ok, self.args.count, self.args.count,
                        "gesture handled while idle (no reply to stop without audio)", ev)

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
                        [h[0] for h in (m, b, r, u) if h])
            return
        self.wait_idle()
        since = self.diag.mark()
        ok, _, _ = self.inject("diag press_boot")
        c = self.wait(r"State: idle -> connecting", 3, since)
        lst = self.wait(r"State: connecting -> listening", 8, since)
        if lst:
            self.say("What time is it?")
        stt, reply = self.wait_reply(since)
        good = ok and all((c, lst, stt, reply))
        self.record("press_boot", int(good), 1, 1, "BOOT click from idle opens a conversation that answers",
                    [h[0] for h in (c, lst, stt, reply) if h])
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
        n_ok, ev = 0, []
        for line, want in steps:
            since = self.diag.mark()
            ok, _, _ = self.inject(line)
            hit = self.wait(want, 3, since)
            n_ok += bool(ok and hit)
            ev += [hit[0]] if hit else []
            time.sleep(0.8)
        ok, dump, _ = self.inject("diag dump")
        closed = ok and '"panel":false' in dump
        n_ok += closed
        self.record("panel", n_ok, len(steps) + 1, len(steps) + 1,
                    "swipe left opens page 1, tap Home -> page 2, swipe down/up, swipe right closes", ev)

    def item_card_tap(self):
        a = self.args
        token = os.environ.get("WEIXUE_PROACTIVE_TOKEN", "")
        if not (a.fixture_base and a.fixture_device and token):
            self.skip("card_tap", "needs --fixture-base, --fixture-device and WEIXUE_PROACTIVE_TOKEN")
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
                    [h[0] for h in (card, sent) if h])
        earcons = self.diag.find_all(r"FaceDisplay: earcon=", since)
        names = [re.search(r"earcon=(\S+)", t).group(1) for _, t in earcons]
        good = "needs_you" in names and "approved" in names
        self.record("earcons", int(good), 1, 1, f"markers: {', '.join(names) or 'none'}", [n for n, _ in earcons])

    def item_one_breath(self):
        n_ok, ev = 0, []
        for _ in range(self.args.count):
            self.wait_idle()
            time.sleep(2)
            since = self.diag.mark()
            self.say("Jarvis, what time is it?")
            stt, reply = self.wait_reply(since)
            good = bool(stt and re.search(r"what time is it", stt[1], re.I) and reply)
            n_ok += good
            ev += [h[0] for h in (stt, reply) if h]
        self.record("one_breath", n_ok, self.args.count, (self.args.count * 9 + 9) // 10,
                    "first transcript holds `what time is it`, reply played", ev)

    def item_wake_cue(self):
        done, ev, tries = {}, [], 0
        while len([c for c in CUES if done.get(c)]) < len(CUES) and tries < 12:
            tries += 1
            self.wait_idle()
            time.sleep(2)
            since = self.diag.mark()
            self.say("Jarvis")
            cue = self.wait(CUE_RE, 8, since)
            if cue is None:
                continue
            self.wait(r"State: speaking -> listening", 5, cue[4])
            since = self.diag.mark()
            self.say("Yes.")
            stt, reply = self.wait_reply(since)
            good = bool(stt and re.search(r"\byes\b", stt[1], re.I) and reply)
            name = cue[2].group(1)
            done[name] = done.get(name) or good
            ev += [h[0] for h in (cue, stt, reply) if h]
        n_ok = sum(bool(done.get(c)) for c in CUES)
        self.record("wake_cue", n_ok, len(CUES), len(CUES),
                    "; ".join(f"{c} {'ok' if done.get(c) else 'missing'}" for c in CUES) + f" ({tries} wakes)", ev)

    def item_long_reply(self):
        self.wait_idle()
        t0 = time.time()
        since = self.ask("tell me a story about a lighthouse keeper that takes about one minute to tell.")
        stt, first = self.wait_reply(since)
        end = self.wait(r"State: speaking -> (listening|idle)", 180, first[4]) if first else None
        t1 = time.time()
        aborts = self.diag.find_all(r"Application: Abort speaking", since)
        server = self.server_lines(t0, t1)
        server_aborts = None if server is None else sum(bool(SERVER_ABORT_RE.search(l)) for l in server)
        seconds = end[3] - first[3] if (first and end) else 0
        good = seconds >= 50 and not aborts and server_aborts == 0
        self.record("long_reply", int(good), 1, 1,
                    f"reply {seconds:.0f} s, device aborts {len(aborts)}, server aborts "
                    f"{'n/a' if server_aborts is None else server_aborts}",
                    [h[0] for h in (stt, first, end) if h] + [n for n, _ in aborts])
        self.wait_idle()

    def item_barge_in(self):
        self.wait_idle()
        since = self.ask("tell me a long story about a dragon.")
        stt, first = self.wait_reply(since)
        if first is None:
            self.record("barge_in", 0, 1, 1, "no reply to interrupt", [h[0] for h in (stt,) if h])
            return
        time.sleep(5)
        t0 = time.time()
        since = self.diag.mark()
        self.say("Stop. What is two plus two?")
        stt2, reply = self.wait_reply(since, reply_timeout=30)
        t1 = time.time()
        server = self.server_lines(t0 - 1, t1)
        server_abort = bool(server) and any(SERVER_ABORT_RE.search(l) for l in server)
        device_abort = self.diag.find_all(r"Application: Abort speaking", since)
        good = bool(stt2 and re.search(r"two plus two|2 \+ 2|2\+2", stt2[1], re.I) and reply and
                    re.search(r"\bfour\b|\b4\b", reply[1], re.I) and (server_abort or device_abort))
        self.record("barge_in", int(good), 1, 1,
                    f"interrupted reply, new turn answered; abort server={server_abort} device={bool(device_abort)}",
                    [h[0] for h in (stt, first, stt2, reply) if h] + [n for n, _ in device_abort])
        self.wait_idle()

    def item_bedside_voice(self):
        self.wait_idle()
        since = self.ask("switch the screen to bedside mode.")
        hint = self.wait(r"FaceDisplay: bedside hint=night", 30, since)
        self.wait_idle()
        on = self.wait(r"FaceDisplay: bedside=on", 25, since)
        off = None
        if on:
            s2 = self.diag.mark()
            self.inject("diag tap 233 60")
            off = self.wait(r"FaceDisplay: bedside=off reason=touch", 3, s2)
        self.wait_idle()
        s3 = self.ask("set the bedside screen back to automatic.")
        auto = self.wait(r"FaceDisplay: bedside hint=auto", 30, s3)
        good = all((hint, on, off, auto))
        self.record("bedside_voice", int(good), 1, 1,
                    "voice night hint -> page 15 s after idle, tap leaves it, voice auto hint",
                    [h[0] for h in (hint, on, off, auto) if h])
        self.wait_idle()

    def item_pick_up_dim(self):
        n_ok, ev = 0, []
        for _ in range(self.args.count):
            ok, dump, _ = self.inject("diag dump")
            if not (ok and '"brightness":20,' in dump):
                if self.wait(DIM_RE, 90) is None:
                    self.diag.note("pick_up_dim: no dim within 90 s")
                    continue
            time.sleep(1)
            since = self.diag.mark()
            ok, _, _ = self.inject("diag gesture pick_up")
            g = self.wait(r"IMU gesture: pick_up \(count \d+\)", 2, since)
            u = self.wait(UNDIM_RE, 3, since)
            good = bool(ok and g and u)
            n_ok += good
            ev += [h[0] for h in (g, u) if h]
        self.record("pick_up_dim", n_ok, self.args.count, self.args.count,
                    "pick_up after the 60 s dim restores the brightness", ev)

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
        f.write(f"Mode: {'no audio (gestures and touches only)' if args.no_audio else 'audio, say -v ' + args.voice}. "
                f"Build: {args.build_info or 'diag'}. Serial log: `{rel(serial_log)}`"
                f"{', server log: `' + rel(server_log) + '`' if server_log else ''}. "
                f"Evidence numbers are serial log lines. Injections use the diag console "
                f"(`DiagConsole: ok ...` lines).\n\n")
        f.write("| # | Item | Count | Verdict | Result | Evidence |\n|---|---|---|---|---|---|\n")
        for i, (item, count, verdict, result, ev) in enumerate(rows, 1):
            f.write(f"| {i} | {item} | {count} | {verdict} | {result} | {ev} |\n")
        f.write(f"\n{passed}/{len(counted)} items pass, {len(rows) - len(counted)} skipped.\n")
    return passed == len(counted)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", required=True)
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--voice", default="Samantha")
    ap.add_argument("--count", type=int, default=10, help="repetitions of the repeated items")
    ap.add_argument("--items", help="comma-separated subset, in order")
    ap.add_argument("--out", help="results table (default docs/logs/session-<date>.md)")
    ap.add_argument("--build-info", default="")
    ap.add_argument("--no-reset", action="store_true", help="do not reset the board at the start")
    ap.add_argument("--fixture-base", help="gateway base URL for the approval card (agent_msg_fixture.py)")
    ap.add_argument("--fixture-device", help="device MAC as on allowed_devices")
    args = ap.parse_args()

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
