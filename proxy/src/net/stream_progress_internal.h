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
