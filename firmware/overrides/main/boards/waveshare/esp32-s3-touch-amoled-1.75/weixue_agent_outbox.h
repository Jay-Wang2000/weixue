#ifndef WEIXUE_AGENT_OUTBOX_H
#define WEIXUE_AGENT_OUTBOX_H

#include <cstdint>
#include <deque>
#include <functional>
#include <string>
#include <utility>
#include <vector>

// RAM outbox of device → server notifications (docs/agent-protocol.md). One send is in flight at
// a time, in queue order. A send that did not reach the transport, or a receipt entry whose
// `agent_ack` did not arrive within kReceiptWaitUs after it did, is tried again after
// kRetryDelayUs (receipt entries from the back of the queue); after kMaxAttempts the entry is
// dropped. Times are µs; no ESP-IDF types.
namespace weixue_agent {

class Outbox {
public:
    static constexpr size_t kSize = 8;
    static constexpr int64_t kRetryDelayUs[] = {1000000, 3000000, 9000000};
    static constexpr int kMaxAttempts = 1 + sizeof(kRetryDelayUs) / sizeof(kRetryDelayUs[0]);
    static constexpr int64_t kReceiptWaitUs = 2000000;

    // done(true): the notification reached the transport, or for a receipt entry, its receipt
    // arrived and says the server recorded it; done(false): dropped, or the receipt refused it.
    using Done = std::function<void(bool)>;

    struct Entry {
        uint32_t id = 0;
        uint32_t seq = 0;
        std::string payload;
        std::string label;
        bool receipt = false;
        bool shown = false;
        int attempts = 0;
        int64_t due_us = 0;
        int64_t receipt_due_us = 0;
        Done done;
    };

    enum class Event : uint8_t { kSent, kAwaiting, kReceipted, kRefused, kRetry, kUnconfirmed, kDropped };
    // Log line source: what happened to an entry.
    struct Report {
        Event event;
        std::string label;
        int attempts;
        int64_t retry_in_us;
    };
    using Reporter = std::function<void(const Report&)>;

    void SetReporter(Reporter r) { reporter_ = std::move(r); }

    // Queues a notification; when full, the oldest queued entry that is not in flight is
    // dropped first. Returns its id.
    uint32_t Push(std::string payload, std::string label, uint32_t seq, bool receipt, int64_t now_us,
                  Done done = nullptr) {
        if (queue_.size() + awaiting_.size() >= kSize) {
            if (queue_.size() > (in_flight_ ? 1u : 0u)) {
                Finish(TakeQueued(in_flight_ ? 1 : 0), Event::kDropped, false);
            } else if (!awaiting_.empty()) {
                Entry e = std::move(awaiting_.front());
                awaiting_.erase(awaiting_.begin());
                Finish(std::move(e), Event::kDropped, false);
            }
        }
        Entry e;
        e.id = next_id_++;
        e.seq = seq;
        e.payload = std::move(payload);
        e.label = std::move(label);
        e.receipt = receipt;
        e.due_us = now_us;
        e.done = std::move(done);
        uint32_t id = e.id;
        queue_.push_back(std::move(e));
        return id;
    }

    // The front entry when it is due and nothing is in flight; it is now in flight.
    Entry* Ready(int64_t now_us) {
        if (in_flight_ || queue_.empty() || now_us < queue_.front().due_us) return nullptr;
        in_flight_ = true;
        queue_.front().attempts++;
        return &queue_.front();
    }

    // Removes the in-flight entry unsent (its done is not called).
    void Discard() {
        if (!in_flight_ || queue_.empty()) return;
        in_flight_ = false;
        queue_.pop_front();
    }

    // Transport outcome of the in-flight send.
    void Sent(bool reached, int64_t now_us) {
        if (!in_flight_ || queue_.empty()) return;
        in_flight_ = false;
        Entry e = TakeQueued(0);
        if (cancelled_ != 0 && cancelled_ == e.id) {
            cancelled_ = 0;
            receipted_ = 0;
            return;
        }
        if (receipted_ != 0 && receipted_ == e.id) {
            receipted_ = 0;
            Finish(std::move(e), receipt_ok_ ? Event::kReceipted : Event::kRefused, receipt_ok_);
            return;
        }
        if (reached && !e.receipt) {
            Finish(std::move(e), Event::kSent, true);
        } else if (reached) {
            e.receipt_due_us = now_us + kReceiptWaitUs;
            Emit(Event::kAwaiting, e, 0);
            awaiting_.push_back(std::move(e));
        } else {
            Retry(std::move(e), now_us, true);
        }
    }

    // The receipt of seq; result_ok = the server recorded the response. False when no entry
    // waits for it.
    bool Receipt(uint32_t seq, bool result_ok) {
        for (size_t i = 0; i < awaiting_.size(); ++i) {
            if (awaiting_[i].seq == seq) {
                Entry e = std::move(awaiting_[i]);
                awaiting_.erase(awaiting_.begin() + i);
                Finish(std::move(e), result_ok ? Event::kReceipted : Event::kRefused, result_ok);
                return true;
            }
        }
        for (size_t i = 0; i < queue_.size(); ++i) {
            if (!queue_[i].receipt || queue_[i].seq != seq) continue;
            if (i == 0 && in_flight_) {
                receipted_ = queue_[i].id;
                receipt_ok_ = result_ok;
                return true;
            }
            Entry e = TakeQueued(i);
            Finish(std::move(e), result_ok ? Event::kReceipted : Event::kRefused, result_ok);
            return true;
        }
        return false;
    }

    // Receipt waits that ran out go back to the queue (or are dropped after the last attempt).
    void Tick(int64_t now_us) {
        for (size_t i = 0; i < awaiting_.size();) {
            if (now_us < awaiting_[i].receipt_due_us) {
                ++i;
                continue;
            }
            Entry e = std::move(awaiting_[i]);
            awaiting_.erase(awaiting_.begin() + i);
            Retry(std::move(e), now_us, false);
        }
    }

    // Drops entry id without calling its done; an in-flight send finishes silently.
    void Cancel(uint32_t id) {
        for (size_t i = 0; i < queue_.size(); ++i) {
            if (queue_[i].id != id) continue;
            if (i == 0 && in_flight_) {
                cancelled_ = id;
                queue_[i].done = nullptr;
            } else {
                TakeQueued(i);
            }
            return;
        }
        for (size_t i = 0; i < awaiting_.size(); ++i) {
            if (awaiting_[i].id == id) {
                awaiting_.erase(awaiting_.begin() + i);
                return;
            }
        }
    }

    // A queued `shown` entry that is not in flight (to coalesce into), or nullptr.
    Entry* QueuedShown() {
        for (size_t i = in_flight_ ? 1 : 0; i < queue_.size(); ++i) {
            if (queue_[i].shown) return &queue_[i];
        }
        return nullptr;
    }
    Entry* Back() { return queue_.empty() ? nullptr : &queue_.back(); }

    bool Empty() const { return queue_.empty() && awaiting_.empty(); }
    bool in_flight() const { return in_flight_; }
    size_t queued() const { return queue_.size(); }
    size_t awaiting() const { return awaiting_.size(); }

private:
    std::deque<Entry> queue_;
    std::vector<Entry> awaiting_;
    bool in_flight_ = false;
    uint32_t next_id_ = 1;
    uint32_t receipted_ = 0;  // in-flight entry whose receipt arrived before its transport outcome
    bool receipt_ok_ = false;
    uint32_t cancelled_ = 0;  // in-flight entry cancelled
    Reporter reporter_;

    Entry TakeQueued(size_t i) {
        Entry e = std::move(queue_[i]);
        queue_.erase(queue_.begin() + i);
        return e;
    }

    void Emit(Event ev, const Entry& e, int64_t retry_in_us) {
        if (reporter_) reporter_({ev, e.label, e.attempts, retry_in_us});
    }

    void Retry(Entry e, int64_t now_us, bool front) {
        if (e.attempts >= kMaxAttempts) {
            Finish(std::move(e), Event::kDropped, false);
            return;
        }
        int64_t delay = kRetryDelayUs[e.attempts - 1];
        e.due_us = now_us + delay;
        Emit(front ? Event::kRetry : Event::kUnconfirmed, e, delay);
        if (front) {
            queue_.push_front(std::move(e));
        } else {
            queue_.push_back(std::move(e));
        }
    }

    void Finish(Entry e, Event ev, bool ok) {
        Emit(ev, e, 0);
        if (e.done) {
            Done done = std::move(e.done);
            done(ok);
        }
    }
};

}  // namespace weixue_agent

#endif  // WEIXUE_AGENT_OUTBOX_H
