#ifndef WEIXUE_AGENT_STORE_H
#define WEIXUE_AGENT_STORE_H

#include <algorithm>
#include <cstdint>
#include <string>
#include <utility>
#include <vector>

#include "weixue_agent_msg.h"

// Device-side agent task state (docs/agent-protocol.md): the latest item per task, at most
// kMaxTasks, the current task per chat and the newest `agent_chats` list. Main task only; no
// ESP-IDF types.
namespace weixue_agent {

// kFull: a new task while every slot holds an open card; the item is not applied.
enum class ApplyResult : uint8_t { kApplied, kDuplicate, kStale, kInvalid, kFull };

inline const char* ApplyResultName(ApplyResult r) {
    static const char* const kNames[] = {"applied", "duplicate", "stale", "invalid", "full"};
    return kNames[static_cast<int>(r)];
}

struct Task {
    Item item;
    int64_t received_ms = 0;
    int64_t deadline_ms = -1;  // card expiry by the device clock, -1 = none
    bool seen = false;
};

inline bool IsOpenCard(State s) { return s == State::kNeedsApproval || s == State::kNeedsInput; }

// UI priority: approval > input > failed (and other unseen terminal states) > unread > working.
// 0 = nothing to show.
inline int Priority(const Task& t) {
    switch (t.item.state) {
        case State::kNeedsApproval:
            return 5;
        case State::kNeedsInput:
            return 4;
        case State::kFailed:
        case State::kCancelled:
        case State::kStopped:
            return t.seen ? 0 : 3;
        case State::kUnread:
            return t.seen ? 0 : 2;
        case State::kWorking:
        case State::kInterrupted:
            return 1;
        case State::kDone:
            return 0;
    }
    return 0;
}

class Store {
public:
    static constexpr size_t kMaxTasks = 8;

    ApplyResult Apply(const Item& item, int64_t now_ms) {
        if (item.task_id.empty() || item.chat_id.empty() || item.seq <= 0) {
            return ApplyResult::kInvalid;
        }
        Task* existing = FindMutable(item.task_id);
        if (existing != nullptr) {
            if (item.seq == existing->item.seq) return ApplyResult::kDuplicate;
            if (item.seq < existing->item.seq) return ApplyResult::kStale;
        } else if (tasks_.size() >= kMaxTasks && !Evict()) {
            return ApplyResult::kFull;
        }
        Task next;
        next.item = item;
        next.received_ms = now_ms;
        next.deadline_ms = item.timeout_s >= 0 ? now_ms + int64_t{item.timeout_s} * 1000 : -1;
        next.seen = item.seen || (existing != nullptr && existing->seen &&
                                  existing->item.state == item.state);
        if (existing != nullptr) {
            *existing = std::move(next);
        } else {
            tasks_.push_back(std::move(next));
        }
        SetCurrent(item.chat_id, item.task_id);
        max_seq_ = std::max(max_seq_, item.seq);
        return ApplyResult::kApplied;
    }

    // Ends a snapshot: tasks not listed are dropped (kept in synced_out()). Returns how many
    // were dropped.
    size_t Sync(const std::vector<std::string>& task_ids, int64_t seq) {
        size_t before = tasks_.size();
        synced_out_.clear();
        tasks_.erase(std::remove_if(tasks_.begin(), tasks_.end(),
                                    [&](const Task& t) {
                                        bool out = std::find(task_ids.begin(), task_ids.end(),
                                                             t.item.task_id) == task_ids.end();
                                        if (out) synced_out_.push_back(t.item.task_id);
                                        return out;
                                    }),
                     tasks_.end());
        ++syncs_;
        current_.erase(std::remove_if(current_.begin(), current_.end(),
                                      [&](const std::pair<std::string, std::string>& c) {
                                          return Find(c.second) == nullptr;
                                      }),
                       current_.end());
        max_seq_ = std::max(max_seq_, seq);
        return before - tasks_.size();
    }

    // The newest chat list replaces the stored one; an equal seq is a duplicate, a lower one stale.
    ApplyResult ApplyChats(const Chats& chats, int64_t now_ms) {
        if (chats.seq <= 0) return ApplyResult::kInvalid;
        if (has_chats_ && chats.seq == chats_.seq) return ApplyResult::kDuplicate;
        if (has_chats_ && chats.seq < chats_.seq) return ApplyResult::kStale;
        chats_ = chats;
        chats_ms_ = now_ms;
        has_chats_ = true;
        max_seq_ = std::max(max_seq_, chats.seq);
        return ApplyResult::kApplied;
    }

    bool MarkSeen(const std::string& task_id) {
        Task* t = FindMutable(task_id);
        if (t == nullptr) return false;
        t->seen = true;
        return true;
    }

    const Task* Find(const std::string& task_id) const {
        for (const auto& t : tasks_) {
            if (t.item.task_id == task_id) return &t;
        }
        return nullptr;
    }

    // The task with the highest Priority, newest first among equals; nullptr when none has
    // anything to show.
    const Task* Top() const {
        const Task* best = nullptr;
        for (const auto& t : tasks_) {
            int p = Priority(t);
            if (p == 0) continue;
            if (best == nullptr || p > Priority(*best) ||
                (p == Priority(*best) && t.item.seq > best->item.seq)) {
                best = &t;
            }
        }
        return best;
    }

    // The task last applied for chat_id, or nullptr.
    const Task* Current(const std::string& chat_id) const {
        for (const auto& c : current_) {
            if (c.first == chat_id) return Find(c.second);
        }
        return nullptr;
    }

    const std::vector<Task>& tasks() const { return tasks_; }
    // Highest seq applied since boot (0 = none): the poll's X-Weixue-Agent-Seq.
    int64_t max_seq() const { return max_seq_; }
    // Syncs applied since boot, and the tasks the last one dropped.
    uint32_t syncs() const { return syncs_; }
    const std::vector<std::string>& synced_out() const { return synced_out_; }
    // The chat list (has_chats: one arrived since boot) and the device time it was applied.
    bool has_chats() const { return has_chats_; }
    const Chats& chats() const { return chats_; }
    int64_t chats_ms() const { return chats_ms_; }

private:
    std::vector<Task> tasks_;
    std::vector<std::string> synced_out_;
    uint32_t syncs_ = 0;
    std::vector<std::pair<std::string, std::string>> current_;  // chat_id -> task_id
    int64_t max_seq_ = 0;
    Chats chats_;
    int64_t chats_ms_ = 0;
    bool has_chats_ = false;

    Task* FindMutable(const std::string& task_id) {
        return const_cast<Task*>(static_cast<const Store*>(this)->Find(task_id));
    }

    void SetCurrent(const std::string& chat_id, const std::string& task_id) {
        for (auto& c : current_) {
            if (c.first == chat_id) {
                c.second = task_id;
                return;
            }
        }
        current_.emplace_back(chat_id, task_id);
    }

    // Removes one task: the oldest seen terminal task, else the oldest terminal task, else the
    // oldest working task, else the oldest task that is not an open card. False when every task
    // is an open card.
    bool Evict() {
        auto oldest = [&](auto pred) {
            auto pick = tasks_.end();
            for (auto it = tasks_.begin(); it != tasks_.end(); ++it) {
                if (pred(*it) && (pick == tasks_.end() || it->item.seq < pick->item.seq)) {
                    pick = it;
                }
            }
            return pick;
        };
        auto victim = oldest([](const Task& t) {
            return IsTerminal(t.item.state) && (t.seen || t.item.state == State::kDone);
        });
        if (victim == tasks_.end()) victim = oldest([](const Task& t) { return IsTerminal(t.item.state); });
        if (victim == tasks_.end()) {
            victim = oldest([](const Task& t) {
                return t.item.state == State::kWorking || t.item.state == State::kInterrupted;
            });
        }
        if (victim == tasks_.end()) {
            victim = oldest([](const Task& t) { return !IsOpenCard(t.item.state); });
        }
        if (victim == tasks_.end()) return false;
        std::string removed = victim->item.task_id;
        tasks_.erase(victim);
        current_.erase(std::remove_if(current_.begin(), current_.end(),
                                      [&](const std::pair<std::string, std::string>& c) {
                                          return c.second == removed;
                                      }),
                       current_.end());
        return true;
    }
};

}  // namespace weixue_agent

#endif  // WEIXUE_AGENT_STORE_H
