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
