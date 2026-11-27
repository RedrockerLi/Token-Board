#include <algorithm>
#include <cctype>
#include <cerrno>
#include <chrono>
#include <limits>
#include <string>

#include "logging.h"

/// Process-wide forward watchdog.
///
/// One thread watches every active upstream forward: it enforces the per-phase
/// deadlines held by ForwardWatch and notices a downstream client that went
/// away.  The loop is event driven — it sleeps on poll() until the earliest
/// watch deadline, a downstream socket event, or an explicit wake-up — so an
/// idle long stream costs no wake-ups at all instead of one full O(active)
/// scan every 5ms (04-C-11).  Deadline accuracy is preserved because the wait
/// is derived from the exact remaining time to the next deadline.
class ForwardWatchdog {
public:
    static ForwardWatchdog &instance() {
        static ForwardWatchdog service;
        return service;
    }

    void add(const std::shared_ptr<ForwardWatch> &watch) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            watches_.push_back(watch);
        }
        wake();
    }

    /// A watch changed its own deadlines: recompute the wait instead of
    /// sleeping out the previous (now stale) deadline.
    void changed() { wake(); }

private:
    // Owner marker for the wake-up pipe inside the poll result vector.
    static constexpr size_t kWakeupOwner = std::numeric_limits<size_t>::max();
#ifdef POLLRDHUP
    // A clean FIN is only reported when it is explicitly requested, and on a
    // TCP peer it arrives as POLLIN|POLLRDHUP (0x2001) while POLLHUP stays
    // clear (04 §附录 A, 99 E-06).
    static constexpr short kReadHangup = POLLRDHUP;
#else
    static constexpr short kReadHangup = 0;
#endif
    // Historical pass length, kept only as the degraded wait when the wake-up
    // pipe cannot be created.
    static constexpr int kFallbackTickMs = 5;
    // Upper bound on how long a pending watch-set change waits for its poll
    // iteration.  It matches the pre-fix loop's own 5ms granularity, so a busy
    // proxy does the same number of O(active) rebuilds per second as before
    // while an idle one still sleeps until its next deadline.
    static constexpr int kCoalesceMs = 5;

    ForwardWatchdog() {
        int fds[2];
        if (::pipe(fds) == 0) {
            for (int fd : fds) {
                const int flags = ::fcntl(fd, F_GETFL, 0);
                ::fcntl(fd, F_SETFL, flags | O_NONBLOCK);
                ::fcntl(fd, F_SETFD, FD_CLOEXEC);
            }
            wake_read_fd_ = fds[0];
            wake_write_fd_ = fds[1];
        }
        worker_ = std::thread([this] { run(); });
    }

    ~ForwardWatchdog() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            stopping_ = true;
        }
        wake();
        if (worker_.joinable()) worker_.join();
        if (wake_read_fd_ >= 0) ::close(wake_read_fd_);
        if (wake_write_fd_ >= 0) ::close(wake_write_fd_);
    }

    static void cancel(const std::shared_ptr<ForwardWatch> &watch) {
        watch->cancel();
    }

    /// Ask the loop to rebuild its watch set.  The request is recorded even
    /// when the loop is already awake (it re-reads the flag before it sleeps),
    /// so a busy proxy pays no wake-up syscall per request; a full pipe
    /// already guarantees one pending wake-up, making EAGAIN benign.
    void wake() {
        dirty_.store(true, std::memory_order_release);
        if (!waiting_.load(std::memory_order_acquire)) return;
        if (wake_write_fd_ < 0) return;
        const char byte = 1;
        const ssize_t written = ::write(wake_write_fd_, &byte, 1);
        (void)written;
    }

    void drain_wakeup() {
        if (wake_read_fd_ < 0) return;
        char buffer[64];
        while (::read(wake_read_fd_, buffer, sizeof buffer) > 0) {}
    }

    static short downstream_events() { return POLLIN | kReadHangup; }

    /// Poll timeout for the next wait: exact time to the earliest deadline, or
    /// infinite when nothing can expire and the wake-up pipe exists.
    int wait_timeout_ms(long long next_due, long long now) const {
        long long timeout =
            next_due > 0 ? std::max<long long>(0, next_due - now) : -1;
        if (wake_read_fd_ < 0) {
            // Degraded mode (pipe() failed): keep a bounded tick so add(),
            // changed() and pending expiries still make progress.
            timeout = timeout < 0
                ? kFallbackTickMs
                : std::min<long long>(timeout, kFallbackTickMs);
        }
        if (timeout > std::numeric_limits<int>::max())
            timeout = std::numeric_limits<int>::max();
        return static_cast<int>(timeout);
    }

    /// Decide what one poll result means for the downstream client.
    enum class DownstreamVerdict {
        kKeepPolling,
        kStopPolling,  // EOF observed but not actionable: drop the fd, stay up
        kDisconnect,
    };

    /// Hard socket errors always count.  A clean FIN is read as a disconnect
    /// only once the response is committed downstream, where a client that
    /// half-closed and kept reading no longer makes sense; before that (and on
    /// the non-streaming path, which never marks it) only hard errors count,
    /// because recv()==0 cannot tell a closed peer from shutdown(SHUT_WR)
    /// (04-C-16).  EOF is terminal either way, so it is latched to keep the
    /// level-triggered read half from spinning the loop.
    static DownstreamVerdict downstream_verdict(
        const std::shared_ptr<ForwardWatch> &watch, short revents) {
        if (revents & (POLLHUP | POLLERR | POLLNVAL)) {
            watch->latch_downstream_read_closed();
            return DownstreamVerdict::kDisconnect;
        }
        // Only a hang-up needs the peek: ordinary readability is a client
        // sending its next request on the connection, which the HTTP layer
        // consumes.  Both transports report a clean FIN as POLLRDHUP.
        if (!(revents & kReadHangup)) return DownstreamVerdict::kKeepPolling;
        // Peek so a pending request byte is never consumed; EOF (0) proves the
        // peer closed its write half.
        char probe = 0;
        const ssize_t got = ::recv(watch->downstream_socket, &probe, 1,
                                   MSG_PEEK | MSG_DONTWAIT);
        const bool eof = got == 0 ||
            (got < 0 && errno != EAGAIN && errno != EWOULDBLOCK &&
             errno != EINTR);
        if (!eof) return DownstreamVerdict::kKeepPolling;
        watch->latch_downstream_read_closed();
        if (watch->downstream_started.load(std::memory_order_acquire)) {
            TB_LOG_DEBUG(
                "[Watchdog] downstream close detected after commit "
                "(socket=%d)\n", watch->downstream_socket);
            return DownstreamVerdict::kDisconnect;
        }
        // Not committed (or a non-streaming call): the client may have
        // half-closed and still be reading.  Keep the forward; a downstream
        // write failure remains the detector.
        return DownstreamVerdict::kStopPolling;
    }

    void run() {
        std::vector<std::shared_ptr<ForwardWatch>> active;
        std::vector<struct pollfd> fds;
        std::vector<size_t> owners;
        long long next_due = 0;
        // Rebuilding the watch set is the only O(active) work in the loop, so
        // it is reserved for the moments that need it: a watch set change
        // (add/finish), a deadline wake-up, or a latched fd that must leave
        // the poll set.  A plain socket event is handled in O(1).
        bool build = true;
        long long last_build_ms = 0;
        for (;;) {
            // A watch-set change is picked up at most once per coalescing
            // slice.  Rebuilding per request would be O(active) work per
            // request on a busy proxy; the pre-fix loop rebuilt every 5ms, so
            // this keeps exactly its cost profile while an idle proxy still
            // sleeps until its next deadline.
            const long long loop_now = now_ms();
            if (build && last_build_ms != 0 &&
                loop_now - last_build_ms < kCoalesceMs) {
                int slice = static_cast<int>(kCoalesceMs -
                                             (loop_now - last_build_ms));
                int timeout = wait_timeout_ms(next_due, loop_now);
                if (timeout < 0 || timeout > slice) timeout = slice;
                waiting_.store(true, std::memory_order_release);
                if (fds.empty()) {
                    waiting_.store(false, std::memory_order_release);
                    std::this_thread::sleep_for(
                        std::chrono::milliseconds(timeout));
                    continue;
                }
                const int ready = ::poll(fds.data(), fds.size(), timeout);
                waiting_.store(false, std::memory_order_release);
                if (ready > 0) {
                    for (size_t i = 0; i < fds.size(); ++i) {
                        if (fds[i].revents == 0) continue;
                        if (owners[i] == kWakeupOwner) {
                            drain_wakeup();
                            continue;
                        }
                        auto &watch = active[owners[i]];
                        const DownstreamVerdict verdict =
                            downstream_verdict(watch, fds[i].revents);
                        if (verdict == DownstreamVerdict::kKeepPolling) continue;
                        if (verdict == DownstreamVerdict::kDisconnect &&
                            watch->mark_client_disconnected())
                            cancel(watch);
                    }
                }
                continue;  // the slice is still running: no rebuild yet
            }
            if (build) {
                last_build_ms = loop_now;
                dirty_.store(false, std::memory_order_release);
                active.clear();
                {
                    std::lock_guard<std::mutex> lock(mutex_);
                    if (stopping_) return;
                    for (auto it = watches_.begin(); it != watches_.end();) {
                        auto watch = it->lock();
                        if (!watch ||
                            !watch->running.load(std::memory_order_acquire)) {
                            it = watches_.erase(it);
                        } else {
                            active.push_back(std::move(watch));
                            ++it;
                        }
                    }
                }
                // 生产可观测指标，无行为分支: the number of active watch
                // entries this round scans.  A plain counter written by the
                // watchdog loop and read by tests/diagnostics only.
                watchdog_scanned_watches().fetch_add(
                    active.size(), std::memory_order_relaxed);
                fds.clear();
                owners.clear();
                next_due = 0;
                if (wake_read_fd_ >= 0) {
                    struct pollfd pfd{};
                    pfd.fd = wake_read_fd_;
                    pfd.events = POLLIN;
                    fds.push_back(pfd);
                    owners.push_back(kWakeupOwner);
                }
                const long long now = now_ms();
                for (size_t i = 0; i < active.size(); ++i) {
                    const long long due = active[i]->next_deadline_ms();
                    if (due > 0 && (next_due == 0 || due < next_due))
                        next_due = due;
                    // Retire only what is due; the rest keeps its state mutex
                    // untouched on this pass.
                    if (due > 0 && now >= due) {
                        // 生产可观测指标，无行为分支: one expire_if_due call.
                        watchdog_expire_calls().fetch_add(
                            1, std::memory_order_relaxed);
                        if (active[i]->expire_if_due(now) != 0) {
                            cancel(active[i]);
                            continue;
                        }
                    }
                    if (active[i]->downstream_socket < 0) continue;
                    // A read half already latched as closed stays
                    // level-readable forever; polling it would spin the loop.
                    if (active[i]->downstream_read_closed.load(
                            std::memory_order_acquire))
                        continue;
                    struct pollfd pfd{};
                    pfd.fd = active[i]->downstream_socket;
                    pfd.events = downstream_events();
                    fds.push_back(pfd);
                    owners.push_back(i);
                }
                build = false;
            }

            int timeout = wait_timeout_ms(next_due, now_ms());
            // Announce the sleep before reading the flag: a concurrent
            // add()/changed() then either writes the wake-up pipe (we are
            // waiting) or leaves the flag set for the check right below, so
            // neither order can lose a request.
            waiting_.store(true, std::memory_order_release);
            // A pending set change (add/finish) must be picked up promptly; the
            // coalescing ceiling keeps a burst of requests from turning into one
            // rebuild per request.
            const bool pending_change = dirty_.load(std::memory_order_acquire);
            if (pending_change && (timeout < 0 || timeout > kCoalesceMs))
                timeout = kCoalesceMs;
            if (fds.empty()) {
                // Degraded mode with nothing to poll: sleep a bounded tick so
                // the loop cannot spin.
                waiting_.store(false, std::memory_order_release);
                std::this_thread::sleep_for(std::chrono::milliseconds(
                    timeout >= 0 ? timeout : kFallbackTickMs));
                build = true;
                continue;
            }
            const int ready = ::poll(fds.data(), fds.size(), timeout);
            waiting_.store(false, std::memory_order_release);
            if (ready < 0 && errno == EINTR) continue;
            if (ready == 0) {
                // Timeout: the earliest deadline may have arrived, so rebuild
                // and retire it on the next pass.
                build = true;
                continue;
            }
            for (size_t i = 0; i < fds.size(); ++i) {
                if (fds[i].revents == 0) continue;
                if (owners[i] == kWakeupOwner) {
                    drain_wakeup();
                    build = true;
                    continue;
                }
                auto &watch = active[owners[i]];
                const DownstreamVerdict verdict =
                    downstream_verdict(watch, fds[i].revents);
                if (verdict == DownstreamVerdict::kKeepPolling) continue;
                // The fd is latched (or the client is gone): drop it from the
                // poll set on the next pass.
                build = true;
                if (verdict == DownstreamVerdict::kDisconnect &&
                    watch->mark_client_disconnected())
                    cancel(watch);
            }
            if (dirty_.load(std::memory_order_acquire)) build = true;
        }
    }

    std::mutex mutex_;
    std::vector<std::weak_ptr<ForwardWatch>> watches_;
    bool stopping_ = false;
    std::atomic<bool> dirty_{false};
    std::atomic<bool> waiting_{false};
    int wake_read_fd_ = -1;
    int wake_write_fd_ = -1;
    std::thread worker_;
};

}  // namespace tb_transport

/// A lowercased, JSON-escape-decoded, whitespace-collapsed view of an upstream
/// error body.  Quota messages reach the proxy in every shape: plain UTF-8
/// text, JSON string values with escaped quotes (`\"`), `\n`-separated lines
/// and even `\uXXXX` escapes.  Matching on this view keeps the quota
/// classification honest across all of them instead of only on an exact byte
/// sequence (04-C-18).
inline std::string error_body_text(const std::string &body) {
    std::string out;
    out.reserve(body.size());
    bool pending_space = false;
    auto emit_space = [&out, &pending_space] {
        if (pending_space && !out.empty()) out.push_back(' ');
        pending_space = false;
    };
    for (std::size_t i = 0; i < body.size(); ++i) {
        const unsigned char raw = static_cast<unsigned char>(body[i]);
        if (raw == '\\' && i + 1 < body.size()) {
            const char escaped = body[++i];
            if (escaped == 'n' || escaped == 'r' || escaped == 't' ||
                escaped == 'b' || escaped == 'f') {
                pending_space = true;
                continue;
            }
            if (escaped == 'u' && i + 4 < body.size()) {
                unsigned code = 0;
                bool hex = true;
                for (int k = 1; k <= 4; ++k) {
                    const unsigned char digit =
                        static_cast<unsigned char>(body[i + k]);
                    code <<= 4;
                    if (digit >= '0' && digit <= '9') code |= digit - '0';
                    else if (digit >= 'a' && digit <= 'f')
                        code |= digit - 'a' + 10;
                    else if (digit >= 'A' && digit <= 'F')
                        code |= digit - 'A' + 10;
                    else {
                        hex = false;
                        break;
                    }
                }
                if (hex) {
                    i += 4;
                    if (code == ' ' || code == '\t' || code == '\n' ||
                        code == '\r') {
                        pending_space = true;
                        continue;
                    }
                    if (code >= 0x20 && code < 0x7f) {
                        emit_space();
                        out.push_back(static_cast<char>(std::tolower(code)));
                        continue;
                    }
                }
                // Non-ASCII (or malformed) escape: treat it as a word break so
                // the two surrounding phrases cannot be glued together.
                pending_space = true;
                continue;
            }
            emit_space();
            out.push_back(static_cast<char>(std::tolower(
                static_cast<unsigned char>(escaped))));
            continue;
        }
        if (raw == ' ' || raw == '\t' || raw == '\n' || raw == '\r' ||
            raw == 0x0b || raw == 0x0c) {
            pending_space = true;
            continue;
        }
        emit_space();
        out.push_back(static_cast<char>(std::tolower(raw)));
    }
    return out;
}

/// Billing/quota exhaustion the account cannot recover from by retrying: the
/// upstream wants a raised spending limit, a topped-up balance or an enabled
/// plan.  Continuing the ordinary 60s rate-limit cycle against such a 429 only
/// burns one upstream call per minute until the next billing cycle, and the
/// client never learns why (04-C-18).
inline bool is_terminal_quota_error(const std::string &body) {
    if (body.empty()) return false;
    const std::string text = error_body_text(body);
    // Google AI Studio: "Your project has exceeded its monthly spending cap".
    if (text.find("spending cap") != std::string::npos) return true;
    // Generic quota phrasing: "quota exceeded".
    if (text.find("quota exceeded") != std::string::npos) return true;
    // OpenAI's insufficient_quota error code and its spaced message form.
    if (text.find("insufficient_quota") != std::string::npos) return true;
    if (text.find("insufficient quota") != std::string::npos) return true;
    // OpenAI: "You exceeded your current quota, please check your plan and
    // billing details".
    if (text.find("exceeded your current quota") != std::string::npos)
        return true;
    return false;
}

/// True for an explicit usage-limit envelope (opencode's GoUsageLimitError /
/// limitName) and for terminal billing/quota text such as Google's monthly
/// spending cap.  Both mean "the key is out of quota", never "slow down".
inline bool is_usage_limit_error(const std::string &body) {
    if (body.empty()) return false;
    if (body.find("GoUsageLimitError") != std::string::npos ||
        body.find("\"limitName\"") != std::string::npos)
        return true;
    return is_terminal_quota_error(body);
}

/// The quota window an explicit usage-limit envelope names, mapped to the
/// stored window names ("5h" / "week" / "month"); empty when the envelope
/// carries no usable limitName.  The scan is deliberately positional (find
/// the key, then its string value) so the net layer stays free of a JSON
/// dependency, and an unknown name falls back to the caller's own ordering
/// rather than guessing a window.
inline std::string usage_limit_window(const std::string &body) {
    const auto key = body.find("\"limitName\"");
    if (key == std::string::npos) return {};
    const auto colon = body.find(':', key);
    if (colon == std::string::npos) return {};
    const auto quote = body.find('"', colon + 1);
    if (quote == std::string::npos) return {};
    const auto end = body.find('"', quote + 1);
    if (end == std::string::npos) return {};
    std::string name = body.substr(quote + 1, end - quote - 1);
    for (auto &c : name) c = static_cast<char>(std::tolower(
        static_cast<unsigned char>(c)));
    if (name.find("roll") != std::string::npos ||
        name.find("hour") != std::string::npos ||
        name.find("5h") != std::string::npos)
        return "5h";
    if (name.find("week") != std::string::npos) return "week";
    if (name.find("month") != std::string::npos) return "month";
    return {};
}
