constexpr size_t CLIENT_POOL_MAX_IDLE = 256;
constexpr size_t CLIENT_POOL_MAX_IDLE_PER_ORIGIN = 64;
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
