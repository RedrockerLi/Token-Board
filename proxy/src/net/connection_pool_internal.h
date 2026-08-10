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
            hits_.fetch_add(1, std::memory_order_relaxed);
            return value;
        }
        misses_.fetch_add(1, std::memory_order_relaxed);
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

    void clear() {
        std::lock_guard<std::mutex> lock(mutex_);
        idle_.clear();
    }

    void invalidate_origins(const std::unordered_set<std::string> &origins) {
        if (origins.empty()) return;
        std::lock_guard<std::mutex> lock(mutex_);
        for (auto it = idle_.begin(); it != idle_.end();) {
            if (origins.count(it->value.origin) != 0)
                it = idle_.erase(it);
            else
                ++it;
        }
    }

    void note_created() { created_.fetch_add(1, std::memory_order_relaxed); }
    UpstreamClient::TransportMetrics metrics() const {
        UpstreamClient::TransportMetrics out;
        out.pool_hits = hits_.load(std::memory_order_relaxed);
        out.pool_misses = misses_.load(std::memory_order_relaxed);
        out.clients_created = created_.load(std::memory_order_relaxed);
        return out;
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
    std::atomic<std::uint64_t> hits_{0};
    std::atomic<std::uint64_t> misses_{0};
    std::atomic<std::uint64_t> created_{0};
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
