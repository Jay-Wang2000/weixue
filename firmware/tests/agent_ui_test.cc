// Host test for agent_ui.h (store -> face, ring, cards, management rows, chats) and the
// RingState arcs style: firmware/tests/run.sh
#include "agent_ui.h"
#include "voice_ring.h"

#include <cstdio>
#include <string>
#include <vector>

namespace {

using namespace agent_ui;
using weixue_agent::ApplyResult;
using weixue_agent::Item;
using weixue_agent::Store;

int failures = 0;

void Check(bool ok, const char* what) {
    std::printf("%s %s\n", ok ? "PASS" : "FAIL", what);
    if (!ok) failures++;
}

int64_t g_seq = 100;

Item MakeItem(const std::string& task, State state, Tier tier = Tier::kWhenIdle,
              const std::string& chat = "c1") {
    Item it;
    it.seq = ++g_seq;
    it.chat_id = chat;
    it.task_id = task;
    it.state = state;
    it.tier = tier;
    it.title = "Title " + task;
    it.summary = "Summary " + task;
    return it;
}

Item Approval(const std::string& task, Tier tier = Tier::kWhenIdle, int timeout_s = 300) {
    Item it = MakeItem(task, State::kNeedsApproval, tier);
    it.card_id = "i_" + task;
    it.risk = Risk::kReversible;
    it.timeout_s = timeout_s;
    it.summary = "Turn on living room lights";
    it.options = {{"approve", "Approve"}, {"deny", "Deny"}};
    return it;
}

Item Question(const std::string& task, int n_options, Tier tier = Tier::kWhenIdle) {
    Item it = MakeItem(task, State::kNeedsInput, tier);
    it.card_id = "q_" + task;
    it.summary = "Which room should I start with?";
    const char* labels[] = {"Kitchen", "Bedroom", "Hallway", "All"};
    for (int i = 0; i < n_options; i++) it.options.push_back({std::string("o") + char('1' + i), labels[i]});
    return it;
}

struct Rig {
    Store store;
    Model model;
    std::vector<Arrival> last;
    std::vector<Arrival> Apply(const Item& it, int64_t now) {
        store.Apply(it, now);
        last = model.SetTasks(store.tasks(), now, "09:40");
        return last;
    }
};

Context IdleCtx(int64_t idle_since) {
    Context c;
    c.voice_idle = true;
    c.idle_since_ms = idle_since;
    c.last_touch_ms = -100000;
    return c;
}

void TestFormatting() {
    Check(FormatRemaining(300000) == "5:00", "remaining 5:00");
    Check(FormatRemaining(298001) == "4:59", "remaining rounds up to the second");
    Check(FormatRemaining(-5) == "0:00", "remaining clamps at 0:00");
    Check(CountdownText(298000) == "5:00", "countdown shows whole minutes above 10 s");
    Check(CountdownText(61000) == "2:00", "countdown 61 s is 2:00");
    Check(CountdownText(60000) == "1:00", "countdown 60 s is 1:00");
    Check(CountdownText(9500) == "0:10", "countdown per second in the last 10 s");
    Check(CountdownText(3000) == "0:03", "countdown 0:03");
    Check(FormatAge(30000) == "now" && FormatAge(120000) == "2 min" && FormatAge(3 * 3600000LL) == "3 h" &&
              FormatAge(2 * 86400000LL) == "2 d",
          "ages now / min / h / d");
    Check(Plain("a#ff0000 b#") == "a ff0000 b ", "recolour markers stripped");
    Check(ClockToLvDeg(0) == 270 && ClockToLvDeg(150) == 60 && ClockToLvDeg(25) == 295,
          "clock to LVGL degrees");
}

void TestWorking() {
    Rig r;
    Item w = MakeItem("w", State::kWorking);
    w.has_progress = true;
    w.progress_step = 2;
    w.progress_total = 5;
    auto arrivals = r.Apply(w, 0);
    Check(arrivals.size() == 1 && EarconFor(arrivals[0]) == Earcon::kNone, "working arrival is silent");
    Complication c = r.model.Face(1000);
    Check(c.show && c.glyph == Glyph::kWorking && c.pose == Pose::kMini, "working: mini face + progress glyph");
    Check(c.main == "Title w" && c.main_color == color::kText, "working main line is the title");
    Check(Join(c.status) == "Step 2 of 5", "working status step");
    Check(c.progress > 0.39f && c.progress < 0.41f, "progress 2/5");
    Check(Join(r.model.Face(3 * 60000).status) == "Step 2 of 5 · 3 min · 3 min ago",
          "freshness after 120 s without an update");
    Check(r.model.Face(5 * 60000).main_color == color::kValue, "main line dims after 5 min unchanged");
    Item again = w;
    again.seq = ++g_seq;
    r.Apply(again, 200000);
    Check(Join(r.model.Face(4 * 60000 + 1000).status) == "Step 2 of 5 · 4 min", "elapsed minutes since the start");

    RingHints h = r.model.Ring(1000, 0);
    Check(std::string(h.kind) == "progress" && h.count == 2, "ring: progress track + fill");
    Check(h.arcs[0].start == kProgressStart && h.arcs[0].span == kProgressSpan && h.arcs[0].color == color::kTrack,
          "progress track 150-210");
    Check(h.arcs[1].span == 24 && h.arcs[1].color == color::kEye, "progress fill = 60 deg x 2/5");
    bool breathes = false;
    uint8_t first = r.model.Ring(0, 0).arcs[1].opa;
    for (int t = 0; t < 4000; t += 100) {
        uint8_t o = r.model.Ring(t, 0).arcs[1].opa;
        if (o != first) breathes = true;
        if (o < 96 || o > 217 || (o & 0x1F) != 0) breathes = false;
    }
    Check(breathes, "progress breathes 0.25 Hz within 40-85 %, quantised");
    Check(r.model.Ring(1000, 2).arcs[0].start == kProgressStart + 2, "static cues rotate with the pixel shift");

    Item unknown = MakeItem("u", State::kWorking);
    r.Apply(unknown, 10);
    Check(Join(r.model.Face(20).status) == "Working", "newest working task first; unknown progress");
    Check(r.model.Face(20).more == 1, "+1 for the other task");
    Check(r.model.Ring(20, 0).arcs[1].span == kProgressSpan, "unknown progress fills the 60 deg");

    Item inter = MakeItem("u", State::kInterrupted);
    r.Apply(inter, 30);
    Check(r.model.Face(40).glyph == Glyph::kWorking && Join(r.model.Face(40).status) == "Interrupted",
          "interrupted keeps the working glyph");
}

void TestApprovalFlow() {
    Rig r;
    auto arrivals = r.Apply(Approval("a"), 0);
    Check(arrivals.size() == 1 && EarconFor(arrivals[0]) == Earcon::kNeedsYou, "approval chime");
    Flash f = FlashFor(arrivals[0], 0);
    Check(f.count == 2 && f.color == color::kAttention, "WHEN_IDLE needs-you flashes x2");
    r.model.StartFlash(f);
    Check(std::string(r.model.Ring(100, 0).kind) == "flash" && r.model.Ring(100, 0).count == 2, "flash on");
    Check(r.model.Ring(400, 0).count == 0, "flash off phase");
    Check(std::string(r.model.Ring(1300, 0).kind) == "ears", "ears after the flash");
    RingHints ears = r.model.Ring(1300, 0);
    Check(ears.arcs[0].start == kEarRightStart && ears.arcs[1].start == kEarLeftStart &&
              ears.arcs[0].span == kEarSpan,
          "ears 25-65 and 295-335");

    Complication c = r.model.Face(1000);
    Check(c.glyph == Glyph::kApproval && c.main == "Needs your OK" && c.main_color == color::kAttention,
          "needs approval: amber state word");
    Check(Join(c.status) == "Title a · 5:00", "status: title + countdown from timeout_s");
    Check(c.pose == Pose::kAttentive, "attentive mini face");
    Check(!c.hint.empty() && r.model.Face(11000).hint.empty(), "hint for 10 s");
    Check(Join(r.model.Face(295000).status) == "Title a · 0:05", "per second in the last 10 s");

    Context busy = IdleCtx(0);
    busy.voice_idle = false;
    Check(r.model.WantAutoOpen(busy, 2000).empty(), "WHEN_IDLE waits while busy");
    Context touched = IdleCtx(0);
    touched.last_touch_ms = 1000;
    Check(r.model.WantAutoOpen(touched, 3000).empty(), "WHEN_IDLE waits 5 s after a touch");
    Context fresh = IdleCtx(2000);
    Check(r.model.WantAutoOpen(fresh, 3000).empty(), "WHEN_IDLE waits 1.5 s of idle");
    Context panel = IdleCtx(0);
    panel.panel_shown = true;
    Check(r.model.WantAutoOpen(panel, 3000).empty(), "WHEN_IDLE waits while the management view is open");
    Check(r.model.WantAutoOpen(IdleCtx(0), 3000) == "a", "WHEN_IDLE opens once idle");

    Check(r.model.OpenCard("a", 3000), "card opens");
    CardView v = r.model.Card(3000);
    Check(v.kind == CardKind::kApproval && v.card_id == "i_a" && v.body == "Turn on living room lights",
          "approval card content");
    Check(Join(v.context) == "Title a · reversible", "context line: title · risk");
    Check(Join(v.footer) == "Say \"approve\" or \"deny\" · 5:00", "footer: voice hint + countdown");
    Check(!v.tap_only, "reversible accepts voice");
    Check(r.model.WantAutoOpen(IdleCtx(0), 3100).empty(), "no second card while one is open");

    Model::Decision d = r.model.Tap(0, 4000);
    Check(d.send && d.kind == "approve" && d.card_id == "i_a" && d.option_id == "approve" && d.chat_id == "c1",
          "tap Approve sends approve with card_id");
    Check(Join(r.model.Card(4000).footer) == "Approve · sending", "two-step: sending, not yet Approved");
    Check(!r.model.Tap(1, 4100).send, "no second decision while sending");
    Check(r.model.OnDecisionSent(d.card_id, d.token, true, 4200) == Earcon::kApproved, "approved earcon on delivery");
    v = r.model.Card(4300);
    Check(v.phase == CardPhase::kOutcome && v.outcome == "Approved" && v.outcome_color == color::kApprove,
          "big Approved");
    Check(!r.model.TickCard(5000).close && r.model.TickCard(5100).close, "outcome 900 ms, then closes");
    Check(!r.model.card_open(), "card closed");
    Complication after = r.model.Face(5200);
    Check(!after.show, "decided approval leaves the face until the server's next item");
    int more = 0;
    auto rows = r.model.Rows(5200, 3, &more);
    Check(rows.size() == 1 && Join(rows[0].status) == "Approved · sent", "row: Approved · sent");
    Check(r.model.WantAutoOpen(IdleCtx(0), 9000).empty(), "decided card does not reopen");

    Item next = MakeItem("a", State::kWorking);
    r.Apply(next, 6000);
    Check(r.model.Face(6100).glyph == Glyph::kWorking, "server's next item replaces the card");
}

void TestNotSentAndRetry() {
    Rig r;
    r.Apply(Approval("a"), 0);
    r.model.OpenCard("a", 0);
    Model::Decision d = r.model.Tap(1, 500);
    Check(d.send && d.kind == "deny", "deny sent");
    Check(r.model.TickCard(4600).close == false && Join(r.model.Card(4600).footer) == "Deny · still sending",
          "4 s in flight: still sending, not Not sent");
    Check(r.model.ExpireSends(29000).empty() && r.model.SendPending(d.token), "in flight until the hard timeout");
    std::vector<uint32_t> expired = r.model.ExpireSends(30500);
    Check(expired.size() == 1 && expired[0] == d.token && Join(r.model.Card(30500).footer) == "Not sent · tap to retry",
          "30 s without a receipt: Not sent, its outbox entry cancelled");
    Check(r.model.Card(30500).pressed == 1, "Not sent keeps the chosen control marked");
    Check(!r.model.Tap(0, 31000).send, "Not sent: a different decision is blocked");
    Model::Decision retry = r.model.Tap(1, 31100);
    Check(retry.send && retry.kind == "deny" && retry.card_id == d.card_id && retry.token != d.token,
          "retry reuses the decision record (same card_id, new send)");
    Check(r.model.OnDecisionSent(d.card_id, d.token, true, 31200) == Earcon::kNone &&
              Join(r.model.Card(31200).footer) == "Deny · sending",
          "late callback of the cancelled send changes nothing");
    Check(r.model.OnDecisionSent(retry.card_id, retry.token, false, 31300) == Earcon::kNone &&
              Join(r.model.Card(31300).footer) == "Not sent · tap to retry",
          "failed delivery: Not sent");
    Model::Decision again = r.model.Tap(1, 32000);
    Check(r.model.OnDecisionSent(again.card_id, again.token, true, 32100) == Earcon::kDenied &&
              r.model.Card(32100).outcome == "Denied",
          "Denied after a successful retry");
}

void TestLaterWhileSending() {
    Rig r;
    r.Apply(Approval("a"), 0);
    r.model.OpenCard("a", 0);
    Model::Decision d = r.model.Tap(0, 500);
    r.model.Later();
    Check(!r.model.card_open(), "right swipe while sending closes the card");
    Check(r.model.WantAutoOpen(IdleCtx(-10000), 10000).empty(), "no auto-open of a card with a decision in flight");
    Check(r.model.OpenCard("a", 1000), "reopen while sending");
    CardView v = r.model.Card(1000);
    Check(v.phase == CardPhase::kSending && v.pressed == 0 && Join(v.footer) == "Approve · sending",
          "reopened card still shows the queued decision");
    Check(!r.model.Tap(1, 1500).send && !r.model.Tap(0, 1600).send, "no second decision for the card");
    r.model.Later();
    Check(r.model.OnDecisionSent(d.card_id, d.token, true, 2000) == Earcon::kApproved,
          "callback after Later still resolves the decision");
    Check(!r.model.Face(2100).show && !r.model.OpenCard("a", 2100), "decided card leaves the face, cannot reopen");

    Rig f;
    f.Apply(Approval("b"), 0);
    f.model.OpenCard("b", 0);
    Model::Decision fd = f.model.Tap(0, 500);
    f.model.Later();
    f.model.OnDecisionSent(fd.card_id, fd.token, false, 1000);
    Check(f.model.Face(1100).main == "Needs your OK", "not sent after Later: still pending on the face");
    f.model.OpenCard("b", 1200);
    Check(Join(f.model.Card(1200).footer) == "Not sent · tap to retry", "reopened: Not sent");
    Check(!f.model.Tap(1, 1700).send, "reopened Not sent card refuses the other decision");
    Model::Decision fr = f.model.Tap(0, 1800);
    Check(fr.send && fr.kind == "approve" && fr.card_id == "i_b", "reopened card retries the same decision");
}

void TestCallbackMatching() {
    Rig r;
    r.Apply(Approval("a"), 0);
    r.Apply(Approval("b"), 0);
    r.model.OpenCard("a", 0);
    Model::Decision da = r.model.Tap(0, 500);
    r.model.Later();
    r.model.OpenCard("b", 600);
    Model::Decision db = r.model.Tap(1, 1100);
    Check(da.card_id == "i_a" && db.card_id == "i_b" && da.token != db.token, "two cards, two sends");
    Check(r.model.OnDecisionSent(db.card_id, da.token, true, 1200) == Earcon::kNone &&
              r.model.OnDecisionSent(da.card_id, db.token, true, 1200) == Earcon::kNone,
          "mismatched (card_id, send) pairs are ignored");
    Check(r.model.OnDecisionSent(da.card_id, da.token, true, 1300) == Earcon::kApproved, "card A resolves");
    CardView v = r.model.Card(1300);
    Check(v.card_id == "i_b" && v.phase == CardPhase::kSending && v.outcome.empty() &&
              Join(v.footer) == "Deny · sending",
          "card A's callback leaves card B sending, no status shown on B");
    int more = 0;
    auto rows = r.model.Rows(1300, 3, &more);
    bool a_sent = false;
    for (const auto& row : rows) {
        if (row.task_id == "a" && Join(row.status) == "Approved · sent") a_sent = true;
    }
    Check(a_sent, "card A marked Approved · sent");
    Check(r.model.OnDecisionSent(db.card_id, db.token, true, 1400) == Earcon::kDenied &&
              r.model.Card(1400).outcome == "Denied",
          "card B resolves on its own callback");
}

void TestTapUsesOpenedCard() {
    Rig r;
    r.Apply(Question("q", 2), 0);
    r.model.OpenCard("q", 0);
    Item changed = Question("q", 2);
    changed.options[0] = {"x1", "Garage"};
    r.Apply(changed, 300);
    Model::Decision d = r.model.Tap(0, 500);
    Check(!d.send && d.refreshed, "options changed since open: tap refreshes, nothing sent");
    Check(r.model.Card(500).options[0].id == "x1", "card shows the live options");
    Check(!r.model.Tap(0, 700).send, "refreshed content has its own tap guard");
    d = r.model.Tap(0, 1000);
    Check(d.send && d.option_id == "x1" && d.card_id == "q_q", "next tap answers the live option");

    Rig n;
    n.Apply(Approval("a"), 0);
    n.model.OpenCard("a", 0);
    Item next = Approval("a");
    next.card_id = "i_a2";
    n.Apply(next, 300);
    d = n.model.Tap(0, 500);
    Check(!d.send && d.refreshed && n.model.Card(500).card_id == "i_a2", "new card on the task: refreshed to it");
    d = n.model.Tap(0, 1000);
    Check(d.send && d.card_id == "i_a2", "decision goes to the card on screen");

    Rig g;
    g.Apply(Approval("a"), 0);
    g.model.OpenCard("a", 0);
    g.Apply(MakeItem("a", State::kWorking), 300);
    d = g.model.Tap(0, 500);
    Check(!d.send && d.refreshed, "task moved on: nothing sent");
    g.model.TickCard(600);
    Check(g.model.Card(600).outcome == "Answered", "then Answered");

    Rig t;
    t.Apply(Question("q", 2), 0);
    t.model.OpenCard("q", 0);
    Item relabel = Question("q", 3);
    t.Apply(relabel, 300);
    Check(t.model.TickCard(400).changed && t.model.Card(400).options.size() == 3,
          "tick refreshes changed options before a tap");
}

void TestAutoOpenTouchGuard() {
    Rig r;
    r.Apply(Approval("i", Tier::kInterrupt), 0);
    Context down = IdleCtx(-10000);
    down.touch_down = true;
    Check(r.model.WantAutoOpen(down, 1000).empty(), "INTERRUPT never opens under a finger");
    Context recent = IdleCtx(-10000);
    recent.last_touch_ms = 800;
    Check(r.model.WantAutoOpen(recent, 1000).empty(), "INTERRUPT waits 400 ms after a touch");
    Check(r.model.WantAutoOpen(recent, 1200) == "i", "INTERRUPT opens once untouched for 400 ms");
    r.model.OpenCard("i", 1200);
    Check(r.model.CardTapGuarded(1500) && !r.model.Tap(0, 1500).send, "taps in the first 400 ms are ignored");
    Check(!r.model.CardTapGuarded(1600) && r.model.Tap(0, 1600).send, "then a tap sends");

    Rig w;
    w.Apply(Approval("w"), 0);
    Check(w.model.WantAutoOpen(down, 10000).empty(), "WHEN_IDLE never opens under a finger");
    Check(w.model.WantAutoOpen(IdleCtx(-10000), 10000) == "w", "WHEN_IDLE opens without one");
}

void TestLaterAndIdle() {
    Rig r;
    r.Apply(Approval("a"), 0);
    r.model.OpenCard("a", 0);
    r.model.Later();
    Check(!r.model.card_open() && r.model.Face(100).main == "Needs your OK", "Later: card closes, item stays");
    Check(r.model.WantAutoOpen(IdleCtx(-10000), 10000).empty(), "Later is not undone by an auto-open");
    Check(r.model.OpenCard("a", 200), "tap reopens");
    r.model.TouchCard(30000);
    Check(!r.model.TickCard(80000).close, "touch restarts the 60 s idle");
    Check(r.model.TickCard(90000).close && !r.model.card_open(), "60 s idle is Later");
}

void TestExternalAndExpiry() {
    Rig r;
    r.Apply(Approval("a", Tier::kWhenIdle, 10), 0);
    r.model.OpenCard("a", 0);
    r.Apply(MakeItem("a", State::kWorking), 2000);
    r.model.TickCard(2100);
    Check(r.model.Card(2100).outcome == "Answered", "answered elsewhere / by voice: outcome");
    Check(r.model.TickCard(3100).close, "then closes");

    Rig e;
    e.Apply(Approval("b", Tier::kWhenIdle, 10), 0);
    e.model.OpenCard("b", 0);
    e.model.TickCard(10000);
    Check(e.model.Card(10000).outcome == "Timed out", "deadline: Timed out on the card");
    Check(!e.model.Face(10000).show, "expired approval leaves the face (the device never denies)");
    int more = 0;
    auto rows = e.model.Rows(10000, 3, &more);
    Check(rows.size() == 1 && Join(rows[0].status) == "Timed out" && rows[0].glyph == Glyph::kFailed,
          "row: Timed out");
    Check(!e.model.OpenCard("b", 11000), "expired card cannot be opened");
}

void TestTiers() {
    Rig r;
    auto a = r.Apply(Approval("i", Tier::kInterrupt), 0);
    Flash f = FlashFor(a[0], 0);
    Check(f.count == 3, "INTERRUPT needs-you flashes x3");
    Context busy = IdleCtx(0);
    busy.voice_idle = false;
    busy.panel_shown = true;
    busy.bedside = true;
    Check(r.model.HasInterruptPending(0) && r.model.WantAutoOpen(busy, 10) == "i",
          "INTERRUPT opens at once, also over the panel, busy or bedside");

    Rig s;
    auto silent = s.Apply(Approval("s", Tier::kSilent), 0);
    Check(EarconFor(silent[0]) == Earcon::kNone && FlashFor(silent[0], 0).count == 0, "SILENT: no sound, no flash");
    Check(s.model.WantAutoOpen(IdleCtx(-10000), 10000).empty() && !s.model.HasPendingOpen(),
          "SILENT never opens a card by itself");
    Check(s.model.Face(100).main == "Needs your OK", "SILENT still updates the face");

    Rig q;
    Item spoken = Question("q", 2);
    spoken.speaks = true;
    auto qa = q.Apply(spoken, 0);
    Check(EarconFor(qa[0]) == Earcon::kNone, "spoken question: the speech, no chime");
    Item unspoken = Question("q2", 2);
    qa = q.Apply(unspoken, 0);
    Check(EarconFor(qa[0]) == Earcon::kNeedsYou, "unspoken question chimes");
}

void TestQuestion() {
    Rig r;
    Item q = Question("q", 3);
    q.has_progress = true;
    q.progress_step = 1;
    q.progress_total = 4;
    r.Apply(q, 0);
    Complication c = r.model.Face(10);
    Check(c.glyph == Glyph::kQuestion && c.main == "Question" && c.pose == Pose::kConfused, "question on the face");
    Check(Join(c.status) == "Title q · tap or say \"show me\"", "question status");
    Check(std::string(r.model.Ring(2000, 0).kind) == "ask" && r.model.Ring(2000, 0).arcs[0].span == kAskSpan &&
              r.model.Ring(2000, 0).arcs[0].opa == 191,
          "ask arc 25-115 at 75 %, static");
    r.model.OpenCard("q", 100);
    CardView v = r.model.Card(100);
    Check(v.kind == CardKind::kQuestion && v.options.size() == 3, "question card with 3 options");
    Check(Join(v.context) == "Title q · step 1 of 4" && Join(v.footer) == "Or just say it", "question context + hint");
    Check(!r.model.Tap(7, 600).send, "out-of-range option ignored");
    Model::Decision d = r.model.Tap(2, 700);
    Check(d.send && d.kind == "answer" && d.option_id == "o3" && d.card_id == "q_q", "tap option 3 sends answer");
    r.model.OnDecisionSent(d.card_id, d.token, true, 800);
    Check(r.model.Card(800).outcome == "Got it", "Got it");
}

void TestResults() {
    Rig r;
    auto a = r.Apply(MakeItem("u", State::kUnread), 0);
    Check(EarconFor(a[0]) == Earcon::kDone && FlashFor(a[0], 0).count == 2 && FlashFor(a[0], 0).fade_to_dot,
          "unread: done earcon, double flash, fade to dot");
    r.model.StartFlash(FlashFor(a[0], 0));
    Complication c = r.model.Face(100);
    Check(c.glyph == Glyph::kDone && Join(c.status) == "Done · tap to hear" && c.pose == Pose::kHappy,
          "unread: check glyph, happy 3 s");
    Check(r.model.Face(3100).pose == Pose::kMini, "then neutral mini");
    RingHints h = r.model.Ring(1300, 0);
    Check(std::string(h.kind) == "dot" && h.count == 1 && h.arcs[0].color == color::kDone && h.arcs[0].opa < 255,
          "dot fades in after the flash");
    Check(r.model.Ring(2000, 0).arcs[0].opa == 255 && r.model.Ring(2000, 0).arcs[0].start == kDotDeg,
          "green dot at 12 o'clock");
    Check(!r.model.Face(31 * 60000).show && r.model.Ring(31 * 60000, 0).count == 1,
          "unread after 30 min: off the face, dot kept");
    r.model.MarkRead("u");
    Check(!r.model.Face(100).show && r.model.Ring(2000, 0).count == 0, "read: complication and dot removed");

    Rig f;
    auto fa = f.Apply(MakeItem("f", State::kFailed, Tier::kWhenIdle), 0);
    Check(EarconFor(fa[0]) == Earcon::kNone && FlashFor(fa[0], 0).count == 1, "failed WHEN_IDLE: one red flash, no alert");
    auto fi = f.Apply(MakeItem("g", State::kFailed, Tier::kInterrupt), 0);
    Check(EarconFor(fi[0]) == Earcon::kError, "failed INTERRUPT: error earcon");
    c = f.model.Face(100);
    Check(c.glyph == Glyph::kFailed && Join(c.status) == "Failed · tap for why" && c.pose == Pose::kSad,
          "failed: cross, sad");
    Check(f.model.Ring(2000, 0).arcs[0].color == color::kFail, "red dot");
    f.Apply(MakeItem("c", State::kCancelled), 0);
    Check(Join(f.model.Face(100).status) == "Cancelled · tap for why", "cancelled");

    Rig s;
    s.Apply(MakeItem("s", State::kStopped), 0);
    Check(s.model.Face(100).show && Join(s.model.Face(100).status) == "Stopped", "stopped shown 900 ms");
    Check(!s.model.Face(1000).show, "stopped leaves the face");
    int more = 0;
    Check(s.model.Rows(1000, 3, &more).size() == 1, "stopped stays in the list until seen");

    Rig seen;
    Item it = MakeItem("x", State::kUnread);
    it.seen = true;
    auto none = seen.Apply(it, 0);
    Check(none.empty() && !seen.model.Face(10).show, "seen result: no arrival, not shown");

    Rig done;
    done.Apply(MakeItem("d", State::kWorking), 0);
    done.Apply(MakeItem("d", State::kDone), 10);
    Check(!done.model.Face(20).show, "done clears the complication");
}

void TestPriority() {
    Rig r;
    r.Apply(MakeItem("w", State::kWorking), 0);
    r.Apply(MakeItem("u", State::kUnread), 0);
    r.Apply(MakeItem("f", State::kFailed), 0);
    r.Apply(Question("q", 2), 0);
    r.Apply(Approval("a"), 0);
    Complication c = r.model.Face(5000);
    Check(c.task_id == "a" && c.more == 4, "approval first, +4");
    int more = 0;
    auto rows = r.model.Rows(5000, 3, &more);
    Check(rows.size() == 3 && rows[0].task_id == "a" && rows[1].task_id == "q" && rows[2].task_id == "f" && more == 2,
          "rows: approval, question, failed, +2 more");
    Check(Join(rows[0].status) == "Needs your OK · 5:00", "row status: needs your OK + countdown");
    Check(Join(rows[2].status) == "Failed · 09:40", "row status: failed at local time");
    RingHints h = r.model.Ring(5000, 0);
    Check(std::string(h.kind) == "ears" && h.count == 4, "ears coexist with progress");
    Check(h.arcs[3].opa == 217, "progress static at 85 % while the ears breathe");
    Check(r.model.NextUp(5000) != nullptr && r.model.NextUp(5000)->item.task_id == "a", "Next up = approval");

    Item other = MakeItem("o", State::kWorking, Tier::kSilent, "c2");
    other.title = "Other";
    r.Apply(other, 0);
    Item trip = MakeItem("p", State::kWorking, Tier::kSilent, "c2");
    trip.title = "Trip plan";
    r.Apply(trip, 0);
    rows = r.model.Rows(5000, 8, &more);
    std::string o_status, p_status;
    for (const auto& row : rows) {
        if (row.task_id == "o") o_status = Join(row.status);
        if (row.task_id == "p") p_status = Join(row.status);
    }
    Check(r.model.current_chat() != "c2" && o_status.find(" · Trip plan") != std::string::npos,
          "row of another chat names the chat");
    Check(p_status == "Working", "no chat suffix that repeats the row's own title");
}

void TestChats() {
    Rig r;
    Item a = MakeItem("a", State::kWorking, Tier::kSilent, "c_home");
    a.title = "Home dev";
    a.has_progress = true;
    a.progress_step = 2;
    a.progress_total = 5;
    r.Apply(a, 0);
    Item b = MakeItem("b", State::kUnread, Tier::kSilent, "c_kids");
    b.title = "Kids' homework";
    r.Apply(b, 120000);
    Check(r.model.current_chat() == "c_home", "current chat = chat of the first item");
    Item c = MakeItem("c", State::kWorking, Tier::kSilent, "c_menu");
    c.title = "Menu";
    r.Apply(c, 60000);
    auto chats = r.model.Chats(130000);
    Check(chats.size() == 3 && chats[0].chat_id == "c_home" && chats[1].chat_id == "c_kids" &&
              chats[2].chat_id == "c_menu",
          "current chat first, then by last activity");
    Check(chats[0].current && chats[0].chip == "Working 2/5" && chats[0].age == "2 min", "current chat chip + age");
    Check(chats[1].chip == "Unread" && chats[1].chip_color == color::kDone && chats[1].age == "now", "unread chip");
    Check(r.model.CurrentTitle() == "Home dev", "header title");
    r.model.SetCurrentChat("c_kids");
    Check(r.model.Chats(130000)[0].chat_id == "c_kids" && r.model.Chats(130000)[0].current &&
              r.model.CurrentTitle() == "Kids' homework",
          "switch_chat sets current");
    r.model.StartNewChat();
    Check(r.model.CurrentTitle() == "New chat" && r.model.new_chat_pending(), "new chat pending");
    Item n = MakeItem("n", State::kWorking, Tier::kSilent, "c_new");
    n.title = "Trip";
    r.Apply(n, 140000);
    Check(r.model.current_chat() == "c_new" && !r.model.new_chat_pending(), "first unknown chat becomes current");
    Model big;
    std::vector<Task> tasks;
    for (int i = 0; i < 25; i++) {
        Task t;
        t.item = MakeItem("t" + std::to_string(i), State::kWorking, Tier::kSilent, "c" + std::to_string(i));
        tasks = {t};
        big.SetTasks(tasks, i * 1000, "");
    }
    Check(big.Chats(30000).size() == kMaxChats, "chat cache keeps 20");
}

// Page-1 state lines: the countdown survives a long chat suffix.
void TestFitLine() {
    auto width = [](const std::string& t) { return static_cast<int32_t>(t.size()) * 10; };
    Line row = {{"Needs your OK", 1}, {" · 1:00", 2}, {" · Leak sensor", 3}};
    Check(Join(FitLine(row, 400, width)) == "Needs your OK · 1:00 · Leak sensor", "fits: unchanged");
    Check(Join(FitLine(row, 250, width)) == "Needs your OK · 1:00", "the third span goes first");
    Check(Join(FitLine(row, 160, width)) == "Needs... · 1:00", "then the longest span is shortened");
    Line face = {{"Living room lights", 1}, {" · 4:00", 2}};
    Line fitted = FitLine(face, 150, width);
    Check(Join(fitted) == "Livi... · 4:00" && fitted[1].color == 2, "complication keeps its countdown");
    Line cjk = {{"周末菜单周末菜单", 1}};
    Check(Join(FitLine(cjk, 100, width)) == "周末...", "UTF-8 safe");
    Check(FitLine(Line{{"abc", 1}}, 0, width).empty(), "nothing fits: empty");
}

// Fixture items carry no title: the card's line above the controls must not repeat the summary.
void TestCardLeadWithoutTitle() {
    Rig r;
    Item home = MakeItem("h", State::kWorking, Tier::kSilent, "c_home");
    home.title = "Home dev";
    r.Apply(home, 0);
    Item ap = Approval("ap");
    ap.chat_id = "c_lights";
    ap.title.clear();
    ap.summary = "Turn on the hall light";
    r.Apply(ap, 100);
    Check(r.model.current_chat() == "c_home", "current chat stays");
    r.model.OpenCard("ap", 200);
    CardView v = r.model.Card(200);
    Check(v.body == "Turn on the hall light" && Join(v.context) == "reversible",
          "no title, chat named after the summary: only the risk");
    Item q = Question("q", 2);
    q.chat_id = "c_q";
    q.title.clear();
    q.summary = "Which lights?";
    r.Apply(q, 300);
    r.model.Later();
    r.model.OpenCard("q", 400);
    v = r.model.Card(400);
    Check(v.body == "Which lights?" && v.context.empty(), "question without title: no repeated summary");
    Item titled = Approval("t");
    titled.chat_id = "c_lights";
    titled.title = "Hall";
    r.Apply(titled, 500);
    r.model.Later();
    r.model.OpenCard("t", 600);
    Check(Join(r.model.Card(600).context) == "Hall · reversible",
          "title kept; the chat (named Hall after its newest task) not repeated");
}

// A snapshot that drops the current chat's tasks: the header does not keep their title.
void TestChatPillAfterSync() {
    Rig r;
    Item leak = MakeItem("leak", State::kNeedsApproval, Tier::kSilent, "c_leak");
    leak.title = "Leak sensor";
    leak.card_id = "i_leak";
    r.Apply(leak, 0);
    Item tidy = MakeItem("tidy", State::kWorking, Tier::kSilent, "c_tidy");
    tidy.title = "Evening tidy-up";
    r.Apply(tidy, 100);
    Check(r.model.CurrentTitle() == "Leak sensor", "current chat titled by its task");
    r.store.Sync({"tidy"}, ++g_seq);
    r.model.SetTasks(r.store.tasks(), 200, "09:40", r.store.synced_out());
    Check(r.model.current_chat() == "c_tidy" && r.model.CurrentTitle() == "Evening tidy-up",
          "dropped chat forgotten; the remaining task's chat is current");
    Check(r.model.Chats(300).size() == 1, "chat list without the dropped chat");
    r.store.Sync({}, ++g_seq);
    r.model.SetTasks(r.store.tasks(), 400, "09:40", r.store.synced_out());
    Check(r.model.CurrentTitle() == "New chat" && r.model.Chats(500).empty(), "empty mirror: no stale title");
    Rig done;
    Item d = MakeItem("d", State::kDone, Tier::kSilent, "c_done");
    d.title = "Done task";
    done.Apply(d, 0);
    done.store.Sync({"d"}, ++g_seq);
    done.model.SetTasks(done.store.tasks(), 100, "", done.store.synced_out());
    Check(done.model.CurrentTitle() == "Done task", "a sync that keeps the task keeps the chat");
}

// Wire payloads (as the fixture's ui scenario sends them) through Parse, the store and the model.
void TestWirePath() {
    const char* payloads[] = {
        R"({"kind":"agent","v":1,"seq":11,"chat_id":"c_home","task_id":"t_sun","state":"working",)"
        R"("title":"Sunset lights","summary":"Editing","progress":{"step":2,"total":5},"delivery":"silent"})",
        R"({"kind":"agent","v":1,"seq":12,"chat_id":"c_home","task_id":"t_lr","state":"needs_approval",)"
        R"("title":"Living room lights","summary":"Turn on living room lights","card_id":"i_lr",)"
        R"("risk":"reversible","timeout_s":300,"delivery":"when_idle",)"
        R"("options":[{"id":"approve","label":"Approve"},{"id":"deny","label":"Deny"}]})",
        R"({"kind":"agent","v":1,"seq":13,"chat_id":"c_kids","task_id":"t_hw","state":"unread",)"
        R"("title":"Kids' homework","summary":"Homework list is ready","delivery":"silent","seen":false})",
    };
    Store store;
    Model model;
    std::vector<Arrival> all;
    for (const char* text : payloads) {
        cJSON* json = cJSON_Parse(text);
        auto parsed = weixue_agent::Parse(json);
        cJSON_Delete(json);
        Check(parsed.message.has_value(), "fixture payload parses");
        if (!parsed.message) continue;
        Check(store.Apply(parsed.message->item, 0) == ApplyResult::kApplied, "fixture payload applies");
        auto a = model.SetTasks(store.tasks(), 0, "09:40");
        all.insert(all.end(), a.begin(), a.end());
    }
    Check(all.size() == 3, "one arrival per item");
    Complication c = model.Face(1000);
    Check(c.task_id == "t_lr" && c.main == "Needs your OK" && c.more == 2, "face shows the approval, +2");
    int more = 0;
    auto rows = model.Rows(1000, 3, &more);
    Check(rows.size() == 3 && rows[1].task_id == "t_hw" && rows[2].task_id == "t_sun", "rows by priority");
    Check(model.WantAutoOpen(IdleCtx(-10000), 10000) == "t_lr", "WHEN_IDLE approval opens when idle");
    auto chats = model.Chats(1000);
    Check(chats.size() == 2 && chats[0].current && chats[0].chip == "Waiting for you", "chats from the store");
}

void TestRingArcs() {
    RingState s;
    s.style = RingState::Style::kArcs;
    s.width = 9;
    s.AddArc({295, 40, 0xFFB020, 128, true});
    s.AddArc({205, 40, 0xFFB020, 128, true});
    s.AddArc({0, 0, 0, 128, false});
    Check(s.n_arcs == 2, "empty arcs are not added");
    int tiles = 0;
    for (int k = 0; k < RingState::kTiles; k++) {
        if (s.TileView(k).style != RingState::Style::kOff) tiles++;
    }
    Check(tiles >= 6 && tiles <= 8, "two 40 deg ears touch 6-8 of the 18 deg tiles");
    RingState t = s;
    t.arcs[0].opa = 160;
    int changed = 0;
    for (int k = 0; k < RingState::kTiles; k++) {
        if (s.TileView(k) != t.TileView(k)) changed++;
    }
    Check(changed >= 3 && changed <= 4, "breathing one ear invalidates only its own tiles");
}

}  // namespace

int main() {
    TestFormatting();
    TestWorking();
    TestApprovalFlow();
    TestNotSentAndRetry();
    TestLaterWhileSending();
    TestCallbackMatching();
    TestTapUsesOpenedCard();
    TestAutoOpenTouchGuard();
    TestLaterAndIdle();
    TestExternalAndExpiry();
    TestTiers();
    TestQuestion();
    TestResults();
    TestPriority();
    TestChats();
    TestRingArcs();
    TestFitLine();
    TestCardLeadWithoutTitle();
    TestChatPillAfterSync();
    TestWirePath();
    std::printf("agent_ui_test: %d failure(s)\n", failures);
    return failures == 0 ? 0 : 1;
}
