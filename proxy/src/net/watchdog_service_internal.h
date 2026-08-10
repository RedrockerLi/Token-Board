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
                    if (!watch ||
                        !watch->running.load(std::memory_order_acquire)) {
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
                    if (watch->mark_client_disconnected()) cancel(watch);
                }
            }

            const long long now = now_ms();
            for (const auto &watch : active)
                if (watch->expire_if_due(now) != 0) cancel(watch);
        }
    }

    std::mutex mutex_;
    std::condition_variable cv_;
    std::vector<std::weak_ptr<ForwardWatch>> watches_;
    bool stopping_ = false;
    std::thread worker_;
};

}  // namespace

inline bool is_usage_limit_error(const std::string &body) {
    if (body.empty()) return false;
    return body.find("GoUsageLimitError") != std::string::npos ||
           body.find("\"limitName\"") != std::string::npos;
}
