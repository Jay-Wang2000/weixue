// Host test for weixue_agent_outbox.h: firmware/tests/run.sh
#include "weixue_agent_outbox.h"

#include <cstdio>
#include <string>
#include <vector>

namespace {

using weixue_agent::Outbox;

int failures = 0;

void Check(bool ok, const char* what) {
    std::printf("%s %s\n", ok ? "PASS" : "FAIL", what);
    if (!ok) {
        failures++;
    }
}

struct Result {
    int calls = 0;
    bool ok = false;
    Outbox::Done Bind() {
        return [this](bool v) {
            calls++;
            ok = v;
        };
    }
};

void TestPlainEntry() {
    Outbox box;
    Result r;
    box.Push("shown", "shown", 1, false, 0, r.Bind());
    Outbox::Entry* e = box.Ready(0);
    Check(e != nullptr && e->attempts == 1 && box.Ready(0) == nullptr, "one send in flight");
    box.Sent(true, 0);
    Check(r.calls == 1 && r.ok && box.Empty(), "a plain entry is done once it reached the transport");
}

void TestTransportRetries() {
    Outbox box;
    Result r;
    box.Push("p", "p", 1, false, 0, r.Bind());
    int64_t t = 0;
    for (int i = 0; i < Outbox::kMaxAttempts; ++i) {
        Check(box.Ready(t - 1) == nullptr || i == 0, "not before its retry time");
        Check(box.Ready(t) != nullptr, "retry due");
        box.Sent(false, t);
        if (i < Outbox::kMaxAttempts - 1) t += Outbox::kRetryDelayUs[i];
    }
    Check(r.calls == 1 && !r.ok && box.Empty(), "dropped after the last attempt (1, 3, 9 s)");
}

void TestReceiptFlow() {
    Outbox box;
    Result approve, shown;
    box.Push("approve", "approve", 3, true, 0, approve.Bind());
    box.Push("shown", "shown", 4, false, 0, shown.Bind());
    box.Ready(0);
    box.Sent(true, 0);
    Check(approve.calls == 0 && box.awaiting() == 1, "a decision that reached the transport waits for its receipt");
    Check(box.Ready(0) != nullptr, "the wait does not block the next entry");
    box.Sent(true, 0);
    Check(shown.calls == 1, "next entry sent");
    Check(!box.Receipt(9, true), "a receipt of another seq matches nothing");
    Check(box.Receipt(3, true) && approve.calls == 1 && approve.ok && box.Empty(), "the receipt completes it");
}

// The session closes before the receipt: the decision is sent again until one arrives.
void TestResendUntilReceipt() {
    Outbox box;
    Result r;
    box.Push("approve", "approve", 3, true, 0, r.Bind());
    box.Ready(0);
    box.Sent(true, 0);
    box.Tick(Outbox::kReceiptWaitUs - 1);
    Check(box.awaiting() == 1, "still waiting before kReceiptWaitUs");
    int64_t t = Outbox::kReceiptWaitUs;
    box.Tick(t);
    Check(box.awaiting() == 0 && box.queued() == 1 && box.Ready(t) == nullptr, "no receipt: queued for a resend");
    t += Outbox::kRetryDelayUs[0];
    Outbox::Entry* e = box.Ready(t);
    Check(e != nullptr && e->attempts == 2 && e->payload == "approve" && e->seq == 3, "same payload and seq again");
    box.Sent(true, t);
    Check(box.Receipt(3, true) && r.calls == 1 && r.ok, "receipt of the resend completes it");
}

void TestReceiptBeforeTransportOutcome() {
    Outbox box;
    Result r;
    box.Push("approve", "approve", 5, true, 0, r.Bind());
    box.Ready(0);
    Check(box.Receipt(5, true) && r.calls == 0, "receipt during the own-session send is kept");
    box.Sent(true, 0);
    Check(r.calls == 1 && r.ok && box.Empty(), "completed when the send returns");
}

void TestNoReceiptGivesUp() {
    Outbox box;
    Result r;
    box.Push("approve", "approve", 6, true, 0, r.Bind());
    int64_t t = 0;
    for (int i = 0; i < Outbox::kMaxAttempts; ++i) {
        Check(box.Ready(t) != nullptr, "attempt due");
        box.Sent(true, t);
        t += Outbox::kReceiptWaitUs;
        box.Tick(t);
        if (i < Outbox::kMaxAttempts - 1) t += Outbox::kRetryDelayUs[i];
    }
    Check(r.calls == 1 && !r.ok && box.Empty(), "given up after the last attempt without a receipt");
}

void TestRefusedAndLate() {
    Outbox box;
    Result r;
    box.Push("approve", "approve", 7, true, 0, r.Bind());
    box.Ready(0);
    box.Sent(true, 0);
    box.Tick(Outbox::kReceiptWaitUs);
    Check(box.Receipt(7, false) && r.calls == 1 && !r.ok && box.Empty(),
          "a late receipt still completes a queued resend; stale is not recorded");
}

void TestCancel() {
    Outbox box;
    Result a, b, c;
    uint32_t ia = box.Push("a", "a", 1, true, 0, a.Bind());
    uint32_t ib = box.Push("b", "b", 2, true, 0, b.Bind());
    box.Ready(0);
    box.Cancel(ia);
    box.Cancel(ib);
    box.Sent(true, 0);
    Check(a.calls == 0 && b.calls == 0 && box.Empty(), "cancelled entries never call done, in flight or queued");
    uint32_t ic = box.Push("c", "c", 3, true, 0, c.Bind());
    box.Ready(0);
    box.Sent(true, 0);
    box.Cancel(ic);
    Check(box.Empty() && !box.Receipt(3, true) && c.calls == 0, "cancelled while awaiting its receipt");
}

void TestFull() {
    Outbox box;
    std::vector<Result> rs(Outbox::kSize + 1);
    for (size_t i = 0; i < Outbox::kSize; ++i) box.Push("x", "x", i + 1, false, 0, rs[i].Bind());
    box.Ready(0);
    box.Push("y", "y", 99, false, 0, rs[Outbox::kSize].Bind());
    Check(rs[0].calls == 0 && rs[1].calls == 1 && !rs[1].ok, "full: the oldest entry not in flight is dropped");
    Check(box.queued() == Outbox::kSize, "size kept");
}

void TestShownCoalesce() {
    Outbox box;
    box.Push("shown", "shown", 1, false, 0);
    box.Back()->shown = true;
    Check(box.QueuedShown() != nullptr, "queued shown found");
    box.Ready(0);
    Check(box.QueuedShown() == nullptr, "in-flight shown is not coalesced into");
}

}  // namespace

int main() {
    TestPlainEntry();
    TestTransportRetries();
    TestReceiptFlow();
    TestResendUntilReceipt();
    TestReceiptBeforeTransportOutcome();
    TestNoReceiptGivesUp();
    TestRefusedAndLate();
    TestCancel();
    TestFull();
    TestShownCoalesce();
    std::printf("%s: %d failure(s)\n", failures ? "FAILED" : "OK", failures);
    return failures ? 1 : 0;
}
