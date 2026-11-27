// Production-observable metrics, no behaviour branch: each counter is a
// function-local atomic that the watchdog loop updates and that tests and
// diagnostics read.  Nothing in production consults them.
inline std::atomic<std::uint64_t> &watchdog_scanned_watches() {
    static std::atomic<std::uint64_t> scanned{0};
    return scanned;
}

inline std::atomic<std::uint64_t> &watchdog_expire_calls() {
    static std::atomic<std::uint64_t> calls{0};
    return calls;
}

struct ForwardWatch {
    std::atomic<bool> running{true};
    std::mutex state_mutex;
    std::condition_variable state_cv;
    bool got_first = false;
    long long first_byte_deadline_ms = 0;  // 0 = disabled
    long long semantic_deadline_ms = 0;    // 0 = disabled
    long long idle_deadline_ms = 0;        // post-semantic; 0 = not armed
    int idle_timeout_secs = 0;
    uint64_t observed_semantic_progress = 0;
    bool receiver_active = false;
    bool semantic_grace_used = false;
    std::atomic<bool> expired{false};
    // 1 = first byte / total, 2 = first semantic event, 3 = stream idle.
    std::atomic<int> expired_reason{0};
    std::atomic<bool> client_disconnected{false};
    // Streaming only: set by the forwarding thread once at least one response
    // byte reached the downstream sink, i.e. the response is committed.  The
    // watchdog reads a clean FIN as "the client is gone" only from that point
    // on: before commit a client may legitimately shutdown(SHUT_WR) and keep
    // reading the response, so only hard socket errors count there, and the
    // non-streaming path never sets this (04-C-16 scope ruling).
    std::atomic<bool> downstream_started{false};
    // Latched once the downstream read half reported EOF (or a hard error).
    // A closed read half stays level-readable forever, so the watchdog must
    // stop polling that fd instead of spinning on it; the separate
    // downstream_started bit decides whether that EOF means "gone".
    std::atomic<bool> downstream_read_closed{false};
    // One watchdog pass of grace for a body callback that is publishing
    // semantic_seen at the exact deadline.
    static constexpr long long kSemanticGraceMs = 5;
    // Set when refresh_semantic_progress_locked() arms the idle deadline from
    // "not armed".  Arming introduces a deadline the event-driven waiter cannot
    // have seen (it computed its poll timeout before the first semantic chunk),
    // so the forwarding thread consumes this and wakes the watchdog; extending
    // an armed deadline needs no wake-up (an early wake recomputes it).
    std::atomic<bool> idle_deadline_armed{false};
    std::shared_ptr<std::atomic<bool>> semantic_seen;
    std::shared_ptr<std::atomic<uint64_t>> semantic_progress;
    int downstream_socket = -1;
    WatchSocket cancel_socket = INVALID_SOCKET;
    httplib::Client *cancel_client = nullptr;
    size_t active_client_cancels = 0;
    std::atomic<bool> cancel_setup_failed{false};

    ~ForwardWatch() { close_socket_copy(cancel_socket); }

    void install_socket(WatchSocket source) {
        WatchSocket copy = duplicate_socket(source);
        std::lock_guard<std::mutex> lock(state_mutex);
        if (copy == INVALID_SOCKET) {
            // A timeout without a cancellation handle can turn into an hour-long
            // backstop wait. Fail this connection immediately and explicitly.
            cancel_setup_failed.store(true, std::memory_order_release);
            if (running.load(std::memory_order_acquire))
                shutdown_socket_copy(source);
            return;
        }
        close_socket_copy(cancel_socket);
        if (!running.load(std::memory_order_acquire)) {
            close_socket_copy(copy);
            cancel_socket = INVALID_SOCKET;
            return;
        }
        cancel_socket = copy;
        if (expired.load(std::memory_order_acquire) ||
            client_disconnected.load(std::memory_order_acquire))
            shutdown_socket_copy(cancel_socket);
    }

    /// Register the transport client so cancel() can fall back to
    /// Client::stop() when the watchdog has no socket duplicate (the common
    /// case on a REUSED keep-alive connection — set_socket_options only fires
    /// on a fresh connect).  Must be re-called for every lease before send.
    void attach_client(httplib::Client *client) {
        std::lock_guard<std::mutex> lock(state_mutex);
        cancel_client = client;
    }

    void cancel() {
        httplib::Client *client = nullptr;
        {
            std::lock_guard<std::mutex> lock(state_mutex);
            if (!running.load(std::memory_order_acquire)) return;
            if (cancel_socket != INVALID_SOCKET) {
                shutdown_socket_copy(cancel_socket);
                return;
            }
            client = cancel_client;
            if (client) ++active_client_cancels;
        }
        if (!client) return;
        client->stop();
        {
            std::lock_guard<std::mutex> lock(state_mutex);
            --active_client_cancels;
        }
        state_cv.notify_all();
    }

    void finish() {
        std::unique_lock<std::mutex> lock(state_mutex);
        running.store(false, std::memory_order_release);
        cancel_client = nullptr;
        close_socket_copy(cancel_socket);
        cancel_socket = INVALID_SOCKET;
        state_cv.wait(lock, [&] { return active_client_cancels == 0; });
    }

    // Four-argument form: callers pass the attempt start timestamp and the
    // per-phase timeouts in seconds (0 = disabled).  Deadlines are converted
    // to absolute ms here so the watchdog compares against a single clock.
    void set_initial_deadlines(long long started_ms,
                               long long first_byte_secs,
                               long long semantic_secs,
                               int idle_secs) {
        std::lock_guard<std::mutex> lock(state_mutex);
        first_byte_deadline_ms = timeout_ms(first_byte_secs)
            ? started_ms + timeout_ms(first_byte_secs) : 0;
        semantic_deadline_ms = timeout_ms(semantic_secs)
            ? started_ms + timeout_ms(semantic_secs) : 0;
        idle_timeout_secs = idle_secs;
        if (semantic_progress)
            observed_semantic_progress =
                semantic_progress->load(std::memory_order_acquire);
    }

    bool begin_chunk(long long at_ms, bool fallback_progress) {
        std::lock_guard<std::mutex> lock(state_mutex);
        if (!running.load(std::memory_order_acquire) ||
            expired.load(std::memory_order_acquire) ||
            client_disconnected.load(std::memory_order_acquire))
            return false;

        if (!got_first && first_byte_deadline_ms != 0 &&
            at_ms >= first_byte_deadline_ms) {
            expired_reason.store(1, std::memory_order_release);
            expired.store(true, std::memory_order_release);
            return false;
        }

        got_first = true;
        refresh_semantic_progress_locked(at_ms, fallback_progress);
        const bool got_semantic = semantic_seen &&
            semantic_seen->load(std::memory_order_acquire);
        if (!got_semantic && semantic_deadline_ms != 0 &&
            at_ms >= semantic_deadline_ms) {
            expired_reason.store(2, std::memory_order_release);
            expired.store(true, std::memory_order_release);
            return false;
        }
        receiver_active = true;
        return true;
    }

    void end_chunk(long long at_ms, bool fallback_progress) {
        std::lock_guard<std::mutex> lock(state_mutex);
        refresh_semantic_progress_locked(at_ms, fallback_progress);
        receiver_active = false;
    }

    bool mark_client_disconnected() {
        std::lock_guard<std::mutex> lock(state_mutex);
        if (!running.load(std::memory_order_acquire)) return false;
        client_disconnected.store(true, std::memory_order_release);
        return true;
    }

    /// Publish that the response is committed downstream (first body byte
    /// written).  From here on a clean FIN means the reader went away.
    void mark_downstream_started() {
        downstream_started.store(true, std::memory_order_release);
    }

    /// Consume the "idle deadline was just armed" notice (forwarding thread
    /// only): the caller then wakes the watchdog so it re-derives its wait
    /// instead of sleeping on the deadline it saw when the watch was added.
    bool take_idle_deadline_armed() {
        return idle_deadline_armed.exchange(false, std::memory_order_acq_rel);
    }

    /// Latch the downstream read half as closed (EOF or a hard error).  The
    /// watchdog calls this once per watch so the level-triggered fd is dropped
    /// from the poll set and cannot spin the loop.
    void latch_downstream_read_closed() {
        downstream_read_closed.store(true, std::memory_order_release);
    }

    /// Earliest absolute time (ms) at which this watch can expire, or 0 when
    /// nothing can expire any more (deadline disabled, finished, already
    /// expired, or the client is gone).  The watchdog sleeps until the
    /// earliest deadline instead of scanning on a fixed 5ms tick, which is
    /// what turns an idle long stream into zero wake-ups (04-C-11).
    long long next_deadline_ms() {
        std::lock_guard<std::mutex> lock(state_mutex);
        if (!running.load(std::memory_order_acquire) ||
            expired.load(std::memory_order_acquire) ||
            client_disconnected.load(std::memory_order_acquire))
            return 0;
        const bool got_semantic =
            semantic_seen && semantic_seen->load(std::memory_order_acquire);
        long long next = 0;
        auto arm = [&next](long long deadline_ms) {
            if (deadline_ms > 0 && (next == 0 || deadline_ms < next))
                next = deadline_ms;
        };
        if (!got_first) arm(first_byte_deadline_ms);
        if (!got_semantic) arm(semantic_deadline_ms);
        else arm(idle_deadline_ms);
        return next;
    }

    int expire_if_due(long long at_ms) {
        std::lock_guard<std::mutex> lock(state_mutex);
        if (!running.load(std::memory_order_acquire) ||
            client_disconnected.load(std::memory_order_acquire) ||
            expired.load(std::memory_order_acquire))
            return 0;

        refresh_semantic_progress_locked(at_ms, false);
        const bool got_semantic = semantic_seen &&
            semantic_seen->load(std::memory_order_acquire);
        const bool first_expired = !got_first && first_byte_deadline_ms != 0 &&
                                   at_ms >= first_byte_deadline_ms;
        bool semantic_expired = !got_semantic && semantic_deadline_ms != 0 &&
                                at_ms >= semantic_deadline_ms;
        bool idle_expired = got_semantic && idle_deadline_ms != 0 &&
                            at_ms >= idle_deadline_ms;

        // A body callback that crossed the exact semantic boundary may be in
        // the act of publishing semantic_seen.  Defer one watchdog pass; this
        // remains bounded because the next pass no longer gets this grace.
        // The deadline moves with it so an event-driven wait recomputes a
        // future wake-up instead of spinning on an already-due deadline.
        if (semantic_expired && receiver_active && !semantic_grace_used) {
            semantic_expired = false;
            semantic_grace_used = true;
            semantic_deadline_ms = at_ms + kSemanticGraceMs;
        }

        // Re-read the externally published semantic state/counter at the
        // cancellation boundary.  first-byte and idle deadline updates are
        // serialized by state_mutex, eliminating their former stale-read race.
        if (semantic_expired && semantic_seen &&
            semantic_seen->load(std::memory_order_acquire)) {
            semantic_expired = false;
            refresh_semantic_progress_locked(at_ms, false);
        }
        if (idle_expired && semantic_progress) {
            const uint64_t current =
                semantic_progress->load(std::memory_order_acquire);
            if (current != observed_semantic_progress) {
                observed_semantic_progress = current;
                idle_deadline_ms = at_ms +
                    static_cast<long long>(idle_timeout_secs) * 1000;
                idle_expired = false;
            }
        }

        const int reason = first_expired ? 1
                         : semantic_expired ? 2
                         : idle_expired ? 3 : 0;
        if (reason != 0) {
            expired_reason.store(reason, std::memory_order_release);
            expired.store(true, std::memory_order_release);
        }
        return reason;
    }

private:
    void refresh_semantic_progress_locked(long long at_ms,
                                          bool fallback_progress) {
        bool advanced = false;
        if (semantic_progress) {
            const uint64_t current =
                semantic_progress->load(std::memory_order_acquire);
            if (current != observed_semantic_progress) {
                observed_semantic_progress = current;
                if (semantic_seen)
                    semantic_seen->store(true, std::memory_order_release);
                advanced = true;
            }
        } else {
            advanced = fallback_progress;
            if (advanced && semantic_seen)
                semantic_seen->store(true, std::memory_order_release);
        }

        if (idle_timeout_secs <= 0 || !semantic_seen ||
            !semantic_seen->load(std::memory_order_acquire))
            return;
        if (idle_deadline_ms == 0 || advanced) {
            const bool newly_armed = idle_deadline_ms == 0;
            idle_deadline_ms = at_ms +
                static_cast<long long>(idle_timeout_secs) * 1000;
            if (newly_armed)
                idle_deadline_armed.store(true, std::memory_order_release);
        }
    }
};
