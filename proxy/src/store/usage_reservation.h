#pragma once

#include <string>
#include <utility>

class Database;

// Non-standard sentinel status for internal_abort UsageEvents: a reserved slot
// that was marked as having begun upstream work but never completed an event
// (exception, early return, or a deferred provider that never ran).  Zero
// tokens, no upstream attempt — distinguishable from real upstream HTTP
// statuses (100-599) and from the 499 client-disconnect sentinel.
inline constexpr int kInternalAbortStatus = 599;

class UsageReservation {
public:
    UsageReservation() = default;
    ~UsageReservation();
    UsageReservation(const UsageReservation &) = delete;
    UsageReservation &operator=(const UsageReservation &) = delete;
    UsageReservation(UsageReservation &&other) noexcept;
    UsageReservation &operator=(UsageReservation &&other) noexcept;

    // Called once the request commits to contacting an upstream.  After this,
    // an abnormal exit (the destructor firing without a completed event) writes
    // an internal_abort UsageEvent instead of silently releasing the slot.
    void mark_upstream_started() noexcept { upstream_started_ = true; }
    // Best-effort context for the abort record, captured from the handler
    // before the first attempt is forwarded.
    void set_context(int account_id, int local_key_id, std::string model,
                     bool streaming) {
        context_account_id_ = account_id;
        context_local_key_id_ = local_key_id;
        context_model_ = std::move(model);
        context_streaming_ = streaming;
    }

private:
    friend class Database;
    explicit UsageReservation(Database *database) : database_(database) {}
    void consume() noexcept { database_ = nullptr; }
    bool belongs_to(const Database *database) const noexcept {
        return database_ == database;
    }
    Database *database_ = nullptr;
    bool upstream_started_ = false;
    int context_account_id_ = 0;
    int context_local_key_id_ = 0;
    std::string context_model_;
    bool context_streaming_ = false;
};
