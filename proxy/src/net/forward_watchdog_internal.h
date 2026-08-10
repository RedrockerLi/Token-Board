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
    std::shared_ptr<std::atomic<bool>> semantic_seen;
    std::shared_ptr<std::atomic<uint64_t>> semantic_progress;
    std::shared_ptr<std::atomic<bool>> terminal_seen;
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

    void force_expire(int reason) {
        std::lock_guard<std::mutex> lock(state_mutex);
        if (!running.load(std::memory_order_acquire) ||
            client_disconnected.load(std::memory_order_acquire) ||
            expired.load(std::memory_order_acquire))
            return;
        expired_reason.store(reason, std::memory_order_release);
        expired.store(true, std::memory_order_release);
        if (cancel_socket != INVALID_SOCKET)
            shutdown_socket_copy(cancel_socket);
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
        // the act of publishing semantic_seen.  Defer one watchdog tick; this
        // remains bounded because the next pass no longer gets this grace.
        if (semantic_expired && receiver_active && !semantic_grace_used) {
            semantic_expired = false;
            semantic_grace_used = true;
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
        if (idle_deadline_ms == 0 || advanced)
            idle_deadline_ms = at_ms +
                static_cast<long long>(idle_timeout_secs) * 1000;
    }
};
