// Host test for weixue_session.h and face_state.h: firmware/tests/run.sh
#include "face_state.h"
#include "weixue_session.h"

#include <cstdio>
#include <memory>
#include <string>
#include <vector>

namespace {

using namespace weixue_session;

int failures = 0;

void Check(bool ok, const char* what) {
    std::printf("%s %s\n", ok ? "PASS" : "FAIL", what);
    if (!ok) {
        failures++;
    }
}

void TestMuteFence() {
    MuteFence fence;
    bool muted = false;

    uint32_t before_mute = fence.generation();
    Check(fence.AcceptTtsStart(muted, before_mute), "tts start accepted while unmuted");

    // A `tts start` and `tts stop` scheduled before the mute run after it.
    muted = true;
    fence.OnMute();
    Check(!fence.AcceptTtsStart(muted, before_mute), "tts start from before a mute dropped (muted)");
    Check(!fence.AcceptTtsStop(before_mute), "tts stop from before a mute dropped");
    Check(!fence.AcceptTtsStart(muted, fence.generation()), "tts start refused while muted");
    muted = false;
    Check(!fence.AcceptTtsStart(muted, before_mute),
          "tts start from before a mute dropped after unmute");

    uint32_t after_unmute = fence.generation();
    Check(fence.AcceptTtsStart(muted, after_unmute), "tts start after unmute accepted");
    Check(fence.AcceptTtsStop(after_unmute), "tts stop after unmute accepted");

    Check(!MuteFence::DecodeIncomingAudio(true, true), "late reply frame dropped while muted");
    Check(!MuteFence::DecodeIncomingAudio(false, false), "reply frame dropped when not speaking");
    Check(MuteFence::DecodeIncomingAudio(true, false), "reply frame decoded while speaking");
    Check(!MuteFence::SendMicAudio(true), "no microphone audio sent while muted");
    Check(MuteFence::SendMicAudio(false), "microphone audio sent while unmuted");
    Check(!MuteFence::StartListening(true), "listening refused while muted");
    Check(MuteFence::StartListening(false), "listening allowed while unmuted");
}

void TestActionsOnMute() {
    // A channel past its receive timeout: not opened, transport still up.
    MuteActions timed_out = ActionsOnMute(true, false, false, true);
    Check(timed_out.close_transport, "mute closes a timed-out channel's transport");
    Check(!timed_out.abort_speaking, "no abort on a timed-out channel");
    Check(timed_out.to_idle, "mute in a session returns to idle");

    MuteActions speaking = ActionsOnMute(true, true, true, true);
    Check(speaking.abort_speaking && speaking.close_transport && speaking.to_idle,
          "mute while speaking aborts, closes and returns to idle");

    MuteActions idle = ActionsOnMute(true, false, false, false);
    Check(idle.close_transport && !idle.to_idle, "mute while idle still closes the transport");

    MuteActions none = ActionsOnMute(false, false, false, true);
    Check(!none.close_transport && !none.abort_speaking, "no protocol: nothing to close");
}

void TestWakeAudioHold() {
    WakeAudioHold<int, 50> hold;
    for (int i = 1; i <= 10; i++) {
        hold.Push(i);
    }
    std::vector<int> sent;
    size_t n = hold.Drain([&sent](int p) { sent.push_back(p); });
    bool in_order = n == 10 && sent.size() == 10;
    for (int i = 0; in_order && i < 10; i++) {
        in_order = sent[i] == i + 1;
    }
    Check(in_order, "held packets drained in capture order");
    Check(hold.size() == 0, "hold empty after draining");

    for (int i = 1; i <= 60; i++) {
        hold.Push(i);
    }
    Check(hold.size() == 50, "hold bounded at 50 packets");
    sent.clear();
    hold.Drain([&sent](int p) { sent.push_back(p); });
    Check(sent.size() == 50 && sent.front() == 11 && sent.back() == 60,
          "overflow drops the oldest packets");

    hold.Push(1);
    hold.Clear();
    Check(hold.size() == 0, "clear drops held packets");

    WakeAudioHold<std::unique_ptr<int>, 2> owned;
    owned.Push(std::make_unique<int>(1));
    owned.Push(std::make_unique<int>(2));
    owned.Push(std::make_unique<int>(3));
    int sum = 0;
    owned.Drain([&sum](std::unique_ptr<int> p) { sum += *p; });
    Check(sum == 5, "move-only packets held and drained");
}

constexpr int64_t kS = 1000 * 1000;
constexpr int64_t kFreshMs = 90000;

void TestBackendRow() {
    PollAge never = PollAgeAt(5 * kS, 0, 0);
    Check(never.age_ms == -1, "no poll yet: age -1");
    Check(BackendRowFor(false, false, 0, never.age_ms, never.polled_since_resume, kFreshMs) ==
              BackendRow::kChecking,
          "no poll yet: checking");

    PollAge recent = PollAgeAt(20 * kS, 10 * kS, 0);
    Check(recent.age_ms == 10000 && recent.polled_since_resume, "age from the last poll");

    // A 10 min session: the last poll finished before it, polling resumed 5 s ago.
    PollAge after_session = PollAgeAt(605 * kS, 10 * kS, 600 * kS);
    Check(after_session.age_ms == 5000 && !after_session.polled_since_resume,
          "age counted from the resume after a long session");
    Check(BackendRowFor(false, false, 200, after_session.age_ms, after_session.polled_since_resume,
                        kFreshMs) == BackendRow::kOnline,
          "healthy backend online after a long session");
    Check(BackendRowFor(false, false, 204, after_session.age_ms, after_session.polled_since_resume,
                        kFreshMs) == BackendRow::kOnline,
          "204 poll is online");

    // The last poll before a mute failed; unmuted 5 s ago, a poll is in flight.
    Check(BackendRowFor(false, false, 0, after_session.age_ms, after_session.polled_since_resume,
                        kFreshMs) == BackendRow::kChecking,
          "failure from before the pause shows checking");

    PollAge failed_since = PollAgeAt(640 * kS, 630 * kS, 600 * kS);
    Check(failed_since.polled_since_resume && failed_since.age_ms == 10000,
          "poll after the resume counts from that poll");
    Check(BackendRowFor(false, false, 0, failed_since.age_ms, failed_since.polled_since_resume,
                        kFreshMs) == BackendRow::kOffline,
          "failure after the resume is offline");
    Check(BackendRowFor(false, false, 502, failed_since.age_ms, failed_since.polled_since_resume,
                        kFreshMs) == BackendRow::kOffline,
          "HTTP error after the resume is offline");

    PollAge stale = PollAgeAt(200 * kS, 10 * kS, 0);
    Check(BackendRowFor(false, false, 200, stale.age_ms, stale.polled_since_resume, kFreshMs) ==
              BackendRow::kOffline,
          "no poll for 190 s is offline");

    Check(BackendRowFor(true, true, 0, -1, false, kFreshMs) == BackendRow::kSession,
          "open channel is session");
    Check(BackendRowFor(false, true, 200, 1000, true, kFreshMs) == BackendRow::kPaused,
          "muted is paused");
}

void TestThinking() {
    ThinkingTurn turn;
    Check(!turn.Thinking(0), "not thinking before a transcript");
    turn.OnTranscript(3);
    Check(turn.Thinking(3), "thinking after a transcript");
    Check(turn.Thinking(3), "thinking has no time limit (no tts yet)");
    Check(!turn.Thinking(4), "the turn's tts stop ends thinking");
    turn.OnTranscript(4);
    Check(turn.Thinking(4), "next transcript thinks again");
    turn.End();
    Check(!turn.Thinking(4), "first sentence, error or idle ends thinking");
}

void TestReplyText() {
    ReplyText reply(240);
    reply.Add("Hello.", 1);
    reply.Add("It is ten.", 1);
    Check(reply.text() == "Hello. It is ten.", "sentences of one reply joined");
    reply.Add("Next turn.", 2);
    Check(reply.text() == "Next turn.", "a new reply replaces the previous one");
    Check(!reply.Add("", 3) && reply.text() == "Next turn.", "empty sentence ignored");
    Check(!reply.Add(nullptr, 3) && reply.text() == "Next turn.", "null sentence ignored");
    reply.Clear();
    Check(reply.text().empty(), "a transcript clears the reply");

    ReplyText small(8);
    small.Add("\xe4\xbd\xa0\xe5\xa5\xbd\xe4\xb8\x96\xe7\x95\x8c", 1);  // 4 CJK characters
    Check(small.text() == "\xe4\xbd\xa0\xe5\xa5\xbd", "cap keeps whole UTF-8 characters");
}

}  // namespace

int main() {
    TestMuteFence();
    TestActionsOnMute();
    TestWakeAudioHold();
    TestBackendRow();
    TestThinking();
    TestReplyText();
    std::printf("%s (%d failures)\n", failures == 0 ? "OK" : "FAILED", failures);
    return failures == 0 ? 0 : 1;
}
