// Host test for weixue_agent_store.h: firmware/tests/run.sh
#include "weixue_agent_store.h"

#include <cstdio>
#include <string>

namespace {

using namespace weixue_agent;

int failures = 0;

void Check(bool ok, const char* what) {
    std::printf("%s %s\n", ok ? "PASS" : "FAIL", what);
    if (!ok) {
        failures++;
    }
}

Item MakeItem(int64_t seq, const std::string& task, State state, const std::string& chat = "c1") {
    Item it;
    it.seq = seq;
    it.chat_id = chat;
    it.task_id = task;
    it.state = state;
    it.summary = "s";
    return it;
}

void TestApplyOrdering() {
    Store store;
    Check(store.Apply(MakeItem(10, "t1", State::kWorking), 0) == ApplyResult::kApplied, "first item applied");
    Check(store.Apply(MakeItem(10, "t1", State::kWorking), 0) == ApplyResult::kDuplicate, "same seq is a duplicate");
    Check(store.Apply(MakeItem(9, "t1", State::kDone), 0) == ApplyResult::kStale, "older seq is stale");
    Check(store.Find("t1")->item.state == State::kWorking, "stale item not applied");
    Check(store.Apply(MakeItem(12, "t1", State::kNeedsInput), 0) == ApplyResult::kApplied, "newer seq applied");
    Check(store.Find("t1")->item.state == State::kNeedsInput && store.Find("t1")->item.seq == 12, "newer item replaces");
    Check(store.Apply(MakeItem(11, "t2", State::kWorking), 0) == ApplyResult::kApplied,
          "per-task marks: a lower seq for another task applies");
    Check(store.max_seq() == 12, "max_seq is the highest applied");
    Item bad = MakeItem(13, "", State::kWorking);
    Check(store.Apply(bad, 0) == ApplyResult::kInvalid, "item without task_id invalid");
}

void TestDeadlineAndSeen() {
    Store store;
    Item card = MakeItem(5, "t1", State::kNeedsApproval);
    card.timeout_s = 300;
    store.Apply(card, 1000);
    Check(store.Find("t1")->deadline_ms == 301000, "deadline counted from receipt");
    store.Apply(MakeItem(6, "t2", State::kUnread), 0);
    Check(store.MarkSeen("t2") && store.Find("t2")->seen, "MarkSeen");
    Check(!store.MarkSeen("t9"), "MarkSeen of an unknown task");
    store.Apply(MakeItem(7, "t2", State::kUnread), 0);
    Check(store.Find("t2")->seen, "seen kept across a newer item in the same state");
    store.Apply(MakeItem(8, "t2", State::kFailed), 0);
    Check(!store.Find("t2")->seen, "seen reset by a state change");
    Item seen = MakeItem(9, "t2", State::kFailed);
    seen.seen = true;
    store.Apply(seen, 0);
    Check(store.Find("t2")->seen, "server seen:true marks the task seen");
}

void TestTop() {
    Store store;
    Check(store.Top() == nullptr, "empty store has no top");
    store.Apply(MakeItem(1, "w", State::kWorking), 0);
    Check(store.Top()->item.task_id == "w", "working shown");
    store.Apply(MakeItem(2, "u", State::kUnread), 0);
    Check(store.Top()->item.task_id == "u", "unread over working");
    store.Apply(MakeItem(3, "f", State::kFailed), 0);
    Check(store.Top()->item.task_id == "f", "failed over unread");
    store.Apply(MakeItem(4, "i", State::kNeedsInput), 0);
    Check(store.Top()->item.task_id == "i", "input over failed");
    store.Apply(MakeItem(5, "a", State::kNeedsApproval), 0);
    Check(store.Top()->item.task_id == "a", "approval over input");
    store.Apply(MakeItem(6, "a2", State::kNeedsApproval), 0);
    Check(store.Top()->item.task_id == "a2", "newest among equal priority");
    Store done;
    done.Apply(MakeItem(1, "d", State::kDone), 0);
    Check(done.Top() == nullptr, "done is cleared");
    done.Apply(MakeItem(2, "s", State::kStopped), 0);
    Check(done.Top() && done.Top()->item.task_id == "s", "stopped shown until seen");
    done.MarkSeen("s");
    Check(done.Top() == nullptr, "seen stopped is cleared");
}

void TestChatsAndSync() {
    Store store;
    store.Apply(MakeItem(1, "t1", State::kWorking, "c1"), 0);
    store.Apply(MakeItem(2, "t2", State::kWorking, "c2"), 0);
    store.Apply(MakeItem(3, "t3", State::kNeedsInput, "c1"), 0);
    Check(store.Current("c1")->item.task_id == "t3", "current task per chat follows the latest item");
    Check(store.Current("c2")->item.task_id == "t2", "other chat keeps its task");
    Check(store.Current("c9") == nullptr, "unknown chat");
    size_t dropped = store.Sync({"t1", "t2"}, 40);
    Check(dropped == 1 && store.Find("t3") == nullptr && store.tasks().size() == 2, "sync drops unlisted tasks");
    Check(store.Current("c1") == nullptr, "current task of a dropped task cleared");
    Check(store.max_seq() == 40, "sync seq raises max_seq");
    Check(store.Sync({}, 41) == 2 && store.tasks().empty(), "empty sync clears the store");
}

void TestEviction() {
    Store store;
    store.Apply(MakeItem(1, "old-working", State::kWorking), 0);
    store.Apply(MakeItem(2, "unseen-failed", State::kFailed), 0);
    Item seen = MakeItem(3, "seen-cancelled", State::kCancelled);
    seen.seen = true;
    store.Apply(seen, 0);
    for (int i = 4; i <= 8; ++i) {
        store.Apply(MakeItem(i, "t" + std::to_string(i), State::kNeedsInput), 0);
    }
    Check(store.tasks().size() == Store::kMaxTasks, "store full at 8 tasks");
    store.Apply(MakeItem(9, "t9", State::kWorking), 0);
    Check(store.tasks().size() == Store::kMaxTasks && store.Find("seen-cancelled") == nullptr,
          "seen terminal task evicted first");
    store.Apply(MakeItem(10, "t10", State::kWorking), 0);
    Check(store.Find("unseen-failed") == nullptr, "then the oldest terminal task");
    store.Apply(MakeItem(11, "t11", State::kWorking), 0);
    Check(store.Find("old-working") == nullptr && store.Find("t4") != nullptr,
          "then the oldest working task, cards kept");
}

// X-Weixue-Agent-Seq is max_seq(): only an accepted item or a sync moves it.
void TestMaxSeqCountsOnlyAcceptedItems() {
    Store store;
    store.Apply(MakeItem(20, "t1", State::kWorking), 0);
    Check(store.Apply(MakeItem(30, "", State::kWorking), 0) == ApplyResult::kInvalid && store.max_seq() == 20,
          "an invalid item does not advance max_seq");
    Item no_chat = MakeItem(31, "t2", State::kWorking, "");
    Check(store.Apply(no_chat, 0) == ApplyResult::kInvalid && store.max_seq() == 20,
          "an item without chat_id does not advance max_seq");
    Check(store.Apply(MakeItem(20, "t1", State::kWorking), 0) == ApplyResult::kDuplicate && store.max_seq() == 20,
          "a duplicate leaves max_seq");
    store.Sync({"t1"}, 40);
    Check(store.max_seq() == 40, "a sync advances max_seq");
}

void TestOpenCardsAreNeverEvicted() {
    Store store;
    for (int i = 1; i <= 8; ++i) {
        store.Apply(MakeItem(i, "c" + std::to_string(i), i % 2 ? State::kNeedsApproval : State::kNeedsInput), 0);
    }
    Check(store.Apply(MakeItem(9, "t9", State::kWorking), 0) == ApplyResult::kFull,
          "a new task is refused when every slot holds an open card");
    Check(store.tasks().size() == Store::kMaxTasks && store.Find("c1") != nullptr && store.Find("t9") == nullptr,
          "every open card kept");
    Check(store.max_seq() == 8, "a refused item does not advance max_seq");
    Check(store.Apply(MakeItem(10, "c1", State::kWorking), 0) == ApplyResult::kApplied,
          "an update of a stored task still applies when full");
    Check(store.Apply(MakeItem(11, "t9", State::kWorking), 0) == ApplyResult::kApplied &&
              store.Find("c1") == nullptr && store.Find("c2") != nullptr,
          "the first non-card task is evicted, not a card");
}

void TestChatList() {
    Store store;
    Check(!store.has_chats(), "no chat list before one arrives");
    Chats list;
    list.seq = 50;
    list.current = "cht_a";
    list.chats = {{"cht_a", "Lights", ChatState::kWorking, 5}, {"cht_b", "", ChatState::kIdle, 60}};
    Check(store.ApplyChats(list, 1000) == ApplyResult::kApplied && store.has_chats() &&
              store.chats().current == "cht_a" && store.chats_ms() == 1000,
          "a chat list applies");
    Check(store.max_seq() == 50, "the chat list counts for the poll header");
    Check(store.ApplyChats(list, 2000) == ApplyResult::kDuplicate && store.chats_ms() == 1000, "same seq: duplicate");
    Chats older = list;
    older.seq = 40;
    older.current = "cht_b";
    Check(store.ApplyChats(older, 3000) == ApplyResult::kStale && store.chats().current == "cht_a", "older: stale");
    Chats newer = list;
    newer.seq = 60;
    newer.current = "cht_b";
    newer.chats.pop_back();
    Check(store.ApplyChats(newer, 4000) == ApplyResult::kApplied && store.chats().current == "cht_b" &&
              store.chats().chats.size() == 1,
          "a newer list replaces it");
    store.Sync({}, 70);
    Check(store.has_chats() && store.chats().seq == 60, "a task snapshot keeps the chat list");
    Chats bad;
    Check(store.ApplyChats(bad, 0) == ApplyResult::kInvalid, "seq 0 is invalid");
}

}  // namespace

int main() {
    TestApplyOrdering();
    TestDeadlineAndSeen();
    TestTop();
    TestChatsAndSync();
    TestEviction();
    TestMaxSeqCountsOnlyAcceptedItems();
    TestOpenCardsAreNeverEvicted();
    TestChatList();
    std::printf("%s: %d failure(s)\n", failures ? "FAILED" : "OK", failures);
    return failures ? 1 : 0;
}
