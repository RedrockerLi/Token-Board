#include "upstream_client.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cctype>
#include <cstdint>
#include <deque>
#include <exception>
#include <list>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <poll.h>
#include <set>
#include <string_view>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>
#ifdef _WIN32
#include <process.h>
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#include <arpa/inet.h>
#include <fcntl.h>
#include <netdb.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

#define CPPHTTPLIB_OPENSSL_SUPPORT
#include "httplib.h"

namespace {

/// A positive select() read-timeout value representing "no timeout".  httplib
/// interprets a 0 read timeout as a non-blocking poll (select with {0,0}),
/// which would fail every read immediately, so "disabled" uses a day.
constexpr int NO_TIMEOUT_SECS = 24 * 3600;
constexpr int DEFAULT_CONNECT_TIMEOUT_SECS = 10;
constexpr int64_t DNS_SUCCESS_TTL_MS = 60 * 1000;
constexpr int64_t DNS_FAILURE_TTL_MS = 5 * 1000;
constexpr int64_t DNS_ADDRESS_BACKOFF_MS = 15 * 1000;
constexpr size_t DNS_CACHE_MAX_HOSTS = 256;
constexpr size_t DNS_QUEUE_MAX = 64;
constexpr size_t DNS_WORKER_COUNT = 4;
constexpr size_t CLIENT_POOL_MAX_IDLE = 64;
constexpr size_t CLIENT_POOL_MAX_IDLE_PER_ORIGIN = 8;
constexpr int64_t CLIENT_POOL_IDLE_TTL_MS = 60 * 1000;

long long now_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::steady_clock::now().time_since_epoch()).count();
}

using WatchSocket = ::socket_t;

WatchSocket duplicate_socket(WatchSocket sock) {
#ifdef _WIN32
    WSAPROTOCOL_INFOW info{};
    if (WSADuplicateSocketW(sock, static_cast<DWORD>(_getpid()), &info) != 0)
        return INVALID_SOCKET;
    return WSASocketW(FROM_PROTOCOL_INFO, FROM_PROTOCOL_INFO,
                      FROM_PROTOCOL_INFO, &info, 0,
                      WSA_FLAG_OVERLAPPED | WSA_FLAG_NO_HANDLE_INHERIT);
#else
#ifdef F_DUPFD_CLOEXEC
    return ::fcntl(sock, F_DUPFD_CLOEXEC, 0);
#else
    int duplicate = ::dup(sock);
    if (duplicate >= 0) ::fcntl(duplicate, F_SETFD, FD_CLOEXEC);
    return duplicate;
#endif
#endif
}

void close_socket_copy(WatchSocket sock) {
    if (sock == INVALID_SOCKET) return;
#ifdef _WIN32
    ::closesocket(sock);
#else
    ::close(sock);
#endif
}

void shutdown_socket_copy(WatchSocket sock) {
    if (sock == INVALID_SOCKET) return;
#ifdef _WIN32
    ::shutdown(sock, SD_BOTH);
#else
    ::shutdown(sock, SHUT_RDWR);
#endif
}

int64_t timeout_ms(int seconds) {
    return seconds > 0 ? static_cast<int64_t>(seconds) * 1000 : 0;
}

int64_t earlier_deadline(int64_t first, int64_t second) {
    if (first == 0) return second;
    if (second == 0) return first;
    return std::min(first, second);
}

int timeout_seconds_for_report(int configured_seconds,
                               int64_t budget_ms = 0) {
    if (budget_ms <= 0) return configured_seconds;
    const int budget_seconds = static_cast<int>(std::max<int64_t>(
        1, (budget_ms + 999) / 1000));
    return configured_seconds > 0
        ? std::min(configured_seconds, budget_seconds) : budget_seconds;
}

/// Connection-establishment timeout choice.  When an attempt budget is set,
/// connection establishment shares that budget (capped by the conservative
/// connect cap so a wedged peer cannot burn the whole budget); otherwise the
/// fixed default applies.  This lets the connection timeout participate in the
/// same attempt budget as first byte / total response instead of a hard-coded
/// 10-second cap terminating a request earlier than its configured deadline.
struct TimeoutChoice {
    int seconds = 0;
};

TimeoutChoice connection_timeout_for(bool streaming,
                                     const ForwardOptions &opts) {
    (void)streaming;  // reserved: per-path tuning if needed later
    if (opts.attempt_budget_ms > 0) {
        const int seconds = static_cast<int>(
            std::max<int64_t>(1, opts.attempt_budget_ms / 1000));
        return {std::min(seconds, DEFAULT_CONNECT_TIMEOUT_SECS)};
    }
    return {DEFAULT_CONNECT_TIMEOUT_SECS};
}

struct OriginParts {
    std::string scheme;
    std::string hostname;
    std::string origin;
    bool valid = false;
};

OriginParts parse_origin(const std::string &origin) {
    OriginParts out;
    const size_t scheme_pos = origin.find("://");
    if (scheme_pos == std::string::npos) return out;
    out.scheme = origin.substr(0, scheme_pos);
    std::transform(out.scheme.begin(), out.scheme.end(), out.scheme.begin(),
                   [](unsigned char c) {
                       return static_cast<char>(std::tolower(c));
                   });
    if (out.scheme != "http" && out.scheme != "https") return out;

    const size_t authority = scheme_pos + 3;
    if (authority >= origin.size()) return out;
    std::string host_port = origin.substr(authority);
    if (host_port.empty() || host_port.find('/') != std::string::npos ||
        host_port.find('@') != std::string::npos)
        return out;

    std::string port;
    if (host_port.front() == '[') {
        const size_t close = host_port.find(']');
        if (close == std::string::npos) return out;
        out.hostname = host_port.substr(1, close - 1);
        if (close + 1 < host_port.size()) {
            if (host_port[close + 1] != ':') return out;
            port = host_port.substr(close + 2);
            if (port.empty()) return out;
        }
    } else {
        const size_t colon = host_port.rfind(':');
        if (colon != std::string::npos &&
            host_port.find(':') == colon) {
            out.hostname = host_port.substr(0, colon);
            port = host_port.substr(colon + 1);
            if (port.empty()) return out;
        } else {
            out.hostname = host_port;
        }
    }
    if (out.hostname.empty()) return out;
    if (!port.empty() &&
        !std::all_of(port.begin(), port.end(), [](unsigned char c) {
            return std::isdigit(c) != 0;
        }))
        return out;
    std::transform(out.hostname.begin(), out.hostname.end(),
                   out.hostname.begin(), [](unsigned char c) {
                       return static_cast<char>(std::tolower(c));
                   });
    const bool ipv6 = out.hostname.find(':') != std::string::npos;
    out.origin = out.scheme + "://" +
        (ipv6 ? "[" + out.hostname + "]" : out.hostname) +
        (port.empty() ? std::string() : ":" + port);
    out.valid = true;
    return out;
}

int64_t deadline_after(int64_t started_ms, int64_t delay_ms) {
    if (delay_ms <= 0) return 0;
    if (delay_ms > std::numeric_limits<int64_t>::max() - started_ms)
        return std::numeric_limits<int64_t>::max();
    return started_ms + delay_ms;
}

bool numeric_host(const std::string &host) {
    struct in_addr ipv4{};
    struct in6_addr ipv6{};
    return ::inet_pton(AF_INET, host.c_str(), &ipv4) == 1 ||
           ::inet_pton(AF_INET6, host.c_str(), &ipv6) == 1;
}

std::string_view trim_ascii(std::string_view value) {
    while (!value.empty() &&
           std::isspace(static_cast<unsigned char>(value.front())))
        value.remove_prefix(1);
    while (!value.empty() &&
           std::isspace(static_cast<unsigned char>(value.back())))
        value.remove_suffix(1);
    return value;
}

std::string lower_ascii(std::string_view value) {
    std::string out;
    out.reserve(value.size());
    for (char c : value)
        out.push_back(static_cast<char>(
            std::tolower(static_cast<unsigned char>(c))));
    return out;
}

bool heartbeat_name(std::string_view value) {
    const std::string lower = lower_ascii(trim_ascii(value));
    return lower == "ping" || lower == "heartbeat" ||
           lower == "keepalive" || lower == "keep-alive";
}

bool heartbeat_payload(std::string_view value) {
    value = trim_ascii(value);
    const std::string lower = lower_ascii(value);
    if (lower.empty() || lower == "ping" || lower == "heartbeat" ||
        lower == "[ping]" || lower == "[heartbeat]")
        return true;

    // Common JSON heartbeat frames used by Anthropic-compatible providers.
    // Restrict the heuristic to small payloads and an explicit type field so
    // ordinary token text containing the word "ping" is never discarded.
    if (lower.size() <= 256 &&
        lower.find("\"type\"") != std::string::npos &&
        (lower.find("\"ping\"") != std::string::npos ||
         lower.find("\"heartbeat\"") != std::string::npos))
        return true;
    return false;
}

/// Classifies complete SSE records.  It is only a compatibility fallback for
/// callers which do not provide ForwardOptions::semantic_progress; exact
/// semantic progress must come from the format parser's shared counter.
class SseProgressFallback {
public:
    bool feed(const char *data, size_t len) {
        pending_.append(data, len);
        bool progress = false;
        size_t consumed = 0;
        for (;;) {
            const size_t newline = pending_.find('\n', consumed);
            if (newline == std::string::npos) break;
            std::string_view line(pending_.data() + consumed,
                                  newline - consumed);
            if (!line.empty() && line.back() == '\r') line.remove_suffix(1);
            progress = process_line(line) || progress;
            consumed = newline + 1;
        }
        if (consumed != 0) pending_.erase(0, consumed);

        // A single valid data line can be large.  Treat it as progress instead
        // of allowing the classifier's own scratch buffer to become unbounded.
        if (pending_.size() > 64 * 1024) {
            pending_.clear();
            event_name_.clear();
            progress = true;
        }
        return progress;
    }

private:
    bool process_line(std::string_view line) {
        if (line.empty()) {
            event_name_.clear();
            return false;
        }
        if (line.front() == ':') return false;

        const size_t colon = line.find(':');
        if (colon == std::string_view::npos) return true;
        const std::string field = lower_ascii(trim_ascii(line.substr(0, colon)));
        std::string_view value = line.substr(colon + 1);
        if (!value.empty() && value.front() == ' ') value.remove_prefix(1);

        if (field == "event") {
            event_name_ = lower_ascii(trim_ascii(value));
            return false;
        }
        if (field == "data")
            return !heartbeat_name(event_name_) && !heartbeat_payload(value);
        if (field == "id" || field == "retry") return false;

        // This is not an SSE control field (for example, newline-delimited
        // JSON), so receiving it is application progress.
        return true;
    }

    std::string pending_;
    std::string event_name_;
};

/// Bounded streaming tail without repeatedly moving one large std::string.
/// Whole chunks are discarded from the front; at most one front chunk is
/// trimmed per append, so long streams stay linear in bytes received.
class BoundedTailBuffer {
public:
    explicit BoundedTailBuffer(size_t limit) : limit_(limit) {}

    void append(const char *data, size_t len) {
        if (len == 0) return;
        if (limit_ != 0 && len >= limit_) {
            chunks_.clear();
            chunks_.emplace_back(data + (len - limit_), limit_);
            size_ = limit_;
            truncated_ = true;
            return;
        }
        constexpr size_t CHUNK_SIZE = 16 * 1024;
        while (len != 0) {
            if (chunks_.empty() || chunks_.back().size() == CHUNK_SIZE)
                chunks_.emplace_back();
            const size_t copy = std::min(
                len, CHUNK_SIZE - chunks_.back().size());
            chunks_.back().append(data, copy);
            data += copy;
            len -= copy;
            size_ += copy;
        }
        if (limit_ == 0 || size_ <= limit_) return;

        truncated_ = true;
        size_t discard = size_ - limit_;
        while (!chunks_.empty() && discard >= chunks_.front().size()) {
            discard -= chunks_.front().size();
            size_ -= chunks_.front().size();
            chunks_.pop_front();
        }
        if (discard != 0 && !chunks_.empty()) {
            chunks_.front().erase(0, discard);
            size_ -= discard;
        }
    }

    std::string take() {
        std::string out;
        out.reserve(size_);
        for (const auto &chunk : chunks_) out.append(chunk);
        chunks_.clear();
        size_ = 0;
        return out;
    }

    bool truncated() const noexcept { return truncated_; }

private:
    size_t limit_ = 0;
    size_t size_ = 0;
    bool truncated_ = false;
    std::deque<std::string> chunks_;
};

struct DnsResolution {
    enum class Status { Ok, Failed, TimedOut, Canceled, Saturated };
    Status status = Status::Failed;
    std::vector<std::string> addresses;
    std::string error;
};

/// Fixed-size asynchronous resolver with per-host single-flight and TTL cache.
/// libc getaddrinfo() is not portably cancellable, so a hung NSS backend may
/// occupy one of the fixed workers, but it can never create unbounded threads,
/// queue entries, or block a request beyond that request's own deadline.
class DnsResolver {
public:
    static DnsResolver &instance() {
        // Deliberately process-lifetime: joining a worker stuck inside an
        // uncancellable NSS module would otherwise hang graceful shutdown.
        static DnsResolver *resolver = new DnsResolver();
        return *resolver;
    }

    template <typename Canceled>
    DnsResolution resolve(const std::string &host, int64_t deadline_ms,
                          Canceled &&canceled) {
        if (numeric_host(host)) {
            return DnsResolution{DnsResolution::Status::Ok, {host}, {}};
        }

        auto entry = entry_for(host);
        if (!entry) {
            return DnsResolution{DnsResolution::Status::Saturated, {},
                                 "DNS cache is saturated"};
        }

        std::unique_lock<std::mutex> lock(entry->mutex);
        for (;;) {
            const int64_t now = now_ms();
            entry->last_access_ms = now;
            if (canceled()) {
                return DnsResolution{DnsResolution::Status::Canceled, {},
                                     "DNS wait canceled"};
            }
            if (deadline_ms != 0 && now >= deadline_ms) {
                return DnsResolution{DnsResolution::Status::TimedOut, {},
                                     "DNS deadline exceeded"};
            }

            if (now < entry->expires_ms) {
                if (entry->addresses.empty()) {
                    return DnsResolution{DnsResolution::Status::Failed, {},
                                         entry->error.empty()
                                             ? "DNS lookup failed"
                                             : entry->error};
                }

                std::vector<std::string> available;
                available.reserve(entry->addresses.size());
                const size_t count = entry->addresses.size();
                const size_t start = entry->next_address++ % count;
                for (size_t i = 0; i < count; ++i) {
                    const auto &address = entry->addresses[(start + i) % count];
                    auto failed = entry->failed_until_ms.find(address);
                    if (failed == entry->failed_until_ms.end() ||
                        now >= failed->second)
                        available.push_back(address);
                }
                if (!available.empty()) {
                    return DnsResolution{DnsResolution::Status::Ok,
                                         std::move(available), {}};
                }
                // mark_failed() expires the entry when its last usable
                // address fails, so reaching this state after a refresh means
                // DNS returned only endpoints which are still in backoff.
                // Do not create an unbounded refresh loop.
                return DnsResolution{
                    DnsResolution::Status::Failed, {},
                    "All DNS addresses are temporarily unavailable"};
            }

            if (!entry->resolving) {
                entry->resolving = true;
                if (!enqueue(host, entry)) {
                    entry->resolving = false;
                    return DnsResolution{DnsResolution::Status::Saturated, {},
                                         "DNS resolver queue is saturated"};
                }
            }

            int64_t wait_ms = 10;
            if (deadline_ms != 0)
                wait_ms = std::max<int64_t>(
                    1, std::min<int64_t>(wait_ms, deadline_ms - now));
            entry->cv.wait_for(lock, std::chrono::milliseconds(wait_ms));
        }
    }

    void mark_failed(const std::string &host, const std::string &address) {
        if (host.empty() || address.empty() || numeric_host(host)) return;
        std::shared_ptr<Entry> entry;
        {
            std::lock_guard<std::mutex> lock(cache_mutex_);
            auto it = cache_.find(host);
            if (it == cache_.end()) return;
            entry = it->second;
        }
        std::lock_guard<std::mutex> lock(entry->mutex);
        entry->failed_until_ms[address] = now_ms() + DNS_ADDRESS_BACKOFF_MS;
        if (!entry->addresses.empty() &&
            std::all_of(entry->addresses.begin(), entry->addresses.end(),
                        [&](const std::string &candidate) {
                            auto it = entry->failed_until_ms.find(candidate);
                            return it != entry->failed_until_ms.end() &&
                                   now_ms() < it->second;
                        })) {
            // Force one authoritative refresh in case the provider rotated
            // its addresses. If it returns the same failed set, resolve()
            // reports the backoff instead of refreshing forever.
            entry->expires_ms = 0;
        }
    }

    void mark_success(const std::string &host, const std::string &address) {
        if (host.empty() || address.empty() || numeric_host(host)) return;
        std::shared_ptr<Entry> entry;
        {
            std::lock_guard<std::mutex> lock(cache_mutex_);
            auto it = cache_.find(host);
            if (it == cache_.end()) return;
            entry = it->second;
        }
        std::lock_guard<std::mutex> lock(entry->mutex);
        entry->failed_until_ms.erase(address);
    }

private:
    struct Entry {
        std::mutex mutex;
        std::condition_variable cv;
        std::vector<std::string> addresses;
        std::unordered_map<std::string, int64_t> failed_until_ms;
        std::string error;
        int64_t expires_ms = 0;
        int64_t last_access_ms = 0;
        size_t next_address = 0;
        bool resolving = false;
    };

    struct Job {
        std::string host;
        std::shared_ptr<Entry> entry;
    };

    DnsResolver() {
        for (size_t i = 0; i < DNS_WORKER_COUNT; ++i)
            std::thread([this] { worker_loop(); }).detach();
    }

    std::shared_ptr<Entry> entry_for(const std::string &host) {
        std::lock_guard<std::mutex> lock(cache_mutex_);
        auto found = cache_.find(host);
        if (found != cache_.end()) return found->second;

        if (cache_.size() >= DNS_CACHE_MAX_HOSTS) {
            auto victim = cache_.end();
            int64_t oldest = std::numeric_limits<int64_t>::max();
            for (auto it = cache_.begin(); it != cache_.end(); ++it) {
                if (it->second.use_count() != 1) continue;
                std::unique_lock<std::mutex> entry_lock(it->second->mutex,
                                                       std::try_to_lock);
                if (!entry_lock || it->second->resolving) continue;
                if (it->second->last_access_ms < oldest) {
                    oldest = it->second->last_access_ms;
                    victim = it;
                }
            }
            if (victim == cache_.end()) return nullptr;
            cache_.erase(victim);
        }

        auto entry = std::make_shared<Entry>();
        entry->last_access_ms = now_ms();
        cache_.emplace(host, entry);
        return entry;
    }

    bool enqueue(const std::string &host,
                 const std::shared_ptr<Entry> &entry) {
        {
            std::lock_guard<std::mutex> lock(queue_mutex_);
            if (jobs_.size() >= DNS_QUEUE_MAX) return false;
            jobs_.push_back(Job{host, entry});
        }
        queue_cv_.notify_one();
        return true;
    }

    static std::pair<std::vector<std::string>, std::string>
    lookup(const std::string &host) {
        struct addrinfo hints{};
        hints.ai_family = AF_UNSPEC;
        hints.ai_socktype = SOCK_STREAM;
        hints.ai_protocol = IPPROTO_TCP;
        struct addrinfo *raw = nullptr;
        const int rc = ::getaddrinfo(host.c_str(), nullptr, &hints, &raw);
        if (rc != 0) {
            return {{}, std::string("DNS lookup failed: ") +
                         ::gai_strerror(rc)};
        }

        std::vector<std::string> addresses;
        std::unordered_set<std::string> seen;
        for (auto *it = raw; it; it = it->ai_next) {
            char text[NI_MAXHOST]{};
            if (::getnameinfo(it->ai_addr, static_cast<socklen_t>(it->ai_addrlen),
                              text, sizeof(text), nullptr, 0,
                              NI_NUMERICHOST) == 0 && seen.insert(text).second)
                addresses.emplace_back(text);
        }
        ::freeaddrinfo(raw);
        if (addresses.empty())
            return {{}, "DNS lookup returned no usable address"};
        return {std::move(addresses), {}};
    }

    void worker_loop() {
        for (;;) {
            Job job;
            {
                std::unique_lock<std::mutex> lock(queue_mutex_);
                queue_cv_.wait(lock, [&] { return !jobs_.empty(); });
                job = std::move(jobs_.front());
                jobs_.pop_front();
            }

            auto resolved = lookup(job.host);
            {
                std::lock_guard<std::mutex> lock(job.entry->mutex);
                job.entry->addresses = std::move(resolved.first);
                job.entry->error = std::move(resolved.second);
                std::unordered_set<std::string> current(
                    job.entry->addresses.begin(), job.entry->addresses.end());
                for (auto it = job.entry->failed_until_ms.begin();
                     it != job.entry->failed_until_ms.end();) {
                    if (current.find(it->first) == current.end())
                        it = job.entry->failed_until_ms.erase(it);
                    else
                        ++it;
                }
                job.entry->expires_ms = now_ms() +
                    (job.entry->addresses.empty() ? DNS_FAILURE_TTL_MS
                                                  : DNS_SUCCESS_TTL_MS);
                job.entry->next_address = 0;
                job.entry->resolving = false;
            }
            job.entry->cv.notify_all();
        }
    }

    std::mutex cache_mutex_;
    std::unordered_map<std::string, std::shared_ptr<Entry>> cache_;
    std::mutex queue_mutex_;
    std::condition_variable queue_cv_;
    std::deque<Job> jobs_;
};

struct PooledClient {
    std::unique_ptr<httplib::Client> client;
    std::string origin;
    std::string hostname;
    std::string address;
};

/// Idle-client pool. A client is removed while leased, so cpp-httplib's
/// single-connection object is never used concurrently by two forwards.
class ClientPool {
public:
    static ClientPool &instance() {
        static ClientPool pool;
        return pool;
    }

    std::optional<PooledClient> take(const std::string &origin) {
        std::lock_guard<std::mutex> lock(mutex_);
        prune_locked(now_ms());
        for (auto it = idle_.begin(); it != idle_.end(); ++it) {
            if (it->value.origin != origin) continue;
            PooledClient value = std::move(it->value);
            idle_.erase(it);
            return value;
        }
        return std::nullopt;
    }

    void put(PooledClient value) {
        if (!value.client) return;
        std::lock_guard<std::mutex> lock(mutex_);
        const int64_t now = now_ms();
        prune_locked(now);

        size_t same_origin = 0;
        for (const auto &idle : idle_)
            if (idle.value.origin == value.origin) ++same_origin;
        if (same_origin >= CLIENT_POOL_MAX_IDLE_PER_ORIGIN) return;

        while (idle_.size() >= CLIENT_POOL_MAX_IDLE) idle_.pop_front();
        idle_.push_back(Idle{std::move(value), now});
    }

    void invalidate(const std::string &origin, const std::string &address) {
        std::lock_guard<std::mutex> lock(mutex_);
        for (auto it = idle_.begin(); it != idle_.end();) {
            if (it->value.origin == origin && it->value.address == address)
                it = idle_.erase(it);
            else
                ++it;
        }
    }

private:
    struct Idle {
        PooledClient value;
        int64_t returned_ms = 0;
    };

    void prune_locked(int64_t now) {
        for (auto it = idle_.begin(); it != idle_.end();) {
            if (now - it->returned_ms >= CLIENT_POOL_IDLE_TTL_MS)
                it = idle_.erase(it);
            else
                ++it;
        }
    }

    std::mutex mutex_;
    std::list<Idle> idle_;
};

/// RAII lease for a pooled client.  The client leaves the pool while leased,
/// gets its per-request configuration re-applied, and returns on scope exit.
/// Call discard() when the connection is suspect (timeout / cancel / connect
/// failure) instead of returning it.
class PooledLease {
public:
    explicit PooledLease(std::optional<PooledClient> value)
        : value_(std::move(value)) {}
    ~PooledLease() { release(); }
    PooledLease(PooledLease &&other) noexcept : value_(std::move(other.value_)) {}
    PooledLease &operator=(PooledLease &&other) noexcept {
        release();
        value_ = std::move(other.value_);
        return *this;
    }
    PooledLease(const PooledLease &) = delete;
    PooledLease &operator=(const PooledLease &) = delete;

    httplib::Client *client() const {
        return value_ ? value_->client.get() : nullptr;
    }
    const std::string &address() const { return value_->address; }
    bool valid() const { return value_ && value_->client; }

    void discard() { value_.reset(); }

private:
    void release() {
        if (!value_ || !value_->client) return;
        ClientPool::instance().put(std::move(*value_));
        value_.reset();
    }
    std::optional<PooledClient> value_;
};

std::optional<PooledClient>
make_client(const OriginParts &origin, const std::string &address,
            std::string &error) {
    try {
        auto client = std::make_unique<httplib::Client>(origin.origin);
        if (!client->is_valid()) {
            error = "Invalid upstream origin";
            return std::nullopt;
        }
        // The transport dials the resolver-selected numeric address, while the
        // Client/SSLClient retains the original hostname for Host, SNI and
        // certificate hostname verification.
        client->set_hostname_addr_map({{origin.hostname, address}});
        client->set_keep_alive(true);
        // httplib defaults TCP_NODELAY to OFF (CPPHTTPLIB_TCP_NODELAY=false).
        // On a reused keep-alive connection, Nagle + delayed ACK stalls each
        // small request/response round-trip for ~40ms on Linux loopback and
        // similarly on real networks; a proxy must never pay that per request.
        client->set_tcp_nodelay(true);
        client->enable_server_certificate_verification(true);
        client->enable_server_hostname_verification(true);
        return PooledClient{std::move(client), origin.origin,
                            origin.hostname, address};
    } catch (const std::exception &ex) {
        error = ex.what();
        return std::nullopt;
    }
}

/// State registered with the one process-wide watchdog.  Individual forwards
/// do not create timer/client-monitor threads: the service polls every active
/// downstream socket in one poll() call and enforces all upstream deadlines.
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
        cv_.notify_one();
    }

    void changed() { cv_.notify_one(); }

private:
    ForwardWatchdog() : worker_([this] { run(); }) {}
    ~ForwardWatchdog() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            stopping_ = true;
        }
        cv_.notify_one();
        if (worker_.joinable()) worker_.join();
    }

    static void cancel(const std::shared_ptr<ForwardWatch> &watch) {
        watch->cancel();
    }

    void run() {
        for (;;) {
            std::vector<std::shared_ptr<ForwardWatch>> active;
            {
                std::unique_lock<std::mutex> lock(mutex_);
                cv_.wait_for(lock, std::chrono::milliseconds(5));
                if (stopping_) return;
                for (auto it = watches_.begin(); it != watches_.end();) {
                    auto watch = it->lock();
                    if (!watch || !watch->running.load(std::memory_order_acquire)) {
                        it = watches_.erase(it);
                    } else {
                        active.push_back(std::move(watch));
                        ++it;
                    }
                }
            }

            std::vector<struct pollfd> downstream;
            std::vector<size_t> downstream_owner;
            downstream.reserve(active.size());
            downstream_owner.reserve(active.size());
            for (size_t i = 0; i < active.size(); ++i) {
                if (active[i]->downstream_socket < 0) continue;
                struct pollfd pfd{};
                pfd.fd = active[i]->downstream_socket;
                pfd.events = POLLIN;
                downstream.push_back(pfd);
                downstream_owner.push_back(i);
            }
            if (!downstream.empty() &&
                ::poll(downstream.data(), downstream.size(), 0) > 0) {
                for (size_t i = 0; i < downstream.size(); ++i) {
                    if (!(downstream[i].revents &
                          (POLLHUP | POLLERR | POLLNVAL))) continue;
                    auto &watch = active[downstream_owner[i]];
                    // POLLRDHUP only means the peer closed its write half; it
                    // may still be reading our response.  HUP/ERR/NVAL are the
                    // actionable full-disconnect/error conditions here.
                    if (watch->mark_client_disconnected()) cancel(watch);
                }
            }

            const long long now = now_ms();
            for (const auto &watch : active) {
                if (watch->expire_if_due(now) != 0) cancel(watch);
            }
        }
    }

    std::mutex mutex_;
    std::condition_variable cv_;
    std::vector<std::weak_ptr<ForwardWatch>> watches_;
    bool stopping_ = false;
    std::thread worker_;
};

}  // namespace

UpstreamClient::ForwardResult
UpstreamClient::forward(const std::string &method,
                        const std::string &base_url,
                        const std::string &upstream_key,
                        const std::string &path,
                        const std::string &body,
                        const std::string &content_type,
                        std::function<bool(const char *, size_t)> on_chunk,
                        const ForwardOptions &opts) {
    ForwardResult result;
    auto t0 = std::chrono::steady_clock::now();

    // Parse base URL into scheme + host components
    // base_url is e.g. "https://uni-api.cstcloud.cn/v1"
    std::string scheme_host;   // "https://uni-api.cstcloud.cn"
    std::string url_path;      // "/v1"

    size_t scheme_end = base_url.find("://");
    if (scheme_end == std::string::npos) {
        result.status_code = 502;
        result.error = "Invalid base_url: no scheme";
        return result;
    }
    scheme_end += 3;  // past "://"
    if (scheme_end >= base_url.size()) {
        result.status_code = 502;
        result.error = "Invalid base_url: missing host";
        return result;
    }

    size_t path_start = base_url.find('/', scheme_end);
    if (path_start == std::string::npos) {
        scheme_host = base_url;
        url_path = "";
    } else {
        scheme_host = base_url.substr(0, path_start);
        url_path = base_url.substr(path_start);
    }

    // Build the full upstream path.  path_is_full bypasses base_url's path
    // component (used for explicit endpoint_path overrides).
    std::string full_path = opts.path_is_full ? path : url_path + path;
    const bool streaming = static_cast<bool>(on_chunk);
    const long long deadline_started_ms = now_ms();
    const TimeoutChoice connection_timeout =
        connection_timeout_for(streaming, opts);

    // Normalized origin: the connection pool and DNS cache are keyed by it.
    // parse_origin is stricter than the scheme check above; reject anything it
    // cannot represent (unexpected characters, non-http(s) schemes).
    const OriginParts origin_parts = parse_origin(scheme_host);
    if (!origin_parts.valid) {
        result.status_code = 502;
        result.error = "Invalid upstream origin";
        return result;
    }

    auto watch = std::make_shared<ForwardWatch>();
    watch->semantic_seen = opts.semantic_seen;
    watch->semantic_progress = opts.semantic_progress;
    watch->terminal_seen = opts.terminal_seen;
    watch->downstream_socket = opts.downstream_socket;

    // Resolve the host once per request via the cached async resolver.  The
    // returned addresses are rotated and filtered against the per-address
    // backoff, so the first usable address is the healthy one.  Numeric hosts
    // short-circuit.  The DNS wait shares the connection-establishment budget.
    std::vector<std::string> dns_addresses;
    if (numeric_host(origin_parts.hostname)) {
        dns_addresses.push_back(origin_parts.hostname);
    } else {
        const int64_t dns_deadline_ms = deadline_after(
            deadline_started_ms, connection_timeout.seconds * 1000LL);
        auto dns = DnsResolver::instance().resolve(
            origin_parts.hostname, dns_deadline_ms,
            [&watch] {
                return !watch->running.load(std::memory_order_acquire) ||
                       watch->expired.load(std::memory_order_acquire);
            });
        switch (dns.status) {
            case DnsResolution::Status::Ok:
                dns_addresses = std::move(dns.addresses);
                break;
            case DnsResolution::Status::TimedOut:
                result.status_code = 504;
                result.is_timeout = true;
                result.timeout_secs = connection_timeout.seconds;
                result.error = "Upstream DNS deadline exceeded";
                return result;
            default:  // Failed / Canceled / Saturated
                result.status_code = 502;
                result.error = dns.error.empty() ? "Upstream DNS lookup failed"
                                                 : dns.error;
                return result;
        }
    }
    if (dns_addresses.empty()) {
        result.status_code = 502;
        result.error = "Upstream DNS returned no usable addresses";
        return result;
    }

    // Build headers.  Anthropic-native upstreams use x-api-key instead of
    // Authorization: Bearer.
    httplib::Headers headers;
    if (opts.auth_scheme == "x-api-key") {
        headers = {
            {"x-api-key", upstream_key},
            {"anthropic-version", "2023-06-01"},
            {"Content-Type", content_type},
        };
    } else {
        headers = {
            {"Authorization", "Bearer " + upstream_key},
            {"Content-Type", content_type},
        };
    }

    // Deadlines are armed once per request; a connect-level retry to another
    // address shares the same absolute budget.
    if (streaming) {
        watch->set_initial_deadlines(deadline_started_ms,
                                     opts.streaming_first_byte_timeout,
                                     opts.streaming_semantic_timeout,
                                     opts.streaming_idle_timeout);
    } else {
        watch->set_initial_deadlines(deadline_started_ms,
                                     opts.non_streaming_total_timeout, 0, 0);
    }
    const bool need_watchdog = streaming
        ? (opts.streaming_first_byte_timeout > 0 ||
           opts.streaming_semantic_timeout > 0 ||
           opts.streaming_idle_timeout > 0)
        : opts.non_streaming_total_timeout > 0;
    const int backstop_sec = streaming
        ? (need_watchdog
               ? std::max({3600, opts.streaming_first_byte_timeout,
                           opts.streaming_semantic_timeout,
                           opts.streaming_idle_timeout})
               : NO_TIMEOUT_SECS)
        : (opts.non_streaming_timeout > 0 ? opts.non_streaming_timeout
                                          : NO_TIMEOUT_SECS);
    if (need_watchdog || opts.downstream_socket >= 0)
        ForwardWatchdog::instance().add(watch);

    // Per-lease configuration: cpp-httplib's Client members retain the previous
    // request's settings, so every lease re-applies timeouts and callbacks.
    auto configure_client = [&](httplib::Client *cli) {
        // Connection establishment participates in the same attempt budget as
        // first byte / total response.  A fixed 10-second cap used to terminate
        // a request earlier than its configured deadline and report the wrong
        // value.
        cli->set_connection_timeout(connection_timeout.seconds, 0);
        cli->set_write_timeout(30, 0);
        cli->enable_server_certificate_verification(true);
        cli->set_read_timeout(backstop_sec, 0);
        cli->set_socket_options([watch](::socket_t sock) {
            // The watchdog owns a duplicate.  If httplib closes its descriptor
            // and the OS immediately reuses that number, a later cancellation
            // can therefore never shutdown an unrelated connection.
            watch->install_socket(sock);
        });
        // On a reused connection no socket_options callback fires (no new
        // socket), so the watchdog cancels through Client::stop() — the client
        // pointer must be current for this lease.
        watch->attach_client(cli);
    };

    auto is_connect_error = [](httplib::Error e) {
        return e == httplib::Error::Connection ||
               e == httplib::Error::SSLConnection ||
               e == httplib::Error::ConnectionTimeout;
    };

    // One send attempt against a leased client.  Fills `result` completely;
    // returns true when the transport never reached the peer (dead address) and
    // another address should be tried.
    auto attempt = [&](httplib::Client &cli) -> bool {
        result = ForwardResult{};
        if (streaming) {
        // ── Streaming path: use Request::content_receiver ─────────
        BoundedTailBuffer accumulated(opts.streaming_body_buffer_limit);
        SseProgressFallback progress_fallback;
        bool client_connected = true;
        bool first_chunk = true;
        std::chrono::steady_clock::time_point t_first;

        // Status captured by response_handler BEFORE the body is read.  The
        // receiver only relays the body to on_chunk for 2xx responses; a
        // non-2xx response (429/5xx — always a complete small error body, the
        // upstream never returns one mid-stream) is buffered into
        // `accumulated` instead, so the caller can fall back to the next
        // candidate without anything having reached the client.
        int upstream_status = 0;

        // ContentReceiverWithProgress: (data, len, offset, total) -> bool
        auto receiver = [&](const char *data, size_t len,
                            uint64_t /*offset*/, uint64_t /*total*/) -> bool {
            const auto received_at = std::chrono::steady_clock::now();
            const long long received_ms = now_ms();
            const bool fallback_progress = opts.semantic_progress
                ? false : progress_fallback.feed(data, len);
            if (!watch->begin_chunk(received_ms, fallback_progress))
                return false;
            if (first_chunk) {
                t_first = received_at;
                first_chunk = false;
            }
            accumulated.append(data, len);
            if (client_connected &&
                upstream_status >= 200 && upstream_status < 300) {
                client_connected = on_chunk(data, len);
            }
            // on_chunk publishes parser-level semantic_seen/progress.  Read
            // those atomics again so the first token arms the semantic-idle
            // deadline immediately; comments and pings never refresh it.
            watch->end_chunk(now_ms(), fallback_progress);
            return client_connected;
        };

        httplib::Request req;
        req.method = method;
        req.path = full_path;
        req.headers = headers;
        req.body = body;
        req.response_handler = [&](const httplib::Response &r) -> bool {
            upstream_status = r.status;
            return true;  // always read the body; the receiver decides
        };
        req.content_receiver = receiver;

        httplib::Response upstream_res;
        httplib::Error err;
        bool ok = cli.send(req, upstream_res, err);

        auto t1 = std::chrono::steady_clock::now();
        result.duration_ms = static_cast<int>(
            std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count());
        if (!first_chunk) {
            result.ttft_ms = static_cast<int>(
                std::chrono::duration_cast<std::chrono::milliseconds>(t_first - t0).count());
        } else {
            result.ttft_ms = result.duration_ms;  // no chunks received
        }
        result.body_truncated = accumulated.truncated();
        result.body = accumulated.take();

        if (watch->client_disconnected.load(std::memory_order_acquire)) {
            result.status_code = 499;
            result.client_disconnected = true;
            result.success = false;
            result.error = "Client disconnected";
        } else if (ok && !watch->expired.load(std::memory_order_acquire)) {
            result.status_code = upstream_res.status;
            // Streaming: a clean EOF before a complete terminal event was
            // published means the provider cut the stream mid-generation.  Do
            // not bill it as a full 2xx success; the caller treats it as a
            // truncated, retryable failure.  Non-streaming calls do not supply
            // terminal_seen, so this never affects them.
            const bool truncated = opts.terminal_seen &&
                !opts.terminal_seen->load(std::memory_order_acquire);
            result.success = (upstream_res.status >= 200 &&
                              upstream_res.status < 300) && !truncated;
            if (!result.success)
                result.error = truncated
                    ? "Upstream stream truncated before terminal event"
                    : "Upstream returned " + std::to_string(upstream_res.status);
        } else {
            result.is_timeout = watch->expired.load(std::memory_order_acquire) ||
                                err == httplib::Error::ConnectionTimeout;
            if (result.is_timeout) {
                switch (watch->expired_reason.load(std::memory_order_acquire)) {
                    case 1:
                        result.timeout_secs = opts.streaming_first_byte_timeout;
                        break;
                    case 2:
                        result.timeout_secs = opts.streaming_semantic_timeout;
                        break;
                    case 3:
                        result.timeout_secs = opts.streaming_idle_timeout;
                        break;
                    default:
                        result.timeout_secs = connection_timeout.seconds;
                        break;
                }
            }
            result.status_code = result.is_timeout ? 504 : 502;
            result.success = false;
            const int reason =
                watch->expired_reason.load(std::memory_order_acquire);
            if (reason == 1)
                result.error = "Upstream first-byte deadline exceeded";
            else if (reason == 2)
                result.error = "Upstream first-semantic deadline exceeded";
            else if (reason == 3)
                result.error =
                    "Upstream semantic-progress idle deadline exceeded";
            else if (err == httplib::Error::ConnectionTimeout)
                result.error = "Upstream connection deadline exceeded";
            else
                result.error = "Upstream request failed: " +
                               std::string(httplib::to_string(err));
            return !ok && !watch->expired.load(std::memory_order_acquire) &&
                   !watch->client_disconnected.load(std::memory_order_acquire) &&
                   is_connect_error(err);
        }
    } else {
        // ── Non-streaming: use GET or POST based on method ─────
        // Bounded by non_streaming_timeout (idle semantics: a read that gets no
        // data for N seconds fails).  NO_TIMEOUT_SECS when disabled (a 0 read
        // timeout would be a non-blocking poll, failing every read instantly).
        httplib::Result upstream_res;
        if (method == "GET") {
            upstream_res = cli.Get(full_path, headers);
        } else {
            upstream_res = cli.Post(full_path, headers, body, content_type.c_str());
        }

        auto t1 = std::chrono::steady_clock::now();
        result.duration_ms = static_cast<int>(
            std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count());
        result.ttft_ms = result.duration_ms;  // non-streaming: no first-token concept

        if (watch->client_disconnected.load(std::memory_order_acquire)) {
            result.status_code = 499;
            result.client_disconnected = true;
            result.success = false;
            result.error = "Client disconnected";
        } else if (upstream_res &&
                   !watch->expired.load(std::memory_order_acquire)) {
            result.status_code = upstream_res->status;
            result.body = upstream_res->body;
            result.success = (upstream_res->status >= 200 && upstream_res->status < 300);
            if (opts.non_streaming_body_limit > 0 &&
                result.body.size() > opts.non_streaming_body_limit) {
                // The response exceeded the documented hard limit: treat it as
                // a failure, not a success, and do not hand a huge blob back.
                result.body_too_large = true;
                result.success = false;
                result.body.clear();
                result.error = "Upstream response exceeded the non-streaming body limit";
            } else if (!result.success) {
                result.error = "Upstream returned " + std::to_string(upstream_res->status);
            }
        } else {
            // cpp-httplib reports an idle read timeout as Error::Read.  The
            // process-wide watchdog can observe the same deadline a few
            // milliseconds later, so classify a read failure at the configured
            // boundary as 504 instead of racing into a misleading 502.
            const bool idle_read_timeout =
                upstream_res.error() == httplib::Error::Read &&
                opts.non_streaming_timeout > 0 &&
                result.duration_ms + 100 >= opts.non_streaming_timeout * 1000;
            result.is_timeout = watch->expired.load(std::memory_order_acquire) ||
                                upstream_res.error() ==
                                    httplib::Error::ConnectionTimeout ||
                                idle_read_timeout;
            if (watch->expired.load(std::memory_order_acquire))
                result.timeout_secs = opts.non_streaming_total_timeout;
            else if (upstream_res.error() == httplib::Error::ConnectionTimeout)
                result.timeout_secs = connection_timeout.seconds;
            else if (idle_read_timeout)
                result.timeout_secs = opts.non_streaming_timeout;
            result.status_code = result.is_timeout ? 504 : 502;
            result.success = false;
            if (watch->expired.load(std::memory_order_acquire))
                result.error = "Upstream total deadline exceeded";
            else if (upstream_res.error() == httplib::Error::ConnectionTimeout)
                result.error = "Upstream connection deadline exceeded";
            else if (idle_read_timeout)
                result.error = "Upstream read-idle deadline exceeded";
            else
                result.error = "Upstream request failed: " +
                    std::string(httplib::to_string(upstream_res.error()));
            return !upstream_res &&
                   !watch->expired.load(std::memory_order_acquire) &&
                   !watch->client_disconnected.load(std::memory_order_acquire) &&
                   is_connect_error(upstream_res.error());
        }
    }
        return false;
    };

    // Leased client acquisition: prefer an idle pooled client; otherwise dial a
    // fresh one for the next untried DNS address.
    std::set<std::string> tried_addresses;
    size_t addr_index = 0;
    auto acquire = [&]() -> PooledLease {
        if (auto pooled = ClientPool::instance().take(origin_parts.origin))
            return PooledLease(std::move(*pooled));
        for (size_t i = addr_index; i < dns_addresses.size(); ++i) {
            if (tried_addresses.count(dns_addresses[i])) continue;
            std::string error;
            auto made = make_client(origin_parts, dns_addresses[i], error);
            if (made) return PooledLease(std::move(*made));
        }
        return PooledLease(std::nullopt);
    };

    PooledLease lease(std::nullopt);
    for (;;) {
        if (!lease.valid()) {
            lease = acquire();
            if (!lease.valid()) {
                result.status_code = 502;
                result.error = "Unable to establish upstream client";
                return result;
            }
            configure_client(lease.client());
        }
        const bool retry = attempt(*lease.client());
        if (retry && addr_index < dns_addresses.size()) {
            const std::string &dead = lease.address();
            tried_addresses.insert(dead);
            DnsResolver::instance().mark_failed(origin_parts.hostname, dead);
            ClientPool::instance().invalidate(origin_parts.origin, dead);
            // No dangling cancel target while the watchdog may still poll this
            // watch between the discard and the next acquire.
            watch->attach_client(nullptr);
            lease.discard();
            while (addr_index < dns_addresses.size() &&
                   tried_addresses.count(dns_addresses[addr_index]))
                ++addr_index;
            if (addr_index < dns_addresses.size()) continue;
        }
        break;
    }

    watch->finish();
    ForwardWatchdog::instance().changed();

    if (result.success && !numeric_host(origin_parts.hostname) && lease.valid())
        DnsResolver::instance().mark_success(origin_parts.hostname,
                                             lease.address());

    return result;
}
