"""Judging rules of the device session driver (scripts/audio_session.py).

Each rule reads the device console lines of one trial, the clips the Mac played, the server log
lines and, for earcons, the driver's agent-service evidence, and returns a Verdict built only from
device and server events: state changes, wake, cue, transcript (`>>`), sentence (`<<`), abort,
gesture, backlight, bedside, card, receipt and earcon lines. No I/O.
"""
import re
from collections import namedtuple

Line = namedtuple("Line", "n t text")  # serial log line number, host seconds, console text
Play = namedtuple("Play", "n t text")  # a clip started on the Mac speaker: log line number of its note, host seconds
Verdict = namedtuple("Verdict", "ok cls reason evidence")  # ok None: the trial is not counted

CUES = ("Hmm?", "Mm-hm?", "Yes?")
WAKE_RE = re.compile(r"Application: Wake word detected")
STT_RE = re.compile(r"Application: >> (.+)")
REPLY_RE = re.compile(r"Application: << (?!(?:Hmm|Mm-hm|Yes)\?$)(.+)")
CUE_RE = re.compile(r"Application: << (Hmm\?|Mm-hm\?|Yes\?)$")
SENTENCE_RE = re.compile(r"Application: << ")
SPEAK_START_RE = re.compile(r"StateMachine: State: \w+ -> speaking")
SPEAK_END_RE = re.compile(r"StateMachine: State: speaking -> (\w+)")
TO_LISTENING_RE = re.compile(r"StateMachine: State: speaking -> listening")
ABORT_RE = re.compile(r"Application: Abort speaking")
DIM_RE = re.compile(r"Backlight: Set brightness to 20$")
UNDIM_RE = re.compile(r"Backlight: Set brightness to (?!20$)(\d+)$")
HINT_RE = re.compile(r"FaceDisplay: bedside hint=(\w+)")
BEDSIDE_ON_RE = re.compile(r"FaceDisplay: bedside=on")
BEDSIDE_TOUCH_OFF_RE = re.compile(r"FaceDisplay: bedside=off reason=touch")
SERVER_CUE_RE = re.compile(r"wake cue: (.+)$")
SERVER_CUE_SKIP_RE = re.compile(r"wake cue (?:skipped|dropped after loading): (.+)$")
SERVER_ABORT_RE = re.compile(r"Abort message received(?!-end)|收到abort")
SERVER_DROP_RE = re.compile(r"barge-in disabled: dropped utterance during playback: (.+)$")
YES_RE = re.compile(r"\byes\b", re.I)
EARCON_RE = re.compile(r"FaceDisplay: earcon=(\w+)(.*)$")
RECEIPT_RE = re.compile(r"WeixueAgent: agent receipt: approve seq=\d+ card=\S+ result=(\w+)")
APPROVED_RE = re.compile(r"FaceDisplay: earcon=approved$")
REPLAYED = ("ok", "error")  # tool call status once the approved call ran (error: Home Assistant refused it)

PREROLL_S = 2.0  # a non-"yes" transcript up to this long after the "Yes." clip starts belongs to the wake segment
VIBRATION_S = 1.0  # an unprompted pick_up up to this long after a playback start is speaker vibration
LONG_REPLY_S = 50
INJECT_GAP_S = 0.5


def gesture_re(name):
    return re.compile(rf"IMU gesture: {name} \(count \d+\)")


def injected_re(name):
    return re.compile(rf"IMU diag: injected {name}$")


def first(lines, rx, after=None):
    """First line matching rx with a log line number above `after` (a Line, Play or None)."""
    floor = -1 if after is None else after.n
    return next((l for l in lines if l.n > floor and rx.search(l.text)), None)


def card_open_re(task_id):
    return re.compile(rf"agent ui: card open task={re.escape(task_id)}$")


def heard(line):
    text = STT_RE.search(line.text).group(1).strip()
    return f"'{text[:48]}{'...' if len(text) > 48 else ''}'"


def ev(*hits):
    return [h.n for h in hits if h is not None]


def server_drops(server, limit=3):
    """The utterances the server dropped during playback (barge-in off), quoted, or ''."""
    found = [m.group(1)[:48] for m in map(SERVER_DROP_RE.search, server or []) if m]
    return ", ".join(found[:limit]) + (f" (+{len(found) - limit})" if len(found) > limit else "")


def server_count(server, rx):
    return None if server is None else sum(bool(rx.search(l)) for l in server)


def _request(lines, request_re):
    """The transcript of the request (matching request_re, else the first one) and its reply."""
    stts = [l for l in lines if STT_RE.search(l.text)]
    stt = next((l for l in stts if request_re.search(l.text)), stts[0] if stts else None)
    return stt, (first(lines, REPLY_RE, stt) if stt else None)


def _no_turn(lines, stt, reply, request_re, tail):
    """Verdict for a request with no transcript or no reply, else None."""
    wake = first(lines, WAKE_RE)
    if stt is None:
        woke = f"wake L{wake.n}" if wake else "no `Wake word detected`"
        return Verdict(False, "no_transcript", f"{woke}, no transcript{tail}", ev(wake))
    if reply is None:
        return Verdict(False, "no_reply", f"heard {heard(stt)} L{stt.n}{_mismatch(stt, request_re)}, no reply{tail}",
                       ev(wake, stt))
    return None


def _mismatch(stt, request_re):
    return "" if request_re.search(stt.text) else " (not the request)"


def not_idle(state, line_n, waited_s):
    """The device was not idle within the wait before a wake clip; the clip was not played."""
    since = f" since L{line_n}" if line_n else ""
    return Verdict(False, "not_idle", f"device {state}{since}, not idle after {waited_s:.0f} s; wake clip not played",
                   [line_n] if line_n else [])


def cue_name(lines, server):
    """The cue played after the wake: device `<< <cue>`, else server `wake cue: <cue>`, else None."""
    wake = first(lines, WAKE_RE)
    cue = first(lines, CUE_RE, wake) if wake else None
    if cue:
        return CUE_RE.search(cue.text).group(1)
    srv = next((m for m in map(SERVER_CUE_RE.search, server or []) if m), None) if wake else None
    return srv.group(1) if srv else None


def judge_wake_cue(lines, plays, server):
    """One wake: pass when a cue played (device `<< <cue>` or server `wake cue:`) and the "Yes."
    that followed got a "yes" transcript and a reply. A transcript other than "yes" before it, or
    within PREROLL_S of the clip start when there is none, is the wake pre-roll turned into a turn."""
    wake = first(lines, WAKE_RE)
    if wake is None:
        return Verdict(False, "no_wake", "no `Wake word detected`", [])
    cue = first(lines, CUE_RE, wake)
    name = cue_name(lines, server)
    if name is None:
        skip = next((m for m in map(SERVER_CUE_SKIP_RE.search, server or []) if m), None)
        why = (f"server: skipped, {skip.group(1)}" if skip else
               "server log unavailable" if server is None else "no cue on device or server")
        return Verdict(False, "cue_missing", f"wake L{wake.n}, cue missing ({why})", ev(wake))
    played = f"cue `{name}` " + (f"L{cue.n}" if cue else "(server only)")
    yes = next((p for p in plays if p.text == "Yes."), None)
    stts = [l for l in lines if l.n > wake.n and STT_RE.search(l.text)]
    yes_stt = next((l for l in stts if YES_RE.search(STT_RE.search(l.text).group(1))), None)
    others = [l for l in stts if l is not yes_stt and not YES_RE.search(STT_RE.search(l.text).group(1))]

    def preroll(l):
        if yes_stt is not None:
            return l.n < yes_stt.n
        return yes is None or l.t <= yes.t + PREROLL_S

    junk = [l for l in others if preroll(l)]
    misheard = [l for l in others if not preroll(l)]
    reply = first(lines, REPLY_RE, yes_stt) if yes_stt else None
    if yes is None:
        clip = plays[0] if plays else None
        answer = "'Yes.' not played" + (f" (cue {cue.t - clip.t:.0f} s after the wake clip)" if cue and clip else "")
    elif yes_stt and reply:
        answer = f"'Yes.' -> {heard(yes_stt)} L{yes_stt.n}, reply L{reply.n}"
    elif yes_stt:
        answer = f"'Yes.' -> {heard(yes_stt)} L{yes_stt.n}, no reply"
    elif misheard:
        answer = f"'Yes.' heard as {heard(misheard[0])} L{misheard[0].n}"
    else:
        answer = "'Yes.' got no transcript"
    dropped = server_drops(server)
    if dropped:
        answer += f"; server dropped {dropped} during playback"
    evidence = ev(wake, cue, *junk, yes_stt, reply, *misheard[:1])
    if junk:
        turns = ", ".join(f"{heard(l)} L{l.n}" for l in junk)
        return Verdict(False, "preroll_turn", f"{played}; pre-roll became a turn: {turns}; {answer}", evidence)
    if yes is None:
        return Verdict(False, "cue_late", f"{played}; {answer}", evidence)
    if not (yes_stt and reply):
        return Verdict(False, "no_answer", f"{played}; {answer}", evidence)
    return Verdict(True, "pass", f"{played}; {answer}", evidence)


def judge_turn(lines, request_re):
    """A spoken request: pass when its transcript holds request_re and a reply played."""
    stt, reply = _request(lines, request_re)
    bad = _no_turn(lines, stt, reply, request_re, "")
    if bad:
        return bad
    if not request_re.search(stt.text):
        return Verdict(False, "wrong_transcript", f"heard {heard(stt)} L{stt.n}, reply L{reply.n}", ev(stt, reply))
    return Verdict(True, "pass", f"heard {heard(stt)} L{stt.n}, reply L{reply.n}", ev(stt, reply))


def judge_double_tap(lines, request_re):
    """A double-tap during the reply: pass on `IMU gesture: double_tap`, then `Abort speaking`,
    then speaking -> listening. A trial with no reply, or whose reply ended first, fails with
    the reason the tap was not sent."""
    stt, reply = _request(lines, request_re)
    bad = _no_turn(lines, stt, reply, request_re, "; tap not sent")
    if bad:
        return bad
    said = f"heard {heard(stt)} L{stt.n}{_mismatch(stt, request_re)}, reply L{reply.n}"
    inj = first(lines, injected_re("double_tap"), reply)
    g = first(lines, gesture_re("double_tap"), reply)
    if g is None:
        if inj is not None:
            return Verdict(False, "no_gesture", f"{said}; tap injected L{inj.n}, no `IMU gesture`", ev(stt, reply, inj))
        end = first(lines, SPEAK_END_RE, reply)
        if end is not None:
            return Verdict(False, "reply_ended", f"{said}; reply ended L{end.n} before the tap; tap not sent",
                           ev(stt, reply, end))
        return Verdict(False, "tap_not_sent", f"{said}; tap not sent", ev(stt, reply))
    a = first(lines, ABORT_RE, g)
    if a is None:
        return Verdict(False, "no_abort", f"{said}; tap L{g.n}, no `Abort speaking`", ev(stt, reply, g))
    s = first(lines, TO_LISTENING_RE, a)
    if s is None:
        return Verdict(False, "no_listening", f"{said}; tap L{g.n}, abort L{a.n}, no speaking -> listening",
                       ev(stt, reply, g, a))
    return Verdict(True, "pass", f"{said}; tap L{g.n}, abort L{a.n}, speaking -> listening L{s.n}",
                   ev(stt, reply, g, a, s))


def judge_long_reply(lines, server, request_re, min_s=LONG_REPLY_S):
    """Reply duration from the TTS start (-> speaking after the transcript) to the TTS stop
    (speaking -> listening|idle); pass at min_s or more with no device or server abort."""
    stt, reply = _request(lines, request_re)
    bad = _no_turn(lines, stt, reply, request_re, "")
    if bad:
        return bad
    said = f"heard {heard(stt)} L{stt.n}{_mismatch(stt, request_re)}"
    start = first(lines, SPEAK_START_RE, stt)
    end = first(lines, SPEAK_END_RE, start) if start else None
    aborts = [l for l in lines if l.n > stt.n and ABORT_RE.search(l.text)]
    srv = server_count(server, SERVER_ABORT_RE)
    counts = f"device aborts {len(aborts)}, server aborts {'n/a' if srv is None else srv}"
    if start is None or end is None:
        what = "no TTS start" if start is None else f"TTS start L{start.n}, no TTS stop"
        return Verdict(False, "no_tts_stop", f"{said}; {what}; {counts}", ev(stt, reply, start, *aborts))
    secs = end.t - start.t
    span = f"TTS start L{start.n} -> stop L{end.n} ({SPEAK_END_RE.search(end.text).group(1)}) {secs:.0f} s"
    reason = f"{said}; {span}; {counts}"
    evidence = ev(stt, start, end, *aborts)
    if aborts or srv:
        return Verdict(False, "aborted", reason, evidence)
    if srv is None:
        return Verdict(False, "no_server_log", reason, evidence)
    if secs < min_s:
        return Verdict(False, "short", reason, evidence)
    return Verdict(True, "pass", reason, evidence)


def judge_barge_in(lines, plays, server, flag, request_re, barge_text, question_re, answer_re):
    """The barge_text clip played during the reply. VOICE_BARGE_IN=0: pass on no device or server
    abort, no turn from the phrase while the reply plays, and the reply's TTS stop. =1: pass on an
    abort, a transcript holding question_re and a reply holding answer_re. `server` covers the
    time from the phrase on."""
    stt, reply = _request(lines, request_re)
    bad = _no_turn(lines, stt, reply, request_re, "; nothing to interrupt")
    if bad:
        return bad
    said = f"VOICE_BARGE_IN={flag}; heard {heard(stt)} L{stt.n}{_mismatch(stt, request_re)}, reply L{reply.n}"
    barge = next((p for p in plays if p.text == barge_text), None)
    if barge is None:
        end = first(lines, SPEAK_END_RE, reply)
        return Verdict(False, "reply_ended", f"{said}; reply ended{f' L{end.n}' if end else ''} before the phrase",
                       ev(stt, reply, end))
    dev = first(lines, ABORT_RE, barge)
    srv = server_count(server, SERVER_ABORT_RE)
    drop = server_drops(server)
    end = first(lines, SPEAK_END_RE, barge)
    stt2 = first(lines, STT_RE, barge)
    reply2 = first(lines, REPLY_RE, stt2) if stt2 else None
    aborts = f"abort device {f'L{dev.n}' if dev else 'none'}, server {'n/a' if srv is None else srv}"
    phrase = f"phrase L{barge.n}"
    evidence = ev(stt, reply, dev, end, stt2, reply2)
    if flag == 0:
        if dev or srv:
            return Verdict(False, "aborted", f"{said}; {phrase}; {aborts} (expected none)", evidence)
        if stt2 is not None and (end is None or stt2.n < end.n):
            return Verdict(False, "turn_during_playback", f"{said}; {phrase}; became a turn: {heard(stt2)} L{stt2.n}",
                           evidence)
        if end is None:
            return Verdict(False, "no_tts_stop", f"{said}; {phrase}; {aborts}; no TTS stop", evidence)
        dropped = f"server dropped {drop}" if drop else "no server drop line"
        return Verdict(True, "pass", f"{said}; {phrase}; {aborts}; played out: TTS stop L{end.n} "
                       f"{end.t - barge.t:.0f} s later; {dropped}", evidence)
    if not (dev or srv):
        return Verdict(False, "no_abort", f"{said}; {phrase}; {aborts}", evidence)
    if stt2 is None:
        return Verdict(False, "no_transcript", f"{said}; {phrase}; {aborts}; no transcript after it", evidence)
    if not question_re.search(stt2.text):
        return Verdict(False, "wrong_transcript", f"{said}; {phrase}; {aborts}; heard {heard(stt2)} L{stt2.n}", evidence)
    if reply2 is None or not answer_re.search(reply2.text):
        got = f"reply L{reply2.n} without the answer" if reply2 else "no reply"
        return Verdict(False, "no_answer", f"{said}; {phrase}; {aborts}; heard {heard(stt2)} L{stt2.n}, {got}", evidence)
    return Verdict(True, "pass", f"{said}; {phrase}; {aborts}; heard {heard(stt2)} L{stt2.n}, answered L{reply2.n}",
                   evidence)


def playback_starts(lines, plays):
    """Host times at which a speaker started playing: device TTS start and each sentence, and Mac clips."""
    return sorted([l.t for l in lines if SPEAK_START_RE.search(l.text) or SENTENCE_RE.search(l.text)] +
                  [p.t for p in plays])


def unprompted_pick_ups(lines, plays):
    """[(line, seconds since the latest playback start within VIBRATION_S, or None)] for each
    `IMU gesture: pick_up` not logged right after `IMU diag: injected pick_up`."""
    starts = playback_starts(lines, plays)
    pick, inj = gesture_re("pick_up"), injected_re("pick_up")
    out = []
    for i, l in enumerate(lines):
        if not pick.search(l.text):
            continue
        if any(inj.search(p.text) and l.t - p.t <= INJECT_GAP_S for p in lines[max(0, i - 3):i]):
            continue
        near = [l.t - s for s in starts if 0 <= l.t - s <= VIBRATION_S]
        out.append((l, min(near) if near else None))
    return out


def vibration_summary(lines, plays):
    found = unprompted_pick_ups(lines, plays)
    vib = [f"L{l.n}" for l, d in found if d is not None]
    other = [f"L{l.n}" for l, d in found if d is None]
    return (f"unprompted pick_up in the session: {len(found)} (speaker_vibration {len(vib)}"
            f"{': ' + ' '.join(vib[:6]) if vib else ''}; other {len(other)}{': ' + ' '.join(other[:6]) if other else ''})")


def judge_pick_up(lines, plays, dimmed=True):
    """One injected pick_up after the dim: pass on the gesture and a brightness restore. An
    unprompted pick_up in the trial within VIBRATION_S of a playback start makes the trial
    speaker_vibration (not counted); any other unprompted one is a false positive."""
    found = unprompted_pick_ups(lines, plays)
    other = [l for l, d in found if d is None]
    if other:
        return Verdict(False, "false_positive", f"unprompted pick_up L{other[0].n}, no playback start within "
                       f"{VIBRATION_S:.0f} s", ev(*other))
    if found:
        l, d = found[0]
        return Verdict(None, "speaker_vibration", f"pick_up L{l.n} {d:.1f} s after a playback start; not counted",
                       ev(*[l for l, _ in found]))
    if not dimmed:
        return Verdict(False, "no_dim", "no dim (brightness 20) within the wait", [])
    inj = first(lines, injected_re("pick_up"))
    if inj is None:
        return Verdict(False, "not_injected", "no `IMU diag: injected pick_up`", [])
    g = first(lines, gesture_re("pick_up"), inj)
    u = first(lines, UNDIM_RE, inj)
    if g is None:
        return Verdict(False, "no_gesture", f"injected L{inj.n}, no `IMU gesture: pick_up`", ev(inj))
    if u is None:
        return Verdict(False, "no_undim", f"gesture L{g.n}, brightness not restored", ev(g))
    return Verdict(True, "pass", f"gesture L{g.n}, brightness {UNDIM_RE.search(u.text).group(1)} L{u.n}", ev(g, u))


def judge_bedside(lines, plays, night_text, auto_text, night_re, auto_re):
    """Voice night hint (device `bedside hint=night`, the tool call), the bedside page after idle,
    a tap leaving it, then the voice auto hint."""
    def segment(play, until):
        if play is None:
            return []
        return [l for l in lines if l.n > play.n and (until is None or l.n < until.n)]

    night = next((p for p in plays if p.text.endswith(night_text)), None)
    auto = next((p for p in plays if p.text.endswith(auto_text)), None)
    seg_n, seg_a = segment(night, auto), segment(auto, None)
    parts, evidence, cls = [], [], None
    for label, seg, want, rx in (("night", seg_n, "night", night_re), ("auto", seg_a, "auto", auto_re)):
        stt = first(seg, STT_RE)
        hint = next((l for l in seg if (m := HINT_RE.search(l.text)) and m.group(1) == want), None)
        part = f"{label}: " + (f"heard {heard(stt)} L{stt.n}" if stt else "no transcript")
        part += f", hint={want} L{hint.n}" if hint else ", no tool call (no hint)"
        evidence += ev(stt, hint)
        if cls is None and stt is None:
            cls = "no_transcript"
        elif cls is None and hint is None:
            cls = "no_tool_call"
        if label == "night":
            on = first(seg, BEDSIDE_ON_RE, hint)
            off = first(seg, BEDSIDE_TOUCH_OFF_RE, on) if on else None
            part += (f", bedside=on L{on.n}" if on else ", no bedside page") + (f", tap off L{off.n}" if off else "")
            evidence += ev(on, off)
            if cls is None and on is None:
                cls = "no_page"
            elif cls is None and off is None:
                cls = "no_touch_exit"
        parts.append(part)
    return Verdict(cls is None, cls or "pass", "; ".join(parts), evidence)


def judge_earcons(lines, run):
    """A held Home Assistant call made through the agent service and approved by a tap on the device
    card. run: the driver's agent-service evidence (task_id, error, actionable, taps, decision,
    status). Pass on `earcon=needs_you`, the task's card open, the device receipt of the approve
    `result=accepted`, `earcon=approved` after it, and the service's approve decision with the
    replayed call's status in REPLAYED."""
    task = run.get("task_id")
    if task is None:
        return Verdict(False, "agent_error", run.get("error") or "no task", [])
    head = f"task {task}"
    if not run.get("actionable"):
        return Verdict(False, "no_hold", f"{head}: {run.get('error') or 'no approvable hold'}", [])
    earcons = [(l, EARCON_RE.search(l.text)) for l in lines if EARCON_RE.search(l.text)]
    needs = next((l for l, m in earcons if m.group(1) == "needs_you" and not m.group(2)), None)
    needs_skipped = next((l for l, m in earcons if m.group(1) == "needs_you" and m.group(2)), None)
    card = first(lines, card_open_re(task))
    receipts = [(l, RECEIPT_RE.search(l.text).group(1)) for l in lines if RECEIPT_RE.search(l.text)]
    accepted = next((l for l, r in receipts if r == "accepted"), None)
    approved = first(lines, APPROVED_RE, accepted) if accepted else None
    decision, status = run.get("decision"), run.get("status")
    parts = [
        f"earcon=needs_you L{needs.n}" if needs else
        (f"needs_you{EARCON_RE.search(needs_skipped.text).group(2)} L{needs_skipped.n}" if needs_skipped
         else "no needs_you"),
        f"card open L{card.n}" if card else "no card open",
        f"{run.get('taps', 0)} tap(s)",
        f"receipt accepted L{accepted.n}" if accepted else
        ("receipts " + ", ".join(f"{r} L{l.n}" for l, r in receipts) if receipts else "no receipt"),
        f"earcon=approved L{approved.n}" if approved else "no approved",
        f"service: decision {decision or 'none'}, {run.get('tool', 'call')} {status or 'not run'}",
    ]
    reason = f"{head}: " + ", ".join(parts)
    evidence = ev(needs or needs_skipped, card, accepted or (receipts[0][0] if receipts else None), approved)
    if card is None:
        return Verdict(False, "no_card", reason, evidence)
    if accepted is None:
        return Verdict(False, "not_accepted", reason, evidence)
    if needs is None:
        return Verdict(False, "no_needs_you", reason, evidence)
    if approved is None:
        return Verdict(False, "no_approved", reason, evidence)
    if decision != "approve" or status not in REPLAYED:
        return Verdict(False, "not_replayed", reason, evidence)
    return Verdict(True, "pass", reason, evidence)


def parse_serial_log(text):
    """(lines, plays) from a session serial log: `[ seconds HH:MM:SSZ] text` lines numbered from 1;
    `### say: <text>` notes become plays, other notes are dropped."""
    lines, plays = [], []
    for n, raw in enumerate(text.splitlines(), 1):
        m = re.match(r"\[\s*([\d.]+) [\d:]+Z\] (.*)$", raw)
        if not m:
            continue
        t, body = float(m.group(1)), m.group(2)
        if body.startswith("### say: "):
            plays.append(Play(n, t, body[len("### say: "):]))
        elif not body.startswith("###"):
            lines.append(Line(n, t, body))
    return lines, plays
